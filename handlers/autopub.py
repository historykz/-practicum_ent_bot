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


@router.message(Command("stop"))
async def cmd_stop_series(message: Message, bot: Bot):
    """Команда /stop в чате — остановить серию тестов. Только для админов бота."""
    if not utils.is_admin(message.from_user.id):
        return
    # Чат где остановить: текущий (если группа) или из активной серии
    target_chat_id = None
    if message.chat.type in ("group", "supergroup"):
        target_chat_id = message.chat.id
    else:
        st = autopub_service.get_active_series()
        if st and st.get('chat_id'):
            target_chat_id = st['chat_id']
        else:
            chats = autopub_service.get_chats()
            target_chat_id = chats[0]['id'] if chats else None
    # Отменяем все pending
    cancelled = 0
    try:
        rows = autopub_service.list_pending()
        for r in rows:
            autopub_service.cancel_pending(r['id'])
            cancelled += 1
    except Exception as e:
        log.warning("stop cancel pending: %s", e)
    # Чистим активную серию (цепочку)
    try:
        autopub_service.clear_active_series()
    except Exception:
        pass
    # Останавливаем активный групповой квиз
    finalized = False
    try:
        from services import group_quiz_service
        if target_chat_id:
            gq = db.fetchone(
                "SELECT id FROM group_quizzes WHERE chat_id=? "
                "AND status IN ('lobby','running')",
                (int(target_chat_id),))
            if gq:
                await group_quiz_service.stop_quiz(bot, int(target_chat_id), 0)
                finalized = True
    except Exception as e:
        log.warning("stop active quiz: %s", e)
    # Открываем чат
    unlocked = False
    if target_chat_id:
        try:
            unlocked = await autopub_service._unlock_chat(bot, int(target_chat_id))
        except Exception as e:
            log.warning("stop unlock: %s", e)
    await message.reply(
        f"🛑 <b>Серия тестов остановлена</b>\n\n"
        f"• Отменено запланированных: <b>{cancelled}</b>\n"
        f"• Активный квиз: <b>{'завершён' if finalized else 'не было'}</b>\n"
        f"• Чат: <b>{'открыт' if unlocked else 'без изменений'}</b>",
        parse_mode="HTML")


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
    channels = autopub_service.get_channels()
    chats = autopub_service.get_chats()
    ch_str = ", ".join(c.get('title') or str(c['id']) for c in channels) if channels else "не заданы"
    chat_str = ", ".join(c.get('title') or str(c['id']) for c in chats) if chats else "не заданы"
    return (
        f"📅 <b>Авто-публикация тестов</b>\n\n"
        f"📢 Каналов: <b>{len(channels)}</b> ({ch_str})\n"
        f"💬 Чатов: <b>{len(chats)}</b> ({chat_str})\n\n"
        f"<i>Бот публикует тесты в чат (через лобби), "
        f"а на канале анонсирует со ссылкой.</i>"
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
    channels = autopub_service.get_channels()
    chats = autopub_service.get_chats()
    lines = ["⚙️ <b>Каналы и чаты</b>\n"]
    lines.append("📢 <b>Каналы для анонсов:</b>")
    if channels:
        for c in channels:
            lines.append(f"• {c.get('title') or c['id']}")
    else:
        lines.append("<i>нет</i>")
    lines.append("\n💬 <b>Чаты для тестов:</b>")
    if chats:
        for c in chats:
            inv = " 🔗" if c.get('invite') else " ⚠️без ссылки"
            lines.append(f"• {c.get('title') or c['id']}{inv}")
    else:
        lines.append("<i>нет</i>")
    text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить канал", callback_data="apub:add_channel")
    kb.button(text="➕ Добавить чат", callback_data="apub:add_chat")
    if channels:
        kb.button(text="🗑 Удалить канал", callback_data="apub:del_channel")
    if chats:
        kb.button(text="🗑 Удалить чат", callback_data="apub:del_chat")
        kb.button(text="🔗 Задать ссылку чату", callback_data="apub:set_chat_link")
    kb.button(text="↩️ Назад", callback_data="adm:autopub")
    kb.adjust(2, 2, 1, 1)
    try:
        await call.message.edit_text(text, reply_markup=kb.as_markup(),
                                       parse_mode="HTML")
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data == "apub:add_channel", IsAdmin())
async def cb_set_channel(call: CallbackQuery, state: FSMContext):
    await state.set_state(AutoPubStates.waiting_channel_id)
    await call.message.answer(
        "📢 <b>Добавить канал для анонсов</b>\n\n"
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
                try:
                    ch = await message.bot.get_chat(channel_id)
                    title = ch.title or str(channel_id)
                except Exception:
                    title = str(channel_id)
            except ValueError:
                await message.answer("Не похоже на ID.")
                return
    if channel_id is None:
        await message.answer("Не понял. Перешли пост, или дай @username/ID.")
        return
    autopub_service.add_channel(channel_id, title or '')
    await state.clear()
    await message.answer(
        f"✅ Канал добавлен: <b>{title or channel_id}</b>")


@router.callback_query(F.data == "apub:del_channel", IsAdmin())
async def cb_del_channel(call: CallbackQuery):
    channels = autopub_service.get_channels()
    kb = InlineKeyboardBuilder()
    for c in channels:
        kb.button(text=f"🗑 {c.get('title') or c['id']}",
                  callback_data=f"apub:delch:{c['id']}")
    kb.button(text="↩️ Назад", callback_data="apub:settings")
    kb.adjust(1)
    try:
        await call.message.edit_text("Какой канал удалить?",
                                       reply_markup=kb.as_markup())
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("apub:delch:"), IsAdmin())
async def cb_delch(call: CallbackQuery):
    channel_id = call.data.split(":", 2)[2]
    autopub_service.remove_channel(channel_id)
    await call.answer("🗑 Удалён")
    await cb_settings_menu(call)


@router.callback_query(F.data == "apub:add_chat", IsAdmin())
async def cb_set_chat(call: CallbackQuery, state: FSMContext):
    await state.set_state(AutoPubStates.waiting_chat_id)
    await call.message.answer(
        "💬 <b>Добавить чат для тестов</b>\n\n"
        "Перешли любое сообщение из чата СЮДА — я возьму ID.\n\n"
        "Или отправь <code>@username</code> или "
        "<code>-100xxxxxxxxxx</code>.\n\n"
        "Бот должен быть админом чата!\n\n"
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
                await message.answer("Не похоже на ID.")
                return
    if chat_id is None:
        await message.answer("Не понял. Перешли сообщение или дай @username/ID.")
        return
    autopub_service.add_chat(chat_id, chat_title or '')
    await state.clear()
    await message.answer(
        f"✅ Чат добавлен: <b>{chat_title or chat_id}</b>\n\n"
        f"⚠️ Не забудь задать ссылку-приглашение для этого чата "
        f"в Настройках («🔗 Задать ссылку чату»).")


@router.callback_query(F.data == "apub:del_chat", IsAdmin())
async def cb_del_chat(call: CallbackQuery):
    chats = autopub_service.get_chats()
    kb = InlineKeyboardBuilder()
    for c in chats:
        kb.button(text=f"🗑 {c.get('title') or c['id']}",
                  callback_data=f"apub:delcht:{c['id']}")
    kb.button(text="↩️ Назад", callback_data="apub:settings")
    kb.adjust(1)
    try:
        await call.message.edit_text("Какой чат удалить?",
                                       reply_markup=kb.as_markup())
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("apub:delcht:"), IsAdmin())
async def cb_delcht(call: CallbackQuery):
    chat_id = call.data.split(":", 2)[2]
    autopub_service.remove_chat(chat_id)
    await call.answer("🗑 Удалён")
    await cb_settings_menu(call)


@router.callback_query(F.data == "apub:set_chat_link", IsAdmin())
async def cb_set_chat_link_pick(call: CallbackQuery, state: FSMContext):
    chats = autopub_service.get_chats()
    kb = InlineKeyboardBuilder()
    for c in chats:
        kb.button(text=f"💬 {c.get('title') or c['id']}",
                  callback_data=f"apub:linkfor:{c['id']}")
    kb.button(text="↩️ Назад", callback_data="apub:settings")
    kb.adjust(1)
    try:
        await call.message.edit_text(
            "Для какого чата задать ссылку-приглашение?",
            reply_markup=kb.as_markup())
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("apub:linkfor:"), IsAdmin())
async def cb_linkfor(call: CallbackQuery, state: FSMContext):
    chat_id = call.data.split(":", 2)[2]
    await state.set_state(AutoPubStates.waiting_invite_link)
    await state.update_data(link_chat_id=chat_id)
    await call.message.answer(
        "🔗 <b>Ссылка-приглашение на чат</b>\n\n"
        "Отправь полную ссылку:\n"
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
    data = await state.get_data()
    chat_id = data.get('link_chat_id')
    if chat_id:
        autopub_service.set_chat_invite(chat_id, link)
    await state.clear()
    await message.answer(f"✅ Ссылка сохранена для чата: {link}")


# ===================== ЗАПУСК СЕРИИ =====================
# Сценарий: выбрал раздел → отметил тесты галочками →
# выбрал время → бот публикует по очереди

@router.callback_query(F.data == "apub:start", IsAdmin())
async def cb_start_series(call: CallbackQuery, state: FSMContext):
    """Шаг 1: показываем разделы для выбора тестов."""
    if not autopub_service.get_chats():
        await call.answer(
            "Сначала добавь чат для публикации в Настройках!",
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
    await state.update_data(apub_minutes=minutes)
    await _show_chat_picker(call, state)


async def _show_chat_picker(call: CallbackQuery, state: FSMContext):
    """Выбор чата для публикации тестов."""
    chats = autopub_service.get_chats()
    if not chats:
        await call.answer("Нет чатов. Добавь в Настройках.", show_alert=True)
        return
    if len(chats) == 1:
        # Один чат — выбираем автоматом, идём к каналу
        await state.update_data(apub_chat_id=chats[0]['id'])
        await _show_channel_picker(call, state)
        return
    kb = InlineKeyboardBuilder()
    for c in chats:
        warn = "" if c.get('invite') else " ⚠️"
        kb.button(text=f"💬 {c.get('title') or c['id']}{warn}",
                  callback_data=f"apub:chat:{c['id']}")
    kb.button(text="↩️ Назад", callback_data="apub:choose_mode")
    kb.adjust(1)
    try:
        await call.message.edit_text(
            "💬 <b>В каком чате проводить тесты?</b>\n\n"
            "(⚠️ = у чата не задана ссылка-приглашение)",
            reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("apub:chat:"), IsAdmin())
async def cb_pick_chat(call: CallbackQuery, state: FSMContext):
    chat_id = call.data.split(":", 2)[2]
    await state.update_data(apub_chat_id=chat_id)
    await _show_channel_picker(call, state)


async def _show_channel_picker(call: CallbackQuery, state: FSMContext):
    """Выбор канала для анонсов."""
    channels = autopub_service.get_channels()
    if not channels:
        # Нет каналов — публикуем без анонса
        await state.update_data(apub_channel_id=None)
        data = await state.get_data()
        await _enqueue_series(call, state, data.get('apub_minutes', 0))
        return
    if len(channels) == 1:
        await state.update_data(apub_channel_id=channels[0]['id'])
        data = await state.get_data()
        await _enqueue_series(call, state, data.get('apub_minutes', 0))
        return
    kb = InlineKeyboardBuilder()
    for c in channels:
        kb.button(text=f"📢 {c.get('title') or c['id']}",
                  callback_data=f"apub:chan:{c['id']}")
    kb.button(text="🚫 Без анонса на канал", callback_data="apub:chan:none")
    kb.adjust(1)
    try:
        await call.message.edit_text(
            "📢 <b>На каком канале анонсировать?</b>",
            reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("apub:chan:"), IsAdmin())
async def cb_pick_channel(call: CallbackQuery, state: FSMContext):
    arg = call.data.split(":", 2)[2]
    await state.update_data(apub_channel_id=(None if arg == "none" else arg))
    data = await state.get_data()
    await _enqueue_series(call, state, data.get('apub_minutes', 0))


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
    sel_chat_id = data.get('apub_chat_id')
    sel_channel_id = data.get('apub_channel_id')
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
        import time as _time
        series_id = f"s{int(_time.time())}"
        autopub_service.enqueue_test(
            mix_id, run_at, call.from_user.id,
            series_id=series_id, series_pos=0, series_total=1,
            series_test_ids=str(mix_id))
        autopub_service.save_series_state(
            series_id, str(mix_id), 1, call.from_user.id,
            chat_id=sel_chat_id, channel_id=sel_channel_id)
        # Если время в будущем — анонс СРАЗУ С ТЕМАМИ
        if minutes > 0 and sel_channel_id:
            when_str = _humanize_minutes(minutes)
            mix_test = db.fetchone("SELECT * FROM tests WHERE id=?", (mix_id,))
            try:
                await autopub_service.announce_batch_with_topics(
                    call.bot, [dict(mix_test)], when_str,
                    channel_id=sel_channel_id)
            except Exception:
                pass
        # minutes == 0 — worker сам отправит полный анонс «уже идёт»

        await state.clear()
        when_human = _humanize_minutes(minutes)
        summary = (
            f"✅ <b>МИКС из 10 вопросов создан!</b>\n\n"
            f"Использовано тестов: <b>{len(selected)}</b>\n"
            f"Запуск: <b>{when_human}</b>\n\n"
            + (f"📢 Короткий анонс отправлен. Когда время подойдёт — "
              f"бот отправит полный анонс с темой.\n" if minutes > 0 else
              f"🚀 Стартуем! Бот сейчас отправит анонс и откроет лобби в чате.\n")
            + f"\nНужно <b>2 человека</b> в чате, чтобы нажали «Пройти тест».")
    else:
        # По очереди — ЦЕПОЧКОЙ. В очередь ставим только ПЕРВЫЙ тест.
        # Остальные запускаются по факту завершения предыдущего.
        import random as _r
        import time as _time
        _r.shuffle(selected)
        base = datetime.utcnow() + timedelta(minutes=minutes)
        series_id = f"s{int(_time.time())}"
        series_test_ids = ",".join(str(t) for t in selected)

        first_tid = selected[0]
        autopub_service.enqueue_test(
            first_tid, base, call.from_user.id,
            series_id=series_id, series_pos=0,
            series_total=len(selected),
            series_test_ids=series_test_ids)
        # Сохраняем «состояние серии» для цепочки
        autopub_service.save_series_state(
            series_id, series_test_ids, len(selected),
            call.from_user.id,
            chat_id=sel_chat_id, channel_id=sel_channel_id)
        enqueued = len(selected)

        # Пре-анонс СРАЗУ С ТЕМАМИ если время в будущем
        if minutes > 0 and sel_channel_id:
            when_str = _humanize_minutes(minutes)
            tests_obj = []
            for tid in selected:
                t = db.fetchone("SELECT * FROM tests WHERE id=?", (tid,))
                if t:
                    tests_obj.append(dict(t))
            try:
                await autopub_service.announce_batch_with_topics(
                    call.bot, tests_obj, when_str, channel_id=sel_channel_id)
            except Exception as e:
                log.warning("pre-announce topics: %s", e)
        # При minutes == 0 worker сам отправит полный анонс с темами

        await state.clear()
        when_human = _humanize_minutes(minutes)
        summary = (
            f"✅ <b>Запланировано {enqueued} тестов!</b>\n\n"
            f"Первый — <b>{when_human}</b>\n"
            f"Следующие — сразу после результатов предыдущего (через 20 сек)\n\n"
            + (f"📢 Анонс с темами отправлен на канал." if minutes > 0
              else "🚀 Стартуем! Бот сейчас отправит анонс."))

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
    if not autopub_service.get_channels():
        await call.answer("Сначала добавь канал в Настройках!", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🇷🇺 Русский", callback_data="rnd_lang:ru")
    kb.button(text="🇰🇿 Қазақша", callback_data="rnd_lang:kz")
    kb.button(text="↩️ Назад", callback_data="adm:autopub")
    kb.adjust(2, 1)
    text = ("🎲 <b>10 вопросов на канал</b>\n\n"
            "Вопросы выйдут как Quiz Poll <b>без таймера</b>, "
            "без нумерации, с задержкой 10 сек.\n\n"
            "Шаг 1 — язык вопросов:")
    try:
        await call.message.edit_text(text, reply_markup=kb.as_markup(),
                                       parse_mode="HTML")
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("rnd_lang:"), IsAdmin())
async def cb_rnd_lang(call: CallbackQuery, state: FSMContext):
    lang = call.data.split(":")[1]
    await state.update_data(rnd_lang=lang, rnd_selected=[])
    await _rnd_show_categories(call.message, state)
    await call.answer()


async def _rnd_show_categories(msg_obj, state: FSMContext):
    data = await state.get_data()
    lang = data.get('rnd_lang', 'ru')
    selected = set(data.get('rnd_selected') or [])

    from collections import defaultdict
    by_cat = defaultdict(list)
    tests = db.fetchall(
        "SELECT id, title, category_id FROM tests "
        "WHERE status='active' AND COALESCE(is_paid,0)=0 "
        "AND COALESCE(is_private,0)=0 AND language=?", (lang,))
    for t in tests:
        by_cat[t.get('category_id')].append(t)

    if not tests:
        try:
            await msg_obj.edit_text("⚠️ Нет подходящих тестов на этом языке.")
        except Exception:
            pass
        return

    text = (f"🎲 <b>10 вопросов</b> · {'🇷🇺' if lang == 'ru' else '🇰🇿'}\n\n"
            f"✅ Выбрано тем: <b>{len(selected)}</b>\n\n"
            f"Шаг 2 — выбери раздел, внутри отметь темы галочками:")
    kb = InlineKeyboardBuilder()
    cats = db.fetchall("SELECT * FROM test_categories ORDER BY id")
    for c in cats:
        cat_tests = by_cat.get(c['id'], [])
        if not cat_tests:
            continue
        sel = sum(1 for t in cat_tests if t['id'] in selected)
        emoji = c.get('emoji') or '📚'
        kb.button(text=f"{emoji} {c['name']} ({sel}/{len(cat_tests)})",
                  callback_data=f"rnd_cat:{c['id']}")
    no_cat = by_cat.get(None, [])
    if no_cat:
        sel = sum(1 for t in no_cat if t['id'] in selected)
        kb.button(text=f"📭 Без раздела ({sel}/{len(no_cat)})",
                  callback_data="rnd_cat:none")
    if selected:
        kb.button(text=f"✅ Готово, выбрать канал ({len(selected)})",
                  callback_data="rnd_pick_channel")
    kb.button(text="↩️ Назад", callback_data="apub:random_canal")
    kb.adjust(1)
    try:
        await msg_obj.edit_text(text, reply_markup=kb.as_markup(),
                                  parse_mode="HTML")
    except Exception:
        try:
            await msg_obj.answer(text, reply_markup=kb.as_markup(),
                                   parse_mode="HTML")
        except Exception:
            pass


@router.callback_query(F.data.startswith("rnd_cat:"), IsAdmin())
async def cb_rnd_cat(call: CallbackQuery, state: FSMContext):
    arg = call.data.split(":")[1]
    data = await state.get_data()
    lang = data.get('rnd_lang', 'ru')
    selected = set(data.get('rnd_selected') or [])
    if arg == "none":
        tests = db.fetchall(
            "SELECT id, title FROM tests WHERE status='active' "
            "AND COALESCE(is_paid,0)=0 AND COALESCE(is_private,0)=0 "
            "AND language=? AND category_id IS NULL ORDER BY id DESC", (lang,))
        cat_title = "📭 Без раздела"
    else:
        cat_id = int(arg)
        cat = db.fetchone("SELECT * FROM test_categories WHERE id=?", (cat_id,))
        tests = db.fetchall(
            "SELECT id, title FROM tests WHERE status='active' "
            "AND COALESCE(is_paid,0)=0 AND COALESCE(is_private,0)=0 "
            "AND language=? AND category_id=? ORDER BY id DESC", (lang, cat_id))
        cat_title = f"{cat.get('emoji') or '📚'} {cat['name']}"
    if not tests:
        await call.answer("Нет тем в разделе.", show_alert=True)
        return
    in_sel = sum(1 for t in tests if t['id'] in selected)
    text = (f"<b>{cat_title}</b>\n\n"
            f"✅ Отмечено: <b>{in_sel}/{len(tests)}</b>\n\n"
            f"Тапай темы — отмечай галочками:")
    kb = InlineKeyboardBuilder()
    for t in tests:
        mark = "✅" if t['id'] in selected else "▫️"
        kb.button(text=f"{mark} {t['title'][:40]}",
                  callback_data=f"rnd_tog:{t['id']}:{arg}")
    if in_sel == len(tests):
        kb.button(text="◻️ Снять все", callback_data=f"rnd_all:{arg}:off")
    else:
        kb.button(text="☑️ Отметить все", callback_data=f"rnd_all:{arg}:on")
    kb.button(text="↩️ К разделам", callback_data="rnd_back_cats")
    kb.adjust(1)
    try:
        await call.message.edit_text(text, reply_markup=kb.as_markup(),
                                       parse_mode="HTML")
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("rnd_tog:"), IsAdmin())
async def cb_rnd_tog(call: CallbackQuery, state: FSMContext):
    try:
        _, tid, arg = call.data.split(":")
        tid = int(tid)
    except (ValueError, IndexError):
        await call.answer()
        return
    data = await state.get_data()
    selected = set(data.get('rnd_selected') or [])
    if tid in selected:
        selected.discard(tid)
    else:
        selected.add(tid)
    await state.update_data(rnd_selected=list(selected))
    fake = type('F', (), {'data': f"rnd_cat:{arg}", 'message': call.message,
                          'from_user': call.from_user, 'bot': call.bot,
                          'answer': call.answer})()
    await cb_rnd_cat(fake, state)


@router.callback_query(F.data.startswith("rnd_all:"), IsAdmin())
async def cb_rnd_all(call: CallbackQuery, state: FSMContext):
    try:
        _, arg, action = call.data.split(":")
    except ValueError:
        await call.answer()
        return
    data = await state.get_data()
    lang = data.get('rnd_lang', 'ru')
    selected = set(data.get('rnd_selected') or [])
    if arg == "none":
        tests = db.fetchall(
            "SELECT id FROM tests WHERE status='active' AND COALESCE(is_paid,0)=0 "
            "AND COALESCE(is_private,0)=0 AND language=? AND category_id IS NULL",
            (lang,))
    else:
        tests = db.fetchall(
            "SELECT id FROM tests WHERE status='active' AND COALESCE(is_paid,0)=0 "
            "AND COALESCE(is_private,0)=0 AND language=? AND category_id=?",
            (lang, int(arg)))
    if action == "on":
        for t in tests:
            selected.add(t['id'])
    else:
        for t in tests:
            selected.discard(t['id'])
    await state.update_data(rnd_selected=list(selected))
    fake = type('F', (), {'data': f"rnd_cat:{arg}", 'message': call.message,
                          'from_user': call.from_user, 'bot': call.bot,
                          'answer': call.answer})()
    await cb_rnd_cat(fake, state)


@router.callback_query(F.data == "rnd_back_cats", IsAdmin())
async def cb_rnd_back_cats(call: CallbackQuery, state: FSMContext):
    await _rnd_show_categories(call.message, state)
    await call.answer()


@router.callback_query(F.data == "rnd_pick_channel", IsAdmin())
async def cb_rnd_pick_channel(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = list(data.get('rnd_selected') or [])
    if not selected:
        await call.answer("Ничего не выбрано.", show_alert=True)
        return
    channels = autopub_service.get_channels()
    if not channels:
        await call.answer("Нет каналов. Добавь в Настройках.", show_alert=True)
        return
    # Если канал один — сразу публикуем
    if len(channels) == 1:
        await _rnd_do_publish(call, state, channels[0]['id'])
        return
    kb = InlineKeyboardBuilder()
    for c in channels:
        kb.button(text=f"📢 {c.get('title') or c['id']}",
                  callback_data=f"rnd_go:{c['id']}")
    kb.button(text="↩️ Назад", callback_data="rnd_back_cats")
    kb.adjust(1)
    try:
        await call.message.edit_text(
            "📤 <b>На какой канал отправить?</b>",
            reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("rnd_go:"), IsAdmin())
async def cb_rnd_go(call: CallbackQuery, state: FSMContext):
    channel_id = call.data.split(":", 1)[1]
    await _rnd_do_publish(call, state, channel_id)


async def _rnd_do_publish(call, state: FSMContext, channel_id):
    data = await state.get_data()
    selected = list(data.get('rnd_selected') or [])
    lang = data.get('rnd_lang', 'ru')
    await state.clear()
    if not selected:
        await call.answer("Список пуст.", show_alert=True)
        return
    chan = autopub_service.get_channel_by_id(channel_id)
    chan_name = (chan.get('title') if chan else channel_id)
    await call.answer("Публикую…", show_alert=False)
    bot_username = ''
    try:
        me = await call.bot.get_me()
        bot_username = me.username or ''
    except Exception:
        pass
    try:
        await call.message.edit_text(
            f"🎲 Публикую 10 вопросов на «{chan_name}»…\n\n"
            f"⏳ Между вопросами 10 сек, подожди ~2 минуты.")
    except Exception:
        pass
    sent, failed = await autopub_service.post_random_quiz_polls_to_channel(
        call.bot, count=10, language=lang, bot_username=bot_username,
        test_ids=selected, channel_id=channel_id)
    msg = (f"✅ <b>Готово!</b>\n\n"
            f"Канал: <b>{chan_name}</b>\n"
            f"Отправлено вопросов: <b>{sent}</b>\n"
            f"Ошибок: {failed}")
    try:
        await call.message.answer(msg, parse_mode="HTML",
                                    reply_markup=_main_menu_kb())
    except Exception:
        pass
