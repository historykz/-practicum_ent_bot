"""Сервис рейтингов.

Общий рейтинг (top_overall / user_overall_position и т.д.) считает СУММУ
баллов всех засчитанных попыток — так было и так остаётся, его показывают
/rating и личный кабинет.

Ниже добавлен рейтинг ПО ПРЕДМЕТУ. Он считает по тем же test_attempts.score,
но берёт лучшую попытку каждого теста: иначе место в предметном рейтинге
росло бы от простого перепрохождения одного и того же ДЗ. Сами баллы и
таблицы общие — второй системы начисления здесь нет.
"""
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


# ===================== Рейтинг по предмету =====================

def subject_points(user_id: int, subject_id: int) -> int:
    """Баллы человека по конкретному предмету (лучшая попытка каждого теста)."""
    from services import gamification as g
    return g.points(user_id, subject_id)


def subject_position(user_id: int, subject_id: int) -> dict:
    """{place, total, points, to_next} внутри предмета."""
    from services import gamification as g
    return g.position(user_id, subject_id)


def top_subject(subject_id: int, limit: int = 20) -> list:
    """Топ учеников предмета — те же баллы, только в границах предмета."""
    from services import gamification as g
    # В таблицу лидеров показываем тех, кто уже набрал баллы: строчки с нулём
    # в «топе» смысла не имеют. На место и знаменатель в карточке ученика это
    # не влияет — там участвуют все ученики предмета.
    rows = [(uid, pts) for uid, pts in g.leaderboard(subject_id) if pts > 0][:limit]
    out = []
    for uid, pts in rows:
        u = db.fetchone(
            "SELECT tg_id, username, first_name, school FROM users WHERE id=?", (uid,))
        if not u:
            continue
        d = dict(u)
        d["total_score"] = pts
        out.append(d)
    return out
