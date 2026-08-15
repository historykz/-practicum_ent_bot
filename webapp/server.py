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
from pathlib import Path

import aiohttp_jinja2
import jinja2
from aiohttp import web
from aiohttp_session import setup as setup_session
from aiohttp_session.cookie_storage import EncryptedCookieStorage

import config
import utils
from webapp import auth, queries, learning, modes, zachet, live, mistakes, hub
from webapp import appeals_web

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 дней


def _session_secret_bytes() -> bytes:
    secret = config.SESSION_SECRET
    if not secret:
        log.warning(
            "SESSION_SECRET не задан — использую случайный ключ на этот запуск. "
            "После каждого рестарта/деплоя все посетители сайта будут разлогинены. "
            "Задайте SESSION_SECRET в переменных окружения Railway."
        )
        secret = os.urandom(32).hex()
    return hashlib.sha256(secret.encode()).digest()


async def index(request: web.Request) -> web.Response:
    context = await auth.nav_context(request)
    context["error"] = request.query.get("error")
    return aiohttp_jinja2.render_template("landing.html", request, context)


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
        return web.HTTPFound("/?error=no_account")
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
        return web.HTTPFound("/?error=login_required")

    data = await queries.get_dashboard_data(tg_id)
    if data is None:
        await auth.log_out(request)
        return web.HTTPFound("/?error=no_account")

    data.update(await auth.nav_context(request))
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


@web.middleware
async def error_pages_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPRequestEntityTooLarge:
        referer = request.headers.get("Referer", "/admin/learn")
        raise web.HTTPFound(
            f"{referer}?message=Файл слишком большой (максимум 20МБ). "
            "Для больших видео используйте ссылку на YouTube."
        )


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

    setup_session(app, EncryptedCookieStorage(
        _session_secret_bytes(),
        cookie_name="smartent_session",
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="Lax",
    ))

    aiohttp_jinja2.setup(
        app, loader=jinja2.FileSystemLoader(str(BASE_DIR / "templates"))
    )

    app.router.add_get("/", index)
    app.router.add_get("/auth/telegram-callback", telegram_callback)
    app.router.add_post("/auth/webapp", auth_webapp)
    app.router.add_get("/auth/ping", auth_ping)
    app.router.add_get("/cabinet", cabinet)
    app.router.add_get("/logout", logout)
    app.router.add_static("/static/", path=str(BASE_DIR / "static"), name="static")

    learning.register_routes(app)
    hub.register_routes(app)
    appeals_web.register_routes(app)
    modes.register_routes(app)
    zachet.register_routes(app)
    live.register_routes(app)
    mistakes.register_routes(app)

    return app


async def start_web_server() -> None:
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.WEB_PORT)
    await site.start()
    log.info("Сайт запущен на порту %s", config.WEB_PORT)
