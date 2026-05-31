"""
Модерация чата: бан, мут, кик, размут, разбан.
Бот действует от имени чата (должен быть админом с правами).
Команды: русские (бан/мут/кик/размут/разбан) и слэш (/ban /mute /kick /unban /unmute).
Цель: реплаем на сообщение ИЛИ @username.
Права: только админы бота.
"""
import re
import logging
from datetime import datetime, timedelta
from typing import Optional

import database as db

log = logging.getLogger(__name__)


def init_moderation_table():
    """Таблица забаненных/мьюченных для отображения списка."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS chat_moderation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_tg_id INTEGER NOT NULL,
            username TEXT,
            full_name TEXT,
            action TEXT NOT NULL,           -- 'ban' | 'mute'
            until_ts TEXT,                   -- NULL = навсегда
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, user_tg_id, action)
        )
    """)


# ===================== ПАРСИНГ ДЛИТЕЛЬНОСТИ =====================

# единицы → секунды
_UNITS = {
    'мин': 60, 'минут': 60, 'минута': 60, 'минуты': 60, 'м': 60,
    'min': 60, 'm': 60,
    'час': 3600, 'часа': 3600, 'часов': 3600, 'ч': 3600,
    'hour': 3600, 'h': 3600,
    'день': 86400, 'дня': 86400, 'дней': 86400, 'д': 86400,
    'day': 86400, 'd': 86400,
    'недел': 604800, 'неделя': 604800, 'недели': 604800, 'нед': 604800,
    'week': 604800, 'w': 604800,
    'месяц': 2592000, 'месяца': 2592000, 'месяцев': 2592000, 'мес': 2592000,
    'month': 2592000,
    'год': 31536000, 'года': 31536000, 'лет': 31536000, 'г': 31536000,
    'year': 31536000, 'y': 31536000,
}


def parse_duration(text: str) -> Optional[int]:
    """
    Парсит '1час', '30 мин', '2дня', '10 лет', '1h', '30m' → секунды.
    Возвращает None если не распознано (= навсегда).
    """
    if not text:
        return None
    text = text.strip().lower().replace(' ', '')
    # Находим число + слово
    m = re.match(r'^(\d+)\s*([а-яёa-z]+)', text)
    if not m:
        # Может просто число — считаем минутами
        if text.isdigit():
            return int(text) * 60
        return None
    num = int(m.group(1))
    unit = m.group(2)
    # Ищем юнит (по началу слова — чтобы 'минут'/'минуты' совпали)
    for key, sec in sorted(_UNITS.items(), key=lambda x: -len(x[0])):
        if unit.startswith(key):
            return num * sec
    return None


def humanize_duration(seconds: Optional[int]) -> str:
    """Секунды → человекочитаемо."""
    if not seconds:
        return "навсегда"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    if seconds < 86400:
        h = seconds // 3600
        return f"{h} ч"
    if seconds < 2592000:
        return f"{seconds // 86400} дн"
    if seconds < 31536000:
        return f"{seconds // 2592000} мес"
    return f"{seconds // 31536000} г"


# ===================== ЗАПИСИ МОДЕРАЦИИ =====================

def record_action(chat_id: int, user_tg_id: int, username: str,
                   full_name: str, action: str,
                   until_ts: Optional[str], created_by: int):
    db.execute(
        """INSERT INTO chat_moderation
              (chat_id, user_tg_id, username, full_name, action, until_ts, created_by)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(chat_id, user_tg_id, action) DO UPDATE SET
              until_ts=excluded.until_ts, created_by=excluded.created_by,
              created_at=CURRENT_TIMESTAMP, username=excluded.username,
              full_name=excluded.full_name""",
        (chat_id, user_tg_id, username or '', full_name or '',
          action, until_ts, created_by))


def remove_action(chat_id: int, user_tg_id: int, action: str):
    db.execute(
        "DELETE FROM chat_moderation WHERE chat_id=? AND user_tg_id=? AND action=?",
        (chat_id, user_tg_id, action))


def list_banned(chat_id: int) -> list:
    """Активные баны в чате."""
    rows = db.fetchall(
        "SELECT * FROM chat_moderation WHERE chat_id=? AND action='ban' "
        "ORDER BY created_at DESC", (chat_id,))
    # Чистим истёкшие
    out = []
    now = datetime.utcnow()
    for r in rows:
        if r.get('until_ts'):
            try:
                if datetime.fromisoformat(r['until_ts']) <= now:
                    remove_action(chat_id, r['user_tg_id'], 'ban')
                    continue
            except Exception:
                pass
        out.append(r)
    return out


def list_muted(chat_id: int) -> list:
    rows = db.fetchall(
        "SELECT * FROM chat_moderation WHERE chat_id=? AND action='mute' "
        "ORDER BY created_at DESC", (chat_id,))
    out = []
    now = datetime.utcnow()
    for r in rows:
        if r.get('until_ts'):
            try:
                if datetime.fromisoformat(r['until_ts']) <= now:
                    remove_action(chat_id, r['user_tg_id'], 'mute')
                    continue
            except Exception:
                pass
        out.append(r)
    return out
