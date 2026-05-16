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
    user = utils.get_user_by_tg(query.from_user.id)
    lang = (user.get('language') if user else None) or 'ru'
    results = share_service.build_inline_results(query.query or "", lang)
    try:
        await query.answer(results, cache_time=config.INLINE_CACHE_TIME,
                            is_personal=True)
    except Exception as e:
        log.warning("Inline answer failed: %s", e)
