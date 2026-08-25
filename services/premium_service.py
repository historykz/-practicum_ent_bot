"""
Фоновая задача: проверка истёкшего Premium и уведомление пользователей.

Запускается из main.py как asyncio.Task раз в час.
"""
import asyncio
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import config
import database as db

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 3600  # раз в час


def _build_renew_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="💬 Менеджер",
            url=f"https://t.me/{config.MANAGER_USERNAME}",
        )],
        [InlineKeyboardButton(
            text="📚 Каталог тестов",
            callback_data="m:tests",
        )],
    ])


async def _notify_expired_once(bot: Bot) -> int:
    """Один проход проверки. Возвращает кол-во уведомлённых."""
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    rows = db.fetchall(
        """SELECT p.id AS pid, p.user_id, p.expires_at, u.tg_id, u.language
           FROM premium_users p
           JOIN users u ON u.id = p.user_id
           WHERE p.expires_at IS NOT NULL
             AND p.expires_at < ?
             AND p.notified_expired = 0""",
        (now_iso,))

    if not rows:
        return 0

    sent = 0
    for r in rows:
        tg_id = r['tg_id']
        if not tg_id:
            # помечаем как уведомлённого, чтобы не висел
            db.execute("UPDATE premium_users SET notified_expired=1 WHERE id=?", (r['pid'],))
            continue
        try:
            text = (
                "⏰ <b>Ваш Premium закончился</b>\n\n"
                "К сожалению, срок действия вашего Premium-доступа истёк.\n\n"
                "💎 Если хотите продлить подписку — свяжитесь с менеджером.\n"
                f"📩 Менеджер: @{config.MANAGER_USERNAME}\n\n"
                "🆓 Бесплатные тесты по-прежнему доступны в каталоге."
            )
            await bot.send_message(
                tg_id, text,
                reply_markup=_build_renew_kb(),
                parse_mode="HTML")
            db.execute("UPDATE premium_users SET notified_expired=1 WHERE id=?", (r['pid'],))
            sent += 1
        except Exception as e:
            logger.warning("Не удалось уведомить tg_id=%s: %s", tg_id, e)
            # помечаем всё равно, иначе будем спамить попытками
            db.execute("UPDATE premium_users SET notified_expired=1 WHERE id=?", (r['pid'],))
    return sent


async def premium_expiry_loop(bot: Bot):
    """Бесконечный цикл проверки. Запускается в main.py."""
    # Небольшая пауза на старте, чтобы дать боту прогреться
    await asyncio.sleep(60)
    while True:
        try:
            n = await _notify_expired_once(bot)
            if n:
                logger.info("Уведомлено об истечении Premium: %s пользователей", n)
        except Exception as e:
            logger.error("Ошибка в premium_expiry_loop: %s", e)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
