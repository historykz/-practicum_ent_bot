"""Inline-режим: поиск тестов и шеринг через @bot."""
import logging

from aiogram import Router, F
from aiogram.types import InlineQuery

import config
import utils
from services import share_service

router = Router(name="inline")
log = logging.getLogger(__name__)


async def _build_share_result(q: str, from_user):
    """Построить inline-результат шеринга результата теста с мотивацией."""
    import database as db
    from aiogram.types import (InlineQueryResultArticle,
                                InputTextMessageContent,
                                InlineKeyboardMarkup, InlineKeyboardButton)
    # Парсим share_<testid>_<correct>_<total>
    try:
        _, test_id, correct, total = q.split("_")
        test_id = int(test_id); correct = int(correct); total = int(total)
    except Exception:
        return []
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))
    if not test:
        return []
    title = test['title']
    percent = round(correct / total * 100) if total else 0

    # Текст мотивации по результату
    if percent >= 70:
        emoji = "🏆"
        headline = f"🏆 Я прошёл тест «{title}» на {correct}/{total} ({percent}%)!"
        motivate = "Слабо побить мой результат? 😎"
        desc = f"Отличный результат {percent}% — бросай вызов друзьям!"
    elif percent >= 40:
        emoji = "📊"
        headline = f"📊 Я прошёл тест «{title}» на {correct}/{total} ({percent}%)."
        motivate = "Попробуй и ты — может обгонишь меня! 💪"
        desc = f"Результат {percent}% — есть куда расти, зови друзей!"
    else:
        emoji = "📚"
        headline = f"📚 Я прошёл тест «{title}» на {correct}/{total}."
        motivate = "Сложный тест! Проверь свои силы 🔥"
        desc = f"Сложный тест — проверь смогут ли друзья лучше!"

    # Кнопка «Пройти этот тест» через deep-link
    bot_username = getattr(config, 'BOT_USERNAME', '') or ''
    if not bot_username:
        try:
            me = await from_user.bot.get_me()
            bot_username = me.username or ''
        except Exception:
            pass

    msg_text = f"{headline}\n\n{motivate}"
    kb = None
    if bot_username:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="🚀 Пройти этот тест",
                url=f"https://t.me/{bot_username}?start=test_{test_id}")
        ]])

    return [InlineQueryResultArticle(
        id=f"share_{test_id}_{correct}",
        title=f"{emoji} Поделиться результатом {correct}/{total}",
        description=desc,
        input_message_content=InputTextMessageContent(
            message_text=msg_text, parse_mode="HTML"),
        reply_markup=kb,
    )]


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

    # Шеринг результата: share_<testid>_<correct>_<total>
    if q.lower().startswith("share_"):
        results = await _build_share_result(q, query.from_user)
        try:
            await query.answer(results, cache_time=1, is_personal=True)
        except Exception as e:
            log.warning("share inline answer failed: %s", e)
        return

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
