"""Скрытый вход владельца и настройки безопасности (v92). Логика — services/owner_security.py."""
import asyncio
import logging
from urllib.parse import quote

import aiohttp_jinja2
from aiohttp import web
from aiohttp_session import get_session

import utils
from services import account_service as acc
from services import owner_security as sec
from services import trash as tr
from webapp import accounts, auth, learning

log = logging.getLogger(__name__)

LOGIN_OK_URL = "/admin"
ERR_PW = "Неверный пароль."
ERR_LOCK = "Слишком много попыток. Попробуйте позже."

MODAL = """<div class="owner-modal" id="ownerModal" role="dialog" aria-modal="true">
  <form class="owner-box" id="ownerForm" autocomplete="off">
    <h2>SMART ЕНТ — Вход владельца</h2>
    <div id="ownerPw"><label for="ownerPass">Пароль администратора</label>
      <input id="ownerPass" type="password" name="password" autocomplete="off" required></div>
    <div id="ownerCode" hidden><label for="ownerCodeIn">Код из Telegram</label>
      <input id="ownerCodeIn" type="text" name="code" inputmode="numeric" autocomplete="one-time-code"></div>
    <div class="owner-err" id="ownerErr"></div>
    <div class="owner-actions"><button class="btn btn-primary" type="submit" id="ownerGo">Войти</button>
      <button class="btn" type="button" id="ownerClose">Отмена</button></div>
  </form>
</div>
<script>
(function () {
  var m = document.getElementById('ownerModal'), f = document.getElementById('ownerForm');
  var err = document.getElementById('ownerErr'), go = document.getElementById('ownerGo');
  var pw = document.getElementById('ownerPass'), code = document.getElementById('ownerCodeIn');
  var step = 'password';
  document.getElementById('ownerClose').onclick = function () { m.parentNode.removeChild(m); };
  pw.focus();
  f.addEventListener('submit', function (e) {
    e.preventDefault(); err.textContent = ''; go.disabled = true;
    var body = new URLSearchParams();
    if (step === 'password') body.set('password', pw.value); else body.set('code', code.value);
    fetch(step === 'password' ? '/owner/login' : '/owner/login/code', { method: 'POST', credentials: 'same-origin', body: body })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.go) { location.href = d.go; return; }
        if (d.need_code) {
          step = 'code'; pw.value = '';
          document.getElementById('ownerPw').hidden = true;
          document.getElementById('ownerCode').hidden = false;
          code.required = true; pw.required = false; code.focus();
        }
        err.textContent = d.error || '';
      })
      .catch(function () { err.textContent = 'Нет связи с сервером.'; })
      .then(function () { go.disabled = false; });
  });
})();
</script>"""


def _ip(request: web.Request) -> str:
    # Railway дописывает настоящий адрес последним; первые значения может подставить кто угодно
    xff = request.headers.get("X-Forwarded-For") or ""
    return (xff.split(",")[-1].strip() if xff else (request.remote or ""))[:64]


def _ua(request: web.Request) -> str:
    return (request.headers.get("User-Agent") or "")[:200]


async def gate(request: web.Request) -> web.Response:
    """Открывает окно входа, если совпала комбинация. Прав не даёт."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({})
    ok = await asyncio.to_thread(sec.gate_matches, (body or {}).get("t"))
    return web.json_response({"html": MODAL} if ok else {})


def _notify_owner(tg_id: int, text: str) -> None:
    async def _send():
        try:
            from aiogram import Bot
            import config
            bot = Bot(token=config.BOT_TOKEN)
            try:
                await bot.send_message(tg_id, text)
            finally:
                await bot.session.close()
        except Exception as e:
            log.warning("уведомление владельцу: %s", e)
    asyncio.create_task(_send())


async def _finish_login(request: web.Request, tg_id: int) -> web.Response:
    await auth.set_logged_in(request, tg_id, source="owner")
    session = await get_session(request)
    import time
    session[auth.SESSION_OWNER_PW] = 1
    session[auth.SESSION_OWNER_SEEN] = int(time.time())
    session.pop("o2", None)
    return web.json_response({"go": LOGIN_OK_URL})


def _fail_sync(tg_id, ip: str, ua: str, reason: str) -> bool:
    """Записать неудачу. True — только что сработала блокировка."""
    was = sec.locked(ip)
    sec.log_attempt(tg_id, ip, ua, False, reason)
    tr.audit(tg_id, "", "site", "login_fail", "", "Вход владельца", None, f"{reason} · {ip}")
    return sec.locked(ip) and not was


async def login(request: web.Request) -> web.Response:
    ip, ua = _ip(request), _ua(request)
    if await asyncio.to_thread(sec.locked, ip):
        await asyncio.to_thread(sec.log_attempt, None, ip, ua, False, "заблокировано")
        return web.json_response({"error": ERR_LOCK}, status=429)
    post = await request.post()
    tg_id = await asyncio.to_thread(sec.find_owner, str(post.get("password") or ""))
    if not tg_id:
        just_locked = await asyncio.to_thread(_fail_sync, None, ip, ua, "неверный пароль")
        if just_locked:
            import config
            for owner in config.ADMIN_IDS:
                _notify_owner(owner, f"⚠️ Smart ENT: подозрительные попытки входа владельца. Адрес: {ip}. "
                                     f"Вход с него закрыт на {sec.FAIL_WINDOW_MIN} минут.")
        return web.json_response({"error": ERR_PW}, status=403)
    if await asyncio.to_thread(sec.twofa_on, tg_id):
        user = await asyncio.to_thread(utils.get_user_by_tg, tg_id)
        wait = await asyncio.to_thread(acc.can_issue_code, user["id"], "owner")
        if not wait:
            code = await asyncio.to_thread(acc.issue_code, user["id"], "owner")
            if not await accounts._send_code_to_bot(tg_id, code):
                return web.json_response({"error": "Не удалось отправить код в Telegram. Попробуйте позже."}, status=503)
        session = await get_session(request)
        import time
        session["o2"] = {"tg": tg_id, "ts": int(time.time())}
        return web.json_response({"need_code": True, "error": f"Подождите {wait} с: код уже отправлен." if wait else ""})
    await asyncio.to_thread(sec.log_attempt, tg_id, ip, ua, True, "пароль")
    await asyncio.to_thread(tr.audit, tg_id, "", "site", "login_ok", "", "Вход владельца", None, ip)
    return await _finish_login(request, tg_id)


async def login_code(request: web.Request) -> web.Response:
    ip, ua = _ip(request), _ua(request)
    if await asyncio.to_thread(sec.locked, ip):
        return web.json_response({"error": ERR_LOCK}, status=429)
    session = await get_session(request)
    pending = session.get("o2") or {}
    import time
    if not pending or time.time() - float(pending.get("ts") or 0) > 600 or not utils.is_owner(pending.get("tg")):
        session.pop("o2", None)
        return web.json_response({"error": "Сначала введите пароль."}, status=403)
    post = await request.post()
    tg_id = int(pending["tg"])
    user = await asyncio.to_thread(utils.get_user_by_tg, tg_id)
    res = await asyncio.to_thread(acc.check_code, user["id"], str(post.get("code") or ""), "owner")
    if not res.get("ok"):
        await asyncio.to_thread(_fail_sync, tg_id, ip, ua, "неверный код")
        return web.json_response({"error": res.get("error") or "Неверный код."}, status=403)
    await asyncio.to_thread(sec.log_attempt, tg_id, ip, ua, True, "пароль и код")
    await asyncio.to_thread(tr.audit, tg_id, "", "site", "login_ok", "", "Вход владельца", None, ip)
    return await _finish_login(request, tg_id)


# ───────────────────────── Настройки → Безопасность ─────────────────────────

def _sec_back(message: str = "", error: str = "") -> web.HTTPFound:
    q = ("message=" + quote(message)) if message else ("error=" + quote(error) if error else "")
    return web.HTTPFound("/admin/security" + ("?" + q if q else ""))


def _ctx_sync(tg_id: int) -> dict:
    return {"has_pw": sec.has_password(tg_id), "twofa": sec.twofa_on(tg_id), "state": sec.session_state(tg_id),
            "gate": sec.gate_config(), "log": [dict(r, ts_local=tr._fmt_local(r["ts"])) for r in sec.history(50)],
            "min_len": sec.MIN_LEN, "reset_ttl": sec.RESET_TTL_MIN}


async def security_page(request: web.Request) -> web.Response:
    tg_id = await learning._require_owner(request)
    ctx = await auth.nav_context(request)
    ctx.update(await asyncio.to_thread(_ctx_sync, tg_id))
    ctx.update({"message": request.query.get("message"), "error": request.query.get("error")})
    return aiohttp_jinja2.render_template("admin_security.html", request, ctx)


async def security_password(request: web.Request) -> web.Response:
    tg_id = await learning._require_owner(request)
    post = await request.post()
    old, new, rep = str(post.get("old") or ""), str(post.get("new") or ""), str(post.get("repeat") or "")
    if await asyncio.to_thread(sec.has_password, tg_id):
        ip = _ip(request)
        if await asyncio.to_thread(sec.locked, ip):
            raise _sec_back(error=ERR_LOCK)
        if not await asyncio.to_thread(sec.check_password, tg_id, old):
            await asyncio.to_thread(_fail_sync, tg_id, ip, _ua(request), "смена пароля: неверный старый")
            raise _sec_back(error="Старый пароль неверный. Пароль не изменён.")
    problem = sec.password_problem(new, rep)
    if problem:
        raise _sec_back(error=problem)
    await asyncio.to_thread(sec.set_password, tg_id, new)
    await asyncio.to_thread(tr.audit, tg_id, "", "site", "password_changed", "", "Пароль администратора", None, _ip(request))
    if post.get("end_others"):
        await _end_others(request, tg_id)
    raise _sec_back(message="Пароль изменён. Старый больше не работает.")


async def _end_others(request: web.Request, tg_id: int) -> None:
    ver = await asyncio.to_thread(sec.end_other_sessions, tg_id)
    session = await get_session(request)
    session[auth.SESSION_OWNER_VER] = ver      # текущий сеанс остаётся
    from services import devices as _dev
    await asyncio.to_thread(_dev.end_all, tg_id, "reset", session.get(auth.SESSION_DEVICE_KEY) or "")
    await asyncio.to_thread(tr.audit, tg_id, "", "site", "sessions_ended", "", "Другие сеансы", None, _ip(request))


async def security_end_sessions(request: web.Request) -> web.Response:
    tg_id = await learning._require_owner(request)
    await _end_others(request, tg_id)
    raise _sec_back(message="Другие сеансы завершены.")


async def security_settings(request: web.Request) -> web.Response:
    tg_id = await learning._require_owner(request)
    post = await request.post()
    what = post.get("what")
    if what == "twofa":
        if not await asyncio.to_thread(sec.check_password, tg_id, str(post.get("password") or "")):
            raise _sec_back(error="Неверный пароль. Настройка не изменена.")
        on = post.get("on") == "1"
        await asyncio.to_thread(sec.set_twofa, tg_id, on)
        msg = "Подтверждение кодом в Telegram включено." if on else "Подтверждение кодом выключено."
    elif what == "idle":
        await asyncio.to_thread(sec.set_idle, tg_id, int(post.get("minutes") or sec.IDLE_DEFAULT))
        msg = "Срок бездействия сохранён."
    elif what == "gate":
        await asyncio.to_thread(sec.set_gate, int(post.get("taps") or sec.GATE_TAPS), int(post.get("seconds") or sec.GATE_SECONDS))
        msg = "Комбинация сохранена."
    else:
        raise web.HTTPBadRequest(text="Неизвестная настройка")
    await asyncio.to_thread(tr.audit, tg_id, "", "site", "settings", "", msg, None, "")
    raise _sec_back(message=msg)


async def reset_page(request: web.Request) -> web.Response:
    token = request.match_info["token"]
    tg = await asyncio.to_thread(sec.reset_owner, token)
    if not tg:
        raise web.HTTPNotFound(text="Ссылка недействительна или устарела.")
    ctx = await auth.nav_context(request)
    ctx.update({"token": token, "min_len": sec.MIN_LEN, "error": request.query.get("error"), "done": False})
    resp = aiohttp_jinja2.render_template("owner_reset.html", request, ctx)
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


async def reset_submit(request: web.Request) -> web.Response:
    token = request.match_info["token"]
    tg = await asyncio.to_thread(sec.reset_owner, token)
    if not tg:
        raise web.HTTPNotFound(text="Ссылка недействительна или устарела.")
    post = await request.post()
    problem = sec.password_problem(str(post.get("new") or ""), str(post.get("repeat") or ""))
    if problem:
        raise web.HTTPFound(f"/owner/reset/{token}?error=" + quote(problem))
    ok = await asyncio.to_thread(sec.use_reset, token, str(post.get("new")))
    if not ok:
        raise web.HTTPNotFound(text="Ссылка недействительна или устарела.")
    await asyncio.to_thread(tr.audit, tg, "", "bot", "password_changed", "", "Пароль сброшен по ссылке из бота", None, _ip(request))
    ctx = await auth.nav_context(request)
    ctx.update({"done": True})
    return aiohttp_jinja2.render_template("owner_reset.html", request, ctx)


def register_routes(app: web.Application) -> None:
    r = app.router
    r.add_post("/owner/gate", gate)
    r.add_post("/owner/login", login)
    r.add_post("/owner/login/code", login_code)
    r.add_get("/owner/reset/{token}", reset_page)
    r.add_post("/owner/reset/{token}", reset_submit)
    r.add_get("/admin/security", security_page)
    r.add_post("/admin/security/password", security_password)
    r.add_post("/admin/security/end-sessions", security_end_sessions)
    r.add_post("/admin/security/settings", security_settings)
