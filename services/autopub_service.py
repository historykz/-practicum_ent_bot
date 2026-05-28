"""
Сервис автоматической публикации тестов в чат + анонсы на канал.

Админ:
  /admin → «📅 Авто-публикация тестов»
  → выбирает раздел
  → выбирает тесты галочками
  → ставит время старта
  → бот по очереди публикует каждый тест в нужный чат
  → перед каждым шлёт анонс на канал со ссылкой на чат

Сохраняем настройки в БД: target_chat_id, channel_id, invite_link.
"""
import asyncio
import logging
import random
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot

import database as db

log = logging.getLogger(__name__)


# Имя settings-ключей
S_CHAT_ID = "autopub_chat_id"          # куда публиковать сами тесты
S_CHAT_TITLE = "autopub_chat_title"    # для отображения
S_CHANNEL_ID = "autopub_channel_id"    # канал для анонсов
S_INVITE_LINK = "autopub_invite_link"  # ссылка-приглашение на чат


def _get_setting(key: str) -> Optional[str]:
    r = db.fetchone("SELECT value FROM settings WHERE key=?", (key,))
    return r['value'] if r else None


def _set_setting(key: str, value: str):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value))


def get_autopub_config() -> dict:
    return {
        'chat_id': _get_setting(S_CHAT_ID),
        'chat_title': _get_setting(S_CHAT_TITLE) or '',
        'channel_id': _get_setting(S_CHANNEL_ID),
        'invite_link': _get_setting(S_INVITE_LINK) or '',
    }


def set_autopub_config(chat_id: str = None, chat_title: str = None,
                        channel_id: str = None, invite_link: str = None):
    if chat_id is not None:
        _set_setting(S_CHAT_ID, str(chat_id))
    if chat_title is not None:
        _set_setting(S_CHAT_TITLE, str(chat_title))
    if channel_id is not None:
        _set_setting(S_CHANNEL_ID, str(channel_id))
    if invite_link is not None:
        _set_setting(S_INVITE_LINK, str(invite_link))


# ===================== ТАБЛИЦА РАСПИСАНИЯ =====================

def ensure_schedule_table():
    """Создаёт таблицу для запланированных публикаций (если её нет)."""
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS autopub_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_id INTEGER NOT NULL,
                run_at TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                error TEXT DEFAULT '',
                created_by INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_autopub_status_time "
                    "ON autopub_queue(status, run_at)")
    except Exception as e:
        log.exception("ensure_schedule_table: %s", e)


def enqueue_test(test_id: int, run_at: datetime, created_by: int) -> int:
    """Поставить тест в очередь на публикацию."""
    cur = db.execute(
        "INSERT INTO autopub_queue (test_id, run_at, created_by) VALUES (?, ?, ?)",
        (test_id, run_at.isoformat(), created_by))
    return cur.lastrowid


def list_pending() -> list:
    return db.fetchall(
        "SELECT * FROM autopub_queue WHERE status='pending' "
        "ORDER BY run_at LIMIT 100")


def cancel_pending(qid: int):
    db.execute("UPDATE autopub_queue SET status='cancelled' WHERE id=?", (qid,))


# ===================== ПУБЛИКАЦИЯ =====================

async def publish_test_to_chat(bot: Bot, test_id: int) -> bool:
    """Опубликовать тест в чат как серию Quiz Poll. Вернёт True если ок."""
    cfg = get_autopub_config()
    chat_id = cfg['chat_id']
    if not chat_id:
        log.warning("publish_test_to_chat: chat_id не задан")
        return False
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))
    if not test:
        return False
    questions = db.fetchall(
        "SELECT * FROM questions WHERE test_id=? ORDER BY order_num, id",
        (test_id,))
    if not questions:
        return False
    # Шапка
    try:
        await bot.send_message(
            int(chat_id),
            f"📚 <b>{test['title']}</b>\n\n"
            f"Вопросов: {len(questions)}\n"
            f"Время на вопрос: {test.get('time_per_question') or 30} сек\n\n"
            f"Поехали! 🚀")
    except Exception as e:
        log.warning("send header: %s", e)
        return False
    # Сами вопросы как Quiz Poll
    for q in questions:
        opts = db.fetchall(
            "SELECT * FROM question_options WHERE question_id=? "
            "ORDER BY order_num, id", (q['id'],))
        if len(opts) < 2:
            continue
        correct_idx = 0
        for i, o in enumerate(opts):
            if o['is_correct']:
                correct_idx = i
                break
        try:
            await bot.send_poll(
                int(chat_id),
                question=q['text'][:300],
                options=[o['text'][:100] for o in opts[:10]],
                type='quiz',
                correct_option_id=correct_idx,
                is_anonymous=True,
                open_period=test.get('time_per_question') or 30,
                explanation=(q.get('explanation') or '')[:200] or None,
            )
            await asyncio.sleep(0.5)  # анти-флуд
        except Exception as e:
            log.warning("send poll q=%s: %s", q['id'], e)
    return True


async def announce_test_on_channel(bot: Bot, test: dict, when_str: str) -> bool:
    """Анонс на канале со ссылкой на чат."""
    cfg = get_autopub_config()
    channel_id = cfg['channel_id']
    if not channel_id:
        log.warning("announce: channel_id не задан")
        return False
    invite = cfg.get('invite_link') or ''

    text = (
        f"🔥 <b>СКОРО ТЕСТ В ЧАТЕ!</b>\n\n"
        f"📚 <b>{test['title']}</b>\n"
        f"⏰ Старт: <b>{when_str}</b>\n"
        f"❓ Вопросов: <b>{db.fetchone('SELECT COUNT(*) AS c FROM questions WHERE test_id=?', (test['id'],))['c']}</b>\n\n"
        f"👇 Заходи в чат, чтобы участвовать:\n"
        f"{invite}"
    )
    try:
        await bot.send_message(int(channel_id), text,
                                 parse_mode="HTML",
                                 disable_web_page_preview=False)
        return True
    except Exception as e:
        log.warning("announce: %s", e)
        return False


# ===================== ВОРКЕР =====================

_worker_task: Optional[asyncio.Task] = None


async def _worker_loop(bot: Bot):
    log.info("autopub worker started")
    while True:
        try:
            await asyncio.sleep(20)  # проверка каждые 20 сек
            now = datetime.utcnow().isoformat()
            rows = db.fetchall(
                "SELECT * FROM autopub_queue "
                "WHERE status='pending' AND run_at <= ? "
                "ORDER BY run_at LIMIT 5", (now,))
            for r in rows:
                qid = r['id']
                test_id = r['test_id']
                # Помечаем как в работе
                db.execute("UPDATE autopub_queue SET status='running' WHERE id=?", (qid,))
                try:
                    ok = await publish_test_to_chat(bot, test_id)
                    if ok:
                        db.execute("UPDATE autopub_queue SET status='done' WHERE id=?",
                                    (qid,))
                    else:
                        db.execute(
                            "UPDATE autopub_queue SET status='failed', error=? WHERE id=?",
                            ('publish returned False', qid))
                except Exception as e:
                    log.exception("worker publish: %s", e)
                    db.execute(
                        "UPDATE autopub_queue SET status='failed', error=? WHERE id=?",
                        (str(e)[:200], qid))
        except asyncio.CancelledError:
            log.info("autopub worker cancelled")
            return
        except Exception as e:
            log.exception("worker loop: %s", e)


def start_worker(bot: Bot):
    global _worker_task
    if _worker_task and not _worker_task.done():
        return
    ensure_schedule_table()
    _worker_task = asyncio.create_task(_worker_loop(bot))


# ===================== РАНДОМНЫЕ ВОПРОСЫ НА КАНАЛ =====================

async def post_random_quiz_polls_to_channel(
        bot: Bot, count: int = 10,
        category_id: Optional[int] = None,
        language: str = 'ru') -> tuple[int, int]:
    """
    Опубликовать N рандомных Quiz Poll на канале из бесплатных НЕприватных тестов.
    Вернёт (отправлено, ошибок).
    """
    cfg = get_autopub_config()
    channel_id = cfg.get('channel_id')
    if not channel_id:
        return 0, 0

    # Собираем кандидатов
    sql = """SELECT q.id, q.text, q.explanation, q.test_id, t.time_per_question
             FROM questions q JOIN tests t ON t.id=q.test_id
             WHERE t.status='active' AND t.is_paid=0
               AND COALESCE(t.is_private,0)=0
               AND t.language=?"""
    args = [language]
    if category_id is not None:
        sql += " AND t.category_id=?"
        args.append(category_id)
    rows = db.fetchall(sql, tuple(args))
    if not rows:
        return 0, 0
    sample = random.sample(rows, min(count, len(rows)))

    sent = 0
    failed = 0
    for q in sample:
        opts = db.fetchall(
            "SELECT * FROM question_options WHERE question_id=? "
            "ORDER BY order_num, id", (q['id'],))
        if len(opts) < 2:
            continue
        correct_idx = 0
        for i, o in enumerate(opts):
            if o['is_correct']:
                correct_idx = i
                break
        try:
            await bot.send_poll(
                int(channel_id),
                question=q['text'][:300],
                options=[o['text'][:100] for o in opts[:10]],
                type='quiz',
                correct_option_id=correct_idx,
                is_anonymous=True,
                open_period=q.get('time_per_question') or 30,
                explanation=(q.get('explanation') or '')[:200] or None,
            )
            sent += 1
            await asyncio.sleep(0.7)
        except Exception as e:
            log.warning("post random poll: %s", e)
            failed += 1
    return sent, failed
