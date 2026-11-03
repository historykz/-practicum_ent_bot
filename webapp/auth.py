"""
Проверка входа через Telegram Login Widget и работа с сессией сайта.

Алгоритм проверки подписи — официальный, описан в
https://core.telegram.org/widgets/login#checking-authorization
"""
import asyncio
import contextvars
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
SESSION_STAMP_KEY = "_ts"                 # когда вход продлевали в последний раз
SESSION_REFRESH_SECONDS = 24 * 3600       # продлеваем не чаще раза в сутки
# Откуда пришёл запрос: «miniapp» или «site». Ставится при проверке входа, потоки
# asyncio.to_thread видят значение своего запроса (водяные знаки, журнал корзины).
_SOURCE = contextvars.ContextVar("smartent_source", default="site")


def current_source() -> str:
    return _SOURCE.get()


SESSION_DEVICE_KEY = "dsid"               # номер устройства в user_sessions (v96)
SESSION_RESET_KEY = "ars"                 # счётчик «сбросить сеансы» из админки
SESSION_OWNER_VER = "osv"                 # версия сеансов владельца: «завершить другие сеансы»
SESSION_OWNER_PW = "op"                   # вход по паролю владельца: у него свой срок бездействия
SESSION_OWNER_SEEN = "op_seen"

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


def _session_row(tg_id: int) -> Optional[dict]:
    r = db.fetchone("SELECT is_blocked, admin_session_reset FROM users WHERE tg_id=?", (tg_id,))
    return dict(r) if r else None


async def set_logged_in(request: web.Request, tg_id: int, source: str = "site") -> dict:
    """Войти. Каждый вход — запись об устройстве (services/devices.py) с проверкой лимита.
    {"ok": False, "error": …} — лимит устройств не пустил, вход не выполнен."""
    from services import devices as _dev
    session = await get_session(request)
    request.pop("_auth_tg", None)
    fresh = session.get(SESSION_USER_KEY) != tg_id
    res = {"ok": True}
    old_sid = session.get(SESSION_DEVICE_KEY)
    if not fresh and old_sid and await asyncio.to_thread(_dev.check, old_sid, tg_id, _dev.client_ip(request)) == "ok":
        pass                                   # то же устройство: мини-приложение входит при каждом открытии
    else:
        res = await asyncio.to_thread(_dev.start, tg_id, request.headers.get("User-Agent") or "",
                                      _dev.client_ip(request), source)
        if not res.get("ok"):
            return res
        session[SESSION_DEVICE_KEY] = res["sid"]
        if res.get("warned"):
            asyncio.create_task(_dev.send_alerts())
    session[SESSION_USER_KEY] = tg_id
    session[SESSION_STAMP_KEY] = int(time.time())
    session.pop(SESSION_RESET_KEY, None)
    for key in (SESSION_OWNER_PW, SESSION_OWNER_SEEN, "src", "kicked"):
        session.pop(key, None)
    if source == "miniapp":
        session["src"] = "miniapp"
    if utils.is_owner(tg_id):
        from services import owner_security as _os
        session[SESSION_OWNER_VER] = (await asyncio.to_thread(_os.session_state, tg_id))["ver"]
    if fresh:
        # Новый вход: профиль в админке показывает, с каких устройств заходил человек
        try:
            from services import account_service as _acc
            await asyncio.to_thread(_acc.log_event, None, tg_id, "web_login",
                                    (request.headers.get("User-Agent") or "")[:200])
        except Exception:
            pass
    return res


async def get_logged_in_tg_id(request: web.Request) -> Optional[int]:
    # За один запрос страница спрашивает о входе несколько раз — проверяем один раз
    if "_auth_tg" in request:
        return request["_auth_tg"]
    tg_id = await _logged_in_tg_id(request)
    request["_auth_tg"] = tg_id
    return tg_id


async def _logged_in_tg_id(request: web.Request) -> Optional[int]:
    session = await get_session(request)
    _SOURCE.set("miniapp" if session.get("src") == "miniapp" else "site")
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
    row = await asyncio.to_thread(_session_row, tg_id)
    if row and row["is_blocked"]:
        return None
    # «Сбросить сеансы» из админки. Старые куки получают отметку при первом обращении.
    reset = int((row or {}).get("admin_session_reset") or 0)
    if SESSION_RESET_KEY not in session:
        session[SESSION_RESET_KEY] = reset
    elif session[SESSION_RESET_KEY] != reset:
        for key in (SESSION_USER_KEY, SESSION_RESET_KEY):
            session.pop(key, None)
        return None
    # Устройство: завершённое (админом, учеником или вытесненное новым входом) больше не пускаем
    from services import devices as _dev
    dsid = session.get(SESSION_DEVICE_KEY)
    if dsid:
        state = await asyncio.to_thread(_dev.check, dsid, tg_id, _dev.client_ip(request))
        if state != "ok":
            for key in (SESSION_USER_KEY, SESSION_DEVICE_KEY):
                session.pop(key, None)
            if state == "kicked":
                session["kicked"] = 1
            return None
    else:
        session[SESSION_DEVICE_KEY] = await asyncio.to_thread(
            _dev.adopt, tg_id, request.headers.get("User-Agent") or "", _dev.client_ip(request),
            "miniapp" if session.get("src") == "miniapp" else "site")
    if utils.is_owner(tg_id):
        from services import owner_security as _os
        state = await asyncio.to_thread(_os.session_state, tg_id)
        try:
            stale = int(session.get(SESSION_OWNER_VER) or 1) != state["ver"]
            now = int(time.time())
            if session.get(SESSION_OWNER_PW):
                stale = stale or now - int(session.get(SESSION_OWNER_SEEN) or 0) > state["idle_min"] * 60
                if not stale and now - int(session.get(SESSION_OWNER_SEEN) or 0) > 60:
                    session[SESSION_OWNER_SEEN] = now
        except (TypeError, ValueError):
            stale = True
        if stale:
            for key in (SESSION_USER_KEY, SESSION_OWNER_PW, SESSION_OWNER_SEEN, SESSION_OWNER_VER):
                session.pop(key, None)
            return None
    # Продлеваем вход, пока человек пользуется сайтом. Раньше кука жила ровно
    # 30 дней с момента входа и не обновлялась: активного ученика выбрасывало
    # из аккаунта посреди учёбы — в тесте это выглядело как «ответ не сохраняется».
    try:
        if time.time() - float(session.get(SESSION_STAMP_KEY) or 0) > SESSION_REFRESH_SECONDS:
            session[SESSION_STAMP_KEY] = int(time.time())
    except (TypeError, ValueError):
        session[SESSION_STAMP_KEY] = int(time.time())
    return tg_id


async def log_out(request: web.Request) -> None:
    session = await get_session(request)
    request.pop("_auth_tg", None)
    dsid = session.get(SESSION_DEVICE_KEY)
    if dsid:
        from services import devices as _dev
        row = await asyncio.to_thread(_dev.get, dsid)
        if row:
            await asyncio.to_thread(_dev.end, row["id"], "logout")
    for key in (SESSION_USER_KEY, SESSION_OWNER_PW, SESSION_OWNER_SEEN, SESSION_OWNER_VER, "src", SESSION_DEVICE_KEY):
        session.pop(key, None)


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
    build_issue = ""
    if is_admin:
        try:
            from services import appeal_service as _asvc
            appeals_pending = await asyncio.to_thread(_asvc.count_pending)
        except Exception:
            appeals_pending = 0
        # Плашка админу: на сервере файлы из разных версий (частичная загрузка)
        try:
            from webapp import build_check as _bc
            build_issue = _bc.summary()
        except Exception:
            build_issue = ""
    try:
        from webapp import admin_hub as _hub
        share = await asyncio.to_thread(_hub.share_config_sync)
    except Exception:
        share = None
    return {
        "share": share,
        "static_v": static_version(),
        "appeals_pending": appeals_pending,
        "build_issue": build_issue,
        "bot_username": config.WEB_BOT_USERNAME,
        "logged_in": tg_id is not None,
        "is_admin": is_admin,
        "has_private_tests": has_private_tests,
    }
