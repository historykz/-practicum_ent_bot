"""Веб-админка владельца (v92): всё в одном месте, на той же базе и тех же сервисах, что и бот."""
import asyncio
import csv
import io
import logging
import re
from urllib.parse import quote

import aiohttp_jinja2
from aiohttp import web

import config
import database as db
import utils
from services import trash as tr
from webapp import auth, learning

log = logging.getLogger(__name__)
PAGE = 50


def _go(path: str, message: str = "", error: str = "") -> web.HTTPFound:
    q = ("message=" + quote(message)) if message else ("error=" + quote(error) if error else "")
    return web.HTTPFound(path + (("&" if "?" in path else "?") + q if q else ""))


def _int(v, default=0) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


async def _render(request, template: str, ctx: dict) -> web.Response:
    base = await auth.nav_context(request)
    base.update(ctx)
    base.setdefault("message", request.query.get("message"))
    base.setdefault("error", request.query.get("error"))
    return aiohttp_jinja2.render_template(template, request, base)


# ───────────────────────── главная панели ─────────────────────────

async def hub(request: web.Request) -> web.Response:
    tg_id = await learning._require_admin(request)
    p = await asyncio.to_thread(tr.perms, tg_id)
    trash_n = await asyncio.to_thread(tr.count_trashed, tg_id) if p.get("trash_view") else 0
    return await _render(request, "admin_hub.html", {
        "owner": utils.is_owner(tg_id), "perms": p, "trash_n": trash_n})


# ───────────────────────── пользователи ─────────────────────────

def _users_sync(q: str, page: int) -> dict:
    q = q.strip()
    where, args = "1=1", []
    if q:
        like = "%" + q.lstrip("@") + "%"
        where = "(username LIKE ? OR first_name LIKE ? OR last_name LIKE ? OR CAST(tg_id AS TEXT) LIKE ? OR phone LIKE ?)"
        args = [like] * 5
    total = db.fetchone(f"SELECT COUNT(*) AS c FROM users WHERE {where}", args)["c"]
    rows = db.fetchall(
        f"SELECT u.id, u.tg_id, u.username, u.first_name, u.last_name, u.is_blocked, u.created_at, u.last_activity_at, "
        f"p.expires_at AS premium_until, p.id AS premium_id FROM users u LEFT JOIN premium_users p ON p.user_id=u.id "
        f"WHERE {where.replace('username', 'u.username').replace('first_name', 'u.first_name').replace('last_name', 'u.last_name').replace('tg_id', 'u.tg_id').replace('phone', 'u.phone')} "
        f"ORDER BY u.id DESC LIMIT ? OFFSET ?", args + [PAGE, max(page, 0) * PAGE])
    return {"rows": [dict(r) for r in rows], "total": total, "pages": (total + PAGE - 1) // PAGE}


async def users_page(request: web.Request) -> web.Response:
    await learning._require_owner(request)
    q = request.query.get("q") or ""
    page = _int(request.query.get("page"))
    data = await asyncio.to_thread(_users_sync, q, page)
    return await _render(request, "admin_users.html", {**data, "q": q, "page": page})


def _user_sync(tg_id: int) -> dict:
    u = utils.get_user_by_tg(tg_id)
    if not u:
        return {}
    uid = u["id"]
    prem = db.fetchone("SELECT * FROM premium_users WHERE user_id=?", (uid,))
    grants = db.fetchall("SELECT * FROM premium_grants WHERE user_id=? ORDER BY id DESC LIMIT 20", (uid,))
    subjects = db.fetchall("SELECT a.subject_id, a.expires_at, a.granted_at, s.title FROM subject_access a "
                           "JOIN subjects s ON s.id=a.subject_id WHERE a.user_tg_id=? ORDER BY a.id DESC", (tg_id,))
    tests = db.fetchall("SELECT a.id, a.test_id, a.expires_at, t.title FROM private_test_access a "
                        "JOIN tests t ON t.id=a.test_id WHERE a.user_tg_id=? ORDER BY a.id DESC LIMIT 100", (tg_id,))
    ia_tasks = db.fetchall("SELECT t.id, t.title FROM interactive_access a JOIN interactive_tasks t ON t.id=a.task_id "
                           "WHERE a.tg_id=? ORDER BY a.id DESC", (tg_id,))
    links = db.fetchall("SELECT k.id, k.test_id, k.created_at, k.expires_at, k.revoked, k.uses, k.last_used_at, t.title "
                        "FROM test_web_links k JOIN tests t ON t.id=k.test_id WHERE k.user_id=? ORDER BY k.id DESC LIMIT 50", (uid,))
    attempts = db.fetchall("SELECT a.id, a.status, a.score, a.correct_answers, a.created_at, a.via, t.title "
                           "FROM test_attempts a JOIN tests t ON t.id=a.test_id WHERE a.user_id=? "
                           "ORDER BY a.id DESC LIMIT 30", (uid,))
    events = db.fetchall("SELECT event, details, created_at FROM auth_events WHERE user_id=? OR tg_id=? "
                         "ORDER BY id DESC LIMIT 50", (uid, tg_id))
    devices = db.fetchall("SELECT details, MAX(created_at) AS last_at, COUNT(*) AS n FROM auth_events "
                          "WHERE tg_id=? AND event='web_login' GROUP BY details ORDER BY last_at DESC LIMIT 20", (tg_id,))
    actions = db.fetchall("SELECT ts, action, title, details, actor_name, actor_tg FROM admin_audit "
                          "WHERE kind='user' AND item_id=? ORDER BY id DESC LIMIT 50", (tg_id,))
    return {"u": u, "premium": dict(prem) if prem else None, "grants": [dict(r) for r in grants],
            "subjects": [dict(r) for r in subjects], "tests": [dict(r) for r in tests],
            "links": [dict(r) for r in links], "ia_tasks": [dict(r) for r in ia_tasks], "attempts": [dict(r) for r in attempts],
            "events": [dict(r) for r in events], "devices": [dict(r) for r in devices],
            "actions": [dict(r) for r in actions],
            "all_subjects": [dict(r) for r in db.fetchall("SELECT id, title FROM subjects ORDER BY sort_order, id")],
            "is_owner_row": utils.is_owner(tg_id)}


async def user_page(request: web.Request) -> web.Response:
    await learning._require_owner(request)
    data = await asyncio.to_thread(_user_sync, int(request.match_info["tg_id"]))
    if not data:
        raise web.HTTPNotFound(text="Пользователь не найден")
    return await _render(request, "admin_user.html", data)


def _user_action_sync(actor: int, tg_id: int, post) -> str:
    u = utils.get_user_by_tg(tg_id)
    if not u:
        raise web.HTTPNotFound(text="Пользователь не найден")
    action = post.get("action")
    msg, note = "", ""
    if action == "block":
        if utils.is_owner(tg_id):
            raise web.HTTPForbidden(text="Владельца заблокировать нельзя.")
        utils.set_blocked(u["id"], True)
        msg = "Пользователь заблокирован."
    elif action == "unblock":
        utils.set_blocked(u["id"], False)
        msg = "Пользователь разблокирован."
    elif action == "reset_sessions":
        db.execute("UPDATE users SET admin_session_reset=COALESCE(admin_session_reset,0)+1 WHERE id=?", (u["id"],))
        msg = "Все сеансы на сайте сброшены."
    elif action == "premium":
        days = _int(post.get("days"))
        utils.grant_premium(u["id"], days, actor)
        note = f"дней: {days or 'бессрочно'}"
        msg = "Premium выдан или продлён."
    elif action == "premium_revoke":
        tr.trash_premium(u["id"], actor, "site")
        msg = tr.MOVED_MSG
    elif action == "subject_grant":
        sid = _int(post.get("subject_id"))
        days = _int(post.get("days"))
        res = learning._grant_subject_access_bulk_sync(sid, [str(tg_id)], days, actor)
        note = f"предмет {sid}, дней: {days or 'бессрочно'}"
        msg = "Доступ к предмету выдан." if res.get("granted") else "Доступ не выдан."
    elif action == "subject_revoke":
        sid = _int(post.get("subject_id"))
        db.execute("DELETE FROM subject_access WHERE subject_id=? AND user_tg_id=?", (sid, tg_id))
        note = f"предмет {sid}"
        msg = "Доступ к предмету снят."
    elif action == "test_revoke":
        db.execute("DELETE FROM private_test_access WHERE id=? AND user_tg_id=?", (_int(post.get("access_id")), tg_id))
        msg = "Доступ к приватному тесту снят."
    elif action == "test_grant":
        tid = _int(post.get("test_id"))
        if not db.fetchone("SELECT 1 FROM tests WHERE id=?", (tid,)):
            return "!Тест с таким номером не найден."
        if not db.fetchone("SELECT 1 FROM private_test_access WHERE test_id=? AND user_tg_id=?", (tid, tg_id)):
            db.execute("INSERT INTO private_test_access (test_id, user_tg_id, granted_by) VALUES (?,?,?)", (tid, tg_id, actor))
        note = f"тест {tid}"
        msg = "Доступ к приватному тесту выдан."
    elif action == "ia_grant":
        tid = _int(post.get("task_id"))
        if not db.fetchone("SELECT 1 FROM interactive_tasks WHERE id=?", (tid,)):
            return "!Задание с таким номером не найдено."
        db.execute("INSERT OR IGNORE INTO interactive_access (task_id, tg_id, granted_by) VALUES (?,?,?)", (tid, tg_id, actor))
        note = f"задание {tid}"
        msg = "Приватный интерактив выдан."
    elif action == "ia_revoke":
        tid = _int(post.get("task_id"))
        db.execute("DELETE FROM interactive_access WHERE task_id=? AND tg_id=?", (tid, tg_id))
        note = f"задание {tid}"
        msg = "Приватный интерактив снят."
    elif action == "link_revoke":
        db.execute("UPDATE test_web_links SET revoked=1 WHERE id=? AND user_id=?", (_int(post.get("link_id")), u["id"]))
        msg = "Личная ссылка отозвана."
    else:
        raise web.HTTPBadRequest(text="Неизвестное действие")
    tr.audit(actor, "", "site", action, "user", u.get("username") or u.get("first_name") or str(tg_id), tg_id, note)
    return msg


async def user_action(request: web.Request) -> web.Response:
    actor = await learning._require_owner(request)
    tg_id = int(request.match_info["tg_id"])
    post = await request.post()
    msg = await asyncio.to_thread(_user_action_sync, actor, tg_id, post)
    if post.get("action") in ("premium", "premium_revoke"):
        try:
            from services import reward_service as _rs
            await asyncio.to_thread(_rs.check_user, tg_id)
        except Exception as e:
            log.warning("reward check_user: %s", e)
    if msg.startswith("!"):
        raise _go(f"/admin/users/{tg_id}", error=msg[1:])
    raise _go(f"/admin/users/{tg_id}", message=msg)


# ───────────────────────── администраторы ─────────────────────────

SITE_PERMS = {"interactive": "Создавать интерактивы", **tr.PERMS}


def _admins_sync() -> dict:
    site = [dict(r) for r in db.fetchall("SELECT * FROM site_admins ORDER BY created_at DESC")]
    for a in site:
        a["subjects"] = [int(x) for x in str(a.get("trash_subjects") or "").replace(" ", "").split(",") if x.isdigit()]
    bot = db.fetchall("SELECT a.tg_id, a.level, a.created_at, u.username, u.first_name FROM admins a "
                      "LEFT JOIN users u ON u.tg_id=a.tg_id ORDER BY a.id DESC")
    hist = db.fetchall("SELECT * FROM admin_audit WHERE kind='admin' ORDER BY id DESC LIMIT 100")
    return {"site": site, "bot": [dict(r) for r in bot], "perm_names": SITE_PERMS,
            "history": [dict(r, ts_local=tr._fmt_local(r["ts"])) for r in hist],
            "all_subjects": [dict(r) for r in db.fetchall("SELECT id, title FROM subjects ORDER BY sort_order, id")],
            "owners": list(config.ADMIN_IDS)}


async def admins_page(request: web.Request) -> web.Response:
    await learning._require_owner(request)
    return await _render(request, "admin_admins.html", await asyncio.to_thread(_admins_sync))


def _admins_action_sync(actor: int, post) -> tuple:
    action = post.get("action")
    target = utils.find_user_by_arg(str(post.get("user") or "")) if post.get("user") else None
    tg = target["tg_id"] if target else _int(post.get("tg_id"))
    if action in ("site_add", "bot_add") and not target:
        return "", "Пользователь не найден: он должен хотя бы раз запустить бота."
    title = (target.get("username") if target else "") or str(tg)
    if action == "site_add":
        db.execute("INSERT INTO site_admins (tg_id, username, granted_by) VALUES (?,?,?) "
                   "ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username", (tg, target.get("username"), actor))
        msg = "Админ сайта добавлен."
    elif action == "site_remove":
        db.execute("DELETE FROM site_admins WHERE tg_id=?", (tg,))
        msg = "Админ сайта убран."
    elif action == "site_perms":
        sets, args = [], []
        for k in SITE_PERMS:
            sets.append(f"perm_{k}=?")
            args.append(1 if post.get("perm_" + k) else 0)
        subj = ",".join(str(_int(v)) for v in post.getall("subjects", []) if str(v).isdigit())
        db.execute(f"UPDATE site_admins SET {', '.join(sets)}, trash_subjects=? WHERE tg_id=?", args + [subj, tg])
        msg = "Права сохранены."
    elif action == "bot_add":
        lvl = max(1, min(_int(post.get("level"), 1), 2))
        db.execute("INSERT INTO admins (tg_id, granted_by, level) VALUES (?,?,?) "
                   "ON CONFLICT(tg_id) DO UPDATE SET level=excluded.level", (tg, actor, lvl))
        msg = "Админ бота добавлен."
    elif action == "bot_level":
        db.execute("UPDATE admins SET level=? WHERE tg_id=?", (max(1, min(_int(post.get("level"), 1), 2)), tg))
        msg = "Уровень изменён."
    elif action == "bot_remove":
        db.execute("DELETE FROM admins WHERE tg_id=?", (tg,))
        msg = "Админ бота убран."
    else:
        raise web.HTTPBadRequest(text="Неизвестное действие")
    tr.audit(actor, "", "site", action, "admin", title, tg, msg)
    return msg, ""


async def admins_action(request: web.Request) -> web.Response:
    actor = await learning._require_owner(request)
    post = await request.post()
    msg, err = await asyncio.to_thread(_admins_action_sync, actor, post)
    raise _go("/admin/admins", msg, err)


# ───────────────────────── разделы каталога бота ─────────────────────────

def _cats_sync() -> list:
    return [dict(r) for r in db.fetchall(
        "SELECT c.*, (SELECT COUNT(*) FROM tests t WHERE t.category_id=c.id) AS tests_n "
        "FROM test_categories c ORDER BY c.sort_order, c.id")]


async def categories_page(request: web.Request) -> web.Response:
    await learning._require_owner(request)
    return await _render(request, "admin_categories.html", {"cats": await asyncio.to_thread(_cats_sync)})


def _cat_action_sync(actor: int, post) -> str:
    action, cid = post.get("action"), _int(post.get("id"))
    name = str(post.get("name") or "").strip()[:100]
    emoji = str(post.get("emoji") or "").strip()[:8]
    if action == "create":
        if not name:
            return "!Укажите название."
        db.execute("INSERT INTO test_categories (name, emoji, created_by) VALUES (?,?,?)", (name, emoji, actor))
        msg = "Раздел создан."
    elif action == "save":
        db.execute("UPDATE test_categories SET name=?, emoji=? WHERE id=?", (name, emoji, cid))
        msg = "Сохранено."
    elif action == "delete":
        tr.trash_category(cid, actor, "site")
        return tr.MOVED_MSG
    else:
        raise web.HTTPBadRequest(text="Неизвестное действие")
    tr.audit(actor, "", "site", action, "category", name, cid, msg)
    return msg


async def categories_action(request: web.Request) -> web.Response:
    actor = await learning._require_owner(request)
    post = await request.post()
    msg = await asyncio.to_thread(_cat_action_sync, actor, post)
    raise _go("/admin/categories", error=msg[1:]) if msg.startswith("!") else _go("/admin/categories", message=msg)


# ───────────────────────── все тесты (и созданные в боте) ─────────────────────────

def _tests_sync(q: str, page: int) -> dict:
    where, args = "COALESCE(t.status,'')<>'system'", []
    if q.strip():
        where += " AND (t.title LIKE ? OR CAST(t.id AS TEXT)=?)"
        args = [f"%{q.strip()}%", q.strip()]
    total = db.fetchone(f"SELECT COUNT(*) AS c FROM tests t WHERE {where}", args)["c"]
    rows = db.fetchall(
        f"SELECT t.id, t.title, t.status, t.is_private, t.source, c.name AS category, "
        f"(SELECT COUNT(*) FROM questions q WHERE q.test_id=t.id) AS questions, "
        f"(SELECT COUNT(*) FROM test_attempts a WHERE a.test_id=t.id AND a.status='finished') AS passed, "
        f"(SELECT l.id FROM lessons l WHERE l.test_id=t.id LIMIT 1) AS lesson_id "
        f"FROM tests t LEFT JOIN test_categories c ON c.id=t.category_id WHERE {where} "
        f"ORDER BY t.id DESC LIMIT ? OFFSET ?", args + [PAGE, max(page, 0) * PAGE])
    return {"rows": [dict(r) for r in rows], "total": total, "pages": (total + PAGE - 1) // PAGE}


async def all_tests_page(request: web.Request) -> web.Response:
    await learning._require_admin(request)
    q = request.query.get("q") or ""
    page = _int(request.query.get("page"))
    return await _render(request, "admin_all_tests.html", {**await asyncio.to_thread(_tests_sync, q, page), "q": q, "page": page})


async def all_tests_delete(request: web.Request) -> web.Response:
    actor, source = await learning._delete_actor(request)
    await asyncio.to_thread(tr.trash_test, int(request.match_info["test_id"]), actor, source)
    raise _go("/admin/all-tests", message=tr.MOVED_MSG)


# ───────────────────────── аналитика ─────────────────────────

def _stats_sync() -> dict:
    from services import stats_service
    try:
        text = stats_service.build_stats_text()
    except Exception as e:
        log.exception("stats: %s", e)
        text = "Статистика сейчас недоступна."
    return {"text": re.sub(r"</?(b|i|u|code|pre|a)\b[^>]*>", "", text)}


async def stats_page(request: web.Request) -> web.Response:
    await learning._require_owner(request)
    return await _render(request, "admin_stats.html", await asyncio.to_thread(_stats_sync))


# ───────────────────────── настройки бота ─────────────────────────

def _bot_settings_sync() -> dict:
    get = lambda k: (db.fetchone("SELECT value FROM settings WHERE key=?", (k,)) or {"value": ""})["value"] or ""
    return {"maintenance": get("maintenance_mode") == "1", "maintenance_text": get("maintenance_text")}


async def bot_settings_page(request: web.Request) -> web.Response:
    await learning._require_owner(request)
    return await _render(request, "admin_bot_settings.html", await asyncio.to_thread(_bot_settings_sync))


def _save_bot_settings_sync(actor: int, post) -> None:
    on = "1" if post.get("maintenance") else "0"
    text = str(post.get("maintenance_text") or "").strip()[:1000]
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('maintenance_mode', ?)", (on,))
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('maintenance_text', ?)", (text,))
    tr.audit(actor, "", "site", "settings", "", "Режим обслуживания: " + ("включён" if on == "1" else "выключен"))


async def bot_settings_save(request: web.Request) -> web.Response:
    actor = await learning._require_owner(request)
    post = await request.post()
    await asyncio.to_thread(_save_bot_settings_sync, actor, post)
    raise _go("/admin/bot-settings", message="Сохранено.")


# ───────────────────────── уведомления ─────────────────────────

def _broadcast_sync() -> dict:
    from services import broadcast_service as bs
    return {"aud": bs.audience(), "recent": bs.recent(15), "active": bs.active()}


async def broadcast_page(request: web.Request) -> web.Response:
    await learning._require_owner(request)
    return await _render(request, "admin_broadcast.html", await asyncio.to_thread(_broadcast_sync))


def _new_bot():
    from aiogram import Bot
    return Bot(token=config.BOT_TOKEN)


async def broadcast_send(request: web.Request) -> web.Response:
    """Текст с сайта кладётся в чат владельца с ботом, дальше обычная рассылка бота."""
    actor = await learning._require_owner(request)
    post = await request.post()
    text = str(post.get("text") or "").strip()
    if not text or post.get("confirm") != "1":
        raise _go("/admin/broadcast", error="Нужен текст и отметка подтверждения.")
    if len(text) > 4000:
        raise _go("/admin/broadcast", error="Слишком длинный текст: не больше 4000 символов.")
    from services import broadcast_service as bs
    bot = _new_bot()
    try:
        sent = await bot.send_message(actor, text)
    except Exception as e:
        log.warning("рассылка с сайта: %s", e)
        await bot.session.close()
        raise _go("/admin/broadcast", error="Бот не смог написать вам. Откройте чат с ботом и нажмите «Старт».")
    bid = await asyncio.to_thread(bs.create, actor, sent.chat.id, sent.message_id, "text", text)
    try:
        total = await asyncio.to_thread(bs.start, bid)
    except bs.BusyError:
        await bot.session.close()
        raise _go("/admin/broadcast", error="Сейчас уже идёт другая рассылка.")
    bs.launch(bot, bid)
    await asyncio.to_thread(tr.audit, actor, "", "site", "broadcast", "", text[:100], bid, f"получателей: {total}")
    raise _go("/admin/broadcast", message=f"Рассылка запущена. Получателей: {total}.")


# ───────────────────────── выгрузки ─────────────────────────

def export_users_sync() -> bytes:
    rows = db.fetchall("SELECT tg_id, username, first_name, language, school, city, is_blocked, created_at, "
                       "last_activity_at FROM users ORDER BY id")
    return _csv(["tg_id", "username", "first_name", "language", "school", "city", "is_blocked", "created_at", "last_activity_at"], rows)


def export_results_sync() -> bytes:
    rows = db.fetchall(
        "SELECT u.tg_id, u.username, t.id AS test_id, t.title, a.status, a.correct_answers, a.wrong_answers, a.score, "
        "a.via, a.created_at FROM test_attempts a JOIN users u ON u.id=a.user_id JOIN tests t ON t.id=a.test_id "
        "WHERE a.cloned_from_attempt_id IS NULL ORDER BY a.id DESC LIMIT 200000")
    return _csv(["tg_id", "username", "test_id", "title", "status", "correct", "wrong", "score", "via", "created_at"], rows)


def _safe_cell(v):
    # Excel не должен принять значение из базы за формулу
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
        return "'" + v
    return v


def _csv(head: list, rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(head)
    for r in rows:
        w.writerow([_safe_cell(x) for x in tuple(r.values() if isinstance(r, dict) else r)])
    return buf.getvalue().encode("utf-8-sig")


async def export_file(request: web.Request) -> web.Response:
    actor = await learning._require_owner(request)
    what = request.match_info["what"]
    fn = {"users": export_users_sync, "results": export_results_sync}.get(what)
    if not fn:
        raise web.HTTPNotFound()
    data = await asyncio.to_thread(fn)
    await asyncio.to_thread(tr.audit, actor, "", "site", "export", "", f"Выгрузка: {what}")
    return web.Response(body=data, content_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="{what}.csv"'})


def register_routes(app: web.Application) -> None:
    r = app.router
    r.add_get("/admin", hub)
    r.add_get("/admin/users", users_page)
    r.add_get("/admin/users/{tg_id:\\d+}", user_page)
    r.add_post("/admin/users/{tg_id:\\d+}/action", user_action)
    r.add_get("/admin/admins", admins_page)
    r.add_post("/admin/admins/action", admins_action)
    r.add_get("/admin/categories", categories_page)
    r.add_post("/admin/categories/action", categories_action)
    r.add_get("/admin/all-tests", all_tests_page)
    r.add_post("/admin/all-tests/{test_id:\\d+}/delete", all_tests_delete)
    r.add_get("/admin/stats", stats_page)
    r.add_get("/admin/bot-settings", bot_settings_page)
    r.add_post("/admin/bot-settings", bot_settings_save)
    r.add_get("/admin/broadcast", broadcast_page)
    r.add_post("/admin/broadcast/send", broadcast_send)
    r.add_get("/admin/export/{what}.csv", export_file)
