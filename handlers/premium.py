"""
Подписка Премиум — доступ ко всем тестам и режимам за Stars.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Router, F, Bot
from aiogram.types import (Message, CallbackQuery, LabeledPrice,
                            InlineKeyboardMarkup, InlineKeyboardButton)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import database as db
import utils

router = Router(name="premium")
log = logging.getLogger(__name__)
ALMATY = timezone(timedelta(hours=5))


def _pl(**kw):
    return json.dumps(kw, separators=(',', ':'))


def get_premium_price() -> int:
    """Цена Премиума в звёздах: из БД (настройка админа), потом config."""
    try:
        row = db.fetchone("SELECT value FROM settings WHERE key='premium_price_stars'")
        if row and row.get('value'):
            return int(row['value'])
    except Exception:
        pass
    return config.PREMIUM_PRICE_STARS


def get_premium_days() -> int:
    """Срок Премиума в днях: из БД, потом config."""
    try:
        row = db.fetchone("SELECT value FROM settings WHERE key='premium_days'")
        if row and row.get('value'):
            return int(row['value'])
    except Exception:
        pass
    return config.PREMIUM_DAYS


def set_premium_price(stars: int):
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('premium_price_stars', ?)",
                (str(stars),))


def set_premium_days(days: int):
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('premium_days', ?)",
                (str(days),))


def get_referral_friends_needed() -> int:
    """Сколько друзей нужно для награды: из БД, потом дефолт 10."""
    try:
        row = db.fetchone("SELECT value FROM settings WHERE key='referral_friends'")
        if row and row.get('value'):
            return int(row['value'])
    except Exception:
        pass
    return 10


def get_referral_reward_days() -> int:
    """Сколько дней премиума за друзей: из БД, потом дефолт 30."""
    try:
        row = db.fetchone("SELECT value FROM settings WHERE key='referral_reward_days'")
        if row and row.get('value'):
            return int(row['value'])
    except Exception:
        pass
    return 30


def set_referral_friends(n: int):
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('referral_friends', ?)",
                (str(n),))


def set_referral_reward_days(days: int):
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('referral_reward_days', ?)",
                (str(days),))


def build_premium_offer(user: dict) -> tuple:
    """Текст + клавиатура предложения Премиума. Общее для callback и /start-диплинка."""
    lang = user.get('language') or 'ru'
    stars = get_premium_price()
    days = get_premium_days()
    uid = user.get('id')
    is_prem = False
    try:
        is_prem = utils.is_premium(uid)
    except Exception:
        pass
    prem_note = ""
    if is_prem:
        prem_note = "\n\n✅ У тебя уже есть Премиум. Покупка продлит его."

    text = (
        f"💎 <b>Премиум-подписка</b>\n\n"
        f"Что даёт:\n"
        f"✅ Доступ ко <b>всем платным тестам</b>\n"
        f"🃏 Режимы <b>Карточки и Заучивание бесплатно</b>\n"
        f"🔁 Бесплатный повтор ошибок\n"
        f"⚡️ Без ограничений\n\n"
        f"💰 Цена: <b>{stars} ⭐️</b> на <b>{days} дней</b>{prem_note}"
        if lang == "ru" else
        f"💎 <b>Премиум жазылым</b>\n\n"
        f"Не береді:\n"
        f"✅ Барлық ақылы тесттерге қол жеткізу\n"
        f"🃏 Карточкалар мен Жаттау тегін\n\n"
        f"💰 Бағасы: <b>{stars} ⭐️</b> {days} күнге{prem_note}"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text=f"💎 Купить за {stars} ⭐️", callback_data="premium:pay")
    kb.button(text="🎁 Или пригласи 10 друзей (бесплатно)",
              callback_data="premium:refer")
    kb.button(text="🔙 Назад", callback_data="m:menu")
    kb.adjust(1)
    return text, kb.as_markup()


@router.callback_query(F.data == "buy:premium")
async def cb_buy_premium(call: CallbackQuery, user: dict):
    text, markup = build_premium_offer(user)
    try:
        await call.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=markup, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "premium:pay")
async def cb_premium_pay(call: CallbackQuery, bot: Bot):
    stars = get_premium_price()
    days = get_premium_days()
    await call.answer()
    try:
        await bot.send_invoice(
            chat_id=call.message.chat.id,
            title=f"Премиум {days} дней"[:32],
            description="Доступ ко всем тестам и режимам",
            payload=_pl(k="premium", d=days),
            currency="XTR",
            prices=[LabeledPrice(label=f"Премиум {days} дней", amount=stars)])
    except Exception as e:
        await call.message.answer(f"⚠️ Не смог создать счёт: {e}")


@router.callback_query(F.data == "premium:refer")
async def cb_premium_refer(call: CallbackQuery, user: dict):
    """Показать реферальную программу."""
    await call.answer()
    from handlers import referral as _ref
    await _ref.show_referral(call, user)


async def handle_premium_payment(message: Message, bot: Bot, pl: dict,
                                  charge: str, stars: int):
    """Обработка оплаты премиума (вызывается из payments.on_payment)."""
    days = pl.get('d', get_premium_days())
    tg_id = message.from_user.id
    user = db.fetchone("SELECT id FROM users WHERE tg_id=?", (tg_id,))
    if not user:
        await message.answer("⚠️ Ошибка: пользователь не найден.")
        return
    from services import referral_reward_service as _rrs
    _rrs._grant_premium(user['id'], days)
    # Записываем покупку
    try:
        db.execute(
            "INSERT INTO purchases (user_tg_id, kind, stars_amount, charge_id) "
            "VALUES (?,?,?,?)", (tg_id, 'premium', stars, charge))
    except Exception:
        pass
    exp = db.fetchone("SELECT expires_at FROM premium_users WHERE user_id=?",
                       (user['id'],))
    exp_label = ""
    if exp and exp.get('expires_at'):
        try:
            dt = datetime.fromisoformat(exp['expires_at'].replace('Z', '+00:00'))
            exp_label = dt.astimezone(ALMATY).strftime("%d.%m.%Y")
        except Exception:
            pass
    await message.answer(
        f"🎉 <b>Премиум активирован!</b>\n\n"
        f"✅ Теперь у тебя доступ ко всем тестам и режимам.\n"
        f"📅 Действует до: <b>{exp_label}</b> (Астана)\n\n"
        f"Удачи на ЕНТ! 💪", parse_mode="HTML")


# ===================== АДМИНКА: НАСТРОЙКА ПРЕМИУМА =====================
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from filters import IsAdmin


class PremiumSettingsStates(StatesGroup):
    waiting_price = State()
    waiting_days = State()
    waiting_ref_friends = State()
    waiting_ref_days = State()


def _settings_text() -> str:
    return (
        f"⚙️ <b>Настройки Премиума и рефералов</b>\n\n"
        f"💎 <b>Премиум-подписка:</b>\n"
        f"• Цена: <b>{get_premium_price()} ⭐️</b>\n"
        f"• Срок: <b>{get_premium_days()} дней</b>\n\n"
        f"🎁 <b>Реферальная программа:</b>\n"
        f"• Нужно друзей: <b>{get_referral_friends_needed()}</b>\n"
        f"• Награда: <b>{get_referral_reward_days()} дней</b> премиума\n\n"
        f"Нажми что изменить:")


def _settings_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💰 Цена Премиума", callback_data="premadm:price")
    kb.button(text="📅 Срок Премиума", callback_data="premadm:days")
    kb.button(text="👥 Кол-во друзей", callback_data="premadm:friends")
    kb.button(text="🎁 Награда (дней)", callback_data="premadm:refdays")
    kb.button(text="↩️ Назад", callback_data="m:admin")
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(F.data == "adm:premium_settings", IsAdmin())
async def cb_premium_settings(call: CallbackQuery):
    await call.message.answer(_settings_text(), reply_markup=_settings_kb(),
                               parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "premadm:price", IsAdmin())
async def cb_set_price(call: CallbackQuery, state: FSMContext):
    await state.set_state(PremiumSettingsStates.waiting_price)
    await call.message.answer(
        f"💰 Текущая цена: {get_premium_price()} ⭐️\n\n"
        f"Введи новую цену Премиума в звёздах (число):\n\n/cancel — отмена")
    await call.answer()


@router.message(PremiumSettingsStates.waiting_price, IsAdmin())
async def msg_set_price(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    if not (message.text or '').strip().isdigit():
        await message.answer("Введи число, например 300")
        return
    set_premium_price(int(message.text.strip()))
    await state.clear()
    await message.answer(f"✅ Цена Премиума: {message.text.strip()} ⭐️",
                          reply_markup=_settings_kb())


@router.callback_query(F.data == "premadm:days", IsAdmin())
async def cb_set_days(call: CallbackQuery, state: FSMContext):
    await state.set_state(PremiumSettingsStates.waiting_days)
    await call.message.answer(
        f"📅 Текущий срок: {get_premium_days()} дней\n\n"
        f"Введи новый срок Премиума в днях (число):\n\n/cancel — отмена")
    await call.answer()


@router.message(PremiumSettingsStates.waiting_days, IsAdmin())
async def msg_set_days(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    if not (message.text or '').strip().isdigit():
        await message.answer("Введи число, например 30")
        return
    set_premium_days(int(message.text.strip()))
    await state.clear()
    await message.answer(f"✅ Срок Премиума: {message.text.strip()} дней",
                          reply_markup=_settings_kb())


@router.callback_query(F.data == "premadm:friends", IsAdmin())
async def cb_set_friends(call: CallbackQuery, state: FSMContext):
    await state.set_state(PremiumSettingsStates.waiting_ref_friends)
    await call.message.answer(
        f"👥 Сейчас нужно друзей: {get_referral_friends_needed()}\n\n"
        f"Введи сколько друзей нужно пригласить для награды (число):\n\n/cancel")
    await call.answer()


@router.message(PremiumSettingsStates.waiting_ref_friends, IsAdmin())
async def msg_set_friends(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    if not (message.text or '').strip().isdigit():
        await message.answer("Введи число, например 10")
        return
    set_referral_friends(int(message.text.strip()))
    await state.clear()
    await message.answer(f"✅ Нужно друзей: {message.text.strip()}",
                          reply_markup=_settings_kb())


@router.callback_query(F.data == "premadm:refdays", IsAdmin())
async def cb_set_refdays(call: CallbackQuery, state: FSMContext):
    await state.set_state(PremiumSettingsStates.waiting_ref_days)
    await call.message.answer(
        f"🎁 Сейчас награда: {get_referral_reward_days()} дней\n\n"
        f"Введи сколько дней премиума давать за друзей (число):\n\n/cancel")
    await call.answer()


@router.message(PremiumSettingsStates.waiting_ref_days, IsAdmin())
async def msg_set_refdays(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    if not (message.text or '').strip().isdigit():
        await message.answer("Введи число, например 30")
        return
    set_referral_reward_days(int(message.text.strip()))
    await state.clear()
    await message.answer(f"✅ Награда: {message.text.strip()} дней премиума",
                          reply_markup=_settings_kb())
