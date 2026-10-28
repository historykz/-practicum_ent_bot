"""
Админка конструктора тестов: /admin/tests

Администратор вручную создаёт тест на сайте: настройки, вопросы пяти типов,
картинки и пояснения, предпросмотр, публикация. Тест лежит в обычных таблицах
tests/questions/question_options, поэтому после публикации он виден и в боте
(если не нужен только сайт), и на сайте по персональной ссылке.
"""
import asyncio
import logging
import os
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp_jinja2
from aiohttp import web

import config
import database as db
import utils
from services import test_builder as tb
from services import typed_answers as ta
from services import web_tests as wt
from webapp import auth, learning, test_web

logger = logging.getLogger(__name__)

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")
IMAGE_MAX = 5 * 1024 * 1024


def _back(test_id: int, message: str = "", error: str = "", anchor: str = "") -> web.HTTPFound:
    url = f"/admin/tests/{test_id}"
    parts = []
    if message:
        parts.append("message=" + quote(message))
    if error:
        parts.append("error=" + quote(error))
    if parts:
        url += "?" + "&".join(parts)
    if anchor:
        url += "#" + anchor
    return web.HTTPFound(url)


def _save_image_sync(question_id: int, upload) -> str:
    """Сохранить картинку вопроса. Возвращает ошибку или пустую строку."""
    name, blob = upload
    ext = os.path.splitext(name or "")[1].lower()
    if ext not in IMAGE_EXT:
        return "Такой формат картинки не поддерживается (нужен jpg, png, webp или gif)."
    if len(blob) > IMAGE_MAX:
        return "Картинка больше 5 МБ."
    root = Path(config.DB_PATH).resolve().parent / "uploads" / "questions"
    root.mkdir(parents=True, exist_ok=True)
    fname = f"q{question_id}_{int(time.time() * 1000)}{ext}"
    with open(root / fname, "wb") as f:
        f.write(blob)
    db.execute("UPDATE questions SET web_image_path=? WHERE id=?", (f"/uploads/questions/{fname}", question_id))
    return ""


def _upload_of(post):
    v = post.get("image")
    if v is not None and hasattr(v, "file") and getattr(v, "filename", ""):
        return v.filename, v.file.read()
    return None


async def _admin(request: web.Request):
    tg_id = await learning._require_admin(request)
    return tg_id


# ───────────────────────── список и создание ─────────────────────────

async def tests_index(request: web.Request) -> web.Response:
    await _admin(request)
    ctx = await auth.nav_context(request)
    ctx["tests"] = await asyncio.to_thread(tb.list_tests)
    ctx["message"] = request.query.get("message")
    ctx["error"] = request.query.get("error")
    return aiohttp_jinja2.render_template("admin_tests.html", request, ctx)


async def test_create(request: web.Request) -> web.Response:
    tg_id = await _admin(request)
    post = await request.post()
    user = await asyncio.to_thread(utils.get_user_by_tg, tg_id)
    test_id = await asyncio.to_thread(
        tb.create_test, (user or {}).get("id") or 0, post.get("title") or "", post.get("subject") or "",
        post.get("topic") or "", post.get("instruction") or "", post.get("language") or "ru")
    raise _back(test_id, "Тест создан как черновик. Добавьте вопросы и опубликуйте.")


# ───────────────────────── редактор ─────────────────────────

def _editor_data_sync(test_id: int) -> dict:
    test = tb.get_test(test_id)
    if not test:
        return {}
    questions = tb.questions_of(test_id)
    cats = [dict(r) for r in db.fetchall("SELECT id, name FROM test_categories ORDER BY sort_order, id")] \
        if db.fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name='test_categories'") else []
    stats = {
        "done": db.fetchone("SELECT COUNT(*) AS c FROM test_attempts WHERE test_id=? AND status='finished' "
                            "AND COALESCE(via,'')<>'preview'", (test_id,))["c"],
        "active": db.fetchone("SELECT COUNT(*) AS c FROM test_attempts WHERE test_id=? AND "
                              "status IN ('in_progress','idle','paused') AND COALESCE(via,'')<>'preview'",
                              (test_id,))["c"],
    }
    return {
        "test": test, "questions": questions, "categories": cats, "stats": stats,
        "problems": tb.publish_problems(test_id) if test.get("builder_state") != tb.PUBLISHED else [],
        "points_total": sum(q["points"] for q in questions),
        "has_typed": any(q["kind"] in ta.TYPED_TYPES for q in questions),
        "web_effective": wt.web_allowed(test),
        "lesson_linked": bool(wt.linked_lessons(test_id)),
        "type_labels": ta.LABELS,
    }


async def test_edit(request: web.Request) -> web.Response:
    await _admin(request)
    test_id = int(request.match_info["test_id"])
    data = await asyncio.to_thread(_editor_data_sync, test_id)
    if not data:
        raise web.HTTPNotFound(text="Тест не найден")
    ctx = await auth.nav_context(request)
    ctx.update(data)
    ctx["message"] = request.query.get("message")
    ctx["error"] = request.query.get("error")
    return aiohttp_jinja2.render_template("admin_test_edit.html", request, ctx)


async def test_settings(request: web.Request) -> web.Response:
    await _admin(request)
    test_id = int(request.match_info["test_id"])
    post = await request.post()
    ok, err = await asyncio.to_thread(tb.update_settings, test_id, dict(post))
    if not ok:
        raise _back(test_id, error=err)
    raise _back(test_id, "Настройки сохранены")


async def question_add(request: web.Request) -> web.Response:
    await _admin(request)
    test_id = int(request.match_info["test_id"])
    post = await request.post()
    data, err = tb.parse_question_form(post)
    if err:
        raise _back(test_id, error=err, anchor="add")

    def _work():
        qid = tb.add_question(test_id, data)
        up = _upload_of(post)
        return qid, (_save_image_sync(qid, up) if up else "")
    qid, img_err = await asyncio.to_thread(_work)
    raise _back(test_id, "Вопрос добавлен" + (f". Но картинка не сохранена: {img_err}" if img_err else ""),
                anchor=f"q{qid}")


async def question_save(request: web.Request) -> web.Response:
    await _admin(request)
    qid = int(request.match_info["question_id"])
    post = await request.post()
    row = await asyncio.to_thread(db.fetchone, "SELECT test_id FROM questions WHERE id=?", (qid,))
    if not row:
        raise web.HTTPNotFound(text="Вопрос не найден")
    test_id = row["test_id"]
    data, err = tb.parse_question_form(post)
    if err:
        raise _back(test_id, error=err, anchor=f"q{qid}")

    def _work():
        tb.update_question(qid, data)
        if post.get("remove_image") == "1":
            db.execute("UPDATE questions SET web_image_path=NULL WHERE id=?", (qid,))
            return ""
        up = _upload_of(post)
        return _save_image_sync(qid, up) if up else ""
    img_err = await asyncio.to_thread(_work)
    raise _back(test_id, "Вопрос сохранён" + (f". Но картинка не сохранена: {img_err}" if img_err else ""),
                anchor=f"q{qid}")


async def question_delete(request: web.Request) -> web.Response:
    await _admin(request)
    test_id = await asyncio.to_thread(tb.delete_question, int(request.match_info["question_id"]))
    if test_id is None:
        raise web.HTTPNotFound(text="Вопрос не найден")
    raise _back(test_id, "Вопрос удалён")


async def question_move(request: web.Request) -> web.Response:
    await _admin(request)
    post = await request.post()
    qid = int(request.match_info["question_id"])
    test_id = await asyncio.to_thread(tb.move_question, qid, "up" if post.get("dir") == "up" else "down")
    if test_id is None:
        raise web.HTTPNotFound(text="Вопрос не найден")
    raise _back(test_id, anchor=f"q{qid}")


async def question_duplicate(request: web.Request) -> web.Response:
    await _admin(request)
    qid = int(request.match_info["question_id"])
    row = await asyncio.to_thread(db.fetchone, "SELECT test_id FROM questions WHERE id=?", (qid,))
    if not row:
        raise web.HTTPNotFound(text="Вопрос не найден")
    new_id = await asyncio.to_thread(tb.duplicate_question, qid)
    raise _back(row["test_id"], "Вопрос продублирован", anchor=f"q{new_id}")


# ───────────────────────── публикация, дубль, удаление ─────────────────────────

async def test_publish(request: web.Request) -> web.Response:
    await _admin(request)
    test_id = int(request.match_info["test_id"])
    ok, problems = await asyncio.to_thread(tb.publish, test_id)
    if not ok:
        raise _back(test_id, error="Опубликовать нельзя: " + " ".join(problems))
    raise _back(test_id, "Тест опубликован: он виден ученикам.")


async def test_unpublish(request: web.Request) -> web.Response:
    await _admin(request)
    test_id = int(request.match_info["test_id"])
    await asyncio.to_thread(tb.unpublish, test_id)
    raise _back(test_id, "Тест снят с публикации и снова стал черновиком. Начатые попытки сохранены.")


async def test_duplicate(request: web.Request) -> web.Response:
    tg_id = await _admin(request)
    test_id = int(request.match_info["test_id"])
    user = await asyncio.to_thread(utils.get_user_by_tg, tg_id)
    new_id = await asyncio.to_thread(tb.duplicate_test, test_id, (user or {}).get("id") or 0)
    if not new_id:
        raise web.HTTPNotFound(text="Тест не найден")
    raise _back(new_id, "Копия создана как черновик.")


async def test_delete(request: web.Request) -> web.Response:
    await _admin(request)
    test_id = int(request.match_info["test_id"])
    ok = await asyncio.to_thread(tb.delete_test, test_id)
    if not ok:
        raise _back(test_id, error="Удалить нельзя: тест не из конструктора или привязан к уроку.")
    raise web.HTTPFound("/admin/tests?message=" + quote("Тест удалён"))


async def revoke_links(request: web.Request) -> web.Response:
    await _admin(request)
    test_id = int(request.match_info["test_id"])
    n = await asyncio.to_thread(wt.revoke_links, test_id)
    raise _back(test_id, f"Отозвано персональных ссылок: {n}. Ученики получат новые в боте.")


# ───────────────────────── предпросмотр и результаты ─────────────────────────

def _preview_attempt_sync(test_id: int, user: dict) -> dict:
    row = db.fetchone(
        "SELECT * FROM test_attempts WHERE user_id=? AND test_id=? AND via='preview' AND status='in_progress' "
        "ORDER BY id DESC LIMIT 1", (user["id"], test_id))
    if row:
        return dict(row)
    test = tb.get_test(test_id)
    aid = wt.create_attempt(user, test, via="preview")
    return dict(db.fetchone("SELECT * FROM test_attempts WHERE id=?", (aid,))) if aid else {}


async def test_preview(request: web.Request) -> web.Response:
    tg_id = await _admin(request)
    test_id = int(request.match_info["test_id"])
    user = await asyncio.to_thread(utils.get_user_by_tg, tg_id)
    test = await asyncio.to_thread(tb.get_test, test_id)
    if not test or not user:
        raise web.HTTPNotFound(text="Тест не найден")
    if request.query.get("restart") == "1":
        await asyncio.to_thread(
            db.execute, "UPDATE test_attempts SET status='aborted' WHERE user_id=? AND test_id=? AND via='preview' "
            "AND status='in_progress'", (user["id"], test_id))
    attempt = await asyncio.to_thread(_preview_attempt_sync, test_id, user)
    if not attempt:
        raise _back(test_id, error="Предпросмотр невозможен: в тесте нет корректных вопросов.")
    return await test_web.render_runner(request, tg_id, user, test, attempt, preview=True)


async def test_results(request: web.Request) -> web.Response:
    await _admin(request)
    test_id = int(request.match_info["test_id"])
    test = await asyncio.to_thread(tb.get_test, test_id)
    if not test:
        raise web.HTTPNotFound(text="Тест не найден")
    ctx = await auth.nav_context(request)
    ctx["test"] = test
    ctx["rows"] = await asyncio.to_thread(tb.results, test_id)
    return aiohttp_jinja2.render_template("admin_test_results.html", request, ctx)


def register_routes(app: web.Application) -> None:
    r = app.router
    r.add_get("/admin/tests", tests_index)
    r.add_post("/admin/tests/create", test_create)
    r.add_get("/admin/tests/{test_id:\\d+}", test_edit)
    r.add_post("/admin/tests/{test_id:\\d+}/settings", test_settings)
    r.add_post("/admin/tests/{test_id:\\d+}/questions/add", question_add)
    r.add_post("/admin/tests/questions/{question_id:\\d+}/save", question_save)
    r.add_post("/admin/tests/questions/{question_id:\\d+}/delete", question_delete)
    r.add_post("/admin/tests/questions/{question_id:\\d+}/move", question_move)
    r.add_post("/admin/tests/questions/{question_id:\\d+}/duplicate", question_duplicate)
    r.add_post("/admin/tests/{test_id:\\d+}/publish", test_publish)
    r.add_post("/admin/tests/{test_id:\\d+}/unpublish", test_unpublish)
    r.add_post("/admin/tests/{test_id:\\d+}/duplicate", test_duplicate)
    r.add_post("/admin/tests/{test_id:\\d+}/delete", test_delete)
    r.add_post("/admin/tests/{test_id:\\d+}/revoke-links", revoke_links)
    r.add_get("/admin/tests/{test_id:\\d+}/preview", test_preview)
    r.add_get("/admin/tests/{test_id:\\d+}/results", test_results)
