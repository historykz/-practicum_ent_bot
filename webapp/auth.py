"""
Проверка входа через Telegram Login Widget и работа с сессией сайта.

Алгоритм проверки подписи — официальный, описан в
https://core.telegram.org/widgets/login#checking-authorization
"""
import asyncio
import hashlib
import hmac
import time
from typing import Optional

from aiohttp import web
from aiohttp_session import get_session

import config
import utils

# Сколько секунд считаем результат виджета свежим (сутки — как рекомендует Telegram)
MAX_AUTH_AGE_SECONDS = 86400

SESSION_USER_KEY = "tg_user_id"


def verify_telegram_login(data: dict) -> bool:
    """Проверяет подпись данных от Telegram Login Widget.

    `data` — словарь с полями id, first_name, ..., auth_date, hash
    (как они приходят в query string колбэка).
    """
    received_hash = data.get("hash")
    if not received_hash:
        return False

    check_fields = {k: v for k, v in data.items() if k != "hash" and v is not None}
    data_check_string = "\n".join(
        f"{k}={check_fields[k]}" for k in sorted(check_fields.keys())
    )

    secret_key = hashlib.sha256(config.BOT_TOKEN.encode()).digest()
    computed_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return False

    try:
        auth_date = int(check_fields.get("auth_date", "0"))
    except ValueError:
        return False
    if time.time() - auth_date > MAX_AUTH_AGE_SECONDS:
        return False

    return True


async def set_logged_in(request: web.Request, tg_id: int) -> None:
    session = await get_session(request)
    session[SESSION_USER_KEY] = tg_id


async def get_logged_in_tg_id(request: web.Request) -> Optional[int]:
    session = await get_session(request)
    tg_id = session.get(SESSION_USER_KEY)
    return int(tg_id) if tg_id is not None else None


async def log_out(request: web.Request) -> None:
    session = await get_session(request)
    session.pop(SESSION_USER_KEY, None)


async def nav_context(request: web.Request) -> dict:
    """Общие поля для шаблонов: вошёл ли, админ ли — для навигации в base.html."""
    tg_id = await get_logged_in_tg_id(request)
    is_admin = False
    if tg_id is not None:
        is_admin = await asyncio.to_thread(utils.is_admin, tg_id)
    return {
        "bot_username": config.WEB_BOT_USERNAME,
        "logged_in": tg_id is not None,
        "is_admin": is_admin,
    }
