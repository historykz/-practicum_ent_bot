"""Inline-режим: поиск тестов и шеринг через @bot."""
import logging

from aiogram import Router, F
from aiogram.types import InlineQuery

import config
import utils
from services import share_service

router = Router(name="inline")
log = logging.getLogger(__name__)


@router.inline_query()
async def inline_search(query: InlineQuery):
    """
    Inline-режим работает так же, как у @QuizBot.
    Поддерживаемые запросы:
      @bot test:42         → конкретный тест
      @bot биология        → поиск
      @bot                 → последние 30 активных тестов
    """
    u = utils.get_user_by_tg(query.from_user.id) or {}
    # Для inline-режима НЕ фильтруем строго по языку — у получателя в группе
    # может быть свой язык. Только если запрос пустой и пользователь известен —
    # покажем сначала тесты его языка.
    user_lang = u.get("language")
    q = (query.query or "").strip()
    # Если в запросе есть test:<id> — игнорируем язык
    if q.lower().startswith("test:") or q.lower().startswith("grp:"):
        results = share_service.build_inline_results(
            q, None, user_tg_id=query.from_user.id)
    else:
        results = share_service.build_inline_results(
            q, user_lang, user_tg_id=query.from_user.id)

    try:
        await query.answer(
            results,
            cache_time=config.INLINE_CACHE_TIME,
            is_personal=True,
        )
    except Exception as e:
        log.warning("Inline answer failed: %s", e)
