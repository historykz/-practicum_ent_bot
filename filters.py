"""
Custom filters.
"""
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message

from utils import is_admin


class IsAdmin(BaseFilter):
    """Пропускает только админов."""
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        if event.from_user is None:
            return False
        return is_admin(event.from_user.id)
