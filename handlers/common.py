"""Общие хендлеры: /start, /cancel, /help, выбор языка, главное меню."""
import logging

from aiogram import Router, F, Bot
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

import config
import utils
from locales import t
from keyboards import language_kb, main_menu_kb, profile_kb, rating_menu_kb, daily_kb, duel_menu_kb
from states import CommonStates
from services import referral_service
from services import share_service

router = Router(name="common")
log = logging.getLogger(__name__)


def _resolve_lang(user: dict) -> str:
    return user.get('language') or 'ru'


@router.message(CommandStart(deep_link=True))
async def cmd_start_deep(message: Message, command: CommandObject, state: FSMContext, user: dict):
    """/start с параметром deep-link."""
    await state.clear()
    arg = (command.args or "").strip()
    lang = _resolve_lang(user)

    # Сохраняем приглашение в state для применения после выбора языка
    pending = {}
    if arg.startswith("ref_"):
        try:
            pending['inviter_tg_id'] = int(arg[4:])
        except ValueError:
            pass
    elif arg.startswith("test_"):
        try:
            pending['open_test_id'] = int(arg[5:])
        except ValueError:
            pass
    elif arg.startswith("note_"):
        try:
            pending['open_note_id'] = int(arg[5:])
        except ValueError:
            pass

    if pending:
        await state.update_data(pending=pending)

    # Если язык ещё не выбран — спросить
    if not user.get('language'):
        await message.answer(t("choose_language", lang), reply_markup=language_kb())
        await state.set_state(CommonStates.choosing_language)
        return

    await _apply_pending_and_show_menu(message, state, user)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, user: dict):
    await state.clear()
    lang = _resolve_lang(user)
    if not user.get('language'):
        await message.answer(t("choose_language", lang), reply_markup=language_kb())
        await state.set_state(CommonStates.choosing_language)
        return
    await message.answer(
        t("main_menu", lang),
        reply_markup=main_menu_kb(lang, utils.is_admin(message.from_user.id)),
    )


async def _apply_pending_and_show_menu(message: Message, state: FSMContext, user: dict):
    """Применить отложенные действия из deep-link после выбора языка."""
    lang = _resolve_lang(user)
    data = await state.get_data()
    pending = data.get('pending') or {}

    # Реферал
    inviter_tg = pending.get('inviter_tg_id')
    if inviter_tg and inviter_tg != message.from_user.id:
        bonus = referral_service.register_referral(inviter_tg, message.from_user.id)
        # Уведомим пригласившего, если бонус есть
        if bonus:
            try:
                inviter = utils.get_user_by_tg(inviter_tg)
                if inviter:
                    inv_lang = inviter.get('language') or 'ru'
                    await message.bot.send_message(
                        inviter_tg, t("ref_bonus_granted", inv_lang, bonus=bonus))
            except Exception:
                pass

    # Открыть тест
    open_test_id = pending.get('open_test_id')
    if open_test_id:
        from handlers.user import show_test_card
        await show_test_card(message.bot, message.chat.id, message.from_user.id,
                             open_test_id, lang)
        await state.update_data(pending=None)
        return

    # Открыть конспект
    open_note_id = pending.get('open_note_id')
    if open_note_id:
        from handlers.notes import show_note_card
        await show_note_card(message.bot, message.chat.id, message.from_user.id,
                             open_note_id, lang)
        await state.update_data(pending=None)
        return

    await message.answer(
        t("main_menu", lang),
        reply_markup=main_menu_kb(lang, utils.is_admin(message.from_user.id)),
    )


@router.callback_query(F.data.startswith("setlang:"))
async def cb_set_language(call: CallbackQuery, state: FSMContext, user: dict):
    lang = call.data.split(":")[1]
    if lang not in ("ru", "kz"):
        await call.answer()
        return
    utils.set_user_lang(call.from_user.id, lang)
    user['language'] = lang
    await call.answer(t("language_chosen", lang), show_alert=False)
    # Если был pending — применить
    data = await state.get_data()
    pending = data.get('pending') or {}
    try:
        await call.message.delete()
    except Exception:
        pass

    if pending:
        # Применяем
        inviter_tg = pending.get('inviter_tg_id')
        if inviter_tg and inviter_tg != call.from_user.id:
            bonus = referral_service.register_referral(inviter_tg, call.from_user.id)
            if bonus:
                try:
                    inviter = utils.get_user_by_tg(inviter_tg)
                    if inviter:
                        inv_lang = inviter.get('language') or 'ru'
                        await call.bot.send_message(
                            inviter_tg, t("ref_bonus_granted", inv_lang, bonus=bonus))
                except Exception:
                    pass

        open_test_id = pending.get('open_test_id')
        if open_test_id:
            from handlers.user import show_test_card
            await show_test_card(call.bot, call.message.chat.id, call.from_user.id,
                                 open_test_id, lang)
            await state.update_data(pending=None)
            await state.set_state(None)
            return

        open_note_id = pending.get('open_note_id')
        if open_note_id:
            from handlers.notes import show_note_card
            await show_note_card(call.bot, call.message.chat.id, call.from_user.id,
                                 open_note_id, lang)
            await state.update_data(pending=None)
            await state.set_state(None)
            return

    await call.message.answer(
        t("main_menu", lang),
        reply_markup=main_menu_kb(lang, utils.is_admin(call.from_user.id)),
    )
    await state.set_state(None)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, user: dict):
    await state.clear()
    lang = _resolve_lang(user)
    await message.answer(t("cancelled", lang),
                         reply_markup=main_menu_kb(lang, utils.is_admin(message.from_user.id)))


@router.callback_query(F.data == "cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext, user: dict):
    await state.clear()
    lang = _resolve_lang(user)
    try:
        await call.message.edit_text(
            t("main_menu", lang),
            reply_markup=main_menu_kb(lang, utils.is_admin(call.from_user.id)),
        )
    except Exception:
        await call.message.answer(
            t("main_menu", lang),
            reply_markup=main_menu_kb(lang, utils.is_admin(call.from_user.id)),
        )
    await call.answer()


@router.callback_query(F.data == "m:menu")
async def cb_main_menu(call: CallbackQuery, state: FSMContext, user: dict):
    await state.clear()
    lang = _resolve_lang(user)
    try:
        await call.message.edit_text(
            t("main_menu", lang),
            reply_markup=main_menu_kb(lang, utils.is_admin(call.from_user.id)),
        )
    except Exception:
        await call.message.answer(
            t("main_menu", lang),
            reply_markup=main_menu_kb(lang, utils.is_admin(call.from_user.id)),
        )
    await call.answer()


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext, user: dict):
    await state.clear()
    lang = _resolve_lang(user)
    await message.answer(
        t("main_menu", lang),
        reply_markup=main_menu_kb(lang, utils.is_admin(message.from_user.id)),
    )


@router.message(Command("help"))
async def cmd_help(message: Message, user: dict):
    lang = _resolve_lang(user)
    await message.answer(t("help_text", lang), reply_markup=main_menu_kb(lang, utils.is_admin(message.from_user.id)))


@router.callback_query(F.data == "m:help")
async def cb_help(call: CallbackQuery, user: dict):
    lang = _resolve_lang(user)
    try:
        await call.message.edit_text(t("help_text", lang),
                                     reply_markup=main_menu_kb(lang, utils.is_admin(call.from_user.id)))
    except Exception:
        await call.message.answer(t("help_text", lang))
    await call.answer()


@router.callback_query(F.data == "m:support")
async def cb_support(call: CallbackQuery, user: dict):
    lang = _resolve_lang(user)
    try:
        await call.message.edit_text(t("support_text", lang, manager=config.MANAGER_USERNAME),
                                     reply_markup=main_menu_kb(lang, utils.is_admin(call.from_user.id)))
    except Exception:
        await call.message.answer(t("support_text", lang, manager=config.MANAGER_USERNAME))
    await call.answer()


@router.callback_query(F.data == "m:invite")
async def cb_invite(call: CallbackQuery, user: dict):
    lang = _resolve_lang(user)
    link = share_service.build_ref_link(call.from_user.id)
    count = referral_service.count_referrals(user['id'])
    try:
        await call.message.edit_text(
            t("ref_invite_text", lang, link=link, count=count),
            reply_markup=main_menu_kb(lang, utils.is_admin(call.from_user.id)),
        )
    except Exception:
        await call.message.answer(
            t("ref_invite_text", lang, link=link, count=count))
    await call.answer()
