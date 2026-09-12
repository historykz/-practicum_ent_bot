"""
Админ-интерфейс автозапуска тестов по расписанию (кнопка «⏰ Автозапуск по
расписанию» в авто-публикациях, callback sched:menu).

Расписаний может быть много. Мастер создания: название → разделы (или без
раздела) → источник тестов → работа над ошибками → тесты вручную →
периодичность → время → дата начала → срок → режим запуска → чат → канал →
ожидание → подтверждение. Каждый шаг потом меняется отдельно из карточки —
без пересоздания. Логика запусков — services/auto_schedule_service.py.
"""
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

import database as db
import utils
from filters import IsAdmin
from services import auto_schedule_service as ass

router = Router(name="auto_schedule")
log = logging.getLogger(__name__)
ALMATY = timezone(timedelta(hours=5))
esc = utils.escape_html


class SchedStates(StatesGroup):
    text_input = State()          # что именно ждём — в w["await"]


# Порядок шагов мастера. В режиме правки — только нужные шаги.
ALL_STEPS = ["title", "cats", "source", "hardest", "tests", "period", "times", "start", "end",
             "mode", "loop", "per_run", "chat", "channel", "delay", "confirm"]
EDIT_STEPS = {"title": ["title"], "cats": ["cats", "source", "tests"], "source": ["source", "tests"],
              "hardest": ["hardest"], "tests": ["tests"], "period": ["period"], "times": ["times"],
              "start": ["start"], "end": ["end"], "mode": ["mode", "loop", "per_run"],
              "chat": ["chat", "channel"], "delay": ["delay"]}
PER_PAGE = 8


# ───────────────────────── служебное ─────────────────────────

async def _w(state: FSMContext) -> dict:
    return (await state.get_data()).get("w") or {}


async def _save(state: FSMContext, w: dict) -> None:
    await state.update_data(w=w)


async def _show(target, text: str, kb=None):
    """Показать экран: в callback — правкой сообщения, в ответ на текст — новым."""
    msg = target.message if isinstance(target, CallbackQuery) else target
    if isinstance(target, CallbackQuery):
        try:
            await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


def _kb(rows: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _btn(text, data) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _fmt_d(s: str) -> str:
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%d.%m.%Y")
    except (ValueError, TypeError):
        return s or "—"


def _from_sched(s: dict) -> dict:
    """Значения расписания → черновик мастера (для правки одного поля)."""
    return {"title": s.get("title") or "", "cats": list(s.get("category_ids") or []),
            "source": s.get("source") or "auto", "hardest": int(s.get("hardest_n") or 0),
            "tests": list(s.get("test_ids") or []), "period": s.get("period") or "daily",
            "weekdays": list(s.get("weekdays") or []), "interval": int(s.get("interval_days") or 2),
            "dates": list(s.get("dates") or []), "times": list(s.get("times") or ["19:00"]),
            "start": s.get("start_date"), "end_mode": s.get("end_mode") or "never",
            "end_date": s.get("end_date") or "", "mode": s.get("run_mode") or "smart",
            "loop": int(s.get("loop") or 0), "per_run": int(s.get("tests_per_day") or 1),
            "chat": s.get("chat_id"), "channel": s.get("channel_id"),
            "delay": int(s.get("announce_delay") or 60)}


def _to_fields(w: dict) -> dict:
    return {"title": (w.get("title") or "").strip()[:60], "category_ids": w.get("cats") or [],
            "source": w.get("source") or "auto", "hardest_n": int(w.get("hardest") or 0),
            "test_ids": w.get("tests") or [], "period": w.get("period") or "daily",
            "weekdays": w.get("weekdays") or [], "interval_days": int(w.get("interval") or 2),
            "dates": w.get("dates") or [], "times": w.get("times") or ["19:00"],
            "start_date": w.get("start") or ass._today_almaty(),
            "end_mode": w.get("end_mode") or "never", "end_date": w.get("end_date") or "",
            "run_mode": w.get("mode") or "smart", "loop": int(w.get("loop") or 0),
            "tests_per_day": int(w.get("per_run") or 1), "chat_id": str(w.get("chat") or ""),
            "channel_id": (str(w["channel"]) if w.get("channel") else None),
            "announce_delay": int(w.get("delay") or 60)}


# ───────────────────────── список и карточка ─────────────────────────

def _list_screen():
    rows = ass.list_schedules()
    if not rows:
        text = ("⏰ <b>Автозапуск тестов по расписанию</b>\n\n"
                "Бот сам запускает тесты в чат: по разделам или по вашему списку, "
                "каждый день, по дням недели или в выбранные даты, до даты или бессрочно.\n\n"
                "Расписаний пока нет — создайте первое.")
    else:
        lines = ["⏰ <b>Автозапуск тестов по расписанию</b>\n"]
        for s in rows:
            nxt = s.get("next_run_at") or ass.refresh_next_run(s["id"])
            nxt_s = (datetime.strptime(nxt, "%Y-%m-%d %H:%M").strftime("%d.%m %H:%M") if nxt else "—")
            lines.append(f"{ass.display_status(s)} <b>{esc(_title(s))}</b> · след.: {nxt_s}")
        text = "\n".join(lines)
    kb = InlineKeyboardBuilder()
    for s in rows[:30]:
        kb.button(text=f"{ass.display_status(s)[:1]} {_title(s)[:40]}", callback_data=f"sc:card:{s['id']}")
    kb.button(text="➕ Новое расписание", callback_data="sc:new")
    kb.button(text="↩️ Назад", callback_data="adm:autopub")
    kb.adjust(1)
    return text, kb.as_markup()


def _title(s: dict) -> str:
    return s.get("title") or f"Расписание #{s['id']}"


def _card_text(s: dict) -> str:
    nxt = s.get("next_run_at") or ass.refresh_next_run(s["id"])
    nxt_s = (datetime.strptime(nxt, "%Y-%m-%d %H:%M").strftime("%d.%m.%Y %H:%M") + " (Астана)") if nxt else "—"
    st = ass.get_schedule_stats(s["id"])
    chan = f"<code>{esc(str(s['channel_id']))}</code>" if s.get("channel_id") else "нет"
    last = s.get("last_run_at")
    try:
        last = datetime.fromisoformat(last).strftime("%d.%m.%Y %H:%M") if last else "ещё не было"
    except ValueError:
        last = str(last)
    return (
        f"⏰ <b>{esc(_title(s))}</b>  <code>#{s['id']}</code>\n"
        f"{ass.display_status(s)} · следующий запуск: <b>{nxt_s}</b>\n\n"
        f"📂 Тесты: {esc(ass.describe_source(s))}\n"
        f"🔁 Режим: {esc(ass.describe_run_mode(s))}\n"
        f"🗓 Периодичность: {esc(ass.describe_period(s))}\n"
        f"🕐 Время: <b>{', '.join(s['times'])}</b> (Астана)\n"
        f"📅 Срок: с {_fmt_d(s['start_date'])} · {esc(ass.describe_end(s))}\n"
        f"💬 Чат: <code>{esc(str(s['chat_id']))}</code> · 📢 канал: {chan} · "
        f"⏳ ожидание {max(1, round(s['announce_delay'] / 60))} мин\n"
        f"💎 Платные: {'да' if s.get('allow_paid') else 'нет'} · "
        f"🔐 Приватные: {'да' if s.get('allow_private') else 'нет'}\n\n"
        f"📚 Тестов в списке: <b>{st.get('total_eligible', 0)}</b> · ещё не запускались: "
        f"{len(st.get('never_run', []))}\n"
        f"📊 Запусков: <b>{st.get('total_runs', 0)}</b> · уникальных участников: "
        f"<b>{st.get('unique_players', 0)}</b> · активных в чате: {st.get('chat_activity', 0)}\n"
        f"🕓 Последний запуск: {esc(last)}")


def _card_kb(s: dict) -> InlineKeyboardMarkup:
    i = s["id"]
    rows = []
    if s["status"] == "active":
        rows.append([_btn("⏸ Пауза", f"sc:pause:{i}"), _btn("⏹ Остановить", f"sc:stop:{i}")])
    elif s["status"] == "paused":
        rows.append([_btn("▶️ Продолжить", f"sc:resume:{i}"), _btn("⏹ Остановить", f"sc:stop:{i}")])
    else:
        rows.append([_btn("▶️ Включить снова", f"sc:resume:{i}")])
    rows += [
        [_btn("📂 Разделы", f"sc:ed:{i}:cats"), _btn("📝 Тесты вручную", f"sc:ed:{i}:tests")],
        [_btn("🔁 Режим запуска", f"sc:ed:{i}:mode"), _btn("🎯 Работа над ошибками", f"sc:ed:{i}:hardest")],
        [_btn("🗓 Периодичность", f"sc:ed:{i}:period"), _btn("🕐 Время", f"sc:ed:{i}:times")],
        [_btn("📅 Дата начала", f"sc:ed:{i}:start"), _btn("⏳ Срок", f"sc:ed:{i}:end")],
        [_btn("💬 Чат и канал", f"sc:ed:{i}:chat"), _btn("⏱ Ожидание", f"sc:ed:{i}:delay")],
        [_btn(("💎 Платные: да" if s.get("allow_paid") else "💎 Платные: нет"), f"sc:paid:{i}"),
         _btn(("🔐 Приватные: да" if s.get("allow_private") else "🔐 Приватные: нет"), f"sc:priv:{i}")],
        [_btn("📜 История запусков", f"sc:hist:{i}"), _btn("📊 Статистика", f"sc:stats:{i}")],
        [_btn("✏️ Название", f"sc:ed:{i}:title"), _btn("🗑 Удалить", f"sc:del:{i}")],
        [_btn("↩️ К списку", "sched:menu")],
    ]
    return _kb(rows)


async def _show_card(target, sid: int):
    s = ass.get_schedule(sid)
    if not s:
        text, kb = _list_screen()
        await _show(target, text, kb)
        return
    await _show(target, _card_text(s), _card_kb(s))


@router.callback_query(F.data == "sched:menu", IsAdmin())
async def cb_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = _list_screen()
    await _show(call, text, kb)
    await call.answer()


@router.callback_query(F.data.startswith("sc:card:"), IsAdmin())
async def cb_card(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show_card(call, int(call.data.split(":")[2]))
    await call.answer()


@router.callback_query(F.data == "noop", IsAdmin())
async def cb_noop(call: CallbackQuery):
    await call.answer()


# ───────────────────────── управление ─────────────────────────

@router.callback_query(F.data.regexp(r"^sc:(pause|resume|stop):\d+$"), IsAdmin())
async def cb_status(call: CallbackQuery, state: FSMContext):
    _, action, sid = call.data.split(":")
    sid = int(sid)
    ass.set_status(sid, {"pause": "paused", "resume": "active", "stop": "stopped"}[action])
    await call.answer({"pause": "Пауза: запуски приостановлены", "resume": "Расписание работает",
                       "stop": "Расписание остановлено"}[action], show_alert=False)
    await _show_card(call, sid)


@router.callback_query(F.data.regexp(r"^sc:(paid|priv):\d+$"), IsAdmin())
async def cb_flags(call: CallbackQuery):
    _, what, sid = call.data.split(":")
    s = ass.get_schedule(int(sid))
    if s:
        col = "allow_paid" if what == "paid" else "allow_private"
        ass.update_schedule(s["id"], **{col: 0 if s.get(col) else 1})
    await call.answer()
    await _show_card(call, int(sid))


@router.callback_query(F.data.startswith("sc:del:"), IsAdmin())
async def cb_del(call: CallbackQuery):
    sid = int(call.data.split(":")[2])
    await _show(call, f"🗑 Удалить расписание #{sid} вместе с историей запусков?\n\n"
                      f"Результаты учеников не затрагиваются.",
                _kb([[_btn("🗑 Да, удалить", f"sc:delyes:{sid}"), _btn("↩️ Нет", f"sc:card:{sid}")]]))
    await call.answer()


@router.callback_query(F.data.startswith("sc:delyes:"), IsAdmin())
async def cb_delyes(call: CallbackQuery, state: FSMContext):
    ass.delete_schedule(int(call.data.split(":")[2]))
    await call.answer("Удалено")
    text, kb = _list_screen()
    await _show(call, text, kb)


@router.callback_query(F.data.startswith("sc:hist:"), IsAdmin())
async def cb_hist(call: CallbackQuery):
    sid = int(call.data.split(":")[2])
    rows = ass.run_history(sid, 15)
    lines = [f"📜 <b>История запусков #{sid}</b>\n"]
    if not rows:
        lines.append("Запусков ещё не было.")
    for r in rows:
        if not r.get("test_id"):
            lines.append(f"• {r['when']} — {r['status_label']} {esc(r.get('error') or '')}")
            continue
        lines.append(f"• план <b>{r['planned']}</b> · факт {r['actual']} — «{esc(r['title'])}» · {esc(r['category'])}\n"
                     f"   получили {r.get('reached') or 0} · начали {r.get('started_count') or 0} · "
                     f"завершили {r.get('finished_count') or 0} · {r['status_label']}"
                     + (f" — {esc(r['error'])}" if r.get("error") else ""))
    await _show(call, "\n".join(lines)[:4000], _kb([[_btn("↩️ К расписанию", f"sc:card:{sid}")]]))
    await call.answer()


@router.callback_query(F.data.startswith("sc:stats:"), IsAdmin())
async def cb_stats(call: CallbackQuery):
    sid = int(call.data.split(":")[2])
    st = ass.get_schedule_stats(sid)
    lines = [f"📊 <b>Статистика расписания #{sid}</b>\n",
             f"Запусков: <b>{st.get('total_runs', 0)}</b> · уникальных участников: "
             f"<b>{st.get('unique_players', 0)}</b> · активных в чате: {st.get('chat_activity', 0)}\n"]
    runs = st.get("runs", [])
    if runs:
        lines.append("<b>Запускавшиеся тесты:</b>")
        for r in runs[:20]:
            lines.append(f"• «{esc(r['title'] or '—')}» — запусков {r['run_count']}, участников "
                         f"{r['total_parts']}, завершили {r['total_finished']}")
    never = st.get("never_run", [])
    if never:
        lines.append(f"\n🆕 <b>Ещё не запускались ({len(never)}):</b>")
        lines += [f"• «{esc(t['title'])}»" for t in never[:10]]
    await _show(call, "\n".join(lines)[:4000], _kb([[_btn("↩️ К расписанию", f"sc:card:{sid}")]]))
    await call.answer()


# ───────────────────────── мастер: переходы ─────────────────────────

async def _advance(target, state: FSMContext, done: str):
    """Шаг `done` завершён — открыть следующий или закончить."""
    w = await _w(state)
    w.pop("await", None)
    await _save(state, w)
    await state.set_state(None)             # экраны с кнопками текста не ждут
    steps = w.get("steps") or ALL_STEPS
    i = steps.index(done) if done in steps else -1
    for nxt in steps[i + 1:]:
        if nxt == "tests" and (w.get("source") or "auto") == "auto":
            continue
        if nxt == "loop" and (w.get("mode") or "smart") not in ("one", "all"):
            continue
        if nxt == "per_run" and (w.get("mode") or "smart") == "all":
            continue
        await SCREENS[nxt](target, state)
        return
    await _finish(target, state)


async def _finish(target, state: FSMContext):
    w = await _w(state)
    fields = _to_fields(w)
    if w.get("mode_kind") == "edit":
        keys = set()
        for step in w.get("steps") or []:
            keys |= {"title": {"title"}, "cats": {"category_ids"}, "source": {"source"},
                     "hardest": {"hardest_n"}, "tests": {"test_ids"},
                     "period": {"period", "weekdays", "interval_days", "dates"}, "times": {"times"},
                     "start": {"start_date"}, "end": {"end_mode", "end_date"},
                     "mode": {"run_mode"}, "loop": {"loop"}, "per_run": {"tests_per_day"},
                     "chat": {"chat_id"}, "channel": {"channel_id"}, "delay": {"announce_delay"}}.get(step, set())
        upd = {k: v for k, v in fields.items() if k in keys}
        if "source" in upd or "category_ids" in upd:
            upd["cursor"] = 0                       # список изменился — с начала
        if "test_ids" in upd:
            upd["cursor"] = 0
        sid = w["sid"]
        ass.update_schedule(sid, **upd)
        await state.clear()
        msg = target.message if isinstance(target, CallbackQuery) else target
        await msg.answer("✅ Сохранено. Изменения действуют для будущих запусков.")
        await _show_card(target, sid)
        return
    bot_un = ""
    try:
        me = await (target.bot if hasattr(target, "bot") else target.message.bot).get_me()
        bot_un = me.username or ""
    except Exception:
        pass
    fields.update(bot_username=bot_un, created_by=target.from_user.id, status="active",
                  allow_paid=0, allow_private=0)
    sid = ass.create(fields)
    await state.clear()
    msg = target.message if isinstance(target, CallbackQuery) else target
    await msg.answer("✅ <b>Расписание создано</b>", parse_mode="HTML")
    await _show_card(msg, sid)


@router.callback_query(F.data == "sc:new", IsAdmin())
async def cb_new(call: CallbackQuery, state: FSMContext):
    await state.clear()
    w = {"mode_kind": "new", "steps": ALL_STEPS, "cats": [], "tests": [], "times": ["19:00"],
         "period": "daily", "source": "auto", "hardest": 0, "end_mode": "never", "mode": "one",
         "loop": 1, "per_run": 1, "delay": 60, "start": ass._today_almaty(), "weekdays": [], "dates": []}
    await _save(state, w)
    await SCREENS["title"](call, state)
    await call.answer()


@router.callback_query(F.data.regexp(r"^sc:ed:\d+:\w+$"), IsAdmin())
async def cb_edit(call: CallbackQuery, state: FSMContext):
    _, _, sid, field = call.data.split(":")
    s = ass.get_schedule(int(sid))
    if not s or field not in EDIT_STEPS:
        await call.answer("Не найдено", show_alert=True)
        return
    await state.clear()
    w = _from_sched(s)
    w.update(mode_kind="edit", sid=s["id"], steps=EDIT_STEPS[field])
    await _save(state, w)
    await SCREENS[EDIT_STEPS[field][0]](call, state)
    await call.answer()


@router.callback_query(F.data == "sc:cancel", IsAdmin())
async def cb_cancel(call: CallbackQuery, state: FSMContext):
    w = await _w(state)
    await state.clear()
    if w.get("sid"):
        await _show_card(call, w["sid"])
    else:
        text, kb = _list_screen()
        await _show(call, text, kb)
    await call.answer("Отменено")


def _cancel_row():
    return [_btn("❌ Отмена", "sc:cancel")]


# ───────────────────────── экраны ─────────────────────────

async def _ask_text(target, state: FSMContext, key: str, text: str, kb=None):
    w = await _w(state)
    w["await"] = key
    await _save(state, w)
    await state.set_state(SchedStates.text_input)
    await _show(target, text, kb or _kb([_cancel_row()]))


async def scr_title(target, state):
    await _ask_text(target, state, "title",
                    "✏️ <b>Название расписания</b>\n\nНапример: «Вечерние тесты по истории».\n"
                    "Можно пропустить — название подставится само.",
                    _kb([[_btn("⏭ Пропустить", "sc:title:skip")], _cancel_row()]))


@router.callback_query(F.data == "sc:title:skip", IsAdmin())
async def cb_title_skip(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await _advance(call, state, "title")


def _cats_kb(w: dict, page: int):
    cats = db.fetchall("SELECT * FROM test_categories ORDER BY sort_order, name")
    total = len(cats)
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, pages - 1))
    chosen = set(int(c) for c in w.get("cats") or [])
    rows = []
    for c in cats[page * PER_PAGE:(page + 1) * PER_PAGE]:
        mark = "✅ " if c["id"] in chosen else ""
        rows.append([_btn(f"{mark}{c.get('emoji') or '📚'} {(c['name'] or '')[:36]}", f"sc:c:{c['id']}")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(_btn("◀️", f"sc:cp:{page - 1}"))
        nav.append(_btn(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(_btn("▶️", f"sc:cp:{page + 1}"))
        rows.append(nav)
    rows.append([_btn("🚫 Без раздела (только свой список)", "sc:c:none")])
    rows.append([_btn(f"✅ Готово ({len(chosen)} выбрано)", "sc:c:ok")])
    rows.append(_cancel_row())
    text = ("📂 <b>Разделы</b>\n\nОтметьте один или несколько разделов — тесты будут браться из них. "
            "Или «Без раздела»: тогда запускаться будет только список, выбранный вручную.\n\n"
            f"Выбрано: <b>{len(chosen)}</b>" + (f" · страница {page + 1}/{pages}" if pages > 1 else ""))
    return text, _kb(rows)


async def scr_cats(target, state, page: int = 0):
    w = await _w(state)
    await state.set_state(None)
    text, kb = _cats_kb(w, page)
    await _show(target, text, kb)


@router.callback_query(F.data.startswith("sc:cp:"), IsAdmin())
async def cb_cats_page(call: CallbackQuery, state: FSMContext):
    await scr_cats(call, state, int(call.data.split(":")[2]))
    await call.answer()


@router.callback_query(F.data.startswith("sc:c:"), IsAdmin())
async def cb_cats_toggle(call: CallbackQuery, state: FSMContext):
    arg = call.data.split(":")[2]
    w = await _w(state)
    if arg == "ok":
        await call.answer()
        await _advance(call, state, "cats")
        return
    if arg == "none":
        w["cats"] = []
        if (w.get("source") or "auto") == "auto":
            w["source"] = "manual"
        await _save(state, w)
        await call.answer("Без раздела")
        await _advance(call, state, "cats")
        return
    cid = int(arg)
    cats = [int(c) for c in w.get("cats") or []]
    if cid in cats:
        cats.remove(cid)
    else:
        cats.append(cid)
    w["cats"] = cats
    await _save(state, w)
    text, kb = _cats_kb(w, 0)
    await _show(call, text, kb)
    await call.answer()


async def scr_source(target, state):
    w = await _w(state)
    has_cats = bool(w.get("cats"))
    rows = []
    if has_cats:
        rows.append([_btn("📂 Все тесты выбранных разделов", "sc:s:auto")])
        rows.append([_btn("📂 + ✋ Разделы плюс тесты вручную", "sc:s:both")])
    else:
        rows.append([_btn("🌐 Все тесты бота", "sc:s:auto")])
    rows.append([_btn("✋ Только выбранные вручную", "sc:s:manual")])
    rows.append(_cancel_row())
    await _show(target, "🧩 <b>Источник тестов</b>\n\nОткуда брать тесты для запусков?", _kb(rows))


@router.callback_query(F.data.startswith("sc:s:"), IsAdmin())
async def cb_source(call: CallbackQuery, state: FSMContext):
    w = await _w(state)
    w["source"] = call.data.split(":")[2]
    await _save(state, w)
    await call.answer()
    await _advance(call, state, "source")


async def scr_hardest(target, state):
    w = await _w(state)
    cur = int(w.get("hardest") or 0)
    rows = [[_btn(("✅ " if cur == n else "") + (f"{n} тестов" if n else "не добавлять"), f"sc:h:{n}")
             for n in (0, 3, 5, 10)], _cancel_row()]
    await _show(target, "🎯 <b>Работа над ошибками</b>\n\nДобавлять к списку тесты, где ученики "
                        "ошибаются чаще всего (считается по реальным ответам)? Список обновляется "
                        "перед каждым запуском.", _kb(rows))


@router.callback_query(F.data.startswith("sc:h:"), IsAdmin())
async def cb_hardest(call: CallbackQuery, state: FSMContext):
    w = await _w(state)
    w["hardest"] = int(call.data.split(":")[2])
    await _save(state, w)
    await call.answer()
    await _advance(call, state, "hardest")


def _tests_kb(w: dict, page: int):
    cats = [int(c) for c in w.get("cats") or []]
    params = []
    where = "status='active' AND (SELECT COUNT(*) FROM questions WHERE test_id=tests.id) > 0"
    if cats:
        where += f" AND category_id IN ({','.join('?' * len(cats))})"
        params += cats
    tests = db.fetchall(f"SELECT id, title FROM tests WHERE {where} ORDER BY category_id, title, id",
                        tuple(params))
    chosen = [int(t) for t in w.get("tests") or []]
    total = len(tests)
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, pages - 1))
    rows = []
    for t in tests[page * PER_PAGE:(page + 1) * PER_PAGE]:
        mark = f"✅ {chosen.index(t['id']) + 1}. " if t["id"] in chosen else ""
        rows.append([_btn(f"{mark}{(t['title'] or '')[:34]}", f"sc:t:{t['id']}")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(_btn("◀️", f"sc:tp:{page - 1}"))
        nav.append(_btn(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(_btn("▶️", f"sc:tp:{page + 1}"))
        rows.append(nav)
    rows.append([_btn("🧹 Снять всё", "sc:t:clear"), _btn(f"✅ Готово ({len(chosen)})", "sc:t:ok")])
    rows.append(_cancel_row())
    text = ("📝 <b>Тесты вручную</b>\n\nНажимайте тесты по порядку — номер показывает очередь запуска. "
            "Повторное нажатие убирает тест из списка. Добавлять и убирать можно и потом, "
            "из карточки расписания.\n\n"
            f"Выбрано: <b>{len(chosen)}</b> из {total}" + (f" · страница {page + 1}/{pages}" if pages > 1 else ""))
    return text, _kb(rows)


async def scr_tests(target, state, page: int = 0):
    w = await _w(state)
    await state.set_state(None)
    text, kb = _tests_kb(w, page)
    await _show(target, text, kb)


@router.callback_query(F.data.startswith("sc:tp:"), IsAdmin())
async def cb_tests_page(call: CallbackQuery, state: FSMContext):
    w = await _w(state)
    w["tpage"] = int(call.data.split(":")[2])
    await _save(state, w)
    await scr_tests(call, state, w["tpage"])
    await call.answer()


@router.callback_query(F.data.startswith("sc:t:"), IsAdmin())
async def cb_tests_toggle(call: CallbackQuery, state: FSMContext):
    arg = call.data.split(":")[2]
    w = await _w(state)
    if arg == "ok":
        if not w.get("tests") and (w.get("source") or "auto") == "manual" and not w.get("cats"):
            await call.answer("Выберите хотя бы один тест", show_alert=True)
            return
        await call.answer()
        await _advance(call, state, "tests")
        return
    if arg == "clear":
        w["tests"] = []
    else:
        tid = int(arg)
        tests = [int(t) for t in w.get("tests") or []]
        if tid in tests:
            tests.remove(tid)
        else:
            tests.append(tid)
        w["tests"] = tests
    await _save(state, w)
    text, kb = _tests_kb(w, int(w.get("tpage") or 0))
    await _show(call, text, kb)
    await call.answer()


async def scr_period(target, state):
    rows = [[_btn("📆 Каждый день", "sc:p:daily"), _btn("↔️ Через день", "sc:p:alt")],
            [_btn("🗓 По дням недели", "sc:p:weekdays"), _btn("🔢 Каждые N дней", "sc:p:everyn")],
            [_btn("📌 Конкретные даты", "sc:p:dates")], _cancel_row()]
    await _show(target, "🗓 <b>Периодичность</b>\n\nКак часто запускать?", _kb(rows))


@router.callback_query(F.data.startswith("sc:p:"), IsAdmin())
async def cb_period(call: CallbackQuery, state: FSMContext):
    w = await _w(state)
    p = call.data.split(":")[2]
    w["period"] = p
    await _save(state, w)
    await call.answer()
    if p == "weekdays":
        await scr_weekdays(call, state)
    elif p == "everyn":
        await _ask_text(call, state, "everyn", "🔢 Раз в сколько дней запускать? Введите число, например <code>3</code>.")
    elif p == "dates":
        await _ask_text(call, state, "dates",
                        "📌 Введите даты через запятую или с новой строки, формат ГГГГ-ММ-ДД:\n"
                        "<code>2026-09-15, 2026-09-22, 2026-10-01</code>")
    else:
        await _advance(call, state, "period")


def _weekdays_kb(w: dict):
    chosen = {int(x) for x in w.get("weekdays") or []}
    row1 = [_btn(("✅" if i in chosen else "") + ass.WEEKDAYS[i], f"sc:wd:{i}") for i in range(7)]
    return _kb([row1[:4], row1[4:], [_btn("Пн–Пт", "sc:wd:work"), _btn("Сб–Вс", "sc:wd:weekend")],
                [_btn(f"✅ Готово ({len(chosen)})", "sc:wd:ok")], _cancel_row()])


async def scr_weekdays(target, state):
    w = await _w(state)
    await _show(target, "🗓 <b>Дни недели</b>\n\nОтметьте дни запуска (Астана):", _weekdays_kb(w))


@router.callback_query(F.data.startswith("sc:wd:"), IsAdmin())
async def cb_weekdays(call: CallbackQuery, state: FSMContext):
    arg = call.data.split(":")[2]
    w = await _w(state)
    days = {int(x) for x in w.get("weekdays") or []}
    if arg == "ok":
        if not days:
            await call.answer("Выберите хотя бы один день", show_alert=True)
            return
        await call.answer()
        await _advance(call, state, "period")
        return
    if arg == "work":
        days = {0, 1, 2, 3, 4}
    elif arg == "weekend":
        days = {5, 6}
    else:
        d = int(arg)
        days ^= {d}
    w["weekdays"] = sorted(days)
    await _save(state, w)
    await _show(call, "🗓 <b>Дни недели</b>\n\nОтметьте дни запуска (Астана):", _weekdays_kb(w))
    await call.answer()


async def scr_times(target, state):
    w = await _w(state)
    cur = ", ".join(w.get("times") or [])
    rows = [[_btn("10:00", "sc:tm:10:00"), _btn("15:00", "sc:tm:15:00"), _btn("19:00", "sc:tm:19:00"),
             _btn("20:00", "sc:tm:20:00")], _cancel_row()]
    await _ask_text(target, state, "times",
                    "🕐 <b>Время запуска</b> (Астана)\n\nВыберите кнопкой или введите своё, можно несколько "
                    f"через запятую — тогда запусков в день будет несколько: <code>10:00, 19:00</code>\n\n"
                    f"Сейчас: <b>{cur or '—'}</b>", _kb(rows))


@router.callback_query(F.data.startswith("sc:tm:"), IsAdmin())
async def cb_time_btn(call: CallbackQuery, state: FSMContext):
    w = await _w(state)
    w["times"] = ass.parse_times(call.data[len("sc:tm:"):])
    await _save(state, w)
    await call.answer()
    await _advance(call, state, "times")


async def scr_start(target, state):
    today = datetime.now(ALMATY)
    rows = [[_btn("Сегодня", "sc:st:0"), _btn("Завтра", "sc:st:1"), _btn("Через неделю", "sc:st:7")],
            _cancel_row()]
    await _ask_text(target, state, "start",
                    "📅 <b>Дата начала</b>\n\nВыберите кнопкой или введите дату ГГГГ-ММ-ДД "
                    f"(сегодня <code>{today.strftime('%Y-%m-%d')}</code>).", _kb(rows))


@router.callback_query(F.data.startswith("sc:st:"), IsAdmin())
async def cb_start_btn(call: CallbackQuery, state: FSMContext):
    w = await _w(state)
    days = int(call.data.split(":")[2])
    w["start"] = (datetime.now(ALMATY) + timedelta(days=days)).strftime("%Y-%m-%d")
    await _save(state, w)
    await call.answer()
    await _advance(call, state, "start")


async def scr_end(target, state):
    rows = [[_btn("♾ Бессрочно", "sc:e:never")],
            [_btn("1 неделя", "sc:e:d7"), _btn("1 месяц", "sc:e:d30"), _btn("3 месяца", "sc:e:d90")],
            [_btn("📅 До даты (ввести)", "sc:e:date"), _btn("🔢 Свой срок (ввести)", "sc:e:days")],
            _cancel_row()]
    await _show(target, "⏳ <b>Срок работы расписания</b>\n\n«Бессрочно» — работает, пока вы сами не "
                        "нажмёте «Остановить». Никаких скрытых ограничений по времени нет.", _kb(rows))


@router.callback_query(F.data.startswith("sc:e:"), IsAdmin())
async def cb_end(call: CallbackQuery, state: FSMContext):
    w = await _w(state)
    arg = call.data.split(":")[2]
    if arg == "never":
        w.update(end_mode="never", end_date="")
        await _save(state, w)
        await call.answer()
        await _advance(call, state, "end")
        return
    if arg.startswith("d") and arg[1:].isdigit():
        start = datetime.strptime(w.get("start") or ass._today_almaty(), "%Y-%m-%d")
        w.update(end_mode="date", end_date=(start + timedelta(days=int(arg[1:]))).strftime("%Y-%m-%d"))
        await _save(state, w)
        await call.answer()
        await _advance(call, state, "end")
        return
    await call.answer()
    if arg == "date":
        await _ask_text(call, state, "end_date", "📅 Введите дату окончания ГГГГ-ММ-ДД (включительно):")
    else:
        await _ask_text(call, state, "end_days",
                        "🔢 Введите срок: число и единицу — <code>45 дней</code>, <code>6 недель</code>, "
                        "<code>4 месяца</code>. Отсчёт от даты начала.")


async def scr_mode(target, state):
    rows = [[_btn("1️⃣ По одному тесту за запуск (по списку)", "sc:m:one")],
            [_btn("📚 Все тесты списка за один запуск", "sc:m:all")],
            [_btn("🎲 Случайный тест", "sc:m:random")],
            [_btn("🧠 Умный выбор (новые → редкие)", "sc:m:smart")], _cancel_row()]
    await _show(target, "🔁 <b>Как запускать тесты</b>\n\nВ чате идёт один тест за раз: если за запуск "
                        "выбрано несколько, они пойдут друг за другом.", _kb(rows))


@router.callback_query(F.data.startswith("sc:m:"), IsAdmin())
async def cb_mode(call: CallbackQuery, state: FSMContext):
    w = await _w(state)
    w["mode"] = call.data.split(":")[2]
    await _save(state, w)
    await call.answer()
    await _advance(call, state, "mode")


async def scr_loop(target, state):
    rows = [[_btn("🔁 Начать сначала", "sc:l:1"), _btn("⏹ Остановить расписание", "sc:l:0")], _cancel_row()]
    await _show(target, "🔚 <b>Когда список тестов закончится</b>", _kb(rows))


@router.callback_query(F.data.startswith("sc:l:"), IsAdmin())
async def cb_loop(call: CallbackQuery, state: FSMContext):
    w = await _w(state)
    w["loop"] = int(call.data.split(":")[2])
    await _save(state, w)
    await call.answer()
    await _advance(call, state, "loop")


async def scr_per_run(target, state):
    rows = [[_btn(str(n), f"sc:n:{n}") for n in (1, 2, 3, 5)], _cancel_row()]
    await _show(target, "🔢 <b>Сколько тестов за один запуск?</b>\n\nОни пойдут друг за другом.", _kb(rows))


@router.callback_query(F.data.startswith("sc:n:"), IsAdmin())
async def cb_per_run(call: CallbackQuery, state: FSMContext):
    w = await _w(state)
    w["per_run"] = int(call.data.split(":")[2])
    await _save(state, w)
    await call.answer()
    await _advance(call, state, "per_run")


async def scr_chat(target, state):
    w = await _w(state)
    cur = f"\n\nСейчас: <code>{esc(str(w['chat']))}</code>" if w.get("chat") else ""
    await _ask_text(target, state, "chat",
                    "💬 <b>Чат для тестов</b>\n\nПерешлите сюда любое сообщение из чата или введите его ID "
                    f"(например <code>-1001234567890</code>).{cur}")


async def scr_channel(target, state):
    w = await _w(state)
    cur = f"\n\nСейчас: <code>{esc(str(w['channel']))}</code>" if w.get("channel") else ""
    await _ask_text(target, state, "channel",
                    "📢 <b>Канал для анонса</b> (необязательно)\n\nПерешлите сообщение из канала или "
                    f"введите его ID. Анонс со ссылкой в бота уйдёт туда перед стартом.{cur}",
                    _kb([[_btn("⏭ Без канала", "sc:ch:skip")], _cancel_row()]))


@router.callback_query(F.data == "sc:ch:skip", IsAdmin())
async def cb_channel_skip(call: CallbackQuery, state: FSMContext):
    w = await _w(state)
    w["channel"] = None
    await _save(state, w)
    await call.answer()
    await _advance(call, state, "channel")


async def scr_delay(target, state):
    rows = [[_btn("1 минута", "sc:d:60"), _btn("2 минуты", "sc:d:120"), _btn("5 минут", "sc:d:300")],
            _cancel_row()]
    await _show(target, "⏱ <b>Сколько ждать перед стартом теста?</b>\n\nПосле анонса бот подождёт, "
                        "чтобы люди зашли в чат, и запустит тест.", _kb(rows))


@router.callback_query(F.data.startswith("sc:d:"), IsAdmin())
async def cb_delay(call: CallbackQuery, state: FSMContext):
    w = await _w(state)
    w["delay"] = int(call.data.split(":")[2])
    await _save(state, w)
    await call.answer()
    await _advance(call, state, "delay")


async def scr_confirm(target, state):
    w = await _w(state)
    preview = ass.load({**_to_fields(w), "id": 0, "status": "active"})
    text = ("✅ <b>Проверьте расписание</b>\n\n"
            f"✏️ {esc(w.get('title') or 'название подставится само')}\n"
            f"📂 Тесты: {esc(ass.describe_source(preview))}\n"
            f"🔁 Режим: {esc(ass.describe_run_mode(preview))}\n"
            f"🗓 {esc(ass.describe_period(preview))} · 🕐 {', '.join(preview['times'])}\n"
            f"📅 с {_fmt_d(preview['start_date'])} · {esc(ass.describe_end(preview))}\n"
            f"💬 чат <code>{esc(str(w.get('chat')))}</code> · 📢 канал "
            f"{('<code>' + esc(str(w['channel'])) + '</code>') if w.get('channel') else 'нет'} · "
            f"⏳ {max(1, round(int(w.get('delay') or 60) / 60))} мин\n\n"
            f"📚 Тестов в списке сейчас: <b>{len(ass.pool(preview))}</b>")
    await _show(target, text, _kb([[_btn("🚀 Создать и включить", "sc:ok:create")], _cancel_row()]))


@router.callback_query(F.data == "sc:ok:create", IsAdmin())
async def cb_create(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await _finish(call, state)


SCREENS = {"title": scr_title, "cats": scr_cats, "source": scr_source, "hardest": scr_hardest,
           "tests": scr_tests, "period": scr_period, "times": scr_times, "start": scr_start,
           "end": scr_end, "mode": scr_mode, "loop": scr_loop, "per_run": scr_per_run,
           "chat": scr_chat, "channel": scr_channel, "delay": scr_delay, "confirm": scr_confirm}


# ───────────────────────── ввод текста ─────────────────────────

def _parse_span(text: str):
    """«45 дней», «6 недель», «4 месяца» → количество дней."""
    parts = text.lower().replace(",", " ").split()
    if not parts or not parts[0].isdigit():
        return None
    n = int(parts[0])
    unit = parts[1] if len(parts) > 1 else "д"
    if unit.startswith(("д", "d")):
        return n
    if unit.startswith(("н", "w")):
        return n * 7
    if unit.startswith(("м", "m")):
        return n * 30
    return None


@router.message(SchedStates.text_input, IsAdmin())
async def on_text(message: Message, state: FSMContext):
    w = await _w(state)
    key = w.get("await")
    text = (message.text or "").strip()
    if text.startswith("/cancel"):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    if key == "title":
        w["title"] = text[:60]
    elif key == "everyn":
        if not text.isdigit() or not 1 <= int(text) <= 365:
            await message.answer("Введите число дней от 1 до 365.")
            return
        w["interval"] = int(text)
    elif key == "dates":
        ds = [p.strip() for p in text.replace("\n", ",").replace(";", ",").split(",") if p.strip()]
        bad = [d for d in ds if not ass._valid_date(d)]
        if not ds or bad:
            await message.answer(f"Не понял даты: {', '.join(bad) or 'пусто'}. Формат ГГГГ-ММ-ДД, через запятую.")
            return
        w["dates"] = sorted(set(ds))
        if not w.get("start") or w["start"] > w["dates"][0]:
            w["start"] = w["dates"][0]
    elif key == "times":
        parsed = ass.parse_times(text)
        if not any(ch.isdigit() for ch in text):
            await message.answer("Введите время, например 19:00 или 10:00, 19:00.")
            return
        w["times"] = parsed
    elif key == "start":
        if not ass._valid_date(text):
            await message.answer("Введите дату как 2026-09-15.")
            return
        w["start"] = text
    elif key == "end_date":
        if not ass._valid_date(text) or text < (w.get("start") or ass._today_almaty()):
            await message.answer("Введите дату окончания как 2026-12-31 — не раньше даты начала.")
            return
        w.update(end_mode="date", end_date=text)
        key = "end"
    elif key == "end_days":
        days = _parse_span(text)
        if not days:
            await message.answer("Например: 45 дней, 6 недель или 4 месяца.")
            return
        start = datetime.strptime(w.get("start") or ass._today_almaty(), "%Y-%m-%d")
        w.update(end_mode="date", end_date=(start + timedelta(days=days)).strftime("%Y-%m-%d"))
        key = "end"
    elif key in ("chat", "channel"):
        cid = None
        if message.forward_from_chat:
            cid = message.forward_from_chat.id
        elif text.lstrip("-").isdigit():
            cid = int(text)
        if not cid:
            await message.answer("Не понял. Перешлите сообщение из чата/канала или введите ID числом.")
            return
        w[key] = cid
    else:
        await state.clear()
        await message.answer("Сессия настройки потеряна — начните заново.")
        return
    w.pop("await", None)
    await _save(state, w)
    await state.set_state(None)
    step = {"everyn": "period", "dates": "period", "end_date": "end", "end_days": "end"}.get(key, key)
    await _advance(message, state, step)
