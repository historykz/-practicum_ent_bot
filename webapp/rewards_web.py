"""
Вознаграждения по предмету на сайте и в мини-приложении.

Ученик: «✨ НАЧАТЬ ОБУЧЕНИЕ ✨», «⚠️ Мои штрафы», «💳 История вознаграждений»,
завершение курса, грамота в PDF. Публичная проверка грамоты по QR-коду.
Админ: включение системы, цены уроков (на весь предмет, на раздел, на урок),
обязательность уроков, проверка грамоты.
"""
import asyncio
import logging
from urllib.parse import quote

import aiohttp_jinja2
from aiohttp import web

import utils
from services import reward_service as rs
from webapp import auth

log = logging.getLogger(__name__)


def _lg():
    from webapp import learning
    return learning


async def _view(request, subject_id: int):
    tg = await _lg()._require_login(request)
    view = await asyncio.to_thread(rs.subject_view, tg, subject_id)
    if not view:
        raise web.HTTPFound(f"/learn/{subject_id}")
    return tg, view


def _subject(subject_id):
    s = rs.subject_row(subject_id) or {}
    return {"id": subject_id, "title": s.get("title") or "Предмет"}


# ───────────────────────── ученик ─────────────────────────

async def start(request: web.Request) -> web.Response:
    tg = await _lg()._require_login(request)
    sid = int(request.match_info["subject_id"])

    def _do():
        u = utils.get_user_by_tg(tg)
        if not u:
            return {"ok": False, "error": "no_user"}
        return rs.start(tg, u["id"], sid)

    res = await asyncio.to_thread(_do)
    if not res.get("ok"):
        raise web.HTTPFound(f"/learn/{sid}?start=no_access")
    raise web.HTTPFound(f"/learn/{sid}?start={res['result']}#start")


async def _history(request: web.Request, mode: str) -> web.Response:
    sid = int(request.match_info["subject_id"])
    tg, view = await _view(request, sid)
    if not view["started"] or not view["enabled"]:
        raise web.HTTPFound(f"/learn/{sid}")
    kinds = ("penalty",) if mode == "penalties" else None
    rows = await asyncio.to_thread(rs.transactions, view["participant"], kinds)
    data = await auth.nav_context(request)
    data.update({"mode": mode, "rows": rows, "reward": view, "subject": _subject(sid)})
    return aiohttp_jinja2.render_template("learn_rewards.html", request, data)


async def penalties_page(request):
    return await _history(request, "penalties")


async def rewards_page(request):
    return await _history(request, "history")


async def complete_page(request: web.Request) -> web.Response:
    sid = int(request.match_info["subject_id"])
    tg, view = await _view(request, sid)
    p = view.get("participant")
    if not view["started"] or not view["enabled"] or not p or p["status"] != "completed":
        raise web.HTTPFound(f"/learn/{sid}")

    def _ctx():
        u = utils.get_user_by_tg(tg) or {}
        summ = rs.completion_summary(p)
        cert = view.get("certificate")
        out = {"summary": summ, "certificate": cert,
               "default_name": " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x),
               "fmt": rs.fmt}
        if cert:
            out["download_url"] = (f"/learn/{sid}/certificate.pdf?t="
                                   + rs.cert_token(cert["serial"], tg))
            out["cert_file"] = f"Грамота_{cert['serial']}.pdf"
        return out

    data = await auth.nav_context(request)
    data.update(await asyncio.to_thread(_ctx))
    data.update({"reward": view, "subject": _subject(sid),
                 "error": request.query.get("error"), "claimed": request.query.get("claimed")})
    return aiohttp_jinja2.render_template("learn_complete.html", request, data)


async def claim(request: web.Request) -> web.Response:
    tg = await _lg()._require_login(request)
    sid = int(request.match_info["subject_id"])
    form = await request.post()

    def _do():
        u = utils.get_user_by_tg(tg)
        if not u:
            return {"ok": False, "error": "no_user"}
        return rs.claim(tg, u["id"], sid, form.get("full_name") or "")

    res = await asyncio.to_thread(_do)
    if not res.get("ok"):
        msg = res.get("message") or {"not_completed": "Курс ещё не завершён.",
                                     "no_access": "Доступ к предмету закончился.",
                                     "disabled": "Система вознаграждений выключена."}.get(
                                         res.get("error"), "Не получилось.")
        raise web.HTTPFound(f"/learn/{sid}/complete?error={quote(msg)}")
    raise web.HTTPFound(f"/learn/{sid}/complete?claimed=1")


async def certificate_pdf(request: web.Request) -> web.Response:
    sid = int(request.match_info["subject_id"])
    token = request.query.get("t") or ""

    def _find(tg):
        u = utils.get_user_by_tg(tg)
        return rs.get_certificate(u["id"], sid) if u else None

    cert = None
    if token:
        try:
            tg_hint = int(token.split(".")[0])
        except (ValueError, IndexError):
            tg_hint = 0
        c = await asyncio.to_thread(_find, tg_hint) if tg_hint else None
        if c and rs.parse_cert_token(c["serial"], token) == tg_hint:
            cert = c
    if cert is None:
        tg = await _lg()._require_login(request)
        cert = await asyncio.to_thread(_find, tg)
    if not cert or cert.get("revoked"):
        raise web.HTTPNotFound(text="Грамота не найдена")

    def _pdf():
        from services import certificate_service as cs
        return cs.render_pdf(cert, verify_url(cert["serial"]))

    body = await asyncio.to_thread(_pdf)
    name = f"Грамота_{cert['serial']}.pdf"
    return web.Response(body=body, headers={
        "Content-Type": "application/pdf",
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
        "Cache-Control": "private, no-store"})


def verify_url(serial: str) -> str:
    return f"{_lg()._site_url_sync()}/verify/{serial}"


async def verify_page(request: web.Request) -> web.Response:
    serial = (request.match_info["serial"] or "").strip()
    certs = await asyncio.to_thread(rs.verify, serial) if not serial.isdigit() else []
    cert = certs[0] if certs else None
    data = await auth.nav_context(request)
    data.update({"serial": serial, "cert": cert, "ok": bool(cert and not cert.get("revoked")),
                 "fmt": rs.fmt, "fmt_date": rs.fmt_date, "admin_mode": False})
    return aiohttp_jinja2.render_template("verify.html", request, data)


# ───────────────────────── админка сайта ─────────────────────────

async def _admin(request):
    return await _lg()._require_admin(request)


def _tiyn(form, key="price") -> int:
    return rs.to_tiyn(form.get(key) or "0")


async def admin_toggle(request):
    await _admin(request)
    sid = int(request.match_info["subject_id"])
    form = await request.post()
    ok, msg = await asyncio.to_thread(rs.set_enabled, sid, form.get("on") == "1")
    raise _lg()._back(form, ("✅ " if ok else "⚠️ ") + msg)


async def admin_settings(request):
    await _admin(request)
    sid = int(request.match_info["subject_id"])
    form = await request.post()
    await asyncio.to_thread(rs.set_pay_prior, sid, form.get("pay_prior") == "on")
    raise _lg()._back(form, "Настройки вознаграждений сохранены")


async def admin_price_all(request):
    await _admin(request)
    sid = int(request.match_info["subject_id"])
    form = await request.post()
    try:
        tiyn = _tiyn(form)
    except (ValueError, ArithmeticError):
        raise _lg()._back(form, "⚠️ Цена — число в тенге, например 70")
    n = await asyncio.to_thread(rs.set_price_all, sid, tiyn)
    raise _lg()._back(form, f"Цена {rs.fmt(tiyn)} установлена на все уроки предмета: {n}")


async def admin_price_section(request):
    await _admin(request)
    sec = int(request.match_info["section_id"])
    form = await request.post()
    try:
        tiyn = _tiyn(form)
    except (ValueError, ArithmeticError):
        raise _lg()._back(form, "⚠️ Цена — число в тенге, например 70")
    n = await asyncio.to_thread(rs.set_price_section, sec, tiyn)
    raise _lg()._back(form, f"Цена {rs.fmt(tiyn)} установлена на уроки раздела: {n}")


async def admin_required_section(request):
    await _admin(request)
    sec = int(request.match_info["section_id"])
    form = await request.post()
    req = form.get("required") == "1"
    n = await asyncio.to_thread(rs.set_required_section, sec, req)
    raise _lg()._back(form, (f"Уроки раздела ({n}) {'обязательны' if req else 'необязательны'} "
                             f"для завершения курса"))


async def admin_lesson_reward(request):
    await _admin(request)
    lid = int(request.match_info["lesson_id"])
    form = await request.post()
    try:
        tiyn = _tiyn(form)
    except (ValueError, ArithmeticError):
        raise _lg()._back(form, "⚠️ Цена — число в тенге, например 70")

    def _save():
        rs.set_price_lesson(lid, tiyn)
        rs.set_required_lesson(lid, form.get("required") == "on")

    await asyncio.to_thread(_save)
    raise _lg()._back(form, f"Урок: вознаграждение {rs.fmt(tiyn)}")


async def admin_verify(request):
    await _admin(request)
    q = (request.query.get("q") or "").strip()
    certs = await asyncio.to_thread(rs.verify, q) if q else []
    data = await auth.nav_context(request)
    data.update({"serial": q, "certs": certs, "cert": certs[0] if len(certs) == 1 else None,
                 "ok": bool(certs) and not all(c.get("revoked") for c in certs),
                 "fmt": rs.fmt, "fmt_date": rs.fmt_date, "admin_mode": True})
    return aiohttp_jinja2.render_template("verify.html", request, data)


def admin_summary(subject_id) -> dict:
    """Блок «Система вознаграждений» на странице предмета в админке."""
    real = rs.real_subject_id(subject_id)
    subj = rs.subject_row(real) or {}
    rows = rs.participants(real)
    users = {}
    if rows:
        ph = ",".join("?" * len(rows))
        for u in rs.db.fetchall(f"SELECT tg_id, username, first_name FROM users WHERE tg_id IN ({ph})",
                                tuple(r["tg_id"] for r in rows)):
            users[u["tg_id"]] = dict(u)
    items = []
    for r in sorted(rows, key=lambda r: (not r["is_active"], r["current_rank"] or 10 ** 9)):
        u = users.get(r["tg_id"], {})
        cert = rs.db.fetchone("SELECT serial FROM reward_certificates WHERE user_id=? AND subject_id=?",
                              (r["user_id"], real))
        items.append({**r, "name": u.get("first_name") or "", "username": u.get("username") or "",
                      "started": rs.fmt_date(r["started_at"]), "completed": rs.fmt_date(r["completed_at"]),
                      "last": rs.fmt_dt(r["last_activity_at"]), "earned_fmt": rs.fmt(r["earned"]),
                      "penalties_fmt": rs.fmt(r["penalties"]), "balance_fmt": rs.fmt(r["balance"]),
                      "status_title": rs.status_title(r),
                      "serial": cert["serial"] if cert else ""})
    return {"enabled": bool(subj.get("rewards_enabled")), "pay_prior": bool(subj.get("reward_pay_prior")),
            "enabled_at": rs.fmt_dt(subj.get("rewards_enabled_at")),
            "priced": rs.priced_lessons_count(real), "lessons": len(rs._lessons(real)),
            "prices": rs.prices(real),
            "participants": items, "active": sum(1 for r in rows if r["is_active"]),
            "completed": sum(1 for r in rows if r["status"] == "completed"),
            "is_copy": real != int(subject_id), "orig_id": real}


def register_routes(app: web.Application) -> None:
    r = app.router
    r.add_post("/learn/{subject_id:\\d+}/start", start)
    r.add_get("/learn/{subject_id:\\d+}/penalties", penalties_page)
    r.add_get("/learn/{subject_id:\\d+}/rewards", rewards_page)
    r.add_get("/learn/{subject_id:\\d+}/complete", complete_page)
    r.add_post("/learn/{subject_id:\\d+}/claim", claim)
    r.add_get("/learn/{subject_id:\\d+}/certificate.pdf", certificate_pdf)
    r.add_get("/verify/{serial}", verify_page)
    r.add_post("/admin/learn/subjects/{subject_id:\\d+}/rewards/toggle", admin_toggle)
    r.add_post("/admin/learn/subjects/{subject_id:\\d+}/rewards/settings", admin_settings)
    r.add_post("/admin/learn/subjects/{subject_id:\\d+}/rewards/price-all", admin_price_all)
    r.add_post("/admin/learn/sections/{section_id:\\d+}/rewards/price", admin_price_section)
    r.add_post("/admin/learn/sections/{section_id:\\d+}/rewards/required", admin_required_section)
    r.add_post("/admin/learn/lessons/{lesson_id:\\d+}/reward", admin_lesson_reward)
    r.add_get("/admin/learn/rewards/verify", admin_verify)
