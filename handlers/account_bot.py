"""
Пароль от сайта — прямо в боте.

Аккаунт определяется по Telegram ID: username переспрашивать не нужно,
он мог смениться. Пароль показываем один раз при выдаче — в базе остаётся
только хэш, поэтому «подсмотреть» прежний нельзя.
"""
import asyncio
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
import utils
from services import account_service as acc

router = Router(name="account_bot")
log = logging.getLogger(__name__)


class PwStates(StatesGroup):
    waiting_new = State()


def _kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Сгенерировать надёжный пароль",
                              callback_data="pw:gen")],
        [InlineKeyboardButton(text="✏️ Придумать свой", callback_data="pw:own")],
        [InlineKeyboardButton(text="↩️ Закрыть", callback_data="pw:close")],
    ])


def _account_line(tg_id: int) -> str:
    a = acc.account_summary(tg_id)
    uname = a["username"] or "не указан"
    return (f"👤 Аккаунт: <b>@{uname}</b>\n"
            + ("🔑 Пароль задан\n" if a["has_password"] else "🔑 Пароль ещё не задан\n"))


@router.message(F.text.in_({"/password", "/parol", "/пароль"}))
async def cmd_password(message: Message, state: FSMContext):
    await state.clear()
    tg_id = message.from_user.id
    # Привязка при первом же обращении к боту: если аккаунт заводили на сайте
    # под этим username, Telegram ID подставится к нему, а не создаст второй.
    await asyncio.to_thread(acc.link_telegram, tg_id,
                            message.from_user.username or "",
                            message.from_user.first_name or "",
                            message.from_user.last_name or "")
    line = await asyncio.to_thread(_account_line, tg_id)
    await message.answer(
        "🔐 <b>Пароль для входа на сайт</b>\n\n" + line +
        "\nПароль хранится в зашифрованном виде — показать прежний нельзя. "
        "Можно выпустить новый: старый сразу перестанет работать.",
        reply_markup=_kb(), parse_mode="HTML")


@router.callback_query(F.data == "pw:close")
async def cb_close(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass


@router.callback_query(F.data == "pw:gen")
async def cb_gen(call: CallbackQuery, state: FSMContext):
    await state.clear()
    tg_id = call.from_user.id
    user = await asyncio.to_thread(acc.find_by_tg, tg_id)
    if not user:
        await call.answer("Аккаунт не найден", show_alert=True)
        return
    pw = await asyncio.to_thread(acc.reset_to_new_password, user["id"])
    await call.answer()
    await call.message.answer(
        "🔑 <b>Новый пароль</b>\n\n"
        f"Логин: <code>{utils.escape_html(user.get('username') or '')}</code>\n"
        f"Пароль: <code>{utils.escape_html(pw)}</code>\n\n"
        "Нажмите на пароль, чтобы скопировать. Сохраните его — показать "
        "повторно мы не сможем. Прежний пароль больше не работает.",
        parse_mode="HTML")


@router.callback_query(F.data == "pw:own")
async def cb_own(call: CallbackQuery, state: FSMContext):
    await state.set_state(PwStates.waiting_new)
    await call.answer()
    await call.message.answer(
        f"✏️ Пришлите новый пароль (не короче {acc.MIN_PASSWORD} символов).\n\n"
        "Сообщение с паролем лучше потом удалить.\n\n/cancel — отмена")


@router.message(PwStates.waiting_new)
async def msg_new_password(message: Message, state: FSMContext):
    if (message.text or "").startswith("/cancel"):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    pw = (message.text or "").strip()
    if not acc.password_strong_enough(pw):
        await message.answer(f"Слишком короткий. Нужно не меньше "
                             f"{acc.MIN_PASSWORD} символов.")
        return
    user = await asyncio.to_thread(acc.find_by_tg, message.from_user.id)
    if not user:
        await state.clear()
        await message.answer("Аккаунт не найден.")
        return
    await asyncio.to_thread(acc.set_password, user["id"], pw)
    await state.clear()
    try:
        await message.delete()      # не оставляем пароль в переписке
    except Exception:
        pass
    await message.answer(
        "✅ Пароль сохранён. Прежний больше не работает.\n\n"
        f"Входить на сайт: логин <code>{utils.escape_html(user.get('username') or '')}</code>",
        parse_mode="HTML")
