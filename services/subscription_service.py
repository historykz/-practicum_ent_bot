"""
Сервис проверки обязательной подписки на канал.
"""
import logging
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

import database as db

logger = logging.getLogger(__name__)

OK_STATUSES = {"member", "administrator", "creator"}


async def check_user_subscription(bot: Bot, channel: str, user_id: int) -> bool:
    """
    Проверяет, подписан ли пользователь на канал.
    channel - username с @ или без, или числовой id.
    """
    if not channel:
        return True
    chat_id = channel if channel.startswith("@") else (
        channel if channel.lstrip("-").isdigit() else "@" + channel.lstrip("@")
    )
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return getattr(member, "status", None) in OK_STATUSES
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.warning("Subscription check failed for %s/%s: %s", chat_id, user_id, e)
        return False
    except Exception as e:
        logger.exception("Subscription check error: %s", e)
        return False


def get_required_channel_for_test(test_id: int) -> Optional[str]:
    """
    Возвращает канал для проверки подписки.
    Приоритет: канал теста -> глобальный канал.
    """
    row = db.fetchone(
        "SELECT required_channel FROM tests WHERE id=? AND required_subscription=1",
        (test_id,),
    )
    if row and row["required_channel"]:
        return row["required_channel"]
    # Канал, привязанный к тесту через required_channels
    row = db.fetchone(
        "SELECT channel_username FROM required_channels WHERE test_id=? LIMIT 1",
        (test_id,),
    )
    if row:
        return row["channel_username"]
    # Глобальный канал
    row = db.fetchone(
        "SELECT channel_username FROM required_channels WHERE is_global=1 LIMIT 1"
    )
    if row:
        return row["channel_username"]
    return None


def get_required_channel_for_note(note_id: int) -> Optional[str]:
    row = db.fetchone(
        "SELECT channel_username FROM required_channels WHERE note_id=? LIMIT 1",
        (note_id,),
    )
    if row:
        return row["channel_username"]
    row = db.fetchone(
        "SELECT channel_username FROM required_channels WHERE is_global=1 LIMIT 1"
    )
    if row:
        return row["channel_username"]
    return None
