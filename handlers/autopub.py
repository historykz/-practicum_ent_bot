"""
UI для авто-публикации тестов.
"""
import asyncio
import logging
import uuid as _uuid
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (CallbackQuery, Message, InlineKeyboardMarkup,
                           InlineKeyboardButton)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database as db
import utils
from filters import IsAdmin, IsOwner
from services import autopub_service

router = Router(name="autopub")
log = logging.getLogger(__name__)


@router.message(Command("stop"))
async def cmd_stop_series(message: Message, bot: Bot):
    """/stop в личке — отменить все запланированные и идущие серии (в группе
    /stop обрабатывает handlers/group_quiz: серия ЭТОГО чата + текущий тест).
    Только для админов бота."""
    if not utils.is_admin(message.from_user.id):
        return
    in_group = message.chat.type in ("group", "supergroup")
    active = autopub_service.list_series(autopub_service.ACTIVE_SERIES, 100)
    if in_group:
        targets = [x for x in active if str(x["test_chat_id"]) == str(message.chat.id)
                   and x["status"] in ("running", "waiting_next")]
    else:
        targets = active
    cancelled = [x for x in targets if autopub_service.cancel_series(x["id"], "остановлена командой /stop")]
    legacy = 0
    try:                                   # записи очереди прошлой версии
        for r in autopub_service.list_pending():
            autopub_service.cancel_pending(r['id'])
            legacy += 1
    except Exception as e:
        log.warning("stop cancel pending: %s", e)
    autopub_service.clear_active_series()
    finalized = unlocked = False
    # Только чаты отменённых серий и только то, что серия сама сделала:
    # чужой тест не останавливаем, незакрытый чат не «открываем»
    for x in cancelled:
        f, u = await _stop_series_side_effects(bot, x)
        finalized, unlocked = finalized or f, unlocked or u
    if in_group and not cancelled:
        from services import group_quiz_service
        try:
            finalized = (await group_quiz_service.stop_quiz(bot, message.chat.id, message.from_user.id))[0]
        except Exception as e:
            log.warning("stop active quiz: %s", e)
    await message.reply(
        f"🛑 <b>Серия тестов остановлена</b>\n\n"
        f"• Отменено серий: <b>{len(cancelled) + legacy}</b>\n"
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
    waiting_custom_time = State()       # мастер до v75: ввод минут
    waiting_seconds = State()
    waiting_minutes = State()
    waiting_time = State()              # HH:MM по Астане
    waiting_datetime = State()          # ДД.ММ.ГГГГ ЧЧ:ММ по Астане


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
    kb.button(text="⏰ Автозапуск по расписанию", callback_data="sched:menu")
    kb.button(text=f"🎲 {autopub_service.get_random_count()} случайных вопросов на канал",
              callback_data="apub:random_canal")
    kb.button(text="✋ Выбрать вопросы вручную", callback_data="apub:manual")
    kb.button(text=f"🔢 Вопросов за публикацию: {autopub_service.get_random_count()}",
              callback_data="apub:qty")
    kb.button(text="📋 Очередь публикаций (серии)", callback_data="apub:queue")
    kb.button(text="🧹 Очистить очередь / сбросить", callback_data="apub:clear")
    kb.button(text="⚙️ Настройки чата/канала", callback_data="apub:settings")
    kb.button(text="↩️ В админ-меню", callback_data="m:admin")
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(F.data == "adm:autopub", IsOwner())
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

# ===================== ЗАПУСК СЕРИИ ТЕСТОВ (мастер, v75) =====================
# Шаги: тесты → режим → порядок → шаблон анонса → способ запуска (+ значение)
# → канал анонса → чат тестов → анонс в боте → проверка прав и подтверждение.
# _next() всегда показывает ПЕРВЫЙ незаполненный шаг, поэтому ни один способ
# запуска (сразу, секунды, минуты, HH:MM, дата и время) не может пропустить
# выбор канала и чата: до кнопки «Запланировать» доходят только с обоими.
# Канал и чат выбираются всегда явно, даже если в списке он один.

def _markup(kb):
    return kb.as_markup() if hasattr(kb, "as_markup") else kb


async def _screen(obj, text: str, kb=None):
    """Показать экран: на кнопку — правим сообщение, на введённый текст — новое."""
    markup = _markup(kb) if kb is not None else None
    if isinstance(obj, CallbackQuery):
        try:
            await obj.message.edit_text(text, reply_markup=markup, parse_mode="HTML",
                                        disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e):
                try:
                    await obj.message.answer(text, reply_markup=markup, parse_mode="HTML",
                                             disable_web_page_preview=True)
                except Exception as e2:
                    log.warning("серия: экран: %s", e2)
        try:
            await obj.answer()
        except Exception:
            pass
    else:
        await obj.answer(text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)


def _titles(ids) -> list:
    out = []
    for tid in ids:
        t = db.fetchone("SELECT title FROM tests WHERE id=?", (tid,))
        out.append(t["title"] if t else f"#{tid}")
    return out


def _tests_of(d: dict) -> list:
    return list(d.get("sr_order") or []) if d.get("sr_mode") == "full" else list(d.get("apub_selected") or [])


def _back_btn(kb, step: str):
    kb.button(text="↩️ Назад", callback_data=f"apub:back:{step}")


async def _next(obj, state: FSMContext):
    d = await state.get_data()
    if not d.get("apub_selected"):
        return await _show_categories(obj, state)
    if d.get("sr_mode") not in ("full", "mix"):
        return await _show_mode(obj, state)
    if d.get("sr_mode") == "full" and not d.get("sr_order_ok"):
        return await _show_order(obj, state)
    if d.get("sr_template") is None:
        return await _show_template_picker(obj, state)
    if not d.get("sr_launch_mode"):
        return await _show_launch(obj, state)
    if not d.get("sr_channel"):
        return await _show_channel_picker(obj, state)
    if not d.get("sr_chat"):
        return await _show_chat_picker(obj, state)
    if d.get("sr_bot_announce") is None:
        return await _ask_bot_announce(obj, state)
    return await _show_confirm(obj, state)


@router.callback_query(F.data == "apub:start", IsAdmin())
async def cb_start_series(call: CallbackQuery, state: FSMContext):
    """Шаг 1: разделы для выбора тестов. Настройки прошлой серии не подхватываются."""
    if not autopub_service.get_chats():
        await call.answer("Сначала добавь чат для тестов в «⚙️ Настройки чата/канала».", show_alert=True)
        return
    await state.clear()
    await state.update_data(apub_selected=[], sr_op=_uuid.uuid4().hex)
    await _show_categories(call, state)


async def _show_categories(obj, state: FSMContext):
    d = await state.get_data()
    selected = list(d.get('apub_selected') or [])
    from collections import defaultdict
    by_cat = defaultdict(list)
    tests = db.fetchall("SELECT id, title, category_id, is_paid, is_private FROM tests WHERE status='active'")
    for tst in tests:
        by_cat[tst.get('category_id')].append(tst)
    if not tests:
        return await _screen(obj, "⚠️ Нет ни одного теста.", _main_menu_kb())
    lines = ["🚀 <b>Запуск серии тестов</b>", "", f"✅ Выбрано: <b>{len(selected)}</b>"]
    for i, t in enumerate(_titles(selected[:15]), 1):
        lines.append(f"{i}. {utils.escape_html(t)}")
    if len(selected) > 15:
        lines.append(f"… и ещё {len(selected) - 15}")
    lines += ["", "💎 платный · 🔐 приватный · 🆓 бесплатный",
              "👇 Выбери раздел — внутри отметишь тесты галочками. Порядок настроишь дальше."]
    sel = set(selected)
    kb = InlineKeyboardBuilder()
    for c in db.fetchall("SELECT * FROM test_categories ORDER BY id"):
        cat_tests = by_cat.get(c['id'], [])
        if cat_tests:
            kb.button(text=f"{c.get('emoji') or '📚'} {c['name']} ({sum(1 for t in cat_tests if t['id'] in sel)}/{len(cat_tests)})",
                      callback_data=f"apubcat:{c['id']}")
    no_cat = by_cat.get(None, [])
    if no_cat:
        kb.button(text=f"📭 Без раздела ({sum(1 for t in no_cat if t['id'] in sel)}/{len(no_cat)})",
                  callback_data="apubcat:none")
    if selected:
        kb.button(text=f"➡️ Далее ({len(selected)} тестов)", callback_data="apub:choose_mode")
    kb.button(text="❌ Отмена", callback_data="apub:cancelwiz")
    kb.adjust(1)
    await _screen(obj, "\n".join(lines), kb)


def _cat_tests(arg: str):
    if arg == "none":
        return "📭 Без раздела", db.fetchall("SELECT id, title, is_paid, is_private FROM tests "
                                             "WHERE status='active' AND category_id IS NULL ORDER BY id DESC")
    cat = db.fetchone("SELECT * FROM test_categories WHERE id=?", (int(arg),))
    tests = db.fetchall("SELECT id, title, is_paid, is_private FROM tests WHERE status='active' AND category_id=? "
                        "ORDER BY id DESC", (int(arg),))
    return (f"{(cat or {}).get('emoji') or '📚'} {(cat or {}).get('name') or ''}", tests)


async def _show_category(obj, state: FSMContext, arg: str):
    try:
        cat_title, tests = _cat_tests(arg)
    except (ValueError, TypeError):
        return await _screen(obj, "Раздел не найден.", None)
    if not tests:
        if isinstance(obj, CallbackQuery):
            await obj.answer("Нет тестов в разделе.", show_alert=True)
        return
    selected = list((await state.get_data()).get('apub_selected') or [])
    in_sel = sum(1 for t in tests if t['id'] in selected)
    text = (f"<b>{utils.escape_html(cat_title)}</b>\n\n✅ Отмечено: <b>{in_sel}/{len(tests)}</b>\n\n"
            f"Тапни тест, чтобы отметить или снять галочку. Номер — место в серии.")
    kb = InlineKeyboardBuilder()
    for t in tests:
        mark = f"✅{selected.index(t['id']) + 1}" if t['id'] in selected else "▫️"
        tag = "💎" if t.get('is_paid') else ("🔐" if t.get('is_private') else "")
        kb.button(text=f"{mark} {tag}{t['title'][:38]}", callback_data=f"apubtog:{t['id']}:{arg}")
    kb.button(text="◻️ Снять все в разделе" if in_sel == len(tests) else "☑️ Отметить все в разделе",
              callback_data=f"apuball:{arg}:{'off' if in_sel == len(tests) else 'on'}")
    kb.button(text="↩️ К разделам", callback_data="apub:back_cats")
    kb.adjust(1)
    await _screen(obj, text, kb)


@router.callback_query(F.data.startswith("apubcat:"), IsAdmin())
async def cb_apub_category(call: CallbackQuery, state: FSMContext):
    await _show_category(call, state, call.data.split(":")[1])


@router.callback_query(F.data.startswith("apubtog:"), IsAdmin())
async def cb_apub_toggle(call: CallbackQuery, state: FSMContext):
    try:
        _, tid, cat_arg = call.data.split(":")
        tid = int(tid)
    except (ValueError, IndexError):
        await call.answer()
        return
    selected = list((await state.get_data()).get('apub_selected') or [])
    if tid in selected:
        selected.remove(tid)
    else:
        selected.append(tid)                  # порядок клика — порядок в серии по умолчанию
    await state.update_data(apub_selected=selected, sr_order_ok=False)
    await _show_category(call, state, cat_arg)


@router.callback_query(F.data.startswith("apuball:"), IsAdmin())
async def cb_apub_all(call: CallbackQuery, state: FSMContext):
    try:
        _, arg, action = call.data.split(":")
        _title, tests = _cat_tests(arg)
    except (ValueError, TypeError):
        await call.answer()
        return
    selected = list((await state.get_data()).get('apub_selected') or [])
    ids = [t['id'] for t in tests]
    if action == "on":
        selected += [t for t in ids if t not in selected]
    else:
        selected = [t for t in selected if t not in ids]
    await state.update_data(apub_selected=selected, sr_order_ok=False)
    await _show_category(call, state, arg)


@router.callback_query(F.data == "apub:back_cats", IsAdmin())
async def cb_apub_back_cats(call: CallbackQuery, state: FSMContext):
    await _show_categories(call, state)


@router.callback_query(F.data == "apub:choose_mode", IsAdmin())
async def cb_choose_mode(call: CallbackQuery, state: FSMContext):
    sel = list((await state.get_data()).get('apub_selected') or [])
    if not sel:
        await call.answer("Ничего не выбрано.", show_alert=True)
        return
    if len(sel) > autopub_service.MAX_SERIES_TESTS:
        await call.answer(f"В серии не больше {autopub_service.MAX_SERIES_TESTS} тестов — снимите лишние.",
                          show_alert=True)
        return
    await state.update_data(sr_mode=None, sr_order_ok=False)
    await _show_mode(call, state)


async def _show_mode(obj, state: FSMContext):
    sel = list((await state.get_data()).get('apub_selected') or [])
    kb = InlineKeyboardBuilder()
    if 2 <= len(sel) <= 4:
        kb.button(text="🎲 МИКС: 10 вопросов из всех", callback_data="apub:mode:mix")
    kb.button(text="📚 По очереди (целиком, в вашем порядке)", callback_data="apub:mode:full")
    kb.button(text="↩️ Назад", callback_data="apub:back_cats")
    kb.adjust(1)
    text = (f"⚙️ <b>Как публиковать?</b>\n\nВыбрано тестов: <b>{len(sel)}</b>\n\n"
            f"📚 <b>По очереди</b> — каждый тест целиком, отдельным лобби, строго в заданном порядке; "
            f"следующий — через 20–30 сек после окончания предыдущего.\n\n"
            f"🎲 <b>МИКС</b> — один квиз из 10 вопросов, поровну из каждого теста"
            + ("." if 2 <= len(sel) <= 4 else " (только для 2–4 тестов)."))
    await _screen(obj, text, kb)


@router.callback_query(F.data.startswith("apub:mode:"), IsAdmin())
async def cb_set_mode(call: CallbackQuery, state: FSMContext):
    mode = call.data.split(":")[2]
    d = await state.get_data()
    sel = list(d.get('apub_selected') or [])
    if mode not in ("mix", "full") or not sel:
        await call.answer()
        return
    if mode == "mix" and not 2 <= len(sel) <= 4:
        await call.answer("Микс — для 2–4 тестов.", show_alert=True)
        return
    prev = list(d.get("sr_order") or [])
    order = [t for t in prev if t in sel] + [t for t in sel if t not in prev]
    await state.update_data(sr_mode=mode, sr_order=order, sr_order_ok=(mode == "mix"))
    await _next(call, state)


async def _show_order(obj, state: FSMContext):
    d = await state.get_data()
    order = list(d.get("sr_order") or d.get("apub_selected") or [])
    titles = _titles(order)
    lines = ["🔢 <b>Порядок запуска</b>", "",
             "Тесты пойдут строго в этом порядке — не по номеру и не по алфавиту. Меняйте стрелками.", ""]
    lines += [f"{i}. {utils.escape_html(t)}" for i, t in enumerate(titles, 1)]
    kb = InlineKeyboardBuilder()
    for i, t in enumerate(titles):
        kb.row(InlineKeyboardButton(text=f"{i + 1}. {t[:28]}", callback_data="apub:ord:noop"),
               InlineKeyboardButton(text="⬆️", callback_data=f"apub:ord:up:{i}"),
               InlineKeyboardButton(text="⬇️", callback_data=f"apub:ord:dn:{i}"))
    kb.row(InlineKeyboardButton(text="✅ Порядок верный", callback_data="apub:ord:ok"))
    kb.row(InlineKeyboardButton(text="↩️ Назад", callback_data="apub:back:mode"))
    await _screen(obj, "\n".join(lines), kb)


@router.callback_query(F.data.startswith("apub:ord:"), IsAdmin())
async def cb_order(call: CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    d = await state.get_data()
    order = list(d.get("sr_order") or d.get("apub_selected") or [])
    if parts[2] == "noop":
        await call.answer()
        return
    if parts[2] == "ok":
        await state.update_data(sr_order=order, sr_order_ok=True)
        return await _next(call, state)
    try:
        i = int(parts[3])
    except (ValueError, IndexError):
        await call.answer()
        return
    j = i - 1 if parts[2] == "up" else i + 1
    if 0 <= i < len(order) and 0 <= j < len(order):
        order[i], order[j] = order[j], order[i]
    await state.update_data(sr_order=order, sr_order_ok=False)
    await _show_order(call, state)


async def _show_template_picker(obj, state: FSMContext):
    """Шаблон анонса в канале."""
    d = await state.get_data()
    kb = InlineKeyboardBuilder()
    for i, tpl in enumerate(autopub_service.ANNOUNCE_TEMPLATES):
        kb.button(text=tpl['name'], callback_data=f"apub:tpl:{i}")
    _back_btn(kb, "order" if d.get("sr_mode") == "full" else "mode")
    kb.adjust(1)
    preview = autopub_service.ANNOUNCE_TEMPLATES[0]['build']("Казахское ханство", "сегодня в 20:00", 10,
                                                             "https://t.me/...")
    await _screen(obj, f"📝 <b>Выбери шаблон анонса</b>\n\n<i>Превью «{autopub_service.ANNOUNCE_TEMPLATES[0]['name']}»:</i>\n"
                       f"━━━━━━━━━━\n{preview}\n━━━━━━━━━━", kb)


@router.callback_query(F.data.startswith("apub:tpl:"), IsAdmin())
async def cb_set_template(call: CallbackQuery, state: FSMContext):
    try:
        tpl_id = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    await state.update_data(sr_template=tpl_id)
    await _next(call, state)


async def _show_launch(obj, state: FSMContext):
    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Сразу после подтверждения", callback_data="apub:lm:now")
    kb.button(text="⏱ Через N секунд (для проверки)", callback_data="apub:lm:seconds")
    kb.button(text="⏰ Через N минут", callback_data="apub:lm:minutes")
    kb.button(text="🕗 В точное время (HH:MM)", callback_data="apub:lm:exact_time")
    kb.button(text="📅 Дата и время", callback_data="apub:lm:datetime")
    _back_btn(kb, "tpl")
    kb.adjust(1)
    await _screen(obj, "⏰ <b>Выберите способ запуска</b>\n\nПосле этого бот обязательно спросит, в какой канал "
                       "отправить анонс и в каком чате запускать тесты, и покажет всё на проверку.\n\n"
                       "Время — по Казахстану (Астана, UTC+5).", kb)


@router.callback_query(F.data.startswith("apub:lm:"), IsAdmin())
async def cb_launch_mode(call: CallbackQuery, state: FSMContext):
    mode = call.data.split(":")[2]
    kb = InlineKeyboardBuilder()
    if mode == "now":
        return await _set_launch(call, state, "now", None)
    if mode == "seconds":
        for n in (30, 60, 120):
            kb.button(text=f"⏱ {n} сек", callback_data=f"apub:sec:{n}")
        kb.button(text="✏️ Ввести число секунд", callback_data="apub:sec:manual")
        _back_btn(kb, "launch")
        kb.adjust(3, 1, 1)
        return await _screen(call, "⏱ <b>Через сколько секунд запустить?</b>\n\nРежим для проверки. Отсчёт — от кнопки "
                                   "«Запланировать» на последнем шаге.", kb)
    if mode == "minutes":
        for n in (5, 15, 30, 60, 180):
            kb.button(text=f"⏰ {n} мин", callback_data=f"apub:min:{n}")
        kb.button(text="✏️ Ввести число минут", callback_data="apub:min:manual")
        _back_btn(kb, "launch")
        kb.adjust(3, 2, 1, 1)
        return await _screen(call, "⏰ <b>Через сколько минут запустить?</b>\n\nОтсчёт — от кнопки «Запланировать».", kb)
    if mode == "exact_time":
        await state.set_state(AutoPubStates.waiting_time)
        _back_btn(kb, "launch")
        return await _screen(call, "🕗 <b>Введите время запуска</b> в формате <b>HH:MM</b>, например <code>20:00</code>.\n\n"
                                   "Время Астаны (UTC+5). Если сегодня это время уже прошло — серия встанет на завтра "
                                   "(дата будет видна на проверке).", kb)
    if mode == "datetime":
        await state.set_state(AutoPubStates.waiting_datetime)
        _back_btn(kb, "launch")
        return await _screen(call, "📅 <b>Введите дату и время запуска</b>, например <code>15.09.2026 20:00</code>.\n\n"
                                   "Время Астаны (UTC+5).", kb)
    await call.answer()


async def _set_launch(obj, state: FSMContext, mode: str, value):
    _at, _delay, err = autopub_service.resolve_launch(mode, value)
    if err:
        if isinstance(obj, CallbackQuery):
            await obj.answer(err, show_alert=True)
        else:
            await obj.answer(f"⚠️ {err}")
        return
    await state.set_state(None)
    await state.update_data(sr_launch_mode=mode, sr_launch_value=value)
    await _next(obj, state)


@router.callback_query(F.data.startswith("apub:sec:"), IsAdmin())
async def cb_seconds(call: CallbackQuery, state: FSMContext):
    arg = call.data.split(":")[2]
    if arg == "manual":
        await state.set_state(AutoPubStates.waiting_seconds)
        kb = InlineKeyboardBuilder()
        _back_btn(kb, "launch")
        lo, hi = autopub_service.SECONDS_RANGE
        return await _screen(call, f"✏️ Введите, через сколько секунд запустить ({lo}–{hi}):", kb)
    await _set_launch(call, state, "seconds", int(arg))


@router.callback_query(F.data.startswith("apub:min:"), IsAdmin())
async def cb_minutes(call: CallbackQuery, state: FSMContext):
    arg = call.data.split(":")[2]
    if arg == "manual":
        await state.set_state(AutoPubStates.waiting_minutes)
        kb = InlineKeyboardBuilder()
        _back_btn(kb, "launch")
        lo, hi = autopub_service.MINUTES_RANGE
        return await _screen(call, f"✏️ Введите, через сколько минут запустить ({lo}–{hi}):", kb)
    await _set_launch(call, state, "minutes", int(arg))


def _is_cancel(message: Message) -> bool:
    return (message.text or "").strip().lower() in ("/cancel", "отмена")


@router.message(AutoPubStates.waiting_seconds, IsAdmin())
async def msg_seconds(message: Message, state: FSMContext):
    if _is_cancel(message):
        return await _cancel_wizard(message, state)
    txt = (message.text or "").strip()
    if not txt.isdigit():
        await message.answer("Введите число секунд, например 60.")
        return
    await _set_launch(message, state, "seconds", int(txt))


@router.message(AutoPubStates.waiting_minutes, IsAdmin())
@router.message(AutoPubStates.waiting_custom_time, IsAdmin())      # ввод минут в мастере до v75
async def msg_minutes(message: Message, state: FSMContext):
    if _is_cancel(message):
        return await _cancel_wizard(message, state)
    txt = (message.text or "").strip()
    if not txt.isdigit():
        await message.answer("Введите число минут, например 15.")
        return
    if int(txt) == 0:
        return await _set_launch(message, state, "now", None)
    await _set_launch(message, state, "minutes", int(txt))


@router.message(AutoPubStates.waiting_time, IsAdmin())
async def msg_time(message: Message, state: FSMContext):
    if _is_cancel(message):
        return await _cancel_wizard(message, state)
    value = autopub_service.exact_time_value(message.text)
    if not value:
        await message.answer("Не понял время. Введите в формате HH:MM, например 20:00.")
        return
    await _set_launch(message, state, "exact_time", value)


@router.message(AutoPubStates.waiting_datetime, IsAdmin())
async def msg_datetime(message: Message, state: FSMContext):
    if _is_cancel(message):
        return await _cancel_wizard(message, state)
    dt = autopub_service.parse_local_datetime(message.text)
    if not dt:
        await message.answer("Не понял дату и время. Формат: 15.09.2026 20:00")
        return
    await _set_launch(message, state, "datetime", dt.strftime("%Y-%m-%dT%H:%M:%S"))


@router.callback_query(F.data.startswith("apub:when:"), IsAdmin())
async def cb_when_legacy(call: CallbackQuery, state: FSMContext):
    """Кнопки времени из меню прошлой версии: способ запуска выбирается заново."""
    await state.update_data(sr_launch_mode=None, sr_launch_value=None)
    await _next(call, state)


async def _show_channel_picker(obj, state: FSMContext):
    """Канал для анонса — выбирается ВСЕГДА явно, даже если он один."""
    chans = autopub_service.get_channels()
    text = ("📢 <b>Выберите канал для анонсирования серии</b>\n\n"
            "Сюда бот опубликует анонс. Сами тесты в канал НЕ отправляются — для них дальше выберете чат.")
    if not chans:
        text += "\n\n<i>Каналов пока нет — добавить можно в ⚙️ Настройках. Можно провести серию без анонса.</i>"
    kb = InlineKeyboardBuilder()
    for c in chans:
        kb.button(text=f"📢 {c.get('title') or c['id']}", callback_data=f"apub:chan:{c['id']}")
    kb.button(text="🚫 Без анонса на канал", callback_data="apub:chan:none")
    _back_btn(kb, "launch")
    kb.adjust(1)
    await _screen(obj, text, kb)


@router.callback_query(F.data.startswith("apub:chan:"), IsAdmin())
async def cb_pick_channel(call: CallbackQuery, state: FSMContext):
    arg = call.data.split(":", 2)[2]
    if arg != "none" and not autopub_service.get_channel_by_id(arg):
        await call.answer("Этого канала больше нет в списке — выберите другой.", show_alert=True)
        return await _show_channel_picker(call, state)
    await state.update_data(sr_channel=arg)
    await _next(call, state)


async def _show_chat_picker(obj, state: FSMContext):
    """Чат, где пройдут тесты, — выбирается ВСЕГДА явно, даже если он один."""
    chats = autopub_service.get_chats()
    text = ("💬 <b>Выберите чат, где будут проходить тесты</b>\n\n"
            "Сюда бот по очереди отправит выбранные тесты: лобби и вопросы-викторины. "
            "Это отдельный выбор — не канал анонса.\n\n(⚠️ = у чата не задана ссылка-приглашение)")
    kb = InlineKeyboardBuilder()
    if not chats:
        text += "\n\n⚠️ Нет ни одного чата — добавьте его в ⚙️ Настройках."
        kb.button(text="⚙️ Настройки чата/канала", callback_data="apub:settings")
    for c in chats:
        kb.button(text=f"💬 {c.get('title') or c['id']}{'' if c.get('invite') else ' ⚠️'}",
                  callback_data=f"apub:chat:{c['id']}")
    _back_btn(kb, "chan")
    kb.adjust(1)
    await _screen(obj, text, kb)


@router.callback_query(F.data.startswith("apub:chat:"), IsAdmin())
async def cb_pick_chat(call: CallbackQuery, state: FSMContext):
    chat_id = call.data.split(":", 2)[2]
    if not autopub_service.get_chat_by_id(chat_id):
        await call.answer("Этого чата больше нет в списке — выберите другой.", show_alert=True)
        return await _show_chat_picker(call, state)
    await state.update_data(sr_chat=chat_id)
    await _next(call, state)


async def _ask_bot_announce(obj, state: FSMContext):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, анонсировать", callback_data="apub:botann:yes")
    kb.button(text="❌ Нет, не анонсировать", callback_data="apub:botann:no")
    _back_btn(kb, "chat")
    kb.adjust(1)
    await _screen(obj, "📣 <b>Анонсировать серию в боте</b> для зарегистрированных пользователей?\n\n"
                       "Все получат уведомление со ссылкой на чат тестов.", kb)


@router.callback_query(F.data.startswith("apub:botann:"), IsAdmin())
async def cb_bot_announce_choice(call: CallbackQuery, state: FSMContext):
    await state.update_data(sr_bot_announce=(call.data.split(":")[2] == "yes"))
    await _next(call, state)


async def _rights(bot: Bot, d: dict) -> dict:
    chat_id, chan = d.get("sr_chat"), d.get("sr_channel")
    chat_chk = await autopub_service.check_destination(bot, chat_id, "chat")
    chan_chk = await autopub_service.check_destination(bot, chan, "channel") if chan and chan != "none" else None
    errors = [f"Чат: {e}" for e in chat_chk["errors"]] + ([f"Канал: {e}" for e in chan_chk["errors"]] if chan_chk else [])
    warnings = [f"Чат: {w}" for w in chat_chk["warnings"]] + ([f"Канал: {w}" for w in chan_chk["warnings"]] if chan_chk else [])
    _at, _d, lerr = autopub_service.resolve_launch(d.get("sr_launch_mode"), d.get("sr_launch_value"))
    if lerr:
        errors.append(lerr)
    return {"chat": chat_chk, "channel": chan_chk, "errors": errors, "warnings": warnings}


async def _show_confirm(obj, state: FSMContext):
    """«Проверьте настройки серии»: всё, что и куда будет отправлено, + права бота."""
    d = await state.get_data()
    chk = await _rights(obj.bot, d)
    chat_row = autopub_service.get_chat_by_id(d["sr_chat"]) or {}
    chan = d["sr_channel"]
    chan_row = autopub_service.get_channel_by_id(chan) or {} if chan != "none" else None
    tests = _tests_of(d)
    titles = _titles(tests)
    lines = ["🧾 <b>Проверьте настройки серии</b>", ""]
    if d.get("sr_mode") == "mix":
        lines.append(f"Режим: <b>🎲 Микс из 10 вопросов</b> по темам ({len(titles)}):")
    else:
        lines.append(f"Тестов: <b>{len(titles)}</b>")
    lines += [f"{i}. {utils.escape_html(t)}" for i, t in enumerate(titles, 1)]
    lines.append("")
    lines += autopub_service.launch_lines(d.get("sr_launch_mode"), d.get("sr_launch_value"))
    lines.append("Канал анонса: <b>" + (utils.escape_html(chan_row.get("title") or chan) if chan_row is not None
                                        else "без анонса на канал") + "</b>")
    lines.append(f"Чат для тестов: <b>{utils.escape_html(chat_row.get('title') or d['sr_chat'])}</b>")
    if d.get("sr_mode") != "mix":
        lines.append(f"Интервал: <b>{autopub_service.INTERVAL_MIN}–{autopub_service.INTERVAL_MAX} секунд после "
                     f"окончания каждого теста</b>")
    lines.append(f"Анонс в боте: <b>{'да' if d.get('sr_bot_announce') else 'нет'}</b>")
    warnings = list(chk["warnings"])
    if not chat_row.get("invite"):
        warnings.append("У чата тестов не задана ссылка-приглашение — в анонсе не будет ссылки на чат "
                        "(⚙️ Настройки → 🔗 Задать ссылку чату).")
    busy = autopub_service.active_series_in_chat(d["sr_chat"])
    if busy:
        warnings.append(f"В этом чате уже есть серия #{busy['id']} "
                        f"({autopub_service.SERIES_STATUS_TITLES.get(busy['status'], busy['status'])}, старт "
                        f"{autopub_service.fmt_local(busy['scheduled_at'])}). Если она ещё будет идти, новая начнётся после "
                        f"её окончания (ждём до {autopub_service.SERIES_QUEUE_WAIT_HOURS} ч).")
    lines += ["", "<b>Проверка прав бота:</b>"]
    if chk["channel"] is not None and chk["channel"]["ok"]:
        lines.append("✅ Канал: бот может публиковать анонс")
    if chk["chat"]["ok"]:
        lines.append("✅ Чат: бот может отправлять тесты и викторины")
    lines += [f"❌ {utils.escape_html(e)}" for e in chk["errors"]]
    lines += [f"⚠️ {utils.escape_html(w)}" for w in warnings]
    kb = InlineKeyboardBuilder()
    if chk["errors"]:
        lines += ["", "Исправьте ошибки — до этого серию запланировать нельзя."]
        kb.button(text="🔄 Проверить ещё раз", callback_data="apub:confirm")
    else:
        kb.button(text="✅ Запланировать серию", callback_data="apub:go")
    kb.button(text="✏️ Изменить", callback_data="apub:edit")
    kb.button(text="❌ Отмена", callback_data="apub:cancelwiz")
    kb.adjust(1)
    await _screen(obj, "\n".join(lines), kb)


@router.callback_query(F.data == "apub:confirm", IsAdmin())
async def cb_confirm(call: CallbackQuery, state: FSMContext):
    await _next(call, state)


@router.callback_query(F.data == "apub:go", IsAdmin())
async def cb_go(call: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if not d.get("apub_selected") or not d.get("sr_op"):
        await call.answer("Эта серия уже запланирована или мастер устарел. Откройте «🚀 Запустить серию тестов» заново.",
                          show_alert=True)
        return
    if not d.get("sr_chat"):
        await call.answer("Выберите чат, в котором будут запускаться тесты.", show_alert=True)
        return await _show_chat_picker(call, state)
    if not d.get("sr_channel"):
        await call.answer("Выберите канал для публикации анонса.", show_alert=True)
        return await _show_channel_picker(call, state)
    if not d.get("sr_launch_mode"):
        await call.answer("Выберите способ запуска.", show_alert=True)
        return await _show_launch(call, state)
    chk = await _rights(call.bot, d)            # права — ещё раз, прямо перед созданием
    if chk["errors"]:
        return await _show_confirm(call, state)
    res = autopub_service.create_series(
        tests=_tests_of(d), mode=d.get("sr_mode") or "full", template_id=d.get("sr_template") or 0,
        launch_mode=d["sr_launch_mode"], launch_value=d.get("sr_launch_value"), channel_choice=d["sr_channel"],
        test_chat_id=d["sr_chat"], bot_announce=bool(d.get("sr_bot_announce")), created_by=call.from_user.id,
        op_key=d["sr_op"])
    if not res["ok"]:
        kb = InlineKeyboardBuilder()
        kb.button(text="✏️ Изменить", callback_data="apub:edit")
        kb.button(text="❌ Отмена", callback_data="apub:cancelwiz")
        kb.adjust(1)
        return await _screen(call, "⚠️ <b>Серия не запланирована</b>\n\n" +
                             "\n".join(f"• {utils.escape_html(e)}" for e in res["errors"]), kb)
    s = res["series"]
    await state.clear()
    announced = False
    if not res.get("duplicate"):
        at = autopub_service._parse_utc(s["scheduled_at"])
        if s.get("announcement_channel_id") and at and at > datetime.utcnow():
            announced = await autopub_service.announce_series_planned(call.bot, s)
        if s.get("bot_announce"):
            invite = (autopub_service.get_chat_by_id(s["test_chat_id"]) or {}).get("invite") or ""
            autopub_service.set_bot_announce(invite, s["source_titles"], active=True)
            asyncio.create_task(autopub_service.broadcast_test_announce(
                call.bot, s["source_titles"], invite, autopub_service.when_words(s)))
    chan_txt = utils.escape_html(s.get("announcement_channel_title") or s.get("announcement_channel_id")) \
        if s.get("announcement_channel_id") else "без анонса на канал"
    lines = [f"✅ <b>Серия #{s['id']} {'уже была запланирована' if res.get('duplicate') else 'запланирована'}</b>", "",
             f"Статус: <b>{autopub_service.SERIES_STATUS_TITLES.get(s['status'], s['status'])}</b>",
             f"Старт: <b>{autopub_service.fmt_local(s['scheduled_at'], seconds=s['launch_mode'] == 'seconds')}</b> (Астана)",
             f"Канал анонса: <b>{chan_txt}</b>",
             f"Чат для тестов: <b>{utils.escape_html(s.get('test_chat_title') or s['test_chat_id'])}</b>",
             f"Тестов: <b>{len(s['items'])}</b> — строго по порядку, следующий через "
             f"{autopub_service.INTERVAL_MIN}–{autopub_service.INTERVAL_MAX} сек после окончания предыдущего."]
    if announced:
        lines.append("📢 Анонс опубликован в канале. Тесты начнутся только в назначенное время.")
    lines.append("\nХод серии — в «📋 Очередь публикаций».")
    await _screen(call, "\n".join(lines), _main_menu_kb())
    if s["launch_mode"] == "now":
        asyncio.create_task(autopub_service.series_tick(call.bot))       # не ждать следующего прохода


@router.callback_query(F.data == "apub:edit", IsAdmin())
async def cb_edit(call: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    kb = InlineKeyboardBuilder()
    kb.button(text="📚 Тесты", callback_data="apub:ed:tests")
    if d.get("sr_mode") == "full":
        kb.button(text="🔢 Порядок", callback_data="apub:ed:order")
    kb.button(text="⚙️ Режим (по очереди / микс)", callback_data="apub:ed:mode")
    kb.button(text="📝 Шаблон анонса", callback_data="apub:ed:tpl")
    kb.button(text="⏰ Способ и время запуска", callback_data="apub:ed:launch")
    kb.button(text="📢 Канал анонса", callback_data="apub:ed:chan")
    kb.button(text="💬 Чат для тестов", callback_data="apub:ed:chat")
    kb.button(text="📣 Анонс в боте", callback_data="apub:ed:botann")
    kb.button(text="↩️ К проверке", callback_data="apub:confirm")
    kb.adjust(1)
    await _screen(call, "✏️ <b>Что изменить?</b>\n\nОстальные настройки сохранятся.", kb)


_CLEAR = {"mode": {"sr_mode": None, "sr_order_ok": False}, "order": {"sr_order_ok": False},
          "tpl": {"sr_template": None}, "launch": {"sr_launch_mode": None, "sr_launch_value": None},
          "chan": {"sr_channel": None}, "chat": {"sr_chat": None}, "botann": {"sr_bot_announce": None}}


@router.callback_query(F.data.startswith("apub:ed:"), IsAdmin())
@router.callback_query(F.data.startswith("apub:back:"), IsAdmin())
async def cb_step_reset(call: CallbackQuery, state: FSMContext):
    """«Назад» и «Изменить»: сбросить один шаг — _next() покажет именно его."""
    step = call.data.split(":")[2]
    await state.set_state(None)
    if step == "tests":
        await state.update_data(sr_mode=None, sr_order_ok=False)
        return await _show_categories(call, state)
    if step not in _CLEAR:
        await call.answer()
        return
    await state.update_data(**_CLEAR[step])
    await _next(call, state)


async def _cancel_wizard(obj, state: FSMContext):
    await state.clear()
    await _screen(obj, "❌ Настройка серии отменена — ничего не запланировано.\n\n" + _settings_card_text(),
                  _main_menu_kb())


@router.callback_query(F.data == "apub:cancelwiz", IsAdmin())
async def cb_cancel_wizard(call: CallbackQuery, state: FSMContext):
    await _cancel_wizard(call, state)


# ===================== ОЧЕРЕДЬ И ИСТОРИЯ СЕРИЙ =====================

def _series_block(s: dict) -> str:
    aps = autopub_service
    total = len(s["items"])
    passed = sum(1 for i in s["items"] if i["status"] in ("done", "skipped", "failed"))
    cur = next((i for i in s["items"] if i["status"] in ("running", "launching")), None)
    chan = (utils.escape_html(s.get("announcement_channel_title") or s["announcement_channel_id"])
            if s.get("announcement_channel_id") else "без анонса")
    lines = [f"<b>#{s['id']}</b> · {aps.SERIES_STATUS_TITLES.get(s['status'], s['status'])} · "
             f"{utils.escape_html(aps.launch_title(s['launch_mode'], s.get('delay_seconds') if s['launch_mode'] == 'seconds' else (s.get('delay_seconds') or 0) // 60))}",
             f"   ⏰ план: {aps.fmt_local(s['scheduled_at'], seconds=True)}"
             + (f" · факт: {aps.fmt_local(s['started_at'], seconds=True)}" if s.get("started_at") else ""),
             f"   📢 {chan} → 💬 {utils.escape_html(s.get('test_chat_title') or s['test_chat_id'])}",
             f"   📚 тестов {total}, пройдено {passed}"
             + (f" · сейчас: {utils.escape_html(cur['title'])} ({cur['order_index'] + 1}/{total})" if cur else "")]
    if s["status"] == "waiting_next" and s.get("next_run_at"):
        lines.append(f"   ⏳ следующий тест: {aps.fmt_local(s['next_run_at'], seconds=True)}")
    if s.get("error"):
        lines.append(f"   ⚠️ {utils.escape_html(s['error'])}")
    return "\n".join(lines)


@router.callback_query(F.data == "apub:queue", IsAdmin())
async def cb_show_queue(call: CallbackQuery):
    active = autopub_service.list_series(autopub_service.ACTIVE_SERIES, 20)
    kb = InlineKeyboardBuilder()
    if active:
        text = "📋 <b>Серии тестов: запланированные и идущие</b>\n\n" + "\n\n".join(_series_block(s) for s in active)
        for s in active:
            kb.button(text=f"🔎 #{s['id']} подробно", callback_data=f"apub:sview:{s['id']}")
            kb.button(text=f"❌ Отменить #{s['id']}", callback_data=f"apub:scancel:{s['id']}")
    else:
        text = "📋 <b>Запланированных и идущих серий нет.</b>"
    kb.button(text="📜 История серий", callback_data="apub:shist")
    kb.button(text="↩️ Назад", callback_data="adm:autopub")
    kb.adjust(*([2] * len(active)), 1, 1)
    await _screen(call, text, kb)


@router.callback_query(F.data == "apub:shist", IsAdmin())
async def cb_series_history(call: CallbackQuery):
    rows = [s for s in autopub_service.list_series(None, 15) if s["status"] not in autopub_service.ACTIVE_SERIES]
    kb = InlineKeyboardBuilder()
    for s in rows:
        kb.button(text=f"🔎 #{s['id']} · {autopub_service.SERIES_STATUS_TITLES.get(s['status'], s['status'])}",
                  callback_data=f"apub:sview:{s['id']}")
    kb.button(text="↩️ К очереди", callback_data="apub:queue")
    kb.adjust(1)
    text = ("📜 <b>История серий</b> (план / факт)\n\n" + "\n\n".join(_series_block(s) for s in rows)) if rows \
        else "📜 <b>История серий пуста.</b>"
    await _screen(call, text[:4000], kb)


@router.callback_query(F.data.startswith("apub:sview:"), IsAdmin())
async def cb_series_view(call: CallbackQuery):
    s = autopub_service.get_series(int(call.data.split(":")[2]))
    if not s:
        await call.answer("Серия не найдена.", show_alert=True)
        return
    aps = autopub_service
    lines = [_series_block(s), "", "<b>Тесты по порядку:</b>"]
    for i in s["items"]:
        lines.append(f"{i['order_index'] + 1}. {utils.escape_html(i['title'])} — {aps.ITEM_STATUS_TITLES.get(i['status'], i['status'])}"
                     + (f" · запуск {aps.fmt_local(i['launched_at'], seconds=True)}" if i.get("launched_at") else "")
                     + (f" · конец {aps.fmt_local(i['finished_at'], seconds=True)}" if i.get("finished_at") else "")
                     + (f" · {utils.escape_html(i['error'])}" if i.get("error") else ""))
    ev = aps.series_events(s["id"], 12)
    if ev:
        lines += ["", "<b>Журнал:</b>"]
        lines += [f"{aps.fmt_local(e['created_at'], seconds=True)} {e['event']}"
                  + (f" · тест {e['position'] + 1}" if (e.get('position') or -1) >= 0 else "")
                  + (f" · {utils.escape_html(e['details'][:120])}" if e.get("details") else "") for e in reversed(ev)]
    kb = InlineKeyboardBuilder()
    if s["status"] in aps.ACTIVE_SERIES:
        kb.button(text=f"❌ Отменить серию #{s['id']}", callback_data=f"apub:scancel:{s['id']}")
    kb.button(text="↩️ К очереди", callback_data="apub:queue")
    kb.adjust(1)
    await _screen(call, "\n".join(lines)[:4000], kb)


async def _stop_series_side_effects(bot: Bot, s: dict) -> tuple:
    """Отменённая серия: остановить её идущий тест и открыть чат, если серия его закрывала."""
    finalized = unlocked = False
    chat = int(s["test_chat_id"])
    try:
        from services import group_quiz_service
        if any(i["status"] in ("running", "launching") for i in s["items"]) or db.fetchone(
                "SELECT id FROM group_quizzes WHERE chat_id=? AND status IN ('lobby','running') AND id IN "
                "(SELECT group_quiz_id FROM autopub_series_items WHERE series_id=?)", (chat, s["id"])):
            finalized = (await group_quiz_service.stop_quiz(bot, chat, 0))[0]
    except Exception as e:
        log.warning("серия %s: остановить тест: %s", s["id"], e)
    if s.get("chat_locked"):
        try:
            unlocked = await autopub_service._unlock_chat(bot, chat)
        except Exception as e:
            log.warning("серия %s: открыть чат: %s", s["id"], e)
    return finalized, unlocked


@router.callback_query(F.data.startswith("apub:scancel:"), IsAdmin())
async def cb_series_cancel(call: CallbackQuery, bot: Bot):
    sid = int(call.data.split(":")[2])
    before = autopub_service.get_series(sid)
    s = autopub_service.cancel_series(sid, "отменена администратором")
    if not s:
        await call.answer("Серия уже завершена или отменена.", show_alert=True)
    else:
        await _stop_series_side_effects(bot, before)
        await call.answer(f"🚫 Серия #{sid} отменена")
    await cb_show_queue(call)


@router.callback_query(F.data == "apub:clear", IsAdmin())
async def cb_clear_queue(call: CallbackQuery, bot: Bot):
    """Отменить все запланированные и идущие серии, очистить старую очередь."""
    n = 0
    for s in autopub_service.list_series(autopub_service.ACTIVE_SERIES, 100):
        if autopub_service.cancel_series(s["id"], "очередь очищена администратором"):
            n += 1
            await _stop_series_side_effects(bot, s)
    autopub_service.clear_all_queue()
    autopub_service.clear_active_series()
    try:
        from services import group_quiz_service
        for c in autopub_service.get_chats():
            try:
                await group_quiz_service.stop_quiz(bot, int(c['id']), 0)
            except Exception:
                pass
    except Exception:
        pass
    await call.answer(f"🧹 Отменено серий: {n}", show_alert=True)
    await _screen(call, f"🧹 <b>Готово!</b>\n\nОтменено серий: <b>{n}</b>. Зависшие лобби остановлены.\n\n"
                        f"Теперь можно запустить серию заново.", _main_menu_kb())


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
    text = (f"🎲 <b>{autopub_service.get_random_count()} вопрос(ов) на канал</b>\n\n"
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
        "WHERE status='active' AND language=?", (lang,))
    for t in tests:
        by_cat[t.get('category_id')].append(t)

    if not tests:
        try:
            await msg_obj.edit_text("⚠️ Нет подходящих тестов на этом языке.")
        except Exception:
            pass
        return

    text = (f"🎲 <b>{autopub_service.get_random_count()} вопрос(ов)</b> · "
            f"{'🇷🇺' if lang == 'ru' else '🇰🇿'}\n\n"
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
            "AND language=? AND category_id IS NULL ORDER BY id DESC", (lang,))
        cat_title = "📭 Без раздела"
    else:
        cat_id = int(arg)
        cat = db.fetchone("SELECT * FROM test_categories WHERE id=?", (cat_id,))
        tests = db.fetchall(
            "SELECT id, title FROM tests WHERE status='active' "
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
            "SELECT id FROM tests WHERE status='active' "
            "AND language=? AND category_id IS NULL",
            (lang,))
    else:
        tests = db.fetchall(
            "SELECT id FROM tests WHERE status='active' "
            "AND language=? AND category_id=?",
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
            f"🎲 Публикую {autopub_service.get_random_count()} вопрос(ов) "
            f"на «{chan_name}»…\n\n"
            f"⏳ Между вопросами 10 сек.")
    except Exception:
        pass
    qty = autopub_service.get_random_count()
    sent, failed = await autopub_service.post_random_quiz_polls_to_channel(
        call.bot, count=qty, language=lang, bot_username=bot_username,
        test_ids=selected, channel_id=channel_id, send_promo=False)
    msg = (f"✅ <b>Готово!</b>\n\n"
            f"Канал: <b>{chan_name}</b>\n"
            f"Отправлено вопросов: <b>{sent}</b>\n"
            f"Ошибок: {failed}\n\n"
            f"Отправить в канал промо-приглашение в бота?")
    # Кнопки: отправить промо или нет
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    promo_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, отправить приглашение",
                              callback_data=f"rndpromo:{channel_id}")],
        [InlineKeyboardButton(text="❌ Нет, не надо", callback_data="rndpromo:no")]
    ])
    try:
        await call.message.answer(msg, parse_mode="HTML", reply_markup=promo_kb)
    except Exception:
        pass


@router.callback_query(F.data.startswith("rndpromo:"), IsAdmin())
async def cb_rnd_promo(call: CallbackQuery):
    """Отправить или нет промо-приглашение после 10 вопросов."""
    arg = call.data.split(":", 1)[1]
    if arg == "no":
        await call.answer("Промо не отправлено.", show_alert=True)
        try:
            await call.message.edit_text("✅ Готово. Промо-приглашение не отправлено.")
        except Exception:
            pass
        return
    channel_id = arg
    await call.answer("Отправляю приглашение…")
    bot_username = ''
    try:
        me = await call.bot.get_me()
        bot_username = me.username or ''
    except Exception:
        pass
    ok = await autopub_service.send_promo_to_channel(
        call.bot, channel_id, bot_username=bot_username)
    try:
        if ok:
            await call.message.edit_text("✅ Промо-приглашение отправлено в канал!")
        else:
            await call.message.edit_text("⚠️ Не удалось отправить промо.")
    except Exception:
        pass


# ===================== СКОЛЬКО ВОПРОСОВ ЗА ПУБЛИКАЦИЮ =====================

class QtyStates(StatesGroup):
    waiting_qty = State()


def _qty_screen():
    cur = autopub_service.get_random_count()
    kb = InlineKeyboardBuilder()
    for n in (5, 10, 15, 20):
        kb.button(text=("✅ " if n == cur else "") + str(n), callback_data=f"apubqty:{n}")
    kb.adjust(4)
    kb.row(InlineKeyboardButton(text="✏️ Ввести вручную", callback_data="apubqty:manual"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:autopub"))
    text = (f"🔢 <b>Количество вопросов за одну публикацию</b>\n\n"
            f"Сейчас: <b>{cur}</b> вопрос(ов)\n\n"
            f"Вопросы делятся между выбранными тестами максимально поровну, "
            f"а внутри теста берутся случайно.")
    return text, kb.as_markup()


@router.callback_query(F.data == "apub:qty", IsAdmin())
async def cb_qty(call: CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = _qty_screen()
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("apubqty:"), IsAdmin())
async def cb_qty_set(call: CallbackQuery, state: FSMContext):
    arg = call.data.split(":")[1]
    if arg == "manual":
        await state.set_state(QtyStates.waiting_qty)
        await call.message.answer(
            "✏️ Введите количество вопросов числом.\n\n"
            "Например: <code>7</code>, <code>17</code>, <code>25</code>\n\n"
            "/cancel — отмена", parse_mode="HTML")
        await call.answer()
        return
    autopub_service.set_random_count(int(arg))
    text, kb = _qty_screen()
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass
    await call.answer(f"Сохранено: {arg}")


@router.message(QtyStates.waiting_qty, IsAdmin())
async def msg_qty(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.startswith("/cancel"):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    if not raw.isdigit() or not (1 <= int(raw) <= 100):
        await message.answer("❌ Введите корректное количество вопросов "
                             "(целое число от 1 до 100).")
        return
    autopub_service.set_random_count(int(raw))
    await state.clear()
    text, kb = _qty_screen()
    await message.answer(
        f"✅ Количество вопросов за одну автопубликацию изменено: <b>{int(raw)}</b>\n\n"
        + text, reply_markup=kb, parse_mode="HTML")


# ===================== РУЧНОЙ ВЫБОР ВОПРОСОВ =====================
# Сама выборка делается на сайте: отметить 10 вопросов из 200 в переписке
# с ботом неудобно. Здесь — только объяснение и кнопка-переход.

@router.callback_query(F.data == "apub:manual", IsAdmin())
async def cb_manual_pick(call: CallbackQuery):
    from webapp import learning as _lg
    from services import publication_service as _ps

    try:
        site = _lg._site_url_sync().rstrip("/")
    except Exception:
        site = ""
    drafts = 0
    try:
        drafts = len([p for p in _ps.all_publications() if p["status"] != "published"])
    except Exception:
        pass

    kb = InlineKeyboardBuilder()
    if site:
        kb.button(text="✋ Открыть выбор вопросов", url=f"{site}/admin/publications")
    kb.button(text="↩️ Назад", callback_data="adm:autopub")
    kb.adjust(1)

    text = (
        "✋ <b>Ручной выбор вопросов</b>\n\n"
        "Выбираете предмет → раздел → урок → тест, отмечаете нужные вопросы "
        "галочками или вписываете их номера через запятую (например "
        "<code>5, 7, 10, 14</code>). Порядок сохранится ровно такой, как вы задали.\n\n"
        "Дальше — предпросмотр и публикация: в канал уйдёт короткий пост "
        "с кнопкой «НАЧАТЬ ТЕСТ», а ученик пройдёт именно ваши вопросы.\n\n"
        f"📝 Черновиков сейчас: <b>{drafts}</b>\n\n"
        "<i>Выбор делается на сайте — там удобно листать даже 200 вопросов.</i>"
    )
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
    except Exception:
        await call.message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
    await call.answer()
