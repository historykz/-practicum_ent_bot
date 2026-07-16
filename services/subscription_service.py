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


def get_global_channels() -> list:
    """Все активные глобальные обязательные каналы."""
    rows = db.fetchall(
        "SELECT channel_username FROM required_channels "
        "WHERE is_global=1 AND COALESCE(is_active,1)=1")
    return [r['channel_username'] for r in rows if r.get('channel_username')]


def get_category_channels(category_id: int) -> list:
    """Обязательные каналы конкретного раздела."""
    if not category_id:
        return []
    rows = db.fetchall(
        "SELECT channel_username FROM required_channels "
        "WHERE category_id=? AND COALESCE(is_active,1)=1", (category_id,))
    return [r['channel_username'] for r in rows if r.get('channel_username')]


async def check_all_subscriptions(bot: Bot, user_id: int,
                                    category_id: int = None) -> list:
    """
    Проверяет подписку на ВСЕ обязательные каналы (глобальные + раздела).
    Возвращает список каналов на которые НЕ подписан (пустой = всё ок).
    """
    channels = get_global_channels()
    if category_id:
        channels += get_category_channels(category_id)
    # Убираем дубли
    seen = set()
    unique = []
    for ch in channels:
        key = ch.lstrip('@').lower()
        if key not in seen:
            seen.add(key)
            unique.append(ch)
    not_subscribed = []
    for ch in unique:
        ok = await check_user_subscription(bot, ch, user_id)
        if not ok:
            not_subscribed.append(ch)
    return not_subscribed


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
