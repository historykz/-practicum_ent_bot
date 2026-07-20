"""
Отложенная выдача доступа тем, кто ещё НЕ запускал бота.

Telegram не позволяет узнать id по @username до первого контакта,
поэтому: админ вносит @username заранее → запись ждёт здесь →
при первом /start доступ применяется автоматически + уведомление.

kind: 'private' (доступ к приватному тесту, test_id + days)
      'premium' (премиум на days) — поддержано активатором на будущее.
Срок отсчитывается с момента АКТИВАЦИИ (человек получает полный срок).
"""
import logging
from datetime import datetime, timedelta, timezone

import database as db

log = logging.getLogger(__name__)
ALMATY = timezone(timedelta(hours=5))


def _norm(username: str) -> str:
    return (username or '').lstrip('@').strip().lower()


def add_pending(username: str, kind: str, test_id, days: int,
                granted_by: int) -> bool:
    """Добавить запись ожидания. False если такая уже ждёт (дубль)."""
    uname = _norm(username)
    if not uname:
        return False
    try:
        dup = db.fetchone(
            """SELECT id FROM pending_access
               WHERE username=? AND kind=? AND COALESCE(test_id,0)=COALESCE(?,0)
                 AND fulfilled=0""",
            (uname, kind, test_id))
        if dup:
            return False
        db.execute(
            """INSERT INTO pending_access
                 (username, kind, test_id, days, granted_by)
               VALUES (?,?,?,?,?)""",
            (uname, kind, test_id, days, granted_by))
        return True
    except Exception as e:
        log.warning("add_pending %s: %s", uname, e)
        return False


def pending_count() -> int:
    try:
        row = db.fetchone(
            "SELECT COUNT(DISTINCT username) AS c FROM pending_access WHERE fulfilled=0")
        return row['c'] if row else 0
    except Exception:
        return 0


def list_pending(limit: int = 30) -> list:
    try:
        rows = db.fetchall(
            """SELECT p.username, p.kind, p.days, p.created_at, t.title
               FROM pending_access p
               LEFT JOIN tests t ON t.id = p.test_id
               WHERE p.fulfilled=0
               ORDER BY p.id DESC LIMIT ?""", (limit,))
        return [dict(r) for r in rows]
    except Exception:
        return []


async def activate_for_user(bot, tg_id: int, username: str,
                             internal_user_id: int) -> int:
    """
    Проверить ожидающие записи для username и применить их.
    Вызывается при /start. Возвращает сколько записей активировано.
    """
    uname = _norm(username)
    if not uname:
        return 0
    try:
        rows = db.fetchall(
            "SELECT * FROM pending_access WHERE username=? AND fulfilled=0",
            (uname,))
    except Exception:
        return 0
    if not rows:
        return 0

    now_almaty = datetime.now(ALMATY)
    granted_at_iso = now_almaty.isoformat()
    activated = 0
    test_titles = []
    max_days = 0
    premium_days = 0
    granted_by = rows[0].get('granted_by')

    for r in rows:
        kind = r.get('kind') or 'private'
        days = r.get('days') or 0
        try:
            if kind == 'private' and r.get('test_id'):
                expires_at = None
                if days > 0:
                    exp_dt = now_almaty + timedelta(days=days)
                    expires_at = exp_dt.astimezone(timezone.utc).isoformat()
                db.execute(
                    """INSERT INTO private_test_access
                          (test_id, user_tg_id, granted_by, granted_at,
                           expires_at, notified_expired)
                       VALUES (?,?,?,?,?,0)
                       ON CONFLICT(test_id, user_tg_id) DO UPDATE SET
                          expires_at=excluded.expires_at,
                          granted_by=excluded.granted_by,
                          granted_at=excluded.granted_at,
                          notified_expired=0""",
                    (r['test_id'], tg_id, r.get('granted_by'),
                     granted_at_iso, expires_at))
                t = db.fetchone("SELECT title FROM tests WHERE id=?",
                                (r['test_id'],))
                if t:
                    test_titles.append(t['title'])
                max_days = max(max_days, days)
            elif kind == 'premium':
                import utils as _u
                _u.grant_premium(internal_user_id, days,
                                 r.get('granted_by') or 0)
                premium_days = max(premium_days, days)
            db.execute(
                "UPDATE pending_access SET fulfilled=1, fulfilled_at=? WHERE id=?",
                (granted_at_iso, r['id']))
            activated += 1
        except Exception as e:
            log.warning("activate pending id=%s: %s", r.get('id'), e)

    if not activated:
        return 0

    # Уведомляем пользователя одним сообщением
    try:
        lines = ["🎁 <b>Тебе выдан доступ!</b>\n"]
        if test_titles:
            if len(test_titles) == 1:
                lines.append(f"🔐 Тест: <b>«{test_titles[0]}»</b>")
            else:
                lines.append(f"🔐 Приватных тестов: <b>{len(test_titles)}</b>")
            lines.append("♾ Срок: бессрочно" if max_days == 0
                         else f"⏱ Срок: {max_days} дней")
        if premium_days:
            lines.append(f"💎 Премиум на <b>{premium_days} дней</b>")
        lines.append("\n📍 Жми «📚 Пройти тесты» — всё уже открыто! 💪")
        await bot.send_message(tg_id, "\n".join(lines), parse_mode="HTML")
    except Exception:
        pass

    # Лог админам
    try:
        from services import admin_log_service as _als
        await _als.log_action(
            bot, granted_by or 0, "⏳ Активирован отложенный доступ",
            f"@{uname} запустил бота — выдано записей: {activated}")
    except Exception:
        pass
    return activated
