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
import database as db
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


def verify_telegram_webapp(init_data: str) -> Optional[dict]:
    """Проверяет initData из Telegram Mini App (кнопка WebApp внутри бота).

    Алгоритм отличается от Login Widget: секретный ключ считается как
    HMAC-SHA256("WebAppData", bot_token), а не SHA256(bot_token).
    См. https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    Возвращает распарсенный dict пользователя (поле `user` из initData) или None.
    """
    import json
    from urllib.parse import parse_qsl

    if not init_data:
        return None

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
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

    user_raw = pairs.get("user")
    if not user_raw:
        return None
    try:
        return json.loads(user_raw)
    except (ValueError, TypeError):
        return None


async def set_logged_in(request: web.Request, tg_id: int) -> None:
    session = await get_session(request)
    session[SESSION_USER_KEY] = tg_id


async def get_logged_in_tg_id(request: web.Request) -> Optional[int]:
    session = await get_session(request)
    tg_id = session.get(SESSION_USER_KEY)
    if tg_id is None:
        return None
    try:
        tg_id = int(tg_id)
    except (TypeError, ValueError):
        # Испорченное значение в cookie (старая версия, ручная правка) —
        # это «не вошёл», а не 500 на каждой странице
        session.pop(SESSION_USER_KEY, None)
        return None
    # Синхронизация блокировки с ботом: заблокированный админом пользователь
    # (users.is_blocked, та же БД) на сайте считается разлогиненным.
    if await asyncio.to_thread(utils.is_blocked, tg_id):
        return None
    return tg_id


async def log_out(request: web.Request) -> None:
    session = await get_session(request)
    session.pop(SESSION_USER_KEY, None)


def _has_any_private_test_sync(tg_id: int) -> bool:
    row = db.fetchone(
        "SELECT 1 FROM private_test_access pta JOIN tests t ON t.id = pta.test_id "
        "WHERE pta.user_tg_id=? AND t.is_private=1 AND t.status='active' "
        "AND (pta.expires_at IS NULL OR pta.expires_at > ?) LIMIT 1",
        (tg_id, utils.now_iso()),
    )
    return row is not None


async def nav_context(request: web.Request) -> dict:
    """Общие поля для шаблонов: вошёл ли, админ ли — для навигации в base.html."""
    tg_id = await get_logged_in_tg_id(request)
    is_admin = False
    has_private_tests = False
    if tg_id is not None:
        is_admin = await asyncio.to_thread(utils.is_site_admin, tg_id)
        has_private_tests = is_admin or await asyncio.to_thread(_has_any_private_test_sync, tg_id)
    from webapp.server import static_version
    appeals_pending = 0
    if is_admin:
        try:
            from services import appeal_service as _asvc
            appeals_pending = await asyncio.to_thread(_asvc.count_pending)
        except Exception:
            appeals_pending = 0
    return {
        "static_v": static_version(),
        "appeals_pending": appeals_pending,
        "bot_username": config.WEB_BOT_USERNAME,
        "logged_in": tg_id is not None,
        "is_admin": is_admin,
        "has_private_tests": has_private_tests,
    }
