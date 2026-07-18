"""
Проверка входа через Telegram Login Widget и работа с сессией сайта.

Алгоритм проверки подписи — официальный, описан в
https://core.telegram.org/widgets/login#checking-authorization
"""
import asyncio
import hashlib
import hmac
import json
import time
from typing import Optional
from urllib.parse import parse_qsl

from aiohttp import web
from aiohttp_session import get_session

import config
import utils

# Сколько секунд считаем результат виджета свежим (сутки — как рекомендует Telegram)
MAX_AUTH_AGE_SECONDS = 86400

SESSION_USER_KEY = "tg_user_id"

# Поля, которые реально подписывает Telegram Login Widget. Любые другие
# параметры в query string (например ?next=... для возврата на нужную
# страницу после входа) должны игнорироваться при проверке подписи —
# иначе подпись не совпадёт.
_TELEGRAM_AUTH_FIELDS = {
    "id", "first_name", "last_name", "username", "photo_url", "auth_date",
}


def verify_telegram_login(data: dict) -> bool:
    """Проверяет подпись данных от Telegram Login Widget.

    `data` — словарь с полями id, first_name, ..., auth_date, hash
    (как они приходят в query string колбэка). Может содержать и другие,
    не относящиеся к Telegram параметры (например next=...) — они
    игнорируются.
    """
    received_hash = data.get("hash")
    if not received_hash:
        return False

    check_fields = {
        k: v for k, v in data.items()
        if k in _TELEGRAM_AUTH_FIELDS and v is not None
    }
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


def verify_telegram_webapp_init_data(init_data: str) -> Optional[dict]:
    """Проверяет initData, которую Telegram передаёт Mini App (сайт открытый
    прямо внутри Telegram через кнопку бота). Это ДРУГОЙ алгоритм подписи,
    чем у Login Widget — см. https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

    Возвращает распарсенные поля (включая user как dict) если подпись верна,
    иначе None.
    """
    if not init_data:
        return None
    pairs = dict(parse_qsl(init_data, strict_parsing=False))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs.keys()))

    secret_key = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    if time.time() - auth_date > MAX_AUTH_AGE_SECONDS:
        return None

    if "user" in pairs:
        try:
            pairs["user"] = json.loads(pairs["user"])
        except (ValueError, TypeError):
            pairs["user"] = None

    return pairs


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
