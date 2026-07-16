"""
Планировщик автозапуска тестов по расписанию.

Админ задаёт: раздел, дату начала/конца, ежедневное время (Астана),
сколько тестов в день. Каждый день в назначенное время бот публикует
тест(ы) в чат с анонсом.

Умный выбор теста:
1. Тесты которые ещё НЕ запускались
2. Тесты которые запускались реже всего
3. Тесты с меньшим числом участников
4. Если всё исчерпано — повтор с начала
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot

import config
import database as db

log = logging.getLogger(__name__)
ALMATY = timezone(timedelta(hours=5))

# Ограничения интервала сообщений во время теста
SLOWMODE_DURING_TEST = 300   # 5 минут между сообщениями
SLOWMODE_NORMAL = 5          # 5 секунд обычный режим


def _now_almaty() -> datetime:
    return datetime.now(ALMATY)


def _today_almaty() -> str:
    return _now_almaty().strftime("%Y-%m-%d")


def create_schedule(chat_id, category_id, start_date, end_date,
                    daily_time, tests_per_day=1, allow_paid=0,
                    allow_private=0, bot_username='', created_by=None,
                    channel_id=None, announce_delay=60) -> int:
    """Создать расписание автозапуска."""
    cur = db.execute(
        """INSERT INTO auto_schedule
           (chat_id, category_id, start_date, end_date, daily_time,
            tests_per_day, allow_paid, allow_private, status,
            bot_username, created_by, channel_id, announce_delay)
           VALUES (?,?,?,?,?,?,?,?, 'active', ?, ?, ?, ?)""",
        (str(chat_id), category_id, start_date, end_date, daily_time,
         tests_per_day, allow_paid, allow_private, bot_username, created_by,
         str(channel_id) if channel_id else None, announce_delay))
    return cur.lastrowid


def get_active_schedules() -> list:
    """Все активные расписания."""
    rows = db.fetchall("SELECT * FROM auto_schedule WHERE status='active'")
    return [dict(r) for r in rows]


def get_schedule(schedule_id: int) -> dict:
    row = db.fetchone("SELECT * FROM auto_schedule WHERE id=?", (schedule_id,))
    return dict(row) if row else None


def get_schedule_by_chat(chat_id) -> dict:
    """Активное расписание для чата (одно на чат)."""
    row = db.fetchone(
        "SELECT * FROM auto_schedule WHERE chat_id=? AND status='active' "
        "ORDER BY id DESC LIMIT 1", (str(chat_id),))
    return dict(row) if row else None


def update_schedule(schedule_id: int, **fields):
    """Обновить поля расписания."""
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [schedule_id]
    db.execute(f"UPDATE auto_schedule SET {cols} WHERE id=?", tuple(vals))


def stop_schedule(schedule_id: int):
    db.execute("UPDATE auto_schedule SET status='stopped' WHERE id=?",
               (schedule_id,))


def _eligible_tests(schedule: dict) -> list:
    """Тесты раздела которые можно запускать (с учётом платных/приватных)."""
    cat_id = schedule.get('category_id')
    filters = ["status='active'",
               "(SELECT COUNT(*) FROM questions WHERE test_id=tests.id) > 0"]
    params = []
    if cat_id:
        filters.append("category_id=?")
        params.append(cat_id)
    if not schedule.get('allow_paid'):
        filters.append("COALESCE(is_paid,0)=0")
    if not schedule.get('allow_private'):
        filters.append("COALESCE(is_private,0)=0")
    where = " AND ".join(filters)
    rows = db.fetchall(f"SELECT * FROM tests WHERE {where}", tuple(params))
    return [dict(r) for r in rows]


def pick_next_test(schedule: dict) -> dict:
    """
    Умный выбор следующего теста:
    1. Ещё не запускавшиеся
    2. Реже всего запускавшиеся
    3. С меньшим числом участников
    4. Повтор с начала
    """
    tests = _eligible_tests(schedule)
    if not tests:
        return None
    sched_id = schedule['id']

    # Статистика запусков по каждому тесту в этом расписании
    stats = {}
    for t in tests:
        row = db.fetchone(
            """SELECT COUNT(*) AS runs, COALESCE(SUM(participants),0) AS parts
               FROM auto_schedule_runs
               WHERE schedule_id=? AND test_id=?""",
            (sched_id, t['id']))
        stats[t['id']] = {
            'runs': row['runs'] if row else 0,
            'parts': row['parts'] if row else 0,
        }

    # Сортируем: (кол-во запусков ASC, кол-во участников ASC, id ASC)
    tests_sorted = sorted(
        tests,
        key=lambda t: (stats[t['id']]['runs'],
                       stats[t['id']]['parts'],
                       t['id']))
    # Первый в списке — наименее запускавшийся с наименьшим числом участников
    return tests_sorted[0]


def record_run(schedule_id: int, test_id: int, participants: int = 0):
    """Записать факт запуска теста."""
    db.execute(
        """INSERT INTO auto_schedule_runs
           (schedule_id, test_id, run_date, participants)
           VALUES (?,?,?,?)""",
        (schedule_id, test_id, _now_almaty().isoformat(), participants))


def update_run_participants(schedule_id: int, test_id: int, participants: int):
    """Обновить число участников последнего запуска теста."""
    row = db.fetchone(
        """SELECT id FROM auto_schedule_runs
           WHERE schedule_id=? AND test_id=?
           ORDER BY id DESC LIMIT 1""", (schedule_id, test_id))
    if row:
        db.execute("UPDATE auto_schedule_runs SET participants=? WHERE id=?",
                   (participants, row['id']))


def record_chat_activity(chat_id, user_tg_id):
    """Отметить что пользователь активен в чате (для статистики)."""
    try:
        db.execute(
            "INSERT OR IGNORE INTO chat_activity (chat_id, user_tg_id) "
            "VALUES (?,?)", (str(chat_id), user_tg_id))
    except Exception:
        pass


def get_schedule_stats(schedule_id: int) -> dict:
    """Статистика по расписанию для админа."""
    sched = get_schedule(schedule_id)
    if not sched:
        return {}
    # Активность в чате за период
    activity = db.fetchone(
        "SELECT COUNT(*) AS c FROM chat_activity WHERE chat_id=?",
        (sched['chat_id'],))['c']
    # Запуски тестов
    runs = db.fetchall(
        """SELECT r.test_id, t.title,
                  COUNT(*) AS run_count,
                  COALESCE(SUM(r.participants),0) AS total_parts
           FROM auto_schedule_runs r
           LEFT JOIN tests t ON t.id = r.test_id
           WHERE r.schedule_id=?
           GROUP BY r.test_id
           ORDER BY run_count DESC""", (schedule_id,))
    # Тесты которые ещё не запускались
    eligible = _eligible_tests(sched)
    run_test_ids = {r['test_id'] for r in runs}
    never_run = [t for t in eligible if t['id'] not in run_test_ids]
    return {
        'chat_activity': activity,
        'runs': [dict(r) for r in runs],
        'never_run': never_run,
        'total_eligible': len(eligible),
    }


async def set_chat_slowmode(bot: Bot, chat_id, seconds: int) -> bool:
    """
    Установить slow mode (интервал между сообщениями) в чате.
    seconds=300 во время теста, seconds=5 обычный режим.
    Чат НЕ закрывается, только меняется интервал.
    """
    try:
        await bot.set_chat_permissions(
            int(chat_id),
            permissions=None,  # не трогаем права, только slow mode ниже
        ) if False else None
        # aiogram: slow_mode_delay через set_chat_slow_mode (нет прямого метода)
        # Используем raw API
        await bot(_make_slowmode_request(int(chat_id), seconds))
        return True
    except Exception as e:
        log.warning("set slowmode %s: %s", chat_id, e)
        return False


def _make_slowmode_request(chat_id: int, seconds: int):
    """Создать запрос setChatSlowMode (raw Telegram API)."""
    from aiogram.methods import TelegramMethod
    from aiogram.methods.base import Response

    class SetChatSlowMode(TelegramMethod[bool]):
        __returning__ = bool
        __api_method__ = "setChatSlowModeDelay"
        chat_id: int
        seconds: int

    return SetChatSlowMode(chat_id=chat_id, seconds=seconds)


async def scheduler_loop(bot: Bot):
    """
    Главный цикл планировщика. Каждую минуту проверяет,
    не пора ли запустить тест по какому-нибудь расписанию.
    """
    from services import autopub_service
    log.info("Планировщик автозапуска запущен")
    while True:
        try:
            now = _now_almaty()
            today = now.strftime("%Y-%m-%d")
            cur_hm = now.strftime("%H:%M")

            for sched in get_active_schedules():
                # Проверяем период
                if today < sched['start_date']:
                    continue  # ещё не началось
                if today > sched['end_date']:
                    update_schedule(sched['id'], status='finished')
                    continue
                # Уже запускали сегодня?
                if sched.get('last_run_date') == today:
                    continue
                # Настало ли время? (с точностью до минуты)
                if cur_hm != sched['daily_time']:
                    continue

                # ЗАПУСКАЕМ тесты на сегодня
                await _run_scheduled_tests(bot, sched)
                update_schedule(sched['id'], last_run_date=today)

        except Exception as e:
            log.warning("scheduler_loop: %s", e)
        await asyncio.sleep(60)  # проверка каждую минуту


async def _run_scheduled_tests(bot: Bot, sched: dict):
    """Запустить N тестов по расписанию с анонсом на канал + в чат."""
    tests_per_day = sched.get('tests_per_day', 1) or 1
    chat_id = sched['chat_id']
    channel_id = sched.get('channel_id')
    announce_delay = sched.get('announce_delay') or 60
    bot_username = sched.get('bot_username', '')

    for _ in range(tests_per_day):
        test = pick_next_test(sched)
        if not test:
            log.info("Нет тестов для запуска в расписании %s", sched['id'])
            break
        try:
            await _announce_and_launch(
                bot, test, chat_id, bot_username,
                category_id=sched.get('category_id'),
                channel_id=channel_id, announce_delay=announce_delay)
            record_run(sched['id'], test['id'], participants=0)
        except Exception as e:
            log.warning("run scheduled test %s: %s", test['id'], e)
        await asyncio.sleep(3)


async def _announce_and_launch(bot: Bot, test: dict, chat_id, bot_username: str,
                                category_id=None, channel_id=None,
                                announce_delay=60):
    """
    Анонс теста на КАНАЛ и в ЧАТ, ожидание (чтобы зашли люди), затем старт.
    Анонс: тема, раздел, время, кнопка перейти к тестированию.
    """
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    qc = db.fetchone(
        "SELECT COUNT(*) AS c FROM questions WHERE test_id=?", (test['id'],))['c']
    time_per_q = test.get('time_per_question') or 30
    cat = None
    if category_id:
        cat = db.fetchone("SELECT name FROM test_categories WHERE id=?",
                          (category_id,))
    cat_name = cat['name'] if cat else "Общий"

    uname = bot_username.lstrip('@') if bot_username else ''
    # Кнопка перейти к тестированию (в бот)
    bot_kb = None
    if uname:
        bot_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="🚀 Перейти к тестированию",
                url=f"https://t.me/{uname}?start=quiz")
        ]])

    delay_min = max(1, round(announce_delay / 60))

    # --- Анонс НА КАНАЛ (со ссылкой в бота) ---
    if channel_id:
        channel_announce = (
            f"🆕 <b>Скоро тест по теме «{test['title']}»!</b>\n\n"
            f"📂 Раздел: {cat_name}\n"
            f"❓ Вопросов: {qc}\n"
            f"⏱ Время на вопрос: {time_per_q} сек\n\n"
            f"⏳ Старт через ~{delay_min} мин в нашем чате.\n"
            f"Заходи и участвуй! 💪")
        try:
            await bot.send_message(int(channel_id), channel_announce,
                                    parse_mode="HTML", reply_markup=bot_kb)
        except Exception as e:
            log.warning("announce to channel %s: %s", channel_id, e)

    # --- Анонс В ЧАТ (предупреждение перед стартом) ---
    chat_announce = (
        f"🔔 <b>Внимание! Скоро начнётся тест!</b>\n\n"
        f"📚 Тема: «{test['title']}»\n"
        f"📂 Раздел: {cat_name}\n"
        f"❓ Вопросов: {qc}\n"
        f"⏱ Время на вопрос: {time_per_q} сек\n\n"
        f"⏳ Старт через ~{delay_min} мин — успей зайти!\n"
        f"Отвечать будем прямо в чате 💪")
    try:
        await bot.send_message(int(chat_id), chat_announce, parse_mode="HTML")
    except Exception as e:
        log.warning("announce to chat %s: %s", chat_id, e)

    # --- Ждём чтобы люди зашли в чат ---
    await asyncio.sleep(announce_delay)

    # Включаем slow mode 5 минут на время теста
    await set_chat_slowmode(bot, chat_id, SLOWMODE_DURING_TEST)

    # --- Запускаем лобби ТОЧНО как «Запустить серию тестов» ---
    # start_lobby сам отправит карточку «🎲 Приготовьтесь пройти тест...»
    # с кнопкой «Пройти тест (2)» и ожиданием 2 человек.
    try:
        from services import group_quiz_service
        ok, key, gq_id = await group_quiz_service.start_lobby(
            bot, test, int(chat_id), admin_tg_id=0,
            language=test.get('language', 'ru'))
        if not ok:
            log.warning("start_lobby не запустил лобби: %s (chat=%s)",
                        key, chat_id)
    except Exception as e:
        log.warning("start group quiz %s: %s", test['id'], e)
