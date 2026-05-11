# practicum_ent_bot v2.0

# =========================================

# IMPORTS

# =========================================

import asyncio
import logging
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from html import escape
from itertools import count
from typing import Final, Optional

from telegram import (
InlineKeyboardButton,
InlineKeyboardMarkup,
Update,
)
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
ApplicationBuilder,
CallbackQueryHandler,
CommandHandler,
ContextTypes,
MessageHandler,
PollAnswerHandler,
filters,
)

# =========================================

# CONFIG

# =========================================

BOT_TOKEN: str = os.getenv(“BOT_TOKEN”, “”).strip()
ADMINS_RAW: str = os.getenv(“ADMINS”, “”).strip()
CHANNEL_ID: str = os.getenv(“CHANNEL_ID”, “”).strip()  # например @historyentk
DB_PATH: str = os.getenv(“DB_PATH”, “bot.db”)

if not BOT_TOKEN:
raise ValueError(“Не найден BOT_TOKEN в переменных окружения”)
if not ADMINS_RAW:
raise ValueError(“Не найден ADMINS в переменных окружения”)

try:
ADMINS: Final[set[int]] = {
int(a.strip()) for a in ADMINS_RAW.split(”,”) if a.strip()
}
except ValueError as exc:
raise ValueError(“ADMINS должен содержать только числовые Telegram ID”) from exc

# –– Лимиты и константы (меняются через админку в БД) ––

DEFAULT_DAILY_LIMIT        = 55    # вопросов/день для обычных
PREMIUM_COIN_DAILY_LIMIT   = 100   # вопросов за которые Premium получает монеты
COINS_EASY                 = 1
COINS_MEDIUM               = 2
COINS_HARD                 = 3
XP_PER_CORRECT             = 10
XP_PER_WRONG               = 2
STREAK_BONUS_EVERY         = 10    # каждые N правильных подряд – бонус
STREAK_BONUS_COINS         = 5
INACTIVITY_PAUSE_COUNT     = 2     # пропусков подряд до авто-паузы
INACTIVITY_REMINDER_MIN    = 30    # минут до reminder после паузы
PREMIUM_COIN_PRICES        = {“3d”: 500, “7d”: 900}
PREMIUM_COIN_COOLDOWN_DAYS = 30    # cooldown между покупками Premium за монеты
COIN_BUY_PREMIUM_ENABLED   = True
QUESTION_TIMEOUT_SEC       = 60    # таймер на вопрос

logging.basicConfig(
format=”%(asctime)s | %(name)s | %(levelname)s | %(message)s”,
level=logging.INFO,
)
logger = logging.getLogger(**name**)

# =========================================

# DATABASE

# =========================================

def get_conn() -> sqlite3.Connection:
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.execute(“PRAGMA journal_mode=WAL”)
conn.execute(“PRAGMA foreign_keys=ON”)
return conn

def init_db() -> None:
“”“Создаёт все таблицы при первом запуске.”””
conn = get_conn()
c = conn.cursor()

```
# Пользователи
c.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id      INTEGER PRIMARY KEY,
    username     TEXT,
    first_name   TEXT,
    last_name    TEXT,
    coins        INTEGER DEFAULT 0,
    xp           INTEGER DEFAULT 0,
    level        INTEGER DEFAULT 1,
    streak       INTEGER DEFAULT 0,
    max_streak   INTEGER DEFAULT 0,
    last_active  TEXT,
    is_banned    INTEGER DEFAULT 0,
    has_access   INTEGER DEFAULT 1,
    referrer_id  INTEGER DEFAULT NULL,
    created_at   TEXT DEFAULT (datetime('now'))
)""")

# Premium
c.execute("""
CREATE TABLE IF NOT EXISTS premium (
    user_id         INTEGER PRIMARY KEY,
    active          INTEGER DEFAULT 0,
    expires_at      TEXT,
    source          TEXT,
    last_coin_buy   TEXT,
    FOREIGN KEY(user_id) REFERENCES users(user_id)
)""")

# Темы
c.execute("""
CREATE TABLE IF NOT EXISTS topics (
    topic_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    is_premium  INTEGER DEFAULT 0,
    is_active   INTEGER DEFAULT 1
)""")

# Вопросы
c.execute("""
CREATE TABLE IF NOT EXISTS questions (
    question_id   TEXT PRIMARY KEY,
    topic_id      INTEGER,
    text          TEXT NOT NULL,
    options       TEXT NOT NULL,
    correct_idx   INTEGER NOT NULL,
    difficulty    INTEGER DEFAULT 1,
    is_rare       INTEGER DEFAULT 0,
    explanation   TEXT,
    times_shown   INTEGER DEFAULT 0,
    times_correct INTEGER DEFAULT 0,
    created_at    TEXT DEFAULT (datetime('now')),
    FOREIGN KEY(topic_id) REFERENCES topics(topic_id)
)""")

# Сессии тестирования
c.execute("""
CREATE TABLE IF NOT EXISTS sessions (
    session_id       TEXT PRIMARY KEY,
    user_id          INTEGER,
    topic_id         INTEGER,
    status           TEXT DEFAULT 'active',
    current_q_idx    INTEGER DEFAULT 0,
    questions_order  TEXT,
    correct_count    INTEGER DEFAULT 0,
    wrong_count      INTEGER DEFAULT 0,
    skipped_count    INTEGER DEFAULT 0,
    coins_earned     INTEGER DEFAULT 0,
    xp_earned        INTEGER DEFAULT 0,
    inactivity_count INTEGER DEFAULT 0,
    current_poll_id  TEXT,
    current_q_id     TEXT,
    started_at       TEXT DEFAULT (datetime('now')),
    updated_at       TEXT DEFAULT (datetime('now')),
    FOREIGN KEY(user_id) REFERENCES users(user_id)
)""")

# Лог ответов
c.execute("""
CREATE TABLE IF NOT EXISTS answers_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER,
    question_id  TEXT,
    is_correct   INTEGER,
    answered_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY(user_id)     REFERENCES users(user_id),
    FOREIGN KEY(question_id) REFERENCES questions(question_id)
)""")

# Spaced repetition -- ошибки пользователя
c.execute("""
CREATE TABLE IF NOT EXISTS user_question_stats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER,
    question_id     TEXT,
    wrong_count     INTEGER DEFAULT 0,
    correct_streak  INTEGER DEFAULT 0,
    last_seen       TEXT,
    priority        REAL DEFAULT 1.0,
    UNIQUE(user_id, question_id),
    FOREIGN KEY(user_id)     REFERENCES users(user_id),
    FOREIGN KEY(question_id) REFERENCES questions(question_id)
)""")

# Рефералы
c.execute("""
CREATE TABLE IF NOT EXISTS referrals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id  INTEGER,
    referred_id  INTEGER UNIQUE,
    activated    INTEGER DEFAULT 0,
    coins_given  INTEGER DEFAULT 0,
    created_at   TEXT DEFAULT (datetime('now')),
    FOREIGN KEY(referrer_id) REFERENCES users(user_id),
    FOREIGN KEY(referred_id) REFERENCES users(user_id)
)""")

# Дневной лимит
c.execute("""
CREATE TABLE IF NOT EXISTS daily_limits (
    user_id      INTEGER,
    date         TEXT,
    q_count      INTEGER DEFAULT 0,
    coins_earned INTEGER DEFAULT 0,
    notified     INTEGER DEFAULT 0,
    PRIMARY KEY(user_id, date),
    FOREIGN KEY(user_id) REFERENCES users(user_id)
)""")

# Апелляции
c.execute("""
CREATE TABLE IF NOT EXISTS appeals (
    appeal_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER,
    question_id  TEXT,
    comment      TEXT,
    source       TEXT,
    status       TEXT DEFAULT 'open',
    created_at   TEXT DEFAULT (datetime('now')),
    FOREIGN KEY(user_id)     REFERENCES users(user_id),
    FOREIGN KEY(question_id) REFERENCES questions(question_id)
)""")

# Drafts импорта
c.execute("""
CREATE TABLE IF NOT EXISTS drafts (
    draft_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id    INTEGER,
    data        TEXT,
    status      TEXT DEFAULT 'pending',
    created_at  TEXT DEFAULT (datetime('now'))
)""")

# Лог уведомлений (против спама)
c.execute("""
CREATE TABLE IF NOT EXISTS notifications_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    type       TEXT,
    sent_at    TEXT DEFAULT (datetime('now')),
    FOREIGN KEY(user_id) REFERENCES users(user_id)
)""")

# Обращения (старый функционал A/B/C/D)
c.execute("""
CREATE TABLE IF NOT EXISTS requests (
    request_id   INTEGER PRIMARY KEY,
    user_id      INTEGER,
    reason_code  TEXT,
    reason_title TEXT,
    message_text TEXT,
    msg_type     TEXT,
    caption      TEXT,
    status       TEXT DEFAULT 'open',
    status_text  TEXT DEFAULT 'НЕ ОТВЕЧЕНО ❌',
    answered_by  TEXT,
    user_reaction TEXT,
    created_at   TEXT DEFAULT (datetime('now')),
    FOREIGN KEY(user_id) REFERENCES users(user_id)
)""")

# Настройки бота (ключ-значение)
c.execute("""
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
)""")

# Вставка дефолтных настроек
defaults = [
    ("daily_limit", str(DEFAULT_DAILY_LIMIT)),
    ("premium_coin_daily_limit", str(PREMIUM_COIN_DAILY_LIMIT)),
    ("coin_buy_premium_enabled", "1"),
    ("coins_easy", str(COINS_EASY)),
    ("coins_medium", str(COINS_MEDIUM)),
    ("coins_hard", str(COINS_HARD)),
    ("referral_coins", "50"),
    ("streak_bonus_coins", str(STREAK_BONUS_COINS)),
    ("premium_3d_coins", "500"),
    ("premium_7d_coins", "900"),
    ("channel_id", CHANNEL_ID),
]
for key, val in defaults:
    c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, val))

# Дефолтные темы
default_topics = [
    ("История Казахстана", 0),
    ("Всемирная история", 0),
    ("Хронология и даты", 0),
    ("Ханства и правители", 0),
    ("Монгольский период", 0),
    ("Советский период", 1),       # Premium
    ("ЕНТ Симулятор", 1),          # Premium
]
for tname, is_prem in default_topics:
    c.execute(
        "INSERT OR IGNORE INTO topics(name, is_premium) VALUES(?,?)",
        (tname, is_prem),
    )

conn.commit()
conn.close()
logger.info("База данных инициализирована.")
```

# –– Вспомогательные DB-функции ––

def db_get_setting(key: str, default: str = “”) -> str:
conn = get_conn()
row = conn.execute(“SELECT value FROM settings WHERE key=?”, (key,)).fetchone()
conn.close()
return row[“value”] if row else default

def db_set_setting(key: str, value: str) -> None:
conn = get_conn()
conn.execute(
“INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)”, (key, value)
)
conn.commit()
conn.close()

def db_get_or_create_user(user_id: int, username: str = None,
first_name: str = None, last_name: str = None) -> sqlite3.Row:
conn = get_conn()
row = conn.execute(“SELECT * FROM users WHERE user_id=?”, (user_id,)).fetchone()
if not row:
conn.execute(
“”“INSERT OR IGNORE INTO users(user_id, username, first_name, last_name, last_active)
VALUES(?,?,?,?,datetime(‘now’))”””,
(user_id, username, first_name, last_name),
)
conn.execute(
“INSERT OR IGNORE INTO premium(user_id) VALUES(?)”, (user_id,)
)
conn.commit()
row = conn.execute(“SELECT * FROM users WHERE user_id=?”, (user_id,)).fetchone()
else:
conn.execute(
“”“UPDATE users SET username=?, first_name=?, last_name=?, last_active=datetime(‘now’)
WHERE user_id=?”””,
(username, first_name, last_name, user_id),
)
conn.commit()
conn.close()
return row

def db_is_premium(user_id: int) -> bool:
conn = get_conn()
row = conn.execute(
“SELECT active, expires_at FROM premium WHERE user_id=?”, (user_id,)
).fetchone()
conn.close()
if not row or not row[“active”]:
return False
if row[“expires_at”]:
exp = datetime.fromisoformat(row[“expires_at”])
if exp < datetime.now():
# Истёк – сбрасываем
_conn = get_conn()
_conn.execute(
“UPDATE premium SET active=0 WHERE user_id=?”, (user_id,)
)
_conn.commit()
_conn.close()
return False
return True

def db_grant_premium(user_id: int, days: int, source: str = “admin”) -> datetime:
expires = datetime.now() + timedelta(days=days)
conn = get_conn()
conn.execute(
“”“INSERT OR REPLACE INTO premium(user_id, active, expires_at, source)
VALUES(?,1,?,?)”””,
(user_id, expires.isoformat(), source),
)
conn.commit()
conn.close()
return expires

def db_revoke_premium(user_id: int) -> None:
conn = get_conn()
conn.execute(“UPDATE premium SET active=0, expires_at=NULL WHERE user_id=?”, (user_id,))
conn.commit()
conn.close()

def db_add_coins(user_id: int, amount: int) -> int:
conn = get_conn()
conn.execute(
“UPDATE users SET coins = coins + ? WHERE user_id=?”, (amount, user_id)
)
conn.commit()
row = conn.execute(“SELECT coins FROM users WHERE user_id=?”, (user_id,)).fetchone()
conn.close()
return row[“coins”] if row else 0

def db_spend_coins(user_id: int, amount: int) -> bool:
conn = get_conn()
row = conn.execute(“SELECT coins FROM users WHERE user_id=?”, (user_id,)).fetchone()
if not row or row[“coins”] < amount:
conn.close()
return False
conn.execute(
“UPDATE users SET coins = coins - ? WHERE user_id=?”, (amount, user_id)
)
conn.commit()
conn.close()
return True

def db_add_xp(user_id: int, amount: int) -> tuple[int, int, bool]:
“”“Возвращает (новый xp, новый level, level_up).”””
level_thresholds = [0, 100, 300, 700, 1500, 3000, 6000, 12000]
conn = get_conn()
conn.execute(
“UPDATE users SET xp = xp + ? WHERE user_id=?”, (amount, user_id)
)
conn.commit()
row = conn.execute(“SELECT xp, level FROM users WHERE user_id=?”, (user_id,)).fetchone()
xp = row[“xp”]
old_level = row[“level”]
new_level = old_level
for i, threshold in enumerate(level_thresholds):
if xp >= threshold:
new_level = i + 1
level_up = new_level > old_level
if level_up:
conn.execute(“UPDATE users SET level=? WHERE user_id=?”, (new_level, user_id))
conn.commit()
conn.close()
return xp, new_level, level_up

LEVEL_NAMES = {
1: “🌱 Новичок”,
2: “📖 Абитуриент”,
3: “🎓 Ученик”,
4: “🧠 Знаток”,
5: “⭐ Эксперт”,
6: “🏆 Мастер ЕНТ”,
7: “👑 Легенда”,
}

def db_get_daily(user_id: int) -> sqlite3.Row:
today = datetime.now().strftime(”%Y-%m-%d”)
conn = get_conn()
row = conn.execute(
“SELECT * FROM daily_limits WHERE user_id=? AND date=?”, (user_id, today)
).fetchone()
if not row:
conn.execute(
“INSERT OR IGNORE INTO daily_limits(user_id, date) VALUES(?,?)”,
(user_id, today),
)
conn.commit()
row = conn.execute(
“SELECT * FROM daily_limits WHERE user_id=? AND date=?”, (user_id, today)
).fetchone()
conn.close()
return row

def db_increment_daily(user_id: int, coins: int = 0) -> tuple[int, int]:
“”“Увеличивает счётчик вопросов и монет за день. Возвращает (q_count, coins_earned).”””
today = datetime.now().strftime(”%Y-%m-%d”)
conn = get_conn()
conn.execute(
“”“INSERT OR IGNORE INTO daily_limits(user_id, date) VALUES(?,?)”””,
(user_id, today),
)
conn.execute(
“”“UPDATE daily_limits SET q_count=q_count+1, coins_earned=coins_earned+?
WHERE user_id=? AND date=?”””,
(coins, user_id, today),
)
conn.commit()
row = conn.execute(
“SELECT q_count, coins_earned FROM daily_limits WHERE user_id=? AND date=?”,
(user_id, today),
).fetchone()
conn.close()
return row[“q_count”], row[“coins_earned”]

def db_get_questions(topic_id: int, limit: int = 200) -> list[sqlite3.Row]:
conn = get_conn()
rows = conn.execute(
“SELECT * FROM questions WHERE topic_id=? ORDER BY RANDOM() LIMIT ?”,
(topic_id, limit),
).fetchall()
conn.close()
return rows

def db_get_question(question_id: str) -> Optional[sqlite3.Row]:
conn = get_conn()
row = conn.execute(
“SELECT * FROM questions WHERE question_id=?”, (question_id,)
).fetchone()
conn.close()
return row

def db_get_user_priority_questions(user_id: int, topic_id: int, limit: int = 50) -> list[sqlite3.Row]:
“”“Возвращает вопросы с учётом spaced repetition (ошибки – приоритет выше).”””
conn = get_conn()
rows = conn.execute(”””
SELECT q.*, COALESCE(uqs.priority, 1.0) as prio
FROM questions q
LEFT JOIN user_question_stats uqs
ON uqs.question_id = q.question_id AND uqs.user_id = ?
WHERE q.topic_id = ?
ORDER BY prio DESC, RANDOM()
LIMIT ?
“””, (user_id, topic_id, limit)).fetchall()
conn.close()
return rows

def db_update_question_stats(user_id: int, question_id: str, is_correct: bool) -> None:
conn = get_conn()
row = conn.execute(
“SELECT * FROM user_question_stats WHERE user_id=? AND question_id=?”,
(user_id, question_id),
).fetchone()

```
now = datetime.now().isoformat()

if not row:
    wrong = 0 if is_correct else 1
    correct_s = 1 if is_correct else 0
    priority = 0.5 if is_correct else 2.0
    conn.execute(
        """INSERT INTO user_question_stats(user_id,question_id,wrong_count,
           correct_streak,last_seen,priority)
           VALUES(?,?,?,?,?,?)""",
        (user_id, question_id, wrong, correct_s, now, priority),
    )
else:
    if is_correct:
        correct_s = row["correct_streak"] + 1
        wrong_c = row["wrong_count"]
        # Чем больше правильных подряд -- тем ниже приоритет
        priority = max(0.1, row["priority"] * 0.6)
    else:
        correct_s = 0
        wrong_c = row["wrong_count"] + 1
        # Ошибка -- приоритет растёт
        priority = min(5.0, row["priority"] * 1.8 + 1.0)

    conn.execute(
        """UPDATE user_question_stats
           SET wrong_count=?, correct_streak=?, last_seen=?, priority=?
           WHERE user_id=? AND question_id=?""",
        (wrong_c, correct_s, now, priority, user_id, question_id),
    )

conn.execute(
    "INSERT INTO answers_log(user_id,question_id,is_correct) VALUES(?,?,?)",
    (user_id, question_id, 1 if is_correct else 0),
)
conn.commit()
conn.close()
```

def db_get_session(user_id: int) -> Optional[sqlite3.Row]:
conn = get_conn()
row = conn.execute(
“SELECT * FROM sessions WHERE user_id=? AND status IN (‘active’,‘paused’) ORDER BY updated_at DESC LIMIT 1”,
(user_id,),
).fetchone()
conn.close()
return row

def db_create_session(user_id: int, topic_id: int, question_ids: list[str]) -> str:
import json
session_id = f”S-{user_id}-{int(time.time())}”
conn = get_conn()
# Закрываем предыдущие сессии
conn.execute(
“UPDATE sessions SET status=‘finished’ WHERE user_id=? AND status IN (‘active’,‘paused’)”,
(user_id,),
)
conn.execute(
“”“INSERT INTO sessions(session_id,user_id,topic_id,questions_order,status)
VALUES(?,?,?,?,‘active’)”””,
(session_id, user_id, topic_id, json.dumps(question_ids)),
)
conn.commit()
conn.close()
return session_id

def db_update_session(session_id: str, **kwargs) -> None:
if not kwargs:
return
conn = get_conn()
kwargs[“updated_at”] = datetime.now().isoformat()
sets = “, “.join(f”{k}=?” for k in kwargs)
vals = list(kwargs.values()) + [session_id]
conn.execute(f”UPDATE sessions SET {sets} WHERE session_id=?”, vals)
conn.commit()
conn.close()

def db_finish_session(session_id: str) -> None:
conn = get_conn()
conn.execute(
“UPDATE sessions SET status=‘finished’, updated_at=datetime(‘now’) WHERE session_id=?”,
(session_id,),
)
conn.commit()
conn.close()

def db_get_leaderboard(limit: int = 10, period: str = “all”) -> list[sqlite3.Row]:
conn = get_conn()
if period == “day”:
today = datetime.now().strftime(”%Y-%m-%d”)
rows = conn.execute(”””
SELECT u.user_id, u.username, u.first_name, dl.q_count as score
FROM users u
JOIN daily_limits dl ON dl.user_id=u.user_id AND dl.date=?
WHERE u.is_banned=0
ORDER BY dl.q_count DESC LIMIT ?
“””, (today, limit)).fetchall()
else:
rows = conn.execute(”””
SELECT user_id, username, first_name, xp as score
FROM users WHERE is_banned=0
ORDER BY xp DESC LIMIT ?
“””, (limit,)).fetchall()
conn.close()
return rows

def db_get_user_rank(user_id: int) -> tuple[int, int]:
“”“Возвращает (ранг, всего пользователей).”””
conn = get_conn()
total = conn.execute(“SELECT COUNT(*) as cnt FROM users WHERE is_banned=0”).fetchone()[“cnt”]
rank_row = conn.execute(”””
SELECT COUNT(*) as cnt FROM users
WHERE xp > (SELECT xp FROM users WHERE user_id=?) AND is_banned=0
“””, (user_id,)).fetchone()
rank = (rank_row[“cnt”] if rank_row else 0) + 1
conn.close()
return rank, total

def db_notification_sent_today(user_id: int, ntype: str) -> bool:
today = datetime.now().strftime(”%Y-%m-%d”)
conn = get_conn()
row = conn.execute(
“SELECT id FROM notifications_log WHERE user_id=? AND type=? AND sent_at LIKE ?”,
(user_id, ntype, f”{today}%”),
).fetchone()
conn.close()
return row is not None

def db_log_notification(user_id: int, ntype: str) -> None:
conn = get_conn()
conn.execute(
“INSERT INTO notifications_log(user_id,type) VALUES(?,?)”, (user_id, ntype)
)
conn.commit()
conn.close()

def db_get_all_topics(include_premium: bool = True) -> list[sqlite3.Row]:
conn = get_conn()
if include_premium:
rows = conn.execute(
“SELECT * FROM topics WHERE is_active=1 ORDER BY topic_id”
).fetchall()
else:
rows = conn.execute(
“SELECT * FROM topics WHERE is_active=1 AND is_premium=0 ORDER BY topic_id”
).fetchall()
conn.close()
return rows

def db_topic_question_count(topic_id: int) -> int:
conn = get_conn()
row = conn.execute(
“SELECT COUNT(*) as cnt FROM questions WHERE topic_id=?”, (topic_id,)
).fetchone()
conn.close()
return row[“cnt”] if row else 0

def db_add_question(topic_id: int, text: str, options: list[str],
correct_idx: int, difficulty: int = 1,
explanation: str = None) -> str:
import json
qid = f”Q-{str(id(text))[-6:].upper()}{random.randint(10,99)}”
conn = get_conn()
conn.execute(
“”“INSERT INTO questions(question_id,topic_id,text,options,correct_idx,difficulty,explanation)
VALUES(?,?,?,?,?,?,?)”””,
(qid, topic_id, text, json.dumps(options, ensure_ascii=False),
correct_idx, difficulty, explanation),
)
conn.commit()
conn.close()
return qid

def db_delete_question(question_id: str) -> None:
conn = get_conn()
conn.execute(“DELETE FROM questions WHERE question_id=?”, (question_id,))
conn.commit()
conn.close()

def db_search_questions(query: str, limit: int = 10) -> list[sqlite3.Row]:
conn = get_conn()
rows = conn.execute(
“SELECT * FROM questions WHERE text LIKE ? LIMIT ?”,
(f”%{query}%”, limit),
).fetchall()
conn.close()
return rows

# =========================================

# STATES (для FSM через context.user_data)

# =========================================

# Ключи состояний пользователя

STATE_STARTED          = “started”
STATE_REASON_SELECTED  = “reason_selected”
STATE_REASON_CODE      = “reason_code”
STATE_REASON_TITLE     = “reason_title”
STATE_IN_QUIZ          = “in_quiz”
STATE_SESSION_ID       = “session_id”
STATE_TOPIC_ID         = “topic_id”
STATE_APPEAL_ACTIVE    = “appeal_active”
STATE_APPEAL_Q_ID      = “appeal_q_id”
STATE_WAIT_APPEAL_TEXT = “wait_appeal_text”
STATE_ADMIN_REPLY_ID   = “reply_request_id”
STATE_ADMIN_REPLY_MSG  = “reply_prompt_message_id”
STATE_ADMIN_SECTION    = “admin_section”
STATE_ADMIN_IMPORT     = “admin_import_mode”
STATE_ADMIN_DRAFT_ID   = “admin_draft_id”
STATE_ADMIN_FIND_USER  = “admin_find_user”
STATE_ADMIN_BROADCAST  = “admin_broadcast_mode”
STATE_BROADCAST_MSG    = “broadcast_msg”
STATE_BROADCAST_TARGET = “broadcast_target”

# In-memory хранилище обращений (для обратной совместимости + скорости)

REQUESTS: dict[int, dict] = {}
RESPONSES: dict[int, dict] = {}
REQUEST_SEQ = count(1001)
RESPONSE_SEQ = count(5001)
BLOCKED_USERS: set[int] = set()
FINISHED_USERS: set[int] = set()

# Хранение poll_id -> (user_id, session_id, question_id)

ACTIVE_POLLS: dict[str, dict] = {}

# =========================================

# UTILS

# =========================================

def is_admin(user_id: int) -> bool:
return user_id in ADMINS

def safe_username(user) -> str:
if user.username:
return f”@{escape(user.username)}”
return escape(user.first_name or “пользователь”)

def admin_name(user) -> str:
if user.username:
return f”@{escape(user.username)}”
return escape(user.first_name or “Администратор”)

def user_mention_html(user_info: dict) -> str:
if user_info.get(“username”):
return f”@{escape(user_info[‘username’])}”
uid = user_info[“id”]
name = escape(user_info.get(“first_name”) or “Открыть профиль”)
return f’<a href="tg://user?id={uid}">{name}</a>’

def get_reason_title(reason_code: str) -> str:
return {
“block”: “🔐 Вопрос по блокировке аккаунта”,
“coop”:  “🤝 Вопрос по сотрудничеству”,
“tests”: “🧠 Покупка тестов”,
“other”: “📕 Свой вопрос”,
}.get(reason_code, “Не выбрано”)

def get_reason_text(reason_code: str) -> str:
if reason_code == “block”:
return “Напишите жалобу:”
if reason_code == “coop”:
return (
“Хорошо ☺️\nДля размещения рекламы ответьте на вопросы:\n\n”
“1️⃣ Укажите тематику рекламы.\n”
“2️⃣ Отправьте готовый рекламный пост.\n”
“3️⃣ На какой срок?\n”
“4️⃣ Дополнительные условия?\n\n”
“📩 После получения рассчитаем стоимость.”
)
if reason_code == “tests”:
return “Платные тесты пока недоступны 🥹\nВы можете проходить бесплатные тесты через меню E.”
return “Напишите ваш вопрос, и мы ответим:”

def detect_message_type(message) -> str:
if message.text:        return “текст”
if message.voice:       return “голосовое”
if message.photo:       return “фото”
if message.video:       return “видео”
if message.document:    return “документ”
if message.audio:       return “аудио”
if message.sticker:     return “стикер”
if message.video_note:  return “видеосообщение”
if message.animation:   return “GIF”
return “другое”

async def delete_message_later(context, chat_id: int, message_id: int, delay: int = 5):
try:
await asyncio.sleep(delay)
await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
except Exception:
pass

async def try_delete(message):
try:
await message.delete()
except Exception:
pass

async def safe_send(bot, chat_id: int, text: str, **kwargs) -> bool:
“”“Отправляет сообщение без краша если пользователь заблокировал бота.”””
try:
await bot.send_message(chat_id=chat_id, text=text, **kwargs)
return True
except Forbidden:
logger.info(f”Пользователь {chat_id} заблокировал бота.”)
return False
except TelegramError as e:
logger.warning(f”Ошибка отправки {chat_id}: {e}”)
return False

async def check_subscription(bot, user_id: int) -> bool:
“”“Проверяет подписку на канал.”””
channel = db_get_setting(“channel_id”, CHANNEL_ID)
if not channel:
return True  # Если канал не настроен – пропускаем проверку
try:
member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
return member.status not in (“left”, “kicked”)
except TelegramError as e:
logger.warning(f”Ошибка проверки подписки: {e}”)
return True  # В случае ошибки – не блокируем

# =========================================

# KEYBOARDS

# =========================================

def reason_keyboard() -> InlineKeyboardMarkup:
return InlineKeyboardMarkup([
[InlineKeyboardButton(“🔐 A) Вопрос по блокировке аккаунта”, callback_data=“reason:block”)],
[InlineKeyboardButton(“🤝 B) Вопрос по сотрудничеству”,      callback_data=“reason:coop”)],
[InlineKeyboardButton(“🧠 C) Покупка тестов”,                 callback_data=“reason:tests”)],
[InlineKeyboardButton(“📕 D) Свой вопрос”,                    callback_data=“reason:other”)],
[InlineKeyboardButton(“📝 E) Проходить тесты”,                callback_data=“quiz:menu”)],
])

def back_keyboard() -> InlineKeyboardMarkup:
return InlineKeyboardMarkup([
[InlineKeyboardButton(“⬅️ Назад”, callback_data=“reason:back”)]
])

def back_to_menu_keyboard() -> InlineKeyboardMarkup:
return InlineKeyboardMarkup([
[InlineKeyboardButton(“🏠 В главное меню”, callback_data=“main:menu”)]
])

def subscribe_keyboard() -> InlineKeyboardMarkup:
channel = db_get_setting(“channel_id”, CHANNEL_ID)
return InlineKeyboardMarkup([
[InlineKeyboardButton(“📢 Подписаться на канал”, url=f”https://t.me/{channel.lstrip(’@’)}”)],
[InlineKeyboardButton(“✅ Проверить подписку”, callback_data=“quiz:check_sub”)],
])

def topics_keyboard(is_premium_user: bool) -> InlineKeyboardMarkup:
topics = db_get_all_topics()
rows = []
for t in topics:
name = t[“name”]
if t[“is_premium”] and not is_premium_user:
name = f”🔒 {name} [Premium]”
cb = f”quiz:locked:{t[‘topic_id’]}”
else:
cnt = db_topic_question_count(t[“topic_id”])
name = f”{name} ({cnt})”
cb = f”quiz:topic:{t[‘topic_id’]}”
rows.append([InlineKeyboardButton(name, callback_data=cb)])

```
rows.append([InlineKeyboardButton("🔁 Мои ошибки",        callback_data="quiz:errors")])
rows.append([InlineKeyboardButton("🎲 Случайные вопросы", callback_data="quiz:random")])
rows.append([InlineKeyboardButton("🏠 В меню",            callback_data="main:menu")])
return InlineKeyboardMarkup(rows)
```

def quiz_stop_keyboard(session_id: str) -> InlineKeyboardMarkup:
return InlineKeyboardMarkup([
[
InlineKeyboardButton(“⏹ СТОП”,      callback_data=f”quiz:stop:{session_id}”),
InlineKeyboardButton(“🚨 Апелляция”, callback_data=f”quiz:appeal:{session_id}”),
]
])

def quiz_paused_keyboard(session_id: str) -> InlineKeyboardMarkup:
return InlineKeyboardMarkup([
[InlineKeyboardButton(“▶️ Продолжить тест”, callback_data=f”quiz:resume:{session_id}”)],
[InlineKeyboardButton(“🏠 В меню”,           callback_data=“main:menu”)],
[InlineKeyboardButton(“💎 Купить Premium”,   callback_data=“premium:menu”)],
])

def limit_reached_keyboard() -> InlineKeyboardMarkup:
return InlineKeyboardMarkup([
[InlineKeyboardButton(“💎 Купить Premium”,   callback_data=“premium:menu”)],
[InlineKeyboardButton(“👥 Пригласить друга”, callback_data=“referral:menu”)],
[InlineKeyboardButton(“🏠 В меню”,           callback_data=“main:menu”)],
])

def premium_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
coin_enabled = db_get_setting(“coin_buy_premium_enabled”, “1”) == “1”
rows = [
[InlineKeyboardButton(“💳 Купить Premium за деньги”, callback_data=“premium:buy_money”)],
]
if coin_enabled:
rows.append([InlineKeyboardButton(“🪙 Купить Premium за монеты”, callback_data=“premium:buy_coins”)])
rows.append([InlineKeyboardButton(“⬅️ Назад”, callback_data=“main:menu”)])
return InlineKeyboardMarkup(rows)

def premium_coins_keyboard() -> InlineKeyboardMarkup:
p3 = db_get_setting(“premium_3d_coins”, “500”)
p7 = db_get_setting(“premium_7d_coins”, “900”)
return InlineKeyboardMarkup([
[InlineKeyboardButton(f”3 дня – {p3} монет 🪙”,  callback_data=“premium:coin_buy:3d”)],
[InlineKeyboardButton(f”7 дней – {p7} монет 🪙”, callback_data=“premium:coin_buy:7d”)],
[InlineKeyboardButton(“⬅️ Назад”,                callback_data=“premium:menu”)],
])

def profile_keyboard() -> InlineKeyboardMarkup:
return InlineKeyboardMarkup([
[InlineKeyboardButton(“📊 Детальная статистика”, callback_data=“profile:stats”)],
[InlineKeyboardButton(“🏆 Рейтинг”,             callback_data=“leaderboard:all”)],
[InlineKeyboardButton(“👥 Рефералы”,            callback_data=“referral:menu”)],
[InlineKeyboardButton(“🏠 В меню”,              callback_data=“main:menu”)],
])

def leaderboard_keyboard(current: str = “all”) -> InlineKeyboardMarkup:
def mark(k):
return f”✅ {k}” if k == current else k
return InlineKeyboardMarkup([
[
InlineKeyboardButton(mark(“Сегодня”),  callback_data=“leaderboard:day”),
InlineKeyboardButton(mark(“Всё время”), callback_data=“leaderboard:all”),
],
[InlineKeyboardButton(“🏠 В меню”, callback_data=“main:menu”)],
])

def user_response_keyboard(response_id: int, selected: str = None) -> InlineKeyboardMarkup:
left  = “✅ 👍” if selected == “👍”   else “👍”
right = “✅ 🫶🏻” if selected == “🫶🏻” else “🫶🏻”
return InlineKeyboardMarkup([[
InlineKeyboardButton(left,  callback_data=f”userreact:{response_id}:👍”),
InlineKeyboardButton(right, callback_data=f”userreact:{response_id}:🫶🏻”),
]])

def admin_card_keyboard(request_id: int) -> InlineKeyboardMarkup:
req = REQUESTS.get(request_id, {})
like_count  = len(req.get(“admin_reactions”, {}).get(“👍”, set()))
heart_count = len(req.get(“admin_reactions”, {}).get(“🫶🏻”, set()))
user_id = req.get(“user”, {}).get(“id”, 0)
is_blocked_user = user_id in BLOCKED_USERS

```
second_row = [InlineKeyboardButton("✅ Завершить диалог", callback_data=f"finish:{request_id}")]
if is_blocked_user:
    second_row.append(InlineKeyboardButton("🔓 Разблокировать", callback_data=f"unblock:{request_id}"))
else:
    second_row.append(InlineKeyboardButton("🚫 Заблокировать", callback_data=f"block:{request_id}"))

return InlineKeyboardMarkup([
    [InlineKeyboardButton("✍️ Ответить", callback_data=f"reply:{request_id}")],
    second_row,
    [
        InlineKeyboardButton(f"👍 {like_count}",  callback_data=f"adminreact:{request_id}:👍"),
        InlineKeyboardButton(f"🫶🏻 {heart_count}", callback_data=f"adminreact:{request_id}:🫶🏻"),
    ],
])
```

def admin_main_keyboard() -> InlineKeyboardMarkup:
return InlineKeyboardMarkup([
[
InlineKeyboardButton(“📊 Статистика”,   callback_data=“adm:stats”),
InlineKeyboardButton(“🧠 Тесты”,        callback_data=“adm:tests”),
],
[
InlineKeyboardButton(“📥 Импорт”,       callback_data=“adm:import”),
InlineKeyboardButton(“👥 Пользователи”, callback_data=“adm:users”),
],
[
InlineKeyboardButton(“💎 Premium”,      callback_data=“adm:premium”),
InlineKeyboardButton(“🪙 Экономика”,    callback_data=“adm:economy”),
],
[
InlineKeyboardButton(“🏆 Рейтинги”,     callback_data=“adm:leaderboard”),
InlineKeyboardButton(“📢 Рассылка”,     callback_data=“adm:broadcast”),
],
[
InlineKeyboardButton(“🚨 Апелляции”,    callback_data=“adm:appeals”),
InlineKeyboardButton(“⚙️ Настройки”,    callback_data=“adm:settings”),
],
[
InlineKeyboardButton(“🔒 Доступы”,      callback_data=“adm:access”),
],
])

# =========================================

# QUIZ SYSTEM

# =========================================

import json as _json

def build_question_order(user_id: int, topic_id: int) -> list[str]:
“””
Формирует список ID вопросов для сессии.
80% новые/случайные, 20% ошибочные (spaced repetition).
“””
all_q = db_get_user_priority_questions(user_id, topic_id, limit=200)
if not all_q:
return []

```
# Разделяем на ошибочные (приоритет > 1.5) и обычные
errors = [q["question_id"] for q in all_q if (q["prio"] if "prio" in q.keys() else 1.0) > 1.5]
normal = [q["question_id"] for q in all_q if (q["prio"] if "prio" in q.keys() else 1.0) <= 1.5]

random.shuffle(errors)
random.shuffle(normal)

# 20% ошибочных вперёд, 80% обычных, перемешать с разбросом
max_q = 60
n_errors = min(len(errors), int(max_q * 0.2))
n_normal = min(len(normal), max_q - n_errors)

chosen_errors = errors[:n_errors]
chosen_normal = normal[:n_normal]

# Вставляем ошибочные равномерно (не подряд, через 5-10)
result = []
err_idx = 0
for i, q in enumerate(chosen_normal):
    result.append(q)
    if err_idx < len(chosen_errors) and (i + 1) % random.randint(5, 10) == 0:
        result.append(chosen_errors[err_idx])
        err_idx += 1

# Добавляем оставшиеся ошибочные в конец
result.extend(chosen_errors[err_idx:])
return result
```

async def send_next_question(bot, user_id: int, session_row: sqlite3.Row,
context: ContextTypes.DEFAULT_TYPE) -> bool:
“””
Отправляет следующий Quiz Poll.
Возвращает False если вопросы закончились или лимит исчерпан.
“””
import json as _json

```
session_id = session_row["session_id"]
q_order = _json.loads(session_row["questions_order"])
current_idx = session_row["current_q_idx"]

# Проверяем лимит
is_prem = db_is_premium(user_id)
daily_limit = int(db_get_setting("daily_limit", str(DEFAULT_DAILY_LIMIT)))
daily = db_get_daily(user_id)

if not is_prem and daily["q_count"] >= daily_limit:
    return False  # Лимит исчерпан

if current_idx >= len(q_order):
    return False  # Вопросы закончились

question_id = q_order[current_idx]
q = db_get_question(question_id)
if not q:
    # Вопрос удалён -- пропускаем
    db_update_session(session_id, current_q_idx=current_idx + 1)
    new_row = db_get_session(user_id)
    if new_row:
        return await send_next_question(bot, user_id, new_row, context)
    return False

options = _json.loads(q["options"])

# Отправляем Quiz Poll
try:
    msg = await bot.send_poll(
        chat_id=user_id,
        question=f"❓ #{current_idx + 1} | {q['text']}",
        options=options,
        type="quiz",
        correct_option_id=q["correct_idx"],
        is_anonymous=False,
        open_period=QUESTION_TIMEOUT_SEC,
        explanation=q["explanation"] or "",
        protect_content=True,  # Защита от пересылки
    )

    # Кнопки СТОП и Апелляция
    await bot.send_message(
        chat_id=user_id,
        text=(
            f"📚 Тема | Вопрос {current_idx + 1}/{len(q_order)}\n"
            f"🔥 Серия: {context.bot_data.get(f'streak_{user_id}', 0)} | "
            f"⭐ {'Лёгкий' if q['difficulty']==1 else 'Средний' if q['difficulty']==2 else 'Сложный'}"
        ),
        reply_markup=quiz_stop_keyboard(session_id),
    )

    # Сохраняем poll в памяти
    ACTIVE_POLLS[msg.poll.id] = {
        "user_id":    user_id,
        "session_id": session_id,
        "question_id": question_id,
        "sent_at":    time.time(),
    }

    # Обновляем сессию
    db_update_session(
        session_id,
        current_q_idx=current_idx + 1,
        current_poll_id=msg.poll.id,
        current_q_id=question_id,
        inactivity_count=0,
    )

    # Сбрасываем счётчик неактивности в context
    context.bot_data[f"inactivity_{user_id}"] = 0

    return True

except Forbidden:
    logger.info(f"Пользователь {user_id} заблокировал бота во время теста.")
    db_finish_session(session_id)
    return False
except TelegramError as e:
    logger.warning(f"Ошибка отправки вопроса {user_id}: {e}")
    return False
```

async def handle_quiz_answer(user_id: int, poll_id: str, option_ids: list[int],
context: ContextTypes.DEFAULT_TYPE) -> None:
“”“Обрабатывает ответ на Quiz Poll.”””
poll_data = ACTIVE_POLLS.pop(poll_id, None)
if not poll_data:
return

```
if poll_data["user_id"] != user_id:
    return

session_id  = poll_data["session_id"]
question_id = poll_data["question_id"]

session = db_get_session(user_id)
if not session or session["session_id"] != session_id:
    return
if session["status"] != "active":
    return

q = db_get_question(question_id)
if not q:
    return

is_correct = len(option_ids) > 0 and option_ids[0] == q["correct_idx"]

# Обновляем spaced repetition
db_update_question_stats(user_id, question_id, is_correct)

# Подсчёт монет
is_prem = db_is_premium(user_id)
coin_daily_limit = int(db_get_setting("premium_coin_daily_limit", str(PREMIUM_COIN_DAILY_LIMIT)))
daily = db_get_daily(user_id)

coins_earned = 0
xp_earned    = XP_PER_CORRECT if is_correct else XP_PER_WRONG

if is_correct:
    difficulty = q["difficulty"]
    if difficulty == 1:
        base_coins = int(db_get_setting("coins_easy",   str(COINS_EASY)))
    elif difficulty == 2:
        base_coins = int(db_get_setting("coins_medium", str(COINS_MEDIUM)))
    else:
        base_coins = int(db_get_setting("coins_hard",   str(COINS_HARD)))

    # Premium ограничен по монетам в день
    can_earn = True
    if is_prem and daily["coins_earned"] >= coin_daily_limit:
        can_earn = False

    if can_earn:
        coins_earned = base_coins

        # Streak бонус
        streak_key = f"streak_{user_id}"
        streak = context.bot_data.get(streak_key, 0) + 1
        context.bot_data[streak_key] = streak
        bonus_every = int(db_get_setting("streak_bonus_coins", str(STREAK_BONUS_COINS)))
        if streak % STREAK_BONUS_EVERY == 0:
            coins_earned += bonus_every

        db_add_coins(user_id, coins_earned)
    else:
        # Сообщаем один раз что монеты исчерпаны
        if not db_notification_sent_today(user_id, "premium_coin_limit"):
            await safe_send(
                context.bot, user_id,
                "✅ Тестирование продолжается без ограничений\n"
                "🪙 Дневной лимит монет исчерпан\n"
                "Завтра снова сможете зарабатывать монеты",
            )
            db_log_notification(user_id, "premium_coin_limit")
else:
    # Сбрасываем streak
    context.bot_data[f"streak_{user_id}"] = 0

db_add_xp(user_id, xp_earned)
q_count, coins_today = db_increment_daily(user_id, coins_earned)

# Обновляем сессию
if is_correct:
    db_update_session(session_id, correct_count=session["correct_count"] + 1,
                      coins_earned=session["coins_earned"] + coins_earned,
                      xp_earned=session["xp_earned"] + xp_earned)
else:
    db_update_session(session_id, wrong_count=session["wrong_count"] + 1,
                      xp_earned=session["xp_earned"] + xp_earned)

# Проверяем лимит после ответа
daily_limit = int(db_get_setting("daily_limit", str(DEFAULT_DAILY_LIMIT)))
if not is_prem and q_count >= daily_limit:
    await show_limit_reached(context.bot, user_id, session_id, context)
    return

# Следующий вопрос
session = db_get_session(user_id)
if session and session["status"] == "active":
    await send_next_question(context.bot, user_id, session, context)
```

async def show_limit_reached(bot, user_id: int, session_id: str,
context: ContextTypes.DEFAULT_TYPE) -> None:
“”“Показывает экран исчерпания лимита.”””
db_finish_session(session_id)
session = db_get_session(user_id)
daily = db_get_daily(user_id)
rank, total = db_get_user_rank(user_id)
pct = max(0, round((1 - rank / max(total, 1)) * 100))

```
# Слабые темы из ошибок
conn = get_conn()
weak = conn.execute("""
    SELECT t.name, COUNT(*) as cnt
    FROM answers_log al
    JOIN questions q ON q.question_id=al.question_id
    JOIN topics t ON t.topic_id=q.topic_id
    WHERE al.user_id=? AND al.is_correct=0
    AND date(al.answered_at)=date('now')
    GROUP BY t.topic_id ORDER BY cnt DESC LIMIT 3
""", (user_id,)).fetchall()
conn.close()

weak_text = "\n".join(f"-- {w['name']}" for w in weak) if weak else "-- нет данных"

# Время до следующего лимита
now = datetime.now()
tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0)
diff = tomorrow - now
h = diff.seconds // 3600
m = (diff.seconds % 3600) // 60
s = diff.seconds % 60

text = (
    f"🔥 Сегодня вы решили {daily['q_count']} вопросов!\n"
    f"🏆 Вы вошли в ТОП {pct}%\n"
    f"📈 Серия: {context.bot_data.get(f'streak_{user_id}', 0)}\n\n"
    f"⚠️ Слабые темы:\n{weak_text}\n\n"
    f"⏳ Лимит обновится через: {h:02d}:{m:02d}:{s:02d}"
)

await safe_send(bot, user_id, text, reply_markup=limit_reached_keyboard())
```

async def show_quiz_results(bot, user_id: int, session_id: str,
context: ContextTypes.DEFAULT_TYPE) -> None:
“”“Показывает итоги теста.”””
conn = get_conn()
session = conn.execute(
“SELECT * FROM sessions WHERE session_id=?”, (session_id,)
).fetchone()
conn.close()

```
if not session:
    return

total  = session["correct_count"] + session["wrong_count"]
pct    = round(session["correct_count"] / max(total, 1) * 100, 1)
rank, total_users = db_get_user_rank(user_id)
rank_pct = max(0, round((1 - rank / max(total_users, 1)) * 100))

# Сильные/слабые темы
conn = get_conn()
strong = conn.execute("""
    SELECT t.name, 
           SUM(al.is_correct)*100.0/COUNT(*) as acc
    FROM answers_log al
    JOIN questions q ON q.question_id=al.question_id
    JOIN topics t ON t.topic_id=q.topic_id
    WHERE al.user_id=?
    GROUP BY t.topic_id HAVING COUNT(*)>=3
    ORDER BY acc DESC LIMIT 2
""", (user_id,)).fetchall()
weak = conn.execute("""
    SELECT t.name,
           SUM(al.is_correct)*100.0/COUNT(*) as acc
    FROM answers_log al
    JOIN questions q ON q.question_id=al.question_id
    JOIN topics t ON t.topic_id=q.topic_id
    WHERE al.user_id=?
    GROUP BY t.topic_id HAVING COUNT(*)>=3
    ORDER BY acc ASC LIMIT 2
""", (user_id,)).fetchall()
conn.close()

strong_text = "\n".join(f"-- {r['name']} ({r['acc']:.0f}%)" for r in strong) or "-- нет данных"
weak_text   = "\n".join(f"-- {r['name']} ({r['acc']:.0f}%)" for r in weak)   or "-- нет данных"
streak = context.bot_data.get(f"streak_{user_id}", 0)

text = (
    "━━━━━━━━━━━━━━━━━\n"
    "🏁 РЕЗУЛЬТАТЫ ТЕСТА\n"
    "━━━━━━━━━━━━━━━━━\n"
    f"✅ Правильных: {session['correct_count']} из {total}\n"
    f"📊 Результат: {pct}%\n"
    f"🔥 Серия: {streak}\n"
    f"⭐ XP получено: +{session['xp_earned']}\n"
    f"🪙 Монеты: +{session['coins_earned']}\n\n"
    f"🏆 Вы обошли {rank_pct}% участников\n"
    f"📈 Ваш рейтинг: #{rank}\n\n"
    f"✅ Сильные темы:\n{strong_text}\n\n"
    f"⚠️ Слабые темы:\n{weak_text}\n"
    "━━━━━━━━━━━━━━━━━"
)

kb = InlineKeyboardMarkup([
    [InlineKeyboardButton("▶️ Пройти ещё",          callback_data="quiz:menu")],
    [InlineKeyboardButton("📊 Полная статистика",    callback_data="profile:stats")],
    [InlineKeyboardButton("🏠 В меню",               callback_data="main:menu")],
])

await safe_send(bot, user_id, text, reply_markup=kb)
db_finish_session(session_id)
```

async def pause_session(bot, user_id: int, session_id: str,
context: ContextTypes.DEFAULT_TYPE) -> None:
“”“Ставит сессию на паузу из-за неактивности.”””
db_update_session(session_id, status=“paused”)

```
session = db_get_session(user_id)
if not session:
    return

conn = get_conn()
topic = conn.execute(
    "SELECT name FROM topics WHERE topic_id=?", (session["topic_id"],)
).fetchone()
conn.close()

topic_name = topic["name"] if topic else "--"
streak = context.bot_data.get(f"streak_{user_id}", 0)
rank, _ = db_get_user_rank(user_id)

text = (
    "⏸ Тестирование приостановлено\n\n"
    "Вы не отвечали несколько раз подряд.\n"
    "Прогресс сохранён:\n\n"
    f"📚 Тема: {topic_name}\n"
    f"✅ Правильных: {session['correct_count']}\n"
    f"🔥 Серия: {streak}\n"
    f"🏆 Рейтинг: #{rank}"
)
await safe_send(
    bot, user_id, text,
    reply_markup=quiz_paused_keyboard(session_id),
)

# Планируем reminder через 30 минут
context.application.job_queue.run_once(
    _send_inactivity_reminder,
    when=timedelta(minutes=INACTIVITY_REMINDER_MIN),
    data={"user_id": user_id, "session_id": session_id},
    name=f"remind_{user_id}",
)
```

async def _send_inactivity_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
“”“Job: напоминание вернуться к тесту.”””
data = context.job.data
user_id    = data[“user_id”]
session_id = data[“session_id”]

```
# Проверяем что сессия всё ещё на паузе
conn = get_conn()
session = conn.execute(
    "SELECT * FROM sessions WHERE session_id=? AND status='paused'", (session_id,)
).fetchone()
conn.close()

if not session:
    return

if db_notification_sent_today(user_id, f"remind_{session_id}"):
    return

conn = get_conn()
topic = conn.execute(
    "SELECT name FROM topics WHERE topic_id=?", (session["topic_id"],)
).fetchone()
conn.close()

topic_name = topic["name"] if topic else "--"

text = (
    "🔥 Вы остановились почти на середине теста!\n\n"
    f"📚 Тема: {topic_name}\n"
    f"✅ Правильных: {session['correct_count']}\n"
    "🏆 До нового уровня осталось совсем немного"
)
kb = InlineKeyboardMarkup([
    [InlineKeyboardButton("▶️ Продолжить тест", callback_data=f"quiz:resume:{session_id}")]
])
await safe_send(context.bot, user_id, text, reply_markup=kb)
db_log_notification(user_id, f"remind_{session_id}")
```

# =========================================

# ECONOMY & PREMIUM

# =========================================

async def process_premium_buy_coins(bot, user_id: int, plan: str,
context: ContextTypes.DEFAULT_TYPE) -> None:
“”“Обрабатывает покупку Premium за монеты.”””
if not COIN_BUY_PREMIUM_ENABLED:
await safe_send(bot, user_id, “❌ Покупка Premium за монеты временно недоступна.”)
return

```
if db_is_premium(user_id):
    await safe_send(bot, user_id, "❌ У вас уже активен Premium.")
    return

# Проверяем cooldown
conn = get_conn()
prem_row = conn.execute("SELECT last_coin_buy FROM premium WHERE user_id=?", (user_id,)).fetchone()
conn.close()

if prem_row and prem_row["last_coin_buy"]:
    last = datetime.fromisoformat(prem_row["last_coin_buy"])
    cooldown = int(db_get_setting("premium_coin_cooldown_days",
                                   str(PREMIUM_COIN_COOLDOWN_DAYS)))
    if datetime.now() - last < timedelta(days=cooldown):
        next_avail = last + timedelta(days=cooldown)
        await safe_send(
            bot, user_id,
            f"❌ Premium за монеты можно купить раз в {cooldown} дней.\n"
            f"Следующая покупка доступна: {next_avail.strftime('%d.%m.%Y')}",
        )
        return

price_key = f"premium_{plan}_coins"
price = int(db_get_setting(price_key, PREMIUM_COIN_PRICES.get(plan, "999")))
days  = int(plan.replace("d", ""))

spent = db_spend_coins(user_id, price)
if not spent:
    conn = get_conn()
    row = conn.execute("SELECT coins FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    balance = row["coins"] if row else 0
    await safe_send(
        bot, user_id,
        f"❌ Недостаточно монет.\n"
        f"Нужно: {price} 🪙\n"
        f"У вас: {balance} 🪙",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Купить за деньги", callback_data="premium:buy_money")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="premium:menu")],
        ]),
    )
    return

expires = db_grant_premium(user_id, days, source="coins")

# Записываем дату покупки
conn = get_conn()
conn.execute(
    "UPDATE premium SET last_coin_buy=datetime('now') WHERE user_id=?", (user_id,)
)
conn.commit()
conn.close()

await safe_send(
    bot, user_id,
    f"🎉 Premium на {days} дней активирован!\n"
    f"Действует до: {expires.strftime('%d.%m.%Y %H:%M')}\n\n"
    f"Списано: {price} 🪙",
)

# Уведомление всем админам
user_conn = get_conn()
u = user_conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
user_conn.close()

uname = f"@{u['username']}" if u and u["username"] else str(user_id)
for admin_id in ADMINS:
    await safe_send(
        bot, admin_id,
        f"🟣 Новый Premium за монеты\n\n"
        f"ID: {user_id}\n"
        f"Username: {uname}\n"
        f"Купил: Premium {days} дней\n"
        f"Списано: {price} монет",
    )
```

async def notify_premium_expired(bot, user_id: int,
context: ContextTypes.DEFAULT_TYPE) -> None:
“”“Уведомление об истечении Premium.”””
conn = get_conn()
total_q = conn.execute(
“SELECT COUNT(*) as cnt FROM answers_log WHERE user_id=?”, (user_id,)
).fetchone()
conn.close()
rank, _ = db_get_user_rank(user_id)
total_q_cnt = total_q[“cnt”] if total_q else 0

```
coin_enabled = db_get_setting("coin_buy_premium_enabled", "1") == "1"
buttons = [
    [InlineKeyboardButton("💳 Купить Premium за деньги", callback_data="premium:buy_money")],
]
if coin_enabled:
    buttons.append([InlineKeyboardButton("🪙 Пробный Premium за монеты", callback_data="premium:buy_coins")])
buttons.append([InlineKeyboardButton("👥 Пригласить друга", callback_data="referral:menu")])

await safe_send(
    bot, user_id,
    f"⚠️ Premium закончился\n\n"
    f"Вы активно пользовались Premium:\n"
    f"-- Решили {total_q_cnt} вопросов\n"
    f"-- Рейтинг: #{rank}\n\n"
    "Чтобы продолжить без ограничений, купите Premium на 30 дней.",
    reply_markup=InlineKeyboardMarkup(buttons),
)
```

# =========================================

# REFERRAL SYSTEM

# =========================================

def get_referral_link(user_id: int, bot_username: str) -> str:
return f”https://t.me/{bot_username}?start=ref{user_id}”

async def process_referral(bot, referrer_id: int, new_user_id: int,
context: ContextTypes.DEFAULT_TYPE) -> None:
“”“Начисляет бонус за реферала.”””
if referrer_id == new_user_id:
return  # нельзя самого себя

```
conn = get_conn()
existing = conn.execute(
    "SELECT * FROM referrals WHERE referred_id=?", (new_user_id,)
).fetchone()

if existing:
    conn.close()
    return  # уже зарегистрирован

conn.execute(
    "INSERT OR IGNORE INTO referrals(referrer_id,referred_id,activated,coins_given) VALUES(?,?,1,?)",
    (referrer_id, new_user_id, int(db_get_setting("referral_coins", "50"))),
)
conn.commit()
conn.close()

ref_coins = int(db_get_setting("referral_coins", "50"))
db_add_coins(referrer_id, ref_coins)

await safe_send(
    bot, referrer_id,
    f"🎉 По вашей ссылке пришёл новый пользователь!\n"
    f"Начислено: {ref_coins} 🪙",
)
```

# =========================================

# ADMIN PANEL – CARD HELPERS

# =========================================

def build_admin_card_text(request_id: int) -> str:
req = REQUESTS.get(request_id, {})
user = req.get(“user”, {})

```
first_name = escape(user.get("first_name") or "Без имени")
last_name  = escape(user.get("last_name")  or "")
full_name  = f"{first_name} {last_name}".strip()
uname      = f"@{escape(user['username'])}" if user.get("username") else user_mention_html(user)
reason     = escape(req.get("reason_title", "--"))
status_t   = escape(req.get("status_text",  "--"))
msg_type   = escape(req.get("message_type", "--"))

meta = (
    "<blockquote>"
    f"👤 Имя: {full_name}\n"
    f"🔹 Username: {uname}\n"
    f"🆔 ID: <code>{user.get('id','--')}</code>\n"
    f"📌 Причина: {reason}\n"
    f"📍 Статус: {status_t}\n"
    f"📨 Тип: {msg_type}"
    "</blockquote>"
)

parts = ["📩 <b>Новое обращение</b>", "", meta]

if req.get("message_text"):
    parts += ["", "<b>💬 Сообщение:</b>", escape(req["message_text"])]
if req.get("caption"):
    parts += ["", "<b>📝 Подпись:</b>", escape(req["caption"])]
if req.get("voice_duration"):
    parts += ["", f"<b>🎤 Длительность:</b> {req['voice_duration']} сек."]
if req.get("user_reaction"):
    parts += ["", f"<b>🙋 Реакция:</b> {escape(req['user_reaction'])}"]

reacts = req.get("admin_reactions", {})
rparts = []
if reacts.get("👍"):  rparts.append(f"👍 {len(reacts['👍'])}")
if reacts.get("🫶🏻"): rparts.append(f"🫶🏻 {len(reacts['🫶🏻'])}")
if rparts:
    parts += ["", "<b>🧷 Реакции:</b> " + " | ".join(rparts)]

return "\n".join(parts)
```

async def refresh_admin_cards(context: ContextTypes.DEFAULT_TYPE, request_id: int) -> None:
if request_id not in REQUESTS:
return
req  = REQUESTS[request_id]
text = build_admin_card_text(request_id)
kb   = admin_card_keyboard(request_id)
for item in req.get(“admin_message_refs”, []):
try:
await context.bot.edit_message_text(
chat_id=item[“chat_id”], message_id=item[“message_id”],
text=text, parse_mode=ParseMode.HTML,
reply_markup=kb, disable_web_page_preview=True,
)
except Exception as e:
logger.warning(f”Не удалось обновить карточку: {e}”)

def find_user_info(user_id: int) -> dict:
for req in reversed(list(REQUESTS.values())):
if req.get(“user”, {}).get(“id”) == user_id:
return req[“user”]
return {“id”: user_id, “username”: None, “first_name”: “Пользователь”, “last_name”: “”}

# =========================================

# IMPORT SYSTEM (текстовый парсер)

# =========================================

def parse_questions_text(text: str) -> list[dict]:
“””
Парсит вопросы из текста формата:
1. Вопрос?
А) Вариант1*
В) Вариант2
С) Вариант3
D) Вариант4
Звёздочка * означает правильный ответ.
“””
results = []
# Разбиваем по нумерации
blocks = re.split(r”\n(?=\d+[.)]\s)”, text.strip())

```
for block in blocks:
    lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
    if not lines:
        continue

    # Первая строка -- вопрос (без номера)
    q_line = re.sub(r"^\d+[\.\)]\s*", "", lines[0]).strip()
    if not q_line:
        continue

    options = []
    correct_idx = 0
    opt_pattern = re.compile(r"^[АABCDЕabcdеАВСД]\)?[\.\)]\s*(.+)$", re.IGNORECASE)

    for line in lines[1:]:
        m = opt_pattern.match(line)
        if m:
            raw = m.group(1).strip()
            is_correct = raw.endswith("*")
            clean = raw.rstrip("*").strip()
            if is_correct:
                correct_idx = len(options)
            options.append(clean)

    if len(options) >= 2:
        results.append({
            "text": q_line,
            "options": options,
            "correct_idx": correct_idx,
        })

return results
```

def parse_quiz_poll_forward(message) -> Optional[dict]:
“”“Парсит пересланный Quiz Poll.”””
if not message.poll:
return None
poll = message.poll
if poll.type != “quiz”:
return None
options = [o.text for o in poll.options]
return {
“text”: poll.question,
“options”: options,
“correct_idx”: poll.correct_option_id or 0,
}

# =========================================

# COMMAND HANDLERS

# =========================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
if not update.message:
return
user = update.effective_user
db_get_or_create_user(user.id, user.username, user.first_name, user.last_name)

```
# Реферальная ссылка
args = context.args
if args and args[0].startswith("ref"):
    try:
        referrer_id = int(args[0][3:])
        await process_referral(context.bot, referrer_id, user.id, context)
    except (ValueError, Exception):
        pass

if is_admin(user.id):
    await update.message.reply_text(
        "✅ <b>Вы вошли как администратор</b>\n\n"
        "Нажмите /admin для открытия панели управления.\n\n"
        "Команды:\n"
        "• /admin -- панель администратора\n"
        "• /id -- ваш Telegram ID\n"
        "• /cancel -- отменить режим ответа\n"
        "• /banlist -- банлист",
        parse_mode=ParseMode.HTML,
    )
    return

if user.id in FINISHED_USERS:
    FINISHED_USERS.discard(user.id)

context.user_data[STATE_STARTED]         = True
context.user_data[STATE_REASON_SELECTED] = False
context.user_data.pop(STATE_REASON_CODE,  None)
context.user_data.pop(STATE_REASON_TITLE, None)
context.user_data.pop(STATE_IN_QUIZ,      None)

await update.message.reply_text(
    f"Здравствуйте, {safe_username(user)}! 👋",
    parse_mode=ParseMode.HTML,
)
await update.message.reply_text(
    "Выберите действие:",
    reply_markup=reason_keyboard(),
)
```

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
if not update.message:
return
user = update.effective_user
if not is_admin(user.id):
await update.message.reply_text(“⛔ Доступ запрещён.”)
return
await update.message.reply_text(
“⚙️ <b>ПАНЕЛЬ АДМИНИСТРАТОРА</b>”,
parse_mode=ParseMode.HTML,
reply_markup=admin_main_keyboard(),
)

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
user = update.effective_user
await update.message.reply_text(
f”Ваш Telegram ID: <code>{user.id}</code>”,
parse_mode=ParseMode.HTML,
)

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
user = update.effective_user
if not is_admin(user.id):
await update.message.reply_text(“⛔ Только для администраторов.”)
return
context.user_data.pop(STATE_ADMIN_REPLY_ID,  None)
msg_id = context.user_data.pop(STATE_ADMIN_REPLY_MSG, None)
if msg_id:
try:
await context.bot.delete_message(update.effective_chat.id, msg_id)
except Exception:
pass
context.user_data.pop(STATE_ADMIN_BROADCAST, None)
context.user_data.pop(STATE_BROADCAST_MSG,    None)
await update.message.reply_text(“✅ Режим отменён.”)

async def cmd_banlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
user = update.effective_user
if not is_admin(user.id):
await update.message.reply_text(“⛔ Только для администраторов.”)
return
if not BLOCKED_USERS:
await update.message.reply_text(“✅ Банлист пуст.”)
return
blocks = [“🚫 <b>Банлист</b>”]
for bid in BLOCKED_USERS:
info = find_user_info(bid)
name = escape(info.get(“first_name”) or “Пользователь”)
uname = f”@{escape(info[‘username’])}” if info.get(“username”) else user_mention_html(info)
blocks.append(
f”<blockquote>👤 {name}\n🔹 {uname}\n🆔 <code>{bid}</code></blockquote>”
)
await update.message.reply_text(
“\n\n”.join(blocks),
parse_mode=ParseMode.HTML,
disable_web_page_preview=True,
)

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
user = update.effective_user
if is_admin(user.id):
return
db_get_or_create_user(user.id, user.username, user.first_name, user.last_name)
await show_profile(update.message.reply_text, user.id, context)

async def show_profile(reply_fn, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
conn = get_conn()
u    = conn.execute(“SELECT * FROM users WHERE user_id=?”, (user_id,)).fetchone()
prem = conn.execute(“SELECT * FROM premium WHERE user_id=?”, (user_id,)).fetchone()
total_q = conn.execute(
“SELECT COUNT(*) as cnt FROM answers_log WHERE user_id=?”, (user_id,)
).fetchone()
correct_q = conn.execute(
“SELECT COUNT(*) as cnt FROM answers_log WHERE user_id=? AND is_correct=1”, (user_id,)
).fetchone()
conn.close()

```
if not u:
    return

rank, total = db_get_user_rank(user_id)
total_cnt   = total_q["cnt"] if total_q else 0
correct_cnt = correct_q["cnt"] if correct_q else 0
acc = round(correct_cnt / max(total_cnt, 1) * 100, 1)
level_name  = LEVEL_NAMES.get(u["level"], "🌱 Новичок")
streak      = context.bot_data.get(f"streak_{user_id}", u["streak"])

prem_text = "❌ Нет"
if prem and prem["active"] and prem["expires_at"]:
    exp = datetime.fromisoformat(prem["expires_at"])
    if exp > datetime.now():
        prem_text = f"✅ до {exp.strftime('%d.%m.%Y')}"

text = (
    "👤 МОЙ ПРОФИЛЬ\n\n"
    f"🆔 ID: {user_id}\n"
    f"📛 Имя: {escape(u['first_name'] or 'Пользователь')}\n\n"
    f"🏅 Уровень: {level_name}\n"
    f"⭐ XP: {u['xp']}\n"
    f"🪙 Монеты: {u['coins']}\n\n"
    f"📊 Статистика:\n"
    f"-- Решено вопросов: {total_cnt}\n"
    f"-- Точность: {acc}%\n"
    f"-- 🔥 Серия: {streak}\n"
    f"-- 🏆 Рейтинг: #{rank} из {total}\n\n"
    f"💎 Premium: {prem_text}"
)
await reply_fn(text, reply_markup=profile_keyboard())
```

# =========================================

# CALLBACK ROUTER

# =========================================

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
query = update.callback_query
if not query or not query.data:
return
await query.answer()

```
data    = query.data
user    = update.effective_user
user_id = user.id

# ---- Главное меню ----
if data == "main:menu":
    db_get_or_create_user(user_id, user.username, user.first_name, user.last_name)
    await query.edit_message_text(
        "Выберите действие:", reply_markup=reason_keyboard()
    )
    return

# ---- Обращения (A/B/C/D) ----
if data.startswith("reason:"):
    await _cb_reason(query, context, user)
    return

# ---- Тесты ----
if data.startswith("quiz:"):
    await _cb_quiz(query, context, user)
    return

# ---- Premium ----
if data.startswith("premium:"):
    await _cb_premium(query, context, user)
    return

# ---- Профиль / статистика ----
if data.startswith("profile:"):
    await _cb_profile(query, context, user)
    return

# ---- Рейтинг ----
if data.startswith("leaderboard:"):
    await _cb_leaderboard(query, context, user)
    return

# ---- Рефералы ----
if data.startswith("referral:"):
    await _cb_referral(query, context, user)
    return

# ---- Реакции пользователя ----
if data.startswith("userreact:"):
    await _cb_user_react(query, context, user)
    return

# ---- Реакции админов ----
if data.startswith("adminreact:"):
    await _cb_admin_react(query, context, user)
    return

# ---- Ответить на обращение ----
if data.startswith("reply:"):
    await _cb_reply(query, context, user)
    return

# ---- Блок/разблок ----
if data.startswith("block:"):
    await _cb_block(query, context, user, block=True)
    return
if data.startswith("unblock:"):
    await _cb_block(query, context, user, block=False)
    return

# ---- Завершить диалог ----
if data.startswith("finish:"):
    await _cb_finish(query, context, user)
    return

# ---- Админ-панель расширенная ----
if data.startswith("adm:import_to_topic:"):
    await _cb_import_to_topic(query, context, user)
    return

if data.startswith("adm:give_prem:"):
    if is_admin(user.id):
        uid = int(data.split(":")[-1])
        context.user_data["adm_action"] = "give_premium_inline"
        context.user_data["adm_target_uid"] = uid
        await query.edit_message_text(
            f"Выдать Premium пользователю {uid}.\nНапишите количество дней (например: 30):",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад", callback_data="adm:users")]
            ]),
        )
    return

if data.startswith("adm:ban:"):
    if is_admin(user.id):
        uid = int(data.split(":")[-1])
        BLOCKED_USERS.add(uid)
        conn = get_conn()
        conn.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (uid,))
        conn.commit()
        conn.close()
        await safe_send(context.bot, uid, "🚫 Вы заблокированы администрацией.")
        await query.edit_message_text(
            f"✅ Пользователь {uid} заблокирован.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад", callback_data="adm:users")]
            ]),
        )
    return

if data.startswith("adm:give_coins:"):
    if is_admin(user.id):
        uid = int(data.split(":")[-1])
        context.user_data["adm_action"] = "give_coins_inline"
        context.user_data["adm_target_uid"] = uid
        await query.edit_message_text(
            f"Выдать монеты пользователю {uid}.\nНапишите количество монет:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад", callback_data="adm:users")]
            ]),
        )
    return

# ---- Админ-панель ----
if data.startswith("adm:"):
    await _cb_admin(query, context, user)
    return

# ---- Импорт (сохранить/отмена) ----
if data.startswith("import:"):
    await _cb_import(query, context, user)
    return
```

# –– Callback: обращения (A/B/C/D) ––

async def _cb_reason(query, context, user) -> None:
if is_admin(user.id):
return
if user.id in BLOCKED_USERS:
return

```
data = query.data
if data == "reason:back":
    context.user_data[STATE_REASON_SELECTED] = False
    context.user_data.pop(STATE_REASON_CODE,  None)
    context.user_data.pop(STATE_REASON_TITLE, None)
    await query.edit_message_text("Выберите действие:", reply_markup=reason_keyboard())
    return

reason_code  = data.split(":", 1)[1]
reason_title = get_reason_title(reason_code)

context.user_data[STATE_STARTED]         = True
context.user_data[STATE_REASON_SELECTED] = True
context.user_data[STATE_REASON_CODE]     = reason_code
context.user_data[STATE_REASON_TITLE]    = reason_title

await query.edit_message_text(f"✅ Причина: {reason_title}")
await query.message.reply_text(get_reason_text(reason_code), reply_markup=back_keyboard())
```

# –– Callback: тесты ––

async def _cb_quiz(query, context, user) -> None:
data    = query.data
user_id = user.id

```
if is_admin(user_id):
    await query.answer("Режим теста недоступен для администраторов.", show_alert=True)
    return

db_get_or_create_user(user_id, user.username, user.first_name, user.last_name)

# Главное меню тестов
if data == "quiz:menu":
    subscribed = await check_subscription(context.bot, user_id)
    if not subscribed:
        await query.edit_message_text(
            "⚠️ Для доступа к тестам необходимо подписаться на канал:",
            reply_markup=subscribe_keyboard(),
        )
        return

    is_prem = db_is_premium(user_id)
    daily   = db_get_daily(user_id)
    limit   = int(db_get_setting("daily_limit", str(DEFAULT_DAILY_LIMIT)))

    if not is_prem and daily["q_count"] >= limit:
        now = datetime.now()
        tom = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0)
        diff = tom - now
        h = diff.seconds // 3600
        m = (diff.seconds % 3600) // 60
        s = diff.seconds % 60
        await query.edit_message_text(
            f"⏳ Дневной лимит исчерпан.\n"
            f"Обновится через: {h:02d}:{m:02d}:{s:02d}",
            reply_markup=limit_reached_keyboard(),
        )
        return

    await query.edit_message_text(
        "📚 Выберите тему для тестирования:",
        reply_markup=topics_keyboard(is_prem),
    )
    return

# Проверка подписки
if data == "quiz:check_sub":
    subscribed = await check_subscription(context.bot, user_id)
    if subscribed:
        is_prem = db_is_premium(user_id)
        await query.edit_message_text(
            "✅ Подписка подтверждена!\n\n📚 Выберите тему:",
            reply_markup=topics_keyboard(is_prem),
        )
    else:
        await query.answer("❌ Вы ещё не подписаны!", show_alert=True)
    return

# Заблокированная тема (Premium)
if data.startswith("quiz:locked:"):
    await query.answer("🔒 Эта тема доступна только для Premium.", show_alert=True)
    return

# Выбор темы
if data.startswith("quiz:topic:"):
    topic_id = int(data.split(":")[-1])
    await _start_quiz(query, context, user_id, topic_id)
    return

# Случайные вопросы
if data == "quiz:random":
    topics = db_get_all_topics(include_premium=db_is_premium(user_id))
    if not topics:
        await query.answer("Нет доступных тем.", show_alert=True)
        return
    topic = random.choice(topics)
    await _start_quiz(query, context, user_id, topic["topic_id"])
    return

# Мои ошибки
if data == "quiz:errors":
    conn = get_conn()
    errors = conn.execute("""
        SELECT DISTINCT q.topic_id FROM user_question_stats uqs
        JOIN questions q ON q.question_id=uqs.question_id
        WHERE uqs.user_id=? AND uqs.wrong_count>0
    """, (user_id,)).fetchall()
    conn.close()
    if not errors:
        await query.answer("У вас пока нет ошибок. Отличная работа! ✅", show_alert=True)
        return
    # Берём первую тему с ошибками
    topic_id = errors[0]["topic_id"]
    await _start_quiz(query, context, user_id, topic_id, errors_only=True)
    return

# СТОП
if data.startswith("quiz:stop:"):
    session_id = data.split(":", 2)[2]
    session = db_get_session(user_id)
    if session and session["session_id"] == session_id:
        await query.edit_message_text("⏹ Тест остановлен. Считаем результаты...")
        await show_quiz_results(context.bot, user_id, session_id, context)
    return

# Апелляция
if data.startswith("quiz:appeal:"):
    session_id = data.split(":", 2)[2]
    session = db_get_session(user_id)
    if session and session["session_id"] == session_id:
        db_update_session(session_id, status="paused")
        context.user_data[STATE_APPEAL_ACTIVE]    = True
        context.user_data[STATE_APPEAL_Q_ID]      = session["current_q_id"]
        context.user_data[STATE_WAIT_APPEAL_TEXT] = True
        context.user_data[STATE_SESSION_ID]       = session_id
        await query.edit_message_text(
            "🚨 АПЕЛЛЯЦИЯ\n\n"
            "Тест поставлен на паузу.\n"
            f"Вопрос ID: {session['current_q_id']}\n\n"
            "Опишите ошибку и укажите источник:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Отмена", callback_data=f"quiz:resume:{session_id}")]
            ]),
        )
    return

# Продолжить тест
if data.startswith("quiz:resume:"):
    session_id = data.split(":", 2)[2]
    context.user_data.pop(STATE_APPEAL_ACTIVE,    None)
    context.user_data.pop(STATE_WAIT_APPEAL_TEXT, None)
    session = db_get_session(user_id)
    if session and session["session_id"] == session_id:
        db_update_session(session_id, status="active")
        await query.edit_message_text("▶️ Продолжаем тест...")
        # Убираем лишние job reminder
        for job in context.application.job_queue.get_jobs_by_name(f"remind_{user_id}"):
            job.schedule_removal()
        fresh = db_get_session(user_id)
        if fresh:
            sent = await send_next_question(context.bot, user_id, fresh, context)
            if not sent:
                await show_quiz_results(context.bot, user_id, session_id, context)
    else:
        await query.edit_message_text(
            "Сессия не найдена. Начните новый тест:",
            reply_markup=topics_keyboard(db_is_premium(user_id)),
        )
    return
```

async def *start_quiz(query, context, user_id: int, topic_id: int,
errors_only: bool = False) -> None:
“”“Запускает новый тест по выбранной теме.”””
# Отменяем предыдущие reminder jobs
for job in context.application.job_queue.get_jobs_by_name(f”remind*{user_id}”):
job.schedule_removal()

```
q_ids = build_question_order(user_id, topic_id)

if errors_only:
    conn = get_conn()
    err_ids = [r["question_id"] for r in conn.execute(
        "SELECT question_id FROM user_question_stats WHERE user_id=? AND wrong_count>0 AND priority>1.5",
        (user_id,)
    ).fetchall()]
    conn.close()
    q_ids = [q for q in q_ids if q in err_ids] or q_ids

if not q_ids:
    await query.edit_message_text(
        "⚠️ В этой теме пока нет вопросов.\n"
        "Администратор скоро добавит их!",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="quiz:menu")]
        ]),
    )
    return

session_id = db_create_session(user_id, topic_id, q_ids)
context.user_data[STATE_IN_QUIZ]    = True
context.user_data[STATE_SESSION_ID] = session_id
context.user_data[STATE_TOPIC_ID]   = topic_id
context.bot_data[f"streak_{user_id}"] = 0

conn = get_conn()
topic = conn.execute("SELECT name FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
conn.close()

await query.edit_message_text(
    f"🚀 Начинаем тест!\n"
    f"📚 Тема: {topic['name'] if topic else '--'}\n"
    f"❓ Вопросов: {len(q_ids)}\n\n"
    "Удачи! 🍀"
)

session_row = db_get_session(user_id)
if session_row:
    await send_next_question(context.bot, user_id, session_row, context)
```

# –– Callback: Premium ––

async def _cb_premium(query, context, user) -> None:
data    = query.data
user_id = user.id
db_get_or_create_user(user_id, user.username, user.first_name, user.last_name)

```
if data == "premium:menu":
    is_prem = db_is_premium(user_id)
    if is_prem:
        conn = get_conn()
        prem = conn.execute("SELECT expires_at FROM premium WHERE user_id=?", (user_id,)).fetchone()
        conn.close()
        exp_str = ""
        if prem and prem["expires_at"]:
            exp = datetime.fromisoformat(prem["expires_at"])
            exp_str = f"\nДействует до: {exp.strftime('%d.%m.%Y %H:%M')}"
        await query.edit_message_text(
            f"💎 У вас активен Premium!{exp_str}\n\n"
            "Вы уже пользуетесь всеми преимуществами.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 В меню", callback_data="main:menu")]
            ]),
        )
        return

    text = (
        "💎 PREMIUM\n\n"
        "🚀 Что даёт Premium:\n"
        "-- Безлимитные вопросы\n"
        "-- Расширенная статистика\n"
        "-- Закрытые темы\n"
        "-- Отдельный рейтинг\n\n"
        "━━━━━━━━━━━━━━━━━\n"
        "💰 ТАРИФЫ:\n"
        "🔷 3 дня -- попробовать (за монеты)\n"
        "🔷 7 дней -- за монеты\n"
        "⭐ 30 дней -- ВЫГОДНЕЕ ВСЕГО (за деньги)\n"
        "👑 90 дней -- только за деньги\n"
    )
    await query.edit_message_text(text, reply_markup=premium_menu_keyboard(user_id))
    return

if data == "premium:buy_money":
    conn = get_conn()
    u = conn.execute("SELECT first_name, username FROM users WHERE user_id=?", (user_id,)).fetchone()
    total_q = conn.execute(
        "SELECT COUNT(*) as cnt FROM answers_log WHERE user_id=?", (user_id,)
    ).fetchone()
    conn.close()
    uname = f"@{u['username']}" if u and u["username"] else str(user_id)
    total_cnt = total_q["cnt"] if total_q else 0

    for admin_id in ADMINS:
        await safe_send(
            context.bot, admin_id,
            f"💳 Запрос на Premium за деньги\n\n"
            f"ID: {user_id}\n"
            f"Username: {uname}\n"
            f"Решено вопросов: {total_cnt}\n\n"
            f"Выдайте Premium через /givepremium {user_id} <дни>",
        )

    await query.edit_message_text(
        "✅ Заявка отправлена администратору!\n\n"
        "Ожидайте подтверждения оплаты.\n"
        "После оплаты Premium будет активирован вручную.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 В меню", callback_data="main:menu")]
        ]),
    )
    return

if data == "premium:buy_coins":
    await query.edit_message_text(
        "🪙 Выберите тариф Premium за монеты:",
        reply_markup=premium_coins_keyboard(),
    )
    return

if data.startswith("premium:coin_buy:"):
    plan = data.split(":")[-1]
    await query.edit_message_text("⏳ Обрабатываем...")
    await process_premium_buy_coins(context.bot, user_id, plan, context)
    return
```

# –– Callback: профиль ––

async def _cb_profile(query, context, user) -> None:
data    = query.data
user_id = user.id
db_get_or_create_user(user_id, user.username, user.first_name, user.last_name)

```
if data == "profile:menu":
    await show_profile(query.edit_message_text, user_id, context)
    return
if data == "profile:stats":
    await show_profile(query.edit_message_text, user_id, context)
    return
```

# –– Callback: рейтинг ––

async def _cb_leaderboard(query, context, user) -> None:
data    = query.data
user_id = user.id
period  = data.split(”:”)[-1]

```
rows = db_get_leaderboard(limit=10, period=period)
rank, total = db_get_user_rank(user_id)

title = "🏆 РЕЙТИНГ -- Сегодня" if period == "day" else "🏆 РЕЙТИНГ -- Всё время"
medals = ["🥇", "🥈", "🥉"]
lines = [title, ""]

for i, row in enumerate(rows):
    medal = medals[i] if i < 3 else f"{i+1}."
    name  = row["username"] or row["first_name"] or "--"
    score = row["score"]
    marker = " ← ВЫ" if row["user_id"] == user_id else ""
    lines.append(f"{medal} @{name} -- {score}{marker}")

lines += ["", f"Ваше место: #{rank} из {total}"]

await query.edit_message_text(
    "\n".join(lines),
    reply_markup=leaderboard_keyboard(period),
)
```

# –– Callback: рефералы ––

async def _cb_referral(query, context, user) -> None:
user_id = user.id
bot_info = await context.bot.get_me()
link = get_referral_link(user_id, bot_info.username)

```
conn = get_conn()
refs = conn.execute(
    "SELECT COUNT(*) as cnt FROM referrals WHERE referrer_id=?", (user_id,)
).fetchone()
coins_from_refs = conn.execute(
    "SELECT SUM(coins_given) as s FROM referrals WHERE referrer_id=?", (user_id,)
).fetchone()
conn.close()

ref_cnt  = refs["cnt"] if refs else 0
ref_coins_total = coins_from_refs["s"] or 0
ref_coins_each  = int(db_get_setting("referral_coins", "50"))

await query.edit_message_text(
    f"👥 РЕФЕРАЛЬНАЯ ПРОГРАММА\n\n"
    f"За каждого друга: {ref_coins_each} 🪙\n\n"
    f"Ваших рефералов: {ref_cnt}\n"
    f"Заработано: {ref_coins_total} 🪙\n\n"
    f"Ваша ссылка:\n{link}",
    reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 В меню", callback_data="main:menu")]
    ]),
    disable_web_page_preview=True,
)
```

# –– Callback: реакции пользователя ––

async def _cb_user_react(query, context, user) -> None:
parts = query.data.split(”:”)
if len(parts) != 3:
return
response_id = int(parts[1])
reaction    = parts[2]
user_id     = user.id

```
if is_admin(user_id):
    return
if response_id not in RESPONSES:
    await query.answer("Сообщение не найдено", show_alert=True)
    return

resp = RESPONSES[response_id]
if resp["user_id"] != user_id:
    await query.answer("Это не ваше сообщение", show_alert=True)
    return
if resp.get("reaction") is not None:
    await query.answer("Реакцию можно поставить только один раз", show_alert=True)
    return

resp["reaction"] = reaction
request_id = resp["request_id"]
if request_id in REQUESTS:
    REQUESTS[request_id]["user_reaction"] = reaction
    await refresh_admin_cards(context, request_id)

try:
    await context.bot.edit_message_reply_markup(
        chat_id=query.message.chat_id,
        message_id=query.message.message_id,
        reply_markup=user_response_keyboard(response_id, selected=reaction),
    )
except Exception:
    pass

# Уведомляем админов
req = REQUESTS.get(request_id, {})
target = user_mention_html(req.get("user", {"id": user_id}))
for admin_id in ADMINS:
    await safe_send(
        context.bot, admin_id,
        f"🔔 Пользователь отреагировал: {reaction}\n"
        f"Пользователь: {target}",
    )
```

# –– Callback: реакции админов ––

async def _cb_admin_react(query, context, user) -> None:
if not is_admin(user.id):
return
parts = query.data.split(”:”)
if len(parts) != 3:
return
request_id = int(parts[1])
reaction   = parts[2]
if request_id not in REQUESTS:
return
req = REQUESTS[request_id]
req[“admin_reactions”].setdefault(“👍”,  set())
req[“admin_reactions”].setdefault(“🫶🏻”, set())
for r in [“👍”, “🫶🏻”]:
req[“admin_reactions”][r].discard(user.id)
req[“admin_reactions”][reaction].add(user.id)
await refresh_admin_cards(context, request_id)

# –– Callback: ответить ––

async def _cb_reply(query, context, user) -> None:
if not is_admin(user.id):
await query.answer(“Только для администраторов”, show_alert=True)
return
parts = query.data.split(”:”)
if len(parts) != 2:
return
request_id = int(parts[1])
if request_id not in REQUESTS:
await query.answer(“Обращение не найдено”, show_alert=True)
return

```
context.user_data[STATE_ADMIN_REPLY_ID] = request_id

old = context.user_data.pop(STATE_ADMIN_REPLY_MSG, None)
if old:
    try:
        await context.bot.delete_message(query.message.chat_id, old)
    except Exception:
        pass

msg = await query.message.reply_text(
    "✍️ Режим ответа включён.\n"
    "Следующее сообщение уйдёт пользователю анонимно."
)
context.user_data[STATE_ADMIN_REPLY_MSG] = msg.message_id
```

# –– Callback: блок/разблок ––

async def _cb_block(query, context, user, block: bool) -> None:
if not is_admin(user.id):
return
parts = query.data.split(”:”)
if len(parts) != 2:
return
request_id = int(parts[1])
if request_id not in REQUESTS:
return
req     = REQUESTS[request_id]
uid     = req[“user”][“id”]
uinfo   = req[“user”]
uname   = f”@{uinfo[‘username’]}” if uinfo.get(“username”) else (uinfo.get(“first_name”) or “Пользователь”)

```
if block:
    BLOCKED_USERS.add(uid)
    req["status_text"] = f"ЗАБЛОКИРОВАН 🚫, админом: {admin_name(user)}"
    await safe_send(context.bot, uid, f"{escape(uname)}, вы заблокированы администрацией.")
else:
    BLOCKED_USERS.discard(uid)
    req["status_text"] = f"РАЗБЛОКИРОВАН ✅, админом: {admin_name(user)}"
    await safe_send(context.bot, uid, "✅ Вы разблокированы. Нажмите /start для продолжения.")

await refresh_admin_cards(context, request_id)
```

# –– Callback: завершить диалог ––

async def _cb_finish(query, context, user) -> None:
if not is_admin(user.id):
return
parts = query.data.split(”:”)
if len(parts) != 2:
return
request_id = int(parts[1])
if request_id not in REQUESTS:
return
req = REQUESTS[request_id]
uid = req[“user”][“id”]
FINISHED_USERS.add(uid)
req[“status_text”] = f”ДИАЛОГ ЗАВЕРШЁН ✅, админом: {admin_name(user)}”
await refresh_admin_cards(context, request_id)
await safe_send(
context.bot, uid,
“Спасибо за обращение! 😊\n”
“Если появятся вопросы – нажмите /start.”
)

# –– Callback: Импорт ––

async def _cb_import(query, context, user) -> None:
if not is_admin(user.id):
return
data = query.data

```
if data == "import:save":
    draft_id = context.user_data.get(STATE_ADMIN_DRAFT_ID)
    if not draft_id:
        await query.edit_message_text("❌ Черновик не найден.")
        return
    conn = get_conn()
    draft = conn.execute("SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
    conn.close()
    if not draft:
        await query.edit_message_text("❌ Черновик не найден.")
        return

    import json as _json
    questions = _json.loads(draft["data"])
    topic_id  = context.user_data.get("import_topic_id", 1)
    saved = 0
    for q in questions:
        db_add_question(
            topic_id=topic_id,
            text=q["text"],
            options=q["options"],
            correct_idx=q["correct_idx"],
        )
        saved += 1

    conn = get_conn()
    conn.execute("UPDATE drafts SET status='saved' WHERE draft_id=?", (draft_id,))
    conn.commit()
    conn.close()

    context.user_data.pop(STATE_ADMIN_DRAFT_ID, None)
    context.user_data.pop(STATE_ADMIN_IMPORT,   None)

    await query.edit_message_text(
        f"✅ Сохранено {saved} вопросов!\n",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад в импорт", callback_data="adm:import")]
        ]),
    )
    return

if data == "import:cancel":
    context.user_data.pop(STATE_ADMIN_DRAFT_ID, None)
    context.user_data.pop(STATE_ADMIN_IMPORT,   None)
    await query.edit_message_text(
        "❌ Импорт отменён.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="adm:import")]
        ]),
    )
    return
```

# –– Callback: Админ-панель ––

async def _cb_admin(query, context, user) -> None:
if not is_admin(user.id):
await query.answer(“⛔ Доступ запрещён.”, show_alert=True)
return
data = query.data

```
# Главная панель
if data == "adm:back":
    await query.edit_message_text(
        "⚙️ <b>ПАНЕЛЬ АДМИНИСТРАТОРА</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_main_keyboard(),
    )
    return

# Статистика
if data == "adm:stats":
    conn = get_conn()
    total_users   = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
    active_today  = conn.execute(
        "SELECT COUNT(*) as c FROM users WHERE last_active LIKE ?",
        (datetime.now().strftime("%Y-%m-%d") + "%",)
    ).fetchone()["c"]
    premium_count = conn.execute(
        "SELECT COUNT(*) as c FROM premium WHERE active=1"
    ).fetchone()["c"]
    total_answers = conn.execute(
        "SELECT COUNT(*) as c FROM answers_log"
    ).fetchone()["c"]
    total_q       = conn.execute(
        "SELECT COUNT(*) as c FROM questions"
    ).fetchone()["c"]
    conn.close()

    text = (
        "📊 СТАТИСТИКА\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"🟢 Активны сегодня: {active_today}\n"
        f"💎 Premium пользователей: {premium_count}\n"
        f"❓ Вопросов в базе: {total_q}\n"
        f"✅ Всего ответов: {total_answers}"
    )
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="adm:back")]
        ]),
    )
    return

# Тесты
if data == "adm:tests":
    conn = get_conn()
    topics = conn.execute("SELECT * FROM topics WHERE is_active=1").fetchall()
    conn.close()
    lines = ["🧠 ТЕМЫ И ВОПРОСЫ\n"]
    for t in topics:
        cnt   = db_topic_question_count(t["topic_id"])
        label = "🔒" if t["is_premium"] else "✅"
        lines.append(f"{label} {t['name']} -- {cnt} вопросов")

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Добавить тему",   callback_data="adm:topic_add")],
            [InlineKeyboardButton("⬅️ Назад",           callback_data="adm:back")],
        ]),
    )
    return

# Импорт
if data == "adm:import":
    context.user_data[STATE_ADMIN_IMPORT] = "waiting"
    await query.edit_message_text(
        "📥 ИМПОРТ ВОПРОСОВ\n\n"
        "Выберите способ импорта:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Текстом",        callback_data="adm:import_text")],
            [InlineKeyboardButton("📊 Quiz Poll",      callback_data="adm:import_poll")],
            [InlineKeyboardButton("⬅️ Назад",          callback_data="adm:back")],
        ]),
    )
    return

if data == "adm:import_text":
    context.user_data[STATE_ADMIN_IMPORT] = "text"
    await query.edit_message_text(
        "📝 Отправьте вопросы в формате:\n\n"
        "1. Вопрос?\n"
        "А) Вариант1*\n"
        "В) Вариант2\n"
        "С) Вариант3\n"
        "D) Вариант4\n\n"
        "* -- правильный ответ",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="adm:import")]
        ]),
    )
    return

if data == "adm:import_poll":
    context.user_data[STATE_ADMIN_IMPORT] = "poll"
    await query.edit_message_text(
        "📊 Перешлите Quiz Poll от @QuizBot или любого бота.\n\n"
        "Бот распознает вопрос, варианты и правильный ответ.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="adm:import")]
        ]),
    )
    return

# Пользователи
if data == "adm:users":
    context.user_data[STATE_ADMIN_FIND_USER] = True
    await query.edit_message_text(
        "👥 ПОЛЬЗОВАТЕЛИ\n\n"
        "Напишите Telegram ID или @username пользователя:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="adm:back")]
        ]),
    )
    return

# Premium (админ)
if data == "adm:premium":
    conn = get_conn()
    active_prems = conn.execute("""
        SELECT u.user_id, u.username, u.first_name, p.expires_at, p.source
        FROM premium p JOIN users u ON u.user_id=p.user_id
        WHERE p.active=1
        ORDER BY p.expires_at
        LIMIT 10
    """).fetchall()
    conn.close()

    lines = ["💎 АКТИВНЫЕ PREMIUM\n"]
    for p in active_prems:
        name  = p["username"] or p["first_name"] or str(p["user_id"])
        exp   = p["expires_at"][:10] if p["expires_at"] else "--"
        lines.append(f"• @{name} -- до {exp} ({p['source']})")

    if not active_prems:
        lines.append("Нет активных Premium")

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Выдать Premium", callback_data="adm:give_premium")],
            [InlineKeyboardButton("⬅️ Назад",          callback_data="adm:back")],
        ]),
    )
    return

if data == "adm:give_premium":
    context.user_data["adm_action"] = "give_premium"
    await query.edit_message_text(
        "Напишите: <ID пользователя> <дни>\n"
        "Пример: 123456789 30",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="adm:premium")]
        ]),
    )
    return

# Экономика
if data == "adm:economy":
    easy   = db_get_setting("coins_easy",   "1")
    medium = db_get_setting("coins_medium", "2")
    hard   = db_get_setting("coins_hard",   "3")
    ref    = db_get_setting("referral_coins", "50")
    p3     = db_get_setting("premium_3d_coins", "500")
    p7     = db_get_setting("premium_7d_coins", "900")
    limit  = db_get_setting("daily_limit", "55")
    coin_e = db_get_setting("coin_buy_premium_enabled", "1")

    text = (
        "🪙 НАСТРОЙКИ ЭКОНОМИКИ\n\n"
        f"Монет за лёгкий вопрос: {easy}\n"
        f"Монет за средний вопрос: {medium}\n"
        f"Монет за сложный вопрос: {hard}\n"
        f"Монет за реферала: {ref}\n"
        f"Premium 3 дня (монеты): {p3}\n"
        f"Premium 7 дней (монеты): {p7}\n"
        f"Дневной лимит вопросов: {limit}\n"
        f"Покупка Premium за монеты: {'✅' if coin_e=='1' else '❌'}"
    )
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="adm:back")]
        ]),
    )
    return

# Рассылка
if data == "adm:broadcast":
    context.user_data[STATE_ADMIN_BROADCAST] = "choose_target"
    await query.edit_message_text(
        "📢 РАССЫЛКА\n\nВыберите аудиторию:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 Всем пользователям",    callback_data="adm:bc_all")],
            [InlineKeyboardButton("💎 Только Premium",         callback_data="adm:bc_premium")],
            [InlineKeyboardButton("🆓 Только бесплатным",      callback_data="adm:bc_free")],
            [InlineKeyboardButton("⬅️ Назад",                  callback_data="adm:back")],
        ]),
    )
    return

if data in ("adm:bc_all", "adm:bc_premium", "adm:bc_free"):
    target_map = {"adm:bc_all": "all", "adm:bc_premium": "premium", "adm:bc_free": "free"}
    context.user_data[STATE_ADMIN_BROADCAST] = "waiting_msg"
    context.user_data[STATE_BROADCAST_TARGET] = target_map[data]
    await query.edit_message_text(
        "✏️ Отправьте текст рассылки (HTML поддерживается):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="adm:broadcast")]
        ]),
    )
    return

# Апелляции
if data == "adm:appeals":
    conn = get_conn()
    appeals = conn.execute("""
        SELECT a.*, u.username, u.first_name
        FROM appeals a JOIN users u ON u.user_id=a.user_id
        WHERE a.status='open'
        ORDER BY a.created_at DESC LIMIT 10
    """).fetchall()
    conn.close()

    if not appeals:
        await query.edit_message_text(
            "🚨 Нет открытых апелляций.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад", callback_data="adm:back")]
            ]),
        )
        return

    lines = ["🚨 ОТКРЫТЫЕ АПЕЛЛЯЦИИ\n"]
    for ap in appeals:
        name = ap["username"] or ap["first_name"] or str(ap["user_id"])
        lines.append(f"• #{ap['appeal_id']} @{name} -- {ap['question_id']}: {ap['comment'][:50]}")

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="adm:back")]
        ]),
    )
    return

# Настройки
if data == "adm:settings":
    channel = db_get_setting("channel_id", "не задан")
    limit   = db_get_setting("daily_limit", "55")
    text = (
        "⚙️ НАСТРОЙКИ\n\n"
        f"📢 Канал для подписки: {channel}\n"
        f"📊 Дневной лимит: {limit}\n\n"
        "Для изменения настроек используйте команды:\n"
        "/setcoin <ключ> <значение>\n"
        "/setlimit <число>\n"
        "/setchannel <@канал>"
    )
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="adm:back")]
        ]),
    )
    return

# Рейтинги (админ)
if data == "adm:leaderboard":
    rows = db_get_leaderboard(10, "all")
    lines = ["🏆 ТОП-10 (всё время)\n"]
    for i, r in enumerate(rows):
        name = r["username"] or r["first_name"] or str(r["user_id"])
        lines.append(f"{i+1}. @{name} -- {r['score']} XP")
    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="adm:back")]
        ]),
    )
    return

# Доступы
if data == "adm:access":
    await query.edit_message_text(
        "🔒 УПРАВЛЕНИЕ ДОСТУПАМИ\n\n"
        "Используйте команды:\n"
        "/giveaccess <ID> -- выдать доступ\n"
        "/revokeaccess <ID> -- забрать доступ\n"
        "/givepremium <ID> <дни> -- выдать Premium\n"
        "/revokepremium <ID> -- снять Premium",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="adm:back")]
        ]),
    )
    return
```

# =========================================

# MESSAGE HANDLERS

# =========================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
“”“Единый обработчик всех входящих сообщений.”””
message = update.message
if not message:
return
user    = update.effective_user
user_id = user.id

```
# ---- АДМИНИСТРАТОР ----
if is_admin(user_id):
    await _handle_admin_message(message, user, context)
    return

# ---- ОБЫЧНЫЙ ПОЛЬЗОВАТЕЛЬ ----
await _handle_user_message(message, user, context)
```

async def _handle_admin_message(message, admin, context) -> None:
“”“Обрабатывает сообщения от администраторов.”””
user_id = admin.id

```
# Режим ответа на обращение
reply_id = context.user_data.get(STATE_ADMIN_REPLY_ID)
if reply_id:
    await _send_admin_reply(message, admin, context, reply_id)
    return

# Режим импорта текстом
if context.user_data.get(STATE_ADMIN_IMPORT) == "text" and message.text:
    questions = parse_questions_text(message.text)
    if not questions:
        await message.reply_text(
            "❌ Не удалось распознать вопросы.\n"
            "Проверьте формат: нумерация, варианты с А/В/С/D, * у правильного ответа."
        )
        return

    import json as _json
    conn = get_conn()
    conn.execute(
        "INSERT INTO drafts(admin_id, data, status) VALUES(?,?,'pending')",
        (user_id, _json.dumps(questions, ensure_ascii=False)),
    )
    conn.commit()
    draft_id = conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
    conn.close()

    context.user_data[STATE_ADMIN_DRAFT_ID] = draft_id

    # Выбор темы для сохранения
    topics = db_get_all_topics()
    kb_rows = []
    for t in topics:
        kb_rows.append([InlineKeyboardButton(
            t["name"], callback_data=f"adm:import_to_topic:{t['topic_id']}"
        )])

    preview = "\n".join(
        f"{i+1}. {q['text'][:50]}..." for i, q in enumerate(questions[:5])
    )
    if len(questions) > 5:
        preview += f"\n... и ещё {len(questions)-5} вопросов"

    await message.reply_text(
        f"✅ Найдено вопросов: {len(questions)}\n\n"
        f"Предпросмотр:\n{preview}\n\n"
        "Выберите тему для сохранения:",
        reply_markup=InlineKeyboardMarkup(kb_rows + [
            [InlineKeyboardButton("❌ Отмена", callback_data="import:cancel")]
        ]),
    )
    return

# Режим импорта Quiz Poll
if context.user_data.get(STATE_ADMIN_IMPORT) == "poll":
    parsed = parse_quiz_poll_forward(message)
    if not parsed:
        await message.reply_text("❌ Пожалуйста, перешлите Quiz Poll (тип: quiz).")
        return

    import json as _json
    conn = get_conn()
    conn.execute(
        "INSERT INTO drafts(admin_id, data, status) VALUES(?,?,'pending')",
        (user_id, _json.dumps([parsed], ensure_ascii=False)),
    )
    conn.commit()
    draft_id = conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
    conn.close()

    context.user_data[STATE_ADMIN_DRAFT_ID] = draft_id

    topics = db_get_all_topics()
    kb_rows = [[InlineKeyboardButton(t["name"],
                callback_data=f"adm:import_to_topic:{t['topic_id']}")] for t in topics]

    await message.reply_text(
        f"✅ Quiz Poll распознан:\n\n"
        f"❓ {parsed['text']}\n"
        f"✅ Правильный: {parsed['options'][parsed['correct_idx']]}\n\n"
        "Выберите тему:",
        reply_markup=InlineKeyboardMarkup(kb_rows + [
            [InlineKeyboardButton("❌ Отмена", callback_data="import:cancel")]
        ]),
    )
    return

# Рассылка -- ожидаем текст
if context.user_data.get(STATE_ADMIN_BROADCAST) == "waiting_msg" and message.text:
    target = context.user_data.get(STATE_BROADCAST_TARGET, "all")
    context.user_data[STATE_ADMIN_BROADCAST] = None

    await message.reply_text("📤 Рассылка запущена...")
    sent, failed = await _do_broadcast(context.bot, message.text, target)
    await message.reply_text(
        f"✅ Рассылка завершена:\n"
        f"Отправлено: {sent}\n"
        f"Ошибок: {failed}"
    )
    return

# Поиск пользователя
if context.user_data.get(STATE_ADMIN_FIND_USER) and message.text:
    text = message.text.strip().lstrip("@")
    context.user_data.pop(STATE_ADMIN_FIND_USER, None)

    conn = get_conn()
    if text.isdigit():
        u = conn.execute("SELECT * FROM users WHERE user_id=?", (int(text),)).fetchone()
    else:
        u = conn.execute("SELECT * FROM users WHERE username=?", (text,)).fetchone()
    conn.close()

    if not u:
        await message.reply_text("❌ Пользователь не найден.")
        return

    uid = u["user_id"]
    is_prem = db_is_premium(uid)
    rank, total = db_get_user_rank(uid)
    conn = get_conn()
    total_q = conn.execute(
        "SELECT COUNT(*) as c FROM answers_log WHERE user_id=?", (uid,)
    ).fetchone()["c"]
    conn.close()

    uname = f"@{u['username']}" if u["username"] else str(uid)
    text_out = (
        f"👤 Пользователь: {uname}\n"
        f"🆔 ID: {uid}\n"
        f"📛 Имя: {u['first_name']} {u['last_name'] or ''}\n"
        f"🪙 Монеты: {u['coins']}\n"
        f"⭐ XP: {u['xp']} | Уровень: {u['level']}\n"
        f"💎 Premium: {'✅' if is_prem else '❌'}\n"
        f"📊 Решено вопросов: {total_q}\n"
        f"🏆 Рейтинг: #{rank} из {total}\n"
        f"🚫 Забанен: {'✅' if u['is_banned'] else '❌'}"
    )
    await message.reply_text(
        text_out,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 Выдать Premium", callback_data=f"adm:give_prem:{uid}")],
            [InlineKeyboardButton("🚫 Забанить",       callback_data=f"adm:ban:{uid}")],
            [InlineKeyboardButton("🪙 Выдать монеты",  callback_data=f"adm:give_coins:{uid}")],
            [InlineKeyboardButton("⬅️ Назад",           callback_data="adm:users")],
        ]),
    )
    return

# Режим выдачи Premium (через /admin → пользователи → кнопка)
if context.user_data.get("adm_action") in ("give_premium", "give_premium_inline") and message.text:
    action = context.user_data.pop("adm_action", None)
    target_uid = context.user_data.pop("adm_target_uid", None)

    if action == "give_premium_inline" and target_uid:
        # Только дни
        if not message.text.strip().isdigit():
            await message.reply_text("❌ Напишите только количество дней. Пример: 30")
            return
        days = int(message.text.strip())
    else:
        # ID + дни
        parts = message.text.strip().split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            await message.reply_text("❌ Формат: <ID> <дни>\nПример: 123456789 30")
            return
        target_uid = int(parts[0])
        days = int(parts[1])

    db_get_or_create_user(target_uid)
    expires = db_grant_premium(target_uid, days, source="admin")
    await message.reply_text(
        f"✅ Premium выдан!\nID: {target_uid}\nДо: {expires.strftime('%d.%m.%Y %H:%M')}"
    )
    await safe_send(
        context.bot, target_uid,
        f"🎉 Вам выдан Premium на {days} дней!\n"
        f"Действует до: {expires.strftime('%d.%m.%Y %H:%M')}",
    )
    return

# Режим выдачи монет (через /admin → пользователи → кнопка)
if context.user_data.get("adm_action") == "give_coins_inline" and message.text:
    context.user_data.pop("adm_action", None)
    target_uid = context.user_data.pop("adm_target_uid", None)
    if not target_uid or not message.text.strip().lstrip("-").isdigit():
        await message.reply_text("❌ Напишите только количество монет. Пример: 100")
        return
    amount = int(message.text.strip())
    db_get_or_create_user(target_uid)
    new_balance = db_add_coins(target_uid, amount)
    await message.reply_text(f"✅ Выдано {amount} монет пользователю {target_uid}. Баланс: {new_balance}")
    await safe_send(context.bot, target_uid, f"🪙 Вам начислено {amount} монет! Баланс: {new_balance}")
    return
```

async def _send_admin_reply(message, admin, context, request_id: int) -> None:
“”“Отправляет ответ администратора пользователю.”””
if request_id not in REQUESTS:
context.user_data.pop(STATE_ADMIN_REPLY_ID, None)
await message.reply_text(“❌ Обращение не найдено.”)
return

```
req    = REQUESTS[request_id]
uid    = req["user"]["id"]

if uid in BLOCKED_USERS:
    warn = await message.reply_text("⛔ Пользователь заблокирован.")
    context.application.create_task(delete_message_later(context, warn.chat_id, warn.message_id))
    return

text_to_send = message.text or message.caption
has_media    = any([message.photo, message.document, message.video,
                    message.voice, message.audio, message.video_note,
                    message.animation, message.sticker])

if not text_to_send and not has_media:
    warn = await message.reply_text("❌ Отправьте текст или медиа.")
    context.application.create_task(delete_message_later(context, warn.chat_id, warn.message_id))
    return

try:
    caption = f"📨 <b>Ответ от администрации</b>\n\n{escape(text_to_send or '')}"
    sent    = None

    if message.text:
        sent = await context.bot.send_message(uid, caption, parse_mode=ParseMode.HTML)
    elif message.photo:
        sent = await context.bot.send_photo(uid, message.photo[-1].file_id, caption=caption, parse_mode=ParseMode.HTML)
    elif message.document:
        sent = await context.bot.send_document(uid, message.document.file_id, caption=caption, parse_mode=ParseMode.HTML)
    elif message.video:
        sent = await context.bot.send_video(uid, message.video.file_id, caption=caption, parse_mode=ParseMode.HTML)
    elif message.voice:
        sent = await context.bot.send_voice(uid, message.voice.file_id, caption=caption, parse_mode=ParseMode.HTML)
    else:
        sent = await context.bot.copy_message(uid, message.chat_id, message.message_id)

    if sent:
        resp_id = next(RESPONSE_SEQ)
        RESPONSES[resp_id] = {
            "request_id": request_id,
            "user_id":    uid,
            "message_id": sent.message_id,
            "chat_id":    uid,
            "reaction":   None,
        }
        try:
            await context.bot.edit_message_reply_markup(
                uid, sent.message_id,
                reply_markup=user_response_keyboard(resp_id),
            )
        except Exception:
            pass

    req["status"]      = "answered"
    req["answered_by"] = admin_name(admin)
    req["status_text"] = f"ОТВЕЧЕНО ✅, admin: {req['answered_by']}"
    await refresh_admin_cards(context, request_id)

    context.user_data.pop(STATE_ADMIN_REPLY_ID,  None)
    old_msg = context.user_data.pop(STATE_ADMIN_REPLY_MSG, None)
    if old_msg:
        try:
            await context.bot.delete_message(message.chat_id, old_msg)
        except Exception:
            pass

    ok = await message.reply_text("✅ Ответ отправлен анонимно.")
    context.application.create_task(delete_message_later(context, ok.chat_id, ok.message_id))

except Exception as e:
    logger.exception("Ошибка при отправке ответа")
    err = await message.reply_text(f"❌ Не удалось отправить: {e}")
    context.application.create_task(delete_message_later(context, err.chat_id, err.message_id))
```

async def _handle_user_message(message, user, context) -> None:
“”“Обрабатывает сообщения обычных пользователей.”””
user_id = user.id

```
# Блокировка
if user_id in BLOCKED_USERS:
    warn = await message.reply_text(
        f"{safe_username(user)}, вы заблокированы администрацией."
    )
    context.application.create_task(delete_message_later(context, warn.chat_id, warn.message_id))
    context.application.create_task(try_delete(message))
    return

# Завершённый диалог
if user_id in FINISHED_USERS:
    warn = await message.reply_text("Диалог завершён. Нажмите /start для нового обращения.")
    context.application.create_task(delete_message_later(context, warn.chat_id, warn.message_id))
    context.application.create_task(try_delete(message))
    return

# Апелляция -- ждём текст
if context.user_data.get(STATE_WAIT_APPEAL_TEXT) and message.text:
    context.user_data.pop(STATE_WAIT_APPEAL_TEXT, None)
    q_id       = context.user_data.pop(STATE_APPEAL_Q_ID, None)
    session_id = context.user_data.get(STATE_SESSION_ID)

    # Сохраняем апелляцию
    conn = get_conn()
    conn.execute(
        "INSERT INTO appeals(user_id,question_id,comment,status) VALUES(?,?,?,'open')",
        (user_id, q_id, message.text[:500]),
    )
    conn.commit()
    appeal_id = conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
    conn.close()

    q = db_get_question(q_id) if q_id else None
    q_text = q["text"] if q else "--"

    for admin_id in ADMINS:
        await safe_send(
            context.bot, admin_id,
            f"🚨 #АПЕЛЛЯЦИЯ #{appeal_id}\n\n"
            f"ID вопроса: {q_id}\n"
            f"Вопрос: {q_text[:100]}\n"
            f"Username: @{user.username or user_id}\n"
            f"Комментарий: {message.text[:300]}",
        )

    await message.reply_text(
        "✅ Апелляция принята! Администратор рассмотрит её.\n\n"
        "Тест продолжается...",
    )

    # Возобновляем сессию
    if session_id:
        db_update_session(session_id, status="active")
        session = db_get_session(user_id)
        if session:
            await send_next_question(context.bot, user_id, session, context)
    return

# Не начат /start
if not context.user_data.get(STATE_STARTED):
    warn = await message.reply_text("⚠️ Нажмите /start для начала работы.")
    context.application.create_task(delete_message_later(context, warn.chat_id, warn.message_id))
    context.application.create_task(try_delete(message))
    return

# Не выбрана причина
reason_code  = context.user_data.get(STATE_REASON_CODE)
reason_title = context.user_data.get(STATE_REASON_TITLE)

if not context.user_data.get(STATE_REASON_SELECTED) or not reason_code:
    warn = await message.reply_text(
        "⚠️ Сначала выберите причину обращения:",
        reply_markup=reason_keyboard(),
    )
    context.application.create_task(delete_message_later(context, warn.chat_id, warn.message_id))
    context.application.create_task(try_delete(message))
    return

# Создаём обращение
db_get_or_create_user(user_id, user.username, user.first_name, user.last_name)
request_id = next(REQUEST_SEQ)
msg_type   = detect_message_type(message)

REQUESTS[request_id] = {
    "reason_code":  reason_code,
    "reason_title": reason_title,
    "status":       "open",
    "status_text":  "НЕ ОТВЕЧЕНО ❌",
    "answered_by":  None,
    "user_reaction": None,
    "admin_reactions": {"👍": set(), "🫶🏻": set()},
    "user": {
        "id":         user_id,
        "username":   user.username,
        "first_name": user.first_name,
        "last_name":  user.last_name,
    },
    "message_type": msg_type,
    "message_text": message.text if message.text else None,
    "caption":      message.caption if message.caption else None,
    "voice_duration": message.voice.duration if message.voice else None,
    "admin_message_refs": [],
}

card_text = build_admin_card_text(request_id)
card_kb   = admin_card_keyboard(request_id)

for admin_id in ADMINS:
    try:
        sent = await context.bot.send_message(
            admin_id, card_text,
            parse_mode=ParseMode.HTML,
            reply_markup=card_kb,
            disable_web_page_preview=True,
        )
        REQUESTS[request_id]["admin_message_refs"].append({
            "chat_id": admin_id, "message_id": sent.message_id
        })
        if not message.text:
            await message.forward(chat_id=admin_id)
    except Exception as e:
        logger.warning(f"Не удалось отправить обращение админу {admin_id}: {e}")

ok = await message.reply_text("✅ Сообщение отправлено администрации.")
context.application.create_task(delete_message_later(context, ok.chat_id, ok.message_id))
```

# =========================================

# POLL ANSWER HANDLER

# =========================================

async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
“”“Обрабатывает ответ на Quiz Poll.”””
poll_answer = update.poll_answer
if not poll_answer:
return

```
poll_id    = poll_answer.poll_id
user_id    = poll_answer.user.id
option_ids = poll_answer.option_ids

# Проверяем что это наш активный poll
if poll_id not in ACTIVE_POLLS:
    return

poll_data = ACTIVE_POLLS.get(poll_id, {})
if poll_data.get("user_id") != user_id:
    return

# Проверяем статус сессии
session = db_get_session(user_id)
if not session or session["status"] != "active":
    return

await handle_quiz_answer(user_id, poll_id, option_ids, context)
```

# =========================================

# BROADCAST HELPER

# =========================================

async def _do_broadcast(bot, text: str, target: str) -> tuple[int, int]:
“”“Выполняет рассылку. Возвращает (отправлено, ошибок).”””
conn = get_conn()
if target == “premium”:
users = conn.execute(”””
SELECT u.user_id FROM users u
JOIN premium p ON p.user_id=u.user_id
WHERE p.active=1 AND u.is_banned=0
“””).fetchall()
elif target == “free”:
users = conn.execute(”””
SELECT u.user_id FROM users u
LEFT JOIN premium p ON p.user_id=u.user_id
WHERE (p.active IS NULL OR p.active=0) AND u.is_banned=0
“””).fetchall()
else:
users = conn.execute(“SELECT user_id FROM users WHERE is_banned=0”).fetchall()
conn.close()

```
sent = failed = 0
for u in users:
    uid = u["user_id"]
    ok  = await safe_send(bot, uid, text, parse_mode=ParseMode.HTML)
    if ok:
        sent   += 1
    else:
        failed += 1
    await asyncio.sleep(0.05)  # Rate limit protection

return sent, failed
```

# =========================================

# ADMIN COMMANDS

# =========================================

async def cmd_givepremium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
user = update.effective_user
if not is_admin(user.id):
return
args = context.args
if not args or len(args) < 2:
await update.message.reply_text(“Использование: /givepremium <ID> <дни>”)
return
try:
uid  = int(args[0])
days = int(args[1])
except ValueError:
await update.message.reply_text(“❌ Неверный формат. Пример: /givepremium 123456789 30”)
return

```
db_get_or_create_user(uid)
expires = db_grant_premium(uid, days, source="admin")
await update.message.reply_text(f"✅ Premium выдан пользователю {uid} на {days} дней.\nДо: {expires.strftime('%d.%m.%Y %H:%M')}")
await safe_send(
    context.bot, uid,
    f"🎉 Вам выдан Premium на {days} дней!\nДействует до: {expires.strftime('%d.%m.%Y %H:%M')}",
)
```

async def cmd_revokepremium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
user = update.effective_user
if not is_admin(user.id):
return
args = context.args
if not args:
await update.message.reply_text(“Использование: /revokepremium <ID>”)
return
try:
uid = int(args[0])
except ValueError:
await update.message.reply_text(“❌ Неверный ID.”)
return
db_revoke_premium(uid)
await update.message.reply_text(f”✅ Premium снят с пользователя {uid}.”)
await safe_send(context.bot, uid, “⚠️ Ваш Premium был деактивирован администрацией.”)

async def cmd_givecoins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
user = update.effective_user
if not is_admin(user.id):
return
args = context.args
if not args or len(args) < 2:
await update.message.reply_text(“Использование: /givecoins <ID> <монеты>”)
return
try:
uid    = int(args[0])
amount = int(args[1])
except ValueError:
await update.message.reply_text(“❌ Неверный формат.”)
return
db_get_or_create_user(uid)
new_balance = db_add_coins(uid, amount)
await update.message.reply_text(f”✅ Выдано {amount} монет пользователю {uid}. Баланс: {new_balance}”)
await safe_send(context.bot, uid, f”🪙 Вам начислено {amount} монет! Баланс: {new_balance}”)

async def cmd_setlimit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
user = update.effective_user
if not is_admin(user.id):
return
args = context.args
if not args or not args[0].isdigit():
await update.message.reply_text(“Использование: /setlimit <число>”)
return
db_set_setting(“daily_limit”, args[0])
await update.message.reply_text(f”✅ Дневной лимит установлен: {args[0]} вопросов”)

async def cmd_setchannel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
user = update.effective_user
if not is_admin(user.id):
return
args = context.args
if not args:
await update.message.reply_text(“Использование: /setchannel <@канал>”)
return
db_set_setting(“channel_id”, args[0])
await update.message.reply_text(f”✅ Канал подписки установлен: {args[0]}”)

async def cmd_setcoin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
user = update.effective_user
if not is_admin(user.id):
return
args = context.args
if not args or len(args) < 2:
await update.message.reply_text(
“Использование: /setcoin <ключ> <значение>\n\n”
“Ключи:\n”
“coins_easy, coins_medium, coins_hard\n”
“referral_coins, premium_3d_coins, premium_7d_coins\n”
“coin_buy_premium_enabled (1/0)\n”
“premium_coin_daily_limit\n”
“streak_bonus_coins”
)
return
db_set_setting(args[0], args[1])
await update.message.reply_text(f”✅ Настройка обновлена: {args[0]} = {args[1]}”)

async def cmd_addtopic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
user = update.effective_user
if not is_admin(user.id):
return
args = context.args
if not args:
await update.message.reply_text(“Использование: /addtopic <название> [premium]”)
return
name      = “ “.join(args[:-1]) if args[-1] == “premium” else “ “.join(args)
is_prem   = 1 if args[-1] == “premium” else 0
conn = get_conn()
conn.execute(“INSERT INTO topics(name, is_premium) VALUES(?,?)”, (name, is_prem))
conn.commit()
conn.close()
await update.message.reply_text(f”✅ Тема добавлена: {name} {’(Premium)’ if is_prem else ‘’}”)

async def cmd_giveaccess(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
user = update.effective_user
if not is_admin(user.id):
return
args = context.args
if not args:
await update.message.reply_text(“Использование: /giveaccess <ID>”)
return
try:
uid = int(args[0])
except ValueError:
await update.message.reply_text(“❌ Неверный ID.”)
return
db_get_or_create_user(uid)
conn = get_conn()
conn.execute(“UPDATE users SET has_access=1 WHERE user_id=?”, (uid,))
conn.commit()
conn.close()
await update.message.reply_text(f”✅ Доступ выдан пользователю {uid}”)
await safe_send(context.bot, uid, “✅ Вам открыт доступ к боту!”)

async def cmd_revokeaccess(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
user = update.effective_user
if not is_admin(user.id):
return
args = context.args
if not args:
await update.message.reply_text(“Использование: /revokeaccess <ID>”)
return
try:
uid = int(args[0])
except ValueError:
await update.message.reply_text(“❌ Неверный ID.”)
return
conn = get_conn()
conn.execute(“UPDATE users SET has_access=0 WHERE user_id=?”, (uid,))
conn.commit()
conn.close()
await update.message.reply_text(f”✅ Доступ снят с пользователя {uid}”)

# Дополнительный callback для выбора темы при импорте

async def _cb_import_to_topic(query, context, user) -> None:
“”“Выбор темы при импорте – сохраняет вопросы.”””
if not is_admin(user.id):
return
topic_id = int(query.data.split(”:”)[-1])
draft_id = context.user_data.get(STATE_ADMIN_DRAFT_ID)

```
if not draft_id:
    await query.edit_message_text("❌ Черновик не найден.")
    return

conn = get_conn()
draft = conn.execute("SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
conn.close()

if not draft:
    await query.edit_message_text("❌ Черновик не найден.")
    return

import json as _json
questions = _json.loads(draft["data"])
saved = 0
for q in questions:
    db_add_question(
        topic_id=topic_id,
        text=q["text"],
        options=q["options"],
        correct_idx=q["correct_idx"],
    )
    saved += 1

conn = get_conn()
conn.execute("UPDATE drafts SET status='saved' WHERE draft_id=?", (draft_id,))
conn.commit()
conn.close()

context.user_data.pop(STATE_ADMIN_DRAFT_ID, None)
context.user_data.pop(STATE_ADMIN_IMPORT,   None)

conn = get_conn()
topic = conn.execute("SELECT name FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
conn.close()

await query.edit_message_text(
    f'Сохранено {saved} вопросов в тему {topic["name"] if topic else "--"}!',
    reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Назад в импорт", callback_data="adm:import")],
        [InlineKeyboardButton("🏠 Главное меню",    callback_data="adm:back")],
    ]),
)
```

# (extended callbacks merged into callback_router below)

# =========================================

# MAIN

# =========================================

def main() -> None:
“”“Запуск бота.”””
init_db()

```
app = ApplicationBuilder().token(BOT_TOKEN).build()

# ---- Команды ----
app.add_handler(CommandHandler("start",         cmd_start))
app.add_handler(CommandHandler("admin",         cmd_admin))
app.add_handler(CommandHandler("id",            cmd_id))
app.add_handler(CommandHandler("cancel",        cmd_cancel))
app.add_handler(CommandHandler("banlist",       cmd_banlist))
app.add_handler(CommandHandler("profile",       cmd_profile))
app.add_handler(CommandHandler("givepremium",   cmd_givepremium))
app.add_handler(CommandHandler("revokepremium", cmd_revokepremium))
app.add_handler(CommandHandler("givecoins",     cmd_givecoins))
app.add_handler(CommandHandler("setlimit",      cmd_setlimit))
app.add_handler(CommandHandler("setchannel",    cmd_setchannel))
app.add_handler(CommandHandler("setcoin",       cmd_setcoin))
app.add_handler(CommandHandler("addtopic",      cmd_addtopic))
app.add_handler(CommandHandler("giveaccess",    cmd_giveaccess))
app.add_handler(CommandHandler("revokeaccess",  cmd_revokeaccess))

# ---- Callback ----
app.add_handler(CallbackQueryHandler(callback_router))

# ---- Poll answers ----
app.add_handler(PollAnswerHandler(handle_poll_answer))

# ---- Сообщения ----
app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

logger.info("✅ Бот запущен.")
app.run_polling(drop_pending_updates=True)
```

if **name** == “**main**”:
main()
