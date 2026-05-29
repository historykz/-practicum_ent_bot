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
                series_id TEXT DEFAULT '',
                series_pos INTEGER DEFAULT 0,
                series_total INTEGER DEFAULT 1,
                series_test_ids TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_autopub_status_time "
                    "ON autopub_queue(status, run_at)")
        # Миграции
        for sql in (
            "ALTER TABLE autopub_queue ADD COLUMN series_id TEXT DEFAULT ''",
            "ALTER TABLE autopub_queue ADD COLUMN series_pos INTEGER DEFAULT 0",
            "ALTER TABLE autopub_queue ADD COLUMN series_total INTEGER DEFAULT 1",
            "ALTER TABLE autopub_queue ADD COLUMN series_test_ids TEXT DEFAULT ''",
        ):
            try:
                db.execute(sql)
            except Exception:
                pass
    except Exception as e:
        log.exception("ensure_schedule_table: %s", e)


def enqueue_test(test_id: int, run_at: datetime, created_by: int,
                  series_id: str = '', series_pos: int = 0,
                  series_total: int = 1, series_test_ids: str = '') -> int:
    """Поставить тест в очередь на публикацию."""
    cur = db.execute(
        "INSERT INTO autopub_queue (test_id, run_at, created_by, "
        "series_id, series_pos, series_total, series_test_ids) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (test_id, run_at.isoformat(), created_by,
          series_id, series_pos, series_total, series_test_ids))
    return cur.lastrowid


def list_pending() -> list:
    return db.fetchall(
        "SELECT * FROM autopub_queue WHERE status='pending' "
        "ORDER BY run_at LIMIT 100")


def cancel_pending(qid: int):
    db.execute("UPDATE autopub_queue SET status='cancelled' WHERE id=?", (qid,))


# ===================== ПУБЛИКАЦИЯ =====================

async def publish_test_to_chat(bot: Bot, test_id: int) -> bool:
    """Запустить лобби теста в чате (как групповой квиз, ждёт 2 игроков)."""
    cfg = get_autopub_config()
    chat_id = cfg['chat_id']
    if not chat_id:
        log.warning("publish_test_to_chat: chat_id не задан")
        return False
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))
    if not test:
        return False
    questions = db.fetchall(
        "SELECT id FROM questions WHERE test_id=?", (test_id,))
    if not questions:
        return False
    # Запускаем лобби через group_quiz_service
    from services import group_quiz_service
    # Прорчищаем потенциально зависшие лобби в этом чате
    try:
        existing = db.fetchone(
            "SELECT id FROM group_quizzes WHERE chat_id=? AND status IN ('lobby','running')",
            (int(chat_id),))
        if existing:
            await group_quiz_service.stop_quiz(bot, int(chat_id), 0)
            await asyncio.sleep(1)
    except Exception:
        pass
    try:
        # admin_tg_id = 0 — системный запуск
        await group_quiz_service.start_lobby(
            bot, dict(test), int(chat_id),
            admin_tg_id=0,
            language=test.get('language') or 'ru')
        return True
    except Exception as e:
        log.exception("publish_test_to_chat lobby: %s", e)
        return False


async def publish_now_with_announce(bot: Bot, test_id: int,
                                      template_id: int = 0) -> bool:
    """
    Опубликовать тест прямо сейчас:
      1. Анонс на канале (без таймера, текст «уже идёт»)
      2. Лобби в чате
    """
    cfg = get_autopub_config()
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))
    if not test:
        return False
    channel_id = cfg.get('channel_id')
    invite = cfg.get('invite_link') or ''
    qc = db.fetchone(
        "SELECT COUNT(*) AS c FROM questions WHERE test_id=?", (test_id,))['c']
    if channel_id:
        try:
            await bot.send_message(
                int(channel_id),
                announce_now_text(template_id, test['title'], qc, invite),
                parse_mode="HTML",
                disable_web_page_preview=False)
        except Exception as e:
            log.warning("announce_now: %s", e)
    return await publish_test_to_chat(bot, test_id)


async def announce_test_on_channel(bot: Bot, test: dict, when_str: str,
                                     template_id: int = 0) -> bool:
    """Анонс на канале со ссылкой на чат. template_id — какой шаблон текста."""
    cfg = get_autopub_config()
    channel_id = cfg['channel_id']
    if not channel_id:
        log.warning("announce: channel_id не задан")
        return False
    invite = cfg.get('invite_link') or ''
    qcount = db.fetchone(
        'SELECT COUNT(*) AS c FROM questions WHERE test_id=?',
        (test['id'],))['c']
    title = test['title']

    text = build_announce_text(template_id, title, when_str, qcount, invite)
    try:
        await bot.send_message(int(channel_id), text,
                                 parse_mode="HTML",
                                 disable_web_page_preview=False)
        return True
    except Exception as e:
        log.warning("announce: %s", e)
        return False


# ===================== ШАБЛОНЫ АНОНСА =====================

ANNOUNCE_TEMPLATES = [
    {
        "name": "🔥 Зажигательный",
        "build": lambda title, when, qc, link: (
            f"🔥🔥🔥 <b>ВНИМАНИЕ, БУДУЩИЕ СТУДЕНТЫ!</b> 🔥🔥🔥\n\n"
            f"📚 Тема: <b>«{title}»</b>\n"
            f"⏰ Старт: <b>{when}</b>\n"
            f"❓ {qc} вопросов на скорость\n\n"
            f"💪 Проверь свои знания перед ЕНТ!\n"
            f"⚡️ Соревнуйся с другими в реальном времени!\n"
            f"🏆 Покажи кто тут лучший!\n\n"
            f"👇 ЗАХОДИ В ЧАТ ПРЯМО СЕЙЧАС:\n{link}\n\n"
            f"⏳ Не пропусти — места ограничены!"
        ),
    },
    {
        "name": "🎯 Деловой",
        "build": lambda title, when, qc, link: (
            f"🎯 <b>ОНЛАЙН-ТЕСТ В ЧАТЕ</b>\n\n"
            f"📖 Раздел: <b>{title}</b>\n"
            f"🕐 Время: <b>{when}</b>\n"
            f"📝 Количество вопросов: {qc}\n\n"
            f"Отличная возможность проверить подготовку к ЕНТ "
            f"в формате живого соревнования.\n\n"
            f"🔗 Присоединяйся к чату:\n{link}"
        ),
    },
    {
        "name": "🚀 Мотивационный",
        "build": lambda title, when, qc, link: (
            f"🚀 <b>ГОТОВ ПРОВЕРИТЬ СЕБЯ?</b>\n\n"
            f"Сегодня разбираем: <b>«{title}»</b>\n"
            f"⏰ Начинаем: <b>{when}</b>\n"
            f"❓ Вопросов: {qc}\n\n"
            f"Каждый тест — шаг к высокому баллу на ЕНТ! 📈\n"
            f"Не учи в одиночку — соревнуйся и запоминай лучше! 🧠\n\n"
            f"👇 Жми и заходи:\n{link}\n\n"
            f"Увидимся в чате! 😎"
        ),
    },
    {
        "name": "⚡️ Краткий",
        "build": lambda title, when, qc, link: (
            f"⚡️ <b>ТЕСТ: {title}</b>\n"
            f"⏰ {when} · {qc} вопросов\n\n"
            f"Заходи в чат 👇\n{link}"
        ),
    },
]


def build_announce_text(template_id: int, title: str, when: str,
                         qc: int, link: str) -> str:
    if template_id < 0 or template_id >= len(ANNOUNCE_TEMPLATES):
        template_id = 0
    return ANNOUNCE_TEMPLATES[template_id]["build"](title, when, qc, link)


def build_series_announce_text(template_id: int, titles: list[str],
                                  when: str, link: str) -> str:
    """Анонс серии нескольких тестов одним сообщением."""
    if template_id < 0 or template_id >= len(ANNOUNCE_TEMPLATES):
        template_id = 0

    # Список тем красивым списком
    topics = "\n".join(f"• <b>{t}</b>" for t in titles)
    count = len(titles)

    if template_id == 0:  # Зажигательный
        return (
            f"🔥🔥🔥 <b>ВНИМАНИЕ, БУДУЩИЕ СТУДЕНТЫ!</b> 🔥🔥🔥\n\n"
            f"📚 Сегодня нас ждёт <b>серия из {count} тестов</b>:\n\n"
            f"{topics}\n\n"
            f"⏰ Старт: <b>{when}</b>\n\n"
            f"💪 Проверь свои знания перед ЕНТ!\n"
            f"⚡️ Соревнуйся с другими в реальном времени!\n"
            f"🏆 Покажи кто тут лучший!\n\n"
            f"👇 ЗАХОДИ В ЧАТ ПРЯМО СЕЙЧАС:\n{link}\n\n"
            f"⏳ Не пропусти!")
    elif template_id == 1:  # Деловой
        return (
            f"🎯 <b>СЕРИЯ ОНЛАЙН-ТЕСТОВ</b>\n\n"
            f"📖 Темы ({count}):\n\n{topics}\n\n"
            f"🕐 Время начала: <b>{when}</b>\n\n"
            f"Отличная возможность проверить подготовку к ЕНТ "
            f"в формате живого соревнования.\n\n"
            f"🔗 Присоединяйся к чату:\n{link}")
    elif template_id == 2:  # Мотивационный
        return (
            f"🚀 <b>ГОТОВ ПРОВЕРИТЬ СЕБЯ?</b>\n\n"
            f"Сегодня разбираем <b>{count} тем</b>:\n\n{topics}\n\n"
            f"⏰ Начинаем: <b>{when}</b>\n\n"
            f"Каждый тест — шаг к высокому баллу на ЕНТ! 📈\n"
            f"Не учи в одиночку — соревнуйся и запоминай лучше! 🧠\n\n"
            f"👇 Жми и заходи:\n{link}\n\n"
            f"Увидимся в чате! 😎")
    else:  # Краткий
        return (
            f"⚡️ <b>СЕРИЯ ТЕСТОВ</b>\n\n"
            f"{topics}\n\n"
            f"⏰ {when}\n\n"
            f"Заходи в чат 👇\n{link}")


def build_series_now_text(template_id: int, titles: list[str], link: str) -> str:
    """Анонс серии когда стартует прямо сейчас."""
    topics = "\n".join(f"• <b>{t}</b>" for t in titles)
    count = len(titles)
    return (
        f"🟢 <b>СЕРИЯ ТЕСТОВ УЖЕ ИДЁТ!</b>\n\n"
        f"📚 Сейчас в чате <b>{count} тестов</b>:\n\n{topics}\n\n"
        f"⚡️ Заходи в чат и участвуй прямо сейчас:\n{link}\n\n"
        f"Успей! ⏳")


def announce_now_text(template_id: int, title: str, qc: int, link: str) -> str:
    """Текст когда тест НАЧИНАЕТСЯ прямо сейчас (без таймера)."""
    return (
        f"🟢 <b>ТЕСТ УЖЕ ИДЁТ!</b>\n\n"
        f"📚 <b>«{title}»</b>\n"
        f"❓ {qc} вопросов\n\n"
        f"⚡️ Заходи в чат и участвуй прямо сейчас:\n{link}\n\n"
        f"Успей ответить! ⏳"
    )


async def announce_batch_on_channel(bot: Bot, tests: list[dict],
                                      when_str: str,
                                      template_id: int = 0) -> bool:
    """ОДИН общий анонс на канале для нескольких тестов сразу."""
    cfg = get_autopub_config()
    channel_id = cfg.get('channel_id')
    if not channel_id:
        return False
    invite = cfg.get('invite_link') or ''
    # Список тем
    topics = "\n".join(f"• {t['title']}" for t in tests[:10])
    total_q = 0
    for t in tests:
        r = db.fetchone(
            "SELECT COUNT(*) AS c FROM questions WHERE test_id=?", (t['id'],))
        total_q += (r['c'] if r else 0)

    text = build_batch_announce_text(template_id, topics, len(tests),
                                       total_q, when_str, invite)
    try:
        await bot.send_message(int(channel_id), text,
                                 parse_mode="HTML",
                                 disable_web_page_preview=False)
        return True
    except Exception as e:
        log.warning("batch announce: %s", e)
        return False


async def announce_batch_short(bot: Bot, count: int, when_str: str) -> bool:
    """Короткий ПРЕД-анонс: только когда начнётся, без тем."""
    cfg = get_autopub_config()
    channel_id = cfg.get('channel_id')
    if not channel_id:
        return False
    invite = cfg.get('invite_link') or ''
    text = (
        f"🔔 <b>СКОРО ТЕСТ В ЧАТЕ</b>\n\n"
        f"⏰ Начинаем: <b>{when_str}</b>\n"
        f"📚 Тестов в серии: <b>{count}</b>\n\n"
        f"📩 Когда время подойдёт — пришлю темы и ссылку.\n\n"
        f"🔗 Чат: {invite}"
    )
    try:
        await bot.send_message(int(channel_id), text,
                                 parse_mode="HTML",
                                 disable_web_page_preview=False)
        return True
    except Exception as e:
        log.warning("short announce: %s", e)
        return False


async def announce_batch_reminder(bot: Bot, tests: list[dict]) -> bool:
    """Краткое напоминание когда время подошло — темы + ссылка."""
    cfg = get_autopub_config()
    channel_id = cfg.get('channel_id')
    if not channel_id:
        return False
    invite = cfg.get('invite_link') or ''
    topics = "\n".join(f"• {t['title']}" for t in tests[:10])
    text = (
        f"⏰ <b>НАЧИНАЕМ!</b>\n\n"
        f"📚 Темы:\n{topics}\n\n"
        f"👇 Заходи в чат:\n{invite}"
    )
    try:
        await bot.send_message(int(channel_id), text,
                                 parse_mode="HTML",
                                 disable_web_page_preview=False)
        return True
    except Exception as e:
        log.warning("reminder: %s", e)
        return False


async def _lock_chat(bot: Bot, chat_id: int) -> bool:
    """Закрыть чат — только админы пишут."""
    try:
        from aiogram.types import ChatPermissions
        perms = ChatPermissions(
            can_send_messages=False,
            can_send_audios=False,
            can_send_documents=False,
            can_send_photos=False,
            can_send_videos=False,
            can_send_video_notes=False,
            can_send_voice_notes=False,
            can_send_polls=False,
            can_send_other_messages=False,
            can_add_web_page_previews=False,
        )
        await bot.set_chat_permissions(chat_id, permissions=perms)
        await bot.send_message(
            chat_id,
            "🔒 <b>Чат закрыт на время тестов</b>\n\n"
            "Писать могут только админы.\n"
            "После окончания серии тестов чат откроется автоматически.",
            parse_mode="HTML")
        return True
    except Exception as e:
        log.warning("lock chat failed: %s", e)
        return False


async def _unlock_chat(bot: Bot, chat_id: int) -> bool:
    """Открыть чат обратно."""
    try:
        from aiogram.types import ChatPermissions
        perms = ChatPermissions(
            can_send_messages=True,
            can_send_audios=True,
            can_send_documents=True,
            can_send_photos=True,
            can_send_videos=True,
            can_send_video_notes=True,
            can_send_voice_notes=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
        )
        await bot.set_chat_permissions(chat_id, permissions=perms)
        await bot.send_message(
            chat_id,
            "🔓 <b>Чат открыт!</b>\n\n"
            "Серия тестов окончена. Можно писать.\n"
            "Спасибо всем участникам! 🎉",
            parse_mode="HTML")
        return True
    except Exception as e:
        log.warning("unlock chat failed: %s", e)
        return False


async def announce_single_reminder(bot: Bot, test: dict) -> bool:
    """Короткое напоминание про следующий тест — в ЧАТЕ где идут тесты, не на канале."""
    cfg = get_autopub_config()
    chat_id = cfg.get('chat_id')
    if not chat_id:
        return False
    text = (
        f"⏳ <b>Через 20 сек — новый тест!</b>\n\n"
        f"📚 <b>{test['title']}</b>\n\n"
        f"Готовься! 🚀"
    )
    try:
        await bot.send_message(int(chat_id), text,
                                 parse_mode="HTML")
        return True
    except Exception as e:
        log.warning("single reminder: %s", e)
        return False


async def announce_batch_now(bot: Bot, tests: list[dict],
                                template_id: int = 0) -> bool:
    """ОДИН анонс «уже идёт» для нескольких тестов сразу."""
    cfg = get_autopub_config()
    channel_id = cfg.get('channel_id')
    if not channel_id:
        return False
    invite = cfg.get('invite_link') or ''
    topics = "\n".join(f"• {t['title']}" for t in tests[:10])
    total_q = 0
    for t in tests:
        r = db.fetchone(
            "SELECT COUNT(*) AS c FROM questions WHERE test_id=?", (t['id'],))
        total_q += (r['c'] if r else 0)
    text = (
        f"🟢 <b>ТЕСТЫ УЖЕ ИДУТ В ЧАТЕ!</b>\n\n"
        f"📚 <b>Темы:</b>\n{topics}\n\n"
        f"❓ Всего вопросов: {total_q}\n\n"
        f"⚡️ Заходи в чат и участвуй прямо сейчас:\n{invite}\n\n"
        f"Успей ответить! ⏳"
    )
    try:
        await bot.send_message(int(channel_id), text,
                                 parse_mode="HTML",
                                 disable_web_page_preview=False)
        return True
    except Exception as e:
        log.warning("batch announce now: %s", e)
        return False


BATCH_TEMPLATES = [
    {
        "name": "🔥 Зажигательный",
        "build": lambda topics, n, qc, when, link: (
            f"🔥🔥🔥 <b>ВНИМАНИЕ, БУДУЩИЕ СТУДЕНТЫ!</b> 🔥🔥🔥\n\n"
            f"📚 <b>Темы ({n}):</b>\n{topics}\n\n"
            f"⏰ Старт: <b>{when}</b>\n"
            f"❓ Всего вопросов: <b>{qc}</b>\n\n"
            f"💪 Проверь знания перед ЕНТ!\n"
            f"⚡️ Соревнуйся в реальном времени!\n"
            f"🏆 Покажи кто тут лучший!\n\n"
            f"👇 ЗАХОДИ В ЧАТ:\n{link}\n\n"
            f"⏳ Места ограничены!"
        ),
    },
    {
        "name": "🎯 Деловой",
        "build": lambda topics, n, qc, when, link: (
            f"🎯 <b>СЕРИЯ ОНЛАЙН-ТЕСТОВ В ЧАТЕ</b>\n\n"
            f"📖 <b>Разделы ({n}):</b>\n{topics}\n\n"
            f"🕐 Старт: <b>{when}</b>\n"
            f"📝 Всего вопросов: {qc}\n\n"
            f"Отличная возможность проверить подготовку к ЕНТ "
            f"в формате живого соревнования.\n\n"
            f"🔗 Чат:\n{link}"
        ),
    },
    {
        "name": "🚀 Мотивационный",
        "build": lambda topics, n, qc, when, link: (
            f"🚀 <b>ГОТОВ ПРОВЕРИТЬ СЕБЯ?</b>\n\n"
            f"Сегодня разбираем <b>{n}</b> темы:\n{topics}\n\n"
            f"⏰ Начинаем: <b>{when}</b>\n"
            f"❓ Вопросов: {qc}\n\n"
            f"Каждый тест — шаг к высокому баллу! 📈\n"
            f"Не учи в одиночку — соревнуйся! 🧠\n\n"
            f"👇 Чат:\n{link}\n\n"
            f"Увидимся! 😎"
        ),
    },
    {
        "name": "⚡️ Краткий",
        "build": lambda topics, n, qc, when, link: (
            f"⚡️ <b>СЕРИЯ ТЕСТОВ ({n})</b>\n\n"
            f"{topics}\n\n"
            f"⏰ {when} · {qc} вопросов\n\n"
            f"Заходи 👇\n{link}"
        ),
    },
]


def build_batch_announce_text(template_id: int, topics: str, n: int,
                                qc: int, when: str, link: str) -> str:
    if template_id < 0 or template_id >= len(BATCH_TEMPLATES):
        template_id = 0
    return BATCH_TEMPLATES[template_id]["build"](topics, n, qc, when, link)


# ===================== МИКС ВОПРОСОВ ИЗ НЕСКОЛЬКИХ ТЕСТОВ =====================

def create_mixed_test(test_ids: list[int], created_by: int,
                       total: int = 10,
                       language: str = 'ru') -> Optional[int]:
    """
    Создаёт временный тест-микс: берёт поровну вопросов из каждого теста,
    добор рандомом до total. Вернёт id нового теста.
    """
    import random
    if not test_ids:
        return None
    n = len(test_ids)
    per = total // n        # поровну
    remainder = total - per * n  # добор рандомом

    selected_qids = []
    pools = {}  # test_id -> список оставшихся вопросов

    for tid in test_ids:
        qs = db.fetchall(
            "SELECT id FROM questions WHERE test_id=? ORDER BY RANDOM()", (tid,))
        pool = [q['id'] for q in qs]
        pools[tid] = pool
        take = pool[:per]
        selected_qids.extend(take)
        pools[tid] = pool[per:]  # остаток для добора

    # Добор остатка рандомом из всех оставшихся
    leftover = []
    for tid in test_ids:
        leftover.extend(pools[tid])
    random.shuffle(leftover)
    selected_qids.extend(leftover[:remainder])

    if not selected_qids:
        return None

    # Название микса
    titles = []
    for tid in test_ids:
        tr = db.fetchone("SELECT title FROM tests WHERE id=?", (tid,))
        if tr:
            titles.append(tr['title'])
    mix_title = " + ".join(titles[:3])
    if len(mix_title) > 120:
        mix_title = mix_title[:117] + "..."

    # Берём время на вопрос из первого теста
    first = db.fetchone("SELECT time_per_question FROM tests WHERE id=?",
                         (test_ids[0],))
    tpq = (first.get('time_per_question') if first else 30) or 30

    # Создаём временный тест (помечаем is_mix=1, не показываем в каталоге)
    cur = db.execute("""
        INSERT INTO tests (title, description, language, time_per_question,
                            is_paid, price, test_type, status, created_by,
                            is_private)
        VALUES (?, '', ?, ?, 0, 0, 'mix', 'mix_temp', ?, 1)
    """, (f"🎲 {mix_title}", language, tpq, created_by))
    mix_test_id = cur.lastrowid

    # Копируем выбранные вопросы в новый тест
    random.shuffle(selected_qids)
    for order, qid in enumerate(selected_qids[:total]):
        q = db.fetchone("SELECT * FROM questions WHERE id=?", (qid,))
        if not q:
            continue
        qcur = db.execute("""
            INSERT INTO questions (test_id, text, explanation, order_num, source_type)
            VALUES (?, ?, ?, ?, 'mix')
        """, (mix_test_id, q['text'], q.get('explanation') or '', order))
        new_qid = qcur.lastrowid
        opts = db.fetchall(
            "SELECT * FROM question_options WHERE question_id=? ORDER BY order_num, id",
            (qid,))
        for j, o in enumerate(opts):
            db.execute("""
                INSERT INTO question_options (question_id, text, is_correct, order_num)
                VALUES (?, ?, ?, ?)
            """, (new_qid, o['text'], o['is_correct'], j))

    return mix_test_id


def cleanup_mix_test(test_id: int):
    """Удалить временный микс-тест после использования."""
    try:
        qs = db.fetchall("SELECT id FROM questions WHERE test_id=?", (test_id,))
        for q in qs:
            db.execute("DELETE FROM question_options WHERE question_id=?", (q['id'],))
        db.execute("DELETE FROM questions WHERE test_id=?", (test_id,))
        db.execute("DELETE FROM tests WHERE id=? AND status='mix_temp'", (test_id,))
    except Exception as e:
        log.warning("cleanup_mix: %s", e)


# ===================== ВОРКЕР =====================

_worker_task: Optional[asyncio.Task] = None


async def _worker_loop(bot: Bot):
    log.info("autopub worker started")
    while True:
        try:
            await asyncio.sleep(10)
            now = datetime.utcnow().isoformat()
            rows = db.fetchall(
                "SELECT * FROM autopub_queue "
                "WHERE status='pending' AND run_at <= ? "
                "ORDER BY run_at LIMIT 5", (now,))
            for r in rows:
                qid = r['id']
                test_id = r['test_id']
                series_pos = r.get('series_pos') or 0
                series_total = r.get('series_total') or 1
                series_ids_str = r.get('series_test_ids') or ''

                db.execute("UPDATE autopub_queue SET status='running' WHERE id=?", (qid,))
                try:
                    posted_full_announce = False
                    # 1) Если первый в серии — шлём ПОЛНЫЙ напоминание-анонс с темами
                    if series_pos == 0 and series_ids_str:
                        try:
                            ids = [int(x) for x in series_ids_str.split(',') if x.strip().isdigit()]
                            tests_obj = []
                            for tid in ids:
                                t = db.fetchone("SELECT * FROM tests WHERE id=?", (tid,))
                                if t:
                                    tests_obj.append(dict(t))
                            if tests_obj:
                                if len(tests_obj) == 1:
                                    # один тест — анонс "уже идёт"
                                    test = tests_obj[0]
                                    cfg = get_autopub_config()
                                    chan = cfg.get('channel_id')
                                    invite = cfg.get('invite_link') or ''
                                    qc = db.fetchone(
                                        "SELECT COUNT(*) AS c FROM questions WHERE test_id=?",
                                        (test['id'],))['c']
                                    if chan:
                                        try:
                                            await bot.send_message(
                                                int(chan),
                                                announce_now_text(0, test['title'], qc, invite),
                                                parse_mode="HTML",
                                                disable_web_page_preview=False)
                                            posted_full_announce = True
                                        except Exception as e:
                                            log.warning("now announce: %s", e)
                                else:
                                    ok = await announce_batch_reminder(bot, tests_obj)
                                    if ok:
                                        posted_full_announce = True
                        except Exception as e:
                            log.warning("series head reminder: %s", e)
                    elif series_pos > 0:
                        # промежуточный — короткий анонс «через 20 сек»
                        test = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))
                        if test:
                            try:
                                await announce_single_reminder(bot, dict(test))
                            except Exception:
                                pass

                    # Пауза чтобы юзеры успели зайти в чат
                    if posted_full_announce:
                        await asyncio.sleep(15)
                    elif series_pos > 0:
                        await asyncio.sleep(20)

                    # При запуске ПЕРВОГО теста серии — закрыть чат
                    if series_pos == 0:
                        cfg = get_autopub_config()
                        chat_id = cfg.get('chat_id')
                        if chat_id:
                            try:
                                await _lock_chat(bot, int(chat_id))
                            except Exception as e:
                                log.warning("lock on first: %s", e)

                    # 2) Запуск лобби
                    ok = await publish_test_to_chat(bot, test_id)
                    if ok:
                        db.execute("UPDATE autopub_queue SET status='done' WHERE id=?",
                                    (qid,))
                        # При запуске ПОСЛЕДНЕГО теста серии — запланировать unlock
                        if series_pos == series_total - 1:
                            # Длительность теста + запас на лобби и финиш
                            test = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))
                            qcount = db.fetchone(
                                "SELECT COUNT(*) AS c FROM questions WHERE test_id=?",
                                (test_id,))['c']
                            tpq = (test.get('time_per_question') if test else 30) or 30
                            duration_sec = qcount * tpq + 120  # +2 мин на лобби/финиш
                            cfg = get_autopub_config()
                            chat_id = cfg.get('chat_id')
                            if chat_id:
                                asyncio.create_task(
                                    _delayed_unlock(bot, int(chat_id), duration_sec))
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


async def _delayed_unlock(bot: Bot, chat_id: int, delay_sec: int):
    """Отложенно открыть чат после окончания серии."""
    try:
        await asyncio.sleep(delay_sec)
        await _unlock_chat(bot, chat_id)
    except asyncio.CancelledError:
        return
    except Exception as e:
        log.warning("delayed unlock: %s", e)


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
