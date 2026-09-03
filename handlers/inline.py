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


async def _build_duel_invite(q: str, from_user):
    """
    Построить inline-результат приглашения на дуэль.
    q = 'duel:<code>'. Кнопка ведёт друга по deep-link на код дуэли.
    """
    import database as db
    from aiogram.types import (InlineQueryResultArticle,
                                InputTextMessageContent,
                                InlineKeyboardMarkup, InlineKeyboardButton)
    try:
        code = q.split(":", 1)[1].strip()
    except Exception:
        return []
    if not code:
        return []

    inv = db.fetchone("SELECT * FROM duel_invites WHERE code=?", (code,))
    cat_name = "все разделы"
    if inv and inv.get('category_id'):
        c = db.fetchone("SELECT name FROM test_categories WHERE id=?",
                        (inv['category_id'],))
        if c:
            cat_name = c['name']

    bot_username = getattr(config, 'BOT_USERNAME', '') or ''
    if not bot_username:
        try:
            me = await from_user.bot.get_me()
            bot_username = me.username or ''
        except Exception:
            pass

    link = f"https://t.me/{bot_username}?start=duel_{code}"
    inviter = from_user.first_name or from_user.username or "Игрок"

    msg_text = (f"⚔️ <b>{utils.escape_html(inviter)} вызывает на дуэль!</b>\n"
                f"📚 Раздел: {cat_name}\n\n"
                f"Кто быстрее и точнее ответит? 😎")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⚔️ Принять вызов", url=link)
    ]])

    return [InlineQueryResultArticle(
        id=f"duel_{code}",
        title="⚔️ Пригласить на дуэль",
        description=f"Раздел: {cat_name} — отправь другу вызов!",
        input_message_content=InputTextMessageContent(
            message_text=msg_text, parse_mode="HTML"),
        reply_markup=kb,
    )]



async def _build_referral_invite(from_user):
    """Inline-приглашение с реф-ссылкой (для набора друзей)."""
    from aiogram.types import (InlineQueryResultArticle,
                                InputTextMessageContent,
                                InlineKeyboardMarkup, InlineKeyboardButton)
    bot_username = getattr(config, 'BOT_USERNAME', '') or ''
    if not bot_username:
        try:
            me = await from_user.bot.get_me()
            bot_username = me.username or ''
        except Exception:
            pass
    link = f"https://t.me/{bot_username}?start=ref_{from_user.id}"
    inviter = from_user.first_name or from_user.username or "Друг"
    msg_text = (
        f"\U0001F4DA <b>\u0413\u043e\u0442\u043e\u0432\u0438\u0448\u044c\u0441\u044f \u043a \u0415\u041d\u0422?</b>\n\n"
        f"{utils.escape_html(inviter)} \u043f\u0440\u0438\u0433\u043b\u0430\u0448\u0430\u0435\u0442 \u0442\u0435\u0431\u044f \u0432 \u0431\u043e\u0442\u0430 \u0434\u043b\u044f \u043f\u043e\u0434\u0433\u043e\u0442\u043e\u0432\u043a\u0438!\n\n"
        f"\u2705 \u0422\u0435\u0441\u0442\u044b \u043f\u043e \u0432\u0441\u0435\u043c \u043f\u0440\u0435\u0434\u043c\u0435\u0442\u0430\u043c\n"
        f"\u2694\ufe0f \u0414\u0443\u044d\u043b\u0438 \u0441 \u0434\u0440\u0443\u0433\u0438\u043c\u0438\n"
        f"\U0001F3C6 \u0420\u0435\u0439\u0442\u0438\u043d\u0433 \u0438 \u043f\u0440\u043e\u0433\u0440\u0435\u0441\u0441\n\n"
        f"\U0001F447 \u0416\u043c\u0438 \u0438 \u043d\u0430\u0447\u0438\u043d\u0430\u0439!")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="\U0001F4DA \u041d\u0430\u0447\u0430\u0442\u044c", url=link)
    ]])
    return [InlineQueryResultArticle(
        id=f"ref_{from_user.id}",
        title="\U0001F4E4 \u041f\u0440\u0438\u0433\u043b\u0430\u0441\u0438\u0442\u044c \u0434\u0440\u0443\u0433\u0430",
        description="\u0417\u0430 10 \u0434\u0440\u0443\u0437\u0435\u0439 \u2014 \u041f\u0440\u0435\u043c\u0438\u0443\u043c!",
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

    # Приглашение на дуэль: duel:<code>
    if q.lower().startswith("duel:"):
        results = await _build_duel_invite(q, query.from_user)
        try:
            await query.answer(results, cache_time=1, is_personal=True)
        except Exception as e:
            log.warning("duel inline answer failed: %s", e)
        return

    # Реферальное приглашение: ref
    if q.lower() == "ref" or q.lower().startswith("ref"):
        results = await _build_referral_invite(query.from_user)
        try:
            await query.answer(results, cache_time=1, is_personal=True)
        except Exception as e:
            log.warning("ref inline answer failed: %s", e)
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
