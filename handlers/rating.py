"""Хендлеры рейтинга."""
import logging

from aiogram import Router, F
from aiogram.types import CallbackQuery

import utils
from locales import t
from keyboards import rating_menu_kb, back_kb
from services import rating_service

router = Router(name="rating")
log = logging.getLogger(__name__)


@router.callback_query(F.data == "m:rating")
async def cb_rating_menu(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    pos, score = rating_service.user_overall_position(user['id'])
    if pos:
        header = (f"{t('rating_title', lang)}\n\n"
                  f"Ваша позиция: <b>#{pos}</b> ({score} очков)")
    else:
        header = t('rating_title', lang)
    try:
        await call.message.edit_text(header, reply_markup=rating_menu_kb(lang))
    except Exception:
        await call.message.answer(header, reply_markup=rating_menu_kb(lang))
    await call.answer()


@router.callback_query(F.data == "rating:overall")
async def cb_rating_overall(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    rows = rating_service.top_overall(20)
    text = "🏆 <b>Топ за всё время</b>\n\n" + rating_service.format_top(rows, lang)
    try:
        await call.message.edit_text(text, reply_markup=back_kb(lang, "m:rating"))
    except Exception:
        await call.message.answer(text, reply_markup=back_kb(lang, "m:rating"))
    await call.answer()


@router.callback_query(F.data == "rating:week")
async def cb_rating_week(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    rows = rating_service.top_week(20)
    text = "📅 <b>Топ за неделю</b>\n\n" + rating_service.format_top(rows, lang)
    try:
        await call.message.edit_text(text, reply_markup=back_kb(lang, "m:rating"))
    except Exception:
        await call.message.answer(text, reply_markup=back_kb(lang, "m:rating"))
    await call.answer()


@router.callback_query(F.data == "rating:daily")
async def cb_rating_daily(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    rows = rating_service.top_daily(20)
    if not rows:
        text = t("rating_empty", lang)
    else:
        lines = ["🔥 <b>Daily Top</b>", ""]
        medals = ['🥇', '🥈', '🥉']
        for i, r in enumerate(rows):
            prefix = medals[i] if i < 3 else f"{i+1}."
            name = r['first_name'] or r['username'] or str(r['tg_id'])
            lines.append(
                f"{prefix} {utils.escape_html(name)} — streak <b>{r['best_streak']}</b> "
                f"({r['daily_count']} дн.)")
        text = "\n".join(lines)
    try:
        await call.message.edit_text(text, reply_markup=back_kb(lang, "m:rating"))
    except Exception:
        await call.message.answer(text, reply_markup=back_kb(lang, "m:rating"))
    await call.answer()


@router.callback_query(F.data == "rating:school")
async def cb_rating_school(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    rows = rating_service.top_schools(20)
    text = "🏫 <b>Топ школ</b>\n\n" + rating_service.format_top(rows, lang)
    try:
        await call.message.edit_text(text, reply_markup=back_kb(lang, "m:rating"))
    except Exception:
        await call.message.answer(text, reply_markup=back_kb(lang, "m:rating"))
    await call.answer()
