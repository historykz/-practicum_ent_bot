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
    # Отложенная выдача: если админ внёс этого юзера заранее — применяем
    try:
        from services import pending_access_service as _pas
        await _pas.activate_for_user(message.bot, user['tg_id'],
                                      user.get('username'), user['id'])
    except Exception:
        pass
    await state.clear()
    arg = (command.args or "").strip()
    lang = _resolve_lang(user)

    # === Дуэль по ссылке-приглашению (start=duel_<code>) ===
    if arg.startswith("duel_") and message.chat.type == "private":
        code = arg[5:]
        from services import duel_service
        status = await duel_service.join_invite(
            message.bot, code, message.from_user.id, message.chat.id, lang)
        if status == 'started':
            # дуэль запустилась — сообщения шлёт сам сервис
            return
        elif status == 'already_full':
            # Третий лишний — ведём в бота, предлагаем свою дуэль
            import config as _cfg
            bot_un = getattr(_cfg, 'BOT_USERNAME', '') or ''
            if not bot_un:
                try:
                    bot_un = (await message.bot.get_me()).username
                except Exception:
                    bot_un = ''
            new_code = await duel_service.create_invite(
                message.from_user.id, message.chat.id, lang)
            new_link = f"https://t.me/{bot_un}?start=duel_{new_code}"
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⚔️ Принять участие", url=new_link)
            ]])
            await message.answer(
                "⚔️ Эта дуэль уже идёт (2 игрока).\n\n"
                "Хочешь свою? Перешли друзьям эту ссылку 👇" if lang == "ru"
                else "⚔️ Бұл дуэль басталып қойды (2 ойыншы).\n\n"
                     "Өзіңдікі керек пе? Достарыңа мына сілтемені жібер 👇")
            await message.answer(
                "⚔️ Я приглашаю тебя на дуэль!\nЗаходи, если не струсил 😏",
                reply_markup=kb)
            return
        elif status == 'host_waiting':
            await message.answer(
                "⏳ Ты создатель этой дуэли. Жди когда соперник "
                "нажмёт «Принять участие»!" if lang == "ru"
                else "⏳ Сен осы дуэльдің авторысың. Қарсыластың "
                     "«Қатысу» басуын күт!")
            return
        else:  # not_found
            await message.answer(
                "⚠️ Эта дуэль не найдена или истекла. "
                "Создай свою через «⚔️ Дуэль» в меню." if lang == "ru"
                else "⚠️ Дуэль табылмады немесе мерзімі бітті. "
                     "Мәзірден өзіңдікін жаса.")
            return

    # === Сразу открыть предложение Премиума (start=premium) ===
    # Используется кнопкой "Приобрести" с сайта — карточки/заучивание за Премиум.
    if arg == "premium" and message.chat.type == "private":
        if not user.get('language'):
            await state.update_data(pending={'open_premium': True})
            await message.answer(t("choose_language", lang), reply_markup=language_kb())
            await state.set_state(CommonStates.choosing_language)
            return
        from handlers.premium import build_premium_offer
        text, markup = build_premium_offer(user)
        await message.answer(text, reply_markup=markup, parse_mode="HTML")
        return

    # === Конспект урока (start=lesson_<id>) — выдача в личку с водяным знаком ===
    if arg.startswith("lesson_") and message.chat.type == "private":
        try:
            _lid = int(arg[7:])
        except ValueError:
            _lid = 0
        if _lid:
            from handlers.lesson_notes import send_lesson_note
            await send_lesson_note(message.bot, message.chat.id, message.from_user.id, _lid)
            return

    # === Принять подарок (start=gift_<code>) ===
    if arg.startswith("gift_") and message.chat.type == "private":
        from handlers.payments import claim_gift
        await claim_gift(message, arg[5:])
        return

    # === Запуск теста в группе (через ?startgroup=launch_X) ===
    if arg.startswith("launch_") and message.chat.type in ("group", "supergroup"):
        try:
            test_id = int(arg[7:])
        except ValueError:
            return
        import utils as _utils
        # Разрешаем: админу бота, анонимному админу чата, или админу чата
        allowed = False
        if _utils.is_anonymous_chat_admin(message):
            allowed = True
        elif message.from_user and _utils.is_admin(message.from_user.id):
            allowed = True
        elif message.from_user:
            try:
                member = await message.bot.get_chat_member(
                    message.chat.id, message.from_user.id)
                if member.status in ("administrator", "creator"):
                    allowed = True
            except Exception:
                pass
        if not allowed:
            try:
                await message.reply(
                    "⛔ Запустить тест может только админ чата или бота.")
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
        _added_by = message.from_user.id if message.from_user else 0
        _db.execute(
            """INSERT OR IGNORE INTO known_groups (chat_id, title, type, added_by, seen_at)
               VALUES (?,?,?,?, CURRENT_TIMESTAMP)""",
            (message.chat.id, message.chat.title or "",
             message.chat.type, _added_by))
        ok, key, gq_id = await group_quiz_service.start_lobby(
            message.bot, dict(test), message.chat.id, _added_by)
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


@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, user: dict):
    # Отложенная выдача: если админ внёс этого юзера заранее — применяем
    try:
        from services import pending_access_service as _pas
        await _pas.activate_for_user(message.bot, user['tg_id'],
                                      user.get('username'), user['id'])
    except Exception:
        pass
    await state.clear()
    # Закрыть незавершённые сессии режимов
    try:
        from handlers import flashcards as _fc, learning as _ln
        _fc.close_user_sessions(message.from_user.id)
        _ln.close_user_sessions(message.from_user.id)
    except Exception:
        pass
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


@router.message(Command("restart"), F.chat.type == "private")
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
        # Новая система: сохраняем навсегда + проверим подписку
        try:
            from services import referral_reward_service as _rrs
            _rrs.record_invite(inviter_tg, message.from_user.id)
            # Проверим подписку и начислим если подписан
            counted = await _rrs.check_and_update_subscription(
                message.bot, message.from_user.id)
            if counted:
                await _rrs.maybe_give_reward(message.bot, inviter_tg)
        except Exception:
            pass
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

    # Сразу открыть предложение Премиума
    if pending.get('open_premium'):
        from handlers.premium import build_premium_offer
        text, markup = build_premium_offer(user)
        await message.answer(text, reply_markup=markup, parse_mode="HTML")
        await state.update_data(pending=None)
        return

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

        if pending.get('open_premium'):
            from handlers.premium import build_premium_offer
            text, markup = build_premium_offer(user)
            await call.bot.send_message(call.message.chat.id, text,
                                         reply_markup=markup, parse_mode="HTML")
            await state.update_data(pending=None)
            await state.set_state(None)
            return

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

    # Если профильные предметы ещё не выбраны и есть профильные категории —
    # показываем экран выбора. Иначе сразу меню.
    from handlers import profile_subjects as _ps
    has_subjects = utils.has_profile_subjects(call.from_user.id)
    optional_cats = _ps.get_optional_categories()
    if not has_subjects and optional_cats:
        await _ps.show_subjects_screen(call, state, from_profile=False)
        return

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


@router.message(Command("menu"), F.chat.type == "private")
async def cmd_menu(message: Message, state: FSMContext, user: dict):
    await state.clear()
    lang = _resolve_lang(user)
    await message.answer(
        t("main_menu", lang),
        reply_markup=main_menu_kb(lang, utils.is_admin(message.from_user.id)),
    )


@router.message(Command("help"), F.chat.type == "private")
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
