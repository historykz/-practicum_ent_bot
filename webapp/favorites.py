"""
⭐️ Избранные вопросы: страницы раздела и подключение к существующим режимам.

  /tests/favorites          — список отмеченных вопросов и кнопка «НАЧАТЬ ТЕСТ»
  /tests/favorites/start    — выбор режима: тест, карточки, заучивание
  /tests/favorites/test     — обычный экран теста (learn_test.html) и тот же движок попыток
  /tests/favorites/cards    — обычные карточки (flashcards.html)
  /tests/favorites/study    — обычное заучивание (study.html)
  /learn/api/favorites/*    — поставить или снять звезду, узнать состояние

Режимы не дублируются: сюда приходит только другой набор вопросов.
"""
import asyncio
import json

import aiohttp_jinja2
from aiohttp import web

import config
import utils
from services import favorites as fav
from webapp import auth


def _json(value) -> str:
    """JSON для вставки в <script>: «</script>» в тексте вопроса не ломает страницу."""
    return (json.dumps(value, ensure_ascii=False).replace("</", "<\\/")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


LESSON = {"id": 0, "title": fav.SYSTEM_TITLE}
BACK = {"back_url": "/tests/favorites", "back_title": "⭐️ Избранные вопросы", "back_label": "⭐️ К избранным"}


async def _user(request):
    from webapp import learning
    tg_id = await learning._require_login(request)
    user = await asyncio.to_thread(utils.get_user_by_tg, tg_id)
    if not user:
        raise web.HTTPFound("/?error=login_required")
    return tg_id, user


def _is_premium_sync(tg_id: int, user_id: int) -> bool:
    return utils.is_admin(tg_id) or utils.is_premium(user_id)


# ───────────────────────── API звезды ─────────────────────────

async def api_toggle(request: web.Request) -> web.Response:
    tg_id = await auth.get_logged_in_tg_id(request)
    if tg_id is None:
        return web.json_response({"ok": False, "error": "Войдите, чтобы сохранять вопросы."}, status=403)
    try:
        body = await request.json()
        qid = int(body.get("question_id"))
    except (ValueError, TypeError):
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)
    on = body.get("on")
    user = await asyncio.to_thread(utils.get_user_by_tg, tg_id)
    if not user:
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    res = await asyncio.to_thread(fav.set_favorite, user["id"], qid, None if on is None else bool(on))
    return web.json_response(res, status=200 if res.get("ok") else 404)


async def api_state(request: web.Request) -> web.Response:
    tg_id = await auth.get_logged_in_tg_id(request)
    if tg_id is None:
        return web.json_response({"ok": True, "ids": [], "guest": True})
    try:
        body = await request.json()
        ids = body.get("ids") or []
    except (ValueError, TypeError):
        ids = []
    user = await asyncio.to_thread(utils.get_user_by_tg, tg_id)
    if not user:
        return web.json_response({"ok": True, "ids": []})
    return web.json_response({"ok": True, "ids": await asyncio.to_thread(fav.state, user["id"], ids)})


# ───────────────────────── страницы раздела ─────────────────────────

async def favorites_page(request: web.Request) -> web.Response:
    tg_id, user = await _user(request)
    rows = await asyncio.to_thread(fav.items, user["id"], tg_id)
    ctx = await auth.nav_context(request)
    ctx.update({"items": rows, "count": len(rows),
                "ready": sum(1 for r in rows if r["accessible"]),
                "locked": sum(1 for r in rows if not r["accessible"])})
    return aiohttp_jinja2.render_template("favorites_list.html", request, ctx)


async def favorites_modes(request: web.Request) -> web.Response:
    tg_id, user = await _user(request)

    def _data():
        return {"n_test": len(fav.practice_ids(user["id"], tg_id, for_test=True)),
                "n_cards": len(fav.practice_ids(user["id"], tg_id, for_test=False)),
                "premium": _is_premium_sync(tg_id, user["id"])}
    data = await asyncio.to_thread(_data)
    if not data["n_test"] and not data["n_cards"]:
        raise web.HTTPFound("/tests/favorites")
    ctx = await auth.nav_context(request)
    ctx.update(data)
    ctx["bot_username"] = config.WEB_BOT_USERNAME
    return aiohttp_jinja2.render_template("favorites_modes.html", request, ctx)


# ───────────────────────── 📝 тест: обычный движок ─────────────────────────

async def favorites_test(request: web.Request) -> web.Response:
    from webapp import learning
    tg_id, user = await _user(request)
    restart = request.query.get("restart") == "1"
    started = await asyncio.to_thread(fav.start_attempt, user["id"], tg_id, restart)
    if not started:
        raise web.HTTPFound("/tests/favorites")
    questions = await asyncio.to_thread(learning._build_questions_out, started["q_ids"], {})
    if not questions:
        # Все вопросы незаконченной попытки удалены — начинаем заново по текущему избранному
        started = await asyncio.to_thread(fav.start_attempt, user["id"], tg_id, True)
        if not started:
            raise web.HTTPFound("/tests/favorites")
        questions = await asyncio.to_thread(learning._build_questions_out, started["q_ids"], {})
    answered = {}
    if started["resume"]:
        answered = await asyncio.to_thread(learning._answered_map_sync, started["attempt_id"], True)
    ctx = await auth.nav_context(request)
    ctx.update({
        "attempt_id": started["attempt_id"], "lesson": dict(LESSON),
        "questions": questions, "questions_json": _json(questions),
        "answered": answered, "answered_json": _json(answered),
        "time_per_question": 0, "show_correct": True, "is_resume": started["resume"],
        "has_note": False, "favorites_mode": True,
        "watermark_svg": await asyncio.to_thread(learning._watermark_svg_sync, tg_id),
    })
    ctx.update(BACK)
    return aiohttp_jinja2.render_template("learn_test.html", request, ctx)


# ───────────────────────── 🃏 карточки и 🧠 заучивание: обычные режимы ─────────────────────────

def _mode_questions_sync(user_id: int, tg_id: int) -> list:
    """Тот же формат, что у карточек и заучивания урока (modes._lesson_mode_access_sync)."""
    out = []
    for qid in fav.practice_ids(user_id, tg_id, for_test=False):
        row = __import__("database").fetchone(
            "SELECT q.id, q.text, q.web_image_path, (SELECT text FROM question_options "
            "WHERE question_id=q.id AND is_correct=1 ORDER BY order_num, id LIMIT 1) AS answer "
            "FROM questions q WHERE q.id=?", (qid,))
        if row:
            out.append({"id": row["id"], "text": row["text"], "web_image_path": row["web_image_path"],
                        "answer": row["answer"] or ""})
    return out


async def _mode_page(request: web.Request, template: str, public_only: bool) -> web.Response:
    from webapp import learning
    tg_id, user = await _user(request)
    ctx = await auth.nav_context(request)
    ctx.update(BACK)
    # Карточки и заучивание — по Премиуму, как и на уроках
    if not await asyncio.to_thread(_is_premium_sync, tg_id, user["id"]):
        ctx.update({"access_state": "need_premium", "lesson_id": 0, "bot_username": config.WEB_BOT_USERNAME})
        return aiohttp_jinja2.render_template("mode_locked.html", request, ctx)
    questions = await asyncio.to_thread(_mode_questions_sync, user["id"], tg_id)
    if not questions:
        raise web.HTTPFound("/tests/favorites")
    if public_only:      # заучивание: ответ в разметку не отдаём
        questions = [{"id": q["id"], "text": q["text"], "web_image_path": q["web_image_path"]} for q in questions]
    ctx.update({"lesson": dict(LESSON), "subject": {"title": ""}, "questions": questions,
                "questions_json": _json(questions), "favorites_mode": True, "finish_url": "",
                "watermark_svg": await asyncio.to_thread(learning._watermark_svg_sync, tg_id)})
    return aiohttp_jinja2.render_template(template, request, ctx)


async def favorites_cards(request: web.Request) -> web.Response:
    return await _mode_page(request, "flashcards.html", public_only=False)


async def favorites_study(request: web.Request) -> web.Response:
    return await _mode_page(request, "study.html", public_only=True)


def register_routes(app: web.Application) -> None:
    app.router.add_post("/learn/api/favorites/toggle", api_toggle)
    app.router.add_post("/learn/api/favorites/state", api_state)
    app.router.add_get("/tests/favorites", favorites_page)
    app.router.add_get("/tests/favorites/start", favorites_modes)
    app.router.add_get("/tests/favorites/test", favorites_test)
    app.router.add_get("/tests/favorites/cards", favorites_cards)
    app.router.add_get("/tests/favorites/study", favorites_study)
