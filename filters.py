"""
Custom filters.
"""
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message

from utils import is_admin, admin_level, is_owner


class IsAdmin(BaseFilter):
    """Пропускает только админов."""
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        if event.from_user is None:
            return False
        return is_admin(event.from_user.id)


class IsOwner(BaseFilter):
    """Только главный админ (ADMIN_IDS) — полный доступ к панелям бота."""
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        if event.from_user is None:
            return False
        return is_owner(event.from_user.id)


class MinLevel(BaseFilter):
    """Админ с уровнем не ниже заданного (2 = импорт txt, 3 = владелец)."""
    def __init__(self, level: int):
        self.level = level

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        if event.from_user is None:
            return False
        return admin_level(event.from_user.id) >= self.level
