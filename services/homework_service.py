"""Сервис домашних заданий."""
import re
from typing import Optional

import database as db
import utils


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def check_open_answer(answer: str, keywords_csv: str) -> tuple[int, int, int]:
    """Возвращает (matched, total_keywords, score_10)."""
    if not keywords_csv:
        return (0, 0, 0)
    keywords = [k.strip().lower() for k in keywords_csv.split(',') if k.strip()]
    if not keywords:
        return (0, 0, 0)
    text = _normalize(answer)
    matched = 0
    for kw in keywords:
        if kw in text:
            matched += 1
    score = round(matched / len(keywords) * 10)
    return (matched, len(keywords), score)


def save_homework_result(user_id: int, note_id: int, score_10: int, answer_text: str):
    existing = db.fetchone(
        "SELECT id FROM user_notes_progress WHERE user_id=? AND note_id=?",
        (user_id, note_id))
    if existing:
        db.execute(
            """UPDATE user_notes_progress SET homework_score=?, homework_answer=?,
                                              updated_at=? WHERE id=?""",
            (score_10, answer_text, utils.now_iso(), existing['id']))
    else:
        db.execute(
            """INSERT INTO user_notes_progress (user_id, note_id, last_page,
                                                homework_score, homework_answer, updated_at)
               VALUES (?, ?, 0, ?, ?, ?)""",
            (user_id, note_id, score_10, answer_text, utils.now_iso()))


def get_user_homework_score(user_id: int, note_id: int) -> Optional[int]:
    r = db.fetchone(
        "SELECT homework_score FROM user_notes_progress WHERE user_id=? AND note_id=?",
        (user_id, note_id))
    if not r:
        return None
    return r['homework_score']
