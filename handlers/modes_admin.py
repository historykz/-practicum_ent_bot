"""
Админка режимов (вкл/выкл, цены, бесплатно, выдать прохождения, доход)
и история результатов для пользователей (Мои результаты).
"""
import json
import logging
from datetime import datetime, timezone, timedelta

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                            InlineKeyboardButton)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database as db
import utils
from filters import IsAdmin
from services import modes_service as ms

router = Router(name="modes_admin")
log = logging.getLogger(__name__)

ALMATY = timezone(timedelta(hours=5))  # Asia/Almaty UTC+5


def _fmt_time(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(ALMATY)
        return local.strftime("%d.%m.%Y, %H:%M")
    except Exception:
        return iso_str


# ===================== АДМИНКА РЕЖИМОВ =====================

class GrantStates(StatesGroup):
    waiting_user = State()
    waiting_count = State()


class PriceModeStates(StatesGroup):
    waiting = State()


def _modes_text(test_id: int) -> str:
    m = ms.get_modes(test_id)
    test = db.fetchone("SELECT title FROM tests WHERE id=?", (test_id,))
    fc_on = "ВКЛ ✅" if m.get('flashcards_enabled') else "ВЫКЛ ❌"
    ln_on = "ВКЛ ✅" if m.get('learning_enabled') else "ВЫКЛ ❌"
    free = "🆓 Бесплатно для всех" if m.get('is_free') else "💰 Платно"
    return (f"⚙️ <b>Режимы теста «{utils.escape_html(test['title'] if test else '')}»</b>\n\n"
            f"🃏 Карточки: {fc_on}\n"
            f"🧠 Заучивание: {ln_on}\n"
            f"{free}\n\n"
            f"<b>Цены Карточки:</b> 1={m['fc_price_1']}⭐️ · "
            f"10={m['fc_price_10']}⭐️ · повтор={m['fc_price_redo']}⭐️\n"
            f"<b>Цены Заучивание:</b> 1={m['ln_price_1']}⭐️ · "
            f"10={m['ln_price_10']}⭐️ · повтор={m['ln_price_redo']}⭐️")


def _modes_kb(test_id: int) -> InlineKeyboardMarkup:
    m = ms.get_modes(test_id)
    kb = InlineKeyboardBuilder()
    kb.button(text=("🃏 Выключить Карточки" if m.get('flashcards_enabled')
                    else "🃏 Включить Карточки"),
              callback_data=f"mtoggle:fc:{test_id}")
    kb.button(text=("🧠 Выключить Заучивание" if m.get('learning_enabled')
                    else "🧠 Включить Заучивание"),
              callback_data=f"mtoggle:ln:{test_id}")
    kb.button(text=("💰 Сделать платными" if m.get('is_free')
                    else "🆓 Сделать бесплатными"),
              callback_data=f"mfree:{test_id}")
    kb.button(text="💵 Цены Карточки", callback_data=f"mprice:fc:{test_id}")
    kb.button(text="💵 Цены Заучивание", callback_data=f"mprice:ln:{test_id}")
    kb.button(text="🎁 Выдать прохождения", callback_data=f"mgrant:{test_id}")
    kb.button(text="↩️ Назад", callback_data=f"admtest:{test_id}")
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(F.data.startswith("admmodes:"), IsAdmin())
async def cb_admin_modes(call: CallbackQuery):
    test_id = int(call.data.split(":")[1])
    await call.message.answer(_modes_text(test_id), reply_markup=_modes_kb(test_id),
                               parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("mtoggle:"), IsAdmin())
async def cb_mtoggle(call: CallbackQuery):
    _, sm, tid = call.data.split(":")
    test_id = int(tid)
    mode = 'flashcards' if sm == 'fc' else 'learning'
    cur = ms.is_mode_enabled(test_id, mode)
    ms.set_mode_enabled(test_id, mode, not cur)
    try:
        await call.message.edit_text(_modes_text(test_id),
                                      reply_markup=_modes_kb(test_id),
                                      parse_mode="HTML")
    except Exception:
        pass
    await call.answer("Готово")


@router.callback_query(F.data.startswith("mfree:"), IsAdmin())
async def cb_mfree(call: CallbackQuery):
    test_id = int(call.data.split(":")[1])
    m = ms.get_modes(test_id)
    ms.set_free(test_id, not m.get('is_free'))
    try:
        await call.message.edit_text(_modes_text(test_id),
                                      reply_markup=_modes_kb(test_id),
                                      parse_mode="HTML")
    except Exception:
        pass
    await call.answer("Готово")


@router.callback_query(F.data.startswith("mprice:"), IsAdmin())
async def cb_mprice(call: CallbackQuery, state: FSMContext):
    _, sm, tid = call.data.split(":")
    mode = 'flashcards' if sm == 'fc' else 'learning'
    await state.set_state(PriceModeStates.waiting)
    await state.update_data(price_test=int(tid), price_mode=mode)
    name = "Карточки" if mode == 'flashcards' else "Заучивание"
    await call.message.answer(
        f"💵 Введи 3 цены для «{name}» через пробел:\n"
        f"<code>цена_1  цена_10  цена_повтора</code>\n\n"
        f"Например: <code>5 10 2</code>\n\n/cancel — отмена",
        parse_mode="HTML")
    await call.answer()


@router.message(PriceModeStates.waiting, IsAdmin())
async def msg_mprice(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    parts = (message.text or '').split()
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        await message.answer("Введи 3 числа через пробел, например: 5 10 2")
        return
    p1, p10, predo = (int(x) for x in parts)
    data = await state.get_data()
    await state.clear()
    ms.set_prices(data['price_test'], data['price_mode'], p1, p10, predo)
    name = "Карточки" if data['price_mode'] == 'flashcards' else "Заучивание"
    await message.answer(
        f"✅ Цены «{name}» обновлены:\n1 раз={p1}⭐️ · 10 раз={p10}⭐️ · повтор={predo}⭐️")


@router.callback_query(F.data.startswith("mgrant:"), IsAdmin())
async def cb_mgrant(call: CallbackQuery, state: FSMContext):
    test_id = int(call.data.split(":")[1])
    await state.set_state(GrantStates.waiting_user)
    await state.update_data(grant_test=test_id)
    await call.message.answer(
        "🎁 Введи @username пользователя, которому выдать прохождения:\n\n"
        "/cancel — отмена")
    await call.answer()


@router.message(GrantStates.waiting_user, IsAdmin())
async def msg_grant_user(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    uname = (message.text or '').strip().lstrip('@')
    u = db.fetchone("SELECT * FROM users WHERE LOWER(username)=LOWER(?)", (uname,))
    if not u:
        await message.answer(f"⚠️ @{uname} не найден. Введи ещё раз или /cancel")
        return
    await state.update_data(grant_user_tg=u['tg_id'])
    await state.set_state(GrantStates.waiting_count)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🃏 Карточки", callback_data="grantmode:fc"),
         InlineKeyboardButton(text="🧠 Заучивание", callback_data="grantmode:ln")]])
    await message.answer("Какой режим и сколько? Сначала выбери режим:",
                          reply_markup=kb)


@router.callback_query(F.data.startswith("grantmode:"), IsAdmin())
async def cb_grant_mode(call: CallbackQuery, state: FSMContext):
    sm = call.data.split(":")[1]
    mode = 'flashcards' if sm == 'fc' else 'learning'
    await state.update_data(grant_mode=mode)
    await call.message.answer("Введи количество прохождений (число):")
    await call.answer()


@router.message(GrantStates.waiting_count, IsAdmin())
async def msg_grant_count(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    if not (message.text or '').strip().isdigit():
        await message.answer("Введи число, например 5")
        return
    count = int(message.text.strip())
    data = await state.get_data()
    mode = data.get('grant_mode')
    if not mode:
        await message.answer("Сначала выбери режим кнопкой выше.")
        return
    await state.clear()
    ms.grant_free_passes(data['grant_user_tg'], data['grant_test'], mode, count)
    name = "Карточки" if mode == 'flashcards' else "Заучивание"
    await message.answer(f"✅ Выдано {count} прохождений «{name}».")


# ===================== ИСТОРИЯ РЕЗУЛЬТАТОВ (для юзера) =====================

@router.callback_query(F.data == "myresults")
async def cb_my_results(call: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Обычные тесты", callback_data="myres:tests")
    kb.button(text="🃏 Карточки", callback_data="myres:flashcards")
    kb.button(text="🧠 Заучивание", callback_data="myres:learning")
    kb.button(text="↩️ Назад", callback_data="m:profile")
    kb.adjust(1)
    try:
        await call.message.edit_text("📊 <b>Мои результаты</b>\n\nВыбери раздел:",
                                      reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        await call.message.answer("📊 <b>Мои результаты</b>\n\nВыбери раздел:",
                                    reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("myres:"))
async def cb_my_results_mode(call: CallbackQuery):
    what = call.data.split(":")[1]
    if what == "tests":
        await call.answer("Результаты обычных тестов — в разделе профиля.",
                          show_alert=True)
        return
    mode = what  # flashcards | learning
    rows = db.fetchall(
        "SELECT * FROM mode_results WHERE user_tg_id=? AND mode=? "
        "ORDER BY id DESC LIMIT 10", (call.from_user.id, mode))
    name = "🃏 Карточки" if mode == 'flashcards' else "🧠 Заучивание"
    if not rows:
        await call.answer(f"{name}: пока нет результатов.", show_alert=True)
        return
    lines = [f"{name} — последние результаты:\n"]
    for r in rows:
        test = db.fetchone("SELECT title FROM tests WHERE id=?", (r['test_id'],))
        tname = test['title'] if test else '—'
        when = _fmt_time(r.get('created_at'))
        if mode == 'flashcards':
            lines.append(
                f"• <b>{utils.escape_html(tname)}</b>\n"
                f"  {when}\n"
                f"  ✅ Знаю: {r['know_count']} · ❌ Не знаю: {r['dontknow_count']} "
                f"(из {r['total']})")
        else:
            correct = (r['correct_first'] or 0) + (r['correct_retry'] or 0)
            lines.append(
                f"• <b>{utils.escape_html(tname)}</b>\n"
                f"  {when}\n"
                f"  Результат: {correct}/{r['total']} · "
                f"❌ Ошибки: {r['wrong_count']}")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="↩️ Назад", callback_data="myresults")]])
    try:
        await call.message.edit_text("\n".join(lines), reply_markup=kb,
                                      parse_mode="HTML")
    except Exception:
        await call.message.answer("\n".join(lines), reply_markup=kb,
                                    parse_mode="HTML")
    await call.answer()
