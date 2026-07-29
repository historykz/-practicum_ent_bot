"""
Сервис импорта вопросов через текст.
"""
import logging

import database as db
from utils import parse_questions_text, now_iso

logger = logging.getLogger(__name__)


def import_text_questions(test_id: int, raw_text: str,
                          topic: str = "", difficulty: int = 2) -> tuple[int, list[str]]:
    """
    Импортирует вопросы из текста в указанный тест.
    Возвращает (added_count, errors).
    """
    questions, errors = parse_questions_text(raw_text)
    if not questions:
        return 0, errors

    added = 0
    # Узнаём текущий max order
    row = db.fetchone(
        "SELECT COALESCE(MAX(order_num), 0) AS m FROM questions WHERE test_id=?",
        (test_id,),
    )
    cur_order = row["m"] if row else 0

    for q in questions:
        try:
            cur_order += 1
            db.execute(
                """INSERT INTO questions (test_id, text, topic, difficulty,
                                          source_type, order_num)
                   VALUES (?,?,?,?,?,?)""",
                (test_id, q["text"], topic, difficulty, "text_import", cur_order),
            )
            qrow = db.fetchone("SELECT last_insert_rowid() AS id")
            qid = qrow["id"]
            for i, opt_text in enumerate(q["options"]):
                is_correct = 1 if i == q["correct_index"] else 0
                db.execute(
                    "INSERT INTO question_options (question_id, text, is_correct, order_num) VALUES (?,?,?,?)",
                    (qid, opt_text, is_correct, i),
                )
            added += 1
        except Exception as e:
            logger.exception("Ошибка при добавлении вопроса: %s", e)
            errors.append(f"Ошибка БД при добавлении: {e}")
    return added, errors
