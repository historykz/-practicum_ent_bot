"""Daily ENT хендлеры."""
import asyncio
import logging

from aiogram import Router, F
from aiogram.types import CallbackQuery

import config
import utils
from locales import t
from keyboards import daily_kb, back_kb, main_menu_kb
from services import daily_service, test_runner, rating_service

router = Router(name="daily")
log = logging.getLogger(__name__)


@router.callback_query(F.data == "m:daily")
async def cb_daily_menu(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    u = utils.get_user_by_tg(call.from_user.id)
    text = t("daily_card", lang,
             streak=u.get('current_streak', 0),
             best=u.get('best_streak', 0))
    try:
        await call.message.edit_text(text, reply_markup=daily_kb(lang))
    except Exception:
        await call.message.answer(text, reply_markup=daily_kb(lang))
    await call.answer()


@router.callback_query(F.data == "daily:start")
async def cb_daily_start(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    if call.message.chat.type != "private":
        await call.answer(t("personal_chat_only", lang), show_alert=True)
        return
    if daily_service.user_did_daily_today(user['id']):
        await call.answer(t("daily_already_done", lang), show_alert=True)
        return

    test_id = daily_service.create_daily_test(user['id'], lang)
    if not test_id:
        await call.answer(t("daily_no_questions", lang), show_alert=True)
        return

    # Создаём попытку на этом служебном тесте
    attempt_id = test_runner.create_attempt(
        user['id'], test_id, lang, group_id=None,
        started_by_user_id=user['id'])

    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer(t("test_start_warning", lang))
    await asyncio.sleep(2)
    await test_runner.send_current_question(call.bot, attempt_id, call.message.chat.id)
    await call.answer()


@router.callback_query(F.data == "daily:streak")
async def cb_daily_streak(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    u = utils.get_user_by_tg(call.from_user.id)
    text = (f"🔥 {t('streak_current', lang)}: <b>{u.get('current_streak', 0)}</b>\n"
            f"🏆 {t('streak_best', lang)}: <b>{u.get('best_streak', 0)}</b>")
    try:
        await call.message.edit_text(text, reply_markup=back_kb(lang, "m:daily"))
    except Exception:
        await call.message.answer(text, reply_markup=back_kb(lang, "m:daily"))
    await call.answer()


@router.callback_query(F.data == "daily:rating")
async def cb_daily_rating(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    rows = rating_service.top_daily(20)
    if not rows:
        text = t("rating_empty", lang)
    else:
        text = "📅 <b>Daily Top</b>\n\n"
        medals = ['🥇', '🥈', '🥉']
        for i, r in enumerate(rows):
            prefix = medals[i] if i < 3 else f"{i+1}."
            name = r['first_name'] or r['username'] or str(r['tg_id'])
            text += (f"{prefix} {utils.escape_html(name)} — "
                     f"streak <b>{r['best_streak']}</b>, дней: {r['daily_count']}\n")
    try:
        await call.message.edit_text(text, reply_markup=back_kb(lang, "m:daily"))
    except Exception:
        await call.message.answer(text, reply_markup=back_kb(lang, "m:daily"))
    await call.answer()
