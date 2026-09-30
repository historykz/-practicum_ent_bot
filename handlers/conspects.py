"""
📖 Конспекты ЕНТ — продажа доступа к конспектам.

Юзер: меню → Конспекты → фото-примеры + описание + цена →
покупка за Stars ИЛИ у менеджера. После оплаты: премиум + ссылка
на мини-приложение/сайт + видео регистрации.

Админ: фото (3-5), цена, описание, видео, ссылка, дни премиума, вкл/выкл.
Все настройки в таблице settings (key-value), переживают рестарт.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Router, F, Bot
from aiogram.types import (Message, CallbackQuery, LabeledPrice,
                            InlineKeyboardMarkup, InlineKeyboardButton,
                            InputMediaPhoto)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

import config
import database as db
import utils
from filters import IsAdmin, IsOwner

router = Router(name="conspects")
log = logging.getLogger(__name__)
ALMATY = timezone(timedelta(hours=5))

DEFAULT_URL = "https://practicumentbot-production.up.railway.app/"
DEFAULT_MINIAPP = "https://t.me/practicum_ent_bot/practicumentbotproductionupra"
DEFAULT_DESCRIPTION = (
    "Все темы в одном месте — коротко, понятно, с таблицами и схемами. "
    "Готовься быстрее! 🚀")


# ============ НАСТРОЙКИ (settings key-value) ============

def _get(key, default=None):
    try:
        row = db.fetchone("SELECT value FROM settings WHERE key=?", (key,))
        if row and row.get('value') is not None:
            return row['value']
    except Exception:
        pass
    return default


def _set(key, value):
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)",
               (key, str(value)))


def get_url() -> str:
    return _get('conspect_url', DEFAULT_URL) or DEFAULT_URL


def get_miniapp_url() -> str:
    """Ссылка мини-приложения (t.me/бот/приложение) — открывается кнопкой."""
    return _get('conspect_miniapp_url', DEFAULT_MINIAPP) or DEFAULT_MINIAPP


def get_description() -> str:
    return _get('conspect_description', DEFAULT_DESCRIPTION) or DEFAULT_DESCRIPTION


def get_photos() -> list:
    raw = _get('conspect_photos', '[]')
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def get_video() -> str:
    return _get('conspect_video', '') or ''


def is_enabled() -> bool:
    return _get('conspect_enabled', '1') == '1'


def get_manager() -> str:
    return _get('conspect_manager',
                utils.manager_username()) or ''


# ============ ЮЗЕР: КАРТОЧКА КОНСПЕКТОВ ============

def _sale_text(lang: str = 'ru') -> str:
    """Карточка конспектов — вход через покупку Премиума."""
    from handlers import premium as _p
    price = _p.get_premium_price()
    days = _p.get_premium_days()
    return (
        f"📖 <b>КОНСПЕКТЫ ДЛЯ ПОДГОТОВКИ К ЕНТ</b>\n\n"
        f"{get_description()}\n\n"
        f"Конспекты входят в <b>💎 ПРЕМИУМ-подписку</b>!\n\n"
        f"✅ <b>Что даёт Премиум:</b>\n"
        f"📖 Полные конспекты по всем темам\n"
        f"🔓 Все платные тесты бота\n"
        f"🃏 Режимы Карточки и Заучивание\n"
        f"📱 Конспекты — в Telegram или на сайте\n\n"
        f"💰 Цена: <b>{price} ⭐️</b> на <b>{days} дней</b>")


def _sale_kb(lang: str = 'ru') -> InlineKeyboardMarkup:
    from handlers import premium as _p
    price = _p.get_premium_price()
    kb = InlineKeyboardBuilder()
    kb.button(text=f"💎 Купить Премиум за {price} ⭐️",
              callback_data="premium:pay")
    manager = get_manager()
    if manager:
        kb.row(InlineKeyboardButton(
            text="💬 Купить у менеджера",
            url=f"https://t.me/{manager.lstrip('@')}"))
    kb.row(InlineKeyboardButton(text="🔙 Назад", callback_data="m:menu"))
    return kb.as_markup()


# Кнопка «КОНСПЕКТЫ ЕНТ» из главного меню теперь обслуживается в
# handlers/notes_promo.py: она открыта всем и Премиум не проверяет.
# Здесь остаётся прежняя витрина продажи — её открывает админ через
# «📕 Конспекты ЕНТ (продажа)» и кнопку «Посмотреть как юзер».
@router.callback_query(F.data == "conspects:sale_view")
async def cb_conspects(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    is_adm = utils.is_admin(call.from_user.id)
    if not is_enabled() and not is_adm:
        await call.answer("Раздел скоро откроется! 🔜", show_alert=True)
        return
    await call.answer()

    # Уже купил Премиум (или админ) → сразу кнопки открытия конспектов
    has_prem = False
    try:
        has_prem = utils.is_premium(user.get('id'))
    except Exception:
        pass
    if has_prem or is_adm:
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="📖 ОТКРЫТЬ КОНСПЕКТЫ 📖",
                                     url=get_miniapp_url()))
        kb.row(InlineKeyboardButton(text="🌐 Открыть на сайте",
                                     url=get_url()))
        kb.row(InlineKeyboardButton(text="🔙 Назад", callback_data="m:menu"))
        await call.message.answer(
            f"📖 <b>ТВОИ КОНСПЕКТЫ</b>\n\n"
            f"У тебя есть 💎 Премиум — конспекты открыты!\n\n"
            f"Жми кнопку ниже — откроется прямо в Telegram.\n"
            f"Или читай на сайте:\n👉 {get_url()}",
            reply_markup=kb.as_markup(), parse_mode="HTML",
            disable_web_page_preview=True)
        return

    # Нет премиума → витрина с фото и покупкой
    photos = get_photos()
    if photos:
        try:
            media = [InputMediaPhoto(media=fid) for fid in photos[:10]]
            await call.bot.send_media_group(call.message.chat.id, media)
        except Exception as e:
            log.warning("conspect photos: %s", e)
    await call.message.answer(_sale_text(lang), reply_markup=_sale_kb(lang),
                               parse_mode="HTML")


async def send_conspect_access(bot: Bot, chat_id: int):
    """
    Отправить купившему Премиум доступ к конспектам:
    сообщение + WebApp-кнопка + ссылка + видео регистрации.
    Вызывается из premium.handle_premium_payment после оплаты.
    """
    if not is_enabled():
        return
    url = get_url()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 ОТКРЫТЬ КОНСПЕКТЫ 📖",
                               url=get_miniapp_url())],
        [InlineKeyboardButton(text="🌐 Открыть на сайте", url=url)],
    ])
    try:
        await bot.send_message(
            chat_id,
            f"📖 <b>Тебе также открыты КОНСПЕКТЫ!</b>\n\n"
            f"📚 <b>Где читать:</b>\n"
            f"Можешь читать прямо в Telegram (кнопка ниже) "
            f"или на сайте:\n\n"
            f"👉 {url}",
            reply_markup=kb, parse_mode="HTML",
            disable_web_page_preview=True)
    except Exception as e:
        log.warning("conspect access msg: %s", e)
    video = get_video()
    if video:
        try:
            await bot.send_video(
                chat_id, video,
                caption="🎬 Видео: как зарегистрироваться на сайте")
        except Exception as e:
            log.warning("conspect video: %s", e)


# ============ АДМИНКА ============

class ConspectStates(StatesGroup):
    waiting_photos = State()
    waiting_price = State()
    waiting_description = State()
    waiting_video = State()
    waiting_url = State()
    waiting_miniapp = State()
    waiting_days = State()


def _admin_text() -> str:
    return (
        f"📖 <b>УПРАВЛЕНИЕ КОНСПЕКТАМИ</b>\n\n"
        f"Статус: {'🟢 Включено' if is_enabled() else '🔴 Выключено'}\n"
        f"💰 Продаётся через Премиум (цена в настройках Премиума)\n"
        f"🖼 Фото загружено: {len(get_photos())}\n"
        f"🎬 Видео регистрации: {'✅ есть' if get_video() else '❌ нет'}\n"
        f"🔗 Сайт: {get_url()[:42]}…\n"
        f"📱 Мини-апп: {get_miniapp_url()[:42]}…")


def _admin_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🖼 Загрузить фото (3–5)", callback_data="consadm:photos")
    kb.button(text="✏️ Изменить описание", callback_data="consadm:desc")
    kb.button(text="🎬 Загрузить видео", callback_data="consadm:video")
    kb.button(text="🔗 Ссылка сайта", callback_data="consadm:url")
    kb.button(text="📱 Ссылка мини-аппа", callback_data="consadm:miniapp")
    kb.button(text=("🔴 Выключить раздел" if is_enabled()
                    else "🟢 Включить раздел"), callback_data="consadm:toggle")
    kb.button(text="👁 Посмотреть витрину продажи", callback_data="conspects:sale_view")
    kb.button(text="↩️ Назад", callback_data="m:admin")
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(F.data == "adm:conspects", IsOwner())
async def cb_adm_conspects(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer(_admin_text(), reply_markup=_admin_kb(),
                               parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "consadm:toggle", IsAdmin())
async def cb_toggle(call: CallbackQuery):
    _set('conspect_enabled', '0' if is_enabled() else '1')
    await call.answer("Готово")
    await call.message.answer(_admin_text(), reply_markup=_admin_kb(),
                               parse_mode="HTML")


@router.callback_query(F.data == "consadm:photos", IsAdmin())
async def cb_photos(call: CallbackQuery, state: FSMContext):
    await state.set_state(ConspectStates.waiting_photos)
    await state.update_data(conspect_new_photos=[])
    await call.message.answer(
        "🖼 Пришли 3–5 фото (по одному или альбомом).\n"
        "Когда закончишь — напиши <b>готово</b>.\n\n/cancel — отмена",
        parse_mode="HTML")
    await call.answer()


@router.message(ConspectStates.waiting_photos, IsAdmin())
async def msg_photos(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=_admin_kb())
        return
    data = await state.get_data()
    photos = data.get('conspect_new_photos', [])
    if message.photo:
        photos.append(message.photo[-1].file_id)
        await state.update_data(conspect_new_photos=photos)
        await message.answer(f"✅ Фото {len(photos)} принято. Ещё или «готово».")
        return
    if message.text and message.text.strip().lower() in ('готово', 'готов', 'done'):
        if not photos:
            await message.answer("Ты не прислал ни одного фото. Пришли фото или /cancel.")
            return
        _set('conspect_photos', json.dumps(photos[:10]))
        await state.clear()
        await message.answer(f"✅ Сохранено фото: {len(photos[:10])}",
                              reply_markup=_admin_kb())
        return
    await message.answer("Пришли фото, или напиши «готово», или /cancel.")


@router.callback_query(F.data == "consadm:desc", IsAdmin())
async def cb_desc(call: CallbackQuery, state: FSMContext):
    await state.set_state(ConspectStates.waiting_description)
    await call.message.answer(
        "✏️ Пришли новый текст описания (то, что видит ученик):\n\n/cancel")
    await call.answer()


@router.message(ConspectStates.waiting_description, IsAdmin())
async def msg_desc(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=_admin_kb())
        return
    if not message.text:
        await message.answer("Пришли текст.")
        return
    _set('conspect_description', message.text.strip()[:800])
    await state.clear()
    await message.answer("✅ Описание сохранено.", reply_markup=_admin_kb())


@router.callback_query(F.data == "consadm:video", IsAdmin())
async def cb_video(call: CallbackQuery, state: FSMContext):
    await state.set_state(ConspectStates.waiting_video)
    await call.message.answer(
        "🎬 Пришли видео-инструкцию регистрации (как обычное видео):\n\n/cancel")
    await call.answer()


@router.message(ConspectStates.waiting_video, IsAdmin())
async def msg_video(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=_admin_kb())
        return
    fid = None
    if message.video:
        fid = message.video.file_id
    elif message.document and (message.document.mime_type or '').startswith('video'):
        fid = message.document.file_id
    if not fid:
        await message.answer("Это не видео. Пришли видео или /cancel.")
        return
    _set('conspect_video', fid)
    await state.clear()
    await message.answer("✅ Видео сохранено.", reply_markup=_admin_kb())


@router.callback_query(F.data == "consadm:url", IsAdmin())
async def cb_url(call: CallbackQuery, state: FSMContext):
    await state.set_state(ConspectStates.waiting_url)
    await call.message.answer(
        f"🔗 Текущая ссылка:\n{get_url()}\n\nПришли новую (https://…):\n\n/cancel")
    await call.answer()


@router.message(ConspectStates.waiting_url, IsAdmin())
async def msg_url(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=_admin_kb())
        return
    url = (message.text or '').strip()
    if not url.startswith('https://'):
        await message.answer("Ссылка должна начинаться с https:// — попробуй ещё раз.")
        return
    _set('conspect_url', url)
    await state.clear()
    await message.answer("✅ Ссылка сохранена.", reply_markup=_admin_kb())


@router.callback_query(F.data == "consadm:miniapp", IsAdmin())
async def cb_miniapp(call: CallbackQuery, state: FSMContext):
    await state.set_state(ConspectStates.waiting_miniapp)
    await call.message.answer(
        f"📱 Текущая ссылка мини-приложения:\n{get_miniapp_url()}\n\n"
        f"Пришли новую (вида https://t.me/бот/приложение):\n\n/cancel")
    await call.answer()


@router.message(ConspectStates.waiting_miniapp, IsAdmin())
async def msg_miniapp(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=_admin_kb())
        return
    url = (message.text or '').strip()
    if not url.startswith('https://t.me/'):
        await message.answer("Ссылка должна начинаться с https://t.me/ — попробуй ещё раз.")
        return
    _set('conspect_miniapp_url', url)
    await state.clear()
    await message.answer("✅ Ссылка мини-приложения сохранена.",
                          reply_markup=_admin_kb())
