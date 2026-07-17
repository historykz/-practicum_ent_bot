"""
Раздел "Начать обучение": предметы -> разделы -> уроки -> тест.

Студенческая и админская части в одном файле — держим раздел
компактным и самодостаточным, не размазывая по многим файлам.

Импорт теста (текст/ZIP) двухшаговый: сначала парсим и показываем
администратору предпросмотр (черновик в lesson_test_drafts), реальный
тест создаётся в базе только после нажатия "Подтвердить".
"""
import asyncio
import json
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp_jinja2
from aiohttp import web

import config
import database as db
import utils
from webapp import auth, lesson_import

logger = logging.getLogger(__name__)

ALMATY = timezone(timedelta(hours=5))


# === Хелперы доступа ===

async def _require_login(request: web.Request):
    tg_id = await auth.get_logged_in_tg_id(request)
    if tg_id is None:
        raise web.HTTPFound("/?error=login_required")
    return tg_id


async def _require_admin(request: web.Request):
    tg_id = await _require_login(request)
    is_adm = await asyncio.to_thread(utils.is_admin, tg_id)
    if not is_adm:
        raise web.HTTPForbidden(text="Доступ только для администраторов")
    return tg_id


def _has_subject_access_sync(subject_id: int, tg_id: int) -> bool:
    subj = db.fetchone("SELECT is_open FROM subjects WHERE id=?", (subject_id,))
    if not subj:
        return False
    if subj["is_open"]:
        return True
    row = db.fetchone(
        "SELECT expires_at FROM subject_access WHERE subject_id=? AND user_tg_id=?",
        (subject_id, tg_id),
    )
    if not row:
        return False
    if not row["expires_at"]:
        return True
    try:
        return datetime.fromisoformat(row["expires_at"]) > datetime.utcnow()
    except ValueError:
        return False


# === Студент: список предметов ===

def _list_subjects_for_user_sync(tg_id: int) -> list:
    subjects = db.fetchall(
        "SELECT * FROM subjects WHERE status='active' ORDER BY sort_order, id"
    )
    result = []
    for s in subjects:
        s = dict(s)
        if _has_subject_access_sync(s["id"], tg_id):
            result.append(s)
    return result


async def learn_index(request: web.Request) -> web.Response:
    tg_id = await _require_login(request)
    subjects = await asyncio.to_thread(_list_subjects_for_user_sync, tg_id)
    context = await auth.nav_context(request)
    context["subjects"] = subjects
    return aiohttp_jinja2.render_template("learn_subjects.html", request, context)


# === Студент: предмет (разделы + уроки) ===

def _subject_detail_sync(subject_id: int, tg_id: int) -> Optional[dict]:
    if not _has_subject_access_sync(subject_id, tg_id):
        return None
    subject = db.fetchone("SELECT * FROM subjects WHERE id=?", (subject_id,))
    if not subject:
        return None
    sections = db.fetchall(
        "SELECT * FROM sections WHERE subject_id=? ORDER BY sort_order, id", (subject_id,)
    )
    result_sections = []
    viewed = {r["lesson_id"] for r in db.fetchall(
        "SELECT lesson_id FROM lesson_progress WHERE user_tg_id=?", (tg_id,)
    )}
    for sec in sections:
        lessons = db.fetchall(
            "SELECT * FROM lessons WHERE section_id=? ORDER BY sort_order, id", (sec["id"],)
        )
        lessons_out = []
        for lesson in lessons:
            lesson = dict(lesson)
            lesson["viewed"] = lesson["id"] in viewed
            lessons_out.append(lesson)
        result_sections.append({"section": dict(sec), "lessons": lessons_out})
    return {"subject": dict(subject), "sections": result_sections}


async def learn_subject(request: web.Request) -> web.Response:
    tg_id = await _require_login(request)
    subject_id = int(request.match_info["subject_id"])
    data = await asyncio.to_thread(_subject_detail_sync, subject_id, tg_id)
    if data is None:
        raise web.HTTPNotFound(text="Предмет не найден или нет доступа")
    data.update(await auth.nav_context(request))
    return aiohttp_jinja2.render_template("learn_subject.html", request, data)


# === Студент: урок ===

def _lesson_detail_sync(lesson_id: int, tg_id: int) -> Optional[dict]:
    lesson = db.fetchone("SELECT * FROM lessons WHERE id=?", (lesson_id,))
    if not lesson:
        return None
    section = db.fetchone("SELECT * FROM sections WHERE id=?", (lesson["section_id"],))
    if not section:
        return None
    if not _has_subject_access_sync(section["subject_id"], tg_id):
        return None
    subject = db.fetchone("SELECT * FROM subjects WHERE id=?", (section["subject_id"],))
    if lesson["status"] != "open":
        return None
    db.execute(
        "INSERT OR IGNORE INTO lesson_progress (user_tg_id, lesson_id) VALUES (?, ?)",
        (tg_id, lesson_id),
    )
    test_info = None
    if lesson["test_id"]:
        test = db.fetchone("SELECT * FROM tests WHERE id=?", (lesson["test_id"],))
        qcount = db.fetchone(
            "SELECT COUNT(*) AS c FROM questions WHERE test_id=?", (lesson["test_id"],)
        )["c"]
        if test:
            user = utils.get_user_by_tg(tg_id)
            attempts_used = db.fetchone(
                "SELECT COUNT(*) AS c FROM test_attempts "
                "WHERE user_id=? AND test_id=? AND status='finished'",
                (user["id"], lesson["test_id"]),
            )["c"]
            limit = test["attempts_limit"] or 0
            test_info = {
                "test_id": lesson["test_id"],
                "questions_count": qcount,
                "attempts_used": attempts_used,
                "attempts_limit": limit,
                "attempts_left": (limit - attempts_used) if limit else None,
                "blocked": bool(limit) and attempts_used >= limit,
            }
    return {
        "lesson": dict(lesson),
        "section": dict(section),
        "subject": dict(subject),
        "test_info": test_info,
    }


async def learn_lesson(request: web.Request) -> web.Response:
    tg_id = await _require_login(request)
    lesson_id = int(request.match_info["lesson_id"])
    data = await asyncio.to_thread(_lesson_detail_sync, lesson_id, tg_id)
    if data is None:
        raise web.HTTPNotFound(text="Урок не найден или нет доступа")
    data.update(await auth.nav_context(request))
    data["error"] = request.query.get("error")
    return aiohttp_jinja2.render_template("learn_lesson.html", request, data)


# === Студент: прохождение теста ===

def _start_attempt_sync(lesson_id: int, tg_id: int) -> Optional[dict]:
    lesson = db.fetchone("SELECT * FROM lessons WHERE id=?", (lesson_id,))
    if not lesson or not lesson["test_id"]:
        return None
    section = db.fetchone("SELECT * FROM sections WHERE id=?", (lesson["section_id"],))
    if not section or not _has_subject_access_sync(section["subject_id"], tg_id):
        return None

    user = utils.get_user_by_tg(tg_id)
    user_id = user["id"]
    test_id = lesson["test_id"]
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))
    if not test:
        return None

    limit = test["attempts_limit"] or 0
    if limit:
        used = db.fetchone(
            "SELECT COUNT(*) AS c FROM test_attempts "
            "WHERE user_id=? AND test_id=? AND status='finished'",
            (user_id, test_id),
        )["c"]
        if used >= limit:
            return {"blocked": True}

    questions = db.fetchall(
        "SELECT id, text, web_image_path FROM questions WHERE test_id=? ORDER BY order_num, id",
        (test_id,),
    )
    questions = [dict(q) for q in questions]
    if test["shuffle_questions"]:
        random.shuffle(questions)
    q_ids = [q["id"] for q in questions]

    db.execute(
        "INSERT INTO test_attempts (user_id, test_id, question_order, status, is_counted) "
        "VALUES (?, ?, ?, 'in_progress', 1)",
        (user_id, test_id, json.dumps(q_ids)),
    )
    attempt_id = db.fetchone("SELECT last_insert_rowid() AS id")["id"]

    questions_out = []
    for q in questions:
        opts = db.fetchall(
            "SELECT id, text FROM question_options WHERE question_id=? ORDER BY order_num, id",
            (q["id"],),
        )
        opts = [dict(o) for o in opts]
        if test["shuffle_options"]:
            random.shuffle(opts)
        questions_out.append({
            "id": q["id"], "text": q["text"], "web_image_path": q["web_image_path"],
            "options": opts,
        })

    return {
        "attempt_id": attempt_id,
        "lesson": dict(lesson),
        "questions": questions_out,
        "time_per_question": test["time_per_question"] or 0,
        "show_correct": bool(test["show_correct"]),
    }


async def learn_test_start(request: web.Request) -> web.Response:
    tg_id = await _require_login(request)
    lesson_id = int(request.match_info["lesson_id"])
    data = await asyncio.to_thread(_start_attempt_sync, lesson_id, tg_id)
    if data is None:
        raise web.HTTPNotFound(text="Тест не найден или нет доступа")
    if data.get("blocked"):
        raise web.HTTPFound(f"/learn/lesson/{lesson_id}?error=attempts_exceeded")
    data.update(await auth.nav_context(request))
    data["questions_json"] = json.dumps(data["questions"], ensure_ascii=False)
    return aiohttp_jinja2.render_template("learn_test.html", request, data)


def _answer_sync(attempt_id: int, tg_id: int, question_id: int, option_id: Optional[int]) -> dict:
    attempt = db.fetchone("SELECT * FROM test_attempts WHERE id=?", (attempt_id,))
    if not attempt:
        return {"error": "attempt_not_found"}
    user = utils.get_user_by_tg(tg_id)
    if not user or attempt["user_id"] != user["id"]:
        return {"error": "forbidden"}

    test = db.fetchone("SELECT show_correct FROM tests WHERE id=?", (attempt["test_id"],))
    show_correct = bool(test["show_correct"]) if test else True

    if option_id is None:
        # Вышло время (таймер) — засчитываем как пропуск
        try:
            db.execute(
                "INSERT INTO attempt_answers (attempt_id, question_id, selected_option_id, is_correct, skipped) "
                "VALUES (?, ?, NULL, 0, 1)",
                (attempt_id, question_id),
            )
        except Exception:
            pass
        db.execute("UPDATE test_attempts SET skipped_answers = skipped_answers + 1 WHERE id=?",
                    (attempt_id,))
        correct_opt = db.fetchone(
            "SELECT id FROM question_options WHERE question_id=? AND is_correct=1", (question_id,))
        return {
            "correct": False if show_correct else None,
            "correct_option_id": (correct_opt["id"] if correct_opt else None) if show_correct else None,
            "skipped": True,
        }

    opt = db.fetchone(
        "SELECT id, is_correct FROM question_options WHERE id=? AND question_id=?",
        (option_id, question_id),
    )
    if not opt:
        return {"error": "bad_option"}
    is_correct = bool(opt["is_correct"])

    correct_opt = db.fetchone(
        "SELECT id FROM question_options WHERE question_id=? AND is_correct=1",
        (question_id,),
    )

    try:
        db.execute(
            "INSERT INTO attempt_answers (attempt_id, question_id, selected_option_id, is_correct) "
            "VALUES (?, ?, ?, ?)",
            (attempt_id, question_id, option_id, 1 if is_correct else 0),
        )
    except Exception:
        pass  # уже отвечал на этот вопрос (UNIQUE), не считаем повторно

    if is_correct:
        db.execute("UPDATE test_attempts SET correct_answers = correct_answers + 1 WHERE id=?",
                    (attempt_id,))
    else:
        db.execute("UPDATE test_attempts SET wrong_answers = wrong_answers + 1 WHERE id=?",
                    (attempt_id,))

    return {
        "correct": is_correct if show_correct else None,
        "correct_option_id": (correct_opt["id"] if correct_opt else None) if show_correct else None,
    }


async def learn_test_answer(request: web.Request) -> web.Response:
    tg_id = await _require_login(request)
    attempt_id = int(request.match_info["attempt_id"])
    body = await request.json()
    question_id = int(body["question_id"])
    option_id = body.get("option_id")
    option_id = int(option_id) if option_id is not None else None
    result = await asyncio.to_thread(_answer_sync, attempt_id, tg_id, question_id, option_id)
    return web.json_response(result)


def _finish_attempt_sync(attempt_id: int, tg_id: int) -> dict:
    attempt = db.fetchone("SELECT * FROM test_attempts WHERE id=?", (attempt_id,))
    user = utils.get_user_by_tg(tg_id)
    if not attempt or not user or attempt["user_id"] != user["id"]:
        return {"error": "forbidden"}

    test = db.fetchone("SELECT show_results FROM tests WHERE id=?", (attempt["test_id"],))
    show_results = bool(test["show_results"]) if test else True

    correct = attempt["correct_answers"]
    wrong = attempt["wrong_answers"]
    total = correct + wrong
    percent = round(correct / total * 100, 1) if total else 0

    db.execute(
        "UPDATE test_attempts SET status='finished', score=?, end_time=? WHERE id=?",
        (correct, datetime.utcnow().isoformat(timespec="seconds"), attempt_id),
    )
    if not show_results:
        return {"show_results": False}
    return {"show_results": True, "correct": correct, "wrong": wrong, "total": total, "percent": percent}


async def learn_test_finish(request: web.Request) -> web.Response:
    tg_id = await _require_login(request)
    attempt_id = int(request.match_info["attempt_id"])
    result = await asyncio.to_thread(_finish_attempt_sync, attempt_id, tg_id)
    return web.json_response(result)


def _attempt_result_sync(attempt_id: int, tg_id: int) -> Optional[dict]:
    attempt = db.fetchone("SELECT * FROM test_attempts WHERE id=?", (attempt_id,))
    user = utils.get_user_by_tg(tg_id)
    if not attempt or not user or attempt["user_id"] != user["id"]:
        return None
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (attempt["test_id"],))
    lesson = db.fetchone("SELECT * FROM lessons WHERE test_id=?", (attempt["test_id"],))
    total = attempt["correct_answers"] + attempt["wrong_answers"]
    percent = round(attempt["correct_answers"] / total * 100, 1) if total else 0
    return {
        "attempt": dict(attempt), "test": dict(test) if test else None,
        "lesson": dict(lesson) if lesson else None, "percent": percent, "total": total,
        "show_results": bool(test["show_results"]) if test else True,
    }


async def learn_test_result(request: web.Request) -> web.Response:
    tg_id = await _require_login(request)
    attempt_id = int(request.match_info["attempt_id"])
    data = await asyncio.to_thread(_attempt_result_sync, attempt_id, tg_id)
    if data is None:
        raise web.HTTPNotFound()
    data.update(await auth.nav_context(request))
    return aiohttp_jinja2.render_template("learn_test_result.html", request, data)


# === Раздача загруженных картинок вопросов (не через static/, хранится рядом с БД) ===

async def uploaded_question_image(request: web.Request) -> web.Response:
    filename = request.match_info["filename"]
    if "/" in filename or ".." in filename:
        raise web.HTTPBadRequest()
    path = lesson_import.upload_dir() / filename
    if not path.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(path)


# === Админка: обзор ===

def _admin_overview_sync() -> list:
    subjects = db.fetchall("SELECT * FROM subjects ORDER BY sort_order, id")
    result = []
    for s in subjects:
        s = dict(s)
        s["sections"] = []
        secs = db.fetchall("SELECT * FROM sections WHERE subject_id=? ORDER BY sort_order, id", (s["id"],))
        for sec in secs:
            sec = dict(sec)
            sec["lessons"] = [dict(l) for l in db.fetchall(
                "SELECT * FROM lessons WHERE section_id=? ORDER BY sort_order, id", (sec["id"],))]
            s["sections"].append(sec)
        result.append(s)
    return result


async def admin_learn_index(request: web.Request) -> web.Response:
    await _require_admin(request)
    subjects = await asyncio.to_thread(_admin_overview_sync)
    context = await auth.nav_context(request)
    context["subjects"] = subjects
    context["message"] = request.query.get("message")
    context["edit_subject"] = request.query.get("edit_subject")
    context["edit_section"] = request.query.get("edit_section")
    return aiohttp_jinja2.render_template("admin_learn.html", request, context)


# === Админка: предметы ===

async def admin_create_subject(request: web.Request) -> web.Response:
    tg_id = await _require_admin(request)
    data = await request.post()
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    is_open = 1 if data.get("is_open") == "on" else 0
    if title:
        await asyncio.to_thread(
            db.execute,
            "INSERT INTO subjects (title, description, is_open, created_by) VALUES (?, ?, ?, ?)",
            (title, description, is_open, tg_id),
        )
    raise web.HTTPFound("/admin/learn?message=Предмет создан")


async def admin_edit_subject(request: web.Request) -> web.Response:
    await _require_admin(request)
    subject_id = int(request.match_info["subject_id"])
    data = await request.post()
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    is_open = 1 if data.get("is_open") == "on" else 0
    if title:
        await asyncio.to_thread(
            db.execute,
            "UPDATE subjects SET title=?, description=?, is_open=? WHERE id=?",
            (title, description, is_open, subject_id),
        )
    raise web.HTTPFound("/admin/learn?message=Предмет обновлён")


def _delete_subject_cascade_sync(subject_id: int) -> None:
    lessons = db.fetchall(
        "SELECT l.id, l.test_id FROM lessons l JOIN sections s ON s.id=l.section_id "
        "WHERE s.subject_id=? AND l.test_id IS NOT NULL", (subject_id,)
    )
    for l in lessons:
        db.execute("DELETE FROM tests WHERE id=?", (l["test_id"],))
    db.execute("DELETE FROM subjects WHERE id=?", (subject_id,))  # каскадом уйдут sections/lessons/access


async def admin_delete_subject(request: web.Request) -> web.Response:
    await _require_admin(request)
    subject_id = int(request.match_info["subject_id"])
    await asyncio.to_thread(_delete_subject_cascade_sync, subject_id)
    raise web.HTTPFound("/admin/learn?message=Предмет удалён")


async def admin_toggle_subject(request: web.Request) -> web.Response:
    await _require_admin(request)
    subject_id = int(request.match_info["subject_id"])

    def _toggle():
        row = db.fetchone("SELECT status FROM subjects WHERE id=?", (subject_id,))
        new_status = "hidden" if row["status"] == "active" else "active"
        db.execute("UPDATE subjects SET status=? WHERE id=?", (new_status, subject_id))

    await asyncio.to_thread(_toggle)
    raise web.HTTPFound("/admin/learn?message=Статус предмета изменён")


async def admin_grant_access(request: web.Request) -> web.Response:
    tg_id = await _require_admin(request)
    subject_id = int(request.match_info["subject_id"])
    data = await request.post()
    target_raw = (data.get("user_tg_id") or "").strip()
    days_raw = (data.get("days") or "0").strip()

    if not target_raw.isdigit():
        raise web.HTTPFound("/admin/learn?message=Некорректный Telegram ID")

    target_tg_id = int(target_raw)
    days = int(days_raw) if days_raw.isdigit() else 0
    expires_at = None
    if days > 0:
        expires_at = (datetime.utcnow() + timedelta(days=days)).isoformat(timespec="seconds")

    await asyncio.to_thread(
        db.execute,
        "INSERT INTO subject_access (subject_id, user_tg_id, expires_at, granted_by_admin) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(subject_id, user_tg_id) DO UPDATE SET expires_at=excluded.expires_at",
        (subject_id, target_tg_id, expires_at, tg_id),
    )
    raise web.HTTPFound("/admin/learn?message=Доступ выдан")


# === Админка: разделы ===

async def admin_create_section(request: web.Request) -> web.Response:
    await _require_admin(request)
    subject_id = int(request.match_info["subject_id"])
    data = await request.post()
    title = (data.get("title") or "").strip()
    if title:
        await asyncio.to_thread(
            db.execute,
            "INSERT INTO sections (subject_id, title) VALUES (?, ?)",
            (subject_id, title),
        )
    raise web.HTTPFound("/admin/learn?message=Раздел создан")


async def admin_edit_section(request: web.Request) -> web.Response:
    await _require_admin(request)
    section_id = int(request.match_info["section_id"])
    data = await request.post()
    title = (data.get("title") or "").strip()
    if title:
        await asyncio.to_thread(
            db.execute, "UPDATE sections SET title=? WHERE id=?", (title, section_id)
        )
    raise web.HTTPFound("/admin/learn?message=Раздел обновлён")


def _delete_section_cascade_sync(section_id: int) -> None:
    lessons = db.fetchall(
        "SELECT id, test_id FROM lessons WHERE section_id=? AND test_id IS NOT NULL", (section_id,)
    )
    for l in lessons:
        db.execute("DELETE FROM tests WHERE id=?", (l["test_id"],))
    db.execute("DELETE FROM sections WHERE id=?", (section_id,))  # каскадом уйдут lessons


async def admin_delete_section(request: web.Request) -> web.Response:
    await _require_admin(request)
    section_id = int(request.match_info["section_id"])
    await asyncio.to_thread(_delete_section_cascade_sync, section_id)
    raise web.HTTPFound("/admin/learn?message=Раздел удалён")


# === Админка: уроки (создание -> либо сразу, либо через превью теста) ===

async def admin_create_lesson(request: web.Request) -> web.Response:
    tg_id = await _require_admin(request)
    section_id = int(request.match_info["section_id"])
    data = await request.post()
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    content_html = (data.get("content") or "").strip()
    test_mode = data.get("test_mode") or "none"
    test_text = data.get("test_text") or ""
    zip_bytes = None
    zip_field = data.get("test_zip")
    if zip_field is not None and hasattr(zip_field, "file"):
        zip_bytes = zip_field.file.read()

    if not title:
        raise web.HTTPFound("/admin/learn?message=Не указано название урока")

    if test_mode == "none" or (test_mode == "text" and not test_text.strip()) or \
            (test_mode == "zip" and not zip_bytes):
        await asyncio.to_thread(
            db.execute,
            "INSERT INTO lessons (section_id, title, description, content_html) VALUES (?, ?, ?, ?)",
            (section_id, title, description, content_html),
        )
        raise web.HTTPFound("/admin/learn?message=Урок создан")

    draft_id = await asyncio.to_thread(
        _create_draft_sync, tg_id, section_id, None, title, description, content_html,
        test_mode, test_text, zip_bytes,
    )
    raise web.HTTPFound(f"/admin/learn/drafts/{draft_id}")


def _create_draft_sync(admin_tg_id: int, section_id: int, lesson_id: Optional[int],
                        title: str, description: str, content: str,
                        test_mode: str, test_text: str, zip_bytes) -> int:
    if test_mode == "text":
        questions, errors = lesson_import.parse_draft_from_text(test_text)
    else:
        questions, errors = lesson_import.parse_draft_from_zip(zip_bytes)

    db.execute(
        "INSERT INTO lesson_test_drafts (admin_tg_id, section_id, lesson_id, lesson_title, "
        "lesson_description, lesson_content, questions_json, errors_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (admin_tg_id, section_id, lesson_id, title, description, content,
         json.dumps(questions, ensure_ascii=False), json.dumps(errors, ensure_ascii=False)),
    )
    return db.fetchone("SELECT last_insert_rowid() AS id")["id"]


def _get_draft_sync(draft_id: int) -> Optional[dict]:
    row = db.fetchone("SELECT * FROM lesson_test_drafts WHERE id=?", (draft_id,))
    if not row:
        return None
    row = dict(row)
    row["questions"] = json.loads(row["questions_json"])
    row["errors"] = json.loads(row["errors_json"])
    return row


async def admin_view_draft(request: web.Request) -> web.Response:
    await _require_admin(request)
    draft_id = int(request.match_info["draft_id"])
    draft = await asyncio.to_thread(_get_draft_sync, draft_id)
    if draft is None:
        raise web.HTTPNotFound()
    context = await auth.nav_context(request)
    context["draft"] = draft
    context["questions_count"] = len(draft["questions"])
    context["preview_questions"] = draft["questions"][:15]
    return aiohttp_jinja2.render_template("admin_import_preview.html", request, context)


def _confirm_draft_sync(draft_id: int, admin_tg_id: int, settings: dict) -> str:
    draft = _get_draft_sync(draft_id)
    if not draft:
        return "Черновик не найден (возможно, уже подтверждён)"
    if not draft["questions"]:
        return "Нельзя подтвердить: не распознано ни одного вопроса"

    test_id = lesson_import.finalize_test(
        f"Тест: {draft['lesson_title']}", admin_tg_id, draft["questions"], settings
    )

    if draft["lesson_id"]:
        old = db.fetchone("SELECT test_id FROM lessons WHERE id=?", (draft["lesson_id"],))
        if old and old["test_id"]:
            db.execute("DELETE FROM tests WHERE id=?", (old["test_id"],))
        db.execute("UPDATE lessons SET test_id=? WHERE id=?", (test_id, draft["lesson_id"]))
    else:
        db.execute(
            "INSERT INTO lessons (section_id, title, description, content_html, test_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (draft["section_id"], draft["lesson_title"], draft["lesson_description"],
             draft["lesson_content"], test_id),
        )

    db.execute("DELETE FROM lesson_test_drafts WHERE id=?", (draft_id,))
    added = len(draft["questions"])
    errs = len(draft["errors"])
    return f"Урок сохранён. Вопросов добавлено: {added}" + (f", ошибок формата: {errs}" if errs else "")


async def admin_confirm_draft(request: web.Request) -> web.Response:
    tg_id = await _require_admin(request)
    draft_id = int(request.match_info["draft_id"])
    data = await request.post()
    settings = {
        "show_correct": data.get("show_correct") == "on",
        "show_results": data.get("show_results") == "on",
        "attempts_limit": data.get("attempts_limit") or 0,
        "time_per_question": data.get("time_per_question") or 0,
        "shuffle_questions": data.get("shuffle_questions") == "on",
    }
    message = await asyncio.to_thread(_confirm_draft_sync, draft_id, tg_id, settings)
    raise web.HTTPFound(f"/admin/learn?message={message}")


async def admin_cancel_draft(request: web.Request) -> web.Response:
    await _require_admin(request)
    draft_id = int(request.match_info["draft_id"])
    await asyncio.to_thread(db.execute, "DELETE FROM lesson_test_drafts WHERE id=?", (draft_id,))
    raise web.HTTPFound("/admin/learn?message=Импорт отменён")


# === Админка: страница редактирования урока ===

def _lesson_edit_data_sync(lesson_id: int) -> Optional[dict]:
    lesson = db.fetchone("SELECT * FROM lessons WHERE id=?", (lesson_id,))
    if not lesson:
        return None
    test = None
    if lesson["test_id"]:
        test = db.fetchone("SELECT * FROM tests WHERE id=?", (lesson["test_id"],))
        if test:
            test = dict(test)
            test["questions_count"] = db.fetchone(
                "SELECT COUNT(*) AS c FROM questions WHERE test_id=?", (lesson["test_id"],))["c"]
    return {"lesson": dict(lesson), "test": test}


async def admin_lesson_edit_page(request: web.Request) -> web.Response:
    await _require_admin(request)
    lesson_id = int(request.match_info["lesson_id"])
    data = await asyncio.to_thread(_lesson_edit_data_sync, lesson_id)
    if data is None:
        raise web.HTTPNotFound()
    data.update(await auth.nav_context(request))
    data["message"] = request.query.get("message")
    return aiohttp_jinja2.render_template("admin_lesson_edit.html", request, data)


async def admin_edit_lesson(request: web.Request) -> web.Response:
    await _require_admin(request)
    lesson_id = int(request.match_info["lesson_id"])
    data = await request.post()
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    content_html = (data.get("content") or "").strip()
    status = "open" if data.get("status") == "on" else "closed"
    if title:
        await asyncio.to_thread(
            db.execute,
            "UPDATE lessons SET title=?, description=?, content_html=?, status=? WHERE id=?",
            (title, description, content_html, status, lesson_id),
        )
    raise web.HTTPFound(f"/admin/learn/lessons/{lesson_id}/edit?message=Урок обновлён")


async def admin_toggle_lesson(request: web.Request) -> web.Response:
    await _require_admin(request)
    lesson_id = int(request.match_info["lesson_id"])

    def _toggle():
        row = db.fetchone("SELECT status FROM lessons WHERE id=?", (lesson_id,))
        new_status = "closed" if row["status"] == "open" else "open"
        db.execute("UPDATE lessons SET status=? WHERE id=?", (new_status, lesson_id))

    await asyncio.to_thread(_toggle)
    raise web.HTTPFound("/admin/learn?message=Статус урока изменён")


def _delete_lesson_sync(lesson_id: int) -> None:
    row = db.fetchone("SELECT test_id FROM lessons WHERE id=?", (lesson_id,))
    if row and row["test_id"]:
        db.execute("DELETE FROM tests WHERE id=?", (row["test_id"],))
    db.execute("DELETE FROM lessons WHERE id=?", (lesson_id,))


async def admin_delete_lesson(request: web.Request) -> web.Response:
    await _require_admin(request)
    lesson_id = int(request.match_info["lesson_id"])
    await asyncio.to_thread(_delete_lesson_sync, lesson_id)
    raise web.HTTPFound("/admin/learn?message=Урок удалён")


async def admin_delete_lesson_content(request: web.Request) -> web.Response:
    await _require_admin(request)
    lesson_id = int(request.match_info["lesson_id"])
    await asyncio.to_thread(
        db.execute, "UPDATE lessons SET content_html='' WHERE id=?", (lesson_id,)
    )
    raise web.HTTPFound(f"/admin/learn/lessons/{lesson_id}/edit?message=Конспект удалён")


def _delete_lesson_test_sync(lesson_id: int) -> None:
    row = db.fetchone("SELECT test_id FROM lessons WHERE id=?", (lesson_id,))
    if row and row["test_id"]:
        db.execute("DELETE FROM tests WHERE id=?", (row["test_id"],))
        db.execute("UPDATE lessons SET test_id=NULL WHERE id=?", (lesson_id,))


async def admin_delete_lesson_test(request: web.Request) -> web.Response:
    await _require_admin(request)
    lesson_id = int(request.match_info["lesson_id"])
    await asyncio.to_thread(_delete_lesson_test_sync, lesson_id)
    raise web.HTTPFound(f"/admin/learn/lessons/{lesson_id}/edit?message=Тест удалён")


async def admin_replace_lesson_test(request: web.Request) -> web.Response:
    tg_id = await _require_admin(request)
    lesson_id = int(request.match_info["lesson_id"])
    data = await request.post()
    test_mode = data.get("test_mode") or "text"
    test_text = data.get("test_text") or ""
    zip_bytes = None
    zip_field = data.get("test_zip")
    if zip_field is not None and hasattr(zip_field, "file"):
        zip_bytes = zip_field.file.read()

    lesson = await asyncio.to_thread(db.fetchone, "SELECT * FROM lessons WHERE id=?", (lesson_id,))
    if not lesson:
        raise web.HTTPNotFound()

    if (test_mode == "text" and not test_text.strip()) or (test_mode == "zip" and not zip_bytes):
        raise web.HTTPFound(f"/admin/learn/lessons/{lesson_id}/edit?message=Не выбран файл/текст теста")

    draft_id = await asyncio.to_thread(
        _create_draft_sync, tg_id, lesson["section_id"], lesson_id,
        lesson["title"], lesson["description"], lesson["content_html"],
        test_mode, test_text, zip_bytes,
    )
    raise web.HTTPFound(f"/admin/learn/drafts/{draft_id}")


async def admin_test_settings(request: web.Request) -> web.Response:
    await _require_admin(request)
    test_id = int(request.match_info["test_id"])
    data = await request.post()
    lesson_id = data.get("lesson_id")

    settings = (
        1 if data.get("show_correct") == "on" else 0,
        1 if data.get("show_correct") == "on" else 0,
        1 if data.get("show_results") == "on" else 0,
        int(data.get("attempts_limit") or 0),
        int(data.get("time_per_question") or 0),
        1 if data.get("shuffle_questions") == "on" else 0,
        test_id,
    )
    await asyncio.to_thread(
        db.execute,
        "UPDATE tests SET show_correct=?, show_explanation=?, show_results=?, "
        "attempts_limit=?, time_per_question=?, shuffle_questions=? WHERE id=?",
        settings,
    )
    raise web.HTTPFound(f"/admin/learn/lessons/{lesson_id}/edit?message=Настройки теста сохранены")


def register_routes(app: web.Application) -> None:
    app.router.add_get("/learn", learn_index)
    app.router.add_get("/learn/{subject_id:\\d+}", learn_subject)
    app.router.add_get("/learn/lesson/{lesson_id:\\d+}", learn_lesson)
    app.router.add_get("/learn/lesson/{lesson_id:\\d+}/test", learn_test_start)
    app.router.add_post("/learn/api/test/{attempt_id:\\d+}/answer", learn_test_answer)
    app.router.add_post("/learn/api/test/{attempt_id:\\d+}/finish", learn_test_finish)
    app.router.add_get("/learn/test/{attempt_id:\\d+}/result", learn_test_result)
    app.router.add_get("/uploads/questions/{filename}", uploaded_question_image)

    app.router.add_get("/admin/learn", admin_learn_index)

    app.router.add_post("/admin/learn/subjects/create", admin_create_subject)
    app.router.add_post("/admin/learn/subjects/{subject_id:\\d+}/edit", admin_edit_subject)
    app.router.add_post("/admin/learn/subjects/{subject_id:\\d+}/delete", admin_delete_subject)
    app.router.add_post("/admin/learn/subjects/{subject_id:\\d+}/toggle", admin_toggle_subject)
    app.router.add_post("/admin/learn/subjects/{subject_id:\\d+}/access/grant", admin_grant_access)

    app.router.add_post("/admin/learn/subjects/{subject_id:\\d+}/sections/create", admin_create_section)
    app.router.add_post("/admin/learn/sections/{section_id:\\d+}/edit", admin_edit_section)
    app.router.add_post("/admin/learn/sections/{section_id:\\d+}/delete", admin_delete_section)
    app.router.add_post("/admin/learn/sections/{section_id:\\d+}/lessons/create", admin_create_lesson)

    app.router.add_get("/admin/learn/lessons/{lesson_id:\\d+}/edit", admin_lesson_edit_page)
    app.router.add_post("/admin/learn/lessons/{lesson_id:\\d+}/edit", admin_edit_lesson)
    app.router.add_post("/admin/learn/lessons/{lesson_id:\\d+}/toggle", admin_toggle_lesson)
    app.router.add_post("/admin/learn/lessons/{lesson_id:\\d+}/delete", admin_delete_lesson)
    app.router.add_post("/admin/learn/lessons/{lesson_id:\\d+}/delete_content", admin_delete_lesson_content)
    app.router.add_post("/admin/learn/lessons/{lesson_id:\\d+}/delete_test", admin_delete_lesson_test)
    app.router.add_post("/admin/learn/lessons/{lesson_id:\\d+}/test/replace", admin_replace_lesson_test)

    app.router.add_post("/admin/learn/tests/{test_id:\\d+}/settings", admin_test_settings)

    app.router.add_get("/admin/learn/drafts/{draft_id:\\d+}", admin_view_draft)
    app.router.add_post("/admin/learn/drafts/{draft_id:\\d+}/confirm", admin_confirm_draft)
    app.router.add_post("/admin/learn/drafts/{draft_id:\\d+}/cancel", admin_cancel_draft)
