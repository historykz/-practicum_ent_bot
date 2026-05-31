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


async def announce_batch_with_topics(bot: Bot, tests: list[dict],
                                       when_str: str) -> bool:
    """ПРЕД-анонс СРАЗУ С ТЕМАМИ (когда планируем на будущее)."""
    cfg = get_autopub_config()
    channel_id = cfg.get('channel_id')
    if not channel_id:
        return False
    invite = cfg.get('invite_link') or ''
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


# ===================== СОСТОЯНИЕ СЕРИИ (ЦЕПОЧКА) =====================

def save_series_state(series_id: str, test_ids_csv: str,
                       total: int, created_by: int):
    """Сохранить состояние серии для запуска цепочкой."""
    import json as _json
    state = {
        "series_id": series_id,
        "test_ids": [int(x) for x in test_ids_csv.split(',') if x.strip().isdigit()],
        "total": total,
        "created_by": created_by,
        "current_index": 0,
    }
    _set_setting(f"series_state:{series_id}", _json.dumps(state))
    # Запомним «активную» серию для чата
    _set_setting("active_series_id", series_id)


def get_active_series() -> Optional[dict]:
    import json as _json
    sid = _get_setting("active_series_id")
    if not sid:
        return None
    raw = _get_setting(f"series_state:{sid}")
    if not raw:
        return None
    try:
        return _json.loads(raw)
    except Exception:
        return None


def _update_series_state(state: dict):
    import json as _json
    _set_setting(f"series_state:{state['series_id']}", _json.dumps(state))


def clear_active_series():
    _set_setting("active_series_id", "")


async def on_series_test_finished(bot: Bot, test_id: int, chat_id: int):
    """
    Вызывается когда групповой тест завершился.
    Если это тест из активной серии — запустить следующий через 20 сек,
    либо (если последний) открыть чат с поздравлением.
    """
    state = get_active_series()
    if not state:
        # Нет активной серии — просто открыть чат если был закрыт
        return
    test_ids = state.get('test_ids') or []
    cur_idx = state.get('current_index', 0)

    # Проверяем что завершившийся тест — это текущий в серии
    if cur_idx >= len(test_ids):
        clear_active_series()
        return
    # Сверяем (учёт mix — там test_id может отличаться, поэтому просто двигаем)
    is_last = (cur_idx >= len(test_ids) - 1)

    if is_last:
        # Последний тест серии — открываем чат с поздравлением
        await _finish_series_open_chat(bot, chat_id)
        clear_active_series()
    else:
        # Двигаем индекс и запускаем следующий
        next_idx = cur_idx + 1
        state['current_index'] = next_idx
        _update_series_state(state)
        next_test_id = test_ids[next_idx]
        next_test = db.fetchone("SELECT * FROM tests WHERE id=?", (next_test_id,))
        if not next_test:
            await _finish_series_open_chat(bot, chat_id)
            clear_active_series()
            return
        # Анонс «через 20 сек новый тест» — В ЧАТ, сразу
        try:
            await announce_single_reminder(bot, dict(next_test))
        except Exception:
            pass
        # Ждём 20 сек и запускаем следующий
        import asyncio as _asyncio
        _asyncio.create_task(
            _launch_next_after_delay(bot, next_test_id, 20))


async def _launch_next_after_delay(bot: Bot, test_id: int, delay: int):
    import asyncio as _asyncio
    try:
        await _asyncio.sleep(delay)
        await publish_test_to_chat(bot, test_id)
    except _asyncio.CancelledError:
        return
    except Exception as e:
        log.warning("launch next: %s", e)


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
                    # Полный анонс с темами (для первого/единственного теста)
                    if series_ids_str:
                        try:
                            ids = [int(x) for x in series_ids_str.split(',') if x.strip().isdigit()]
                            tests_obj = []
                            for tid in ids:
                                t = db.fetchone("SELECT * FROM tests WHERE id=?", (tid,))
                                if t:
                                    tests_obj.append(dict(t))
                            if tests_obj:
                                if len(tests_obj) == 1:
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

                    # Пауза чтобы юзеры успели зайти в чат
                    if posted_full_announce:
                        await asyncio.sleep(15)

                    # Закрываем чат перед первым тестом серии
                    cfg = get_autopub_config()
                    chat_id_cfg = cfg.get('chat_id')
                    if chat_id_cfg:
                        try:
                            await _lock_chat(bot, int(chat_id_cfg))
                        except Exception as e:
                            log.warning("lock on first: %s", e)

                    # Запуск лобби. Цепочка следующих — через on_series_test_finished
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

async def post_random_quiz_polls_to_channel(
        bot: Bot, count: int = 10,
        category_id: Optional[int] = None,
        topic_id: Optional[int] = None,
        language: str = 'ru',
        bot_username: str = '',
        test_ids: Optional[list] = None,
        channel_id: Optional[str] = None) -> tuple[int, int]:
    """
    Опубликовать 10 Quiz Poll на канале БЕЗ таймера, БЕЗ нумерации.
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
        # МИКС поровну из выбранных тестов
        n = len(test_ids)
        per = max(1, count // n)
        pools = {}
        for tid in test_ids:
            qs = db.fetchall(
                "SELECT q.id, q.text, q.explanation, q.test_id "
                "FROM questions q WHERE q.test_id=? ORDER BY RANDOM()", (tid,))
            pools[tid] = qs
            sample.extend(qs[:per])
        # Добор рандомом до count
        if len(sample) < count:
            leftover = []
            for tid in test_ids:
                leftover.extend(pools[tid][per:])
            random.shuffle(leftover)
            sample.extend(leftover[:count - len(sample)])
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
    if sent > 0:
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
