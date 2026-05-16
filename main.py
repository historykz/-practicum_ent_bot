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
from handlers import (common, profile, user, quiz, daily, duel,
                       notes, homework, rating, inline, admin)


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
    log.info("База данных инициализирована: %s", config.DB_PATH)

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

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
    dp.include_router(profile.router)
    dp.include_router(user.router)
    dp.include_router(quiz.router)
    dp.include_router(daily.router)
    dp.include_router(duel.router)
    dp.include_router(notes.router)
    dp.include_router(homework.router)
    dp.include_router(rating.router)
    dp.include_router(inline.router)
    dp.include_router(admin.router)

    await set_default_commands(bot)

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
