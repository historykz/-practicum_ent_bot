"""
Апелляции на сайте: ученик жалуется на вопрос, админ разбирает.

В боте апелляции были давно, на сайте их не хватало: человек проходит тест
в мини-приложении, видит спорный вопрос — и уходить ради жалобы в переписку
с ботом неудобно. Кнопка теперь есть под каждым вопросом, а разбор копится
на отдельной странице админки со счётчиком непрочитанных.
"""
import asyncio

import aiohttp_jinja2
from aiohttp import web

import database as db
import utils
from services import appeal_service as svc
from webapp import auth


async def create_appeal(request: web.Request) -> web.Response:
    """Ученик отправляет апелляцию на вопрос теста."""
    tg_id = await auth.get_logged_in_tg_id(request)
    if tg_id is None:
        return web.json_response({"ok": False, "error": "need_login"}, status=403)
    try:
        body = await request.json()
        question_id = int(body.get("question_id") or 0)
        text = (body.get("text") or "").strip()
    except Exception:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)

    if not question_id or len(text) < 5:
        return web.json_response(
            {"ok": False, "error": "Опишите, что не так с вопросом — хотя бы пару слов."},
            status=400)

    def _save():
        user = utils.get_user_by_tg(tg_id)
        if user:
            banned, until = svc.is_user_banned(user["id"])
            if banned:
                return {"ok": False,
                        "error": f"Отправка апелляций заблокирована до {until}."}
        if not db.fetchone("SELECT id FROM questions WHERE id=?", (question_id,)):
            return {"ok": False, "error": "Вопрос не найден."}
        # Один и тот же вопрос второй раз не принимаем — админ и так его увидит
        dup = db.fetchone(
            "SELECT id FROM appeals WHERE question_id=? AND user_tg_id=? "
            "AND status='pending'", (question_id, tg_id))
        if dup:
            return {"ok": False,
                    "error": "Вы уже отправляли апелляцию на этот вопрос — она на рассмотрении."}
        svc.create_appeal(question_id, tg_id, text)
        return {"ok": True}

    res = await asyncio.to_thread(_save)
    return web.json_response(res, status=200 if res.get("ok") else 400)


def _appeals_sync(status: str = "pending", limit: int = 200) -> dict:
    where = "WHERE a.status=?" if status != "all" else ""
    args = (status,) if status != "all" else ()
    rows = db.fetchall(
        f"""SELECT a.*, q.text AS qtext, q.serial_no, t.title AS test_title,
                   u.username, u.first_name, u.last_name,
                   (SELECT text FROM question_options
                     WHERE question_id=q.id AND is_correct=1 LIMIT 1) AS correct_ans,
                   l.id AS lesson_id, l.title AS lesson_title
            FROM appeals a
            LEFT JOIN questions q ON q.id = a.question_id
            LEFT JOIN tests t ON t.id = q.test_id
            LEFT JOIN lessons l ON l.test_id = t.id
            LEFT JOIN users u ON u.tg_id = a.user_tg_id
            {where}
            ORDER BY a.id DESC LIMIT ?""", args + (int(limit),))
    items = []
    for r in rows:
        r = dict(r)
        name = f"{r.get('first_name') or ''} {r.get('last_name') or ''}".strip()
        r["who"] = name or "без имени"
        r["when"] = (r.get("created_at") or "")[:16].replace("T", " ")
        items.append(r)
    return {"appeals": items,
            "pending_count": svc.count_pending(),
            "filter_status": status}


async def admin_appeals(request: web.Request) -> web.Response:
    from webapp import learning
    await learning._require_admin(request)
    status = request.query.get("status", "pending")
    if status not in ("pending", "approved", "rejected", "all"):
        status = "pending"
    context = await auth.nav_context(request)
    context.update(await asyncio.to_thread(_appeals_sync, status))
    context["message"] = request.query.get("message")
    return aiohttp_jinja2.render_template("admin_appeals.html", request, context)


async def admin_appeal_resolve(request: web.Request) -> web.Response:
    from webapp import learning
    tg_id = await learning._require_admin(request)
    appeal_id = int(request.match_info["appeal_id"])
    data = await request.post()
    action = (data.get("action") or "").strip()

    def _run():
        if action == "approve":
            svc.approve_appeal(appeal_id, tg_id)
            return "Апелляция принята"
        if action == "reject":
            warns, banned = svc.reject_appeal(appeal_id, tg_id)
            return ("Отклонено, ученик заблокирован" if banned
                    else f"Отклонено (предупреждений: {warns})")
        return "Ничего не сделано"

    msg = await asyncio.to_thread(_run)
    back = (data.get("next") or "/admin/appeals").strip()
    if not back.startswith("/admin"):
        back = "/admin/appeals"
    sep = "&" if "?" in back else "?"
    raise web.HTTPFound(f"{back}{sep}message={msg}")


def register_routes(app):
    app.router.add_post("/appeals/create", create_appeal)
    app.router.add_get("/admin/appeals", admin_appeals)
    app.router.add_post("/admin/appeals/{appeal_id:\\d+}/resolve", admin_appeal_resolve)
