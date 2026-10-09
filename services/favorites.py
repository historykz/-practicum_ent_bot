"""
⭐️ Избранные вопросы ученика.

Ученик отмечает вопрос звёздочкой в тесте, карточках или заучивании, а потом
проходит только отмеченные вопросы в тех же самых режимах: тест, карточки,
заучивание. Отдельных режимов для избранного нет — сюда только набор вопросов.

Хранение: user_id + question_id + created_at. Сам вопрос всегда читается из
questions, поэтому любая правка админа видна сразу, а удалённый вопрос
исчезает из избранного сам.

Один и тот же вопрос в нескольких тестах (копии предмета: copied_from_question_id)
считается одним: отмечен оригинал — звезда горит и у копии, и наоборот.

Тест по избранному идёт через обычный движок попыток. Попытка привязана к
служебному скрытому тесту «⭐️ Избранные вопросы» (ни в одном уроке его нет) и
помечена как тренировка: attempt_num=999 и is_counted=0 — так же, как повтор
ошибок в боте. Поэтому она не сдаёт уроки, не даёт наград и не идёт в рейтинг.
"""
import json
import logging
from typing import Optional

import database as db

log = logging.getLogger(__name__)

SYSTEM_TITLE = "⭐️ Избранные вопросы"
SYSTEM_STATUS = "system"
PRACTICE_ATTEMPT_NUM = 999
MAX_CHAIN = 5
_system_id = {"id": None}


# ───────────────────────── служебный тест ─────────────────────────

def system_test_id() -> int:
    """Скрытый тест, к которому привязываются попытки «тест по избранному»."""
    if _system_id["id"]:
        row = db.fetchone("SELECT id FROM tests WHERE id=?", (_system_id["id"],))
        if row:
            return _system_id["id"]
    row = db.fetchone("SELECT value FROM settings WHERE key='favorites_test_id'")
    if row and str(row["value"] or "").isdigit():
        t = db.fetchone("SELECT id FROM tests WHERE id=?", (int(row["value"]),))
        if t:
            _system_id["id"] = int(row["value"])
            return _system_id["id"]
    with db.db_lock():
        row = db.fetchone("SELECT id FROM tests WHERE title=? AND status=?", (SYSTEM_TITLE, SYSTEM_STATUS))
        if row:
            tid = int(row["id"])
        else:
            tid = db.execute(
                "INSERT INTO tests (title, created_by, status, show_correct, show_results, time_per_question) "
                "VALUES (?, 0, ?, 1, 1, 0)", (SYSTEM_TITLE, SYSTEM_STATUS)).lastrowid
        db.execute("INSERT INTO settings (key, value) VALUES ('favorites_test_id', ?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(tid),))
    _system_id["id"] = tid
    return tid


def is_favorites_test(test_id) -> bool:
    try:
        return bool(test_id) and int(test_id) == system_test_id()
    except Exception:
        return False


def is_practice_attempt(attempt: dict) -> bool:
    return bool(attempt) and is_favorites_test(attempt.get("test_id"))


# ───────────────────────── один вопрос в нескольких тестах ─────────────────────────

def root_of(question_id: int) -> int:
    """Оригинал вопроса: идём по copied_from_question_id до настоящей записи."""
    cur = int(question_id)
    for _ in range(MAX_CHAIN):
        row = db.fetchone("SELECT copied_from_question_id AS p FROM questions WHERE id=?", (cur,))
        if not row or not row["p"] or int(row["p"]) == cur:
            return cur
        cur = int(row["p"])
    return cur


def _roots(qids) -> dict:
    return {int(q): root_of(int(q)) for q in qids}


def _stored(user_id: int) -> list:
    """Избранное пользователя: только существующие вопросы, новые сверху."""
    return [dict(r) for r in db.fetchall(
        "SELECT f.question_id, f.created_at FROM favorite_questions f "
        "JOIN questions q ON q.id = f.question_id WHERE f.user_id=? "
        "ORDER BY f.created_at DESC, f.rowid DESC", (int(user_id),))]


def _unique(user_id: int) -> list:
    """Без повторов: из копий одного вопроса оставляем последнюю отмеченную."""
    out, seen = [], set()
    for r in _stored(user_id):
        root = root_of(r["question_id"])
        if root in seen:
            continue
        seen.add(root)
        out.append(r)
    return out


# ───────────────────────── отметить / снять ─────────────────────────

def state(user_id: int, qids) -> list:
    """Какие из этих вопросов у человека в избранном (с учётом копий)."""
    qids = [int(q) for q in qids if str(q).lstrip("-").isdigit()][:500]
    if not qids or not user_id:
        return []
    roots = _roots(qids)
    fav_roots = {root_of(r["question_id"]) for r in _stored(user_id)}
    return [q for q in qids if roots[q] in fav_roots]


def set_favorite(user_id: int, question_id: int, on: Optional[bool] = None) -> dict:
    """Поставить или снять звезду. on=None — переключить. Возвращает новое состояние."""
    qid = int(question_id)
    if not db.fetchone("SELECT id FROM questions WHERE id=?", (qid,)):
        return {"ok": False, "error": "Вопрос не найден — возможно, его удалили."}
    root = root_of(qid)
    with db.db_lock():
        same = [r["question_id"] for r in _stored(user_id) if root_of(r["question_id"]) == root]
        now_on = bool(same)
        target = (not now_on) if on is None else bool(on)
        if target and not now_on:
            db.execute("INSERT OR IGNORE INTO favorite_questions (user_id, question_id) VALUES (?, ?)",
                       (int(user_id), qid))
        elif not target and now_on:
            marks = ",".join("?" * len(same))
            db.execute(f"DELETE FROM favorite_questions WHERE user_id=? AND question_id IN ({marks})",
                       (int(user_id), *same))
    return {"ok": True, "favorite": target, "count": count(user_id)}


def count(user_id: int) -> int:
    return len(_unique(user_id)) if user_id else 0


# ───────────────────────── доступ к вопросу ─────────────────────────

def _accessible_tests(tg_id: int, test_ids) -> dict:
    """test_id → можно ли сейчас заниматься вопросами этого теста.

    Платный урок, на который доступ закончился, в тренировку не попадает:
    иначе избранное стало бы обходом оплаты."""
    from webapp import learning
    import utils
    out = {}
    if utils.is_site_admin(tg_id):
        return {int(t): True for t in test_ids}
    for tid in set(int(t) for t in test_ids if t):
        lessons = [dict(r) for r in db.fetchall(
            "SELECT l.*, sec.subject_id FROM lessons l JOIN sections sec ON sec.id=l.section_id "
            "JOIN subjects s ON s.id=sec.subject_id WHERE l.test_id=? AND l.status='open' "
            "AND s.status='active'", (tid,))]
        if lessons:
            out[tid] = any(learning._subject_gate_ok_sync(l["subject_id"], tg_id)
                           and learning._has_lesson_paid_access_sync(l, tg_id) for l in lessons)
            continue
        t = db.fetchone("SELECT is_private FROM tests WHERE id=?", (tid,))
        if t and t["is_private"]:
            out[tid] = learning._has_private_test_access_sync(tid, tg_id)
        else:
            out[tid] = True
    return out


def items(user_id: int, tg_id: int) -> list:
    """Список для страницы «Избранные вопросы»: актуальный вопрос, ответ, откуда он."""
    rows = _unique(user_id)
    if not rows:
        return []
    qmap = {}
    for r in rows:
        q = db.fetchone(
            "SELECT q.id, q.text, q.web_image_path, q.test_id, t.title AS test_title, "
            "(SELECT text FROM question_options WHERE question_id=q.id AND is_correct=1 ORDER BY order_num, id LIMIT 1) AS answer, "
            "(SELECT COUNT(*) FROM question_options WHERE question_id=q.id) AS n_opts "
            "FROM questions q LEFT JOIN tests t ON t.id=q.test_id WHERE q.id=?", (r["question_id"],))
        if q:
            qmap[r["question_id"]] = dict(q)
    access = _accessible_tests(tg_id, [q["test_id"] for q in qmap.values()])
    out = []
    for r in rows:
        q = qmap.get(r["question_id"])
        if not q or not (q["text"] or "").strip():
            continue
        place = db.fetchone(
            "SELECT s.title AS subject, l.title AS lesson FROM lessons l JOIN sections sec ON sec.id=l.section_id "
            "JOIN subjects s ON s.id=sec.subject_id WHERE l.test_id=? ORDER BY l.id LIMIT 1", (q["test_id"],))
        out.append({
            "id": q["id"], "text": q["text"], "image": q["web_image_path"],
            "answer": q["answer"] or "", "n_opts": q["n_opts"] or 0,
            "where": (f"{place['subject']} · {place['lesson']}" if place else (q["test_title"] or "")),
            "accessible": access.get(int(q["test_id"]), True) if q["test_id"] else True,
            "added": r["created_at"],
        })
    return out


def practice_ids(user_id: int, tg_id: int, for_test: bool = True) -> list:
    """Вопросы для тренировки: доступные, а для теста — ещё и с вариантами ответа.
    Порядок — как добавлял ученик: сначала самые давние."""
    rows = [i for i in items(user_id, tg_id) if i["accessible"]]
    if for_test:
        rows = [i for i in rows if i["n_opts"] >= 2]
    else:
        rows = [i for i in rows if i["answer"]]
    return [i["id"] for i in reversed(rows)]


# ───────────────────────── попытка теста ─────────────────────────

def start_attempt(user_id: int, tg_id: int, restart: bool = False) -> Optional[dict]:
    """Попытка теста по избранному через обычный движок. Незаконченная — продолжается."""
    from datetime import datetime
    tid = system_test_id()
    existing = db.fetchone(
        "SELECT * FROM test_attempts WHERE user_id=? AND test_id=? AND status IN ('in_progress','idle') "
        "ORDER BY id DESC LIMIT 1", (int(user_id), tid))
    if existing and restart:
        db.execute("UPDATE test_attempts SET status='aborted', end_time=? WHERE id=?",
                   (datetime.utcnow().isoformat(timespec="seconds"), existing["id"]))
        existing = None
    if existing:
        if existing["status"] == "idle":
            db.execute("UPDATE test_attempts SET status='in_progress' WHERE id=?", (existing["id"],))
        try:
            q_ids = json.loads(existing["question_order"] or "[]")
        except (ValueError, TypeError):
            q_ids = []
        return {"attempt_id": existing["id"], "q_ids": q_ids, "resume": True}
    q_ids = practice_ids(user_id, tg_id, for_test=True)
    if not q_ids:
        return None
    aid = db.execute(
        "INSERT INTO test_attempts (user_id, test_id, question_order, options_order, status, is_counted, "
        "attempt_num, is_first_attempt) VALUES (?, ?, ?, '{}', 'in_progress', 0, ?, 0)",
        (int(user_id), tid, json.dumps(q_ids), PRACTICE_ATTEMPT_NUM)).lastrowid
    return {"attempt_id": aid, "q_ids": q_ids, "resume": False}
