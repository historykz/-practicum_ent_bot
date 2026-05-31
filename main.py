"""Точка входа Telegram-бота для подготовки к ЕНТ.

Запуск:
    python main.py

Перед запуском:
    1. Установить зависимости: pip install -r requirements.txt
    2. Создать .env с BOT_TOKEN, ADMIN_IDS, MANAGER_USERNAME
    3. В BotFather: /setinline (включить inline), /setjoingroups (Enable),
       /setprivacy (Disable — чтобы бот видел сообщения в группах)
"""
import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

import config
import database
from middlewares import UserContextMiddleware, AntiSpamMiddleware
from handlers import (common, profile, user, quiz, duel,
                       homework, rating, inline, admin, group_quiz,
                       private_access, categories, question_editor, autopub,
                       appeals, profile_subjects, moderation)


def setup_logging() -> None:
    """Логи и в файл, и в консоль."""
    log_level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    root = logging.getLogger()
    root.setLevel(log_level)
    # Убираем дубли при повторных запусках
    for h in list(root.handlers):
        root.removeHandler(h)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter(fmt))
    root.addHandler(sh)

    try:
        fh = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
        fh.setFormatter(logging.Formatter(fmt))
        root.addHandler(fh)
    except Exception as e:
        # Не критично, если файл недоступен
        sys.stderr.write(f"Не удалось открыть лог-файл: {e}\n")

    # Тише болтают aiogram'овские либы
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def set_default_commands(bot: Bot) -> None:
    cmds = [
        BotCommand(command="start", description="Запуск / меню"),
        BotCommand(command="menu", description="Главное меню"),
        BotCommand(command="help", description="Помощь"),
        BotCommand(command="cancel", description="Отмена"),
        BotCommand(command="admin", description="Админ-панель"),
        BotCommand(command="opens", description="Закрытый доступ (admin)"),
    ]
    try:
        await bot.set_my_commands(cmds)
    except Exception as e:
        logging.warning("Не удалось установить команды: %s", e)


async def main() -> None:
    setup_logging()
    log = logging.getLogger("main")

    if not config.BOT_TOKEN or config.BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        log.error("BOT_TOKEN не задан. Заполните .env (BOT_TOKEN=...).")
        return

    # БД создаётся на старте автоматически
    database.init_db()
    log.info("DB_PATH: %s", config.DB_PATH)
    # Предупреждение если БД лежит в репозитории (не Volume)
    if not config.DB_PATH.startswith("/data") and not config.DB_PATH.startswith("/mnt"):
        log.warning(
            "⚠️ DB_PATH=%s — БД лежит в репозитории и БУДЕТ СТЕРТА при следующем деплое! "
            "Создай Volume на Railway (Settings → Volumes), смонтируй на /data, "
            "и поставь переменную DB_PATH=/data/bot.db",
            config.DB_PATH)
    log.info("База данных инициализирована: %s", config.DB_PATH)

    # Обязательный канал — прописываем в required_channels, если задан
    if config.REQUIRED_CHANNEL:
        ch = config.REQUIRED_CHANNEL.strip()
        if not ch.startswith("@") and not ch.lstrip("-").isdigit():
            ch = "@" + ch.lstrip("@")
        import database as _db
        existing = _db.fetchone(
            "SELECT id FROM required_channels WHERE channel_username=? AND is_global=1",
            (ch,))
        if not existing:
            _db.execute(
                """INSERT INTO required_channels (channel_username, is_global, created_at)
                   VALUES (?, 1, datetime('now'))""", (ch,))
            log.info("Зарегистрирован обязательный канал: %s", ch)
        else:
            log.info("Обязательный канал уже зарегистрирован: %s", ch)

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    # Глобальный обработчик ошибок — снимает «загрузку» и не даёт боту падать
    from aiogram.types import ErrorEvent, CallbackQuery
    @dp.errors()
    async def global_error_handler(event: ErrorEvent):
        log.exception("Unhandled exception: %s", event.exception)
        err_text = str(event.exception)[:200]
        try:
            update = event.update
            if update and update.callback_query:
                user_id = update.callback_query.from_user.id if update.callback_query.from_user else None
                # Админу показываем настоящую ошибку, обычному юзеру — общую
                is_a = False
                try:
                    from utils import is_admin
                    is_a = is_admin(user_id) if user_id else False
                except Exception:
                    pass
                if is_a:
                    msg = f"⚠️ Ошибка: {err_text}"
                else:
                    msg = "⚠️ Произошла ошибка. Попробуйте ещё раз."
                try:
                    await update.callback_query.answer(msg, show_alert=True)
                except Exception:
                    pass
            elif update and update.message:
                try:
                    user_id = update.message.from_user.id if update.message.from_user else None
                    from utils import is_admin
                    if is_admin(user_id):
                        await update.message.answer(f"⚠️ Ошибка: {err_text}")
                except Exception:
                    pass
        except Exception:
            pass
        return True

    # Узнаём username бота — нужен для deep-link'ов
    try:
        me = await bot.get_me()
        config.BOT_USERNAME = me.username or ""
        log.info("Бот: @%s (id=%s)", me.username, me.id)
    except Exception as e:
        log.error("Не удалось получить info о боте: %s", e)
        return

    # Middlewares
    user_mw = UserContextMiddleware()
    anti_mw = AntiSpamMiddleware()
    dp.message.middleware(user_mw)
    dp.callback_query.middleware(user_mw)
    dp.inline_query.middleware(user_mw)
    dp.message.middleware(anti_mw)
    dp.callback_query.middleware(anti_mw)

    # Routers — порядок важен (общие → специфичные)
    dp.include_router(common.router)
    dp.include_router(appeals.router)
    dp.include_router(profile_subjects.router)
    dp.include_router(moderation.router)
    dp.include_router(profile.router)
    dp.include_router(user.router)
    dp.include_router(quiz.router)
    dp.include_router(duel.router)
    dp.include_router(homework.router)
    dp.include_router(rating.router)
    dp.include_router(inline.router)
    dp.include_router(group_quiz.router)
    dp.include_router(private_access.router)
    dp.include_router(categories.router)
    dp.include_router(question_editor.router)
    dp.include_router(autopub.router)
    dp.include_router(admin.router)

    await set_default_commands(bot)

    # Фоновая задача: уведомления об истечении Premium (раз в час)
    from services import premium_service as _ps
    asyncio.create_task(_ps.premium_expiry_loop(bot))

    # Фоновая задача: истечение приватного доступа
    from handlers import private_access as _pa
    asyncio.create_task(_pa.expiry_check_loop(bot))

    # Фоновая задача: авто-публикация тестов по расписанию
    from services import autopub_service as _aps
    _aps.start_worker(bot)

    log.info("Запуск polling...")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        log.info("Бот остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Прерывание пользователем.")
