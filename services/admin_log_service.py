"""
Логирование действий администраторов в отдельный чат.
Каждое важное действие (создание теста, выдача доступа, удаление и т.д.)
отправляется в чат ADMIN_LOG_CHAT (или главному админу в личку).
"""
import logging
from datetime import datetime, timezone, timedelta

import config
import database as db

log = logging.getLogger(__name__)

ALMATY = timezone(timedelta(hours=5))


def _fmt_now() -> str:
    return datetime.now(ALMATY).strftime("%d.%m.%Y %H:%M")


def _log_chat_id():
    """Куда слать логи: из БД (команда /setlogchat), потом env, потом главный админ."""
    # 1. Из настроек БД (задаётся командой /setlogchat в нужном чате)
    try:
        row = db.fetchone("SELECT value FROM settings WHERE key='admin_log_chat'")
        if row and row.get('value'):
            return int(row['value'])
    except Exception:
        pass
    # 2. Из переменной окружения
    if config.ADMIN_LOG_CHAT:
        try:
            return int(config.ADMIN_LOG_CHAT)
        except ValueError:
            return config.ADMIN_LOG_CHAT
    # 3. Фолбэк — главный админ
    try:
        return config._HARDCODED_ADMIN_IDS[0]
    except Exception:
        return None


def set_log_chat(chat_id: int):
    """Сохранить чат для логов в БД."""
    db.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('admin_log_chat', ?)",
        (str(chat_id),))


def _admin_label(tg_id: int) -> str:
    """Имя админа: @username (ID) или ID."""
    u = db.fetchone("SELECT username, first_name FROM users WHERE tg_id=?", (tg_id,))
    if u and u.get('username'):
        return f"@{u['username']} (ID {tg_id})"
    if u and u.get('first_name'):
        return f"{u['first_name']} (ID {tg_id})"
    return f"ID {tg_id}"


async def log_action(bot, admin_tg_id: int, action: str, details: str = ""):
    """
    Отправить запись о действии админа в лог-чат.
    action — краткое название, details — подробности.
    """
    chat_id = _log_chat_id()
    if not chat_id:
        return
    who = _admin_label(admin_tg_id)
    text = (f"👮 <b>Действие админа</b>\n"
            f"🕐 {_fmt_now()}\n"
            f"👤 {who}\n\n"
            f"📌 <b>{action}</b>")
    if details:
        text += f"\n{details}"
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception as e:
        log.warning("admin log send to %s failed: %s", chat_id, e)
        # Фолбэк: если не удалось в лог-чат — шлём главному админу с причиной
        try:
            main_admin = config._HARDCODED_ADMIN_IDS[0]
            if str(chat_id) != str(main_admin):
                await bot.send_message(
                    main_admin,
                    f"⚠️ <b>Не удалось отправить лог в чат {chat_id}</b>\n"
                    f"Причина: {str(e)[:150]}\n\n"
                    f"Проверь: бот добавлен в чат? Бот админ в чате? "
                    f"ID чата правильный (с минусом для групп)?\n\n"
                    f"Сам лог:\n{text}",
                    parse_mode="HTML")
        except Exception:
            pass


async def log_test_created(bot, admin_tg_id: int, test_id: int):
    """Лог создания теста с деталями + zip выгрузкой."""
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))
    if not test:
        return
    qcount = db.fetchone(
        "SELECT COUNT(*) AS c FROM questions WHERE test_id=?", (test_id,))['c']
    # Тип доступа
    if test.get('is_private'):
        access = "🔐 Закрытый (приватный)"
    elif test.get('is_paid'):
        stars = test.get('price_stars') or 0
        tenge = test.get('price') or 0
        access = f"💎 Платный ({tenge}₸ · {stars}⭐️)"
    else:
        access = "🆓 Бесплатный"
    cat = db.fetchone("SELECT name FROM test_categories WHERE id=?",
                       (test.get('category_id'),)) if test.get('category_id') else None
    cat_name = cat['name'] if cat else "без раздела"

    details = (f"📝 Тест: «{test['title']}»\n"
               f"🆔 ID теста: {test_id}\n"
               f"📚 Вопросов: {qcount}\n"
               f"📂 Раздел: {cat_name}\n"
               f"🔓 Доступ: {access}")
    await log_action(bot, admin_tg_id, "Создан тест", details)

    # Выгрузка теста в txt-файл
    chat_id = _log_chat_id()
    if chat_id and qcount > 0:
        try:
            await _send_test_export(bot, chat_id, test_id, admin_tg_id)
        except Exception as e:
            log.warning("test export in log: %s", e)


async def _send_test_export(bot, chat_id: int, test_id: int, admin_tg_id: int):
    """Отправить содержимое теста в txt-файле."""
    from aiogram.types import BufferedInputFile
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))
    questions = db.fetchall(
        "SELECT * FROM questions WHERE test_id=? ORDER BY order_num, id", (test_id,))
    lines = [f"Тест: {test['title']}",
             f"ID: {test_id}",
             f"Язык: {test.get('language')}",
             f"Вопросов: {len(questions)}",
             "=" * 40, ""]
    for i, q in enumerate(questions, 1):
        lines.append(f"{i}. {q['text']}")
        opts = db.fetchall(
            "SELECT * FROM question_options WHERE question_id=? ORDER BY order_num",
            (q['id'],))
        for o in opts:
            mark = "*" if o.get('is_correct') else " "
            lines.append(f"   [{mark}] {o['text']}")
        if q.get('explanation'):
            lines.append(f"   💡 {q['explanation']}")
        lines.append("")
    content = "\n".join(lines).encode('utf-8')
    who = _admin_label(admin_tg_id)
    fname = f"test_{test_id}_{test['title'][:20].replace(' ','_')}.txt"
    doc = BufferedInputFile(content, filename=fname)
    await bot.send_document(
        chat_id, doc,
        caption=f"📄 Содержимое теста «{test['title']}» (ID {test_id})\n"
                f"Создал: {who}")


async def log_access_granted(bot, admin_tg_id: int, target_info: str,
                              test_title: str, access_type: str):
    """Лог выдачи доступа пользователю."""
    details = (f"👥 Кому: {target_info}\n"
               f"📝 Тест: «{test_title}»\n"
               f"🔓 Тип: {access_type}")
    await log_action(bot, admin_tg_id, "Выдан доступ", details)


async def log_test_deleted(bot, admin_tg_id: int, test_title: str, test_id: int):
    """Лог удаления теста."""
    details = f"📝 Тест: «{test_title}»\n🆔 ID: {test_id}"
    await log_action(bot, admin_tg_id, "🗑 Удалён тест", details)
