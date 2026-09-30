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
from services import study_subjects as sj
from services import study_tracker as st
from services import gamification as gm

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
    waiting_subject_link = State() # ссылка на предмет платформы
    waiting_pace = State()         # темп предмета, уроков в неделю


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
    _tot = sj.totals()
    kb.button(text=f"📚 Предметы под контролем ({_tot['subjects']})",
              callback_data="study:subjects")
    kb.button(text="📊 Учебная аналитика", callback_data="study:stats")
    kb.button(text="🔎 Проверить грамоту", callback_data="rw:verify")
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
    ("plan", "Отстают от учебного плана"),
    ("premium_soon", "Премиум скоро кончится"),
    ("new", "Новые ученики"),
    ("returned", "Вернулись после напоминания"),
]

FILTER_TITLES = dict(FILTERS)

# Чем сортировать список учеников. Данные те же, меняется только порядок.
SORTS = [
    ("lag", "⚠️ По отставанию"),
    ("points", "⭐ По баллам"),
    ("lessons", "📚 По пройденным урокам"),
    ("percent", "📊 По проценту прохождения"),
    ("recent", "🕐 По последней активности"),
    ("started", "📅 По дате начала обучения"),
    ("name", "🔤 По имени"),
]
SORT_TITLES = dict(SORTS)


def _sort_people(people: list, sort: str) -> list:
    """Отсортировать карточки. Тяжёлые цифры (баллы) добираем только когда нужно."""
    if sort in ("points", "lessons", "percent", "started"):
        for p in people:
            if "total_points" not in p:
                try:
                    card = gm.admin_user_card(p["tg_id"])
                except Exception:
                    card = {}
                p["total_points"] = card.get("total_points", 0)
                p["game_lessons_done"] = card.get("lessons_done", 0)
                p["game_percent"] = card.get("percent", 0)
                p["game_started"] = card.get("started_at") or ""
    if sort == "points":
        return sorted(people, key=lambda p: -(p.get("total_points") or 0))
    if sort == "lessons":
        return sorted(people, key=lambda p: -(p.get("game_lessons_done") or 0))
    if sort == "percent":
        return sorted(people, key=lambda p: -(p.get("game_percent") or 0))
    if sort == "started":
        return sorted(people, key=lambda p: (p.get("game_started") or "9999"))
    if sort == "recent":
        return sorted(people, key=lambda p: (p.get("last_visit_at") or ""), reverse=True)
    if sort == "name":
        return sorted(people, key=lambda p: (p.get("name") or p.get("username") or "").lower())
    return people


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
    if kind == "plan":
        return sorted(groups.get("plan") or [], key=lambda p: -(p.get("plan_lag") or 0))
    if kind.startswith("s") and kind[1:].isdigit():
        # Ученики одного предмета под контролем — по величине отставания
        return [st.profile(r["user_tg_id"]) for r in sj.students_of(int(kind[1:]))]

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
    extra = ""
    if p.get("total_points") is not None and "total_points" in p:
        extra = (f"\n    ⭐ {p.get('total_points') or 0} балл. · "
                 f"📚 {p.get('game_lessons_done') or 0} уроков · "
                 f"📊 {p.get('game_percent') or 0}%")
    return (f"{p['risk_title'].split()[0]} <b>{utils.escape_html(p.get('name') or 'без имени')}</b> "
            f"{utils.escape_html(uname)}\n"
            f"    без учёбы: {idle_s} · незакрытых ДЗ: {p['unfinished_count']}" + extra)


@router.callback_query(F.data.startswith("lag:sort:"), IsAdmin())
async def cb_lag_sort(call: CallbackQuery):
    """Выбор сортировки — тот же список, другой порядок."""
    parts = call.data.split(":")
    kind = parts[2] if len(parts) > 2 else "all"
    kb = InlineKeyboardBuilder()
    for key, title in SORTS:
        kb.button(text=title, callback_data=f"lag:list:{kind}:0:{key}")
    kb.button(text="↩️ Назад", callback_data=f"lag:list:{kind}:0")
    kb.adjust(1)
    await _show(call, "↕️ <b>Чем отсортировать список</b>", kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("lag:list:"), IsAdmin())
async def cb_lag_list(call: CallbackQuery):
    parts = call.data.split(":")
    kind = parts[2] if len(parts) > 2 else "all"
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    sort = parts[4] if len(parts) > 4 else "lag"

    people = await asyncio.to_thread(_collect, kind)
    people = await asyncio.to_thread(_sort_people, people, sort)
    total = len(people)
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = people[page * PAGE:(page + 1) * PAGE]

    head = (f"⚠️ <b>{FILTER_TITLES.get(kind, 'Отстающие')}</b>\n"
            f"<i>{SORT_TITLES.get(sort, '')}</i>\n\nВсего: <b>{total}</b>")
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
            kb.button(text="⬅️", callback_data=f"lag:list:{kind}:{page - 1}:{sort}")
        if page < pages - 1:
            kb.button(text="➡️", callback_data=f"lag:list:{kind}:{page + 1}:{sort}")
    if total:
        kb.button(text="✉️ Написать всем из списка", callback_data=f"lag:mail:{kind}")
    kb.button(text="↕️ Сортировка", callback_data=f"lag:sort:{kind}")
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
    if p.get("subjects"):
        lines.append("")
        lines.append("<b>Предметы под контролем:</b>")
        for sx in p["subjects"]:
            lines.append(
                f"📚 {utils.escape_html(sx['title'])}: закрыто <b>{sx['lessons_done']}</b> "
                f"из {sx['lessons_total']}, по плану <b>{sx['expected_done']}</b> "
                f"→ {sx['status_title']}"
                + (f", отставание {sx['lag']} ур." if sx.get("lag") else "")
                + (f" · доступ до {str(sx['access_until'])[:10]}" if sx.get("access_until") else ""))
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

    # Полная сводка ученика: Премиум, баллы, предметы, достижения
    card = await asyncio.to_thread(gm.admin_user_card, tg_id)
    if card:
        lines.insert(3, (
            f"💎 Премиум: {'✅ активен' if card['is_premium'] else '⛔️ нет'}"
            + (f" (с {str(card['premium_since'])[:10]})" if card.get("premium_since") else "")))
        lines += [
            "",
            f"⭐ Общий балл: <b>{card['total_points']}</b>"
            f" · 📚 уроков пройдено: <b>{card['lessons_done']}</b> из {card['lessons_total']}"
            f" ({card['percent']}%)",
            f"📗 Предметов: <b>{card['subjects_count']}</b>"
            f" · 🔥 серия: <b>{card['streak']}</b> дн."
            f" · 🏅 достижения: <b>{len(card['achievements'])}</b> из {card['achievements_total']}",
            f"📅 Начал обучение: <b>{_when(card.get('started_at'))}</b>"
            f" · 🕐 последняя активность: <b>{_when(card.get('last_activity'))}</b>",
        ]
        if card["subjects"]:
            lines.append("")
            lines.append("<b>Баллы по предметам:</b>")
            for s in card["subjects"]:
                lines.append(
                    f"⭐ {utils.escape_html(s['title'])} — <b>{s['points']}</b> балл. · "
                    f"{s['lessons_done']}/{s['lessons_total']} уроков")

    kb = InlineKeyboardBuilder()
    for s in (card.get("subjects") if card else [])[:6]:
        kb.button(text=f"📊 {s['title'][:24]}",
                  callback_data=f"lag:subj:{tg_id}:{s['subject_id']}")
    kb.button(text="💰 Вознаграждения", callback_data=f"rw:user:{tg_id}")
    kb.button(text="🏅 Достижения", callback_data=f"lag:ach:{tg_id}")
    kb.button(text="🕐 Последние действия", callback_data=f"lag:acts:{tg_id}")
    kb.button(text="✉️ Написать ученику", callback_data=f"lag:one:{tg_id}")
    kb.button(text="🔄 Сбросить предупреждения", callback_data=f"lag:reset:{tg_id}")
    kb.button(text="↩️ К списку", callback_data=f"lag:list:{kind}:{page}")
    kb.adjust(1)
    await _show(call, "\n".join(x for x in lines if x is not None), kb.as_markup())
    await call.answer()


def _when(value) -> str:
    """Дата-время из базы в человеческий вид."""
    s = str(value or "").replace("T", " ")
    return s[:16] if s.strip() else "—"


@router.callback_query(F.data.startswith("lag:subj:"), IsAdmin())
async def cb_lag_subject(call: CallbackQuery):
    """Полная статистика ученика по одному предмету."""
    parts = call.data.split(":")
    tg_id, subject_id = int(parts[2]), int(parts[3])

    def _build():
        u = utils.get_user_by_tg(tg_id)
        if not u:
            return None
        return gm.admin_subject_stats(tg_id, u["id"], subject_id)

    s = await asyncio.to_thread(_build)
    if not s:
        await call.answer("Ученик не найден.", show_alert=True)
        return
    lines = [
        f"📊 <b>{utils.escape_html(s['title'])}</b>",
        f"<code>{tg_id}</code>",
        "",
        f"📅 Начал: <b>{_when(s.get('started_at'))}</b>",
        f"🕐 Последняя активность: <b>{_when(s.get('last_activity'))}</b>",
        "",
        f"📚 Всего уроков: <b>{s['lessons_total']}</b>",
        f"✅ Пройдено: <b>{s['lessons_done']}</b> · ⏳ осталось: <b>{s['lessons_left']}</b>",
        f"📊 Прогресс: <b>{s['percent']}%</b> · 👀 открыто тем: <b>{s['lessons_opened']}</b>",
        f"⭐ Баллы: <b>{s['points']}</b>"
        + (f" · 🏆 {s['place']} место из {s['total_students']}" if s["place"] else ""),
        f"✍️ Сдано ДЗ: <b>{s['tests_done']}</b> (попыток: {s['attempts']})",
        f"🔥 Серия по предмету: <b>{s['streak']}</b> дн.",
    ]
    if s.get("status_title"):
        lines += ["", f"План: по графику <b>{s.get('expected_done') or 0}</b> уроков "
                      f"→ {s['status_title']}"
                      + (f", отставание {s['lag']} ур." if s.get("lag") else "")]
    if s.get("access_until"):
        lines.append(f"💎 Доступ до: <b>{str(s['access_until'])[:10]}</b>")
    if s.get("top_opened"):
        lines += ["", "<b>Часто открываемые темы:</b>"]
        for i, t in enumerate(s["top_opened"], start=1):
            lines.append(f"{i}. {utils.escape_html(t['title'])} — {t['opens']} открытий")
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ К карточке", callback_data=f"lag:card:{tg_id}")
    kb.adjust(1)
    await _show(call, "\n".join(lines), kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("lag:ach:"), IsAdmin())
async def cb_lag_achievements(call: CallbackQuery):
    """Достижения ученика: полученные и оставшиеся."""
    tg_id = int(call.data.split(":")[2])

    def _build():
        u = utils.get_user_by_tg(tg_id)
        if not u:
            return None, None
        return gm.earned(u["id"]), gm.locked(u["id"])

    got, left = await asyncio.to_thread(_build)
    if got is None:
        await call.answer("Ученик не найден.", show_alert=True)
        return
    lines = [f"🏅 <b>Достижения</b> — получено {len(got)} из {gm.TOTAL_ACHIEVEMENTS}", ""]
    if got:
        for a in got:
            lines.append(f"{a['icon']} <b>{utils.escape_html(a['title'])}</b>"
                         f" — {_when(a.get('at'))}")
    else:
        lines.append("<i>Пока ни одного.</i>")
    if left:
        lines += ["", "<b>Ещё не получены:</b>"]
        for a in left[:12]:
            lines.append(f"🔒 {utils.escape_html(a['title'])} — {utils.escape_html(a['how'])}")
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ К карточке", callback_data=f"lag:card:{tg_id}")
    kb.adjust(1)
    await _show(call, "\n".join(lines), kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("lag:acts:"), IsAdmin())
async def cb_lag_activity(call: CallbackQuery):
    """Последние действия ученика — что и когда он делал."""
    tg_id = int(call.data.split(":")[2])
    acts = await asyncio.to_thread(gm.recent_activity, tg_id, None, 15)
    lines = ["🕐 <b>Последние действия</b>", ""]
    if acts:
        for a in acts:
            what = a["title_event"]
            if a.get("lesson_title"):
                what += f" «{utils.escape_html(a['lesson_title'])}»"
            lines.append(f"{_when(a.get('at'))} — {what}")
    else:
        lines.append("<i>Действий пока нет.</i>")
    lines.append("")
    lines.append("<i>Открытие темы — не то же самое, что пройденный урок: "
                 "урок закрывается сдачей ДЗ.</i>")
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ К карточке", callback_data=f"lag:card:{tg_id}")
    kb.adjust(1)
    await _show(call, "\n".join(lines), kb.as_markup())
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


# ================= Предметы под контролем =================

def _subjects_text() -> str:
    items = sj.tracked()
    lines = ["📚 <b>Предметы под контролем</b>\n"]
    if not items:
        lines.append("Пока ни один предмет не подключён: слежение идёт по общему "
                     "Премиуму без привязки к предмету.\n\nВставьте ссылку на "
                     "предмет из платформы — ту же, что даёт кнопка «Ссылка на "
                     "предмет» (…startapp=subj_N). Ученики с доступом к нему "
                     "подтянутся сами, прогресс и отставание считаются по каждому.")
    for i, s in enumerate(items, start=1):
        sync = (s.get("last_sync_at") or "")[:16].replace("T", " ")
        lines.append(
            f"{i}. <b>{utils.escape_html(s['title'])}</b> — учеников <b>{s['students_count']}</b>, "
            f"отстают от плана <b>{s['behind_count']}</b>, темп {sj.pace_for(s)} ур/нед"
            + (f", синхр. {sync}" if sync else ", ещё не синхронизирован"))
    return "\n".join(lines)


@router.callback_query(F.data == "study:subjects", IsAdmin())
async def cb_subjects(call: CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
    await asyncio.to_thread(sj.migrate_from_campaigns)   # ранее вставленные ссылки
    text = await asyncio.to_thread(_subjects_text)
    kb = InlineKeyboardBuilder()
    for s in await asyncio.to_thread(sj.tracked):
        kb.button(text=f"📚 {s['title'][:28]} ({s['students_count']})",
                  callback_data=f"study:subj:{s['subject_id']}")
    kb.button(text="➕ Подключить предмет по ссылке", callback_data="study:subj_add")
    if await asyncio.to_thread(sj.tracked):
        kb.button(text="🔄 Синхронизировать все", callback_data="study:sync_all")
    kb.button(text="↩️ Назад", callback_data="adm:study")
    kb.adjust(1)
    await _show(call, text, kb.as_markup())
    await call.answer()


def _subject_card_text(s: dict) -> str:
    students = sj.students_of(s["subject_id"])
    behind = [x for x in students if x["status"] in ("behind", "far")]
    sync = (s.get("last_sync_at") or "")[:16].replace("T", " ")
    lines = [
        f"📚 <b>{utils.escape_html(s['title'])}</b>\n",
        f"Ссылка: <code>{utils.escape_html(s.get('link') or '—')}</code>",
        f"Темп плана: <b>{sj.pace_for(s)}</b> уроков в неделю"
        + ("" if s.get("pace_per_week") else " (общая настройка)"),
        f"Учеников под контролем: <b>{len(students)}</b>",
        f"Отстают от плана: <b>{len(behind)}</b>",
        f"Последняя синхронизация: {sync or '—'}",
    ]
    if students:
        lines.append("")
        lines.append("<b>Сильнее всего отстают:</b>")
        for x in students[:6]:
            name = x.get("first_name") or x.get("username") or str(x["user_tg_id"])
            lines.append(f"• {utils.escape_html(name)} — закрыто {x['lessons_done']} из "
                         f"{x['lessons_total']}, по плану {x['expected_done']} "
                         f"({sj.STATUS_TITLES.get(x['status'], x['status'])})")
    return "\n".join(lines)


@router.callback_query(F.data.startswith("study:subj:"), IsAdmin())
async def cb_subject_card(call: CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
    sid = int(call.data.split(":")[2])
    s = await asyncio.to_thread(sj.get, sid)
    if not s:
        await call.answer("Предмет не подключён.", show_alert=True)
        return
    text = await asyncio.to_thread(_subject_card_text, s)
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Синхронизировать", callback_data=f"study:sync:{sid}")
    kb.button(text=f"👥 Ученики ({s['students_count']})", callback_data=f"lag:list:s{sid}:0")
    kb.button(text="⏱ Изменить темп", callback_data=f"study:pace:{sid}")
    kb.button(text="🗑 Отключить контроль предмета", callback_data=f"study:subj_del:{sid}")
    kb.button(text="↩️ К предметам", callback_data="study:subjects")
    kb.adjust(1)
    await _show(call, text, kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("study:sync:"), IsAdmin())
async def cb_subject_sync(call: CallbackQuery):
    sid = int(call.data.split(":")[2])
    res = await asyncio.to_thread(sj.sync_subject, sid)
    if res.get("error"):
        await call.answer("Предмет не подключён.", show_alert=True)
        return
    await call.answer(
        f"Готово: учеников {res['students']} (новых {res['added']}, выбыло {res['removed']}), "
        f"отстают {res['behind']}", show_alert=True)
    await cb_subject_card(_with_data(call, f"study:subj:{sid}"))


@router.callback_query(F.data == "study:sync_all", IsAdmin())
async def cb_sync_all(call: CallbackQuery):
    res = await asyncio.to_thread(sj.sync_all)
    n = sum(r.get("students", 0) for r in res if isinstance(r, dict))
    await call.answer(f"Синхронизировано предметов: {len(res)}, учеников: {n}",
                      show_alert=True)
    await cb_subjects(_with_data(call, "study:subjects"))


@router.callback_query(F.data.startswith("study:subj_del:"), IsAdmin())
async def cb_subject_del(call: CallbackQuery):
    sid = int(call.data.split(":")[2])
    await asyncio.to_thread(sj.detach, sid)
    await call.answer("Контроль по предмету отключён.", show_alert=True)
    await cb_subjects(_with_data(call, "study:subjects"))


@router.callback_query(F.data == "study:subj_add", IsAdmin())
async def cb_subject_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(StudyStates.waiting_subject_link)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="study:subjects")
    await _show(call,
                "➕ <b>Подключить предмет</b>\n\n"
                "Пришлите ссылку на предмет из платформы — ту же, что даёт "
                "«Ссылка на предмет» в админке сайта:\n"
                "<code>https://t.me/бот/app?startapp=subj_12</code>\n"
                "Подойдёт и ссылка сайта вида <code>/learn/12</code>, и просто номер предмета.\n\n"
                "Ученики с доступом к предмету подтянутся автоматически.",
                kb.as_markup())
    await call.answer()


@router.message(StudyStates.waiting_subject_link, IsAdmin())
async def msg_subject_link(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    sid = sj.parse_subject_link(raw)
    subj = await asyncio.to_thread(sj.resolve_subject, sid) if sid else None
    if not subj:
        await message.answer("Не узнал предмет по этой ссылке. Нужна ссылка вида "
                             "…startapp=subj_12, /learn/12 или номер предмета.")
        return
    await state.clear()
    await asyncio.to_thread(sj.attach, subj["id"], raw, "manual")
    res = await asyncio.to_thread(sj.sync_subject, subj["id"])
    kb = InlineKeyboardBuilder()
    kb.button(text="📚 Карточка предмета", callback_data=f"study:subj:{subj['id']}")
    kb.button(text="↩️ К предметам", callback_data="study:subjects")
    kb.adjust(1)
    await message.answer(
        f"✅ Предмет <b>{utils.escape_html(subj['title'])}</b> подключён.\n\n"
        f"Учеников с доступом: <b>{res['students']}</b>, отстают от плана: "
        f"<b>{res['behind']}</b>. Прогресс и отставание пересчитываются автоматически.",
        parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("study:pace:"), IsAdmin())
async def cb_subject_pace(call: CallbackQuery, state: FSMContext):
    sid = int(call.data.split(":")[2])
    await state.update_data(pace_subject=sid)
    await state.set_state(StudyStates.waiting_pace)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=f"study:subj:{sid}")
    await _show(call, "⏱ Сколько уроков в неделю должен проходить ученик по этому "
                      "предмету? Пришлите число (0 — использовать общую настройку).",
                kb.as_markup())
    await call.answer()


@router.message(StudyStates.waiting_pace, IsAdmin())
async def msg_subject_pace(message: Message, state: FSMContext):
    data = await state.get_data()
    sid = data.get("pace_subject")
    raw = (message.text or "").strip()
    if not sid or not raw.isdigit():
        await message.answer("Нужно число.")
        return
    await state.clear()
    await asyncio.to_thread(sj.set_pace, int(sid), int(raw))
    await asyncio.to_thread(sj.sync_subject, int(sid))
    kb = InlineKeyboardBuilder()
    kb.button(text="📚 К предмету", callback_data=f"study:subj:{sid}")
    await message.answer("✅ Темп сохранён, отставание пересчитано.",
                         reply_markup=kb.as_markup())


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
    ("study_pace_per_week", "Темп плана: уроков в неделю"),
    ("study_lag_slight", "Отставание «чуть»: уроков"),
    ("study_lag_behind", "Отставание «отстаёт»: уроков"),
    ("study_lag_far", "Отставание «сильно»: уроков"),
    ("study_plan_repeat_days", "Письмо про план не чаще, дней"),
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

    stats = await asyncio.to_thread(ms.stats)
    text = ["🔥 <b>Мотивация</b>\n"]
    text.append(f"Всего материалов: <b>{total}</b>, активных: <b>{stats['active']}</b>")
    text.append(f"Не повторять чаще, чем раз в <b>{ss.get('study_motivation_repeat_days')}</b> дней")
    text.append(f"Отправлено ученикам за 7 дней: <b>{stats['sent_7d']}</b>"
                + (f", последняя: {stats['last_sent'][:16].replace('T', ' ')}" if stats['last_sent'] else ""))
    text.append("Когда уходят: вместе с напоминанием "
                f"с <b>{ss.get_int('study_warn2_days', 3)}-го дня</b> без учёбы, "
                f"в окно <b>{ss.get_int('study_send_hour', 18)}:00–{ss.get_int('study_send_until', 21)}:59</b> по Алматы; "
                f"контроль обучения: <b>{'вкл' if ss.get_bool('study_enabled') else 'ВЫКЛ'}</b>, "
                f"мотивация в письмах: <b>{'вкл' if ss.get_bool('study_motivation_enabled') else 'ВЫКЛ'}</b>")
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
        raw = ms.decode_import(buf.read())
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
    text.append("Приходит сразу после поздравления при каждой выдаче Премиума: "
                "покупка за звёзды, ручная выдача в боте или на сайте, отложенный "
                "доступ. За приглашённых друзей — не отправляется.\n")
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
