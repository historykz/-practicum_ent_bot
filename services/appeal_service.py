"""
Сервис апелляций на вопросы теста.

Пользователь может оспорить ответ во время прохождения личного теста.
Админ одобряет (балл засчитывается) или отклоняет (юзер получает предупреждение).
3 ложных апелляции = 1 день бана от тестов.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

import database as db

log = logging.getLogger(__name__)

MAX_WARNINGS = 3
BAN_DAYS = 1  # на сколько дней банить за 3 предупреждения


def create_appeal(question_id: int, user_tg_id: int, user_text: str) -> int:
    """Создать запись апелляции, вернуть id."""
    cur = db.execute(
        "INSERT INTO appeals (question_id, user_tg_id, user_text, status) "
        "VALUES (?, ?, ?, 'pending')",
        (question_id, user_tg_id, user_text[:2000]))
    return cur.lastrowid


def get_appeal(appeal_id: int) -> Optional[dict]:
    return db.fetchone("SELECT * FROM appeals WHERE id=?", (appeal_id,))


def list_pending_appeals(limit: int = 50) -> list:
    return db.fetchall(
        "SELECT * FROM appeals WHERE status='pending' "
        "ORDER BY created_at DESC LIMIT ?", (limit,))


def count_pending() -> int:
    r = db.fetchone("SELECT COUNT(*) AS c FROM appeals WHERE status='pending'")
    return r['c'] if r else 0


def approve_appeal(appeal_id: int, admin_tg_id: int):
    db.execute(
        "UPDATE appeals SET status='approved', resolved_by=?, "
        "resolved_at=CURRENT_TIMESTAMP WHERE id=?",
        (admin_tg_id, appeal_id))


def reject_appeal(appeal_id: int, admin_tg_id: int) -> tuple[int, bool]:
    """Отклонить апелляцию + дать юзеру предупреждение.
    Вернёт (текущее число предупреждений, забанен ли)."""
    appeal = get_appeal(appeal_id)
    if not appeal:
        return (0, False)
    db.execute(
        "UPDATE appeals SET status='rejected', resolved_by=?, "
        "resolved_at=CURRENT_TIMESTAMP WHERE id=?",
        (admin_tg_id, appeal_id))
    user_tg = appeal['user_tg_id']

    # Инкрементим предупреждения
    u = db.fetchone(
        "SELECT appeal_warnings FROM users WHERE tg_id=?", (user_tg,))
    cur_warn = (u.get('appeal_warnings') if u else 0) or 0
    new_warn = cur_warn + 1
    db.execute("UPDATE users SET appeal_warnings=? WHERE tg_id=?",
                (new_warn, user_tg))

    banned = False
    if new_warn >= MAX_WARNINGS:
        ban_until = (datetime.utcnow() + timedelta(days=BAN_DAYS)).isoformat()
        db.execute("UPDATE users SET banned_until=?, appeal_warnings=0 "
                    "WHERE tg_id=?", (ban_until, user_tg))
        banned = True

    return (new_warn, banned)


def is_user_banned(user_id: int) -> tuple[bool, Optional[str]]:
    """Проверить забанен ли юзер. Принимает tg_id ИЛИ internal id.
    Вернёт (банен?, до какого времени iso)."""
    # Пробуем как tg_id
    u = db.fetchone("SELECT banned_until FROM users WHERE tg_id=?",
                     (user_id,))
    if not u:
        # Пробуем как internal id
        u = db.fetchone("SELECT banned_until FROM users WHERE id=?",
                         (user_id,))
    if not u or not u.get('banned_until'):
        return (False, None)
    try:
        ban_until = datetime.fromisoformat(u['banned_until'])
        if ban_until > datetime.utcnow():
            return (True, u['banned_until'])
        # Срок истёк — очистим (по обоим возможным полям)
        try:
            db.execute("UPDATE users SET banned_until=NULL WHERE tg_id=? OR id=?",
                        (user_id, user_id))
        except Exception:
            pass
        return (False, None)
    except Exception:
        return (False, None)


def get_user_warnings(user_tg_id: int) -> int:
    u = db.fetchone("SELECT appeal_warnings FROM users WHERE tg_id=?",
                     (user_tg_id,))
    return (u.get('appeal_warnings') if u else 0) or 0


def get_question_stats(question_id: int) -> dict:
    """Полная статистика по вопросу: прохождения, правильные/нет, апелляции."""
    out = {
        "passes": 0,        # сколько раз вопрос показывался
        "correct": 0,        # сколько ответили правильно
        "wrong": 0,           # сколько неправильно
        "correct_pct": 0,    # процент правильных
        "appeals_total": 0,
        "appeals_approved": 0,
        "appeals_rejected": 0,
        "appeals_pending": 0,
    }
    # Через test_attempts/question_responses (если такие таблицы есть)
    try:
        r = db.fetchone(
            "SELECT COUNT(*) AS c FROM question_responses WHERE question_id=?",
            (question_id,))
        out['passes'] = (r['c'] if r else 0) or 0
    except Exception:
        pass
    try:
        r = db.fetchone(
            "SELECT COUNT(*) AS c FROM question_responses "
            "WHERE question_id=? AND is_correct=1", (question_id,))
        out['correct'] = (r['c'] if r else 0) or 0
        out['wrong'] = max(0, out['passes'] - out['correct'])
        if out['passes'] > 0:
            out['correct_pct'] = round(out['correct'] / out['passes'] * 100)
    except Exception:
        pass

    rows = db.fetchall(
        "SELECT status, COUNT(*) AS c FROM appeals WHERE question_id=? GROUP BY status",
        (question_id,))
    for r in rows:
        out['appeals_total'] += r['c']
        if r['status'] == 'approved':
            out['appeals_approved'] = r['c']
        elif r['status'] == 'rejected':
            out['appeals_rejected'] = r['c']
        elif r['status'] == 'pending':
            out['appeals_pending'] = r['c']
    return out


def find_question_by_serial(serial: str) -> Optional[dict]:
    """Найти вопрос по серийному номеру Q-NNNN."""
    s = (serial or "").strip().upper()
    # Допускаем разные варианты: 'Q-12', 'Q12', '12'
    if s.isdigit():
        s = f"Q-{int(s):04d}"
    elif s.startswith('Q') and not s.startswith('Q-'):
        # Q1234 → Q-1234
        try:
            n = int(s[1:])
            s = f"Q-{n:04d}"
        except ValueError:
            pass
    return db.fetchone("SELECT * FROM questions WHERE serial_no=?", (s,))
