"""
Админ-интерфейс автопубликации квизов из выбранных тестов в канал.

Мастер: тесты (чекбоксы) → 5/10 вопросов → период → время → канал.
Управление: пауза/возобновить/остановить/сейчас/история/удалить.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Router, F, Bot
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                            InlineKeyboardButton)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

import database as db
from filters import IsAdmin
from services import quiz_publish_service as qps

router = Router(name="quiz_publish")
log = logging.getLogger(__name__)
ALMATY = timezone(timedelta(hours=5))

PAGE_SIZE = 8
DAY_LABELS = [('MO', 'Пн'), ('TU', 'Вт'), ('WE', 'Ср'), ('TH', 'Чт'),
              ('FR', 'Пт'), ('SA', 'Сб'), ('SU', 'Вс')]


class QuizPubStates(StatesGroup):
    waiting_time = State()
    waiting_channel = State()


def _sched_label(job: dict) -> str:
    st = job.get('schedule_type')
    if st == 'daily':
        return "каждый день"
    if st == 'every2':
        return "через день"
    if st == 'weekdays':
        codes = (job.get('weekdays') or '').split(',')
        names = [n for c, n in DAY_LABELS if c in codes]
        return "по дням: " + ", ".join(names)
    return st or "?"


# ================= МЕНЮ =================

@router.callback_query(F.data == "qp:menu", IsAdmin())
async def cb_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    jobs = qps.list_jobs()
    lines = ["📅 <b>Вопросы в канал по расписанию</b>\n"]
    kb = InlineKeyboardBuilder()
    if jobs:
        for j in jobs:
            status = "🟢" if j['status'] == 'active' else "⏸"
            lines.append(
                f"{status} #{j['id']}: {j['questions_per_run']} вопр · "
                f"{_sched_label(j)} · {j['run_time']}")
            kb.button(text=f"⚙️ Задача #{j['id']}",
                      callback_data=f"qp:job:{j['id']}")
    else:
        lines.append("Пока нет активных задач.\n\n"
                     "Создай — бот будет сам публиковать квизы из выбранных "
                     "тестов в канал по расписанию, пока не отменишь.")
    kb.button(text="➕ Создать автопубликацию", callback_data="qp:new")
    kb.button(text="↩️ Назад", callback_data="adm:autopub")
    kb.adjust(1)
    try:
        await call.message.edit_text("\n".join(lines),
                                      reply_markup=kb.as_markup(),
                                      parse_mode="HTML")
    except Exception:
        await call.message.answer("\n".join(lines),
                                    reply_markup=kb.as_markup(),
                                    parse_mode="HTML")
    await call.answer()


# ================= МАСТЕР: ШАГ 1 — ТЕСТЫ ЧЕКБОКСАМИ =================

def _tests_page(selected: set, page: int):
    tests = db.fetchall(
        """SELECT t.id, t.title, COALESCE(t.is_paid,0) AS is_paid,
                  (SELECT COUNT(*) FROM questions WHERE test_id=t.id) AS qc
           FROM tests t WHERE t.status='active'
             AND (SELECT COUNT(*) FROM questions WHERE test_id=t.id) > 0
           ORDER BY t.id DESC""")
    tests = [dict(t) for t in tests]
    total_pages = max(1, (len(tests) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = tests[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    kb = InlineKeyboardBuilder()
    for t in chunk:
        mark = "✅" if t['id'] in selected else "⬜️"
        paid = " 💎" if t['is_paid'] else ""
        title = t['title'][:28]
        kb.button(text=f"{mark} {title}{paid} ({t['qc']})",
                  callback_data=f"qpt:{t['id']}:{page}")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"qppage:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"qppage:{page+1}"))
    kb.adjust(1)
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text=f"✅ Готово ({len(selected)})",
                                 callback_data="qp:tests_done"))
    kb.row(InlineKeyboardButton(text="❌ Отмена", callback_data="qp:menu"))
    text = (f"📚 <b>Шаг 1/5:</b> Выбери тесты галочками\n"
            f"(вопросы будут браться из них поровну)\n\n"
            f"Выбрано: <b>{len(selected)}</b> · Стр. {page+1}/{total_pages}")
    return text, kb.as_markup()


@router.callback_query(F.data == "qp:new", IsAdmin())
async def cb_new(call: CallbackQuery, state: FSMContext):
    await state.update_data(qp_tests=[], qp_page=0)
    text, kb = _tests_page(set(), 0)
    await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("qpt:"), IsAdmin())
async def cb_toggle_test(call: CallbackQuery, state: FSMContext):
    _, tid, page = call.data.split(":")
    tid, page = int(tid), int(page)
    data = await state.get_data()
    selected = set(data.get('qp_tests') or [])
    if tid in selected:
        selected.discard(tid)
    else:
        selected.add(tid)
    await state.update_data(qp_tests=list(selected), qp_page=page)
    text, kb = _tests_page(selected, page)
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("qppage:"), IsAdmin())
async def cb_page(call: CallbackQuery, state: FSMContext):
    page = int(call.data.split(":")[1])
    data = await state.get_data()
    selected = set(data.get('qp_tests') or [])
    await state.update_data(qp_page=page)
    text, kb = _tests_page(selected, page)
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data == "qp:tests_done", IsAdmin())
async def cb_tests_done(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get('qp_tests') or []
    if not selected:
        await call.answer("Выбери хотя бы один тест!", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="5 вопросов", callback_data="qpn:5")
    kb.button(text="10 вопросов", callback_data="qpn:10")
    kb.button(text="❌ Отмена", callback_data="qp:menu")
    kb.adjust(2, 1)
    await call.message.answer(
        f"🔢 <b>Шаг 2/5:</b> Сколько вопросов публиковать за раз?\n\n"
        f"Выбрано тестов: {len(selected)}",
        reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("qpn:"), IsAdmin())
async def cb_count(call: CallbackQuery, state: FSMContext):
    n = int(call.data.split(":")[1])
    await state.update_data(qp_n=n)
    kb = InlineKeyboardBuilder()
    kb.button(text="📆 Каждый день", callback_data="qps:daily")
    kb.button(text="🔁 Через день", callback_data="qps:every2")
    kb.button(text="🗓 По дням недели", callback_data="qps:weekdays")
    kb.button(text="❌ Отмена", callback_data="qp:menu")
    kb.adjust(1)
    await call.message.answer(
        "📅 <b>Шаг 3/5:</b> Как часто публиковать?",
        reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("qps:"), IsAdmin())
async def cb_sched(call: CallbackQuery, state: FSMContext):
    st = call.data.split(":")[1]
    await state.update_data(qp_sched=st, qp_days=[])
    if st == 'weekdays':
        text, kb = _days_kb(set())
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
        await call.answer()
        return
    await _ask_time(call.message, state)
    await call.answer()


def _days_kb(selected: set):
    kb = InlineKeyboardBuilder()
    for code, name in DAY_LABELS:
        mark = "✅" if code in selected else "⬜️"
        kb.button(text=f"{mark} {name}", callback_data=f"qpd:{code}")
    kb.adjust(4, 3)
    kb.row(InlineKeyboardButton(text=f"✅ Готово ({len(selected)})",
                                 callback_data="qp:days_done"))
    kb.row(InlineKeyboardButton(text="❌ Отмена", callback_data="qp:menu"))
    return "🗓 Отметь дни недели для публикации:", kb.as_markup()


@router.callback_query(F.data.startswith("qpd:"), IsAdmin())
async def cb_day_toggle(call: CallbackQuery, state: FSMContext):
    code = call.data.split(":")[1]
    data = await state.get_data()
    days = set(data.get('qp_days') or [])
    if code in days:
        days.discard(code)
    else:
        days.add(code)
    await state.update_data(qp_days=list(days))
    text, kb = _days_kb(days)
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data == "qp:days_done", IsAdmin())
async def cb_days_done(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not (data.get('qp_days') or []):
        await call.answer("Отметь хотя бы один день!", show_alert=True)
        return
    await _ask_time(call.message, state)
    await call.answer()


async def _ask_time(message, state: FSMContext):
    await state.set_state(QuizPubStates.waiting_time)
    await message.answer(
        "🕐 <b>Шаг 4/5:</b> Во сколько публиковать?\n\n"
        "Введи время в формате ЧЧ:ММ по Астане (UTC+5)\n"
        "Например: <code>19:00</code>\n\n/cancel — отмена",
        parse_mode="HTML")


@router.message(QuizPubStates.waiting_time, IsAdmin())
async def msg_time(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    t = (message.text or '').strip()
    try:
        datetime.strptime(t, "%H:%M")
    except ValueError:
        await message.answer("Неверный формат. Введи как 19:00")
        return
    await state.update_data(qp_time=t)
    await state.set_state(QuizPubStates.waiting_channel)
    await message.answer(
        "📢 <b>Шаг 5/5:</b> В какой канал публиковать?\n\n"
        "Перешли сюда любое сообщение из КАНАЛА, или введи его ID "
        "(например -1001234567890).\n\n"
        "⚠️ Бот должен быть админом канала!\n\n/cancel",
        parse_mode="HTML")


@router.message(QuizPubStates.waiting_channel, IsAdmin())
async def msg_channel(message: Message, state: FSMContext, user: dict):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    channel_id = None
    if message.forward_from_chat:
        channel_id = message.forward_from_chat.id
    elif message.text and message.text.strip().lstrip('-').isdigit():
        channel_id = int(message.text.strip())
    if not channel_id:
        await message.answer("Не понял канал. Перешли сообщение из канала или введи ID.")
        return
    data = await state.get_data()
    job_id = qps.create_job(
        created_by=user.get('tg_id') or message.from_user.id,
        test_ids=data.get('qp_tests') or [],
        questions_per_run=data.get('qp_n', 5),
        schedule_type=data.get('qp_sched', 'daily'),
        weekdays=",".join(data.get('qp_days') or []),
        run_time=data.get('qp_time'),
        channel_id=channel_id)
    await state.clear()
    job = qps.get_job(job_id)
    text, kb = _job_card(job)
    await message.answer("✅ <b>Автопубликация создана!</b>\n\n" + text,
                          reply_markup=kb, parse_mode="HTML")


# ================= КАРТОЧКА ЗАДАЧИ =================

def _job_card(job: dict):
    tids = qps.job_tests(job)
    titles = []
    for tid in tids[:5]:
        t = db.fetchone("SELECT title FROM tests WHERE id=?", (tid,))
        if t:
            titles.append(t['title'][:30])
    ttl = ", ".join(f"«{x}»" for x in titles)
    if len(tids) > 5:
        ttl += f" … +{len(tids)-5}"
    status_map = {'active': '🟢 Активна', 'paused': '⏸ На паузе',
                  'stopped': '⏹ Остановлена'}
    text = (
        f"📅 <b>Автопубликация #{job['id']}</b>\n\n"
        f"📚 Тесты ({len(tids)}): {ttl}\n"
        f"🔢 Вопросов за раз: <b>{job['questions_per_run']}</b>\n"
        f"📆 Период: <b>{_sched_label(job)}</b>\n"
        f"🕐 Время: <b>{job['run_time']}</b> (Астана)\n"
        f"📢 Канал: <code>{job['channel_id']}</code>\n"
        f"📊 Статус: {status_map.get(job['status'], job['status'])}\n"
        f"📤 Опубликовано всего: {qps.published_total(job['id'])}\n"
        f"🗓 Последний запуск: {job.get('last_run_date') or '—'}")
    kb = InlineKeyboardBuilder()
    if job['status'] == 'active':
        kb.button(text="⏸ Пауза", callback_data=f"qp:pause:{job['id']}")
    elif job['status'] == 'paused':
        kb.button(text="▶️ Возобновить", callback_data=f"qp:resume:{job['id']}")
    kb.button(text="🚀 Опубликовать сейчас", callback_data=f"qp:runnow:{job['id']}")
    kb.button(text="📜 История", callback_data=f"qp:hist:{job['id']}")
    kb.button(text="🗑 Удалить задачу", callback_data=f"qp:del:{job['id']}")
    kb.button(text="↩️ К списку", callback_data="qp:menu")
    kb.adjust(1)
    return text, kb.as_markup()


@router.callback_query(F.data.startswith("qp:job:"), IsAdmin())
async def cb_job(call: CallbackQuery):
    job = qps.get_job(int(call.data.split(":")[2]))
    if not job:
        await call.answer("Задача не найдена.", show_alert=True)
        return
    text, kb = _job_card(job)
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("qp:pause:"), IsAdmin())
async def cb_pause(call: CallbackQuery):
    jid = int(call.data.split(":")[2])
    qps.set_status(jid, 'paused')
    await call.answer("Пауза ⏸")
    call.data = f"qp:job:{jid}"
    await cb_job(call)


@router.callback_query(F.data.startswith("qp:resume:"), IsAdmin())
async def cb_resume(call: CallbackQuery):
    jid = int(call.data.split(":")[2])
    qps.set_status(jid, 'active')
    await call.answer("Возобновлено ▶️")
    call.data = f"qp:job:{jid}"
    await cb_job(call)


@router.callback_query(F.data.startswith("qp:runnow:"), IsAdmin())
async def cb_runnow(call: CallbackQuery):
    jid = int(call.data.split(":")[2])
    job = qps.get_job(jid)
    if not job:
        await call.answer("Не найдена.", show_alert=True)
        return
    await call.answer("Публикую…")
    sent, failed = await qps.publish_run(call.bot, job)
    await call.message.answer(
        f"✅ Опубликовано: <b>{sent}</b>" +
        (f"\n⚠️ Ошибок: {failed} (бот админ канала?)" if failed else ""),
        parse_mode="HTML")


@router.callback_query(F.data.startswith("qp:hist:"), IsAdmin())
async def cb_hist(call: CallbackQuery):
    jid = int(call.data.split(":")[2])
    items = qps.job_history(jid, 15)
    if not items:
        await call.answer("История пуста.", show_alert=True)
        return
    lines = [f"📜 <b>История задачи #{jid}</b> (последние {len(items)}):\n"]
    for it in items:
        dt = (it.get('published_at') or '')[:16].replace('T', ' ')
        lines.append(f"• {dt} — «{it.get('title') or '?'}»: "
                     f"{(it.get('qtext') or '')[:40]}…")
    await call.message.answer("\n".join(lines), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("qp:del:"), IsAdmin())
async def cb_del(call: CallbackQuery):
    jid = int(call.data.split(":")[2])
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Да, удалить",
                              callback_data=f"qp:delok:{jid}"),
        InlineKeyboardButton(text="❌ Нет", callback_data=f"qp:job:{jid}"),
    ]])
    await call.message.answer(
        f"Удалить автопубликацию #{jid}? Публикации прекратятся, "
        f"история очистится.", reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("qp:delok:"), IsAdmin())
async def cb_delok(call: CallbackQuery):
    jid = int(call.data.split(":")[2])
    qps.delete_job(jid)
    await call.answer("Удалено 🗑", show_alert=True)
    # показать меню заново
    jobs = qps.list_jobs()
    kb = InlineKeyboardBuilder()
    for j in jobs:
        kb.button(text=f"⚙️ Задача #{j['id']}", callback_data=f"qp:job:{j['id']}")
    kb.button(text="➕ Создать автопубликацию", callback_data="qp:new")
    kb.button(text="↩️ Назад", callback_data="adm:autopub")
    kb.adjust(1)
    try:
        await call.message.edit_text("📅 <b>Вопросы в канал по расписанию</b>\n\n"
                                      "Задача удалена.",
                                      reply_markup=kb.as_markup(),
                                      parse_mode="HTML")
    except Exception:
        pass
