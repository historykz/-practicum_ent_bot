"""
Сервис импорта Quiz Poll.

ВАЖНО:
Telegram Bot API при пересылке Quiz Poll НЕ гарантирует, что в Poll-объекте
будет correct_option_id. Если правильный ответ не доступен:
1) Сохраняем вопрос как черновик (question_drafts).
2) Просим админа выбрать правильный ответ вручную.

Если correct_option_id есть - сразу создаём вопрос в questions.
"""
import json
import logging
from typing import Optional

from aiogram.types import Poll

import database as db

logger = logging.getLogger(__name__)


def is_quiz_poll(poll: Poll) -> bool:
    """Проверяет, является ли poll викториной."""
    return getattr(poll, "type", None) == "quiz"


def save_poll_as_question(test_id: int, poll: Poll, imported_by: int) -> tuple[str, Optional[int]]:
    """
    Сохраняет Quiz Poll в базу.

    Возвращает (status, id):
      status = 'ok'    -> вопрос создан, id = question_id
      status = 'draft' -> сохранён черновик (нужен ручной выбор), id = draft_id
      status = 'err'   -> ошибка, id = None
    """
    if not is_quiz_poll(poll):
        return "err", None

    question_text = poll.question or ""
    options_texts = [opt.text for opt in (poll.options or [])]
    if not question_text or len(options_texts) < 2:
        return "err", None

    explanation = getattr(poll, "explanation", "") or ""
    correct_option_id = getattr(poll, "correct_option_id", None)

    # Запоминаем сам факт импорта
    raw = {
        "question": question_text,
        "options": options_texts,
        "correct_option_id": correct_option_id,
        "explanation": explanation,
        "poll_id": poll.id,
    }
    try:
        db.execute(
            """INSERT INTO imported_polls (test_id, poll_id, question_text, raw_data,
                                          correct_option_id, needs_manual_correct_answer, imported_by)
               VALUES (?,?,?,?,?,?,?)""",
            (test_id, poll.id, question_text, json.dumps(raw, ensure_ascii=False),
             correct_option_id,
             0 if correct_option_id is not None else 1,
             imported_by),
        )
    except Exception as e:
        logger.warning("Ошибка записи imported_polls: %s", e)

    # Если правильный ответ известен - сразу создаём вопрос
    if correct_option_id is not None:
        # Узнаём текущий max order
        row = db.fetchone(
            "SELECT COALESCE(MAX(order_num), 0) AS m FROM questions WHERE test_id=?",
            (test_id,),
        )
        cur_order = (row["m"] if row else 0) + 1
        db.execute(
            """INSERT INTO questions (test_id, text, explanation, source_type,
                                      poll_id, order_num)
               VALUES (?,?,?,?,?,?)""",
            (test_id, question_text, explanation, "poll_import", poll.id, cur_order),
        )
        qrow = db.fetchone("SELECT last_insert_rowid() AS id")
        qid = qrow["id"]
        for i, opt_text in enumerate(options_texts):
            db.execute(
                "INSERT INTO question_options (question_id, text, is_correct, order_num) VALUES (?,?,?,?)",
                (qid, opt_text, 1 if i == correct_option_id else 0, i),
            )
        return "ok", qid

    # Иначе - черновик
    db.execute(
        """INSERT INTO question_drafts (test_id, source_type, question_text, raw_options,
                                        status, created_by)
           VALUES (?,?,?,?,?,?)""",
        (test_id, "poll_forwarded", question_text,
         json.dumps(options_texts, ensure_ascii=False), "pending", imported_by),
    )
    drow = db.fetchone("SELECT last_insert_rowid() AS id")
    return "draft", drow["id"]


def save_poll_dict_as_question(test_id: int, p: dict, imported_by: int) -> str:
    """
    Сохраняет poll (приходящий как dict из FSM-буфера).
    Возвращает 'ok' | 'draft' | 'err'.
    """
    question_text = (p.get("question") or "").strip()
    options_texts = p.get("options") or []
    if not question_text or len(options_texts) < 2:
        return "err"
    correct_option_id = p.get("correct_option_id")
    explanation = p.get("explanation") or ""
    poll_id = p.get("id") or ""

    try:
        db.execute(
            """INSERT INTO imported_polls (test_id, poll_id, question_text, raw_data,
                                          correct_option_id, needs_manual_correct_answer, imported_by)
               VALUES (?,?,?,?,?,?,?)""",
            (test_id, poll_id, question_text, json.dumps(p, ensure_ascii=False),
             correct_option_id,
             0 if correct_option_id is not None else 1,
             imported_by),
        )
    except Exception as e:
        logger.warning("imported_polls insert: %s", e)

    if correct_option_id is not None:
        row = db.fetchone(
            "SELECT COALESCE(MAX(order_num), 0) AS m FROM questions WHERE test_id=?",
            (test_id,),
        )
        cur_order = (row["m"] if row else 0) + 1
        db.execute(
            """INSERT INTO questions (test_id, text, explanation, source_type,
                                      poll_id, order_num)
               VALUES (?,?,?,?,?,?)""",
            (test_id, question_text, explanation, "poll_import", poll_id, cur_order),
        )
        qid = db.fetchone("SELECT last_insert_rowid() AS id")["id"]
        for i, opt_text in enumerate(options_texts):
            db.execute(
                "INSERT INTO question_options (question_id, text, is_correct, order_num) VALUES (?,?,?,?)",
                (qid, opt_text, 1 if i == correct_option_id else 0, i),
            )
        return "ok"

    db.execute(
        """INSERT INTO question_drafts (test_id, source_type, question_text, raw_options,
                                        status, created_by)
           VALUES (?,?,?,?,?,?)""",
        (test_id, "poll_forwarded", question_text,
         json.dumps(options_texts, ensure_ascii=False), "pending", imported_by),
    )
    return "draft"


def list_drafts(test_id: int) -> list[dict]:
    rows = db.fetchall(
        "SELECT * FROM question_drafts WHERE test_id=? AND status='pending' ORDER BY id",
        (test_id,),
    )
    return [dict(r) for r in rows]


def get_draft(draft_id: int) -> Optional[dict]:
    row = db.fetchone("SELECT * FROM question_drafts WHERE id=?", (draft_id,))
    return dict(row) if row else None


def finalize_draft(draft_id: int, correct_index: int) -> bool:
    """Превращает черновик в полноценный вопрос."""
    draft = get_draft(draft_id)
    if not draft or draft["status"] != "pending":
        return False
    try:
        options = json.loads(draft["raw_options"])
    except (ValueError, TypeError):
        return False
    if correct_index < 0 or correct_index >= len(options):
        return False

    row = db.fetchone(
        "SELECT COALESCE(MAX(order_num), 0) AS m FROM questions WHERE test_id=?",
        (draft["test_id"],),
    )
    cur_order = (row["m"] if row else 0) + 1
    db.execute(
        """INSERT INTO questions (test_id, text, source_type, order_num)
           VALUES (?,?,?,?)""",
        (draft["test_id"], draft["question_text"], "poll_forwarded", cur_order),
    )
    qrow = db.fetchone("SELECT last_insert_rowid() AS id")
    qid = qrow["id"]
    for i, opt_text in enumerate(options):
        db.execute(
            "INSERT INTO question_options (question_id, text, is_correct, order_num) VALUES (?,?,?,?)",
            (qid, opt_text, 1 if i == correct_index else 0, i),
        )
    db.execute(
        "UPDATE question_drafts SET status='completed', draft_correct_option=? WHERE id=?",
        (correct_index, draft_id),
    )
    return True


def delete_draft(draft_id: int) -> None:
    db.execute("DELETE FROM question_drafts WHERE id=?", (draft_id,))
