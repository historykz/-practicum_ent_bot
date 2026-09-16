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
    root = rs.root_id(real)
    subj = rs.settings_row(real)                     # настройки — у корня программы
    rows = rs.participants(real)                     # участники всей программы
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
                              (r["user_id"], r["subject_id"]))
        items.append({**r, "name": u.get("first_name") or "", "username": u.get("username") or "",
                      "started": rs.fmt_date(r["started_at"]), "completed": rs.fmt_date(r["completed_at"]),
                      "last": rs.fmt_dt(r["last_activity_at"]), "earned_fmt": rs.fmt(r["earned"]),
                      "penalties_fmt": rs.fmt(r["penalties"]), "balance_fmt": rs.fmt(r["balance"]),
                      "status_title": rs.status_title(r),
                      "money": not int(r.get("money_off") or 0),
                      "serial": cert["serial"] if cert else ""})
    return {"enabled": bool(subj.get("rewards_enabled")), "pay_prior": bool(subj.get("reward_pay_prior")),
            "enabled_at": rs.fmt_dt(subj.get("rewards_enabled_at")),
            "priced": rs.priced_lessons_count(real), "lessons": len(rs._lessons(real)),
            "prices": rs.prices(real),
            "participants": items, "active": sum(1 for r in rows if r["is_active"]),
            "completed": sum(1 for r in rows if r["status"] == "completed"),
            "is_copy": real != int(subject_id), "orig_id": real,
            "root_id": root, "shared_with": (rs.subject_row(root) or {}).get("title") if root != real else None,
            "members": len(rs.group_ids(real)),
            "new_closed": rs.fmt_dt(subj.get("reward_new_closed_at")) if subj.get("reward_new_closed_at") else "",
            "money_off": sum(1 for r in rows if int(r.get("money_off") or 0) and r["is_active"])}


def diagnose_sync(ident: str, subject_id: int) -> dict:
    """Почему ученик видит или не видит рейтинг, кнопку «Начать обучение» и
    деньги на странице предмета — по шагам, с причинами. Только чтение."""
    from services import gamification as gm
    from webapp import learning as lg
    from webapp import shortcuts as sc
    out = {"checks": [], "ident": ident, "subject": None, "user": None}

    def add(ok, title, detail=""):
        out["checks"].append({"ok": ok, "title": title, "detail": detail})

    subj = rs.subject_row(subject_id)
    if not subj:
        add(False, "Предмет не найден", f"id {subject_id}")
        return out
    out["subject"] = subj
    u = utils.find_user_by_arg((ident or "").strip()) if ident else None
    if not u:
        add(False, "Ученик не найден в базе бота",
            "Он ни разу не открывал бота или приложение — либо введён неверный @username / ID. "
            "Пока ученика нет в базе, ему нельзя выдать Премиум и он не увидит ни рейтинга, ни кнопки.")
        return out
    out["user"] = u
    tg, uid = u["tg_id"], u["id"]
    add(True, "Ученик найден", f"{u.get('first_name') or ''} @{u.get('username') or '—'} · ID {tg}")
    ps = utils.premium_status(uid)
    if ps.get("active"):
        add(True, "Премиум действует", "бессрочно" if ps.get("forever") else f"до {ps.get('until_date')}")
    else:
        add(False, "Премиума нет", ("истёк " + str(ps.get("until_date"))) if ps.get("until_date") else
            "строки в premium_users нет — выдайте Премиум в боте или на сайте")
    real = rs.real_subject_id(subject_id)
    group = rs.group_ids(real)
    root = rs.root_id(real)
    sc_mode = sc.subject_mode(subj)
    titles = {r["id"]: r["title"] for r in rs.db.fetchall(
        f"SELECT id, title FROM subjects WHERE id IN ({','.join('?' * len(group))})", tuple(group))}
    add(None, "Предмет", f"«{subj['title']}» · режим: {sc.MODE_TITLES.get(sc_mode, sc_mode)}"
        + (" · продаётся отдельно (Премиум не открывает)" if sc.premium_ignored(subj) else "")
        + (f" · программа: {', '.join(titles.get(g, str(g)) for g in group)} (корень «{titles.get(root, root)}»)"
           if len(group) > 1 else ""))
    live = rs._live_subject_access(tg, group)
    add(True if live else None, "Доступ, выданный именно на предмет (или его копию)",
        "есть, действует" if live else "нет (это нормально, если открывает Премиум)")
    covers = bool(ps.get("active")) and any(
        sc.subject_mode(x) in (sc.OPEN, sc.PREMIUM) and not sc.premium_ignored(x)
        for x in rs._family(real, whole=False))
    elig = rs.eligible(tg, uid, real, here=True)
    if elig:
        add(True, "Может участвовать: рейтинг, кнопка «Начать обучение», вознаграждения",
            "через выданный доступ" if live else "через Премиум")
    else:
        why = []
        if not ps.get("active"):
            why.append("нет Премиума")
        elif not covers:
            why.append("Премиум этот предмет не открывает: режим «"
                       + str(sc.MODE_TITLES.get(sc_mode, sc_mode)) + "»"
                       + (" и/или галочка «Продаётся отдельно»" if sc.premium_ignored(subj) else "")
                       + " — нужен доступ, выданный именно на предмет")
        if not live:
            why.append("доступа на предмет не выдано")
        add(False, "Участвовать НЕ может — блоков на странице не будет", "; ".join(why))
        if rs.eligible(tg, uid, real):
            add(None, "Но есть право на другую копию этой программы",
                "там блоки будут; счёт и рейтинг общие на всю программу")
    ga = gm.has_access(tg, uid, real)
    add(ga, "Блок «Геймификация и рейтинг» показывается", "" if ga else "нет доступа к предмету (см. выше)")
    p = rs.get_participant(uid, real)
    if p:
        add(p["is_active"] == 1, "Нажал «Начать обучение»",
            f"{rs.status_title(p)} · счёт в «{titles.get(p['subject_id'], p['subject_id'])}» · с {rs.fmt_date(p['started_at'])}"
            f" · место {p['current_rank'] or '—'} из {p['rank_total'] or '—'} · баланс {rs.fmt(rs.money(p)['balance'])}")
    else:
        add(False, "Кнопку «Начать обучение» ещё не нажимал",
            "поэтому его нет в рейтинге, а уроки закрыты до нажатия" if elig else "")
    gate = lg._needs_start_sync(real, tg)
    add(not gate, "Уроки открываются", "сначала нужно нажать «Начать обучение» (так и задумано)" if gate else "")
    cfg = rs.settings_row(real)
    priced, total = rs.priced_lessons_count(real), len(rs._lessons(real))
    add(bool(cfg.get("rewards_enabled")), "Система вознаграждений включена (у программы)",
        (f"с {rs.fmt_dt(cfg.get('rewards_enabled_at'))} · " if cfg.get("rewards_enabled") else "")
        + f"уроков с ценой {priced} из {total}" + ("" if priced else " — без цен начислять нечего"))
    closed = cfg.get("reward_new_closed_at")
    if closed:
        mine = (not int(p.get("money_off") or 0)) if (p and p.get("is_active")) else rs.money_allowed(tg, uid, real)
        add(None, f"Денежная акция для новых учеников закрыта с {rs.fmt_dt(closed)}",
            "этот ученик был до закрытия — деньги и штрафы у него идут" if mine else
            "этот ученик новый — рейтинг, геймификация и «Начать обучение» есть, денег и штрафов нет "
            "(так и задумано)")
    if p:
        txs = rs.transactions(p, limit=5)
        add(None, "Последние операции",
            "; ".join(f"{t['amount_fmt']} {t['reason'] or t['type_title']}" for t in txs) or "пока нет")
    try:
        n_mot = rs.db.fetchone("SELECT COUNT(*) AS c FROM motivations")["c"]
    except Exception:
        n_mot = 0
    add(bool(n_mot), "Мотивации загружены",
        f"{n_mot} шт." if n_mot else "файл с мотивациями не загружен (бот → Контроль обучения)")
    view = rs.subject_view(tg, real)
    if view is None:
        add(False, "Что видит ученик на странице предмета", "ни рейтинга, ни кнопки, ни денег")
    else:
        what = ["рейтинг и геймификацию", "кнопку «Начать обучение»" if not view["started"] else "«Продолжить обучение»"]
        if view["started"] and view["enabled"]:
            what.append(f"деньги: баланс {view.get('balance_fmt')}")
        elif not view["enabled"]:
            what.append("денег нет — " + ("денежная акция закрыта для новых учеников"
                                          if view.get("money_closed") else "система выключена"))
        add(True, "Что видит ученик на странице предмета", ", ".join(what))
    return out


async def admin_diagnose(request: web.Request) -> web.Response:
    await _admin(request)
    ident = (request.query.get("u") or "").strip()
    sid = request.query.get("subject") or ""
    sid = int(sid) if sid.isdigit() else 0
    res = await asyncio.to_thread(diagnose_sync, ident, sid) if (ident and sid) else {"checks": [], "ident": ident}
    subjects = await asyncio.to_thread(lambda: [dict(r) for r in rs.db.fetchall(
        "SELECT id, title FROM subjects WHERE status='active' ORDER BY sort_order, id")])
    data = await auth.nav_context(request)
    data.update({"res": res, "ident": ident, "sid": sid, "subjects": subjects})
    return aiohttp_jinja2.render_template("admin_diagnose.html", request, data)


async def admin_new_users(request):
    """Закрыть / открыть денежную акцию для новых учеников программы."""
    await _admin(request)
    sid = int(request.match_info["subject_id"])
    form = await request.post()
    ok, msg = await asyncio.to_thread(rs.set_new_users_closed, sid, form.get("close") == "1")
    raise _lg()._back(form, ("✅ " if ok else "⚠️ ") + msg)


async def admin_new_users_all(request):
    await _admin(request)
    form = await request.post()
    _n, msg = await asyncio.to_thread(rs.set_new_users_closed_all, form.get("close") == "1")
    raise _lg()._back(form, "✅ " + msg)


def _parse_date(value):
    from datetime import date as _date
    try:
        return _date.fromisoformat((value or "").strip()) if value else None
    except ValueError:
        return None


def journal_ctx_sync(q) -> dict:
    sid = int(q.get("subject")) if (q.get("subject") or "").isdigit() else 0
    ident = (q.get("u") or "").strip()
    kind = q.get("type") or ""
    d_from, d_to = _parse_date(q.get("from")), _parse_date(q.get("to"))
    u = utils.find_user_by_arg(ident) if ident else None
    ctx = {"sid": sid, "ident": ident, "kind": kind, "d_from": q.get("from") or "", "d_to": q.get("to") or "",
           "kinds": [(k, v[0]) for k, v in rs.JOURNAL_KINDS.items()], "user": u,
           "not_found": bool(ident and not u), "rows": [], "items": [], "participant": None}
    ctx["subjects"] = [dict(r) for r in rs.db.fetchall(
        "SELECT id, title FROM subjects WHERE status='active' ORDER BY sort_order, id")]
    if ctx["not_found"]:
        return ctx
    rows = rs.journal(sid or None, u["id"] if u else None, kind or None, d_from, d_to)
    ctx["rows"] = rows
    ctx["sum_rewards"] = rs.fmt(sum(r["amount"] for r in rows if r["amount"] > 0))
    ctx["sum_penalties"] = rs.fmt(sum(r["amount"] for r in rows if r["amount"] < 0))
    if u and sid:
        p = rs.get_participant(u["id"], sid)
        if p:
            m = rs.money(p)
            ctx["participant"] = {"status": rs.status_title(p), "started": rs.fmt_dt(p["started_at"]),
                                  "balance": rs.fmt(m["balance"]), "payout": rs.fmt(m["payout"]),
                                  "money": not int(p.get("money_off") or 0),
                                  "run": int(p.get("absence_run") or 0),
                                  "checked": p.get("days_checked_through") or "—"}
        ctx["items"] = rs.items_for(u["id"], sid)
    return ctx


async def admin_journal(request):
    """Журнал наград и штрафов: почему изменился баланс ученика."""
    await _admin(request)
    data = await auth.nav_context(request)
    data.update(await asyncio.to_thread(journal_ctx_sync, request.query))
    return aiohttp_jinja2.render_template("admin_reward_journal.html", request, data)


def register_routes(app: web.Application) -> None:
    r = app.router
    r.add_get("/admin/learn/rewards/journal", admin_journal)
    r.add_post("/admin/learn/rewards/new-users-all", admin_new_users_all)
    r.add_post("/admin/learn/subjects/{subject_id:\\d+}/rewards/new-users", admin_new_users)
    r.add_get("/admin/learn/diagnose", admin_diagnose)
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
