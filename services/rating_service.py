"""Сервис рейтингов."""
from datetime import datetime, timedelta

import database as db
import utils


def _user_label(row) -> str:
    name = row['first_name'] or row['username'] or str(row['tg_id'])
    return utils.escape_html(name)


def top_overall(limit: int = 10) -> list[dict]:
    rows = db.fetchall("""
        SELECT u.tg_id, u.username, u.first_name, u.school,
               COALESCE(SUM(a.score), 0) AS total_score,
               COUNT(a.id) AS attempts
        FROM users u
        LEFT JOIN test_attempts a ON a.user_id = u.id AND a.is_counted = 1 AND a.status='finished'
        WHERE u.is_blocked = 0
        GROUP BY u.id
        HAVING total_score > 0
        ORDER BY total_score DESC, attempts DESC
        LIMIT ?
    """, (limit,))
    return [dict(r) for r in rows]


def top_week(limit: int = 10) -> list[dict]:
    week_ago = (datetime.now() - timedelta(days=7)).isoformat()
    rows = db.fetchall("""
        SELECT u.tg_id, u.username, u.first_name, u.school,
               COALESCE(SUM(a.score), 0) AS total_score,
               COUNT(a.id) AS attempts
        FROM users u
        LEFT JOIN test_attempts a ON a.user_id = u.id AND a.is_counted = 1
                                      AND a.status='finished' AND a.created_at >= ?
        WHERE u.is_blocked = 0
        GROUP BY u.id
        HAVING total_score > 0
        ORDER BY total_score DESC, attempts DESC
        LIMIT ?
    """, (week_ago, limit))
    return [dict(r) for r in rows]


def top_daily(limit: int = 10) -> list[dict]:
    """По количеству решённых Daily ENT и суммарному проценту."""
    rows = db.fetchall("""
        SELECT u.tg_id, u.username, u.first_name, u.school,
               u.current_streak, u.best_streak,
               COUNT(d.id) AS daily_count,
               COALESCE(SUM(d.percentage), 0) AS total_percent
        FROM users u
        LEFT JOIN daily_results d ON d.user_id = u.id
        WHERE u.is_blocked = 0
        GROUP BY u.id
        HAVING daily_count > 0
        ORDER BY u.best_streak DESC, daily_count DESC, total_percent DESC
        LIMIT ?
    """, (limit,))
    return [dict(r) for r in rows]


def top_schools(limit: int = 10) -> list[dict]:
    rows = db.fetchall("""
        SELECT u.school AS school,
               COUNT(DISTINCT u.id) AS users_count,
               COALESCE(SUM(a.score), 0) AS total_score
        FROM users u
        LEFT JOIN test_attempts a ON a.user_id = u.id AND a.is_counted = 1
                                      AND a.status='finished'
        WHERE u.is_blocked = 0 AND u.school IS NOT NULL AND TRIM(u.school) != ''
        GROUP BY u.school
        HAVING total_score > 0
        ORDER BY total_score DESC, users_count DESC
        LIMIT ?
    """, (limit,))
    return [dict(r) for r in rows]


def user_overall_position(user_id: int) -> tuple[int, int]:
    """(position, total_score)"""
    row = db.fetchone("""
        SELECT COALESCE(SUM(score), 0) AS s FROM test_attempts
        WHERE user_id=? AND is_counted=1 AND status='finished'
    """, (user_id,))
    my_score = row['s']
    if my_score <= 0:
        return (0, 0)
    better = db.fetchone("""
        SELECT COUNT(*) AS c FROM (
            SELECT u.id, COALESCE(SUM(a.score), 0) AS total
            FROM users u
            LEFT JOIN test_attempts a ON a.user_id = u.id AND a.is_counted=1
                                          AND a.status='finished'
            WHERE u.is_blocked = 0
            GROUP BY u.id
            HAVING total > ?
        )
    """, (my_score,))['c']
    return (better + 1, my_score)


def format_top(rows: list[dict], lang: str, score_field: str = 'total_score',
               score_label: str = "очков") -> str:
    from locales import t as tr
    if not rows:
        return tr("rating_empty", lang)
    lines = []
    medals = ['🥇', '🥈', '🥉']
    for i, r in enumerate(rows):
        prefix = medals[i] if i < 3 else f"{i+1}."
        name = _user_label(r)
        if 'school' in r and r.get('school') and 'tg_id' not in r:
            # Школьный рейтинг
            lines.append(f"{prefix} <b>{utils.escape_html(r['school'])}</b> — {r[score_field]} {score_label}")
        else:
            extra = ""
            if r.get('school'):
                extra = f" ({utils.escape_html(r['school'])})"
            lines.append(f"{prefix} {name}{extra} — <b>{r[score_field]}</b> {score_label}")
    return "\n".join(lines)
