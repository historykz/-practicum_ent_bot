"""Хендлеры профиля."""
import logging

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

import database as db
import utils
from locales import t, lang_label
from keyboards import profile_kb, language_kb, main_menu_kb, back_kb
from states import CommonStates
from services import referral_service

router = Router(name="profile")
log = logging.getLogger(__name__)


def _resolve_lang(user: dict) -> str:
    return user.get('language') or 'ru'


def _build_profile_text(user: dict, lang: str) -> str:
    name = user.get('first_name') or user.get('username') or "—"
    school = user.get('school') or "—"
    city = user.get('city') or "—"
    tests_done = db.fetchone(
        "SELECT COUNT(*) AS c FROM test_attempts WHERE user_id=? AND status='finished'",
        (user['id'],))['c']
    streak = user.get('current_streak') or 0
    best = user.get('best_streak') or 0
    refs = referral_service.count_referrals(user['id'])

    premium_info = utils.get_premium_info(user['id'])
    if premium_info:
        expires_at = premium_info.get('expires_at')
        if expires_at is None or expires_at == '':
            prem_line = f"{t('profile_premium_on', lang)} ({t('profile_premium_forever', lang)})"
        else:
            prem_line = f"{t('profile_premium_on', lang)} {t('profile_premium_until', lang)} {str(expires_at)[:10]}"
    else:
        prem_line = t('profile_premium_off', lang)

    lines = [
        t("profile_title", lang),
        "",
        f"{t('profile_name', lang)}: {utils.escape_html(name)}",
        f"{t('profile_lang', lang)}: {lang_label(lang)}",
        f"{t('profile_school', lang)}: {utils.escape_html(school)}",
        f"{t('profile_city', lang)}: {utils.escape_html(city)}",
        f"{t('profile_tests_done', lang)}: <b>{tests_done}</b>",
        f"{t('profile_streak', lang)}: <b>{streak}</b> 🔥",
        f"{t('profile_best_streak', lang)}: <b>{best}</b>",
        f"{t('profile_referrals', lang)}: <b>{refs}</b>",
        prem_line,
    ]
    return "\n".join(lines)


@router.callback_query(F.data == "m:profile")
async def cb_profile(call: CallbackQuery, user: dict):
    lang = _resolve_lang(user)
    # обновим из БД, чтобы получить актуальные стрейки
    u = utils.get_user_by_tg(call.from_user.id)
    text = _build_profile_text(u, lang)
    try:
        await call.message.edit_text(text, reply_markup=profile_kb(lang))
    except Exception:
        await call.message.answer(text, reply_markup=profile_kb(lang))
    await call.answer()


@router.callback_query(F.data == "profile:lang")
async def cb_profile_lang(call: CallbackQuery, state: FSMContext, user: dict):
    lang = _resolve_lang(user)
    try:
        await call.message.edit_text(t("choose_language", lang), reply_markup=language_kb())
    except Exception:
        await call.message.answer(t("choose_language", lang), reply_markup=language_kb())
    await state.set_state(CommonStates.choosing_language)
    await call.answer()


@router.callback_query(F.data == "profile:school")
async def cb_profile_school(call: CallbackQuery, state: FSMContext, user: dict):
    lang = _resolve_lang(user)
    await call.message.answer(t("set_school_ask", lang),
                              reply_markup=back_kb(lang, "m:profile"))
    await state.set_state(CommonStates.set_school)
    await call.answer()


@router.message(CommonStates.set_school)
async def msg_set_school(message: Message, state: FSMContext, user: dict):
    lang = _resolve_lang(user)
    txt = (message.text or "").strip()
    if not txt:
        return
    # Разделим по запятой
    if "," in txt:
        school, city = [p.strip() for p in txt.split(",", 1)]
    else:
        school, city = txt, ""
    db.execute("UPDATE users SET school=?, city=? WHERE tg_id=?",
               (school[:200], city[:100], message.from_user.id))
    await state.clear()
    await message.answer(t("set_school_done", lang),
                         reply_markup=main_menu_kb(lang, utils.is_admin(message.from_user.id)))
