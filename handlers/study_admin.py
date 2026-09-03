"""
Админка «Контроль обучения».

Разделы: аналитика, отстающие ученики с фильтрами и карточками, настройки
напоминаний, шаблоны писем, мотивационные материалы, инструкция после
выдачи Премиума и контакт поддержки.

Все тексты и сроки живут в базе — здесь только экраны для их правки.
"""
import asyncio
import json
import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database as db
import utils
from filters import IsAdmin
from services import motivation_service as ms
from services import onboarding_service as onb
from services import study_reminders as sr
from services import study_settings as ss
from services import study_tracker as st

router = Router(name="study_admin")
log = logging.getLogger(__name__)

PAGE = 6


class StudyStates(StatesGroup):
    waiting_setting = State()      # новое значение настройки
    waiting_template = State()     # новый текст шаблона
    waiting_motivation = State()   # материал мотивации
    waiting_import = State()       # файл импорта мотивашек
    waiting_block = State()        # блок инструкции
    waiting_support = State()      # ник менеджера / текст поддержки
    waiting_broadcast = State()    # сообщение выбранным ученикам


def _with_data(call: CallbackQuery, data: str):
    """Перерисовать другой экран: объекты aiogram менять нельзя."""
    return type('F', (), {'data': data, 'message': call.message,
                          'from_user': call.from_user, 'bot': call.bot,
                          'answer': call.answer})()


async def _show(call: CallbackQuery, text: str, kb):
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        try:
            await call.message.answer(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass


# ================= Главное меню раздела =================

@router.callback_query(F.data == "adm:study", IsAdmin())
async def cb_study_home(call: CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
    groups = await asyncio.to_thread(sr.summary)
    on = ss.get_bool("study_enabled")

    text = (
        "🎯 <b>Контроль обучения</b>\n\n"
        f"Слежение: {'✅ включено' if on else '⛔️ выключено'}\n"
        f"Пишем: {'только Премиум' if ss.get_bool('study_premium_only') else 'всем'}\n\n"
        + sr.summary_text(groups) +
        "\n\n<i>Бот смотрит, что ученик реально делал: открыл конспект, начал "
        "ДЗ, сдал ДЗ. Просто заход в бота учёбой не считается.</i>"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="⚠️ Отстающие ученики", callback_data="lag:list:all:0")
    kb.button(text="📊 Учебная аналитика", callback_data="study:stats")
    kb.button(text="🔔 Напоминания и сроки", callback_data="study:settings")
    kb.button(text="📝 Шаблоны сообщений", callback_data="study:tpl")
    kb.button(text="🔥 Мотивация", callback_data="mot:list:0")
    kb.button(text="🎓 Инструкция после Премиума", callback_data="onb:list")
    kb.button(text="❓ Поддержка", callback_data="study:support")
    kb.button(text=("⛔️ Выключить слежение" if on else "✅ Включить слежение"),
              callback_data="study:toggle")
    kb.button(text="↩️ В админ-меню", callback_data="m:admin")
    kb.adjust(1)
    await _show(call, text, kb.as_markup())
    await call.answer()


@router.callback_query(F.data == "study:toggle", IsAdmin())
async def cb_toggle(call: CallbackQuery):
    ss.set_value("study_enabled", "0" if ss.get_bool("study_enabled") else "1")
    await call.answer("Готово.")
    await cb_study_home(_with_data(call, "adm:study"))


# ================= Аналитика =================

@router.callback_query(F.data == "study:stats", IsAdmin())
async def cb_stats(call: CallbackQuery):
    def _calc():
        people = sr.candidates()
        total = len(people)
        active7 = 0
        done_total = 0
        unfinished_total = 0
        risks = {"ok": 0, "slight": 0, "behind": 0, "far": 0}
        for tg_id in people:
            data = st.profile(tg_id)
            risks[data["risk"]] = risks.get(data["risk"], 0) + 1
            done_total += data["done_topics"]
            unfinished_total += data["unfinished_count"]
            idle = data.get("days_idle")
            if idle is not None and idle < 7:
                active7 += 1
        notif = db.fetchone(
            "SELECT COUNT(*) AS c FROM study_notifications "
            "WHERE datetime(sent_at) > datetime('now', '-7 days')")
        returned = db.fetchone(
            "SELECT COUNT(*) AS c FROM study_notifications "
            "WHERE kind='returned' AND datetime(sent_at) > datetime('now','-30 days')")
        return {"total": total, "active7": active7, "done": done_total,
                "unfinished": unfinished_total, "risks": risks,
                "notif7": notif["c"] if notif else 0,
                "returned30": returned["c"] if returned else 0}

    d = await asyncio.to_thread(_calc)
    text = (
        "📊 <b>Учебная аналитика</b>\n\n"
        f"👥 Под наблюдением: <b>{d['total']}</b>\n"
        f"🏃 Занимались за неделю: <b>{d['active7']}</b>\n\n"
        f"{st.RISK_TITLES['ok']}: <b>{d['risks'].get('ok', 0)}</b>\n"
        f"{st.RISK_TITLES['slight']}: <b>{d['risks'].get('slight', 0)}</b>\n"
        f"{st.RISK_TITLES['behind']}: <b>{d['risks'].get('behind', 0)}</b>\n"
        f"{st.RISK_TITLES['far']}: <b>{d['risks'].get('far', 0)}</b>\n\n"
        f"✅ Закрытых тем всего: <b>{d['done']}</b>\n"
        f"📌 Незакрытых тем: <b>{d['unfinished']}</b>\n\n"
        f"✉️ Писем за неделю: <b>{d['notif7']}</b>\n"
        f"🔥 Вернулись после напоминания за месяц: <b>{d['returned30']}</b>"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="⚠️ Отстающие", callback_data="lag:list:all:0")
    kb.button(text="↩️ Назад", callback_data="adm:study")
    kb.adjust(1)
    await _show(call, text, kb.as_markup())
    await call.answer()


# ================= Отстающие ученики =================

FILTERS = [
    ("all", "Все отстающие"),
    ("d2", "Не занимались 2 дня"),
    ("d3", "Не занимались 3 дня"),
    ("d4", "Не занимались 4+ дней"),
    ("reading_no_hw", "Читают, но не делают ДЗ"),
    ("unfinished", "Есть незакрытые ДЗ"),
    ("far", "Сильно отстают"),
    ("never", "Ни разу не начинали"),
    ("premium_soon", "Премиум скоро кончится"),
    ("new", "Новые ученики"),
    ("returned", "Вернулись после напоминания"),
]

FILTER_TITLES = dict(FILTERS)


def _collect(kind: str) -> list:
    """Список учеников по выбранному фильтру."""
    groups = sr.summary()
    if kind == "d2":
        return groups["d2"]
    if kind == "d3":
        return groups["d3"]
    if kind == "d4":
        return groups["d4"]
    if kind == "reading_no_hw":
        return groups["reading_no_hw"]
    if kind == "never":
        return groups["never"]

    people = [st.profile(tg) for tg in sr.candidates()]
    if kind == "far":
        return [p for p in people if p["risk"] == "far"]
    if kind == "unfinished":
        return [p for p in people if p["unfinished_count"] > 0]
    if kind == "premium_soon":
        # Премиум заканчивается в ближайшую неделю: days_since для будущей
        # даты отрицателен, поэтому «осталось меньше 7 дней» — это -7..0
        out = []
        for p in people:
            until = (p.get("premium_until") or "").strip()
            if not until:
                continue
            left = st.days_since(until)
            if left is not None and -7 <= left <= 0:
                out.append(p)
        return out
    if kind == "new":
        out = []
        for p in people:
            rows = db.fetchone(
                "SELECT created_at FROM users WHERE tg_id=?", (p["tg_id"],))
            if rows and rows.get("created_at"):
                age = st.days_since(rows["created_at"])
                if age is not None and age <= 7:
                    out.append(p)
        return out
    if kind == "returned":
        rows = db.fetchall(
            "SELECT DISTINCT tg_id FROM study_notifications WHERE kind='returned' "
            "AND datetime(sent_at) > datetime('now', '-14 days')")
        ids = {r["tg_id"] for r in rows}
        return [p for p in people if p["tg_id"] in ids]
    # all — все, у кого есть хоть какое-то отставание
    return [p for p in people if p["risk"] != "ok"]


def _person_line(p: dict) -> str:
    idle = p.get("days_idle")
    idle_s = f"{int(idle)} дн." if idle is not None else "не начинал"
    uname = f"@{p['username']}" if p.get("username") else ""
    return (f"{p['risk_title'].split()[0]} <b>{utils.escape_html(p.get('name') or 'без имени')}</b> "
            f"{utils.escape_html(uname)}\n"
            f"    без учёбы: {idle_s} · незакрытых ДЗ: {p['unfinished_count']}")


@router.callback_query(F.data.startswith("lag:list:"), IsAdmin())
async def cb_lag_list(call: CallbackQuery):
    parts = call.data.split(":")
    kind = parts[2] if len(parts) > 2 else "all"
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0

    people = await asyncio.to_thread(_collect, kind)
    total = len(people)
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = people[page * PAGE:(page + 1) * PAGE]

    head = f"⚠️ <b>{FILTER_TITLES.get(kind, 'Отстающие')}</b>\n\nВсего: <b>{total}</b>"
    if total:
        head += f"  ·  страница {page + 1} из {pages}\n\n"
        head += "\n\n".join(_person_line(p) for p in chunk)
        head += "\n\n<i>Нажмите на ученика — откроется карточка с историей.</i>"
    else:
        head += "\n\nПо этому фильтру никого нет 👍"

    kb = InlineKeyboardBuilder()
    for p in chunk:
        label = (p.get("name") or p.get("username") or str(p["tg_id"]))[:22]
        kb.button(text=f"👤 {label}", callback_data=f"lag:card:{p['tg_id']}:{kind}:{page}")
    if pages > 1:
        if page > 0:
            kb.button(text="⬅️", callback_data=f"lag:list:{kind}:{page - 1}")
        if page < pages - 1:
            kb.button(text="➡️", callback_data=f"lag:list:{kind}:{page + 1}")
    if total:
        kb.button(text="✉️ Написать всем из списка", callback_data=f"lag:mail:{kind}")
    kb.button(text="🔎 Фильтры", callback_data="lag:filters")
    kb.button(text="↩️ Назад", callback_data="adm:study")
    kb.adjust(1)
    await _show(call, head, kb.as_markup())
    await call.answer()


@router.callback_query(F.data == "lag:filters", IsAdmin())
async def cb_lag_filters(call: CallbackQuery):
    kb = InlineKeyboardBuilder()
    for key, title in FILTERS:
        kb.button(text=title, callback_data=f"lag:list:{key}:0")
    kb.button(text="↩️ Назад", callback_data="adm:study")
    kb.adjust(1)
    await _show(call, "🔎 <b>Кого показать</b>", kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("lag:card:"), IsAdmin())
async def cb_lag_card(call: CallbackQuery):
    parts = call.data.split(":")
    tg_id = int(parts[2])
    kind = parts[3] if len(parts) > 3 else "all"
    page = parts[4] if len(parts) > 4 else "0"

    p = await asyncio.to_thread(st.profile, tg_id)

    def _history():
        rows = db.fetchall(
            "SELECT kind, sent_at, reaction FROM study_notifications "
            "WHERE tg_id=? ORDER BY id DESC LIMIT 8", (tg_id,))
        return [dict(r) for r in rows]

    history = await asyncio.to_thread(_history)

    idle = p.get("days_idle")
    lines = [
        f"👤 <b>{utils.escape_html(p.get('name') or 'без имени')}</b>",
        (f"@{utils.escape_html(p['username'])}" if p.get("username") else "") +
        f"  <code>{tg_id}</code>",
        "",
        f"Статус: {p['risk_title']}",
        f"Премиум до: <b>{(p.get('premium_until') or '—')[:10]}</b>",
        "",
        f"🕒 Последний вход: {(p.get('last_visit_at') or '—')[:16].replace('T', ' ')}",
        f"📖 Последний конспект: {utils.escape_html(p.get('last_note_title') or '—')}"
        f" ({(p.get('last_note_at') or '—')[:10]})",
        f"✅ Последнее ДЗ: {utils.escape_html(p.get('last_hw_title') or '—')}"
        f" ({(p.get('last_hw_at') or '—')[:10]})",
        "",
        f"⏳ Без учёбы: <b>{int(idle) if idle is not None else '—'}</b> дн.",
        f"📌 Незакрытых ДЗ: <b>{p['unfinished_count']}</b>",
        f"✅ Закрытых тем: <b>{p['done_topics']}</b> из {p['opened_topics']} открытых"
        f" ({p['percent']}%)",
        f"📈 Дней с занятиями за 2 недели: <b>{p['rhythm_days']}</b>",
    ]
    if p["unfinished"]:
        lines.append("")
        lines.append("<b>Начатые, но не закрытые темы:</b>")
        for t in p["unfinished"][:5]:
            lines.append(f"• {utils.escape_html(t['title'])} — {t['status_title']}")
    if history:
        lines.append("")
        lines.append("<b>История уведомлений:</b>")
        for h in history:
            when = (h["sent_at"] or "")[:16].replace("T", " ")
            react = f" → {h['reaction']}" if (h.get("reaction") or "").strip() else ""
            lines.append(f"• {when} — {h['kind']}{react}")

    kb = InlineKeyboardBuilder()
    kb.button(text="✉️ Написать ученику", callback_data=f"lag:one:{tg_id}")
    kb.button(text="🔄 Сбросить предупреждения", callback_data=f"lag:reset:{tg_id}")
    kb.button(text="↩️ К списку", callback_data=f"lag:list:{kind}:{page}")
    kb.adjust(1)
    await _show(call, "\n".join(x for x in lines if x is not None), kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("lag:reset:"), IsAdmin())
async def cb_lag_reset(call: CallbackQuery):
    tg_id = int(call.data.split(":")[2])
    st.set_state(tg_id, warn_level=0, last_warn_at=None)
    await call.answer("Цепочка предупреждений сброшена.", show_alert=True)
    await cb_lag_card(_with_data(call, f"lag:card:{tg_id}:all:0"))


@router.callback_query(F.data.startswith("lag:one:"), IsAdmin())
async def cb_lag_one(call: CallbackQuery, state: FSMContext):
    tg_id = int(call.data.split(":")[2])
    await state.update_data(mail_targets=[tg_id])
    await state.set_state(StudyStates.waiting_broadcast)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=f"lag:card:{tg_id}:all:0")
    await _show(call, "✉️ Пришлите текст — отправлю его этому ученику.",
                kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("lag:mail:"), IsAdmin())
async def cb_lag_mail(call: CallbackQuery, state: FSMContext):
    kind = call.data.split(":")[2]
    people = await asyncio.to_thread(_collect, kind)
    targets = [p["tg_id"] for p in people]
    if not targets:
        await call.answer("Некому писать.", show_alert=True)
        return
    await state.update_data(mail_targets=targets)
    await state.set_state(StudyStates.waiting_broadcast)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=f"lag:list:{kind}:0")
    await _show(call,
                f"✉️ Пришлите текст — отправлю его <b>{len(targets)}</b> ученикам "
                f"из списка «{FILTER_TITLES.get(kind, kind)}».\n\n"
                f"<i>Можно использовать {{name}} — подставится имя ученика.</i>",
                kb.as_markup())
    await call.answer()


@router.message(StudyStates.waiting_broadcast, IsAdmin())
async def msg_broadcast(message: Message, state: FSMContext):
    data = await state.get_data()
    targets = data.get("mail_targets") or []
    await state.clear()
    text = (message.text or "").strip()
    if not text or not targets:
        await message.answer("Пусто — ничего не отправил.")
        return

    sent = failed = 0
    for tg_id in targets:
        try:
            profile = await asyncio.to_thread(st.profile, tg_id)
            personal = text.replace("{name}", (profile.get("name") or "").strip() or "Привет")
            await message.bot.send_message(tg_id, personal)
            sent += 1
            await asyncio.sleep(0.05)      # бережём лимиты Telegram
        except Exception:
            failed += 1
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ К отстающим", callback_data="lag:list:all:0")
    await message.answer(f"✅ Отправлено: <b>{sent}</b>"
                         + (f"\n⚠️ Не доставлено: <b>{failed}</b>" if failed else ""),
                         parse_mode="HTML", reply_markup=kb.as_markup())


# ================= Настройки напоминаний =================

SETTING_ROWS = [
    ("study_warn1_days", "Первое напоминание, дней"),
    ("study_warn2_days", "Второе напоминание, дней"),
    ("study_warn3_days", "Строгое напоминание, дней"),
    ("study_send_hour", "Начало окна отправки, час"),
    ("study_send_until", "Конец окна отправки, час"),
    ("study_quiet_from", "Ночная тишина с, час"),
    ("study_quiet_to", "Ночная тишина до, час"),
    ("study_min_gap_hours", "Минимум между письмами, часов"),
    ("study_motivation_repeat_days", "Не повторять мотивацию, дней"),
    ("study_report_hour", "Час ежедневного отчёта"),
]


@router.callback_query(F.data == "study:settings", IsAdmin())
async def cb_settings(call: CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
    lines = ["🔔 <b>Напоминания и сроки</b>\n"]
    for key, title in SETTING_ROWS:
        lines.append(f"• {title}: <b>{ss.get(key)}</b>")
    lines.append("")
    lines.append(f"Кому пишем: <b>"
                 f"{'только Премиум' if ss.get_bool('study_premium_only') else 'всем'}</b>")
    lines.append(f"Мотивация в письмах: <b>"
                 f"{'включена' if ss.get_bool('study_motivation_enabled') else 'выключена'}</b>")
    lines.append(f"Ежедневный отчёт админу: <b>"
                 f"{'включён' if ss.get_bool('study_report_enabled') else 'выключен'}</b>")

    kb = InlineKeyboardBuilder()
    for key, title in SETTING_ROWS:
        kb.button(text=f"✏️ {title}", callback_data=f"study:set:{key}")
    kb.button(text=("👥 Пишем: только Премиум" if ss.get_bool("study_premium_only")
                    else "👥 Пишем: всем"),
              callback_data="study:sw:study_premium_only")
    kb.button(text=("🔥 Мотивация: вкл" if ss.get_bool("study_motivation_enabled")
                    else "🔥 Мотивация: выкл"),
              callback_data="study:sw:study_motivation_enabled")
    kb.button(text=("📊 Отчёт админу: вкл" if ss.get_bool("study_report_enabled")
                    else "📊 Отчёт админу: выкл"),
              callback_data="study:sw:study_report_enabled")
    kb.button(text="↩️ Назад", callback_data="adm:study")
    kb.adjust(1)
    await _show(call, "\n".join(lines), kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("study:sw:"), IsAdmin())
async def cb_switch(call: CallbackQuery):
    key = call.data.split(":", 2)[2]
    ss.set_value(key, "0" if ss.get_bool(key) else "1")
    await call.answer("Готово.")
    await cb_settings(_with_data(call, "study:settings"))


@router.callback_query(F.data.startswith("study:set:"), IsAdmin())
async def cb_set(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":", 2)[2]
    title = dict(SETTING_ROWS).get(key, key)
    await state.update_data(setting_key=key)
    await state.set_state(StudyStates.waiting_setting)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="study:settings")
    await _show(call, f"✏️ <b>{title}</b>\n\nСейчас: <b>{ss.get(key)}</b>\n\n"
                      f"Пришлите новое число.", kb.as_markup())
    await call.answer()


@router.message(StudyStates.waiting_setting, IsAdmin())
async def msg_set(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("setting_key")
    await state.clear()
    raw = (message.text or "").strip()
    if not key or not raw.isdigit():
        await message.answer("Нужно число. Значение оставил прежним.")
        return
    ss.set_value(key, int(raw))
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ К настройкам", callback_data="study:settings")
    await message.answer(f"✅ Новое значение: <b>{raw}</b>", parse_mode="HTML",
                         reply_markup=kb.as_markup())


# ================= Шаблоны сообщений =================

@router.callback_query(F.data == "study:tpl", IsAdmin())
async def cb_templates(call: CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
    items = await asyncio.to_thread(ss.all_templates)
    text = ("📝 <b>Шаблоны сообщений</b>\n\n"
            "Можно использовать переменные:\n"
            "<code>" + "</code>  <code>".join(ss.VARIABLES) + "</code>\n\n"
            "Выключенный шаблон бот не отправляет.")
    kb = InlineKeyboardBuilder()
    for t in items:
        mark = "✅" if t["enabled"] else "⛔️"
        kb.button(text=f"{mark} {t['title']}", callback_data=f"study:tpl:{t['key']}")
    kb.button(text="↩️ Назад", callback_data="adm:study")
    kb.adjust(1)
    await _show(call, text, kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("study:tpl:"), IsAdmin())
async def cb_template_one(call: CallbackQuery):
    key = call.data.split(":", 2)[2]
    text_now = ss.template(key) or ss.TEMPLATE_DEFAULTS.get(key, "")
    title = ss.TEMPLATE_TITLES.get(key, key)
    row = db.fetchone("SELECT enabled FROM study_templates WHERE key=?", (key,))
    enabled = bool(row["enabled"]) if row else True

    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить текст", callback_data=f"study:tpledit:{key}")
    kb.button(text=("⛔️ Выключить" if enabled else "✅ Включить"),
              callback_data=f"study:tpltog:{key}")
    kb.button(text="↩️ К шаблонам", callback_data="study:tpl")
    kb.adjust(1)
    await _show(call,
                f"📝 <b>{title}</b>\n"
                f"Состояние: {'✅ отправляется' if enabled else '⛔️ выключен'}\n\n"
                f"<code>{utils.escape_html(text_now)}</code>",
                kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("study:tpltog:"), IsAdmin())
async def cb_template_toggle(call: CallbackQuery):
    key = call.data.split(":", 2)[2]
    row = db.fetchone("SELECT enabled FROM study_templates WHERE key=?", (key,))
    now = bool(row["enabled"]) if row else True
    ss.set_template(key, enabled=not now)
    await call.answer("Готово.")
    await cb_template_one(_with_data(call, f"study:tpl:{key}"))


@router.callback_query(F.data.startswith("study:tpledit:"), IsAdmin())
async def cb_template_edit(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":", 2)[2]
    await state.update_data(tpl_key=key)
    await state.set_state(StudyStates.waiting_template)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=f"study:tpl:{key}")
    await _show(call,
                f"✏️ Пришлите новый текст для «{ss.TEMPLATE_TITLES.get(key, key)}».\n\n"
                f"Переменные: <code>" + "</code> <code>".join(ss.VARIABLES) + "</code>",
                kb.as_markup())
    await call.answer()


@router.message(StudyStates.waiting_template, IsAdmin())
async def msg_template(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("tpl_key")
    await state.clear()
    text = (message.text or "").strip()
    if not key or not text:
        await message.answer("Пусто — текст не менял.")
        return
    ss.set_template(key, text=text)
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ К шаблонам", callback_data="study:tpl")
    await message.answer("✅ Текст сохранён.", reply_markup=kb.as_markup())


# ================= Поддержка =================

@router.callback_query(F.data == "study:support", IsAdmin())
async def cb_support_settings(call: CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
    manager = utils.manager_username()
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Ник менеджера", callback_data="study:sup:username")
    kb.button(text="✏️ Текст поддержки", callback_data="study:sup:text")
    kb.button(text="↩️ Назад", callback_data="adm:study")
    kb.adjust(1)
    await _show(call,
                "❓ <b>Поддержка</b>\n\n"
                f"Менеджер: <b>{'@' + manager if manager else 'не задан'}</b>\n\n"
                f"Текст:\n<i>{utils.escape_html(ss.get('support_text'))}</i>\n\n"
                "<i>Поменяете ник — все кнопки «Написать менеджеру» сразу "
                "поведут на новый аккаунт, обновлять бота не нужно.</i>",
                kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("study:sup:"), IsAdmin())
async def cb_support_edit(call: CallbackQuery, state: FSMContext):
    what = call.data.split(":")[2]
    await state.update_data(support_field=what)
    await state.set_state(StudyStates.waiting_support)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="study:support")
    prompt = ("Пришлите @ник менеджера." if what == "username"
              else "Пришлите текст, который увидит ученик в разделе «Поддержка».")
    await _show(call, prompt, kb.as_markup())
    await call.answer()


@router.message(StudyStates.waiting_support, IsAdmin())
async def msg_support(message: Message, state: FSMContext):
    data = await state.get_data()
    what = data.get("support_field")
    await state.clear()
    value = (message.text or "").strip()
    if not value:
        await message.answer("Пусто — ничего не менял.")
        return
    if what == "username":
        ss.set_value("support_username", value.lstrip("@")[:64])
    else:
        ss.set_value("support_text", value[:1000])
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ К поддержке", callback_data="study:support")
    await message.answer("✅ Сохранено.", reply_markup=kb.as_markup())


# ================= Мотивационные материалы =================

def _media_from(message: Message) -> tuple:
    """Достаёт из сообщения тип и file_id — что бы админ ни прислал."""
    if message.photo:
        return "photo", message.photo[-1].file_id, (message.caption or "")
    if message.video:
        return "video", message.video.file_id, (message.caption or "")
    if message.animation:
        return "animation", message.animation.file_id, (message.caption or "")
    if message.video_note:
        return "video_note", message.video_note.file_id, ""
    if message.voice:
        return "voice", message.voice.file_id, (message.caption or "")
    if message.document:
        return "document", message.document.file_id, (message.caption or "")
    return "text", "", (message.text or "")


@router.callback_query(F.data.startswith("mot:list"), IsAdmin())
async def cb_mot_list(call: CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
    parts = call.data.split(":")
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

    items = await asyncio.to_thread(ms.all_items)
    total = len(items)
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = items[page * PAGE:(page + 1) * PAGE]

    text = ["🔥 <b>Мотивация</b>\n"]
    text.append(f"Всего материалов: <b>{total}</b>")
    text.append(f"Не повторять чаще, чем раз в <b>{ss.get('study_motivation_repeat_days')}</b> дней")
    if not total:
        text.append("\nПока пусто. Добавьте материалы — бот будет подмешивать "
                    "их к строгим напоминаниям, чтобы не только ругать, но и "
                    "поддерживать.")
    else:
        text.append(f"\nСтраница {page + 1} из {pages}\n")
        for i, m in enumerate(chunk, start=page * PAGE + 1):
            mark = "✅" if m["enabled"] else "⛔️"
            preview = (m["text"] or "").replace("\n", " ")[:48]
            text.append(f"{mark} {i}. {ms.KIND_TITLES.get(m['kind'], m['kind'])}"
                        + (f" — {utils.escape_html(preview)}" if preview else ""))

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить материал", callback_data="mot:add")
    kb.button(text="📥 Импорт из файла", callback_data="mot:import")
    kb.button(text="📄 Скачать шаблон", callback_data="mot:template")
    for m in chunk:
        label = (m["text"] or ms.KIND_TITLES.get(m["kind"], ""))[:24] or "материал"
        kb.button(text=f"{'✅' if m['enabled'] else '⛔️'} {label}",
                  callback_data=f"mot:one:{m['id']}:{page}")
    if pages > 1:
        if page > 0:
            kb.button(text="⬅️", callback_data=f"mot:list:{page - 1}")
        if page < pages - 1:
            kb.button(text="➡️", callback_data=f"mot:list:{page + 1}")
    kb.button(text="↩️ Назад", callback_data="adm:study")
    kb.adjust(1)
    await _show(call, "\n".join(text), kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("mot:one:"), IsAdmin())
async def cb_mot_one(call: CallbackQuery):
    parts = call.data.split(":")
    item_id = int(parts[2])
    page = parts[3] if len(parts) > 3 else "0"
    item = await asyncio.to_thread(ms.get, item_id)
    if not item:
        await call.answer("Не найдено.", show_alert=True)
        return

    def _sent_count():
        row = db.fetchone("SELECT COUNT(*) AS c FROM motivation_log "
                          "WHERE motivation_id=?", (item_id,))
        return row["c"] if row else 0

    sent = await asyncio.to_thread(_sent_count)
    kb = InlineKeyboardBuilder()
    kb.button(text="👀 Показать мне", callback_data=f"mot:preview:{item_id}")
    kb.button(text=("⛔️ Выключить" if item["enabled"] else "✅ Включить"),
              callback_data=f"mot:tog:{item_id}:{page}")
    kb.button(text="🗑 Удалить", callback_data=f"mot:del:{item_id}:{page}")
    kb.button(text="↩️ К списку", callback_data=f"mot:list:{page}")
    kb.adjust(1)
    await _show(call,
                f"🔥 <b>Материал №{item_id}</b>\n\n"
                f"Тип: {ms.KIND_TITLES.get(item['kind'], item['kind'])}\n"
                f"Состояние: {'✅ используется' if item['enabled'] else '⛔️ выключен'}\n"
                f"Отправлен: <b>{sent}</b> раз\n\n"
                + (f"<code>{utils.escape_html((item['text'] or '')[:600])}</code>"
                   if item["text"] else "<i>без текста</i>"),
                kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("mot:preview:"), IsAdmin())
async def cb_mot_preview(call: CallbackQuery):
    item_id = int(call.data.split(":")[2])
    item = await asyncio.to_thread(ms.get, item_id)
    if item:
        await ms.send(call.bot, call.from_user.id, item)
    await call.answer("Отправил вам этот материал.")


@router.callback_query(F.data.startswith("mot:tog:"), IsAdmin())
async def cb_mot_toggle(call: CallbackQuery):
    parts = call.data.split(":")
    item_id, page = int(parts[2]), parts[3] if len(parts) > 3 else "0"
    await asyncio.to_thread(ms.toggle, item_id)
    await call.answer("Готово.")
    await cb_mot_one(_with_data(call, f"mot:one:{item_id}:{page}"))


@router.callback_query(F.data.startswith("mot:del:"), IsAdmin())
async def cb_mot_delete(call: CallbackQuery):
    parts = call.data.split(":")
    item_id, page = int(parts[2]), parts[3] if len(parts) > 3 else "0"
    await asyncio.to_thread(ms.delete, item_id)
    await call.answer("Удалено.", show_alert=True)
    await cb_mot_list(_with_data(call, f"mot:list:{page}"))


@router.callback_query(F.data == "mot:add", IsAdmin())
async def cb_mot_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(StudyStates.waiting_motivation)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="mot:list:0")
    await _show(call,
                "➕ <b>Новый материал</b>\n\n"
                "Пришлите то, что должно приходить ученику: текст, фото, видео, "
                "GIF, кружок, голосовое или файл.\n\n"
                "Можно прислать несколько подряд — каждое станет отдельным "
                "материалом. Когда закончите, нажмите «Готово».",
                kb.as_markup())
    await call.answer()


@router.message(StudyStates.waiting_motivation, IsAdmin())
async def msg_mot_add(message: Message, state: FSMContext):
    kind, file_id, text = _media_from(message)
    if kind == "text" and not (text or "").strip():
        await message.answer("Не понял, что это. Пришлите текст или файл.")
        return
    await asyncio.to_thread(ms.add, kind, file_id, text, message.from_user.id)
    total = len(await asyncio.to_thread(ms.all_items))
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data="mot:list:0")
    await message.answer(
        f"✅ Добавлено ({ms.KIND_TITLES.get(kind, kind)}). Всего материалов: {total}.\n"
        f"Можно прислать ещё.", reply_markup=kb.as_markup())


@router.callback_query(F.data == "mot:template", IsAdmin())
async def cb_mot_template(call: CallbackQuery):
    """Отдаём файл-шаблон с объяснением и готовым запросом для нейросети."""
    from aiogram.types import BufferedInputFile
    data = ms.TEMPLATE_TEXT.encode("utf-8")
    try:
        await call.message.answer_document(
            BufferedInputFile(data, filename="motivashki_shablon.txt"),
            caption="📄 Шаблон для импорта мотивашек.\n\n"
                    "Внутри — пояснение и готовый запрос, который можно дать "
                    "нейросети, чтобы она написала тексты за вас.")
    except Exception as e:
        log.warning("шаблон мотивашек: %s", e)
    await call.answer()


@router.callback_query(F.data == "mot:import", IsAdmin())
async def cb_mot_import(call: CallbackQuery, state: FSMContext):
    await state.set_state(StudyStates.waiting_import)
    kb = InlineKeyboardBuilder()
    kb.button(text="📄 Скачать шаблон", callback_data="mot:template")
    kb.button(text="❌ Отмена", callback_data="mot:list:0")
    kb.adjust(1)
    await _show(call,
                "📥 <b>Импорт мотивашек</b>\n\n"
                "Пришлите файл .txt — каждая строка станет отдельной мотивашкой. "
                "Строки, начинающиеся с #, бот пропустит.\n\n"
                "Не знаете, что писать — скачайте шаблон: внутри пояснение и "
                "готовый запрос для нейросети.",
                kb.as_markup())
    await call.answer()


@router.message(StudyStates.waiting_import, IsAdmin())
async def msg_mot_import(message: Message, state: FSMContext):
    if not message.document:
        await message.answer("Пришлите файл .txt")
        return
    await state.clear()
    try:
        buf = await message.bot.download(message.document.file_id)
        raw = buf.read().decode("utf-8", errors="ignore")
    except Exception as e:
        await message.answer(f"Не смог прочитать файл: {e}")
        return
    texts, skipped = ms.parse_import(raw)
    if not texts:
        await message.answer("В файле не нашлось ни одной строки с текстом.")
        return
    added = await asyncio.to_thread(ms.import_texts, texts, message.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="🔥 К мотивации", callback_data="mot:list:0")
    await message.answer(
        f"✅ Загружено мотивашек: <b>{added}</b>"
        + (f"\nСтрок-пояснений пропущено: {skipped}" if skipped else ""),
        parse_mode="HTML", reply_markup=kb.as_markup())


# ================= Инструкция после Премиума =================

@router.callback_query(F.data == "onb:list", IsAdmin())
async def cb_onb_list(call: CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
    items = await asyncio.to_thread(onb.blocks)
    on = ss.get_bool("onboarding_enabled")

    text = ["🎓 <b>Инструкция после выдачи Премиума</b>\n"]
    text.append(f"Отправка: {'✅ включена' if on else '⛔️ выключена'}")
    text.append("Приходит один раз — при первой выдаче Премиума любым способом: "
                "покупка за звёзды, ручная выдача, промокод, подарок, награда "
                "за друзей.\n")
    if not items:
        text.append("Своих блоков пока нет — бот отправит короткую инструкцию "
                    "по умолчанию. Добавьте блоки, чтобы собрать свою.")
    else:
        text.append("<b>Блоки по порядку:</b>")
        for i, b in enumerate(items, start=1):
            mark = "✅" if b["enabled"] else "⛔️"
            preview = (b["text"] or "").replace("\n", " ")[:40]
            text.append(f"{mark} {i}. {onb.KIND_TITLES.get(b['kind'], b['kind'])}"
                        + (f" — {utils.escape_html(preview)}" if preview else ""))

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить блок", callback_data="onb:add")
    for b in items:
        label = (b["text"] or onb.KIND_TITLES.get(b["kind"], ""))[:24] or "блок"
        kb.button(text=f"{'✅' if b['enabled'] else '⛔️'} {label}",
                  callback_data=f"onb:one:{b['id']}")
    kb.button(text="👀 Показать мне целиком", callback_data="onb:preview")
    kb.button(text=("⛔️ Выключить отправку" if on else "✅ Включить отправку"),
              callback_data="onb:toggle")
    kb.button(text="↩️ Назад", callback_data="adm:study")
    kb.adjust(1)
    await _show(call, "\n".join(text), kb.as_markup())
    await call.answer()


@router.callback_query(F.data == "onb:toggle", IsAdmin())
async def cb_onb_toggle(call: CallbackQuery):
    ss.set_value("onboarding_enabled", "0" if ss.get_bool("onboarding_enabled") else "1")
    await call.answer("Готово.")
    await cb_onb_list(_with_data(call, "onb:list"))


@router.callback_query(F.data == "onb:preview", IsAdmin())
async def cb_onb_preview(call: CallbackQuery):
    await onb.send_instruction(call.bot, call.from_user.id)
    await call.answer("Отправил вам инструкцию так, как её увидит ученик.")


@router.callback_query(F.data == "onb:add", IsAdmin())
async def cb_onb_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(StudyStates.waiting_block)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="onb:list")
    await _show(call,
                "➕ <b>Новый блок инструкции</b>\n\n"
                "Пришлите текст, видео, фото, GIF, кружок, голосовое или файл — "
                "блок встанет в конец. Порядок потом можно поменять.\n\n"
                "<i>Например: блок 1 — приветствие, блок 2 — видео «Как "
                "пользоваться платформой», блок 3 — текст с шагами.</i>",
                kb.as_markup())
    await call.answer()


@router.message(StudyStates.waiting_block, IsAdmin())
async def msg_onb_add(message: Message, state: FSMContext):
    kind, file_id, text = _media_from(message)
    if kind == "text" and not (text or "").strip():
        await message.answer("Не понял, что это. Пришлите текст или файл.")
        return
    await asyncio.to_thread(onb.add_block, kind, file_id, text)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data="onb:list")
    await message.answer(
        f"✅ Блок добавлен ({onb.KIND_TITLES.get(kind, kind)}). "
        f"Можно прислать следующий.", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("onb:one:"), IsAdmin())
async def cb_onb_one(call: CallbackQuery):
    block_id = int(call.data.split(":")[2])
    b = await asyncio.to_thread(onb.get_block, block_id)
    if not b:
        await call.answer("Не найден.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text=("⛔️ Выключить" if b["enabled"] else "✅ Включить"),
              callback_data=f"onb:tog:{block_id}")
    kb.button(text="⬆️ Выше", callback_data=f"onb:move:{block_id}:up")
    kb.button(text="⬇️ Ниже", callback_data=f"onb:move:{block_id}:down")
    kb.button(text="🗑 Удалить", callback_data=f"onb:del:{block_id}")
    kb.button(text="↩️ К блокам", callback_data="onb:list")
    kb.adjust(1, 2, 1, 1)
    await _show(call,
                f"🎓 <b>Блок №{block_id}</b>\n\n"
                f"Тип: {onb.KIND_TITLES.get(b['kind'], b['kind'])}\n"
                f"Состояние: {'✅ отправляется' if b['enabled'] else '⛔️ выключен'}\n\n"
                + (f"<code>{utils.escape_html((b['text'] or '')[:700])}</code>"
                   if b["text"] else "<i>без текста</i>"),
                kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("onb:tog:"), IsAdmin())
async def cb_onb_block_toggle(call: CallbackQuery):
    block_id = int(call.data.split(":")[2])
    await asyncio.to_thread(onb.toggle_block, block_id)
    await call.answer("Готово.")
    await cb_onb_one(_with_data(call, f"onb:one:{block_id}"))


@router.callback_query(F.data.startswith("onb:move:"), IsAdmin())
async def cb_onb_move(call: CallbackQuery):
    parts = call.data.split(":")
    block_id, direction = int(parts[2]), parts[3]
    await asyncio.to_thread(onb.move_block, block_id, direction)
    await call.answer("Порядок изменён.")
    await cb_onb_list(_with_data(call, "onb:list"))


@router.callback_query(F.data.startswith("onb:del:"), IsAdmin())
async def cb_onb_del(call: CallbackQuery):
    block_id = int(call.data.split(":")[2])
    await asyncio.to_thread(onb.delete_block, block_id)
    await call.answer("Блок удалён.", show_alert=True)
    await cb_onb_list(_with_data(call, "onb:list"))


# ================= Инструкция по кнопке ученика =================

@router.callback_query(F.data == "onb:again")
async def cb_onb_again(call: CallbackQuery):
    """«Как пользоваться платформой» из профиля и из кнопок под инструкцией.

    Показываем то, что настроено сейчас: если админ поменял видео или текст,
    ученик увидит новую версию.
    """
    await call.answer("Отправляю инструкцию…")
    try:
        await onb.send_instruction(call.bot, call.from_user.id)
    except Exception as e:
        log.warning("повторная инструкция %s: %s", call.from_user.id, e)
