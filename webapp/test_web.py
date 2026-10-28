"""
Прохождение теста на сайте по персональной ссылке.

  /t/<токен>                         страница теста (или результат, или понятный отказ)
  /tests/web/<id теста>              «Открыть на сайте» из списка тестов: выдаёт ссылку и ведёт на неё
  /learn/api/test/<попытка>/question-start   серверный таймер вопроса

Ссылка сама доступа не даёт. Сервер каждый раз проверяет, что вошёл именно тот
ученик, которому она выдана, что тест опубликован, на сайте разрешён и что у
ученика есть доступ (приватность, раздел, оплата, подписка, лимит попыток).
Ссылка, пересланная другому человеку, у него не открывается.
"""
import asyncio
import json
import logging
from typing import Optional
from urllib.parse import quote

import aiohttp_jinja2
from aiohttp import web

import database as db
import utils
from services import web_tests as wt
from webapp import auth, learning

logger = logging.getLogger(__name__)

NOTICES = {
    "bad_link": ("Ссылка недействительна", "Срок действия ссылки мог истечь, или она заменена новой. "
                 "Откройте тест в боте и нажмите «Перейти на сайт»."),
    "foreign": ("Эта ссылка выдана другому ученику", "Персональная ссылка работает только в аккаунте "
               "того, кому её выдали. Откройте тест в своём аккаунте через бота."),
    "not_found": ("Тест не найден", "Тест снят с публикации или удалён."),
    "web_off": ("Тест проходят на платформе", "Для этого теста отключено прохождение на сайте. "
                "Откройте его в боте."),
    "private": ("Нет доступа к тесту", "Этот тест приватный. Если доступ должен быть, обратитесь к администратору."),
    "category": ("Нет доступа к тесту", "Раздел с этим тестом закрыт для вашего аккаунта."),
    "paid": ("Тест платный", "Откройте тест в боте: там можно получить доступ."),
    "banned": ("Тесты временно недоступны", "Доступ к тестам ограничен. Подробности есть в боте."),
    "limit": ("Попытки закончились", "Лимит попыток для этого теста исчерпан."),
    "empty": ("В тесте нет вопросов", "Автор ещё не добавил вопросы или они заполнены с ошибками."),
    "closed": ("Попытка завершена", "Эта попытка уже закончена, а новая для этой ссылки не создаётся. "
               "Чтобы пройти тест ещё раз, получите новую ссылку в боте."),
}


async def _notice(request: web.Request, key: str, status: int = 200, extra: Optional[dict] = None) -> web.Response:
    title, hint = NOTICES[key]
    ctx = await auth.nav_context(request)
    ctx.update({"title": title, "hint": hint, "key": key})
    if extra:
        ctx.update(extra)
    resp = aiohttp_jinja2.render_template("web_test_notice.html", request, ctx)
    resp.set_status(status)
    return resp


def _event(user: Optional[dict], tg_id: Optional[int], event: str, details: str) -> None:
    try:
        from services.account_service import log_event
        log_event((user or {}).get("id"), tg_id, event, details)
    except Exception:
        pass


# ───────────────────────── общая сборка страницы теста ─────────────────────────

def runner_context(user: dict, tg_id: int, test: dict, attempt: dict, preview: bool = False) -> dict:
    """Всё, что нужно learn_test.html: вопросы, уже данные ответы, серверные таймеры, «аренда»."""
    try:
        q_ids = json.loads(attempt["question_order"] or "[]")
    except (ValueError, TypeError):
        q_ids = []
    try:
        options_order = json.loads(attempt["options_order"] or "{}")
    except (ValueError, TypeError):
        options_order = {}
    questions = learning._build_questions_out(q_ids, options_order)
    show_correct = bool(test.get("show_correct"))
    answered = learning._answered_map_sync(attempt["id"], show_correct, explain=True)
    first_open = next((q for q in questions if str(q["id"]) not in answered), None)
    remaining = wt.question_clock(attempt, test, first_open["id"]) if first_open else None
    # перечитываем: question_clock мог записать дедлайн
    fresh = db.fetchone("SELECT * FROM test_attempts WHERE id=?", (attempt["id"],))
    lease = wt.issue_lease(attempt["id"])
    return {
        "attempt_id": attempt["id"],
        "lesson": {"id": 0, "title": test["title"], "url_id": 0},
        "questions": questions,
        "questions_json": json.dumps(questions, ensure_ascii=False).replace("</", "<\\/"),
        "answered_json": json.dumps(answered, ensure_ascii=False).replace("</", "<\\/"),
        "time_per_question": wt.seconds_per_question(test),
        "show_correct": show_correct,
        "is_resume": bool(answered),
        "has_note": False,
        "instruction": (test.get("instruction") or "").strip(),
        "link_mode": True,
        "preview_mode": preview,
        "lease": lease,
        "remaining_ms": remaining,
        "overall_left_ms": wt.overall_left_ms(dict(fresh)) if fresh else None,
    }


async def render_runner(request: web.Request, tg_id: int, user: dict, test: dict, attempt: dict,
                        preview: bool = False) -> web.Response:
    data = await asyncio.to_thread(runner_context, user, tg_id, test, attempt, preview)
    data.update(await auth.nav_context(request))
    data["watermark_svg"] = await asyncio.to_thread(learning._watermark_svg_sync, tg_id)
    resp = aiohttp_jinja2.render_template("learn_test.html", request, data)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ───────────────────────── открыть ссылку ─────────────────────────

def _decide_sync(link: dict, user: dict, tg_id: int) -> dict:
    """Что делать с открытой ссылкой. Только решение, без показа страницы."""
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (link["test_id"],))
    if not test:
        return {"kind": "notice", "key": "not_found", "status": 404}
    test = dict(test)
    state = wt.access_state(user, tg_id, test)
    if state != "ok":
        key = state if state in NOTICES else "not_found"
        return {"kind": "notice", "key": key, "status": 403 if key != "not_found" else 404}

    # Тест урока: вся защита урока (подписка, оплата, стоп-уроки) уже в маршруте
    # урока, туда и отправляем. Ссылка при этом личная и проверена выше.
    lessons = [l for l in wt.linked_lessons(test["id"]) if l["status"] == "open"]
    if lessons:
        if not wt.web_allowed(test) and not wt.find_active_attempt(user["id"], test["id"]):
            return {"kind": "notice", "key": "web_off", "status": 403}
        wt.touch_link(link["id"])
        return {"kind": "redirect", "url": f"/learn/lesson/{lessons[0]['id']}/test"}

    attempt = None
    if link.get("attempt_id"):
        row = db.fetchone("SELECT * FROM test_attempts WHERE id=? AND user_id=? AND test_id=?",
                          (link["attempt_id"], user["id"], test["id"]))
        attempt = dict(row) if row else None
    if attempt and attempt["status"] == "finished":
        wt.touch_link(link["id"])
        return {"kind": "redirect", "url": f"/learn/test/{attempt['id']}/result"}
    if attempt and attempt["status"] not in wt.ACTIVE:
        return {"kind": "notice", "key": "closed", "status": 200}
    if not attempt:
        attempt = wt.find_active_attempt(user["id"], test["id"])
    if attempt:
        # Продолжаем ту же попытку (в том числе начатую в боте): ответы, порядок
        # вопросов и оставшееся время сохраняются, новая попытка не создаётся
        attempt = wt.resume_if_paused(attempt)
        if (attempt.get("via") or "") not in ("link", "preview"):
            db.execute("UPDATE test_attempts SET via='link', handoff_at=? WHERE id=?",
                       (utils.now_iso(), attempt["id"]))
            attempt = dict(db.fetchone("SELECT * FROM test_attempts WHERE id=?", (attempt["id"],)))
    else:
        if not wt.web_allowed(test):
            return {"kind": "notice", "key": "web_off", "status": 403}
        left = wt.attempts_left(user, test)
        if left is not None and left <= 0:
            return {"kind": "notice", "key": "limit", "status": 403}
        attempt_id = wt.create_attempt(user, test, via="link")
        if not attempt_id:
            return {"kind": "notice", "key": "empty", "status": 200}
        attempt = dict(db.fetchone("SELECT * FROM test_attempts WHERE id=?", (attempt_id,)))
    wt.touch_link(link["id"], attempt["id"])
    return {"kind": "run", "test": test, "attempt": attempt}


async def link_runner(request: web.Request) -> web.Response:
    token = request.match_info["token"]
    link = await asyncio.to_thread(wt.resolve_link, token)
    if not link:
        return await _notice(request, "bad_link", 404)
    tg_id = await auth.get_logged_in_tg_id(request)
    if tg_id is None:
        # Не вошёл: показываем вход. В мини-приложении Telegram вход выполнится сам
        # (base.html) и страница перезагрузится уже на ссылке. В браузере человек
        # входит и возвращается сюда же.
        ctx = await auth.nav_context(request)
        ctx["next"] = f"/t/{quote(token)}"
        resp = aiohttp_jinja2.render_template("web_test_login.html", request, ctx)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    user = await asyncio.to_thread(utils.get_user_by_tg, tg_id)
    if not user:
        return await _notice(request, "bad_link", 404)
    if link["user_id"] != user["id"]:
        _event(user, tg_id, "test_link_foreign", f"test={link['test_id']} owner={link['user_id']}")
        return await _notice(request, "foreign", 403)

    decision = await asyncio.to_thread(_decide_sync, link, user, tg_id)
    if decision["kind"] == "notice":
        return await _notice(request, decision["key"], decision.get("status", 200))
    if decision["kind"] == "redirect":
        raise web.HTTPFound(decision["url"])

    test, attempt = decision["test"], decision["attempt"]
    # Обязательные каналы (глобальные и раздела), как в боте
    channels = await asyncio.to_thread(wt.required_channels, test)
    missing = []
    for ch in channels:
        if not await learning._is_subscribed(tg_id, ch, force=request.query.get("recheck") == "1"):
            missing.append({"channel_username": ch, "title": ""})
    if missing:
        ctx = await auth.nav_context(request)
        ctx.update({"channels": missing, "recheck_url": f"{request.path}?recheck=1"})
        return aiohttp_jinja2.render_template("subject_subscribe.html", request, ctx)
    return await render_runner(request, tg_id, user, test, attempt)


# ───────────────────────── «Открыть на сайте» из списка ─────────────────────────

def _issue_for_user_sync(user: dict, tg_id: int, test_id: int) -> Optional[str]:
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))
    if not test:
        return None
    test = dict(test)
    if test.get("status") != "active" or not wt.web_allowed(test):
        return None
    if wt.access_state(user, tg_id, test) != "ok":
        return None
    active = wt.find_active_attempt(user["id"], test_id)
    return wt.create_link(user["id"], test_id, active["id"] if active else None)


async def open_on_site(request: web.Request) -> web.Response:
    tg_id = await learning._require_login(request)
    user = await asyncio.to_thread(utils.get_user_by_tg, tg_id)
    if not user:
        raise web.HTTPFound("/?error=login_required")
    test_id = int(request.match_info["test_id"])
    token = await asyncio.to_thread(_issue_for_user_sync, user, tg_id, test_id)
    if not token:
        return await _notice(request, "web_off", 403)
    raise web.HTTPFound(f"/t/{token}")


# ───────────────────────── серверный таймер вопроса ─────────────────────────

async def question_start(request: web.Request) -> web.Response:
    tg_id = await learning._require_login(request)
    attempt_id = int(request.match_info["attempt_id"])
    try:
        body = await request.json()
        question_id = int(body.get("question_id"))
    except (ValueError, TypeError, AttributeError):
        raise web.HTTPBadRequest(text="bad_request")

    def _work():
        user = utils.get_user_by_tg(tg_id)
        row = db.fetchone("SELECT * FROM test_attempts WHERE id=?", (attempt_id,))
        if not user or not row or row["user_id"] != user["id"]:
            return 403, {"error": "forbidden"}
        attempt = dict(row)
        if attempt["status"] not in ("in_progress", "idle"):
            return 200, {"error": "attempt_finished"}
        if not wt.lease_ok(attempt, request.headers.get("X-Lease")):
            return 409, {"error": "lease_lost"}
        if (attempt.get("via") or "") not in ("link", "preview"):
            return 200, {"remaining_ms": None}        # старые попытки считают время у себя
        try:
            if question_id not in json.loads(attempt["question_order"] or "[]"):
                return 400, {"error": "bad_question"}
        except (ValueError, TypeError):
            return 400, {"error": "bad_question"}
        test = dict(db.fetchone("SELECT * FROM tests WHERE id=?", (attempt["test_id"],)) or {})
        return 200, {"remaining_ms": wt.question_clock(attempt, test, question_id),
                     "overall_left_ms": wt.overall_left_ms(attempt)}

    status, data = await asyncio.to_thread(_work)
    return web.json_response(data, status=status)


def register_routes(app: web.Application) -> None:
    app.router.add_get("/t/{token}", link_runner)
    app.router.add_get("/tests/web/{test_id:\\d+}", open_on_site)
    app.router.add_post("/learn/api/test/{attempt_id:\\d+}/question-start", question_start)
