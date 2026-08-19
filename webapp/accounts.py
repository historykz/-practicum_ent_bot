"""
Вход и регистрация на сайте.

На сайте — вручную: Telegram username и свой пароль. В мини-приложении форм
нет вовсе: аккаунт находится или заводится сам по данным Telegram.

Как только Telegram привязан, одного пароля для входа с сайта мало — нужен
одноразовый код, который приходит владельцу в бота. Так посторонний, даже
зная пароль, внутрь не попадёт.
"""
import asyncio
import logging

import aiohttp_jinja2
from aiohttp import web
from aiohttp_session import get_session

import config
import database as db
from services import account_service as acc
from webapp import auth

log = logging.getLogger(__name__)
PENDING_KEY = "pending_login_user"


def _safe_next(raw: str) -> str:
    nxt = (raw or "").strip()
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else "/learn"


async def _send_code_to_bot(tg_id: int, code: str, purpose: str = "login") -> bool:
    """Отправить код владельцу аккаунта в Telegram."""
    try:
        from aiogram import Bot
        text = (f"🔐 Код для входа в Smart ENT: <b>{code}</b>\n\n"
                f"Код действует {acc.CODE_TTL_MINUTES} минут. "
                f"Никому его не сообщайте.") if purpose == "login" else (
            f"🔑 Код для смены пароля Smart ENT: <b>{code}</b>\n\n"
            f"Код действует {acc.CODE_TTL_MINUTES} минут.")
        bot = Bot(token=config.BOT_TOKEN)
        try:
            await bot.send_message(tg_id, text, parse_mode="HTML")
        finally:
            await bot.session.close()
        return True
    except Exception as e:
        log.warning("Не смог отправить код в Telegram %s: %s", tg_id, e)
        return False


# ===================== регистрация =====================

async def register_page(request: web.Request) -> web.Response:
    if await auth.get_logged_in_tg_id(request) is not None:
        raise web.HTTPFound("/learn")
    ctx = await auth.nav_context(request)
    ctx["next"] = _safe_next(request.query.get("next"))
    return aiohttp_jinja2.render_template("auth_register.html", request, ctx)


async def register_submit(request: web.Request) -> web.Response:
    data = await request.post()
    username = (data.get("username") or "").strip()
    pw = data.get("password") or ""
    pw2 = data.get("password2") or ""
    ctx = await auth.nav_context(request)
    ctx.update({"next": _safe_next(data.get("next")), "username": username})

    if pw != pw2:
        ctx["error"] = "Пароли не совпадают"
        return aiohttp_jinja2.render_template("auth_register.html", request, ctx)

    res = await asyncio.to_thread(acc.register_web, username, pw)
    if not res.get("ok"):
        ctx["error"] = res["error"]
        return aiohttp_jinja2.render_template("auth_register.html", request, ctx)

    user = res["user"]
    await auth.set_logged_in(request, int(user["tg_id"]))
    raise web.HTTPFound(ctx["next"])


# ===================== вход =====================

async def login_page(request: web.Request) -> web.Response:
    if await auth.get_logged_in_tg_id(request) is not None:
        raise web.HTTPFound("/learn")
    ctx = await auth.nav_context(request)
    ctx["next"] = _safe_next(request.query.get("next"))
    return aiohttp_jinja2.render_template("auth_login.html", request, ctx)


async def login_submit(request: web.Request) -> web.Response:
    data = await request.post()
    ctx = await auth.nav_context(request)
    nxt = _safe_next(data.get("next"))
    ctx.update({"next": nxt, "username": (data.get("username") or "").strip()})

    res = await asyncio.to_thread(acc.check_login, ctx["username"],
                                  data.get("password") or "")
    if not res.get("ok"):
        ctx["error"] = res["error"]
        return aiohttp_jinja2.render_template("auth_login.html", request, ctx)

    user = res["user"]
    if not res.get("need_code"):
        await auth.set_logged_in(request, int(user["tg_id"]))
        raise web.HTTPFound(nxt)

    # Telegram привязан — второй шаг с кодом
    wait = await asyncio.to_thread(acc.can_issue_code, user["id"], "login")
    if not wait:
        code = await asyncio.to_thread(acc.issue_code, user["id"], "login")
        sent = await _send_code_to_bot(int(user["tg_id"]), code, "login")
        if not sent:
            ctx["error"] = ("Не получилось отправить код в Telegram. Откройте бота "
                            "и нажмите «Старт», затем попробуйте снова.")
            ctx["bot_username"] = config.WEB_BOT_USERNAME
            return aiohttp_jinja2.render_template("auth_login.html", request, ctx)

    session = await get_session(request)
    session[PENDING_KEY] = user["id"]
    raise web.HTTPFound(f"/login/code?next={nxt}")


async def login_code_page(request: web.Request) -> web.Response:
    session = await get_session(request)
    if not session.get(PENDING_KEY):
        raise web.HTTPFound("/login")
    ctx = await auth.nav_context(request)
    ctx["next"] = _safe_next(request.query.get("next"))
    return aiohttp_jinja2.render_template("auth_code.html", request, ctx)


async def login_code_submit(request: web.Request) -> web.Response:
    session = await get_session(request)
    user_id = session.get(PENDING_KEY)
    if not user_id:
        raise web.HTTPFound("/login")
    data = await request.post()
    ctx = await auth.nav_context(request)
    ctx["next"] = _safe_next(data.get("next"))

    res = await asyncio.to_thread(acc.check_code, int(user_id),
                                  data.get("code") or "", "login")
    if not res.get("ok"):
        ctx["error"] = res["error"]
        return aiohttp_jinja2.render_template("auth_code.html", request, ctx)

    row = await asyncio.to_thread(db.fetchone, "SELECT tg_id FROM users WHERE id=?",
                                  (int(user_id),))
    session.pop(PENDING_KEY, None)
    await auth.set_logged_in(request, int(row["tg_id"]))
    await asyncio.to_thread(acc.log_event, int(user_id), int(row["tg_id"]), "login_ok")
    raise web.HTTPFound(ctx["next"])


async def login_code_resend(request: web.Request) -> web.Response:
    session = await get_session(request)
    user_id = session.get(PENDING_KEY)
    if not user_id:
        raise web.HTTPFound("/login")
    wait = await asyncio.to_thread(acc.can_issue_code, int(user_id), "login")
    if wait:
        raise web.HTTPFound(f"/login/code?message=Новый код можно запросить через {wait} с")
    user = await asyncio.to_thread(db.fetchone, "SELECT * FROM users WHERE id=?",
                                   (int(user_id),))
    code = await asyncio.to_thread(acc.issue_code, int(user_id), "login")
    await _send_code_to_bot(int(user["tg_id"]), code, "login")
    raise web.HTTPFound("/login/code?message=Новый код отправлен в Telegram")


# ===================== забыли пароль =====================

async def forgot_page(request: web.Request) -> web.Response:
    ctx = await auth.nav_context(request)
    return aiohttp_jinja2.render_template("auth_forgot.html", request, ctx)


async def forgot_submit(request: web.Request) -> web.Response:
    data = await request.post()
    username = (data.get("username") or "").strip()
    ctx = await auth.nav_context(request)
    ctx["username"] = username

    user = await asyncio.to_thread(acc.find_by_username, username)
    # Про существование аккаунта не рассказываем — ответ всегда одинаковый
    generic = ("Если такой аккаунт существует и к нему привязан Telegram, "
               "мы отправили код в бота.")
    if not user or not acc.can_reset_via_telegram(user):
        ctx["message"] = generic
        ctx["hint_no_telegram"] = True
        return aiohttp_jinja2.render_template("auth_forgot.html", request, ctx)

    wait = await asyncio.to_thread(acc.can_issue_code, user["id"], "reset")
    if not wait:
        code = await asyncio.to_thread(acc.issue_code, user["id"], "reset")
        await _send_code_to_bot(int(user["tg_id"]), code, "reset")
    session = await get_session(request)
    session["pending_reset_user"] = user["id"]
    raise web.HTTPFound("/forgot/code")


async def forgot_code_page(request: web.Request) -> web.Response:
    session = await get_session(request)
    if not session.get("pending_reset_user"):
        raise web.HTTPFound("/forgot")
    ctx = await auth.nav_context(request)
    return aiohttp_jinja2.render_template("auth_reset.html", request, ctx)


async def forgot_code_submit(request: web.Request) -> web.Response:
    session = await get_session(request)
    user_id = session.get("pending_reset_user")
    if not user_id:
        raise web.HTTPFound("/forgot")
    data = await request.post()
    ctx = await auth.nav_context(request)

    res = await asyncio.to_thread(acc.check_code, int(user_id),
                                  data.get("code") or "", "reset")
    if not res.get("ok"):
        ctx["error"] = res["error"]
        return aiohttp_jinja2.render_template("auth_reset.html", request, ctx)

    pw = (data.get("password") or "").strip()
    if pw:
        if not acc.password_strong_enough(pw):
            ctx["error"] = f"Пароль должен быть не короче {acc.MIN_PASSWORD} символов"
            return aiohttp_jinja2.render_template("auth_reset.html", request, ctx)
        await asyncio.to_thread(acc.set_password, int(user_id), pw)
        ctx["new_password"] = ""
    else:
        ctx["new_password"] = await asyncio.to_thread(acc.reset_to_new_password,
                                                      int(user_id))
    session.pop("pending_reset_user", None)
    ctx["done"] = True
    return aiohttp_jinja2.render_template("auth_reset.html", request, ctx)


# ===================== пароль в профиле =====================

async def profile_new_password(request: web.Request) -> web.Response:
    """Сгенерировать новый пароль и показать его один раз.

    Показать текущий пароль технически нельзя: в базе только хэш. Поэтому
    вместо небезопасного хранения открытого пароля даём выпустить новый.
    """
    tg_id = await auth.get_logged_in_tg_id(request)
    if tg_id is None:
        return web.json_response({"ok": False, "error": "need_login"}, status=403)
    user = await asyncio.to_thread(acc.find_by_tg, tg_id)
    if not user:
        return web.json_response({"ok": False, "error": "no_user"}, status=404)
    pw = await asyncio.to_thread(acc.reset_to_new_password, user["id"])
    return web.json_response({"ok": True, "password": pw,
                              "username": user.get("username") or ""})


async def profile_set_password(request: web.Request) -> web.Response:
    tg_id = await auth.get_logged_in_tg_id(request)
    if tg_id is None:
        return web.json_response({"ok": False, "error": "need_login"}, status=403)
    body = await request.post()
    pw = (body.get("password") or "").strip()
    if not acc.password_strong_enough(pw):
        return web.json_response(
            {"ok": False, "error": f"Не короче {acc.MIN_PASSWORD} символов"}, status=400)
    user = await asyncio.to_thread(acc.find_by_tg, tg_id)
    await asyncio.to_thread(acc.set_password, user["id"], pw)
    raise web.HTTPFound("/cabinet?message=Пароль обновлён")


def register_routes(app):
    app.router.add_get("/register", register_page)
    app.router.add_post("/register", register_submit)
    app.router.add_get("/login", login_page)
    app.router.add_post("/login", login_submit)
    app.router.add_get("/login/code", login_code_page)
    app.router.add_post("/login/code", login_code_submit)
    app.router.add_post("/login/code/resend", login_code_resend)
    app.router.add_get("/forgot", forgot_page)
    app.router.add_post("/forgot", forgot_submit)
    app.router.add_get("/forgot/code", forgot_code_page)
    app.router.add_post("/forgot/code", forgot_code_submit)
    app.router.add_post("/account/new-password", profile_new_password)
    app.router.add_post("/account/set-password", profile_set_password)
    app.router.add_post("/notes/send/{lesson_id:\\d+}", send_note_to_owner)


# ===================== выдача конспекта владельцу аккаунта =====================

async def send_note_to_owner(request: web.Request) -> web.Response:
    """Отправить конспект в Telegram ВЛАДЕЛЬЦА аккаунта.

    Раньше кнопка была обычной ссылкой t.me — и материал уходил тому, кто
    физически нажал, то есть в чужой Telegram, если в аккаунт кто-то вошёл
    с сайта. Теперь адресат берётся из аккаунта: сохранённый Telegram ID.
    """
    tg_id = await auth.get_logged_in_tg_id(request)
    if tg_id is None:
        return web.json_response({"ok": False, "error": "need_login"}, status=403)
    lesson_id = int(request.match_info["lesson_id"])
    user = await asyncio.to_thread(acc.find_by_tg, tg_id)
    if not user:
        return web.json_response({"ok": False, "error": "no_user"}, status=404)

    if not acc.can_reset_via_telegram(user):
        # Telegram к аккаунту ещё не привязан — слать некуда
        return web.json_response({
            "ok": False, "need_bot": True,
            "error": "К аккаунту ещё не привязан Telegram. Откройте бота — "
                     "аккаунт свяжется автоматически, и конспект придёт в Telegram.",
            "bot_url": f"https://t.me/{config.WEB_BOT_USERNAME}"}, status=409)

    owner_tg = int(user["tg_id"])
    try:
        from aiogram import Bot
        from handlers.lesson_notes import send_lesson_note
        bot = Bot(token=config.BOT_TOKEN)
        try:
            sent = await send_lesson_note(bot, owner_tg, owner_tg, lesson_id)
        finally:
            await bot.session.close()
    except Exception as e:
        msg = str(e).lower()
        # Бот не может написать первым, пока человек не открыл диалог —
        # это не поломка аккаунта и не повод что-то менять в базе
        if "forbidden" in msg or "chat not found" in msg or "blocked" in msg:
            return web.json_response({
                "ok": False, "need_bot": True,
                "error": "Бот не может написать вам первым. Откройте бота и "
                         "нажмите «Старт» — после этого конспект придёт сразу.",
                "bot_url": f"https://t.me/{config.WEB_BOT_USERNAME}"}, status=409)
        log.warning("send note to owner: %s", e)
        return web.json_response({"ok": False, "error": "Не получилось отправить"},
                                 status=500)

    await asyncio.to_thread(acc.log_event, user["id"], owner_tg, "note_sent",
                            f"lesson={lesson_id}")
    return web.json_response({"ok": bool(sent),
                              "error": "" if sent else "Урок недоступен"})
