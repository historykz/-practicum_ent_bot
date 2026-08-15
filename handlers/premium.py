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


def _setting(key: str, default: str = "") -> str:
    try:
        row = db.fetchone("SELECT value FROM settings WHERE key=?", (key,))
        if row and row.get('value'):
            return str(row['value'])
    except Exception:
        pass
    return default


def stars_enabled() -> bool:
    """Админ может полностью отключить оплату звёздами — тогда кнопки нет."""
    return _setting('premium_stars_enabled', '1') == '1'


def money_enabled() -> bool:
    return _setting('premium_money_enabled', '0') == '1'


def get_price_money() -> int:
    try:
        return int(_setting('premium_price_money', '0') or 0)
    except ValueError:
        return 0


def get_currency() -> str:
    return _setting('premium_currency', '₸')


def get_pay_url() -> str:
    """Куда вести за оплатой деньгами: своя ссылка либо контакт преподавателя."""
    url = _setting('premium_pay_url', '').strip()
    if url:
        return url
    contact = _setting('site_contact_username', '').lstrip('@').strip()
    return f"https://t.me/{contact}" if contact else ""


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

    money = get_price_money()
    cur = get_currency()
    pay_url = get_pay_url()
    price_lines = []
    if stars_enabled():
        price_lines.append(f"⭐️ <b>{stars}</b> Stars")
    if money_enabled() and money:
        price_lines.append(f"💳 <b>{money} {cur}</b>")
    price_str = " или ".join(price_lines) if price_lines else "по договорённости с преподавателем"

    text = (
        f"💎 <b>Премиум-подписка</b>\n\n"
        f"Что даёт:\n"
        f"✅ Доступ ко <b>всем платным тестам</b>\n"
        f"🃏 Режимы <b>Карточки и Заучивание бесплатно</b>\n"
        f"🔁 Бесплатный повтор ошибок\n"
        f"⚡️ Без ограничений\n\n"
        f"💰 Цена: {price_str} на <b>{days} дней</b>{prem_note}"
        if lang == "ru" else
        f"💎 <b>Премиум жазылым</b>\n\n"
        f"Не береді:\n"
        f"✅ Барлық ақылы тесттерге қол жеткізу\n"
        f"🃏 Карточкалар мен Жаттау тегін\n\n"
        f"💰 Бағасы: {price_str} {days} күнге{prem_note}"
    )
    kb = InlineKeyboardBuilder()
    # Выключённый админом способ оплаты не показываем вообще
    if stars_enabled():
        kb.button(text=f"⭐️ Купить за {stars} Stars", callback_data="premium:pay")
    if money_enabled() and pay_url:
        label = f"💳 Оплатить {money} {cur}" if money else "💳 Оплатить деньгами"
        kb.button(text=label, url=pay_url)
    if not stars_enabled() and not money_enabled():
        contact = _setting('site_contact_username', '').lstrip('@').strip()
        if contact:
            kb.button(text="✍️ Написать преподавателю", url=f"https://t.me/{contact}")
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
    if not stars_enabled():
        await call.answer("Оплата звёздами сейчас отключена", show_alert=True)
        return
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
    # ...и в журнал выдачи Премиума: видно, что куплено за звёзды и за сколько.
    # Реферальная запись, если она была секунду назад, здесь не нужна.
    try:
        from utils import log_premium_grant
        db.execute("DELETE FROM premium_grants WHERE user_id=? AND source='referral' "
                   "AND id=(SELECT MAX(id) FROM premium_grants WHERE user_id=? "
                   "AND source='referral')", (user['id'], user['id']))
        exp_row = db.fetchone("SELECT expires_at FROM premium_users WHERE user_id=?",
                              (user['id'],))
        log_premium_grant(user['id'], "stars", days, stars, "⭐", 0,
                          f"оплата в боте, чек {charge}",
                          exp_row['expires_at'] if exp_row else None)
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
    # Конспекты входят в Премиум — отправляем доступ
    try:
        from handlers import conspects as _cons
        await _cons.send_conspect_access(bot, message.chat.id)
    except Exception:
        pass


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
