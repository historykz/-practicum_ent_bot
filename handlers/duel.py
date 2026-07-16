"""Хендлеры дуэлей 1 на 1."""
import logging

from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.fsm.context import FSMContext

import utils
from locales import t
from keyboards import duel_menu_kb, duel_cancel_kb, back_kb, main_menu_kb
from states import DuelStates
import database as db
from services import duel_service

router = Router(name="duel")
log = logging.getLogger(__name__)


@router.callback_query(F.data == "m:duel")
async def cb_duel_menu(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    if call.message.chat.type != "private":
        await call.answer(t("personal_chat_only", lang), show_alert=True)
        return
    try:
        await call.message.edit_text(t("duel_menu", lang),
                                       reply_markup=duel_menu_kb(lang),
                                       parse_mode="HTML")
    except Exception:
        await call.message.answer(t("duel_menu", lang),
                                    reply_markup=duel_menu_kb(lang),
                                    parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "duel:invite")
async def cb_duel_invite(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    if call.message.chat.type != "private":
        await call.answer(t("personal_chat_only", lang), show_alert=True)
        return
    await call.answer()
    # Сначала выбор раздела
    cats = duel_service.get_duel_categories(lang)
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    for c in cats:
        emoji = c.get('emoji') or '📚'
        kb.button(text=f"{emoji} {c['name']}",
                  callback_data=f"duelcat:{c['id']}")
    # Все разделы вместе
    kb.button(text="🎲 Все разделы (вперемешку)", callback_data="duelcat:all")
    kb.button(text="↩️ Назад", callback_data="duel:menu")
    kb.adjust(1)
    if not cats:
        await call.message.answer(
            "⚠️ Пока нет бесплатных тестов для дуэли. Попроси админа добавить."
            if lang == "ru" else
            "⚠️ Дуэль үшін тегін тесттер жоқ. Әкімшіден сұра.")
        return
    await call.message.answer(
        "⚔️ <b>Дуэль</b>\n\nПо какому разделу сыграем?\n"
        "<i>Вопросы будут только из выбранного раздела.</i>"
        if lang == "ru" else
        "⚔️ <b>Дуэль</b>\n\nҚай бөлім бойынша ойнаймыз?",
        reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("duelcat:"))
async def cb_duel_category_chosen(call: CallbackQuery, user: dict):
    """Раздел выбран — создаём ссылку-приглашение."""
    lang = user.get('language') or 'ru'
    arg = call.data.split(":")[1]
    category_id = None if arg == "all" else int(arg)
    await call.answer()

    code = await duel_service.create_invite(
        call.from_user.id, call.message.chat.id, lang, category_id=category_id)

    # Название раздела
    cat_name = "все разделы"
    if category_id:
        c = db.fetchone("SELECT name FROM test_categories WHERE id=?", (category_id,))
        cat_name = c['name'] if c else "раздел"

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    # Кнопка «Пригласить друга» — открывает выбор чата/друга через инлайн
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="👥 Пригласить друга на дуэль",
            switch_inline_query=f"duel:{code}")],
        [InlineKeyboardButton(
            text="❌ Отменить дуэль",
            callback_data=f"duelcancel:{code}")],
    ])
    await call.message.answer(
        f"⚔️ <b>Дуэль создана!</b>\n"
        f"📚 Раздел: {cat_name}\n\n"
        f"Нажми кнопку ниже — выбери друга или чат, "
        f"бот отправит ему вызов со ссылкой.\n\n"
        f"⏳ Первый кто примет — сыграет с тобой!"
        if lang == "ru" else
        f"⚔️ <b>Дуэль құрылды!</b>\n"
        f"📚 Бөлім: {cat_name}\n\n"
        f"Төмендегі батырманы бас — досыңды таңда.\n\n"
        f"⏳ Бірінші қабылдаған сенімен ойнайды!",
        reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("duelcancel:"))
async def cb_duel_invite_cancel(call: CallbackQuery, user: dict):
    code = call.data.split(":")[1]
    duel_service.cleanup_invite(code)
    await call.answer("Дуэль отменена")
    try:
        await call.message.edit_text("❌ Дуэль отменена.")
    except Exception:
        pass



@router.callback_query(F.data == "duel:fast")
async def cb_duel_fast_menu(call: CallbackQuery, state: FSMContext, user: dict):
    """Быстрая дуэль — сначала выбор раздела (профильные предметы юзера)."""
    lang = user.get('language') or 'ru'
    active = await duel_service.get_active_duel_for(user['id'])
    if active:
        await call.answer(t("duel_already_in", lang), show_alert=True)
        return
    await call.answer()

    # Профильные предметы юзера
    profile_ids = []
    if user.get('profile_subjects'):
        try:
            profile_ids = [int(x) for x in str(user['profile_subjects']).split(',') if x.strip()]
        except Exception:
            profile_ids = []

    all_cats = duel_service.get_duel_categories(lang)
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()

    # Сначала профильные разделы юзера (если есть вопросы)
    shown = set()
    for c in all_cats:
        if c['id'] in profile_ids:
            emoji = c.get('emoji') or '📚'
            kb.button(text=f"{emoji} {c['name']}", callback_data=f"fastcat:{c['id']}")
            shown.add(c['id'])
    # Остальные разделы
    for c in all_cats:
        if c['id'] not in shown:
            emoji = c.get('emoji') or '📚'
            kb.button(text=f"{emoji} {c['name']}", callback_data=f"fastcat:{c['id']}")
    # Все вместе
    kb.button(text="🎲 Все разделы (вперемешку)", callback_data="fastcat:all")
    kb.button(text="↩️ Назад", callback_data="m:duel")
    kb.adjust(1)

    if not all_cats:
        await call.message.answer(
            "⚠️ Пока нет бесплатных тестов для дуэли." if lang == "ru"
            else "⚠️ Дуэль үшін тегін тесттер жоқ.")
        return

    await call.message.answer(
        "⚡️ <b>Быстрая дуэль</b>\n\nПо какому разделу играем?\n"
        "<i>Бот подберёт соперника, вопросы только из этого раздела.</i>"
        if lang == "ru" else
        "⚡️ <b>Жылдам дуэль</b>\n\nҚай бөлім бойынша?",
        reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("fastcat:"))
async def cb_fast_category(call: CallbackQuery, state: FSMContext, user: dict):
    """Раздел для быстрой дуэли выбран — ищем пару."""
    lang = user.get('language') or 'ru'
    arg = call.data.split(":")[1]
    category_id = None if arg == "all" else int(arg)
    await cb_duel_fast(call, state, user, category_id=category_id)


async def cb_duel_fast(call: CallbackQuery, state: FSMContext, user: dict,
                        category_id=None):
    lang = user.get('language') or 'ru'
    active = await duel_service.get_active_duel_for(user['id'])
    if active:
        await call.answer(t("duel_already_in", lang), show_alert=True)
        return

    duel_id = await duel_service.join_queue(call.bot, user['id'],
                                             call.message.chat.id, lang,
                                             category_id=category_id)
    if duel_id:
        try:
            await call.message.delete()
        except Exception:
            pass
        await state.set_state(DuelStates.in_duel)
    else:
        try:
            await call.message.edit_text(t("duel_searching", lang),
                                         reply_markup=duel_cancel_kb(lang))
        except Exception:
            await call.message.answer(t("duel_searching", lang),
                                      reply_markup=duel_cancel_kb(lang))
        await state.set_state(DuelStates.searching)
    await call.answer()


@router.callback_query(F.data == "duel:subject")
async def cb_duel_subject(call: CallbackQuery, state: FSMContext, user: dict):
    """Дуэль по предмету = быстрая дуэль с выбором раздела."""
    await cb_duel_fast_menu(call, state, user)


@router.callback_query(F.data == "duel:cancel")
async def cb_duel_cancel(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    removed = await duel_service.leave_queue(user['id'])
    if not removed:
        # Может, активная дуэль
        active = await duel_service.get_active_duel_for(user['id'])
        if active:
            await duel_service.abort_duel_by_user(call.bot, user['id'])
    await state.clear()
    try:
        await call.message.edit_text(t("duel_cancelled", lang),
                                     reply_markup=main_menu_kb(lang, utils.is_admin(call.from_user.id)))
    except Exception:
        await call.message.answer(t("duel_cancelled", lang),
                                  reply_markup=main_menu_kb(lang, utils.is_admin(call.from_user.id)))
    await call.answer()


@router.callback_query(F.data == "duel:history")
async def cb_duel_history(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    stats = duel_service.get_duels_stats(user['id'])
    text = t("duel_history", lang,
             wins=stats['wins'], losses=stats['losses'], total=stats['total'])
    try:
        await call.message.edit_text(text, reply_markup=back_kb(lang, "m:duel"))
    except Exception:
        await call.message.answer(text, reply_markup=back_kb(lang, "m:duel"))
    await call.answer()


@router.callback_query(F.data.startswith("duelans:"))
async def cb_duel_answer(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    parts = call.data.split(":")
    if len(parts) != 4:
        await call.answer()
        return
    try:
        duel_id = int(parts[1])
        question_id = int(parts[2])
        option_id = int(parts[3])
    except ValueError:
        await call.answer()
        return

    result = await duel_service.process_duel_answer(
        call.bot, duel_id, user['id'], question_id, option_id)

    if result == 'ok':
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await call.answer(t("answer_recorded", lang))
    elif result == 'already':
        await call.answer(t("already_answered", lang), show_alert=True)
    elif result == 'old':
        await call.answer(t("old_button", lang), show_alert=True)
    else:
        await call.answer(t("error_generic", lang), show_alert=True)


# ===================== ЗАВЕРШЕНИЕ ДУЭЛИ (КНОПКА) =====================
# Обработка ответов через Quiz Poll подключена в handlers/quiz.py::on_poll_answer
# (централизованно), чтобы не конфликтовать с групповыми тестами.


@router.callback_query(F.data.startswith("duelquit:"))
async def cb_duel_quit(call: CallbackQuery, user: dict):
    """Завершить дуэль (сдаться)."""
    lang = user.get('language') or 'ru'
    try:
        duel_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    ok = await duel_service.quit_duel(call.bot, duel_id, call.from_user.id)
    if ok:
        await call.answer("Дуэль завершена." if lang == "ru" else "Дуэль аяқталды.",
                          show_alert=True)
    else:
        await call.answer("Дуэль уже завершена." if lang == "ru"
                          else "Дуэль аяқталған.", show_alert=True)
