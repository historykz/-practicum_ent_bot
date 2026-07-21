"""
Автопубликация вопросов из ВЫБРАННЫХ тестов в канал по расписанию (Quiz Poll).

Флоу: админ выбирает тесты галочками → 5/10 вопросов → период
(каждый день / через день / дни недели) → время (Астана) → канал →
работает до отмены.

Выбор вопросов:
- Квота распределяется поровну между тестами (остаток — случайным +1).
- Ведём лог опубликованных (job+test+question): свежие вопросы в приоритете.
- Пул теста исчерпан → лог по нему сбрасывается → повтор по кругу,
  публикация не останавливается.
- Порядок тестов и вопросов — случайный.

Отправка: sendPoll type=quiz, is_anonymous=True (обязательно для каналов),
correct_option_id; фото вопроса — отдельным sendPhoto ПЕРЕД опросом.
Лимиты Telegram: вопрос ≤300, вариант ≤100, вариантов ≤10.
"""
import asyncio
import json
import logging
import random
from datetime import datetime, timedelta, timezone

from aiogram import Bot

import database as db

log = logging.getLogger(__name__)
ALMATY = timezone(timedelta(hours=5))

PHOTO_POLL_PAUSE = 0.35     # пауза фото→опрос (flood)
BETWEEN_QUESTIONS = 1.6     # пауза между вопросами


# ================= JOBS =================

def create_job(created_by: int, test_ids: list, questions_per_run: int,
               schedule_type: str, weekdays: str, run_time: str,
               channel_id) -> int:
    cur = db.execute(
        """INSERT INTO auto_publish_jobs
             (created_by, test_ids, questions_per_run, schedule_type,
              weekdays, run_time, channel_id, status)
           VALUES (?,?,?,?,?,?,?, 'active')""",
        (created_by, json.dumps(test_ids), questions_per_run,
         schedule_type, weekdays or '', run_time, str(channel_id)))
    return cur.lastrowid


def get_job(job_id: int):
    row = db.fetchone("SELECT * FROM auto_publish_jobs WHERE id=?", (job_id,))
    return dict(row) if row else None


def list_jobs(include_stopped: bool = False) -> list:
    q = "SELECT * FROM auto_publish_jobs"
    if not include_stopped:
        q += " WHERE status IN ('active','paused')"
    rows = db.fetchall(q + " ORDER BY id DESC")
    return [dict(r) for r in rows]


def set_status(job_id: int, status: str):
    db.execute("UPDATE auto_publish_jobs SET status=? WHERE id=?",
               (status, job_id))


def delete_job(job_id: int):
    db.execute("DELETE FROM auto_publish_jobs WHERE id=?", (job_id,))
    db.execute("DELETE FROM published_questions_log WHERE job_id=?", (job_id,))


def job_tests(job: dict) -> list:
    try:
        ids = json.loads(job.get('test_ids') or '[]')
        return [int(i) for i in ids]
    except Exception:
        return []


def published_total(job_id: int) -> int:
    row = db.fetchone(
        "SELECT COUNT(*) AS c FROM published_questions_log WHERE job_id=?",
        (job_id,))
    return row['c'] if row else 0


def job_history(job_id: int, limit: int = 15) -> list:
    rows = db.fetchall(
        """SELECT l.published_at, l.question_id, t.title,
                  substr(q.text,1,50) AS qtext
           FROM published_questions_log l
           LEFT JOIN tests t ON t.id=l.test_id
           LEFT JOIN questions q ON q.id=l.question_id
           WHERE l.job_id=? ORDER BY l.id DESC LIMIT ?""",
        (job_id, limit))
    return [dict(r) for r in rows]


# ================= ВЫБОР ВОПРОСОВ =================

def _fresh_question_ids(job_id: int, test_id: int) -> list:
    """Вопросы теста, ещё не публиковавшиеся этой задачей."""
    rows = db.fetchall(
        """SELECT q.id FROM questions q
           WHERE q.test_id=?
             AND q.id NOT IN (SELECT question_id FROM published_questions_log
                              WHERE job_id=? AND test_id=?)""",
        (test_id, job_id, test_id))
    return [r['id'] for r in rows]


def _all_question_ids(test_id: int) -> list:
    rows = db.fetchall("SELECT id FROM questions WHERE test_id=?", (test_id,))
    return [r['id'] for r in rows]


def pick_questions(job: dict) -> list:
    """
    Вернуть список (test_id, question_id) на одну публикацию.
    Поровну между тестами; сначала исчерпываем ВСЕ свежие (по всем тестам),
    и только когда свежих нигде нет — повтор по кругу (сброс лога теста).
    """
    job_id = job['id']
    n = job.get('questions_per_run') or 5
    tests = [t for t in job_tests(job) if _all_question_ids(t)]
    if not tests:
        return []
    random.shuffle(tests)

    # Квоты: поровну + остаток случайным тестам
    base = n // len(tests)
    extra = n % len(tests)
    quotas = {t: base + (1 if i < extra else 0) for i, t in enumerate(tests)}

    fresh = {}
    for t in tests:
        f = _fresh_question_ids(job_id, t)
        random.shuffle(f)
        fresh[t] = f

    picked = []
    # Шаг 1: раздаём по квотам из свежих
    debt = 0
    for t in tests:
        take = fresh[t][:quotas[t]]
        fresh[t] = fresh[t][quotas[t]:]
        picked += [(t, q) for q in take]
        debt += quotas[t] - len(take)
    # Шаг 2: долг добираем из ОСТАВШИХСЯ свежих любых тестов
    if debt > 0:
        rest = [(t, q) for t in tests for q in fresh[t]]
        random.shuffle(rest)
        take = rest[:debt]
        picked += take
        debt -= len(take)
    # Шаг 3: свежих нигде нет — повтор: сброс лога исчерпанных тестов
    if debt > 0:
        pool = []
        for t in tests:
            if not _fresh_question_ids(job_id, t):
                db.execute(
                    "DELETE FROM published_questions_log "
                    "WHERE job_id=? AND test_id=?", (job_id, t))
            for q in _all_question_ids(t):
                if (t, q) not in picked:
                    pool.append((t, q))
        random.shuffle(pool)
        picked += pool[:debt]

    random.shuffle(picked)
    return picked[:n]


# ================= ОТПРАВКА =================

def _prep_poll(question: dict, options: list):
    """Подогнать вопрос под лимиты Telegram. Вернёт (qtext, opts, correct_id, explanation)."""
    qtext = (question.get('text') or '').strip()
    explanation = (question.get('explanation') or '').strip() or None
    if len(qtext) > 300:
        # хвост вопроса переносим в explanation
        tail = qtext[290:]
        qtext = qtext[:290] + "…"
        explanation = ("…" + tail + ("\n\n" + explanation if explanation else ""))[:200]
    if explanation and len(explanation) > 200:
        explanation = explanation[:197] + "…"

    # Варианты: ≤10, правильный обязан попасть
    correct = [o for o in options if o.get('is_correct')]
    others = [o for o in options if not o.get('is_correct')]
    if len(options) > 10:
        options = (correct[:1] + others)[:10]
    opts, correct_id = [], 0
    for i, o in enumerate(options):
        t = (o.get('text') or '').strip()
        if len(t) > 100:
            t = t[:97] + "…"
        if not t:
            t = "—"
        opts.append(t)
        if o.get('is_correct'):
            correct_id = i
    return qtext, opts, correct_id, explanation


async def publish_run(bot: Bot, job: dict) -> tuple:
    """Одна публикация по задаче. Возвращает (sent, failed)."""
    picked = pick_questions(job)
    channel_id = int(job['channel_id'])
    sent = failed = 0
    now_iso = datetime.now(ALMATY).isoformat()
    for test_id, qid in picked:
        q = db.fetchone("SELECT * FROM questions WHERE id=?", (qid,))
        if not q:
            continue
        opts = db.fetchall(
            "SELECT * FROM question_options WHERE question_id=? ORDER BY order_num",
            (qid,))
        opts = [dict(o) for o in opts]
        if len(opts) < 2 or not any(o.get('is_correct') for o in opts):
            continue
        qtext, poll_opts, correct_id, expl = _prep_poll(dict(q), opts)
        try:
            if q.get('photo_file_id'):
                try:
                    await bot.send_photo(channel_id, q['photo_file_id'])
                    await asyncio.sleep(PHOTO_POLL_PAUSE)
                except Exception as e:
                    log.warning("qp photo %s: %s", qid, e)
            await bot.send_poll(
                chat_id=channel_id, question=qtext, options=poll_opts,
                type="quiz", correct_option_id=correct_id,
                is_anonymous=True, explanation=expl)
            db.execute(
                """INSERT INTO published_questions_log
                     (job_id, test_id, question_id, published_at)
                   VALUES (?,?,?,?)""", (job['id'], test_id, qid, now_iso))
            sent += 1
        except Exception as e:
            log.warning("qp send %s: %s", qid, e)
            failed += 1
        await asyncio.sleep(BETWEEN_QUESTIONS)
    return sent, failed


# ================= РАСПИСАНИЕ =================

WEEKDAY_CODES = ['MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU']


def _is_due(job: dict, now: datetime) -> bool:
    """Пора ли запускать задачу сейчас (проверка раз в минуту)."""
    if job.get('status') != 'active':
        return False
    if now.strftime("%H:%M") != (job.get('run_time') or ''):
        return False
    today = now.strftime("%Y-%m-%d")
    last = job.get('last_run_date')
    if last == today:
        return False
    st = job.get('schedule_type') or 'daily'
    if st == 'daily':
        return True
    if st == 'every2':
        if not last:
            return True
        try:
            d_last = datetime.strptime(last, "%Y-%m-%d").date()
            return (now.date() - d_last).days >= 2
        except Exception:
            return True
    if st == 'weekdays':
        code = WEEKDAY_CODES[now.weekday()]
        days = (job.get('weekdays') or '').split(',')
        return code in days
    return False


async def quiz_publish_loop(bot: Bot):
    """Фоновый цикл: раз в минуту проверяет задачи."""
    log.info("Автопубликация квизов: планировщик запущен")
    while True:
        try:
            now = datetime.now(ALMATY)
            for job in list_jobs():
                if not _is_due(job, now):
                    continue
                db.execute(
                    "UPDATE auto_publish_jobs SET last_run_date=? WHERE id=?",
                    (now.strftime("%Y-%m-%d"), job['id']))
                try:
                    sent, failed = await publish_run(bot, job)
                    log.info("qp job %s: sent=%s failed=%s",
                             job['id'], sent, failed)
                except Exception as e:
                    log.warning("qp run job %s: %s", job['id'], e)
        except Exception as e:
            log.warning("quiz_publish_loop: %s", e)
        await asyncio.sleep(60)
