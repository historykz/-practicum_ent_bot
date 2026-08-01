"""
Симулятор зачётов: банк вопросов по темам, выборка поровну по темам на попытку,
письменные ответы, процент по каждой теме, проходной балл, карточки после сдачи.

Зачёт — это урок с флагом is_zachet=1, поэтому он автоматически участвует в
последовательности стоп-уроков, прогрессе и drag&drop. Банк вопросов лежит в
zachet_questions (по темам), попытки — в zachet_attempts.
"""
import json
import math
import random
import re
from datetime import datetime

import aiohttp_jinja2
from aiohttp import web

import database as db
import utils
from webapp import auth


# ============ Проверка письменного ответа ============

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def _norm(s: str) -> str:
    s = (s or "").strip().lower().replace("ё", "е")
    s = _PUNCT.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def check_answer(user: str, reference: str) -> bool:
    """Нечёткая сверка. Эталон может содержать альтернативы через / или |.
    Совпадение: точное после нормализации, либо число совпадает, либо
    короткий эталон целиком входит в ответ ученика."""
    u = _norm(user)
    if not u:
        return False
    variants = [_norm(v) for v in re.split(r"[/|]", reference) if v.strip()]
    for ref in variants:
        if not ref:
            continue
        if u == ref:
            return True
        # число (дата/год): сравниваем только цифры
        ru = re.sub(r"\D", "", u)
        rr = re.sub(r"\D", "", ref)
        if rr and ru == rr:
            return True
        # короткий эталон (1-3 слова) целиком присутствует в ответе
        if len(ref) >= 3 and (ref in u or u in ref) and len(ref.split()) <= 3:
            return True
    return False


# ============ Парсинг TXT (Тема / Вопрос / Ответ) ============

_TOPIC_RE = re.compile(r"^\s*тема\s*\d*\s*[:.\-)]\s*(.+)$", re.IGNORECASE)
_Q_RE = re.compile(r"^\s*вопрос\s*(\d+)\s*[:.\-)]\s*(.+)$", re.IGNORECASE)
_A_RE = re.compile(r"^\s*ответ\s*(\d+)\s*[:.\-)]\s*(.+)$", re.IGNORECASE)


def parse_zachet_txt(text: str):
    """Возвращает (topics: [{'topic':..,'questions':[{'q','a','num'}]}], errors: [str])."""
    topics = []
    errors = []
    cur = None
    pending_q = None  # (num, text)
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _TOPIC_RE.match(line)
        if m:
            cur = {"topic": m.group(1).strip(), "questions": []}
            topics.append(cur)
            pending_q = None
            continue
        mq = _Q_RE.match(line)
        if mq:
            if cur is None:
                cur = {"topic": "Без темы", "questions": []}
                topics.append(cur)
            pending_q = (mq.group(1), mq.group(2).strip())
            continue
        ma = _A_RE.match(line)
        if ma:
            if pending_q is None:
                errors.append(f"Ответ без вопроса: {line[:40]}")
                continue
            if pending_q[0] != ma.group(1):
                errors.append(f"Номер вопроса и ответа не совпал (В{pending_q[0]}/О{ma.group(1)})")
            cur["questions"].append({
                "num": pending_q[0], "q": pending_q[1], "a": ma.group(1) and ma.group(2).strip()})
            pending_q = None
            continue
        # строка-продолжение вопроса без ключевого слова — игнор (или добавить к вопросу)
    if pending_q is not None:
        errors.append(f"Вопрос без ответа: {pending_q[1][:40]}")
    topics = [t for t in topics if t["questions"]]
    return topics, errors


TEMPLATE_TXT = """Тема 1: Тюркский период
Вопрос 1: В каком году Бумын каган выиграл у Жужаней?
Ответ 1: 552
Вопрос 2: Кто окончательно разгромил Жужаней?
Ответ 2: Мукан каган

Тема 2: Западно-Тюркский каганат
Вопрос 1: Как называлось объединение западных племён?
Ответ 1: он ок будун
Вопрос 2: Столица Западно-Тюркского каганата?
Ответ 2: Суяб
"""

AI_PROMPT = """Сгенерируй банк вопросов для зачёта по предмету [ПРЕДМЕТ/ТЕМА КУРСА] строго в следующем текстовом формате, без каких-либо пояснений, markdown-разметки или лишнего текста до и после:

Тема 1: [Название темы 1]
Вопрос 1: [Текст вопроса]
Ответ 1: [Краткий точный ответ]
Вопрос 2: [Текст вопроса]
Ответ 2: [Краткий точный ответ]
...
Тема 2: [Название темы 2]
Вопрос 1: [Текст вопроса]
Ответ 1: [Краткий точный ответ]
...

Требования:
1. Тем должно быть [ЧИСЛО ТЕМ], темы: [ПЕРЕЧИСЛИТЬ НАЗВАНИЯ ТЕМ].
2. В каждой теме — ровно [ЧИСЛО] вопросов, нумерация вопросов внутри каждой темы начинается заново с 1.
3. Ответы короткие и однозначные (дата, имя, термин, факт) — без развёрнутых объяснений, чтобы их можно было автоматически сверять.
4. Не повторяй вопросы между темами и внутри одной темы.
5. Вопросы разного уровня сложности внутри темы.
6. Строго соблюдай ключевые слова "Тема N:", "Вопрос N:", "Ответ N:" — менять их формулировку нельзя."""


# ============ Данные банка ============

def bank_topics_sync(lesson_id: int) -> dict:
    """{тема: [ {id,q,a} ... ]} в порядке добавления."""
    rows = db.fetchall(
        "SELECT * FROM zachet_questions WHERE lesson_id=? ORDER BY order_num, id",
        (lesson_id,))
    out = {}
    for r in rows:
        out.setdefault(r["topic"], []).append(
            {"id": r["id"], "q": r["question"], "a": r["answer"]})
    return out


def bank_total_sync(lesson_id: int) -> int:
    row = db.fetchone("SELECT COUNT(*) c FROM zachet_questions WHERE lesson_id=?", (lesson_id,))
    return row["c"] if row else 0


def zachet_passed_sync(lesson_id: int, tg_id: int) -> bool:
    row = db.fetchone(
        "SELECT 1 FROM zachet_attempts WHERE lesson_id=? AND user_tg_id=? "
        "AND status='finished' AND passed=1 LIMIT 1", (lesson_id, tg_id))
    return bool(row)


# ============ Выборка вопросов на попытку ============

def _seen_ids_sync(lesson_id: int, tg_id: int) -> set:
    seen = set()
    for r in db.fetchall(
            "SELECT question_ids FROM zachet_attempts WHERE lesson_id=? AND user_tg_id=?",
            (lesson_id, tg_id)):
        try:
            seen.update(json.loads(r["question_ids"] or "[]"))
        except (ValueError, TypeError):
            pass
    return seen


def build_attempt_sync(lesson_id: int, tg_id: int):
    """Собирает попытку: поровну по темам, без повторов (пока есть неиспользованные)."""
    lesson = db.fetchone("SELECT * FROM lessons WHERE id=?", (lesson_id,))
    if not lesson:
        return {"error": "not_found", "message": "Зачёт не найден"}
    topics = bank_topics_sync(lesson_id)
    if not topics:
        return {"error": "empty", "message": "В зачёте пока нет вопросов"}

    per_attempt = lesson["zachet_per_attempt"] or 20
    n_topics = len(topics)
    per_topic = max(1, per_attempt // n_topics)  # поровну; остаток игнорируем

    seen = _seen_ids_sync(lesson_id, tg_id)
    chosen = []
    for topic, qs in topics.items():
        fresh = [q for q in qs if q["id"] not in seen]
        pool = fresh if len(fresh) >= per_topic else qs[:]  # банк темы исчерпан → берём заново
        random.shuffle(pool)
        take = pool[:per_topic]
        if len(take) < per_topic:  # вопросов в теме физически меньше — добираем с повтором
            extra = qs[:]
            random.shuffle(extra)
            while len(take) < per_topic and extra:
                take.append(extra.pop())
        for q in take:
            chosen.append({"id": q["id"], "topic": topic, "q": q["q"]})

    # Порядок: сгруппировано по темам для удобства чтения
    chosen.sort(key=lambda x: x["topic"])
    qids = [c["id"] for c in chosen]
    db.execute(
        "INSERT INTO zachet_attempts (lesson_id, user_tg_id, question_ids, status) "
        "VALUES (?, ?, ?, 'in_progress')",
        (lesson_id, tg_id, json.dumps(qids)))
    attempt_id = db.fetchone("SELECT last_insert_rowid() id")["id"]
    return {"attempt_id": attempt_id, "questions": chosen}


def grade_attempt_sync(attempt_id: int, tg_id: int, user_answers: dict):
    att = db.fetchone("SELECT * FROM zachet_attempts WHERE id=?", (attempt_id,))
    if not att or att["user_tg_id"] != tg_id:
        return {"error": "forbidden"}
    lesson = db.fetchone("SELECT * FROM lessons WHERE id=?", (att["lesson_id"],))
    if att["status"] == "finished":
        return _result_payload(att, lesson)

    try:
        qids = json.loads(att["question_ids"] or "[]")
    except (ValueError, TypeError):
        qids = []
    per_topic = {}
    answers_store = {}
    total = correct = 0
    for qid in qids:
        q = db.fetchone("SELECT * FROM zachet_questions WHERE id=?", (qid,))
        if not q:
            continue
        ua = (user_answers or {}).get(str(qid)) or (user_answers or {}).get(qid) or ""
        ok = check_answer(ua, q["answer"])
        total += 1
        correct += 1 if ok else 0
        t = per_topic.setdefault(q["topic"], {"total": 0, "correct": 0})
        t["total"] += 1
        t["correct"] += 1 if ok else 0
        answers_store[str(qid)] = {"user": ua, "ok": 1 if ok else 0}

    topic_thr = lesson["zachet_topic_threshold"] or 65
    pass_pct = lesson["zachet_pass_percent"] or 70
    for t in per_topic.values():
        t["percent"] = round(t["correct"] / t["total"] * 100) if t["total"] else 0
        t["weak"] = t["percent"] < topic_thr
    percent = round(correct / total * 100, 1) if total else 0
    passed = 1 if percent >= pass_pct else 0

    db.execute(
        "UPDATE zachet_attempts SET answers_json=?, per_topic_json=?, total=?, correct=?, "
        "percent=?, passed=?, status='finished', finished_at=? WHERE id=?",
        (json.dumps(answers_store, ensure_ascii=False), json.dumps(per_topic, ensure_ascii=False),
         total, correct, percent, passed, datetime.utcnow().isoformat(timespec="seconds"),
         attempt_id))
    att = db.fetchone("SELECT * FROM zachet_attempts WHERE id=?", (attempt_id,))
    return _result_payload(att, lesson)


def _result_payload(att, lesson):
    try:
        per_topic = json.loads(att["per_topic_json"] or "{}")
    except (ValueError, TypeError):
        per_topic = {}
    return {
        "per_topic": per_topic,
        "percent": att["percent"],
        "passed": bool(att["passed"]),
        "pass_percent": lesson["zachet_pass_percent"] or 70,
        "topic_threshold": lesson["zachet_topic_threshold"] or 65,
    }


# ============ Роуты: студент ============

async def _login(request):
    tg_id = await auth.get_logged_in_tg_id(request)
    if tg_id is None:
        raise web.HTTPFound("/?error=login_required")
    return tg_id


def _zachet_access_sync(lesson_id: int, tg_id: int):
    """Проверка доступа к зачёту (как к уроку). Возвращает lesson или None."""
    from webapp import learning
    lesson = db.fetchone("SELECT * FROM lessons WHERE id=? AND is_zachet=1", (lesson_id,))
    if not lesson:
        return None
    section = db.fetchone("SELECT * FROM sections WHERE id=?", (lesson["section_id"],))
    if not section:
        return None
    if not learning._has_subject_access_sync(section["subject_id"], tg_id) \
            and not utils.is_site_admin(tg_id):
        return None
    return dict(lesson)


async def zachet_page(request):
    tg_id = await _login(request)
    import asyncio
    lesson_id = int(request.match_info["lesson_id"])
    lesson = await asyncio.to_thread(_zachet_access_sync, lesson_id, tg_id)
    if not lesson:
        raise web.HTTPNotFound(text="Зачёт не найден или нет доступа")
    ctx = await auth.nav_context(request)
    ctx["lesson"] = lesson
    ctx["bank_total"] = await asyncio.to_thread(bank_total_sync, lesson_id)
    ctx["topics_count"] = len(await asyncio.to_thread(bank_topics_sync, lesson_id))
    ctx["passed"] = await asyncio.to_thread(zachet_passed_sync, lesson_id, tg_id)
    return aiohttp_jinja2.render_template("zachet.html", request, ctx)


async def zachet_start(request):
    import asyncio
    tg_id = await _login(request)
    lesson_id = int(request.match_info["lesson_id"])
    if not await asyncio.to_thread(_zachet_access_sync, lesson_id, tg_id):
        return web.json_response({"error": "forbidden", "message": "Нет доступа"}, status=403)
    data = await asyncio.to_thread(build_attempt_sync, lesson_id, tg_id)
    return web.json_response(data)


async def zachet_submit(request):
    import asyncio
    tg_id = await _login(request)
    attempt_id = int(request.match_info["attempt_id"])
    try:
        body = await request.json()
    except Exception:
        body = {}
    res = await asyncio.to_thread(grade_attempt_sync, attempt_id, tg_id, body.get("answers") or {})
    if res.get("error"):
        return web.json_response(res, status=403)
    return web.json_response(res)


# ============ Роуты: карточки из зачёта (после сдачи) ============

async def zachet_cards(request):
    import asyncio
    tg_id = await _login(request)
    lesson_id = int(request.match_info["lesson_id"])
    lesson = await asyncio.to_thread(_zachet_access_sync, lesson_id, tg_id)
    if not lesson:
        raise web.HTTPNotFound()
    passed = await asyncio.to_thread(zachet_passed_sync, lesson_id, tg_id)
    is_adm = await asyncio.to_thread(utils.is_site_admin, tg_id)
    if not passed and not is_adm:
        raise web.HTTPFound(f"/learn/lesson/{lesson_id}/zachet?error=need_pass")

    def _cards():
        rows = db.fetchall(
            "SELECT question q, answer a FROM zachet_questions WHERE lesson_id=? ORDER BY id",
            (lesson_id,))
        return [{"text": r["q"], "answer": r["a"], "web_image_path": None} for r in rows]

    cards = await asyncio.to_thread(_cards)
    ctx = await auth.nav_context(request)
    ctx["lesson"] = lesson
    ctx["questions_json"] = json.dumps(cards, ensure_ascii=False)
    from webapp import learning
    ctx["watermark_svg"] = await asyncio.to_thread(learning._watermark_svg_sync, tg_id)
    return aiohttp_jinja2.render_template("flashcards.html", request, ctx)


# ============ Роуты: админ ============

async def _require_admin(request):
    from webapp import learning
    return await learning._require_admin(request)


async def admin_zachet_page(request):
    import asyncio
    await _require_admin(request)
    lesson_id = int(request.match_info["lesson_id"])

    def _data():
        lesson = db.fetchone("SELECT * FROM lessons WHERE id=?", (lesson_id,))
        topics = bank_topics_sync(lesson_id)
        return lesson, topics

    lesson, topics = await asyncio.to_thread(_data)
    if not lesson:
        raise web.HTTPNotFound()
    ctx = await auth.nav_context(request)
    ctx["lesson"] = dict(lesson)
    ctx["topics"] = [{"topic": t, "questions": qs} for t, qs in topics.items()]
    ctx["bank_total"] = sum(len(qs) for qs in topics.values())
    ctx["message"] = request.query.get("message")
    ctx["ai_prompt"] = AI_PROMPT
    return aiohttp_jinja2.render_template("admin_zachet.html", request, ctx)


async def admin_zachet_template(request):
    await _require_admin(request)
    return web.Response(
        body=TEMPLATE_TXT.encode("utf-8"),
        headers={"Content-Type": "text/plain; charset=utf-8",
                 "Content-Disposition": 'attachment; filename="zachet_template.txt"'})


async def admin_zachet_import(request):
    """Импорт txt: парсим и СРАЗУ сохраняем (превью в этой же странице списком)."""
    import asyncio
    await _require_admin(request)
    lesson_id = int(request.match_info["lesson_id"])
    data = await request.post()
    field = data.get("zachet_file")
    text = ""
    if field is not None and hasattr(field, "file"):
        text = field.file.read().decode("utf-8", errors="replace")
    else:
        text = data.get("zachet_text") or ""
    replace = data.get("replace") == "on"
    topics, errors = parse_zachet_txt(text)

    def _save():
        if replace:
            db.execute("DELETE FROM zachet_questions WHERE lesson_id=?", (lesson_id,))
        last = db.fetchone(
            "SELECT COALESCE(MAX(order_num),0) m FROM zachet_questions WHERE lesson_id=?",
            (lesson_id,))
        order = (last["m"] if last else 0) or 0
        n = 0
        for t in topics:
            for q in t["questions"]:
                if not q.get("q") or not q.get("a"):
                    continue
                order += 1
                db.execute(
                    "INSERT INTO zachet_questions (lesson_id, topic, question, answer, order_num) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (lesson_id, t["topic"], q["q"], q["a"], order))
                n += 1
        return n

    n = await asyncio.to_thread(_save)
    from urllib.parse import quote
    msg = f"Импортировано вопросов: {n}"
    if errors:
        msg += f"; предупреждений: {len(errors)}"
    raise web.HTTPFound(f"/admin/learn/lessons/{lesson_id}/zachet?message={quote(msg)}")


async def admin_zachet_settings(request):
    import asyncio
    await _require_admin(request)
    lesson_id = int(request.match_info["lesson_id"])
    data = await request.post()

    def _int(name, lo, hi, dflt):
        try:
            return max(lo, min(hi, int((data.get(name) or "").strip())))
        except (ValueError, TypeError):
            return dflt
    per = _int("zachet_per_attempt", 1, 200, 20)
    thr = _int("zachet_topic_threshold", 1, 100, 65)
    pass_pct = _int("zachet_pass_percent", 1, 100, 70)
    await asyncio.to_thread(
        db.execute,
        "UPDATE lessons SET is_zachet=1, zachet_per_attempt=?, zachet_topic_threshold=?, "
        "zachet_pass_percent=? WHERE id=?",
        (per, thr, pass_pct, lesson_id))
    raise web.HTTPFound(f"/admin/learn/lessons/{lesson_id}/zachet?message=Настройки сохранены")


async def admin_zachet_clear(request):
    import asyncio
    await _require_admin(request)
    lesson_id = int(request.match_info["lesson_id"])
    await asyncio.to_thread(
        db.execute, "DELETE FROM zachet_questions WHERE lesson_id=?", (lesson_id,))
    raise web.HTTPFound(f"/admin/learn/lessons/{lesson_id}/zachet?message=Банк очищен")


async def admin_zachet_move(request):
    """Передвинуть вопросы одной темы в другой урок-зачёт."""
    import asyncio
    await _require_admin(request)
    lesson_id = int(request.match_info["lesson_id"])
    data = await request.post()
    topic = (data.get("topic") or "").strip()
    target = (data.get("target_lesson_id") or "").strip()
    if not topic or not target.isdigit():
        raise web.HTTPFound(f"/admin/learn/lessons/{lesson_id}/zachet?message=Не указана цель")
    await asyncio.to_thread(
        db.execute,
        "UPDATE zachet_questions SET lesson_id=? WHERE lesson_id=? AND topic=?",
        (int(target), lesson_id, topic))
    from urllib.parse import quote
    raise web.HTTPFound(
        f"/admin/learn/lessons/{lesson_id}/zachet?message={quote('Тема перенесена в урок ' + target)}")


def register_routes(app):
    app.router.add_get("/learn/lesson/{lesson_id:\\d+}/zachet", zachet_page)
    app.router.add_post("/learn/api/zachet/{lesson_id:\\d+}/start", zachet_start)
    app.router.add_post("/learn/api/zachet/attempt/{attempt_id:\\d+}/submit", zachet_submit)
    app.router.add_get("/learn/lesson/{lesson_id:\\d+}/zachet-cards", zachet_cards)
    # админ
    app.router.add_get("/admin/learn/lessons/{lesson_id:\\d+}/zachet", admin_zachet_page)
    app.router.add_get("/admin/learn/zachet-template.txt", admin_zachet_template)
    app.router.add_post("/admin/learn/lessons/{lesson_id:\\d+}/zachet/import", admin_zachet_import)
    app.router.add_post("/admin/learn/lessons/{lesson_id:\\d+}/zachet/settings", admin_zachet_settings)
    app.router.add_post("/admin/learn/lessons/{lesson_id:\\d+}/zachet/clear", admin_zachet_clear)
    app.router.add_post("/admin/learn/lessons/{lesson_id:\\d+}/zachet/move", admin_zachet_move)
