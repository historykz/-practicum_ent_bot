"""
Веб-сервер сайта Smart ENT (лендинг + личный кабинет).

Работает в том же процессе, что и Telegram-бот (запускается фоновой
задачей из main.py). Использует те же database.py/services, что и бот —
никакой отдельной копии логики премиума/рефералов/рейтинга.
"""
import asyncio
import hashlib
import logging
import os
import sqlite3
import time
import traceback
import uuid
from pathlib import Path

import aiohttp_jinja2
import jinja2
from aiohttp import web
from aiohttp_session import setup as setup_session
from aiohttp_session.cookie_storage import EncryptedCookieStorage

import config
import utils
from aiohttp_session import get_session
from webapp import auth, queries, learning, modes, zachet, live, mistakes, hub
from webapp import appeals_web, accounts, music, publications

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 дней


def _session_secret_bytes() -> bytes:
    # Раньше без SESSION_SECRET брался СЛУЧАЙНЫЙ ключ на запуск — и после
    # каждого деплоя все посетители оказывались разлогинены. Отсюда и «мигание»
    # между «вошёл» и «войдите», и урок, вдруг требующий вход у того, кто
    # только что зашёл. Запасной ключ выводим из токена бота: он постоянный.
    secret = config.SESSION_SECRET
    if not secret:
        if config.BOT_TOKEN:
            log.warning(
                "SESSION_SECRET не задан — беру постоянный ключ из токена бота. "
                "Сессии переживут перезапуск. Для независимости от токена "
                "задайте SESSION_SECRET в переменных окружения.")
            secret = "smartent-session::" + config.BOT_TOKEN
        else:
            log.error("Нет ни SESSION_SECRET, ни BOT_TOKEN — сессии будут "
                      "слетать при каждом перезапуске.")
            secret = os.urandom(32).hex()
    return hashlib.sha256(secret.encode()).digest()


async def index(request: web.Request) -> web.Response:
    """Главная сразу открывает обучение.

    Приветственной страницы с «Войдите через Telegram» больше нет: человек
    попадает в каталог предметов, а вход и регистрация живут в «Профиле».
    Смотреть каталог можно и не входя.
    """
    nxt = "/learn"
    err = request.query.get("error")
    if err:
        nxt += f"?message={err}"
    raise web.HTTPFound(nxt)


async def telegram_callback(request: web.Request) -> web.Response:
    data = dict(request.query)
    if not auth.verify_telegram_login(data):
        log.warning("Отклонён вход через Telegram: неверная подпись (tg_id=%s)", data.get("id"))
        return web.HTTPFound("/?error=invalid_login")

    try:
        tg_id = int(data["id"])
    except (KeyError, ValueError):
        return web.HTTPFound("/?error=invalid_login")

    user = await asyncio.to_thread(utils.get_user_by_tg, tg_id)
    if not user:
        raise web.HTTPFound("/cabinet")
    if user.get("is_blocked"):
        return web.HTTPFound("/?error=blocked")

    await auth.set_logged_in(request, tg_id)
    next_url = request.query.get("next") or "/cabinet"
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/cabinet"  # защита от открытого редиректа на чужой домен
    return web.HTTPFound(next_url)


async def auth_webapp(request: web.Request) -> web.Response:
    """Автовход/автологин для тех, кто открыл сайт из мини-приложения в Telegram.

    JS в base.html шлёт сюда window.Telegram.WebApp.initData при загрузке
    страницы. В отличие от Login Widget — тут пользователя создаём, если
    его ещё нет (в мини-апп можно попасть и до /start у бота).
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)

    init_data = (body or {}).get("init_data") or ""
    tg_user = auth.verify_telegram_webapp(init_data)
    if not tg_user or not tg_user.get("id"):
        return web.json_response({"ok": False, "error": "invalid_signature"}, status=403)

    tg_id = int(tg_user["id"])
    # Единая точка входа: найдём по Telegram ID, иначе подхватим аккаунт,
    # заведённый на сайте под тем же username, иначе создадим новый.
    # Регистрацию человеку показывать не нужно.
    from services import account_service as _acc
    link = await asyncio.to_thread(
        _acc.link_telegram, tg_id, tg_user.get("username") or "",
        tg_user.get("first_name") or "", tg_user.get("last_name") or "")
    # Пароль для входа с обычного сайта здесь НЕ создаём. Его хэш (PBKDF2,
    # 240 000 раундов) — это ~200 мс процессора на каждого нового человека,
    # и при наплыве новых учеников именно вход становился самым медленным
    # запросом. Пароль появится при первом открытии профиля — там он и
    # показывается, ровно один раз, как и раньше.
    await asyncio.to_thread(
        utils.get_or_create_user,
        tg_id,
        tg_user.get("username"),
        tg_user.get("first_name"),
        tg_user.get("last_name"),
    )
    if await asyncio.to_thread(utils.is_blocked, tg_id):
        return web.json_response({"ok": False, "error": "blocked"}, status=403)
    await auth.set_logged_in(request, tg_id)
    return web.json_response({"ok": True})


async def cabinet(request: web.Request) -> web.Response:
    tg_id = await auth.get_logged_in_tg_id(request)
    if tg_id is None:
        # Гостю показываем сам раздел «Профиль» со входом и регистрацией —
        # выбрасывать его на отдельную страницу незачем.
        ctx = await auth.nav_context(request)
        ctx["next"] = "/cabinet"
        return aiohttp_jinja2.render_template("cabinet_guest.html", request, ctx)

    data = await queries.get_dashboard_data(tg_id)
    if data is None:
        await auth.log_out(request)
        return web.HTTPFound("/?error=no_account")

    data.update(await auth.nav_context(request))
    from services import account_service as _acc
    data["account"] = await asyncio.to_thread(_acc.account_summary, tg_id)
    _sess = await get_session(request)
    fresh = _sess.pop("fresh_password", "")   # показываем один раз
    if not fresh:
        # Аккаунт заведён из мини-приложения без пароля — создаём его сейчас,
        # в профиле, а не на входе (см. auth_webapp). Показываем один раз.
        _u = await asyncio.to_thread(utils.get_user_by_tg, tg_id)
        if _u and not await asyncio.to_thread(_acc.has_password, _u):
            fresh = await asyncio.to_thread(_acc.ensure_password_for_miniapp, _u)
    data["fresh_password"] = fresh
    data.update(await asyncio.to_thread(learning.get_channels_context_sync))
    data.update(await asyncio.to_thread(learning.get_ent_countdown_context_sync))
    return aiohttp_jinja2.render_template("cabinet.html", request, data)


async def logout(request: web.Request) -> web.Response:
    await auth.log_out(request)
    return web.HTTPFound("/")


async def auth_ping(request: web.Request) -> web.Response:
    """Быстрая проверка сессии для защиты контента: при возврате в Mini App
    страница спрашивает — жив ли доступ. Нет сессии → 403 (страница перезагрузится)."""
    tg_id = await auth.get_logged_in_tg_id(request)
    if tg_id is None:
        return web.json_response({"ok": False}, status=403)
    return web.json_response({"ok": True})


def _wants_json(request: web.Request) -> bool:
    """API-запросы получают JSON с ошибкой, страницы — человеческую страницу."""
    p = request.path
    if p.startswith("/api/") or p.startswith("/auth/") or "/api/" in p:
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


async def _session_tg_id_quiet(request: web.Request):
    """tg_id из сессии для логов. Никогда не бросает."""
    try:
        return await auth.get_logged_in_tg_id(request)
    except Exception:
        return None


@web.middleware
async def request_context_middleware(request: web.Request, handler):
    """Каждому запросу — свой id и замер времени.

    По логу потом видно: какой endpoint, сколько занял, кто (tg_id) и какой
    request_id показать пользователю, чтобы найти именно его ошибку.
    """
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    request["request_id"] = rid
    started = time.monotonic()
    request["_t0"] = started      # чтобы обработчик ошибок знал, сколько шёл запрос
    try:
        resp = await handler(request)
    finally:
        took_ms = int((time.monotonic() - started) * 1000)
        request["took_ms"] = took_ms
        # Медленные запросы пишем всегда — это сигнал узкого места
        if took_ms >= 1500:
            log.warning("slow request rid=%s %s %s took=%dms",
                        rid, request.method, request.path, took_ms)
    try:
        resp.headers["X-Request-ID"] = rid
    except Exception:
        pass
    return resp


@web.middleware
async def error_pages_middleware(request: web.Request, handler):
    """Ни одна ошибка не должна доходить до человека сырой страницей aiohttp
    «Server got itself in trouble». Всё логируем с контекстом, наружу —
    понятный текст и правильный HTTP-код.
    """
    try:
        resp = await handler(request)
        # Страницы не должны кэшироваться: встроенный браузер Telegram и Safari
        # держат их очень цепко, и после обновления люди видели старую версию.
        # Мета-тега в HTML для этого мало — нужен настоящий заголовок ответа.
        try:
            ctype = (resp.headers.get("Content-Type") or "")
            if "text/html" in ctype:
                resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                resp.headers["Pragma"] = "no-cache"
                resp.headers["Expires"] = "0"
        except Exception:
            pass
        return resp
    except web.HTTPRequestEntityTooLarge:
        referer = request.headers.get("Referer", "/admin/learn")
        raise web.HTTPFound(
            f"{referer}?message=Файл слишком большой (максимум 20МБ). "
            "Для больших видео используйте ссылку на YouTube."
        )
    except web.HTTPException:
        raise                       # редиректы, 403, 404 — это не ошибки
    except asyncio.CancelledError:
        raise
    except Exception as e:
        rid = request.get("request_id", "-")
        tg_id = await _session_tg_id_quiet(request)
        took = int((time.monotonic() - request.get("_t0", time.monotonic())) * 1000)

        # База занята/повреждена схема — это 503 «попробуйте позже», а не 500
        status = 500
        code = "internal"
        if isinstance(e, sqlite3.OperationalError):
            msg = str(e).lower()
            if "locked" in msg or "busy" in msg:
                status, code = 503, "db_busy"
            elif "no such column" in msg or "no such table" in msg:
                status, code = 503, "schema_outdated"
        elif isinstance(e, sqlite3.IntegrityError):
            status, code = 409, "conflict"

        # В лог — всё, что нужно для разбора, кроме секретов и initData
        log.error(
            "request failed rid=%s %s %s status=%s code=%s tg_id=%s took=%dms "
            "ua=%s err=%s: %s\n%s",
            rid, request.method, request.path_qs[:300], status, code, tg_id, took,
            (request.headers.get("User-Agent") or "")[:80],
            type(e).__name__, str(e)[:300], traceback.format_exc()[-3000:],
        )

        if _wants_json(request):
            resp = web.json_response(
                {"ok": False, "error": code, "request_id": rid}, status=status)
            resp.headers["X-Request-ID"] = rid
            return resp

        titles = {
            "db_busy": "Сервер сейчас перегружен",
            "schema_outdated": "Идёт обновление сервера",
        }
        hints = {
            "db_busy": "Слишком много людей одновременно. Подождите несколько "
                       "секунд и обновите страницу.",
            "schema_outdated": "Обновление ещё применяется. Через минуту всё "
                               "заработает — просто откройте страницу заново.",
        }
        try:
            ctx = {"static_v": static_version(), "logged_in": False,
                   "is_admin": False, "has_private_tests": False,
                   "appeals_pending": 0, "bot_username": config.WEB_BOT_USERNAME,
                   "request_id": rid, "status": status,
                   "title": titles.get(code, "Что-то пошло не так"),
                   "hint": hints.get(code, "Мы уже знаем об ошибке. Обновите "
                                           "страницу — чаще всего помогает.")}
            resp = aiohttp_jinja2.render_template("error.html", request, ctx)
            resp.set_status(status)
            resp.headers["X-Request-ID"] = rid     # код ошибки и в заголовке
            return resp
        except Exception:
            # Даже если шаблон не отрисовался — простой текст, а не пустой экран
            return web.Response(
                status=status, content_type="text/html", charset="utf-8",
                text=(f"<h2>Что-то пошло не так</h2><p>Обновите страницу.</p>"
                      f"<p style='color:#888'>Код: {rid}</p>"))


# Колонки, добавленные миграциями в последних версиях. Если сайт обновили,
# а database.py — нет (или база из бэкапа старее кода), запросы к этим
# колонкам падают на КАЖДОЙ странице. Проверяем при старте и лечим сами.
_CRITICAL_COLUMNS = (
    ("test_attempts", "publication_id"),
    ("attempt_answers", "selected_option_ids"),
    ("live_answers", "option_ids"),
    ("lessons", "workbook_path"),
    ("test_categories", "is_private"),
    ("live_rooms", "auto_advance"),
)


def _schema_guard() -> None:
    """Убедиться, что схема базы соответствует коду; иначе прогнать миграции."""
    import database as _db
    missing = []
    for table, col in _CRITICAL_COLUMNS:
        try:
            cols = {r["name"] for r in _db.fetchall(f"PRAGMA table_info({table})")}
        except Exception:
            cols = set()
        if cols and col not in cols:
            missing.append(f"{table}.{col}")
    if not missing:
        return
    log.warning("Схема базы отстаёт от кода (%s) — применяю миграции", ", ".join(missing))
    try:
        _db.init_db()
    except Exception as e:
        log.error("Не удалось применить миграции: %s", e)


async def health_live(request: web.Request) -> web.Response:
    """Процесс жив и отвечает. Ничего тяжёлого — для проверки платформой."""
    return web.json_response({"ok": True, "status": "live"})


async def health_ready(request: web.Request) -> web.Response:
    """Готов ли сервер обслуживать: база отвечает и схема свежая."""
    import database as _db
    checks = {}
    ok = True
    t0 = time.monotonic()
    try:
        await asyncio.to_thread(_db.fetchone, "SELECT 1 AS one")
        checks["db"] = "ok"
    except Exception as e:
        checks["db"] = f"error: {type(e).__name__}"
        ok = False
    checks["db_ms"] = int((time.monotonic() - t0) * 1000)
    missing = []
    for table, col in _CRITICAL_COLUMNS:
        try:
            cols = {r["name"] for r in await asyncio.to_thread(
                _db.fetchall, f"PRAGMA table_info({table})")}
            if cols and col not in cols:
                missing.append(f"{table}.{col}")
        except Exception:
            pass
    checks["schema"] = "ok" if not missing else "outdated: " + ", ".join(missing)
    if missing:
        ok = False
    return web.json_response({"ok": ok, "status": "ready" if ok else "degraded",
                              "checks": checks}, status=200 if ok else 503)


def static_version() -> str:
    """Отпечаток style.css — подставляется в ссылку как ?v=…

    Webview Telegram держит очень цепкий кэш: после обновления он отдавал
    СТАРЫЙ style.css к НОВОМУ html, и мини-приложение разъезжалось (в Safari
    при этом всё было хорошо — там кэш свежий). Меняется файл — меняется
    адрес, и webview обязан скачать новый.
    """
    try:
        f = BASE_DIR / "static" / "style.css"
        st = f.stat()
        return hashlib.md5(f"{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()[:10]
    except Exception:
        return "0"


def create_app() -> web.Application:
    # Лимит намеренно небольшой: Railway обрывает долгие загрузки по таймауту прокси
    # (не настраивается из кода приложения), поэтому большому файлу лучше сразу
    # чётко отказать, чем зависать на несколько минут и оборваться само по себе.
    app = web.Application(
        client_max_size=20 * 1024 * 1024,  # 20МБ
        middlewares=[error_pages_middleware],
    )

    # Safari и webview Telegram строже к кукам: на https нужна пометка Secure,
    # иначе браузер может её не сохранить — и человек «выпадает» из аккаунта.
    _https = str(getattr(config, "SITE_URL", "") or "").startswith("https://")
    setup_session(app, EncryptedCookieStorage(
        _session_secret_bytes(),
        cookie_name="smartent_session",
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="Lax",
        secure=_https or None,
        path="/",
    ))

    aiohttp_jinja2.setup(
        app, loader=jinja2.FileSystemLoader(str(BASE_DIR / "templates"))
    )

    app.router.add_get("/", index)
    app.router.add_get("/auth/telegram-callback", telegram_callback)
    app.router.add_post("/auth/webapp", auth_webapp)
    app.router.add_get("/auth/ping", auth_ping)
    app.router.add_get("/health", health_live)
    app.router.add_get("/health/live", health_live)
    app.router.add_get("/health/ready", health_ready)
    app.router.add_get("/cabinet", cabinet)
    app.router.add_get("/logout", logout)
    app.router.add_static("/static/", path=str(BASE_DIR / "static"), name="static")

    learning.register_routes(app)
    hub.register_routes(app)
    appeals_web.register_routes(app)
    accounts.register_routes(app)
    modes.register_routes(app)
    zachet.register_routes(app)
    live.register_routes(app)
    mistakes.register_routes(app)
    music.register_routes(app)
    publications.register_routes(app)

    return app


async def start_web_server() -> None:
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.WEB_PORT)
    await site.start()
    log.info("Сайт запущен на порту %s", config.WEB_PORT)
