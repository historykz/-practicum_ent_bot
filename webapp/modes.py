"""
Режимы "🃏 Карточки" и "🧠 Заучивание" для уроков сайта.

Данные берутся из тех же questions/question_options, что и обычный тест
урока (авто-конвертация — отдельно ничего импортировать не нужно).
Проверка ответа в Заучивании переиспользует services/modes_service.py
(check_answer/normalize_answer) — то же самое нечёткое сравнение
(92%/82%), что уже работает в боте, без дублирования логики.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import aiohttp_jinja2
from aiohttp import web

import config
import database as db
import utils
from services import modes_service
from webapp import auth, learning

logger = logging.getLogger(__name__)
ALMATY = timezone(timedelta(hours=5))


def _lesson_mode_access_sync(lesson_id: int, tg_id):
    """Возвращает (lesson, questions) если доступ есть, иначе (None, access_state)."""
    lesson = db.fetchone("SELECT * FROM lessons WHERE id=? AND status='open'", (lesson_id,))
    if not lesson or not lesson["test_id"]:
        return None, "not_found"
    section = db.fetchone("SELECT * FROM sections WHERE id=?", (lesson["section_id"],))
    if not section:
        return None, "not_found"
    subject = db.fetchone("SELECT * FROM subjects WHERE id=? AND status='active'", (section["subject_id"],))
    if not subject:
        return None, "not_found"
    if tg_id is None:
        return None, "need_login"
    if not learning._has_subject_access_sync(section["subject_id"], tg_id):
        return None, "need_subject_access"
    if not learning._has_lesson_paid_access_sync(dict(lesson), tg_id):
        return None, "need_payment"

    questions = db.fetchall(
        "SELECT * FROM questions WHERE test_id=? ORDER BY order_num, id", (lesson["test_id"],)
    )
    out = []
    for q in questions:
        q = dict(q)
        correct = db.fetchone(
            "SELECT text FROM question_options WHERE question_id=? AND is_correct=1", (q["id"],)
        )
        out.append({
            "id": q["id"], "text": q["text"], "web_image_path": q.get("web_image_path"),
            "answer": correct["text"] if correct else "",
        })
    return {"lesson": dict(lesson), "subject": dict(subject), "questions": out}, "ok"


# === Карточки ===

async def flashcards_start(request: web.Request) -> web.Response:
    tg_id = await auth.get_logged_in_tg_id(request)
    lesson_id = int(request.match_info["lesson_id"])
    data, state = await asyncio.to_thread(_lesson_mode_access_sync, lesson_id, tg_id)
    context = await auth.nav_context(request)
    if data is None:
        if state == "not_found":
            raise web.HTTPNotFound()
        context["access_state"] = state
        context["lesson_id"] = lesson_id
        context["bot_username"] = config.WEB_BOT_USERNAME
        return aiohttp_jinja2.render_template("mode_locked.html", request, context)

    context.update(data)
    context["questions_json"] = json.dumps(data["questions"], ensure_ascii=False)
    context["watermark_svg"] = await asyncio.to_thread(learning._watermark_svg_sync, tg_id)
    return aiohttp_jinja2.render_template("flashcards.html", request, context)


async def flashcards_finish(request: web.Request) -> web.Response:
    tg_id = await auth.get_logged_in_tg_id(request)
    if tg_id is None:
        return web.json_response({"error": "forbidden"}, status=403)
    lesson_id = int(request.match_info["lesson_id"])
    body = await request.json()

    def _save():
        lesson = db.fetchone("SELECT test_id FROM lessons WHERE id=?", (lesson_id,))
        if not lesson or not lesson["test_id"]:
            return
        db.execute(
            "INSERT INTO mode_results (user_tg_id, test_id, mode, total, know_count, "
            "dontknow_count, duration_sec) VALUES (?, ?, 'flashcards', ?, ?, ?, ?)",
            (tg_id, lesson["test_id"], body.get("total", 0), body.get("know", 0),
             body.get("dontknow", 0), body.get("duration_sec", 0)),
        )

    await asyncio.to_thread(_save)
    return web.json_response({"ok": True})


# === Заучивание ===

async def study_start(request: web.Request) -> web.Response:
    tg_id = await auth.get_logged_in_tg_id(request)
    lesson_id = int(request.match_info["lesson_id"])
    data, state = await asyncio.to_thread(_lesson_mode_access_sync, lesson_id, tg_id)
    context = await auth.nav_context(request)
    if data is None:
        if state == "not_found":
            raise web.HTTPNotFound()
        context["access_state"] = state
        context["lesson_id"] = lesson_id
        context["bot_username"] = config.WEB_BOT_USERNAME
        return aiohttp_jinja2.render_template("mode_locked.html", request, context)

    context.update(data)
    # Ответ ученику не отдаём в разметке — только id/текст/картинка
    questions_public = [
        {"id": q["id"], "text": q["text"], "web_image_path": q["web_image_path"]}
        for q in data["questions"]
    ]
    context["questions_json"] = json.dumps(questions_public, ensure_ascii=False)
    context["watermark_svg"] = await asyncio.to_thread(learning._watermark_svg_sync, tg_id)
    return aiohttp_jinja2.render_template("study.html", request, context)


def _study_check_sync(question_id: int, user_text: str) -> dict:
    question = db.fetchone("SELECT * FROM questions WHERE id=?", (question_id,))
    if not question:
        return {"correct": False, "close": False, "correct_text": ""}
    return modes_service.check_answer(user_text, dict(question))


async def study_check(request: web.Request) -> web.Response:
    tg_id = await auth.get_logged_in_tg_id(request)
    if tg_id is None:
        return web.json_response({"error": "forbidden"}, status=403)
    body = await request.json()
    question_id = int(body["question_id"])
    user_text = (body.get("answer") or "").strip()
    result = await asyncio.to_thread(_study_check_sync, question_id, user_text)
    return web.json_response(result)


async def study_finish(request: web.Request) -> web.Response:
    tg_id = await auth.get_logged_in_tg_id(request)
    if tg_id is None:
        return web.json_response({"error": "forbidden"}, status=403)
    lesson_id = int(request.match_info["lesson_id"])
    body = await request.json()

    def _save():
        lesson = db.fetchone("SELECT test_id FROM lessons WHERE id=?", (lesson_id,))
        if not lesson or not lesson["test_id"]:
            return
        db.execute(
            "INSERT INTO mode_results (user_tg_id, test_id, mode, total, correct_first, "
            "correct_retry, wrong_count, skipped_count, duration_sec) "
            "VALUES (?, ?, 'learning', ?, ?, ?, ?, ?, ?)",
            (tg_id, lesson["test_id"], body.get("total", 0), body.get("correct_first", 0),
             body.get("correct_retry", 0), body.get("wrong", 0), body.get("skipped", 0),
             body.get("duration_sec", 0)),
        )

    await asyncio.to_thread(_save)
    return web.json_response({"ok": True})


def register_routes(app: web.Application) -> None:
    app.router.add_get("/learn/lesson/{lesson_id:\\d+}/flashcards", flashcards_start)
    app.router.add_post("/learn/api/flashcards/{lesson_id:\\d+}/finish", flashcards_finish)
    app.router.add_get("/learn/lesson/{lesson_id:\\d+}/study", study_start)
    app.router.add_post("/learn/api/study/check", study_check)
    app.router.add_post("/learn/api/study/{lesson_id:\\d+}/finish", study_finish)
