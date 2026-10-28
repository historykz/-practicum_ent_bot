"""
Конструктор тестов: ручное создание и правка тестов на сайте.

Тест, вопросы и варианты лежат в тех же таблицах (tests, questions,
question_options), что и у всего остального: бот, уроки, статистика и
личные ссылки работают с ними как с любым другим тестом. Конструктор только
добавляет: тему, инструкцию, черновик, тип вопроса, баллы, лимит времени и
переключатель «Разрешить прохождение на сайте».

Тест остаётся черновиком (status='hidden', builder_state='draft'), пока автор
его не опубликует: в каталогах бота и сайта он не виден. Опубликовать можно только
тест без ошибок, и список ошибок показывается автору.
"""
import json
import logging
from typing import Optional

import database as db
import utils
from services import typed_answers as ta

log = logging.getLogger(__name__)

MIN_OPTIONS, MAX_OPTIONS = 2, 10
MAX_POINTS = 100
DRAFT, PUBLISHED = "draft", "published"


# ───────────────────────── тесты ─────────────────────────

def _int(value, default=0, lo=None, hi=None) -> int:
    try:
        v = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def get_test(test_id: int) -> Optional[dict]:
    row = db.fetchone("SELECT * FROM tests WHERE id=?", (int(test_id),))
    return dict(row) if row else None


def is_builder_test(test: Optional[dict]) -> bool:
    return bool(test) and (test.get("source") or "") == "builder"


def list_tests() -> list:
    rows = db.fetchall(
        "SELECT t.*, "
        "(SELECT COUNT(*) FROM questions q WHERE q.test_id=t.id) AS q_count, "
        "(SELECT COUNT(*) FROM test_attempts a WHERE a.test_id=t.id AND a.status='finished' "
        "  AND COALESCE(a.via,'')<>'preview') AS done_count, "
        "(SELECT COUNT(*) FROM test_attempts a WHERE a.test_id=t.id "
        "  AND a.status IN ('in_progress','idle','paused') AND COALESCE(a.via,'')<>'preview') AS active_count "
        "FROM tests t WHERE t.source='builder' ORDER BY t.id DESC")
    return [dict(r) for r in rows]


def create_test(created_by: int, title: str, subject: str = "", topic: str = "",
                instruction: str = "", language: str = "ru") -> int:
    title = (title or "").strip()[:200] or "Новый тест"
    cur = db.execute(
        "INSERT INTO tests (title, subject, topic, instruction, language, status, source, builder_state, "
        "show_correct, show_explanation, show_results, shuffle_questions, shuffle_options, "
        "time_per_question, attempts_limit, created_by, allow_web) "
        "VALUES (?,?,?,?,?, 'hidden', 'builder', 'draft', 1, 1, 1, 0, 1, 0, 0, ?, 1)",
        (title, (subject or "").strip()[:120], (topic or "").strip()[:200],
         (instruction or "").strip()[:2000], "kz" if language == "kz" else "ru", int(created_by or 0)))
    return cur.lastrowid


def update_settings(test_id: int, form: dict) -> tuple:
    """Сохранить настройки теста. Возвращает (ok, ошибка)."""
    test = get_test(test_id)
    if not test:
        return False, "Тест не найден."
    title = (form.get("title") or "").strip()
    if not title:
        return False, "Укажите название теста."

    def flag(name):
        return 1 if str(form.get(name) or "").lower() in ("on", "1", "true", "yes") else 0

    lang = "kz" if form.get("language") == "kz" else "ru"
    cat = _int(form.get("category_id"), 0)
    if cat and not db.fetchone("SELECT id FROM test_categories WHERE id=?", (cat,)):
        cat = 0
    db.execute(
        "UPDATE tests SET title=?, subject=?, topic=?, instruction=?, language=?, category_id=?, "
        "time_per_question=?, time_limit_min=?, attempts_limit=?, shuffle_questions=?, shuffle_options=?, "
        "show_results=?, show_correct=?, show_explanation=?, first_attempt_only=?, allow_web=?, is_private=?, "
        "updated_at=? WHERE id=?",
        (title[:200], (form.get("subject") or "").strip()[:120], (form.get("topic") or "").strip()[:200],
         (form.get("instruction") or "").strip()[:2000], lang, cat or None,
         _int(form.get("time_per_question"), 0, 0, 3600), _int(form.get("time_limit_min"), 0, 0, 600),
         _int(form.get("attempts_limit"), 0, 0, 100), flag("shuffle_questions"), flag("shuffle_options"),
         flag("show_results"), flag("show_correct"), flag("show_correct"),
         flag("first_attempt_only"), flag("allow_web"), flag("is_private"), utils.now_iso(), int(test_id)))
    return True, ""


def delete_test(test_id: int) -> bool:
    test = get_test(test_id)
    if not test or not is_builder_test(test):
        return False
    if db.fetchone("SELECT 1 FROM lessons WHERE test_id=?", (int(test_id),)):
        return False           # тест привязан к уроку: сначала отвяжите
    db.execute("DELETE FROM tests WHERE id=?", (int(test_id),))
    return True


def duplicate_test(test_id: int, created_by: int) -> Optional[int]:
    src = get_test(test_id)
    if not src:
        return None
    new_id = create_test(created_by, f"{src['title']} (копия)", src.get("subject") or "",
                         src.get("topic") or "", src.get("instruction") or "", src.get("language") or "ru")
    db.execute(
        "UPDATE tests SET category_id=?, time_per_question=?, time_limit_min=?, attempts_limit=?, "
        "shuffle_questions=?, shuffle_options=?, show_results=?, show_correct=?, show_explanation=?, "
        "first_attempt_only=?, allow_web=?, is_private=? WHERE id=?",
        (src.get("category_id"), src.get("time_per_question"), src.get("time_limit_min"),
         src.get("attempts_limit"), src.get("shuffle_questions"), src.get("shuffle_options"),
         src.get("show_results"), src.get("show_correct"), src.get("show_explanation"),
         src.get("first_attempt_only"), src.get("allow_web"), src.get("is_private"), new_id))
    for q in db.fetchall("SELECT * FROM questions WHERE test_id=? ORDER BY order_num, id", (int(test_id),)):
        duplicate_question(q["id"], new_id)
    return new_id


# ───────────────────────── вопросы ─────────────────────────

def question_view(q: dict) -> dict:
    """Вопрос для редактора: тип, данные, варианты и короткая сводка."""
    q = dict(q)
    kind = ta.question_kind(q)
    payload = ta.load_payload(q)
    opts = [dict(o) for o in db.fetchall(
        "SELECT id, text, is_correct FROM question_options WHERE question_id=? ORDER BY order_num, id", (q["id"],))]
    if not kind:
        kind = ta.MULTIPLE if sum(1 for o in opts if o["is_correct"]) > 1 else ta.SINGLE
    q["kind"] = kind
    q["kind_label"] = ta.LABELS.get(kind, kind)
    q["options"] = opts
    q["payload_obj"] = payload
    q["points"] = ta.points_of(q)
    if kind == ta.TEXT:
        q["summary"] = " / ".join(payload.get("answers") or [])
    elif kind == ta.MATCHING:
        q["summary"] = "; ".join(f"{p['l']} → {p['r']}" for p in payload.get("pairs") or [])
    elif kind == ta.ORDERING:
        q["summary"] = " → ".join(payload.get("items") or [])
    else:
        q["summary"] = "; ".join(("✔ " if o["is_correct"] else "") + o["text"] for o in opts)
    q["image_url"] = q.get("web_image_path") or ""
    q["problem"] = question_problem(q)
    return q


def questions_of(test_id: int) -> list:
    return [question_view(q) for q in db.fetchall(
        "SELECT * FROM questions WHERE test_id=? ORDER BY order_num, id", (int(test_id),))]


def question_problem(q: dict) -> str:
    """Чем вопрос плох: пусто, если всё в порядке."""
    if not (q.get("text") or "").strip():
        return "нет текста вопроса"
    kind = q.get("kind") or ta.question_kind(q)
    if kind in ta.TYPED_TYPES:
        _p, err = ta.clean_payload(kind, ta.load_payload(q))
        return err
    opts = q.get("options")
    if opts is None:
        opts = [dict(o) for o in db.fetchall(
            "SELECT is_correct FROM question_options WHERE question_id=?", (q["id"],))]
    if len(opts) < MIN_OPTIONS:
        return "меньше двух вариантов ответа"
    correct = sum(1 for o in opts if o["is_correct"])
    if not correct:
        return "не отмечен правильный ответ"
    if kind == ta.SINGLE and correct != 1:
        return "у вопроса с одним ответом отмечено несколько правильных"
    if kind == ta.MULTIPLE and correct < 2:
        return "у вопроса с несколькими ответами отмечен только один правильный"
    return ""


def parse_question_form(form) -> tuple:
    """Данные вопроса из формы редактора. Возвращает (данные, ошибка).

    form — MultiDict (getall для повторяющихся полей) или обычный dict."""
    def getall(name):
        if hasattr(form, "getall"):
            return [str(v) for v in form.getall(name, [])]
        v = form.get(name)
        return [str(x) for x in v] if isinstance(v, (list, tuple)) else ([str(v)] if v is not None else [])

    def one(name, default=""):
        vals = getall(name)
        return vals[0] if vals else default

    kind = one("q_type", ta.SINGLE)
    if kind not in ta.ALL_TYPES:
        return None, "Неизвестный тип вопроса."
    text = one("text").strip()
    if not text:
        return None, "Введите текст вопроса."
    out = {"kind": kind, "text": text[:2000], "explanation": one("explanation").strip()[:1500],
           "topic": one("topic").strip()[:200], "points": _int(one("points"), 1, 1, MAX_POINTS)}

    if kind in ta.CHOICE_TYPES:
        texts = [t.strip() for t in getall("opt_text")]
        marks = set(getall("opt_correct"))
        options = []
        for i, t in enumerate(texts):
            if t:
                options.append({"text": t[:500], "correct": str(i) in marks})
        if len(options) < MIN_OPTIONS:
            return None, "Нужно минимум два варианта ответа."
        if len(options) > MAX_OPTIONS:
            return None, f"Вариантов не больше {MAX_OPTIONS}."
        correct = sum(1 for o in options if o["correct"])
        if not correct:
            return None, "Отметьте правильный ответ."
        if kind == ta.SINGLE and correct != 1:
            return None, "В вопросе с одним правильным ответом отметьте ровно один вариант."
        if kind == ta.MULTIPLE and correct < 2:
            return None, "В вопросе с несколькими ответами отметьте минимум два правильных."
        out["options"] = options
        return out, ""

    if kind == ta.TEXT:
        raw = {"answers": [l for l in one("text_answers").splitlines()],
               "case_sensitive": one("case_sensitive") == "on", "numeric": one("numeric") == "on",
               "tolerance": one("tolerance")}
    elif kind == ta.MATCHING:
        lefts, rights = getall("pair_l"), getall("pair_r")
        raw = {"pairs": [{"l": l, "r": r} for l, r in zip(lefts, rights) if (l or "").strip() or (r or "").strip()],
               "extra": [l for l in one("extra_right").splitlines()]}
    else:
        raw = {"items": [l for l in one("order_items").splitlines()]}
    payload, err = ta.clean_payload(kind, raw)
    if err:
        return None, err
    out["payload"] = payload
    return out, ""


def _save_options(qid: int, options: list) -> None:
    """Варианты обновляются на месте: id у неизменённых позиций сохраняются, так что
    старые попытки по-прежнему показывают, что именно выбрал ученик."""
    existing = [r["id"] for r in db.fetchall(
        "SELECT id FROM question_options WHERE question_id=? ORDER BY order_num, id", (qid,))]
    for i, o in enumerate(options):
        if i < len(existing):
            db.execute("UPDATE question_options SET text=?, is_correct=?, order_num=? WHERE id=?",
                       (o["text"], 1 if o["correct"] else 0, i, existing[i]))
        else:
            db.execute("INSERT INTO question_options (question_id, text, is_correct, order_num) VALUES (?,?,?,?)",
                       (qid, o["text"], 1 if o["correct"] else 0, i))
    for oid in existing[len(options):]:
        db.execute("DELETE FROM question_options WHERE id=?", (oid,))


def add_question(test_id: int, data: dict) -> int:
    nxt = db.fetchone("SELECT COALESCE(MAX(order_num), 0) + 1 AS n FROM questions WHERE test_id=?", (int(test_id),))["n"]
    cur = db.execute(
        "INSERT INTO questions (test_id, text, explanation, score, topic, source_type, order_num, q_type, payload) "
        "VALUES (?,?,?,?,?, 'manual', ?, ?, ?)",
        (int(test_id), data["text"], data.get("explanation") or "", data["points"], data.get("topic") or "",
         nxt, data["kind"], json.dumps(data["payload"], ensure_ascii=False) if data.get("payload") else None))
    qid = cur.lastrowid
    if data["kind"] in ta.CHOICE_TYPES:
        _save_options(qid, data["options"])
    return qid


def update_question(qid: int, data: dict) -> bool:
    row = db.fetchone("SELECT id FROM questions WHERE id=?", (int(qid),))
    if not row:
        return False
    db.execute(
        "UPDATE questions SET text=?, explanation=?, score=?, topic=?, q_type=?, payload=? WHERE id=?",
        (data["text"], data.get("explanation") or "", data["points"], data.get("topic") or "", data["kind"],
         json.dumps(data["payload"], ensure_ascii=False) if data.get("payload") else None, int(qid)))
    if data["kind"] in ta.CHOICE_TYPES:
        _save_options(int(qid), data["options"])
    else:
        db.execute("DELETE FROM question_options WHERE question_id=?", (int(qid),))
    return True


def delete_question(qid: int) -> Optional[int]:
    row = db.fetchone("SELECT test_id FROM questions WHERE id=?", (int(qid),))
    if not row:
        return None
    db.execute("DELETE FROM questions WHERE id=?", (int(qid),))
    return row["test_id"]


def move_question(qid: int, direction: str) -> Optional[int]:
    row = db.fetchone("SELECT id, test_id FROM questions WHERE id=?", (int(qid),))
    if not row:
        return None
    ids = [r["id"] for r in db.fetchall(
        "SELECT id FROM questions WHERE test_id=? ORDER BY order_num, id", (row["test_id"],))]
    i = ids.index(row["id"])
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
    for n, qid_ in enumerate(ids, 1):
        db.execute("UPDATE questions SET order_num=? WHERE id=?", (n, qid_))
    return row["test_id"]


def duplicate_question(qid: int, to_test_id: Optional[int] = None) -> Optional[int]:
    q = db.fetchone("SELECT * FROM questions WHERE id=?", (int(qid),))
    if not q:
        return None
    q = dict(q)
    target = int(to_test_id or q["test_id"])
    nxt = db.fetchone("SELECT COALESCE(MAX(order_num), 0) + 1 AS n FROM questions WHERE test_id=?", (target,))["n"]
    cur = db.execute(
        "INSERT INTO questions (test_id, text, explanation, score, topic, source_type, order_num, q_type, payload, "
        "web_image_path) VALUES (?,?,?,?,?, 'manual', ?, ?, ?, ?)",
        (target, q["text"], q.get("explanation") or "", q.get("score") or 1, q.get("topic") or "", nxt,
         q.get("q_type"), q.get("payload"), q.get("web_image_path")))
    new_id = cur.lastrowid
    for o in db.fetchall("SELECT text, is_correct, order_num FROM question_options WHERE question_id=? "
                         "ORDER BY order_num, id", (q["id"],)):
        db.execute("INSERT INTO question_options (question_id, text, is_correct, order_num) VALUES (?,?,?,?)",
                   (new_id, o["text"], o["is_correct"], o["order_num"]))
    return new_id


# ───────────────────────── публикация ─────────────────────────

def publish_problems(test_id: int) -> list:
    """Что мешает опубликовать тест. Пустой список — можно публиковать."""
    test = get_test(test_id)
    if not test:
        return ["Тест не найден."]
    problems = []
    if not (test.get("title") or "").strip():
        problems.append("Не указано название теста.")
    qs = questions_of(test_id)
    if not qs:
        problems.append("В тесте нет вопросов.")
    for n, q in enumerate(qs, 1):
        if q["problem"]:
            problems.append(f"Вопрос {n}: {q['problem']}.")
    # Бот показывает вопросы только вариантами выбора (опросами Telegram)
    typed = [q for q in qs if q["kind"] in ta.TYPED_TYPES]
    if typed and not test.get("allow_web"):
        problems.append("В тесте есть вопросы ввода, сопоставления или последовательности: бот их показать "
                        "не может. Включите «Разрешить прохождение на сайте».")
    return problems


def publish(test_id: int) -> tuple:
    problems = publish_problems(test_id)
    if problems:
        return False, problems
    db.execute("UPDATE tests SET status='active', builder_state='published', updated_at=? WHERE id=?",
               (utils.now_iso(), int(test_id)))
    return True, []


def unpublish(test_id: int) -> None:
    db.execute("UPDATE tests SET status='hidden', builder_state='draft', updated_at=? WHERE id=?",
               (utils.now_iso(), int(test_id)))


# ───────────────────────── результаты для автора ─────────────────────────

def results(test_id: int, limit: int = 200) -> list:
    rows = db.fetchall(
        "SELECT a.id, a.status, a.via, a.score, a.max_score, a.correct_answers, a.wrong_answers, "
        "a.skipped_answers, a.start_time, a.end_time, u.username, u.first_name, u.tg_id "
        "FROM test_attempts a JOIN users u ON u.id=a.user_id "
        "WHERE a.test_id=? AND COALESCE(a.via,'')<>'preview' AND COALESCE(a.attempt_num,0)<>999 "
        "ORDER BY a.id DESC LIMIT ?", (int(test_id), int(limit)))
    out = []
    for r in rows:
        r = dict(r)
        mx = r.get("max_score") or 0
        r["percent"] = round((r.get("score") or 0) / mx * 100, 1) if mx else None
        out.append(r)
    return out
