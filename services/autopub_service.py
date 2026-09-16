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


# ===================== СПИСКИ КАНАЛОВ И ЧАТОВ =====================
# Хранятся как JSON-массивы в settings.
#   channels: [{"id": -100..., "title": "..."}]
#   chats:    [{"id": -100..., "title": "...", "invite": "https://t.me/..."}]

S_CHANNELS = "autopub_channels"
S_CHATS = "autopub_chats"


def get_channels() -> list[dict]:
    import json as _json
    raw = _get_setting(S_CHANNELS)
    out = []
    if raw:
        try:
            out = _json.loads(raw)
        except Exception:
            out = []
    # Подмешаем старый одиночный канал если списка ещё нет
    if not out:
        old = _get_setting(S_CHANNEL_ID)
        if old:
            out = [{"id": old, "title": "Канал"}]
    return out


def get_chats() -> list[dict]:
    import json as _json
    raw = _get_setting(S_CHATS)
    out = []
    if raw:
        try:
            out = _json.loads(raw)
        except Exception:
            out = []
    if not out:
        old = _get_setting(S_CHAT_ID)
        if old:
            out = [{"id": old,
                    "title": _get_setting(S_CHAT_TITLE) or "Чат",
                    "invite": _get_setting(S_INVITE_LINK) or ""}]
    return out


def add_channel(channel_id, title: str = ""):
    import json as _json
    chans = get_channels()
    # Не дублируем
    for c in chans:
        if str(c.get('id')) == str(channel_id):
            c['title'] = title or c.get('title') or ''
            _set_setting(S_CHANNELS, _json.dumps(chans, ensure_ascii=False))
            return
    chans.append({"id": str(channel_id), "title": title or "Канал"})
    _set_setting(S_CHANNELS, _json.dumps(chans, ensure_ascii=False))


def remove_channel(channel_id):
    import json as _json
    chans = [c for c in get_channels() if str(c.get('id')) != str(channel_id)]
    _set_setting(S_CHANNELS, _json.dumps(chans, ensure_ascii=False))


def add_chat(chat_id, title: str = "", invite: str = ""):
    import json as _json
    chats = get_chats()
    for c in chats:
        if str(c.get('id')) == str(chat_id):
            c['title'] = title or c.get('title') or ''
            if invite:
                c['invite'] = invite
            _set_setting(S_CHATS, _json.dumps(chats, ensure_ascii=False))
            return
    chats.append({"id": str(chat_id), "title": title or "Чат",
                   "invite": invite or ""})
    _set_setting(S_CHATS, _json.dumps(chats, ensure_ascii=False))


def remove_chat(chat_id):
    import json as _json
    chats = [c for c in get_chats() if str(c.get('id')) != str(chat_id)]
    _set_setting(S_CHATS, _json.dumps(chats, ensure_ascii=False))


def set_chat_invite(chat_id, invite: str):
    import json as _json
    chats = get_chats()
    for c in chats:
        if str(c.get('id')) == str(chat_id):
            c['invite'] = invite
            _set_setting(S_CHATS, _json.dumps(chats, ensure_ascii=False))
            return


def get_chat_by_id(chat_id) -> Optional[dict]:
    for c in get_chats():
        if str(c.get('id')) == str(chat_id):
            return c
    return None


def get_channel_by_id(channel_id) -> Optional[dict]:
    for c in get_channels():
        if str(c.get('id')) == str(channel_id):
            return c
    return None


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

async def publish_test_to_chat(bot: Bot, test_id: int,
                                chat_id=None) -> bool:
    """Запустить лобби теста в чате. chat_id явный или берём активную серию/первый."""
    if not chat_id:
        # Скрытых значений по умолчанию нет (v75): чат всегда указывает вызывающий
        log.warning("publish_test_to_chat: чат не указан — тест %s не запущен", test_id)
        return False
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))
    if not test:
        return False
    questions = db.fetchall(
        "SELECT id FROM questions WHERE test_id=?", (test_id,))
    if not questions:
        return False
    from services import group_quiz_service
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
        ok, key, gq_id = await group_quiz_service.start_lobby(
            bot, dict(test), int(chat_id),
            admin_tg_id=0,
            language=test.get('language') or 'ru')
        if not ok:
            log.warning("start_lobby не запустил лобби: %s (chat=%s)", key, chat_id)
            # Сообщим в чат если уже идёт
            if key == "already_running":
                try:
                    await bot.send_message(
                        int(chat_id),
                        "⚠️ В этом чате уже идёт тест. Дождитесь окончания или /stop.")
                except Exception:
                    pass
            return False
        return True
    except Exception as e:
        log.exception("publish_test_to_chat lobby: %s", e)
        # Попробуем сообщить об ошибке в чат
        try:
            await bot.send_message(
                int(chat_id),
                "⚠️ Не смог запустить тест в чате. "
                "Проверьте что бот — администратор чата.")
        except Exception:
            pass
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


async def announce_batch_with_topics(bot: Bot, tests: list[dict],
                                       when_str: str,
                                       channel_id=None,
                                       chat_id=None) -> bool:
    """ПРЕД-анонс СРАЗУ С ТЕМАМИ (когда планируем на будущее)."""
    if not channel_id:
        chans = get_channels()
        channel_id = chans[0]['id'] if chans else get_autopub_config().get('channel_id')
    if not channel_id:
        return False
    # Ссылка — из выбранного чата, иначе из первого
    invite = ''
    if chat_id:
        c = get_chat_by_id(chat_id)
        invite = (c.get('invite') if c else '') or ''
    if not invite:
        chats = get_chats()
        invite = (chats[0].get('invite') if chats else '') or \
                 get_autopub_config().get('invite_link') or ''
    topics = "\n".join(f"• {t['title']}" for t in tests[:10])
    total_q = 0
    for t in tests:
        r = db.fetchone("SELECT COUNT(*) AS c FROM questions WHERE test_id=?",
                         (t['id'],))
        total_q += (r['c'] if r else 0)
    text = (
        f"🔥 <b>СКОРО ТЕСТЫ В ЧАТЕ!</b>\n\n"
        f"📚 <b>Темы ({len(tests)}):</b>\n{topics}\n\n"
        f"⏰ Начинаем: <b>{when_str}</b>\n"
        f"❓ Всего вопросов: <b>{total_q}</b>\n\n"
        f"👇 Заходи в чат заранее, чтобы успеть:\n{invite}"
    )
    try:
        await bot.send_message(int(channel_id), text,
                                 parse_mode="HTML",
                                 disable_web_page_preview=False)
        return True
    except Exception as e:
        log.warning("announce with topics: %s", e)
        return False


# ===================== СОСТОЯНИЕ СЕРИИ =====================
# С v75 серия хранится в базе (см. «СЕРИИ ТЕСТОВ» ниже). Здесь — только сброс
# старых ключей settings и очереди прошлой версии.

def clear_active_series():
    _set_setting("active_series_id", "")
    # Анонс в боте тоже больше не актуален
    try:
        clear_bot_announce()
    except Exception:
        pass


def clear_all_queue():
    """Удалить pending/running записи СТАРОЙ очереди (серии v75 отменяются cancel_series)."""
    try:
        db.execute("DELETE FROM autopub_queue WHERE status IN ('pending','running')")
    except Exception as e:
        log.warning("clear_all_queue: %s", e)


async def _finish_series_open_chat(bot: Bot, chat_id: int):
    """Открыть чат и поздравить после последнего теста серии."""
    try:
        await _unlock_chat_congrats(bot, chat_id)
    except Exception as e:
        log.warning("finish series open: %s", e)


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


async def announce_batch_reminder(bot: Bot, tests: list[dict],
                                    channel_id=None, invite='') -> bool:
    """Краткое напоминание когда время подошло — темы + ссылка."""
    if not channel_id:
        chans = get_channels()
        channel_id = chans[0]['id'] if chans else get_autopub_config().get('channel_id')
    if not channel_id:
        return False
    if not invite:
        chats = get_chats()
        invite = (chats[0].get('invite') if chats else '') or \
                 get_autopub_config().get('invite_link') or ''
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
    """Закрыть чат — только админы пишут. True, если права сменились (даже если
    уведомление потом не ушло: иначе чат остался бы закрытым навсегда)."""
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
    except Exception as e:
        log.warning("lock chat failed: %s", e)
        return False
    try:
        await bot.send_message(
            chat_id,
            "🔒 <b>Чат закрыт на время тестов</b>\n\n"
            "Писать могут только админы.\n"
            "После окончания серии тестов чат откроется автоматически.",
            parse_mode="HTML")
    except Exception as e:
        log.warning("lock chat notice: %s", e)
    return True


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


async def _unlock_chat_congrats(bot: Bot, chat_id: int) -> bool:
    """Открыть чат после ВСЕЙ серии + большое поздравление."""
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
    except Exception as e:
        log.warning("unlock congrats perms: %s", e)
    try:
        await bot.send_message(
            chat_id,
            "🎉 <b>ВСЕ ТЕСТЫ ПРОЙДЕНЫ!</b>\n\n"
            "Вы большие молодцы! 💪\n"
            "Каждый тест — это шаг к высокому баллу на ЕНТ.\n\n"
            "Надеюсь, вы получите <b>140/140</b>! 🏆\n\n"
            "🔓 Чат снова открыт — общайтесь, обсуждайте вопросы.\n"
            "До новых тестов! 🚀",
            parse_mode="HTML")
        return True
    except Exception as e:
        log.warning("unlock congrats msg: %s", e)
        return False


async def announce_single_reminder(bot: Bot, test: dict) -> bool:
    """Короткое напоминание про следующий тест — в ЧАТЕ серии."""
    st = get_active_series()
    chat_id = (st.get('chat_id') if st else None)
    if not chat_id:
        return False
    text = (
        f"⏳ <b>Через 20 сек — новый тест!</b>\n\n"
        f"📚 <b>{test['title']}</b>\n\n"
        f"Готовься! 🚀"
    )
    try:
        await bot.send_message(int(chat_id), text, parse_mode="HTML")
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

# ===================== СЕРИИ ТЕСТОВ (v75) =====================
# «🚀 Запустить серию тестов». Серия хранится в базе (autopub_series,
# autopub_series_items, журнал autopub_series_events) и переживает перезапуск.
# Два РАЗНЫХ места публикации:
#   announcement_channel_id — только анонсы и информационные сообщения;
#   test_chat_id            — лобби, вопросы (Quiz/Poll) и вся серия.
# Скрытых значений по умолчанию нет: канал (или «без анонса») и чат админ
# выбирает явно, серия без чата не создаётся. Тесты идут строго по
# order_index; следующий — только после ПОЛНОГО окончания предыдущего и паузы
# 20–30 сек (next_run_at в базе, а не таймер в памяти).
#
# До v75 серия жила в settings («активная серия» одна на весь бот) и цепочкой
# таймеров в памяти: ЛЮБОЙ завершившийся групповой тест в любом чате двигал её
# дальше — серия на 20:00 начиналась раньше; ручной ввод минут пропускал выбор
# чата и канала; при одном чате/канале они выбирались молча; порядок тестов
# перемешивался; перезапуск бота обрывал цепочку.

import html as _html
import json as _json
import random as _random
import re as _re
from datetime import timezone as _timezone

ALMATY = _timezone(timedelta(hours=5))
SERIES_STATUS_TITLES = {
    "draft": "📝 черновик", "scheduled": "🕒 запланирована", "running": "▶️ идёт",
    "waiting_next": "⏳ пауза перед следующим тестом", "completed": "✅ завершена",
    "cancelled": "🚫 отменена", "failed": "❌ сбой",
}
ITEM_STATUS_TITLES = {"pending": "ждёт", "launching": "запускается", "running": "идёт", "done": "пройден",
                      "skipped": "пропущен", "failed": "ошибка", "cancelled": "отменён"}
ACTIVE_SERIES = ("scheduled", "running", "waiting_next")
INTERVAL_MIN, INTERVAL_MAX = 20, 30        # пауза после окончания теста, сек
MAX_LATE_MINUTES = 180                     # бот был выключен дольше — серия не догоняется
CHAT_BUSY_WAIT_MINUTES = 30                # в чате чужой тест — ждём не дольше
BUSY_RETRY_SECONDS = 30
LAUNCH_STUCK_SECONDS = 180
SERIES_QUEUE_WAIT_HOURS = 6                # ждём, пока в чате закончится другая серия
LAUNCH_RETRIES = 3                         # сбой связи с Telegram при запуске — столько попыток
SECONDS_RANGE = (5, 86400)
MINUTES_RANGE = (1, 10080)
MAX_AHEAD_DAYS = 60
MAX_SERIES_TESTS = 30
_ONCE_EVENTS = ("SERIES_STARTED", "ITEM_LAUNCHED", "ITEM_FINISHED", "SERIES_COMPLETED")


def _now_utc() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0, tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")


def _parse_utc(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T")[:19])
    except ValueError:
        return None


def to_local(dt_utc: datetime) -> datetime:
    return dt_utc.replace(tzinfo=_timezone.utc).astimezone(ALMATY)


def fmt_local(value, seconds: bool = False) -> str:
    dt = _parse_utc(value)
    if not dt:
        return "—"
    return to_local(dt).strftime("%d.%m.%Y %H:%M:%S" if seconds else "%d.%m.%Y %H:%M")


def _esc(text) -> str:
    return _html.escape(str(text or ""), quote=False)


# ── время запуска ──

def parse_hhmm(text) -> Optional[tuple]:
    m = _re.fullmatch(r"\s*(\d{1,2})\s*[:.\s]\s*(\d{2})\s*", text or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return (h, mi) if 0 <= h <= 23 and 0 <= mi <= 59 else None


def parse_local_datetime(text, now_utc: datetime = None) -> Optional[datetime]:
    """«15.09.2026 20:00», «15.09 20:00», «2026-09-15 20:00» → время Астаны без зоны."""
    t = _re.sub(r"\s+", " ", (text or "").replace(",", " ").strip())
    m = _re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))? (\d{1,2})[:.](\d{2})", t)
    if m:
        d, mo, y, h, mi = m.groups()
        y = int(y) if y else to_local(now_utc or _now_utc()).year
        y = y + 2000 if y < 100 else y
    else:
        m = _re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})", t)
        if not m:
            return None
        y, mo, d, h, mi = m.groups()
    try:
        return datetime(int(y), int(mo), int(d), int(h), int(mi))
    except ValueError:
        return None


def exact_time_value(hhmm, now_utc: datetime = None) -> Optional[str]:
    """HH:MM → ближайшее такое время по Астане: сегодня, если ещё не прошло, иначе завтра.
    Дата фиксируется сразу — и видна админу на подтверждении."""
    hm = parse_hhmm(hhmm)
    if not hm:
        return None
    now_l = to_local(now_utc or _now_utc()).replace(tzinfo=None)
    cand = now_l.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    if cand <= now_l:
        cand += timedelta(days=1)
    return cand.strftime("%Y-%m-%dT%H:%M:%S")


def resolve_launch(mode, value, now_utc: datetime = None) -> tuple:
    """(время старта UTC, задержка в секундах, ошибка).
    «Через N секунд/минут» считается от кнопки «Запланировать», а не от
    выбора способа. Точное время хранится временем Астаны (UTC+5)."""
    now = now_utc or _now_utc()
    if mode == "now":
        return now, 0, None
    if mode in ("seconds", "minutes"):
        try:
            n = int(value)
        except (TypeError, ValueError):
            return None, 0, "Укажите число."
        lo, hi = SECONDS_RANGE if mode == "seconds" else MINUTES_RANGE
        if not lo <= n <= hi:
            return None, 0, f"Допустимо от {lo} до {hi} {'секунд' if mode == 'seconds' else 'минут'}."
        sec = n if mode == "seconds" else n * 60
        return now + timedelta(seconds=sec), sec, None
    if mode in ("exact_time", "datetime"):
        local = _parse_utc(value)
        if not local:
            return None, 0, "Укажите дату и время запуска."
        utc = local - timedelta(hours=5)
        if utc < now - timedelta(minutes=5):
            return None, 0, f"Время {local.strftime('%d.%m.%Y %H:%M')} уже прошло — укажите новое."
        if utc > now + timedelta(days=MAX_AHEAD_DAYS):
            return None, 0, f"Не дальше чем на {MAX_AHEAD_DAYS} дней вперёд."
        return utc, max(0, int((utc - now).total_seconds())), None
    return None, 0, "Выберите способ запуска."


def launch_title(mode, value=None) -> str:
    return {"now": "Сразу после подтверждения", "seconds": f"Через {value} секунд",
            "minutes": f"Через {value} минут", "exact_time": "По точному времени",
            "datetime": "По точному времени", "legacy": "Перенесена из прошлой версии"}.get(mode, mode or "—")


def launch_lines(mode, value, now_utc: datetime = None) -> list:
    """Строки «Способ запуска / Дата / Время» для подтверждения."""
    lines = [f"Способ запуска: <b>{_esc(launch_title(mode, value))}</b>"]
    at, _d, err = resolve_launch(mode, value, now_utc)
    if err or not at:
        return lines
    loc = to_local(at)
    if mode in ("exact_time", "datetime"):
        lines += [f"Дата: <b>{loc:%d.%m.%Y}</b>", f"Время: <b>{loc:%H:%M}</b> (Астана, UTC+5)"]
    elif mode == "seconds":
        lines.append(f"Старт: примерно в <b>{loc:%H:%M:%S}</b> — отсчёт от кнопки «Запланировать»")
    elif mode == "minutes":
        lines.append(f"Старт: примерно в <b>{loc:%d.%m %H:%M}</b> — отсчёт от кнопки «Запланировать»")
    return lines


def when_words(s: dict, now_utc: datetime = None) -> str:
    now = now_utc or _now_utc()
    at = _parse_utc(s["scheduled_at"]) or now
    delta = (at - now).total_seconds()
    if delta < 60:
        return f"через {max(1, int(delta))} сек"
    loc, today = to_local(at), to_local(now).date()
    day = ("сегодня" if loc.date() == today else "завтра" if loc.date() == today + timedelta(days=1)
           else loc.strftime("%d.%m.%Y"))
    return f"{day} в {loc:%H:%M} (время Астаны)"


# ── права бота ──

def _transient(e) -> bool:
    """Временный сбой связи с Telegram (повторить), а не отказ по правам."""
    return type(e).__name__ in ("TelegramNetworkError", "TelegramServerError", "TelegramRetryAfter",
                                "RestartingTelegram", "TimeoutError", "ClientConnectionError", "ClientOSError",
                                "ServerDisconnectedError") or isinstance(e, asyncio.TimeoutError)


def name_permanent(e) -> bool:
    return type(e).__name__.startswith("Telegram") and not _transient(e)


def _st(member) -> str:
    v = getattr(member, "status", "")
    return str(getattr(v, "value", v) or "")


async def check_destination(bot: Bot, chat_id, kind: str) -> dict:
    """Права бота ДО создания серии. kind: 'channel' (анонс) | 'chat' (тесты)."""
    res = {"ok": True, "errors": [], "warnings": [], "title": ""}
    what = "канал" if kind == "channel" else "чат"
    try:
        me = await bot.get_me()
        chat = await bot.get_chat(int(chat_id))
        member = await bot.get_chat_member(int(chat_id), me.id)
    except (ValueError, TypeError) as e:
        res.update(ok=False, errors=[f"Неверный ID: {chat_id}."])
        return res
    except Exception as e:
        if name_permanent(e):
            res.update(ok=False, errors=[f"Бот не видит выбранный {what}: он удалён, ID неверный или бот "
                                         f"не добавлен ({str(e)[:90]})."])
        else:
            # Не проверили — не пропускаем: без проверки серию не создаём
            res.update(ok=False, errors=[f"Не удалось проверить права бота в {what}е ({str(e)[:90]}). "
                                         f"Нажмите «Проверить ещё раз»."])
        return res
    res["title"] = getattr(chat, "title", "") or ""
    ctype = str(getattr(getattr(chat, "type", ""), "value", getattr(chat, "type", "")))
    status = _st(member)
    if kind == "channel":
        if ctype != "channel":
            res["warnings"].append("Выбранный «канал» — группа, а не канал: анонс уйдёт туда обычным сообщением.")
        if status in ("left", "kicked"):
            res["errors"].append("Бот не добавлен в выбранный канал.")
        elif status == "administrator" and getattr(member, "can_post_messages", None) is False:
            res["errors"].append("Бот не имеет права публиковать сообщения в выбранном канале.")
        elif ctype == "channel" and status not in ("creator", "administrator"):
            res["errors"].append("Бот не имеет права публиковать сообщения в выбранном канале: сделайте его "
                                 "администратором с правом «Публикация сообщений».")
        elif status == "restricted" and getattr(member, "can_send_messages", True) is False:
            res["errors"].append("Бот не имеет права публиковать сообщения в выбранном канале.")
        elif ctype in ("group", "supergroup") and status == "member" and \
                getattr(getattr(chat, "permissions", None), "can_send_messages", True) is False:
            res["errors"].append("Бот не может публиковать анонс в выбранной группе: участникам запрещено писать, "
                                 "а бот не администратор.")
    else:
        perms = getattr(chat, "permissions", None)
        if ctype not in ("group", "supergroup"):
            res["errors"].append("Выбранный чат — не группа: тесты-викторины запускаются только в группе.")
        elif status in ("left", "kicked"):
            res["errors"].append("Бот не состоит в выбранной группе — добавьте его в группу.")
        elif status == "restricted":
            if getattr(member, "can_send_messages", True) is False:
                res["errors"].append("Бот не может отправлять тесты в выбранный чат: ему запрещено писать.")
            if getattr(member, "can_send_polls", True) is False:
                res["errors"].append("Бот не может создавать викторины (Quiz) в выбранном чате.")
        elif status == "member" and perms is not None:
            if getattr(perms, "can_send_messages", True) is False:
                res["errors"].append("Бот не может отправлять тесты в выбранный чат: участникам запрещено писать, "
                                     "а бот не администратор.")
            if getattr(perms, "can_send_polls", True) is False:
                res["errors"].append("Бот не может создавать викторины (Quiz) в выбранном чате: опросы запрещены, "
                                     "а бот не администратор.")
        if not res["errors"] and (status in ("member", "restricted") or
                                  (status == "administrator" and getattr(member, "can_restrict_members", True) is False)):
            res["warnings"].append("Бот не сможет закрыть чат на время тестов (нужно право администратора "
                                   "«Блокировка участников»). Тесты всё равно пройдут.")
    res["ok"] = not res["errors"]
    return res


# ── серия в базе ──

def _event(sid, event: str, position: int = -1, test_id=None, status=None, details: str = "",
           now: datetime = None) -> bool:
    cur = db.execute(("INSERT OR IGNORE" if event in _ONCE_EVENTS else "INSERT") +
                     " INTO autopub_series_events (series_id, event, position, test_id, status, details, created_at) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (sid, event, position, test_id, status, (details or "")[:500], _iso(now or _now_utc())))
    if cur.rowcount:
        log.info("%s series_id=%s position=%s test_id=%s status=%s %s", event, sid, position, test_id, status, details)
    return bool(cur.rowcount)


def get_series(sid) -> Optional[dict]:
    r = db.fetchone("SELECT * FROM autopub_series WHERE id=?", (int(sid),))
    if not r:
        return None
    s = dict(r)
    s["items"] = [dict(x) for x in db.fetchall(
        "SELECT * FROM autopub_series_items WHERE series_id=? ORDER BY order_index", (s["id"],))]
    try:
        s["source_titles"] = _json.loads(s.get("source_titles") or "[]")
    except ValueError:
        s["source_titles"] = []
    return s


def list_series(statuses=None, limit: int = 20) -> list:
    sql, args = "SELECT id FROM autopub_series", []
    if statuses:
        sql += f" WHERE status IN ({','.join('?' * len(statuses))})"
        args += list(statuses)
    return [get_series(r["id"]) for r in db.fetchall(sql + " ORDER BY id DESC LIMIT ?", tuple(args) + (limit,))]


def series_events(sid, limit: int = 40) -> list:
    return [dict(r) for r in db.fetchall("SELECT * FROM autopub_series_events WHERE series_id=? ORDER BY id DESC LIMIT ?",
                                         (int(sid), limit))]


def active_series_in_chat(chat_id) -> Optional[dict]:
    r = db.fetchone(f"SELECT id FROM autopub_series WHERE test_chat_id=? AND status IN "
                    f"({','.join('?' * len(ACTIVE_SERIES))}) ORDER BY scheduled_at LIMIT 1",
                    (str(chat_id), *ACTIVE_SERIES))
    return get_series(r["id"]) if r else None


def create_series(*, tests: list, mode: str, template_id: int, launch_mode: str, launch_value,
                  channel_choice, test_chat_id, bot_announce: bool, created_by: int,
                  op_key: str = None, now: datetime = None) -> dict:
    """Запланировать серию. Без явно выбранного чата и канала (или «без анонса») —
    отказ. Порядок тестов — ровно как передан (order_index)."""
    now = now or _now_utc()
    tests = [int(t) for t in (tests or [])]
    errors = []
    if not tests:
        errors.append("Выберите хотя бы один тест.")
    if len(tests) > MAX_SERIES_TESTS:
        errors.append(f"В серии не больше {MAX_SERIES_TESTS} тестов.")
    if not test_chat_id:
        errors.append("Выберите чат, в котором будут запускаться тесты.")
    elif not get_chat_by_id(test_chat_id):
        errors.append("Выбранного чата нет в списке чатов — выберите чат заново.")
    if channel_choice in (None, ""):
        errors.append("Выберите канал для публикации анонса.")
    elif str(channel_choice) != "none" and not get_channel_by_id(channel_choice):
        errors.append("Выбранного канала нет в списке каналов — выберите канал заново.")
    at, delay, err = resolve_launch(launch_mode, launch_value, now)
    if err:
        errors.append(err)
    if errors:
        return {"ok": False, "errors": errors}
    if op_key:
        dup = db.fetchone("SELECT id FROM autopub_series WHERE op_key=?", (op_key,))
        if dup:
            return {"ok": True, "duplicate": True, "series": get_series(dup["id"])}
    titles = []
    for tid in tests:
        t = db.fetchone("SELECT title FROM tests WHERE id=?", (tid,))
        if not t:
            return {"ok": False, "errors": [f"Тест #{tid} не найден — выберите тесты заново."]}
        titles.append(t["title"])
    items = list(zip(tests, titles))
    if mode == "mix" and len(tests) >= 2:
        mix_id = create_mixed_test(tests, created_by, total=10, language="ru")
        if not mix_id:
            return {"ok": False, "errors": ["Не получилось собрать микс: у выбранных тестов нет вопросов."]}
        mt = db.fetchone("SELECT title FROM tests WHERE id=?", (mix_id,))
        items = [(mix_id, mt["title"] if mt else "Микс")]
    chan = None if str(channel_choice) == "none" else str(channel_choice)
    chat_row, chan_row = get_chat_by_id(test_chat_id) or {}, (get_channel_by_id(chan) or {}) if chan else {}
    cur = db.execute(
        "INSERT OR IGNORE INTO autopub_series (status, launch_mode, delay_seconds, scheduled_at, "
        "announcement_channel_id, announcement_channel_title, test_chat_id, test_chat_title, mode, template_id, "
        "bot_announce, source_titles, op_key, created_by, created_at) VALUES ('draft',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (launch_mode, delay, _iso(at), chan, chan_row.get("title") if chan else None, str(test_chat_id),
         chat_row.get("title") or str(test_chat_id), mode if mode in ("full", "mix") else "full",
         int(template_id or 0), 1 if bot_announce else 0, _json.dumps(titles, ensure_ascii=False),
         op_key, created_by, _iso(now)))
    if not cur.rowcount:                          # тот же op_key уже записан параллельным нажатием
        dup = db.fetchone("SELECT id FROM autopub_series WHERE op_key=?", (op_key,))
        return {"ok": True, "duplicate": True, "series": get_series(dup["id"]) if dup else None}
    sid = cur.lastrowid
    db.executemany("INSERT INTO autopub_series_items (series_id, order_index, test_id, title) VALUES (?,?,?,?)",
                   [(sid, i, tid, title) for i, (tid, title) in enumerate(items)])
    db.execute("UPDATE autopub_series SET status='scheduled' WHERE id=? AND status='draft'", (sid,))
    log.info("SERIES_CREATED series_id=%s announcement_channel_id=%s test_chat_id=%s launch_mode=%s "
             "delay_seconds=%s scheduled_at=%s tests=%s", sid, chan or "none", test_chat_id, launch_mode, delay,
             to_local(at).isoformat(), ",".join(str(t) for t, _ in items))
    _event(sid, "SERIES_CREATED", details=f"announcement_channel_id={chan or 'none'} test_chat_id={test_chat_id} "
                                          f"launch_mode={launch_mode} delay_seconds={delay} "
                                          f"scheduled_at={to_local(at).isoformat()}", now=now)
    return {"ok": True, "series": get_series(sid)}


def _release_bot_announce(sid) -> None:
    r = db.fetchone("SELECT bot_announce FROM autopub_series WHERE id=?", (int(sid),))
    if not r or not r["bot_announce"]:
        return
    other = db.fetchone(f"SELECT id FROM autopub_series WHERE id<>? AND bot_announce=1 AND status IN "
                        f"({','.join('?' * len(ACTIVE_SERIES))}) LIMIT 1", (int(sid), *ACTIVE_SERIES))
    if not other:
        clear_bot_announce()


def cancel_series_in_chat(chat_id, reason: str) -> list:
    """/stop в группе: отменить идущие серии этого чата. Возвращает снимки ДО отмены."""
    out = []
    for r in db.fetchall("SELECT id FROM autopub_series WHERE test_chat_id=? AND status IN ('running','waiting_next')",
                         (str(chat_id),)):
        before = get_series(r["id"])
        if cancel_series(r["id"], reason):
            out.append(before)
    return out


def cancel_series(sid, reason: str = "отменена администратором", now: datetime = None) -> Optional[dict]:
    now = now or _now_utc()
    cur = db.execute("UPDATE autopub_series SET status='cancelled', error=?, finished_at=?, next_run_at=NULL "
                     "WHERE id=? AND status IN ('draft','scheduled','running','waiting_next')",
                     (reason[:300], _iso(now), int(sid)))
    if not cur.rowcount:
        return None
    db.execute("UPDATE autopub_series_items SET status='cancelled' WHERE series_id=? AND status IN ('pending','launching')",
               (int(sid),))
    _event(int(sid), "SERIES_CANCELLED", status="cancelled", details=reason, now=now)
    _release_bot_announce(sid)
    return get_series(sid)


# ── тексты анонсов: ссылка — только чата ЭТОЙ серии ──

def _invite_of(s: dict) -> str:
    return ((get_chat_by_id(s["test_chat_id"]) or {}).get("invite") or "").strip()


def _titles_of(s: dict) -> list:
    return s.get("source_titles") or [i["title"] for i in s.get("items") or []]


async def announce_series_planned(bot: Bot, s: dict, now: datetime = None) -> bool:
    """Анонс заранее — ТОЛЬКО в канал анонса. Тесты он не запускает."""
    chan = s.get("announcement_channel_id")
    if not chan:
        return False
    invite, titles, when = _invite_of(s), [_esc(t) for t in _titles_of(s)], when_words(s, now)
    if invite:
        text = build_series_announce_text(s.get("template_id") or 0, titles, when, invite)
    else:
        text = ("🔔 <b>Скоро серия тестов в нашем чате!</b>\n\n📚 Темы ({}):\n{}\n\n⏰ Старт: <b>{}</b>"
                .format(len(titles), "\n".join(f"• <b>{t}</b>" for t in titles), when))
    try:
        await bot.send_message(int(chan), text, parse_mode="HTML", disable_web_page_preview=False)
    except Exception as e:
        _event(s["id"], "ANNOUNCE_FAILED", details=f"announcement_channel_id={chan} {str(e)[:200]}", now=now)
        return False
    _event(s["id"], "PRE_ANNOUNCED", details=f"announcement_channel_id={chan}", now=now)
    return True


async def _announce_start(bot: Bot, s: dict, now: datetime) -> None:
    chan = s.get("announcement_channel_id")
    if not chan:
        return
    invite, titles = _invite_of(s), [_esc(t) for t in _titles_of(s)]
    text = (f"🟢 <b>СЕРИЯ ТЕСТОВ НАЧАЛАСЬ!</b>\n\n📚 Темы ({len(titles)}):\n"
            + "\n".join(f"• <b>{t}</b>" for t in titles)
            + (f"\n\n⚡️ Заходи в чат и участвуй:\n{invite}" if invite else "\n\n⚡️ Тесты уже идут в нашем чате — заходи!"))
    try:
        await bot.send_message(int(chan), text, parse_mode="HTML", disable_web_page_preview=False)
        _event(s["id"], "ANNOUNCE_START", details=f"announcement_channel_id={chan}", now=now)
    except Exception as e:
        _event(s["id"], "ANNOUNCE_FAILED", details=f"announcement_channel_id={chan} {str(e)[:200]}", now=now)


# ── исполнение ──

def _next_sleep() -> float:
    """До ближайшего старта — но не дольше 5 сек: тест начинается вовремя, не раньше."""
    try:
        r = db.fetchone("SELECT MIN(t) AS t FROM (SELECT scheduled_at AS t FROM autopub_series WHERE status='scheduled' "
                        "UNION ALL SELECT next_run_at FROM autopub_series WHERE status='waiting_next')")
    except Exception:
        return 5.0
    at = _parse_utc(r["t"]) if r else None
    if not at:
        return 5.0
    return max(0.3, min(5.0, (at - datetime.utcnow()).total_seconds()))


async def series_tick(bot: Bot, now: datetime = None) -> list:
    """Один проход: присмотр за идущими и запуск тех, чьё время пришло.
    Каждый переход — условным UPDATE: второй воркер, двойной вызов или
    повтор после перезапуска тот же тест второй раз не отправят."""
    now = now or _now_utc()
    await _watchdog(bot, now)
    started = []
    rows = db.fetchall("SELECT id, status FROM autopub_series WHERE (status='scheduled' AND scheduled_at<=?) "
                       "OR (status='waiting_next' AND next_run_at<=?) ORDER BY COALESCE(next_run_at, scheduled_at), id",
                       (_iso(now), _iso(now)))
    for r in rows:
        try:
            if await _start_due(bot, r["id"], r["status"], now):
                started.append(r["id"])
        except Exception as e:
            log.exception("серия %s: запуск: %s", r["id"], e)
    return started


async def _start_due(bot: Bot, sid: int, status: str, now: datetime) -> bool:
    s = get_series(sid)
    if not s:
        return False
    if status == "scheduled":
        at = _parse_utc(s["scheduled_at"])
        if at and now - at > timedelta(minutes=MAX_LATE_MINUTES):
            await _fail(bot, s, f"Серия пропущена: в {fmt_local(at)} бот был выключен и включился через "
                                f"{int((now - at).total_seconds() // 60)} мин.", now)
            return False
        cur = db.execute("UPDATE autopub_series SET status='running', started_at=?, error='' "
                         "WHERE id=? AND status='scheduled' AND scheduled_at<=?", (_iso(now), sid, _iso(now)))
        if not cur.rowcount:
            return False
        _event(sid, "SERIES_STARTED", details=f"scheduled_at={to_local(at).isoformat() if at else '—'} "
                                              f"actual_start_at={to_local(now).isoformat()} "
                                              f"test_chat_id={s['test_chat_id']}", now=now)
    else:
        cur = db.execute("UPDATE autopub_series SET status='running', next_run_at=NULL "
                         "WHERE id=? AND status='waiting_next' AND next_run_at<=?", (sid, _iso(now)))
        if not cur.rowcount:
            return False
    return await _launch_current(bot, sid, now)


async def _launch_current(bot: Bot, sid: int, now: datetime) -> bool:
    s = get_series(sid)
    if not s or s["status"] != "running":
        return False
    item = next((i for i in s["items"] if i["status"] == "pending"), None)
    if item is None:
        await _complete(bot, s, now)
        return False
    chat = s["test_chat_id"]
    blocker = _blocking_series(s)
    if blocker:
        return await _wait_series_queue(bot, sid, now, blocker)
    from services import auto_schedule_service as _ass
    if _ass._chat_busy(chat):
        return await _wait_busy(bot, sid, now, "в чате идёт другой тест — ждём его окончания")
    cur = db.execute("UPDATE autopub_series_items SET status='launching', launched_at=?, "
                     "launch_attempts=launch_attempts+1 WHERE id=? AND status='pending'", (_iso(now), item["id"]))
    if not cur.rowcount:
        return False
    db.execute("UPDATE autopub_series SET current_pos=?, busy_since=NULL, error='' WHERE id=?", (item["order_index"], sid))
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (item["test_id"],))
    qn = db.fetchone("SELECT COUNT(*) AS c FROM questions WHERE test_id=?", (item["test_id"],))["c"] if test else 0
    if not test or not qn:
        why = "тест удалён" if not test else "в тесте нет вопросов"
        db.execute("UPDATE autopub_series_items SET status='failed', error=?, finished_at=? WHERE id=?",
                   (why, _iso(now), item["id"]))
        _event(sid, "ITEM_FAILED", item["order_index"], item["test_id"], "failed", why, now)
        return await _after_item(bot, sid, now, wait=0)
    # Перед первым тестом — анонс «Начинаем» ТОЛЬКО в канал анонса и закрыть чат тестов (один раз)
    if db.execute("UPDATE autopub_series SET start_announced_at=? WHERE id=? AND start_announced_at IS NULL",
                  (_iso(now), sid)).rowcount:
        await _announce_start(bot, s, now)
        try:
            if await _lock_chat(bot, int(chat)):
                db.execute("UPDATE autopub_series SET chat_locked=1 WHERE id=?", (sid,))
        except Exception as e:
            log.warning("серия %s: закрыть чат: %s", sid, e)
    if (get_series(sid) or {}).get("status") != "running":        # отменили, пока шёл анонс
        await _unlock_if_locked(bot, sid)
        return False
    from services import group_quiz_service as gqs
    try:
        ok, key, gq_id = await gqs.start_lobby(bot, dict(test), int(chat), admin_tg_id=0,
                                               language=test.get("language") or "ru")
    except Exception as e:
        if _transient(e) and (item.get("launch_attempts") or 0) + 1 < LAUNCH_RETRIES:
            why = f"сбой связи с Telegram — повтор через 15 сек ({str(e)[:120]})"
            db.execute("UPDATE autopub_series_items SET status='pending' WHERE id=? AND status='launching'", (item["id"],))
            db.execute("UPDATE autopub_series SET status='waiting_next', next_run_at=?, error=? WHERE id=? AND status='running'",
                       (_iso(now + timedelta(seconds=15)), why, sid))
            _event(sid, "LAUNCH_RETRY", item["order_index"], item["test_id"], "pending", why, now)
            return False
        ok, key, gq_id = False, f"{type(e).__name__}: {str(e)[:150]}", None
    if ok:
        cur = db.execute("UPDATE autopub_series_items SET status='running', group_quiz_id=? WHERE id=? AND status='launching'",
                         (gq_id, item["id"]))
        if not cur.rowcount:
            # Серию отменили, пока создавалось лобби: убрать его и открыть чат
            try:
                await gqs.stop_quiz(bot, int(chat), 0)
            except Exception as e:
                log.warning("серия %s: убрать лобби отменённой серии: %s", sid, e)
            await _unlock_if_locked(bot, sid)
            _event(sid, "ITEM_ABANDONED", item["order_index"], item["test_id"], "cancelled",
                   f"серия отменена во время запуска, лобби #{gq_id} убрано", now)
            return False
        _event(sid, "ITEM_LAUNCHED", item["order_index"], item["test_id"], "running",
               f"test_chat_id={chat} group_quiz_id={gq_id}", now)
        return True
    db.execute("UPDATE autopub_series_items SET status='pending' WHERE id=? AND status='launching'", (item["id"],))
    if key == "already_running":
        return await _wait_busy(bot, sid, now, "в чате идёт другой тест — ждём его окончания")
    await _fail(bot, get_series(sid), f"Бот не может запустить тест в выбранном чате: {str(key)[:200]}", now)
    return False


async def _wait_busy(bot: Bot, sid: int, now: datetime, why: str) -> bool:
    s = get_series(sid)
    since = _parse_utc(s.get("busy_since")) or now
    if now - since > timedelta(minutes=CHAT_BUSY_WAIT_MINUTES):
        await _fail(bot, s, f"Чат тестов занят другим тестом дольше {CHAT_BUSY_WAIT_MINUTES} мин — серия остановлена.", now)
        return False
    db.execute("UPDATE autopub_series SET status='waiting_next', next_run_at=?, busy_since=?, error=? "
               "WHERE id=? AND status='running'", (_iso(now + timedelta(seconds=BUSY_RETRY_SECONDS)), _iso(since), why, sid))
    if s.get("error") != why:
        _event(sid, "WAIT_CHAT_BUSY", s["current_pos"], details=why, now=now)
    return False


def _blocking_series(s: dict) -> Optional[dict]:
    """Другая серия в этом же чате, начавшаяся раньше и ещё не закончившаяся.
    Ждём её целиком, а не вклиниваемся в её паузу между тестами."""
    mine = s.get("started_at") or "9999"
    r = db.fetchone("SELECT id FROM autopub_series WHERE test_chat_id=? AND id<>? AND status IN ('running','waiting_next') "
                    "AND started_at IS NOT NULL AND (started_at<? OR (started_at=? AND id<?)) ORDER BY started_at, id LIMIT 1",
                    (s["test_chat_id"], s["id"], mine, mine, s["id"]))
    return dict(r) if r else None


async def _wait_series_queue(bot: Bot, sid: int, now: datetime, blocker: dict) -> bool:
    s = get_series(sid)
    started = _parse_utc(s.get("started_at")) or now
    if now - started > timedelta(hours=SERIES_QUEUE_WAIT_HOURS):
        await _fail(bot, s, f"Чат тестов дольше {SERIES_QUEUE_WAIT_HOURS} ч занят серией #{blocker['id']} — "
                            f"серия остановлена.", now)
        return False
    why = f"ждём окончания серии #{blocker['id']} в этом чате"
    db.execute("UPDATE autopub_series SET status='waiting_next', next_run_at=?, error=? WHERE id=? AND status='running'",
               (_iso(now + timedelta(seconds=BUSY_RETRY_SECONDS)), why, sid))
    if s.get("error") != why:
        _event(sid, "WAIT_SERIES", s["current_pos"], details=why, now=now)
    return False


async def _unlock_if_locked(bot: Bot, sid: int) -> None:
    s = get_series(sid)
    if s and s.get("chat_locked"):
        try:
            await _unlock_chat(bot, int(s["test_chat_id"]))
        except Exception as e:
            log.warning("серия %s: открыть чат: %s", sid, e)


async def on_series_test_finished(bot: Bot, test_id: int, chat_id: int, gq_id: int = None,
                                  cancelled: bool = False) -> bool:
    """Групповой тест закончился (лидерборд уже отправлен). Двигаем ТОЛЬКО ту
    серию, которой принадлежит этот тест — по id группового теста. Чужие тесты
    (автозапуск, ручной запуск, другая серия) серию больше не двигают: из-за
    этого серия раньше стартовала до своего времени."""
    now = _now_utc()
    if gq_id:
        it = db.fetchone("SELECT * FROM autopub_series_items WHERE group_quiz_id=? AND status='running'", (int(gq_id),))
    else:
        it = db.fetchone("SELECT i.* FROM autopub_series_items i JOIN autopub_series s ON s.id=i.series_id "
                         "WHERE s.status='running' AND i.status='running' AND s.test_chat_id=? AND i.test_id=? "
                         "ORDER BY i.id DESC LIMIT 1", (str(chat_id), int(test_id)))
    if not it:
        return False
    return await _item_finished(bot, dict(it), cancelled, now)


async def _item_finished(bot: Bot, item: dict, cancelled: bool, now: datetime) -> bool:
    new = "skipped" if cancelled else "done"
    why = "не набралось игроков или тест остановлен" if cancelled else ""
    cur = db.execute("UPDATE autopub_series_items SET status=?, finished_at=?, error=? WHERE id=? AND status='running'",
                     (new, _iso(now), why, item["id"]))
    if not cur.rowcount:
        return False
    _event(item["series_id"], "ITEM_FINISHED", item["order_index"], item["test_id"], new, why, now)
    return await _after_item(bot, item["series_id"], now)


async def _after_item(bot: Bot, sid: int, now: datetime, wait: int = None) -> bool:
    """Тест кончился: пауза 20–30 сек и следующий по порядку, или конец серии."""
    s = get_series(sid)
    if not s or s["status"] != "running":
        return False
    nxt = next((i for i in s["items"] if i["status"] == "pending"), None)
    if nxt is None:
        await _complete(bot, s, now)
        return True
    wait = _random.randint(INTERVAL_MIN, INTERVAL_MAX) if wait is None else wait
    at = now + timedelta(seconds=wait)
    cur = db.execute("UPDATE autopub_series SET status='waiting_next', current_pos=?, next_run_at=? "
                     "WHERE id=? AND status='running'", (nxt["order_index"], _iso(at), sid))
    if not cur.rowcount:
        return False
    _event(sid, "WAITING_NEXT", nxt["order_index"], nxt["test_id"], "waiting_next",
           f"next_run_at={to_local(at).isoformat()} wait={wait}s", now)
    if wait > 0:
        try:
            await bot.send_message(int(s["test_chat_id"]),
                                   f"⏳ <b>Через {wait} сек — следующий тест</b> ({nxt['order_index'] + 1}/{len(s['items'])})"
                                   f"\n\n📚 <b>{_esc(nxt['title'])}</b>\n\nГотовьтесь! 🚀", parse_mode="HTML")
        except Exception as e:
            log.warning("серия %s: сообщение о следующем тесте: %s", sid, e)
    return True


async def _complete(bot: Bot, s: dict, now: datetime) -> None:
    cur = db.execute("UPDATE autopub_series SET status='completed', finished_at=?, next_run_at=NULL "
                     "WHERE id=? AND status IN ('running','waiting_next')", (_iso(now), s["id"]))
    if not cur.rowcount:
        return
    _event(s["id"], "SERIES_COMPLETED", status="completed", details=f"test_chat_id={s['test_chat_id']}", now=now)
    if s.get("start_announced_at"):
        await _finish_series_open_chat(bot, int(s["test_chat_id"]))
    _release_bot_announce(s["id"])


async def _fail(bot: Bot, s: dict, why: str, now: datetime) -> None:
    cur = db.execute("UPDATE autopub_series SET status='failed', error=?, finished_at=?, next_run_at=NULL "
                     "WHERE id=? AND status IN ('draft','scheduled','running','waiting_next')", (why[:300], _iso(now), s["id"]))
    if not cur.rowcount:
        return
    db.execute("UPDATE autopub_series_items SET status='cancelled' WHERE series_id=? AND status IN ('pending','launching')",
               (s["id"],))
    _event(s["id"], "SERIES_FAILED", s.get("current_pos", -1), status="failed", details=why, now=now)
    _release_bot_announce(s["id"])
    log.error("SERIES_FAILED series_id=%s announcement_channel_id=%s test_chat_id=%s error=%s",
              s["id"], s.get("announcement_channel_id") or "none", s["test_chat_id"], why)
    if s.get("chat_locked"):
        try:
            await _unlock_chat(bot, int(s["test_chat_id"]))
        except Exception:
            pass
    if s.get("created_by"):
        try:
            await bot.send_message(int(s["created_by"]), f"❌ <b>Серия тестов #{s['id']} остановлена</b>\n\n"
                                                         f"Причина: {_esc(why)}", parse_mode="HTML")
        except Exception:
            pass


async def _watchdog(bot: Bot, now: datetime) -> None:
    """После перезапуска и при потерянных сигналах: тест серии закончился, а
    серия об этом не узнала (бот перезапускался, лобби отменили вручную) — двигаем."""
    from services import group_quiz_service as gqs
    for r in db.fetchall("SELECT i.*, g.status AS gq_status FROM autopub_series_items i "
                         "JOIN autopub_series s ON s.id=i.series_id LEFT JOIN group_quizzes g ON g.id=i.group_quiz_id "
                         "WHERE s.status='running' AND i.status='running'"):
        gs = r["gq_status"]
        if gs == "paused":
            continue                       # /pause: тест не закончен — ждём /resume или /stop
        if gs in ("lobby", "running"):
            if not gqs.is_orphan(r["group_quiz_id"]):
                continue
            await gqs.cancel_orphan(bot, r["group_quiz_id"], "перезапуск бота")
            gs = "cancelled"
        item = {k: r[k] for k in ("id", "series_id", "order_index", "test_id")}
        await _item_finished(bot, item, cancelled=(gs != "finished"), now=now)
    stuck = _iso(now - timedelta(seconds=LAUNCH_STUCK_SECONDS))
    for r in db.fetchall("SELECT i.* FROM autopub_series_items i JOIN autopub_series s ON s.id=i.series_id "
                         "WHERE s.status='running' AND i.status='launching' AND i.launched_at<?", (stuck,)):
        if (r["launch_attempts"] or 0) >= 2:
            db.execute("UPDATE autopub_series_items SET status='failed', error='запуск дважды оборвался', finished_at=? "
                       "WHERE id=? AND status='launching'", (_iso(now), r["id"]))
            _event(r["series_id"], "ITEM_FAILED", r["order_index"], r["test_id"], "failed", "запуск дважды оборвался", now)
            await _after_item(bot, r["series_id"], now, wait=0)
        else:
            db.execute("UPDATE autopub_series_items SET status='pending' WHERE id=? AND status='launching'", (r["id"],))
            db.execute("UPDATE autopub_series SET status='waiting_next', next_run_at=? WHERE id=? AND status='running'",
                       (_iso(now), r["series_id"]))
    # Серия «идёт», но ни один тест не запущен и не запускается (процесс упал между шагами)
    for r in db.fetchall("SELECT s.id FROM autopub_series s WHERE s.status='running' AND COALESCE(s.started_at,'')<? "
                         "AND NOT EXISTS (SELECT 1 FROM autopub_series_items i WHERE i.series_id=s.id "
                         "AND i.status IN ('launching','running'))", (stuck,)):
        db.execute("UPDATE autopub_series SET status='waiting_next', next_run_at=? WHERE id=? AND status='running'",
                   (_iso(now), r["id"]))


def get_active_series() -> Optional[dict]:
    """Совместимость: идущая сейчас серия (её чат тестов)."""
    r = db.fetchone("SELECT * FROM autopub_series WHERE status IN ('running','waiting_next') ORDER BY id DESC LIMIT 1")
    if not r:
        return None
    return {"series_id": r["id"], "chat_id": r["test_chat_id"], "channel_id": r["announcement_channel_id"]}


def migrate_legacy_queue() -> int:
    """Серии, запланированные прошлой версией (autopub_queue + settings), —
    в новую таблицу, с тем же порядком и выбранными чатом/каналом. Серия без
    сохранённого чата не угадывается: помечается сбоем, админ создаёт заново."""
    try:
        rows = [dict(r) for r in db.fetchall("SELECT * FROM autopub_queue WHERE status='pending' ORDER BY id")]
    except Exception:
        return 0
    n = 0
    for r in rows:
        raw = _get_setting(f"series_state:{r.get('series_id')}") if r.get("series_id") else None
        try:
            st = _json.loads(raw) if raw else {}
        except ValueError:
            st = {}
        ids = [int(x) for x in (st.get("test_ids") or str(r.get("series_test_ids") or r["test_id"]).split(","))
               if str(x).strip().isdigit()]
        chat = st.get("chat_id")
        if not chat or not ids:
            db.execute("UPDATE autopub_queue SET status='failed', error=? WHERE id=?",
                       ("серия прошлой версии без выбранного чата — создайте её заново", r["id"]))
            log.error("SERIES_FAILED legacy_queue_id=%s: не выбран чат тестов — серия не перенесена", r["id"])
            continue
        chan = st.get("channel_id")
        titles = []
        for tid in ids:
            t = db.fetchone("SELECT title FROM tests WHERE id=?", (tid,))
            titles.append(t["title"] if t else f"#{tid}")
        cur = db.execute(
            "INSERT OR IGNORE INTO autopub_series (status, launch_mode, delay_seconds, scheduled_at, "
            "announcement_channel_id, announcement_channel_title, test_chat_id, test_chat_title, mode, source_titles, "
            "op_key, created_by, created_at) VALUES ('draft','legacy',0,?,?,?,?,?,'full',?,?,?,?)",
            (_iso(_parse_utc(r["run_at"]) or _now_utc()), chan, (get_channel_by_id(chan) or {}).get("title") if chan else None,
             str(chat), (get_chat_by_id(chat) or {}).get("title") or str(chat), _json.dumps(titles, ensure_ascii=False),
             f"legacy:{r['id']}", r.get("created_by"), _iso(_now_utc())))
        if cur.rowcount:
            sid = cur.lastrowid
            db.executemany("INSERT INTO autopub_series_items (series_id, order_index, test_id, title) VALUES (?,?,?,?)",
                           [(sid, i, tid, titles[i]) for i, tid in enumerate(ids)])
            db.execute("UPDATE autopub_series SET status='scheduled' WHERE id=? AND status='draft'", (sid,))
            _event(sid, "SERIES_CREATED", details=f"перенесена из очереди прошлой версии #{r['id']} "
                                                  f"announcement_channel_id={chan or 'none'} test_chat_id={chat}")
            n += 1
        db.execute("UPDATE autopub_queue SET status='migrated' WHERE id=?", (r["id"],))
    db.execute("UPDATE autopub_queue SET status='done' WHERE status='running'")
    if n:
        _set_setting("active_series_id", "")
    return n


_worker_task: Optional[asyncio.Task] = None


async def _worker_loop(bot: Bot):
    """Исполнитель серий. Просыпается к ближайшему старту (не реже раза в 5 сек):
    серия на 20:00 стартует в 20:00, не раньше. Всё состояние — в базе."""
    log.info("autopub worker started")
    try:
        n = migrate_legacy_queue()
        if n:
            log.warning("Перенесено серий из очереди прошлой версии: %s", n)
    except Exception as e:
        log.exception("перенос старой очереди серий: %s", e)
    while True:
        try:
            await series_tick(bot)
        except asyncio.CancelledError:
            log.info("autopub worker cancelled")
            return
        except Exception as e:
            log.exception("серии тестов, проход: %s", e)
        try:
            await asyncio.sleep(_next_sleep())
        except asyncio.CancelledError:
            log.info("autopub worker cancelled")
            return


async def _delayed_unlock(bot: Bot, chat_id: int, delay_sec: int):
    """Отложенно открыть чат (резервный механизм)."""
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

DEFAULT_RANDOM_COUNT = 10


def get_random_count() -> int:
    """Сколько вопросов уходит за одну автопубликацию. Настраивает админ."""
    try:
        row = db.fetchone("SELECT value FROM settings WHERE key='autopub_random_count'")
        if row and row.get("value"):
            n = int(row["value"])
            if 1 <= n <= 100:
                return n
    except Exception:
        pass
    return DEFAULT_RANDOM_COUNT


def set_random_count(n: int) -> None:
    db.execute("INSERT OR REPLACE INTO settings (key, value) "
               "VALUES ('autopub_random_count', ?)", (str(max(1, min(100, int(n)))),))


async def post_random_quiz_polls_to_channel(
        bot: Bot, count: int = 10,
        category_id: Optional[int] = None,
        topic_id: Optional[int] = None,
        language: str = 'ru',
        bot_username: str = '',
        test_ids: Optional[list] = None,
        channel_id: Optional[str] = None,
        send_promo: bool = True) -> tuple[int, int]:
    """
    Опубликовать заданное число Quiz Poll на канале БЕЗ таймера и нумерации.
    test_ids — если задан список, берём вопросы ПОРОВНУ из этих тестов.
    channel_id — явный канал (если не задан, берём первый из списка).
    Между вопросами задержка 10 сек. В конце — пост с кнопкой.
    """
    if not channel_id:
        chans = get_channels()
        channel_id = chans[0]['id'] if chans else get_autopub_config().get('channel_id')
    if not channel_id:
        return 0, 0

    sample = []
    if test_ids:
        # МИКС максимально поровну из выбранных тестов.
        # Раньше при count меньше числа тестов вопросы всегда доставались
        # первым тестам списка — теперь порядок каждый раз перемешивается,
        # так что со временем очередь доходит до всех.
        ids = list(test_ids)
        random.shuffle(ids)
        n = len(ids)
        base, extra = divmod(max(0, int(count)), n)   # остаток раздаём по одному
        quota = {tid: base + (1 if i < extra else 0) for i, tid in enumerate(ids)}

        pools = {}
        for tid in ids:
            pools[tid] = db.fetchall(
                "SELECT q.id, q.text, q.explanation, q.test_id "
                "FROM questions q WHERE q.test_id=? ORDER BY RANDOM()", (tid,))
            sample.extend(pools[tid][:quota[tid]])

        # Где-то вопросов не хватило — недобор равномерно разбираем по кругу
        # между остальными, чтобы всё равно набрать заказанное количество.
        if len(sample) < count:
            taken = {tid: min(quota[tid], len(pools[tid])) for tid in ids}
            while len(sample) < count:
                added = False
                for tid in ids:
                    if len(sample) >= count:
                        break
                    if taken[tid] < len(pools[tid]):
                        sample.append(pools[tid][taken[tid]])
                        taken[tid] += 1
                        added = True
                if not added:
                    break        # вопросы закончились во всех тестах
        sample = sample[:count]
        random.shuffle(sample)
    else:
        # Старый путь — по категории/теме
        sql = """SELECT q.id, q.text, q.explanation, q.test_id
                 FROM questions q JOIN tests t ON t.id=q.test_id
                 WHERE t.status='active' AND t.is_paid=0
                   AND COALESCE(t.is_private,0)=0
                   AND t.language=?"""
        args = [language]
        if topic_id is not None:
            sql += " AND t.id=?"
            args.append(topic_id)
        elif category_id is not None:
            sql += " AND t.category_id=?"
            args.append(category_id)
        rows = db.fetchall(sql, tuple(args))
        if not rows:
            return 0, 0
        sample = random.sample(rows, min(count, len(rows)))

    if not sample:
        return 0, 0

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
        # Фото вопроса (если есть)
        _qphoto = db.fetchone(
            "SELECT photo_file_id FROM questions WHERE id=?", (q['id'],))
        if _qphoto and _qphoto.get('photo_file_id'):
            try:
                await bot.send_photo(int(channel_id),
                                      photo=_qphoto['photo_file_id'])
            except Exception:
                pass
        try:
            await bot.send_poll(
                int(channel_id),
                question=q['text'][:300],
                options=[o['text'][:100] for o in opts[:10]],
                type='quiz',
                correct_option_id=correct_idx,
                is_anonymous=True,
                # БЕЗ open_period — опрос без таймера
                explanation=(q.get('explanation') or '')[:200] or None,
            )
            sent += 1
            # Задержка 10 сек между вопросами
            await asyncio.sleep(10)
        except Exception as e:
            log.warning("post random poll: %s", e)
            failed += 1

    # Финальный пост с призывом и кнопкой «Начать тестирование»
    if sent > 0 and send_promo:
        try:
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            uname = bot_username.lstrip('@') if bot_username else ''
            start_url = f"https://t.me/{uname}?start=quiz" if uname else None
            kb = None
            if start_url:
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🚀 Начать тестирование",
                                         url=start_url)
                ]])
            await bot.send_message(
                int(channel_id),
                "📚 <b>Понравились вопросы?</b>\n\n"
                "В нашем боте <b>намного больше тестов</b> по всем предметам ЕНТ!\n\n"
                "✅ Проходи тесты в удобное время\n"
                "⚔️ Соревнуйся в дуэлях с другими\n"
                "🏆 Поднимайся в рейтинге\n"
                "📊 Отслеживай свой прогресс\n\n"
                "👇 <b>Как начать:</b>\n"
                "1. Нажми кнопку ниже\n"
                "2. Выбери язык\n"
                "3. Тапни «📚 Пройти тест» и выбери тему\n\n"
                "Удачи на ЕНТ! 💪",
                reply_markup=kb,
                parse_mode="HTML")
        except Exception as e:
            log.warning("final CTA: %s", e)

    return sent, failed


# ===================== АНОНС В БОТЕ =====================

# Флаг активного анонса (чтобы после теста предложить снова)
S_BOT_ANNOUNCE = "bot_announce_active"


def set_bot_announce(chat_invite: str, titles: list, active: bool = True):
    """Сохранить активный анонс для допоказа после теста."""
    import json as _json
    if active:
        _set_setting(S_BOT_ANNOUNCE, _json.dumps({
            "invite": chat_invite or "",
            "titles": titles[:10],
        }))
    else:
        _set_setting(S_BOT_ANNOUNCE, "")


def get_bot_announce() -> Optional[dict]:
    import json as _json
    raw = _get_setting(S_BOT_ANNOUNCE)
    if not raw:
        return None
    try:
        return _json.loads(raw)
    except Exception:
        return None


def clear_bot_announce():
    _set_setting(S_BOT_ANNOUNCE, "")


async def broadcast_test_announce(bot: Bot, titles: list,
                                    chat_invite: str, when_str: str):
    """
    Разослать анонс теста ВСЕМ зарегистрированным юзерам, на их языке.
    Если юзер сейчас проходит тест — помечаем чтобы прервать с выбором.
    """
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    users = db.fetchall("SELECT tg_id, language FROM users WHERE tg_id IS NOT NULL")
    topics_ru = "\n".join(f"• {t}" for t in titles[:10])
    sent = 0
    for u in users:
        tg = u['tg_id']
        lang = u.get('language') or 'ru'
        if lang == 'kz':
            text = (
                f"🔔 <b>Чатта тестілеу басталады!</b>\n\n"
                f"📚 Тақырыптар:\n{topics_ru}\n\n"
                f"⏰ {when_str}\n\n"
                f"👇 Қатысу үшін чатқа кір:")
            btn = "🚀 Тестілеуге өту"
        else:
            text = (
                f"🔔 <b>Скоро тестирование в чате!</b>\n\n"
                f"📚 Темы:\n{topics_ru}\n\n"
                f"⏰ {when_str}\n\n"
                f"👇 Заходи в чат чтобы участвовать:")
            btn = "🚀 Перейти к тестированию"
        kb = None
        if chat_invite:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=btn, url=chat_invite)]])
        try:
            await bot.send_message(tg, text, parse_mode="HTML",
                                     reply_markup=kb,
                                     disable_web_page_preview=True)
            sent += 1
        except Exception:
            pass
        if sent % 25 == 0:
            await asyncio.sleep(1)  # антифлуд
    log.info("bot announce broadcast sent to %s users", sent)
    return sent


async def send_promo_to_channel(bot: Bot, channel_id, bot_username: str = '') -> bool:
    """Отправить только промо-приглашение в канал (после подтверждения админа)."""
    try:
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        uname = bot_username.lstrip('@') if bot_username else ''
        start_url = f"https://t.me/{uname}?start=quiz" if uname else None
        kb = None
        if start_url:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🚀 Начать тестирование", url=start_url)
            ]])
        await bot.send_message(
            int(channel_id),
            "📚 <b>Понравились вопросы?</b>\n\n"
            "В нашем боте <b>намного больше тестов</b> по всем предметам ЕНТ!\n\n"
            "✅ Проходи тесты в удобное время\n"
            "⚔️ Соревнуйся в дуэлях с другими\n"
            "🏆 Поднимайся в рейтинге\n"
            "📊 Отслеживай свой прогресс\n\n"
            "👇 <b>Как начать:</b>\n"
            "1. Нажми кнопку ниже\n"
            "2. Выбери язык\n"
            "3. Тапни «📚 Пройти тест» и выбери тему\n\n"
            "Удачи на ЕНТ! 💪",
            reply_markup=kb, parse_mode="HTML")
        return True
    except Exception as e:
        log.warning("send_promo: %s", e)
        return False
