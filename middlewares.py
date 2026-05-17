"""
Middlewares:
- AntiSpamMiddleware - защита от слишком частых действий.
- UserContextMiddleware - upsert пользователя, проверка блока, кладёт user в data.
"""
import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from config import ANTISPAM_COOLDOWN_SECONDS
from locales import t
from utils import get_or_create_user, is_blocked

logger = logging.getLogger(__name__)


class AntiSpamMiddleware(BaseMiddleware):
    """Минимальная задержка между действиями одного пользователя."""

    def __init__(self) -> None:
        super().__init__()
        self._last: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        # Пропускаем без проверки: Poll-сообщения и пересланные (массовый импорт админом).
        if isinstance(event, Message):
            if event.poll is not None:
                return await handler(event, data)
            if event.forward_origin is not None or event.forward_from is not None \
                    or event.forward_from_chat is not None:
                return await handler(event, data)

        now = time.monotonic()
        last = self._last.get(user.id, 0)
        if now - last < ANTISPAM_COOLDOWN_SECONDS:
            # Игнорируем callback, на сообщение можем ответить (но не будем плодить флуд)
            if isinstance(event, CallbackQuery):
                try:
                    lang = data.get("lang", "ru")
                    await event.answer(t("spam_warning", lang), show_alert=False)
                except Exception:
                    pass
            return
        self._last[user.id] = now
        return await handler(event, data)


class UserContextMiddleware(BaseMiddleware):
    """
    Создаёт/обновляет пользователя в БД и кладёт в data:
      data['user'] - dict с пользователем (или None для каналов/сервисных)
      data['lang'] - язык пользователя
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)

        try:
            user = get_or_create_user(
                tg_id=tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name,
                last_name=tg_user.last_name,
            )
        except Exception as e:
            logger.exception("Не удалось создать/обновить пользователя: %s", e)
            return await handler(event, data)

        data["user"] = user
        data["lang"] = user.get("language") or "ru"

        # Блок
        if is_blocked(tg_user.id):
            if isinstance(event, Message):
                try:
                    await event.answer(t("blocked", data["lang"]))
                except Exception:
                    pass
            elif isinstance(event, CallbackQuery):
                try:
                    await event.answer(t("blocked", data["lang"]), show_alert=True)
                except Exception:
                    pass
            return

        return await handler(event, data)
