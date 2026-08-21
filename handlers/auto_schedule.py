"""
Админ-интерфейс планировщика автозапуска тестов.
Мастер настройки: раздел → даты → время → кол-во тестов → чат.
"""
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Router, F, Bot
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                            InlineKeyboardButton)
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


class SchedStates(StatesGroup):
    waiting_start_date = State()
    waiting_end_date = State()
    waiting_time = State()
    waiting_tests_per_day = State()
    waiting_chat_id = State()
    waiting_channel_id = State()
    waiting_delay = State()
    waiting_extend_date = State()
    waiting_new_time = State()


def _fmt_schedule(sched: dict) -> str:
    """Карточка расписания."""
    cat = None
    if sched.get('category_id'):
        cat = db.fetchone("SELECT name FROM test_categories WHERE id=?",
                          (sched['category_id'],))
    cat_name = cat['name'] if cat else "Все разделы"
    status_map = {'active': '🟢 Активно', 'stopped': '🔴 Остановлено',
                  'finished': '⚫️ Завершено'}
    status = status_map.get(sched['status'], sched['status'])

    # Следующий тест
    next_test = ass.pick_next_test(sched)
    next_name = next_test['title'] if next_test else "нет доступных"

    stats = ass.get_schedule_stats(sched['id'])
    paid = "✅ да" if sched.get('allow_paid') else "❌ нет"
    priv = "✅ да" if sched.get('allow_private') else "❌ нет"
    channel_info = "не задан"
    if sched.get('channel_id'):
        channel_info = f"<code>{sched['channel_id']}</code>"
    delay_min = max(1, round((sched.get('announce_delay') or 60) / 60))

    return (
        f"⏰ <b>Автозапуск по расписанию</b>\n\n"
        f"📂 Раздел: <b>{cat_name}</b>\n"
        f"💬 Чат: <code>{sched['chat_id']}</code>\n"
        f"📢 Канал анонса: {channel_info}\n"
        f"📅 Период: {sched['start_date']} → {sched['end_date']}\n"
        f"🕐 Время запуска: <b>{sched['daily_time']}</b> (Астана)\n"
        f"⏳ Ожидание перед стартом: {delay_min} мин\n"
        f"🔢 Тестов в день: <b>{sched.get('tests_per_day', 1)}</b>\n"
        f"💎 Платные: {paid}\n"
        f"🔐 Приватные: {priv}\n"
        f"📊 Статус: {status}\n\n"
        f"▶️ Следующий тест: «{next_name}»\n"
        f"👥 Активность в чате: {stats.get('chat_activity', 0)} чел.\n"
        f"📚 Доступно тестов: {stats.get('total_eligible', 0)}\n"
        f"🆕 Ещё не запускались: {len(stats.get('never_run', []))}")


def _menu_kb(has_active: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if has_active:
        kb.button(text="⏸ Остановить автотесты", callback_data="sched:stop")
        kb.button(text="➕ Продлить период", callback_data="sched:extend")
        kb.button(text="🕐 Изменить время запуска", callback_data="sched:time")
        kb.button(text="📅 Посмотреть расписание", callback_data="sched:view")
        kb.button(text="📊 Статистика тестов", callback_data="sched:stats")
        kb.button(text="👥 Активность участников", callback_data="sched:activity")
        kb.button(text="💎 Разрешить платные", callback_data="sched:allowpaid")
        kb.button(text="🔐 Разрешить приватные", callback_data="sched:allowpriv")
    else:
        kb.button(text="🚀 Запустить автотесты", callback_data="sched:new")
    kb.button(text="↩️ Назад", callback_data="adm:autopub")
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(F.data == "sched:menu", IsAdmin())
async def cb_sched_menu(call: CallbackQuery):
    # Ищем активное расписание (по любому чату)
    active = db.fetchone(
        "SELECT * FROM auto_schedule WHERE status='active' ORDER BY id DESC LIMIT 1")
    if active:
        text = _fmt_schedule(dict(active))
        kb = _menu_kb(True)
    else:
        text = ("⏰ <b>Автозапуск тестов по расписанию</b>\n\n"
                "Бот будет каждый день в заданное время автоматически "
                "запускать тесты выбранного раздела в чат.\n\n"
                "Умный выбор: сначала новые тесты, потом реже запускавшиеся.\n\n"
                "Нажми «🚀 Запустить автотесты» чтобы настроить.")
        kb = _menu_kb(False)
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


# ============ МАСТЕР СОЗДАНИЯ ============

CATS_PER_PAGE = 8   # больше в одну клавиатуру Telegram уже не принимает


def _cats_screen(page: int = 0):
    """Экран выбора раздела — страницами.

    Раньше сюда складывались ВСЕ разделы разом. Когда их стало много,
    Telegram отказывался принимать такую клавиатуру («reply markup is too
    long»), и мастер запуска серии просто не открывался. Режем на страницы
    и подрезаем слишком длинные названия.
    """
    cats = db.fetchall("SELECT * FROM test_categories ORDER BY sort_order, name")
    total = len(cats)
    pages = max(1, (total + CATS_PER_PAGE - 1) // CATS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = cats[page * CATS_PER_PAGE:(page + 1) * CATS_PER_PAGE]

    kb = InlineKeyboardBuilder()
    for c in chunk:
        name = (c["name"] or "")[:38]
        kb.button(text=f"{c.get('emoji') or '📚'} {name}",
                  callback_data=f"schedcat:{c['id']}")
    kb.adjust(1)
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"schedcatp:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"schedcatp:{page + 1}"))
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="🎲 Все разделы", callback_data="schedcat:all"))
    kb.row(InlineKeyboardButton(text="❌ Отмена", callback_data="sched:menu"))

    text = ("📂 <b>Шаг 1/5:</b> Выбери раздел для автозапуска:\n\n"
            f"Разделов: <b>{total}</b>")
    if pages > 1:
        text += f" · страница {page + 1} из {pages}"
    if not total:
        text += "\n\n<i>Разделов нет — можно выбрать «Все разделы».</i>"
    return text, kb.as_markup()


@router.callback_query(F.data == "sched:new", IsAdmin())
async def cb_sched_new(call: CallbackQuery, state: FSMContext):
    """Шаг 1: выбор раздела."""
    text, kb = _cats_screen(0)
    await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("schedcatp:"), IsAdmin())
async def cb_sched_cat_page(call: CallbackQuery):
    """Листание страниц разделов."""
    page = int(call.data.split(":")[1])
    text, kb = _cats_screen(page)
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "noop", IsAdmin())
async def cb_sched_noop(call: CallbackQuery):
    await call.answer()


@router.callback_query(F.data.startswith("schedcat:"), IsAdmin())
async def cb_sched_cat(call: CallbackQuery, state: FSMContext):
    arg = call.data.split(":")[1]
    cat_id = None if arg == "all" else int(arg)
    await state.update_data(sched_category_id=cat_id)
    await state.set_state(SchedStates.waiting_start_date)
    today = datetime.now(ALMATY).strftime("%Y-%m-%d")
    await call.message.answer(
        f"📅 <b>Шаг 2/5:</b> Введи дату НАЧАЛА в формате ГГГГ-ММ-ДД\n\n"
        f"Например сегодня: <code>{today}</code>\n\n/cancel — отмена",
        parse_mode="HTML")
    await call.answer()


def _valid_date(s: str) -> bool:
    try:
        datetime.strptime(s.strip(), "%Y-%m-%d")
        return True
    except ValueError:
        return False


@router.message(SchedStates.waiting_start_date, IsAdmin())
async def s_start_date(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    if not _valid_date(message.text):
        await message.answer("Неверный формат. Введи как 2026-07-05")
        return
    await state.update_data(sched_start=message.text.strip())
    await state.set_state(SchedStates.waiting_end_date)
    start = datetime.strptime(message.text.strip(), "%Y-%m-%d")
    max_end = (start + timedelta(days=31)).strftime("%Y-%m-%d")
    await message.answer(
        f"📅 <b>Шаг 3/5:</b> Введи дату ОКОНЧАНИЯ (ГГГГ-ММ-ДД)\n\n"
        f"Максимум 1 месяц, то есть до <code>{max_end}</code>\n\n/cancel",
        parse_mode="HTML")


@router.message(SchedStates.waiting_end_date, IsAdmin())
async def s_end_date(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    if not _valid_date(message.text):
        await message.answer("Неверный формат. Введи как 2026-08-05")
        return
    data = await state.get_data()
    start = datetime.strptime(data['sched_start'], "%Y-%m-%d")
    end = datetime.strptime(message.text.strip(), "%Y-%m-%d")
    if end < start:
        await message.answer("Дата окончания раньше начала. Введи снова.")
        return
    if (end - start).days > 31:
        await message.answer("Период больше 1 месяца. Максимум 31 день. Введи снова.")
        return
    await state.update_data(sched_end=message.text.strip())
    await state.set_state(SchedStates.waiting_time)
    await message.answer(
        "🕐 <b>Шаг 4/5:</b> Во сколько запускать тест каждый день?\n\n"
        "Введи время в формате ЧЧ:ММ по Астане (UTC+5)\n"
        "Например: <code>18:00</code>\n\n/cancel", parse_mode="HTML")


def _valid_time(s: str) -> bool:
    try:
        datetime.strptime(s.strip(), "%H:%M")
        return True
    except ValueError:
        return False


@router.message(SchedStates.waiting_time, IsAdmin())
async def s_time(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    if not _valid_time(message.text):
        await message.answer("Неверный формат. Введи как 18:00")
        return
    await state.update_data(sched_time=message.text.strip())
    await state.set_state(SchedStates.waiting_tests_per_day)
    await message.answer(
        "🔢 <b>Шаг 5/5:</b> Сколько тестов запускать в день?\n\n"
        "Введи число (например 1, 2, 3)\n\n/cancel", parse_mode="HTML")


@router.message(SchedStates.waiting_tests_per_day, IsAdmin())
async def s_tests_per_day(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    if not (message.text or '').strip().isdigit():
        await message.answer("Введи число, например 2")
        return
    n = int(message.text.strip())
    if n < 1 or n > 20:
        await message.answer("От 1 до 20 тестов в день. Введи снова.")
        return
    await state.update_data(sched_per_day=n)
    # Спрашиваем чат
    await state.set_state(SchedStates.waiting_chat_id)
    await message.answer(
        "💬 В какой чат/канал публиковать тесты?\n\n"
        "Перешли сюда любое сообщение из чата, ИЛИ введи ID чата "
        "(например -1001234567890)\n\n/cancel")


@router.message(SchedStates.waiting_chat_id, IsAdmin())
async def s_chat_id(message: Message, state: FSMContext, user: dict):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    chat_id = None
    if message.forward_from_chat:
        chat_id = message.forward_from_chat.id
    elif message.text and (message.text.strip().lstrip('-').isdigit()):
        chat_id = int(message.text.strip())
    if not chat_id:
        await message.answer("Не понял чат. Перешли сообщение из чата или введи ID числом.")
        return
    await state.update_data(sched_chat_id=chat_id)
    # Спрашиваем канал для анонса (опционально)
    await state.set_state(SchedStates.waiting_channel_id)
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить (без канала)", callback_data="sched:skipchannel")
    kb.adjust(1)
    await message.answer(
        "📢 <b>Канал для анонса</b> (необязательно)\n\n"
        "На канал будет отправляться анонс со ссылкой в бота перед стартом теста.\n\n"
        "Перешли сообщение из КАНАЛА или введи его ID.\n"
        "Или нажми «Пропустить» если канал не нужен.",
        reply_markup=kb.as_markup(), parse_mode="HTML")


async def _finish_schedule_creation(message_or_call, state, user, channel_id=None):
    """Финал создания расписания — спросить задержку."""
    await state.update_data(sched_channel_id=channel_id)
    await state.set_state(SchedStates.waiting_delay)
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="1 минута", callback_data="scheddelay:60")
    kb.button(text="2 минуты", callback_data="scheddelay:120")
    kb.button(text="5 минут", callback_data="scheddelay:300")
    kb.adjust(1)
    target = (message_or_call.message if hasattr(message_or_call, 'message')
              else message_or_call)
    await target.answer(
        "⏳ <b>Сколько ждать перед стартом теста?</b>\n\n"
        "После анонса бот подождёт это время (чтобы люди зашли в чат), "
        "потом запустит тест.",
        reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(F.data == "sched:skipchannel", IsAdmin())
async def cb_skip_channel(call: CallbackQuery, state: FSMContext, user: dict):
    await call.answer()
    await _finish_schedule_creation(call, state, user, channel_id=None)


@router.message(SchedStates.waiting_channel_id, IsAdmin())
async def s_channel_id(message: Message, state: FSMContext, user: dict):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    channel_id = None
    if message.forward_from_chat:
        channel_id = message.forward_from_chat.id
    elif message.text and (message.text.strip().lstrip('-').isdigit()):
        channel_id = int(message.text.strip())
    if not channel_id:
        await message.answer("Не понял канал. Перешли сообщение из канала или введи ID.")
        return
    await _finish_schedule_creation(message, state, user, channel_id=channel_id)


@router.callback_query(F.data.startswith("scheddelay:"), IsAdmin())
async def cb_set_delay(call: CallbackQuery, state: FSMContext, user: dict):
    delay = int(call.data.split(":")[1])
    await call.answer()
    data = await state.get_data()
    bot_un = ''
    try:
        me = await call.bot.get_me()
        bot_un = me.username or ''
    except Exception:
        pass

    sched_id = ass.create_schedule(
        chat_id=data['sched_chat_id'],
        category_id=data.get('sched_category_id'),
        start_date=data['sched_start'],
        end_date=data['sched_end'],
        daily_time=data['sched_time'],
        tests_per_day=data.get('sched_per_day', 1),
        allow_paid=0, allow_private=0,
        bot_username=bot_un,
        created_by=user.get('tg_id') or call.from_user.id,
        channel_id=data.get('sched_channel_id'),
        announce_delay=delay)
    await state.clear()
    sched = ass.get_schedule(sched_id)
    await call.message.answer(
        "✅ <b>Автозапуск настроен!</b>\n\n" + _fmt_schedule(sched),
        reply_markup=_menu_kb(True), parse_mode="HTML")


# ============ УПРАВЛЕНИЕ ============

@router.callback_query(F.data == "sched:stop", IsAdmin())
async def cb_sched_stop(call: CallbackQuery):
    active = db.fetchone(
        "SELECT id FROM auto_schedule WHERE status='active' ORDER BY id DESC LIMIT 1")
    if active:
        ass.stop_schedule(active['id'])
        await call.answer("Автотесты остановлены.", show_alert=True)
    await cb_sched_menu(call)


@router.callback_query(F.data == "sched:allowpaid", IsAdmin())
async def cb_allow_paid(call: CallbackQuery):
    active = db.fetchone(
        "SELECT * FROM auto_schedule WHERE status='active' ORDER BY id DESC LIMIT 1")
    if active:
        new_val = 0 if active['allow_paid'] else 1
        ass.update_schedule(active['id'], allow_paid=new_val)
        await call.answer(
            "Платные тесты " + ("разрешены ✅" if new_val else "запрещены ❌"),
            show_alert=True)
    await cb_sched_menu(call)


@router.callback_query(F.data == "sched:allowpriv", IsAdmin())
async def cb_allow_priv(call: CallbackQuery):
    active = db.fetchone(
        "SELECT * FROM auto_schedule WHERE status='active' ORDER BY id DESC LIMIT 1")
    if active:
        new_val = 0 if active['allow_private'] else 1
        ass.update_schedule(active['id'], allow_private=new_val)
        await call.answer(
            "Приватные тесты " + ("разрешены ✅" if new_val else "запрещены ❌"),
            show_alert=True)
    await cb_sched_menu(call)


@router.callback_query(F.data == "sched:extend", IsAdmin())
async def cb_extend(call: CallbackQuery, state: FSMContext):
    await state.set_state(SchedStates.waiting_extend_date)
    await call.message.answer(
        "➕ Введи новую дату окончания (ГГГГ-ММ-ДД, максимум месяц от начала):\n\n/cancel")
    await call.answer()


@router.message(SchedStates.waiting_extend_date, IsAdmin())
async def s_extend(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    if not _valid_date(message.text):
        await message.answer("Неверный формат. Введи как 2026-08-05")
        return
    active = db.fetchone(
        "SELECT * FROM auto_schedule WHERE status='active' ORDER BY id DESC LIMIT 1")
    if not active:
        await state.clear()
        await message.answer("Нет активного расписания.")
        return
    start = datetime.strptime(active['start_date'], "%Y-%m-%d")
    new_end = datetime.strptime(message.text.strip(), "%Y-%m-%d")
    if (new_end - start).days > 31:
        await message.answer("Больше месяца нельзя. Введи снова.")
        return
    ass.update_schedule(active['id'], end_date=message.text.strip())
    await state.clear()
    await message.answer(f"✅ Период продлён до {message.text.strip()}")


@router.callback_query(F.data == "sched:time", IsAdmin())
async def cb_change_time(call: CallbackQuery, state: FSMContext):
    await state.set_state(SchedStates.waiting_new_time)
    await call.message.answer("🕐 Введи новое время запуска (ЧЧ:ММ, Астана):\n\n/cancel")
    await call.answer()


@router.message(SchedStates.waiting_new_time, IsAdmin())
async def s_new_time(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    if not _valid_time(message.text):
        await message.answer("Неверный формат. Введи как 18:00")
        return
    active = db.fetchone(
        "SELECT id FROM auto_schedule WHERE status='active' ORDER BY id DESC LIMIT 1")
    if active:
        ass.update_schedule(active['id'], daily_time=message.text.strip(),
                            last_run_date=None)
    await state.clear()
    await message.answer(f"✅ Время запуска изменено на {message.text.strip()} (Астана)")


@router.callback_query(F.data == "sched:view", IsAdmin())
async def cb_view(call: CallbackQuery):
    active = db.fetchone(
        "SELECT * FROM auto_schedule WHERE status='active' ORDER BY id DESC LIMIT 1")
    if not active:
        await call.answer("Нет активного расписания.", show_alert=True)
        return
    sched = dict(active)
    # Расписание на ближайшие дни
    start = datetime.strptime(sched['start_date'], "%Y-%m-%d")
    end = datetime.strptime(sched['end_date'], "%Y-%m-%d")
    today = datetime.now(ALMATY)
    lines = [f"📅 <b>Расписание запусков</b>\n",
             f"Время каждый день: {sched['daily_time']} (Астана)\n"]
    d = max(start, today.replace(hour=0, minute=0, second=0, microsecond=0))
    count = 0
    while d <= end and count < 14:
        mark = "▶️" if d.strftime("%Y-%m-%d") == today.strftime("%Y-%m-%d") else "•"
        lines.append(f"{mark} {d.strftime('%d.%m.%Y')} в {sched['daily_time']}")
        d += timedelta(days=1)
        count += 1
    await call.message.answer("\n".join(lines), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "sched:stats", IsAdmin())
async def cb_stats(call: CallbackQuery):
    active = db.fetchone(
        "SELECT * FROM auto_schedule WHERE status='active' ORDER BY id DESC LIMIT 1")
    if not active:
        await call.answer("Нет активного расписания.", show_alert=True)
        return
    stats = ass.get_schedule_stats(active['id'])
    lines = ["📊 <b>Статистика тестов</b>\n"]
    runs = stats.get('runs', [])
    if runs:
        lines.append("<b>Запускавшиеся тесты:</b>")
        for r in runs:
            lines.append(f"• «{r['title']}» — запусков: {r['run_count']}, "
                         f"участников: {r['total_parts']}")
    never = stats.get('never_run', [])
    if never:
        lines.append(f"\n🆕 <b>Ещё не запускались ({len(never)}):</b>")
        for t in never[:10]:
            lines.append(f"• «{t['title']}»")
    await call.message.answer("\n".join(lines), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "sched:activity", IsAdmin())
async def cb_activity(call: CallbackQuery):
    active = db.fetchone(
        "SELECT * FROM auto_schedule WHERE status='active' ORDER BY id DESC LIMIT 1")
    if not active:
        await call.answer("Нет активного расписания.", show_alert=True)
        return
    stats = ass.get_schedule_stats(active['id'])
    total_parts = sum(r['total_parts'] for r in stats.get('runs', []))
    await call.message.answer(
        f"👥 <b>Активность участников</b>\n\n"
        f"Зашло в чат за период: <b>{stats.get('chat_activity', 0)}</b> чел.\n"
        f"Всего участий в тестах: <b>{total_parts}</b>\n"
        f"Запущено тестов: <b>{len(stats.get('runs', []))}</b>",
        parse_mode="HTML")
    await call.answer()
