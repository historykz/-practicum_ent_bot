"""
Daily ENT - ежедневные задания.
"""
import json
import logging
import random
from datetime import date, timedelta
from typing import Optional

import database as db
from config import DAILY_DEFAULT_QUESTIONS
from utils import today_str, yesterday_str

logger = logging.getLogger(__name__)


def get_or_create_daily_task(language: str, count: int = DAILY_DEFAULT_QUESTIONS) -> Optional[dict]:
    """
    Получает (или создаёт) задание на сегодня для указанного языка.
    Берёт случайные вопросы из всех активных тестов этого языка с allow_daily=1
    или из всех вопросов, если таких нет.
    """
    today = today_str()
    row = db.fetchone(
        "SELECT * FROM daily_tasks WHERE task_date=? AND language=?",
        (today, language),
    )
    if row:
        return dict(row)

    # Пытаемся набрать вопросы из тестов с allow_daily=1
    rows = db.fetchall(
        """SELECT q.id FROM questions q
           JOIN tests t ON q.test_id = t.id
           WHERE t.language=? AND t.status='active' AND t.allow_daily=1""",
        (language,),
    )
    if not rows:
        # Иначе берём из всех активных тестов
        rows = db.fetchall(
            """SELECT q.id FROM questions q
               JOIN tests t ON q.test_id = t.id
               WHERE t.language=? AND t.status='active'""",
            (language,),
        )
    if not rows:
        return None

    qids = [r["id"] for r in rows]
    random.shuffle(qids)
    qids = qids[:count]

    db.execute(
        """INSERT INTO daily_tasks (task_date, language, question_ids, mode)
           VALUES (?,?,?,?)""",
        (today, language, json.dumps(qids), "random"),
    )
    row = db.fetchone(
        "SELECT * FROM daily_tasks WHERE task_date=? AND language=?",
        (today, language),
    )
    return dict(row) if row else None


def user_did_daily_today(user_id: int) -> bool:
    today = today_str()
    row = db.fetchone(
        "SELECT id FROM daily_results WHERE user_id=? AND task_date=?",
        (user_id, today),
    )
    return bool(row)


def create_daily_test(user_id: int, lang: str) -> Optional[int]:
    """
    Создаёт временный тест для Daily ENT для конкретного пользователя.
    Возвращает test_id (короткоживущий, специально для daily).

    Логика: для упрощения создаём тест с вопросами из daily_tasks и сразу запускаем.
    """
    task = get_or_create_daily_task(lang)
    if not task:
        return None
    try:
        qids = json.loads(task["question_ids"])
    except (ValueError, TypeError):
        return None
    if not qids:
        return None

    # Создаём служебный тест
    db.execute(
        """INSERT INTO tests (title, description, subject, language, test_type,
                              status, shuffle_questions, shuffle_options,
                              show_correct, show_explanation, time_per_question,
                              created_by)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            f"Daily ENT {today_str()} ({lang})", "Daily ENT", "", lang,
            "daily", "hidden", 1, 1, 1, 1, 30, 0,
        ),
    )
    row = db.fetchone("SELECT last_insert_rowid() AS id")
    new_test_id = row["id"]

    # Копируем вопросы (берём только нужные id)
    # Используем существующие вопросы через простую связку (а не дублируем) - но для упрощения
    # дублируем структурно, чтобы прохождение шло через стандартный test_runner.
    for order, qid in enumerate(qids, start=1):
        q = db.fetchone("SELECT * FROM questions WHERE id=?", (qid,))
        if not q:
            continue
        db.execute(
            """INSERT INTO questions (test_id, text, explanation, score, image_file_id,
                                      topic, difficulty, source_type, order_num)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (new_test_id, q["text"], q["explanation"], q["score"], q["image_file_id"],
             q["topic"], q["difficulty"], "daily_copy", order),
        )
        new_qrow = db.fetchone("SELECT last_insert_rowid() AS id")
        new_qid = new_qrow["id"]
        opts = db.fetchall(
            "SELECT * FROM question_options WHERE question_id=? ORDER BY order_num, id",
            (qid,),
        )
        for o in opts:
            db.execute(
                "INSERT INTO question_options (question_id, text, is_correct, order_num) VALUES (?,?,?,?)",
                (new_qid, o["text"], o["is_correct"], o["order_num"]),
            )
    return new_test_id


def update_streak_after_daily(user_id: int, percent: float) -> None:
    """
    После завершения Daily вычисляем streak.
    """
    today = today_str()
    yesterday = yesterday_str()

    user = db.fetchone("SELECT * FROM users WHERE id=?", (user_id,))
    if not user:
        return
    last_date = user["last_daily_date"]
    if last_date == yesterday:
        new_streak = (user["current_streak"] or 0) + 1
    elif last_date == today:
        # уже считалось сегодня
        return
    else:
        new_streak = 1
    best = max(user["best_streak"] or 0, new_streak)
    db.execute(
        "UPDATE users SET current_streak=?, best_streak=?, last_daily_date=? WHERE id=?",
        (new_streak, best, today, user_id),
    )
    db.execute(
        """INSERT OR IGNORE INTO daily_results
           (user_id, task_date, percentage, streak, best_streak)
           VALUES (?,?,?,?,?)""",
        (user_id, today, percent, new_streak, best),
    )
