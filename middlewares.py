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

        # Пропускаем без проверки: Poll-сообщения и пересланные.
        if isinstance(event, Message):
            if event.poll is not None:
                return await handler(event, data)
            if event.forward_origin is not None or event.forward_from is not None \
                    or event.forward_from_chat is not None:
                return await handler(event, data)

        # ВАЖНЫЕ callback'и пропускаем без anti-spam:
        # онбординг, выбор языка, отмена — иначе юзер не сможет пройти первый /start
        if isinstance(event, CallbackQuery) and event.data:
            critical_prefixes = ("onb:", "setlang:", "cancel")
            if any(event.data.startswith(p) for p in critical_prefixes):
                return await handler(event, data)

        # Пропускаем админов — у них могут быть массовые действия
        try:
            from utils import is_admin
            if is_admin(user.id):
                return await handler(event, data)
        except Exception:
            pass

        now = time.monotonic()
        last = self._last.get(user.id, 0)
        if now - last < ANTISPAM_COOLDOWN_SECONDS:
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer()
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
            # Гарантируем что в data всегда есть user (пусть пустой)
            data.setdefault("user", {})
            data.setdefault("lang", "ru")
            return await handler(event, data)

        user = None
        try:
            user = get_or_create_user(
                tg_id=tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name,
                last_name=tg_user.last_name,
            )
        except Exception as e:
            logger.exception("Не удалось создать/обновить пользователя: %s", e)

        # Fallback — минимальный user dict если БД упала
        if not user:
            user = {
                'id': 0,
                'tg_id': tg_user.id,
                'username': tg_user.username or '',
                'first_name': tg_user.first_name or '',
                'language': 'ru',
            }

        data["user"] = user
        data["lang"] = user.get("language") or "ru"

        # Блок (с защитой)
        try:
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
        except Exception:
            pass

        return await handler(event, data)
