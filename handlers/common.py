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

    # === Запуск теста в группе (через ?startgroup=launch_X) ===
    if arg.startswith("launch_") and message.chat.type in ("group", "supergroup"):
        try:
            test_id = int(arg[7:])
        except ValueError:
            return
        # Проверяем что добавивший — админ
        import utils as _utils
        if not _utils.is_admin(message.from_user.id):
            try:
                await message.reply("⛔ Запустить тест может только администратор бота.")
            except Exception:
                pass
            return
        # Запускаем тест в этой группе
        from services import test_runner, group_quiz_service
        import database as _db
        test = test_runner.get_test(test_id)
        if not test:
            try:
                await message.reply("❌ Тест не найден.")
            except Exception:
                pass
            return
        # Записываем группу
        _db.execute(
            """INSERT OR IGNORE INTO known_groups (chat_id, title, type, added_by, seen_at)
               VALUES (?,?,?,?, CURRENT_TIMESTAMP)""",
            (message.chat.id, message.chat.title or "",
             message.chat.type, message.from_user.id))
        ok, key, gq_id = await group_quiz_service.start_lobby(
            message.bot, dict(test), message.chat.id, message.from_user.id)
        if not ok:
            try:
                if key == "already_running":
                    await message.reply("⚠️ В этой группе уже идёт тест. Сначала /stop.")
                else:
                    await message.reply(f"❌ Не удалось запустить: {key}")
            except Exception:
                pass
        return

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
    # В группах не показываем главное меню
    if message.chat.type in ("group", "supergroup"):
        try:
            await message.reply(
                "👋 Бот добавлен в группу. Для запуска теста админ может "
                "использовать команду /launch_&lt;test_id&gt;",
                parse_mode="HTML")
        except Exception:
            pass
        return

    # ВСЕГДА спрашиваем язык в начале — так договаривались
    await message.answer(
        "👋 <b>Привет! Сәлем!</b>\n\n"
        "Я твой помощник по ЕНТ.\n"
        "Сначала выбери язык — на нём бот будет с тобой общаться.\n\n"
        "🌐 <b>Выберите язык</b> · <b>Тілді таңдаңыз</b>\n\n"
        "👇 Тапни на нужный язык:",
        reply_markup=language_kb(), parse_mode="HTML")
    await state.set_state(CommonStates.choosing_language)


@router.message(Command("restart"))
async def cmd_restart(message: Message, state: FSMContext, user: dict):
    """Перезапуск — спрашиваем язык заново."""
    await state.clear()
    await message.answer(
        "🌐 <b>Выберите язык</b> · <b>Тілді таңдаңыз</b>",
        reply_markup=language_kb(), parse_mode="HTML")
    await state.set_state(CommonStates.choosing_language)


async def _apply_pending_and_show_menu(message: Message, state: FSMContext, user: dict):
    """Применить отложенные действия из deep-link после выбора языка."""
    lang = _resolve_lang(user)
    data = await state.get_data()
    pending = data.get('pending') or {}

    # Реферал
    inviter_tg = pending.get('inviter_tg_id')
    if inviter_tg and inviter_tg != message.from_user.id:
        referral_service.register_referral(inviter_tg, message.from_user.id)
        try:
            # Проверим, подписан ли приглашённый на обязательный канал
            verified = await referral_service.verify_referral(message.bot, message.from_user.id)
            if verified:
                inviter = utils.get_user_by_tg(inviter_tg)
                if inviter:
                    inv_lang = inviter.get('language') or 'ru'
                    cnt = referral_service.count_verified_referrals(inviter['id'])
                    await message.bot.send_message(
                        inviter_tg,
                        f"🎁 У вас новое подтверждённое приглашение! Всего: <b>{cnt}/10</b>")
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
            referral_service.register_referral(inviter_tg, call.from_user.id)
            try:
                verified = await referral_service.verify_referral(call.bot, call.from_user.id)
                if verified:
                    inviter = utils.get_user_by_tg(inviter_tg)
                    if inviter:
                        cnt = referral_service.count_verified_referrals(inviter['id'])
                        await call.bot.send_message(
                            inviter_tg,
                            f"🎁 У вас новое подтверждённое приглашение! Всего: <b>{cnt}/10</b>")
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

    # Просто главное меню — без длинного intro
    await state.set_state(None)
    await state.clear()

    # Объяснение про смену языка — на двух языках, чтобы юзер всегда понял
    chosen_msg = (
        ("✅ <b>Язык выбран: Русский</b>" if lang == "ru"
         else "✅ <b>Тіл таңдалды: Қазақша</b>") +
        "\n\n"
        "🇷🇺 <b>Если захочешь сменить язык:</b>\n"
        "«👤 Профиль» → «🌐 Сменить язык»\n\n"
        "🇰🇿 <b>Тілді ауыстырғың келсе:</b>\n"
        "«👤 Профиль» → «🌐 Тілді ауыстыру»")
    try:
        await call.message.answer(chosen_msg, parse_mode="HTML")
    except Exception:
        pass

    await call.message.answer(
        t("main_menu", lang),
        reply_markup=main_menu_kb(lang, utils.is_admin(call.from_user.id)),
        parse_mode="HTML",
    )


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
    text = t("help_text", lang, manager=config.MANAGER_USERNAME)
    try:
        await call.message.edit_text(text, parse_mode="HTML",
                                       reply_markup=main_menu_kb(lang, utils.is_admin(call.from_user.id)),
                                       disable_web_page_preview=True)
    except Exception:
        await call.message.answer(text, parse_mode="HTML",
                                    reply_markup=main_menu_kb(lang, utils.is_admin(call.from_user.id)),
                                    disable_web_page_preview=True)
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
    verified = referral_service.count_verified_referrals(user['id'])
    total = referral_service.count_referrals(user['id'])
    text = (
        f"🎁 <b>Пригласи друзей — получи доступ к платным тестам!</b>\n\n"
        f"Условия:\n"
        f"• Друг должен открыть бота по твоей ссылке\n"
        f"• Подписаться на обязательный канал\n\n"
        f"Когда наберёшь <b>10 подтверждённых</b> приглашений — получишь доступ.\n\n"
        f"📊 Твой прогресс: <b>{verified}/10</b>\n"
        f"(всего перешло по ссылке: {total})\n\n"
        f"Твоя ссылка:\n{link}"
    )
    try:
        await call.message.edit_text(
            text,
            reply_markup=main_menu_kb(lang, utils.is_admin(call.from_user.id)),
            disable_web_page_preview=True,
        )
    except Exception:
        await call.message.answer(text,
            reply_markup=main_menu_kb(lang, utils.is_admin(call.from_user.id)),
            disable_web_page_preview=True)
    await call.answer()
