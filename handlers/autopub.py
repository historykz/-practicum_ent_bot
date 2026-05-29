"""
UI для авто-публикации тестов.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (CallbackQuery, Message, InlineKeyboardMarkup)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database as db
import utils
from filters import IsAdmin
from services import autopub_service

router = Router(name="autopub")
log = logging.getLogger(__name__)


def _humanize_minutes(minutes: int) -> str:
    """Превращает минуты в человекочитаемый текст."""
    if minutes <= 0:
        return "прямо сейчас"
    if minutes == 1:
        return "через 1 минуту"
    if minutes < 5:
        return f"через {minutes} минуты"
    if minutes < 60:
        return f"через {minutes} минут"
    hours = minutes // 60
    rem = minutes % 60
    if rem == 0:
        if hours == 1:
            return "через 1 час"
        if 2 <= hours <= 4:
            return f"через {hours} часа"
        return f"через {hours} часов"
    return f"через {hours} ч {rem} мин"


class AutoPubStates(StatesGroup):
    waiting_chat_id = State()
    waiting_channel_id = State()
    waiting_invite_link = State()
    waiting_custom_time = State()


def _settings_card_text() -> str:
    cfg = autopub_service.get_autopub_config()
    chat = cfg.get('chat_id') or '<i>не задан</i>'
    chat_title = cfg.get('chat_title') or ''
    channel = cfg.get('channel_id') or '<i>не задан</i>'
    link = cfg.get('invite_link') or '<i>не задана</i>'
    return (
        f"📅 <b>Авто-публикация тестов</b>\n\n"
        f"<b>Настройки:</b>\n"
        f"💬 Чат для тестов: <code>{chat}</code>"
        + (f" ({chat_title})" if chat_title else "") + "\n"
        f"📢 Канал для анонсов: <code>{channel}</code>\n"
        f"🔗 Пригласительная ссылка: {link}\n\n"
        f"<i>Бот будет публиковать тесты в чат, а на канале — "
        f"анонсировать со ссылкой.</i>"
    )


def _main_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Запустить серию тестов", callback_data="apub:start")
    kb.button(text="🎲 10 случайных вопросов на канал",
              callback_data="apub:random_canal")
    kb.button(text="📋 Очередь публикаций", callback_data="apub:queue")
    kb.button(text="⚙️ Настройки чата/канала", callback_data="apub:settings")
    kb.button(text="↩️ В админ-меню", callback_data="m:admin")
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(F.data == "adm:autopub", IsAdmin())
async def cb_autopub_menu(call: CallbackQuery):
    autopub_service.ensure_schedule_table()
    try:
        await call.message.edit_text(_settings_card_text(),
                                       reply_markup=_main_menu_kb(),
                                       parse_mode="HTML")
    except Exception:
        await call.message.answer(_settings_card_text(),
                                    reply_markup=_main_menu_kb(),
                                    parse_mode="HTML")
    await call.answer()


# ===================== НАСТРОЙКИ =====================

@router.callback_query(F.data == "apub:settings", IsAdmin())
async def cb_settings_menu(call: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Задать чат (ID или @username)", callback_data="apub:set_chat")
    kb.button(text="📢 Задать канал для анонсов", callback_data="apub:set_channel")
    kb.button(text="🔗 Задать пригласительную ссылку",
              callback_data="apub:set_link")
    kb.button(text="↩️ Назад", callback_data="adm:autopub")
    kb.adjust(1)
    try:
        await call.message.edit_text(
            _settings_card_text() +
            "\n\n<b>Выбери что хочешь изменить:</b>",
            reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data == "apub:set_chat", IsAdmin())
async def cb_set_chat(call: CallbackQuery, state: FSMContext):
    await state.set_state(AutoPubStates.waiting_chat_id)
    await call.message.answer(
        "💬 <b>Куда публиковать тесты?</b>\n\n"
        "Перешли любое сообщение из чата СЮДА (можно из канала-чата) "
        "— я возьму ID автоматически.\n\n"
        "Или отправь:\n"
        "• <code>@username</code> чата (если он публичный)\n"
        "• <code>-100xxxxxxxxxx</code> (ID супергруппы)\n\n"
        "Важно: бот должен быть админом в этом чате!\n\n"
        "/cancel для отмены.")
    await call.answer()


@router.message(AutoPubStates.waiting_chat_id, IsAdmin())
async def msg_set_chat(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return

    chat_id = None
    chat_title = None
    # Пересланное сообщение
    if message.forward_from_chat:
        chat_id = message.forward_from_chat.id
        chat_title = message.forward_from_chat.title or ''
    elif message.text:
        txt = message.text.strip()
        if txt.startswith('@'):
            try:
                ch = await message.bot.get_chat(txt)
                chat_id = ch.id
                chat_title = ch.title or txt
            except Exception as e:
                await message.answer(f"Не нашёл такой чат: {e}")
                return
        elif txt.startswith('-') or txt.isdigit():
            try:
                chat_id = int(txt)
                try:
                    ch = await message.bot.get_chat(chat_id)
                    chat_title = ch.title or str(chat_id)
                except Exception:
                    chat_title = str(chat_id)
            except ValueError:
                await message.answer("Не похоже на ID. Пришли число или @username или перешли сообщение.")
                return
    if chat_id is None:
        await message.answer("Не понял. Перешли сообщение из чата или дай @username/ID.")
        return

    autopub_service.set_autopub_config(chat_id=chat_id, chat_title=chat_title or '')
    await state.clear()
    await message.answer(
        f"✅ Чат сохранён: <code>{chat_id}</code>"
        + (f" ({chat_title})" if chat_title else ""))


@router.callback_query(F.data == "apub:set_channel", IsAdmin())
async def cb_set_channel(call: CallbackQuery, state: FSMContext):
    await state.set_state(AutoPubStates.waiting_channel_id)
    await call.message.answer(
        "📢 <b>На какой канал слать анонсы?</b>\n\n"
        "Перешли пост с канала, или отправь <code>@username</code> "
        "или ID <code>-100xxxxxxxxxx</code>.\n\n"
        "Бот должен быть админом канала!\n\n"
        "/cancel для отмены.")
    await call.answer()


@router.message(AutoPubStates.waiting_channel_id, IsAdmin())
async def msg_set_channel(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    channel_id = None
    title = None
    if message.forward_from_chat:
        channel_id = message.forward_from_chat.id
        title = message.forward_from_chat.title or ''
    elif message.text:
        txt = message.text.strip()
        if txt.startswith('@'):
            try:
                ch = await message.bot.get_chat(txt)
                channel_id = ch.id
                title = ch.title or txt
            except Exception as e:
                await message.answer(f"Не нашёл канал: {e}")
                return
        elif txt.startswith('-') or txt.isdigit():
            try:
                channel_id = int(txt)
            except ValueError:
                await message.answer("Не похоже на ID.")
                return
    if channel_id is None:
        await message.answer("Не понял. Перешли пост, или дай @username/ID.")
        return
    autopub_service.set_autopub_config(channel_id=channel_id)
    await state.clear()
    await message.answer(
        f"✅ Канал сохранён: <code>{channel_id}</code>"
        + (f" ({title})" if title else ""))


@router.callback_query(F.data == "apub:set_link", IsAdmin())
async def cb_set_link(call: CallbackQuery, state: FSMContext):
    await state.set_state(AutoPubStates.waiting_invite_link)
    await call.message.answer(
        "🔗 <b>Пригласительная ссылка на чат</b>\n\n"
        "Отправь полную ссылку, по которой юзеры зайдут в чат с тестами.\n\n"
        "Например:\n"
        "<code>https://t.me/+fo17_e1XrBAzZTEy</code>\n\n"
        "/cancel для отмены.")
    await call.answer()


@router.message(AutoPubStates.waiting_invite_link, IsAdmin())
async def msg_set_link(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    link = (message.text or "").strip()
    if not link.startswith(('http://', 'https://', 't.me/')):
        await message.answer("Не похоже на ссылку. Отправь полный URL.")
        return
    autopub_service.set_autopub_config(invite_link=link)
    await state.clear()
    await message.answer(f"✅ Ссылка сохранена: {link}")


# ===================== ЗАПУСК СЕРИИ =====================
# Сценарий: выбрал раздел → отметил тесты галочками →
# выбрал время → бот публикует по очереди

@router.callback_query(F.data == "apub:start", IsAdmin())
async def cb_start_series(call: CallbackQuery, state: FSMContext):
    """Шаг 1: показываем разделы для выбора тестов."""
    cfg = autopub_service.get_autopub_config()
    if not cfg.get('chat_id'):
        await call.answer(
            "Сначала задай чат для публикации в Настройках!",
            show_alert=True)
        return
    await state.update_data(apub_selected=[])
    await _show_categories(call.message, state)
    await call.answer()


async def _show_categories(msg_obj, state: FSMContext):
    data = await state.get_data()
    selected = set(data.get('apub_selected') or [])

    from collections import defaultdict
    by_cat = defaultdict(list)
    tests = db.fetchall(
        "SELECT id, title, category_id FROM tests "
        "WHERE status='active' AND COALESCE(is_paid,0)=0 "
        "AND COALESCE(is_private,0)=0")
    for tst in tests:
        by_cat[tst.get('category_id')].append(tst)

    if not tests:
        await msg_obj.answer("⚠️ Нет ни одного бесплатного теста.")
        return

    text = (f"🚀 <b>Запуск серии тестов</b>\n\n"
            f"✅ Выбрано: <b>{len(selected)}</b>\n\n"
            f"👇 Выбери раздел — внутри отметишь тесты галочками.")

    kb = InlineKeyboardBuilder()
    cats = db.fetchall("SELECT * FROM test_categories ORDER BY id")
    for c in cats:
        cat_tests = by_cat.get(c['id'], [])
        if not cat_tests:
            continue
        sel_cnt = sum(1 for t in cat_tests if t['id'] in selected)
        emoji = c.get('emoji') or '📚'
        kb.button(text=f"{emoji} {c['name']} ({sel_cnt}/{len(cat_tests)})",
                  callback_data=f"apubcat:{c['id']}")
    no_cat = by_cat.get(None, [])
    if no_cat:
        sel_cnt = sum(1 for t in no_cat if t['id'] in selected)
        kb.button(text=f"📭 Без раздела ({sel_cnt}/{len(no_cat)})",
                  callback_data="apubcat:none")
    if selected:
        kb.button(text=f"➡️ Далее ({len(selected)} тестов)",
                  callback_data="apub:choose_mode")
    kb.button(text="❌ Отмена", callback_data="adm:autopub")
    kb.adjust(1)
    try:
        await msg_obj.edit_text(text, reply_markup=kb.as_markup(),
                                  parse_mode="HTML")
    except Exception:
        await msg_obj.answer(text, reply_markup=kb.as_markup(),
                               parse_mode="HTML")


@router.callback_query(F.data.startswith("apubcat:"), IsAdmin())
async def cb_apub_category(call: CallbackQuery, state: FSMContext):
    arg = call.data.split(":")[1]
    data = await state.get_data()
    selected = set(data.get('apub_selected') or [])
    if arg == "none":
        tests = db.fetchall(
            "SELECT id, title FROM tests "
            "WHERE status='active' AND COALESCE(is_paid,0)=0 "
            "AND COALESCE(is_private,0)=0 AND category_id IS NULL "
            "ORDER BY id DESC")
        cat_title = "📭 Без раздела"
    else:
        try:
            cat_id = int(arg)
        except ValueError:
            await call.answer()
            return
        cat = db.fetchone("SELECT * FROM test_categories WHERE id=?", (cat_id,))
        tests = db.fetchall(
            "SELECT id, title FROM tests "
            "WHERE status='active' AND COALESCE(is_paid,0)=0 "
            "AND COALESCE(is_private,0)=0 AND category_id=? "
            "ORDER BY id DESC", (cat_id,))
        cat_title = f"{cat.get('emoji') or '📚'} {cat['name']}"

    if not tests:
        await call.answer("Нет тестов в разделе.", show_alert=True)
        return

    in_sel = sum(1 for t in tests if t['id'] in selected)
    text = (f"<b>{cat_title}</b>\n\n"
            f"✅ Отмечено: <b>{in_sel}/{len(tests)}</b>\n\n"
            f"Тапни тест чтобы отметить/снять галочку.")
    kb = InlineKeyboardBuilder()
    for t in tests:
        mark = "✅" if t['id'] in selected else "▫️"
        kb.button(text=f"{mark} {t['title'][:40]}",
                  callback_data=f"apubtog:{t['id']}:{arg}")
    if in_sel == len(tests):
        kb.button(text="◻️ Снять все в разделе",
                  callback_data=f"apuball:{arg}:off")
    else:
        kb.button(text="☑️ Отметить все в разделе",
                  callback_data=f"apuball:{arg}:on")
    kb.button(text="↩️ К разделам", callback_data="apub:back_cats")
    kb.adjust(1)
    try:
        await call.message.edit_text(text, reply_markup=kb.as_markup(),
                                       parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb.as_markup(),
                                    parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("apubtog:"), IsAdmin())
async def cb_apub_toggle(call: CallbackQuery, state: FSMContext):
    try:
        _, tid, cat_arg = call.data.split(":")
        tid = int(tid)
    except (ValueError, IndexError):
        await call.answer()
        return
    data = await state.get_data()
    selected = set(data.get('apub_selected') or [])
    if tid in selected:
        selected.discard(tid)
    else:
        selected.add(tid)
    await state.update_data(apub_selected=list(selected))
    fake = type('F', (), {
        'data': f"apubcat:{cat_arg}", 'message': call.message,
        'from_user': call.from_user, 'bot': call.bot, 'answer': call.answer})()
    await cb_apub_category(fake, state)


@router.callback_query(F.data.startswith("apuball:"), IsAdmin())
async def cb_apub_all(call: CallbackQuery, state: FSMContext):
    try:
        _, arg, action = call.data.split(":")
    except ValueError:
        await call.answer()
        return
    if arg == "none":
        tests = db.fetchall(
            "SELECT id FROM tests WHERE status='active' AND COALESCE(is_paid,0)=0 "
            "AND COALESCE(is_private,0)=0 AND category_id IS NULL")
    else:
        try:
            cat_id = int(arg)
        except ValueError:
            await call.answer()
            return
        tests = db.fetchall(
            "SELECT id FROM tests WHERE status='active' AND COALESCE(is_paid,0)=0 "
            "AND COALESCE(is_private,0)=0 AND category_id=?", (cat_id,))
    data = await state.get_data()
    selected = set(data.get('apub_selected') or [])
    if action == "on":
        for t in tests:
            selected.add(t['id'])
    else:
        for t in tests:
            selected.discard(t['id'])
    await state.update_data(apub_selected=list(selected))
    fake = type('F', (), {
        'data': f"apubcat:{arg}", 'message': call.message,
        'from_user': call.from_user, 'bot': call.bot, 'answer': call.answer})()
    await cb_apub_category(fake, state)


@router.callback_query(F.data == "apub:back_cats", IsAdmin())
async def cb_apub_back_cats(call: CallbackQuery, state: FSMContext):
    await _show_categories(call.message, state)
    await call.answer()


@router.callback_query(F.data == "apub:choose_mode", IsAdmin())
async def cb_choose_mode(call: CallbackQuery, state: FSMContext):
    """Шаг 2: выбор режима — микс или по очереди."""
    data = await state.get_data()
    selected = list(data.get('apub_selected') or [])
    if not selected:
        await call.answer("Ничего не выбрано.", show_alert=True)
        return
    if len(selected) > 4:
        await call.answer(
            "Для микса максимум 4 теста. Сними галочки лишних.",
            show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    if len(selected) >= 2:
        kb.button(text=f"🎲 МИКС: 10 вопросов из всех",
                  callback_data="apub:mode:mix")
    kb.button(text=f"📚 По очереди (целиком)",
              callback_data="apub:mode:full")
    kb.button(text="↩️ Назад", callback_data="apub:back_cats")
    kb.adjust(1)
    text = (
        f"⚙️ <b>Как публиковать?</b>\n\n"
        f"Выбрано тестов: <b>{len(selected)}</b>\n\n"
        f"🎲 <b>МИКС</b> — бот возьмёт <b>10 вопросов</b>, поделит "
        f"поровну из каждого теста, добавит рандом для добора. "
        f"В чате один большой квиз.\n\n"
        f"📚 <b>По очереди</b> — публикует тесты целиком, каждый отдельным лобби."
    )
    try:
        await call.message.edit_text(text, reply_markup=kb.as_markup(),
                                       parse_mode="HTML")
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("apub:mode:"), IsAdmin())
async def cb_set_mode(call: CallbackQuery, state: FSMContext):
    mode = call.data.split(":")[2]
    if mode not in ("mix", "full"):
        await call.answer()
        return
    await state.update_data(apub_mode=mode)
    await _show_template_picker(call, state)


async def _show_template_picker(call: CallbackQuery, state: FSMContext):
    """Шаг 3: выбор шаблона анонса."""
    from services import autopub_service
    kb = InlineKeyboardBuilder()
    for i, tpl in enumerate(autopub_service.ANNOUNCE_TEMPLATES):
        kb.button(text=tpl['name'], callback_data=f"apub:tpl:{i}")
    kb.button(text="↩️ Назад", callback_data="apub:choose_mode")
    kb.adjust(1)
    # Превью первого шаблона
    cfg = autopub_service.get_autopub_config()
    invite = cfg.get('invite_link') or 'https://t.me/...'
    preview = autopub_service.ANNOUNCE_TEMPLATES[0]['build'](
        "Казахское ханство", "сейчас", 10, invite)
    text = (f"📝 <b>Выбери шаблон анонса</b>\n\n"
            f"<i>Превью «{autopub_service.ANNOUNCE_TEMPLATES[0]['name']}»:</i>\n"
            f"━━━━━━━━━━\n{preview}\n━━━━━━━━━━")
    try:
        await call.message.edit_text(text, reply_markup=kb.as_markup(),
                                       parse_mode="HTML",
                                       disable_web_page_preview=True)
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("apub:tpl:"), IsAdmin())
async def cb_set_template(call: CallbackQuery, state: FSMContext):
    try:
        tpl_id = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    await state.update_data(apub_template=tpl_id)
    # Шаг 4: время
    await _show_time_picker(call, state)


async def _show_time_picker(call: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Прямо сейчас", callback_data="apub:when:0")
    kb.button(text="⏰ Через 5 мин", callback_data="apub:when:5")
    kb.button(text="⏰ Через 15 мин", callback_data="apub:when:15")
    kb.button(text="⏰ Через 30 мин", callback_data="apub:when:30")
    kb.button(text="⏰ Через 1 час", callback_data="apub:when:60")
    kb.button(text="⏰ Через 3 часа", callback_data="apub:when:180")
    kb.button(text="✏️ Ввести минуты вручную", callback_data="apub:when:manual")
    kb.button(text="↩️ Назад", callback_data="apub:choose_mode")
    kb.adjust(2, 2, 2, 1, 1)
    data = await state.get_data()
    mode = data.get('apub_mode', 'mix')
    mode_label = "🎲 Микс из 10 вопросов" if mode == "mix" else "📚 По очереди"
    text = (f"⏰ <b>Когда запустить?</b>\n\n"
            f"Режим: {mode_label}\n\n"
            f"<i>«Прямо сейчас» — бот сразу запустит лобби. "
            f"В чате появится карточка теста, нужно 2 человека "
            f"чтобы нажали «Пройти тест» — потом вопросы.</i>")
    try:
        await call.message.edit_text(text, reply_markup=kb.as_markup(),
                                       parse_mode="HTML")
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("apub:when:"), IsAdmin())
async def cb_when_chosen(call: CallbackQuery, state: FSMContext):
    arg = call.data.split(":")[2]
    if arg == "manual":
        await state.set_state(AutoPubStates.waiting_custom_time)
        await call.message.answer(
            "✏️ Введи через сколько минут запустить (от 0 до 10080):")
        await call.answer()
        return
    try:
        minutes = int(arg)
    except ValueError:
        await call.answer()
        return
    await _enqueue_series(call, state, minutes)


@router.message(AutoPubStates.waiting_custom_time, IsAdmin())
async def msg_custom_time(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    txt = (message.text or "").strip()
    if not txt.isdigit():
        await message.answer("Введи число (минуты).")
        return
    minutes = int(txt)
    if not 0 <= minutes <= 10080:
        await message.answer("От 0 до 10080 минут (7 дней).")
        return
    await _enqueue_series_msg(message, state, minutes)


async def _enqueue_series(call: CallbackQuery, state: FSMContext, minutes: int):
    data = await state.get_data()
    selected = list(data.get('apub_selected') or [])
    mode = data.get('apub_mode', 'mix')
    tpl_id = data.get('apub_template', 0)
    lang = 'ru'  # язык по умолчанию для микса
    if not selected:
        await call.answer("Список пуст.", show_alert=True)
        return

    if mode == 'mix' and len(selected) >= 2:
        # Создаём один большой микс
        mix_id = autopub_service.create_mixed_test(
            selected, call.from_user.id, total=10, language=lang)
        if not mix_id:
            await call.answer("Не смог собрать микс.", show_alert=True)
            return
        run_at = datetime.utcnow() + timedelta(minutes=minutes)
        autopub_service.enqueue_test(mix_id, run_at, call.from_user.id)
        mix_test = db.fetchone("SELECT * FROM tests WHERE id=?", (mix_id,))
        # ОДИН анонс на канале (если время в будущем)
        if minutes > 0:
            when_str = _humanize_minutes(minutes)
            try:
                await autopub_service.announce_test_on_channel(
                    call.bot, dict(mix_test), when_str, template_id=tpl_id)
            except Exception:
                pass
        await state.clear()
        summary = (
            f"✅ <b>МИКС из 10 вопросов создан!</b>\n\n"
            f"Использовано тестов: <b>{len(selected)}</b>\n"
            f"Запуск через: <b>{minutes} мин</b>\n\n"
            + (f"Анонс отправлен на канал.\n" if minutes > 0 else
                "Запускаю прямо сейчас — следи за чатом.\n")
            + f"В чате появится лобби — нужно <b>2 человека</b>, "
            f"чтобы нажали «Я готов».")
    else:
        # По очереди — целиком. ОДИН общий анонс со всеми темами.
        import random as _r
        _r.shuffle(selected)
        base = datetime.utcnow() + timedelta(minutes=minutes)
        GAP_MIN = 1
        enqueued = 0
        cursor = base
        titles = []  # для общего анонса
        first_run_at = base

        for tid in selected:
            run_at = cursor
            autopub_service.enqueue_test(tid, run_at, call.from_user.id)
            enqueued += 1
            test = db.fetchone("SELECT * FROM tests WHERE id=?", (tid,))
            if test:
                titles.append(test['title'])
            qcount = db.fetchone(
                "SELECT COUNT(*) AS c FROM questions WHERE test_id=?",
                (tid,))['c']
            tpq = (test.get('time_per_question') if test else 30) or 30
            test_duration_sec = qcount * tpq + 60  # +60 сек на лобби+шапку
            cursor = cursor + timedelta(seconds=test_duration_sec) \
                     + timedelta(minutes=GAP_MIN)

        # ОДИН анонс — со всеми темами разом
        tests_obj = []
        for tid in selected:
            t = db.fetchone("SELECT * FROM tests WHERE id=?", (tid,))
            if t:
                tests_obj.append(dict(t))

        if tests_obj:
            if minutes > 0:
                when_str = _humanize_minutes(minutes)
                try:
                    await autopub_service.announce_batch_on_channel(
                        call.bot, tests_obj, when_str, template_id=tpl_id)
                except Exception as e:
                    log.warning("batch announce: %s", e)
            else:
                try:
                    await autopub_service.announce_batch_now(
                        call.bot, tests_obj, template_id=tpl_id)
                except Exception as e:
                    log.warning("batch now: %s", e)

        await state.clear()
        summary = (
            f"✅ <b>Запланировано {enqueued} тестов!</b>\n\n"
            f"Первый — через {minutes} мин\n"
            f"Следующие — после окончания предыдущего + 1 мин паузы\n\n"
            + (f"Один общий анонс отправлен на канал." if minutes > 0
                else "Первый запускается сейчас."))

    try:
        await call.message.edit_text(summary, reply_markup=_main_menu_kb(),
                                       parse_mode="HTML")
    except Exception:
        await call.message.answer(summary, reply_markup=_main_menu_kb(),
                                    parse_mode="HTML")
    await call.answer("✅")


async def _noop_answer(*a, **k):
    pass


async def _enqueue_series_msg(message: Message, state: FSMContext, minutes: int):
    """Ручной ввод минут — переиспользуем логику через fake-call."""
    fake_call = type('F', (), {
        'data': '',
        'message': message,
        'from_user': message.from_user,
        'bot': message.bot,
        'answer': _noop_answer
    })()
    await _enqueue_series(fake_call, state, minutes)


# ===================== ОЧЕРЕДЬ =====================

@router.callback_query(F.data == "apub:queue", IsAdmin())
async def cb_show_queue(call: CallbackQuery):
    rows = autopub_service.list_pending()
    if not rows:
        text = "📋 <b>Очередь пуста.</b>"
        kb = InlineKeyboardBuilder()
        kb.button(text="↩️ Назад", callback_data="adm:autopub")
        kb.adjust(1)
        try:
            await call.message.edit_text(text, reply_markup=kb.as_markup(),
                                           parse_mode="HTML")
        except Exception:
            pass
        await call.answer()
        return
    lines = ["📋 <b>Очередь публикаций:</b>\n"]
    kb = InlineKeyboardBuilder()
    for r in rows[:20]:
        test = db.fetchone("SELECT title FROM tests WHERE id=?", (r['test_id'],))
        title = (test.get('title') if test else f"#{r['test_id']}")[:40]
        try:
            dt = datetime.fromisoformat(r['run_at']).strftime('%d.%m %H:%M')
        except Exception:
            dt = r['run_at']
        lines.append(f"• {dt} UTC — {utils.escape_html(title)}")
        kb.button(text=f"❌ Отменить: {title[:25]} ({dt})",
                  callback_data=f"apubcancel:{r['id']}")
    kb.button(text="↩️ Назад", callback_data="adm:autopub")
    kb.adjust(1)
    text = "\n".join(lines)
    try:
        await call.message.edit_text(text, reply_markup=kb.as_markup(),
                                       parse_mode="HTML")
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("apubcancel:"), IsAdmin())
async def cb_cancel_queue(call: CallbackQuery):
    try:
        qid = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    autopub_service.cancel_pending(qid)
    await call.answer("✅ Отменено")
    # Перерисуем
    await cb_show_queue(call)


# ===================== 10 СЛУЧАЙНЫХ ВОПРОСОВ НА КАНАЛ =====================

@router.callback_query(F.data == "apub:random_canal", IsAdmin())
async def cb_random_canal(call: CallbackQuery, state: FSMContext):
    cfg = autopub_service.get_autopub_config()
    if not cfg.get('channel_id'):
        await call.answer("Сначала задай канал в Настройках!", show_alert=True)
        return
    # Выбор раздела для выборки вопросов
    cats = db.fetchall("SELECT * FROM test_categories ORDER BY id")
    kb = InlineKeyboardBuilder()
    kb.button(text="🎲 Из ВСЕХ разделов", callback_data="apubrnd:all:ru")
    kb.button(text="🎲 Из ВСЕХ (қазақ)", callback_data="apubrnd:all:kz")
    for c in cats:
        emoji = c.get('emoji') or '📚'
        kb.button(text=f"{emoji} {c['name']} (RU)",
                  callback_data=f"apubrnd:{c['id']}:ru")
        kb.button(text=f"{emoji} {c['name']} (KZ)",
                  callback_data=f"apubrnd:{c['id']}:kz")
    kb.button(text="↩️ Назад", callback_data="adm:autopub")
    kb.adjust(2)
    text = ("🎲 <b>10 случайных вопросов на канал</b>\n\n"
            "Выбери из какого раздела взять вопросы.\n"
            "Бот пришлёт 10 случайных Quiz Poll на канал.\n\n"
            "<i>Берутся только из бесплатных и публичных тестов.</i>")
    try:
        await call.message.edit_text(text, reply_markup=kb.as_markup(),
                                       parse_mode="HTML")
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("apubrnd:"), IsAdmin())
async def cb_random_canal_do(call: CallbackQuery):
    try:
        _, arg, lang = call.data.split(":")
    except ValueError:
        await call.answer()
        return
    cat_id = None if arg == "all" else int(arg)
    await call.answer("Публикую…")
    sent, failed = await autopub_service.post_random_quiz_polls_to_channel(
        call.bot, count=10, category_id=cat_id, language=lang)
    msg = (f"✅ <b>Готово!</b>\n\n"
            f"Отправлено вопросов: <b>{sent}</b>\n"
            f"Ошибок: {failed}")
    try:
        await call.message.answer(msg, parse_mode="HTML")
    except Exception:
        pass
