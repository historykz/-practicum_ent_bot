"""
Кнопка «КОНСПЕКТЫ ЕНТ» и автонапоминания о конспектах.

Кнопка открыта всем: Премиум не проверяется, покупка не предлагается.
Текст, подпись кнопки и ссылка берутся из настроек — админ меняет их
в панели, перезапуск не нужен.

Автонапоминание — отдельная кампания со своим текстом и выключателем.
Оно не трогает состояние человека: обычное сообщение, никаких /start,
сбросов теста и смены экрана.
"""
import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import database as db
from services import reminder_service as rs

router = Router(name="notes_promo")
log = logging.getLogger(__name__)


def build_message(key: str):
    """Текст и клавиатура кампании — ровно то, что увидит человек."""
    camp = rs.get_campaign(key)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=camp["button_text"], url=camp["button_url"])
    ]])
    return camp["message_text"], kb


@router.callback_query(F.data == "m:conspects")
async def cb_open_notes(call: CallbackQuery):
    """Главная кнопка «КОНСПЕКТЫ ЕНТ» — без Премиума и без витрины продажи."""
    await call.answer()
    await asyncio.to_thread(rs.touch_activity, call.from_user.id)
    text, kb = await asyncio.to_thread(build_message, rs.MANUAL)
    await call.message.answer(text, reply_markup=kb, parse_mode="HTML",
                              disable_web_page_preview=True)


@router.message(F.text.in_({"/notes_ent", "/konspekty"}))
async def cmd_open_notes(message: Message):
    await asyncio.to_thread(rs.touch_activity, message.from_user.id)
    text, kb = await asyncio.to_thread(build_message, rs.MANUAL)
    await message.answer(text, reply_markup=kb, parse_mode="HTML",
                         disable_web_page_preview=True)


# ===================== рассылка автонапоминаний =====================

async def _busy_now(bot: Bot, tg_id: int, safe_delay: int) -> str:
    """Занят ли человек. Дополнительно смотрим незакрытый шаг сценария."""
    reason = await asyncio.to_thread(rs.busy_reason, tg_id, safe_delay)
    if reason:
        return reason
    try:
        from aiogram.fsm.storage.base import StorageKey
        dp = getattr(bot, "_smartent_dp", None)
        if dp is not None:
            key = StorageKey(bot_id=bot.id, chat_id=tg_id, user_id=tg_id)
            if await dp.storage.get_state(key):
                return "open_state"     # человек в середине формы или мастера
    except Exception:
        pass
    return ""


async def send_reminder(bot: Bot, tg_id: int, campaign: dict) -> str:
    """Одна отправка со всеми проверками. Возвращает итог: sent/причина."""
    # 1) кампания включена, 2) человек есть, 3) не заблокировал бота,
    # 4-5) время пришло — это же условие держит атомарный захват
    if not campaign.get("enabled"):
        return "disabled"
    if not await asyncio.to_thread(
            db.fetchone, "SELECT id FROM users WHERE tg_id=?", (tg_id,)):
        return "no_user"

    safe_delay = campaign.get("safe_delay_seconds") or 0
    reason = await _busy_now(bot, tg_id, safe_delay)
    if reason:
        await asyncio.to_thread(rs.mark_deferred, tg_id, campaign["id"], reason)
        return reason

    # 9) никто другой не отправляет это же сообщение прямо сейчас
    if not await asyncio.to_thread(rs.claim, tg_id, campaign["id"]):
        return "claimed_by_other"

    # пока занимали право — человек мог начать тест; проверяем ещё раз
    reason = await _busy_now(bot, tg_id, safe_delay)
    if reason:
        await asyncio.to_thread(rs.mark_deferred, tg_id, campaign["id"], reason)
        return reason

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=campaign["button_text"], url=campaign["button_url"])]])
    try:
        await bot.send_message(tg_id, campaign["message_text"], reply_markup=kb,
                               parse_mode="HTML", disable_web_page_preview=True)
    except TelegramForbiddenError:
        await asyncio.to_thread(rs.mark_blocked, tg_id, campaign["id"])
        return "bot_blocked"
    except TelegramBadRequest as e:
        msg = str(e).lower()
        if "chat not found" in msg or "user is deactivated" in msg:
            await asyncio.to_thread(rs.mark_blocked, tg_id, campaign["id"], "chat_unavailable")
            return "chat_unavailable"
        await asyncio.to_thread(rs.mark_failed, tg_id, campaign["id"], str(e))
        return "failed"
    except Exception as e:
        await asyncio.to_thread(rs.mark_failed, tg_id, campaign["id"], str(e))
        return "failed"

    await asyncio.to_thread(rs.mark_sent, tg_id, campaign["id"],
                            campaign.get("cooldown_seconds") or 259200)
    return "sent"


BATCH = 25          # сколько человек берём за один заход
PAUSE = 0.35        # пауза между сообщениями, чтобы не упереться в лимиты
TICK = 600          # как часто проверяем очередь


async def reminder_loop(bot: Bot):
    """Фоновая рассылка: маленькими порциями, с проверками перед каждым сообщением."""
    await asyncio.sleep(90)
    while True:
        try:
            camp = await asyncio.to_thread(rs.get_campaign, rs.REMINDER)
            if camp.get("enabled"):
                users = await asyncio.to_thread(rs.due_users, camp["id"], BATCH)
                sent = 0
                for tg_id in users:
                    res = await send_reminder(bot, tg_id, camp)
                    if res == "sent":
                        sent += 1
                        await asyncio.sleep(PAUSE)
                if sent:
                    log.info("Напоминания о конспектах: отправлено %d", sent)
        except Exception as e:
            log.warning("reminder loop: %s", e)
        await asyncio.sleep(TICK)
