"""Хендлеры пользователя: каталог тестов, карточка теста, запуск, шеринг."""
import asyncio
import logging

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import database as db
import utils
from locales import t
from keyboards import (tests_list_kb, test_card_kb, paid_test_kb, subscription_kb,
                       back_kb, main_menu_kb)
from services import test_runner, subscription_service, share_service, group_quiz_service

router = Router(name="user")
log = logging.getLogger(__name__)


def _resolve_lang(user: dict) -> str:
    return user.get('language') or 'ru'


def _ttype_label(ttype: str, lang: str) -> str:
    return t(f"ttype_{ttype}", lang)


def _access_label(test: dict, lang: str) -> str:
    if test['is_paid']:
        return t("access_paid", lang)
    return t("access_free", lang)


def _list_active_tests(language: str, ttype_filter: str = None) -> list[dict]:
    if ttype_filter:
        rows = db.fetchall(
            """SELECT * FROM tests WHERE status='active' AND language=? AND test_type=?
                AND COALESCE(is_private,0)=0
                ORDER BY id DESC""", (language, ttype_filter))
    else:
        rows = db.fetchall(
            """SELECT * FROM tests WHERE status='active' AND language=?
                AND COALESCE(is_private,0)=0
                AND test_type NOT IN ('daily','duel','tournament') ORDER BY id DESC""",
            (language,))
    return [dict(r) for r in rows]


@router.callback_query(F.data == "m:tests")
async def cb_tests_menu(call: CallbackQuery, user: dict):
    """Каталог тестов — показывает разделы."""
    lang = _resolve_lang(user)
    from handlers import categories, private_access as _pa

    cats = categories.get_categories()
    private_tests = _pa.list_user_private_tests(call.from_user.id)
    tests_no_cat = categories.get_tests_without_category(lang)

    if lang == "kz":
        text = ("📚 <b>Тесттер каталогы</b>\n\n"
                "👇 Қажетті пәнді таңда — ішінде тесттер тізімі ашылады.\n"
                "Содан кейін тестті бас → «▶️ Тестті өту»")
    else:
        text = ("📚 <b>Каталог тестов</b>\n\n"
                "👇 Выбери предмет — внутри откроется список тестов.\n"
                "Потом тапни на тест → «▶️ Пройти тест»")

    kb = InlineKeyboardBuilder()

    # Раздел приватных тестов — если есть
    if private_tests:
        if lang == "kz":
            text += f"\n\n🔐 Сенде <b>{len(private_tests)} жабық тест</b> бар!"
        else:
            text += f"\n\n🔐 У тебя есть <b>{len(private_tests)} закрытых теста</b>!"
        kb.button(text=f"🔐 Мои закрытые тесты ({len(private_tests)})",
                  callback_data="m:private_tests")

    # Категории: обязательные (всем) + профильные юзера
    profile_ids_raw = utils.get_profile_subjects(call.from_user.id)
    has_other = 'other' in profile_ids_raw
    profile_ids = set(x for x in profile_ids_raw if x != 'other')

    def _cnt(cat_id):
        return db.fetchone(
            """SELECT COUNT(*) AS c FROM tests
               WHERE category_id=? AND status='active'
                 AND COALESCE(is_private,0)=0 AND language=?""",
            (cat_id, lang))['c']

    required_cats = [c for c in cats if c.get('is_required')]
    if has_other:
        # «Другое» — показываем ВСЕ профильные разделы
        profile_cats = [c for c in cats if not c.get('is_required')]
    else:
        profile_cats = [c for c in cats
                        if not c.get('is_required') and c['id'] in profile_ids]

    has_any = False
    # Обязательные
    shown_required = [(c, _cnt(c['id'])) for c in required_cats]
    shown_required = [(c, n) for c, n in shown_required if n > 0]
    if shown_required:
        has_any = True
        text += ("\n\n⭐️ <b>Міндетті:</b>" if lang == "kz"
                 else "\n\n⭐️ <b>Обязательные:</b>")
        for c, n in shown_required:
            emoji = c.get('emoji') or '📚'
            kb.button(text=f"{emoji} {c['name']} ({n})",
                      callback_data=f"m:cat:{c['id']}")

    # Профильные юзера
    shown_profile = [(c, _cnt(c['id'])) for c in profile_cats]
    shown_profile = [(c, n) for c, n in shown_profile if n > 0]
    if shown_profile:
        has_any = True
        text += ("\n\n🎓 <b>Сенің бейіндік:</b>" if lang == "kz"
                 else "\n\n🎓 <b>Твои профильные:</b>")
        for c, n in shown_profile:
            emoji = c.get('emoji') or '📚'
            kb.button(text=f"{emoji} {c['name']} ({n})",
                      callback_data=f"m:cat:{c['id']}")

    # Если у юзера ещё не выбраны профильные — предложим выбрать
    if not profile_ids:
        optional_exist = any(not c.get('is_required') for c in cats)
        if optional_exist:
            kb.button(
                text=("🎓 Бейіндік пәндерді таңда" if lang == "kz"
                      else "🎓 Выбрать профильные предметы"),
                callback_data="m:change_subjects")

    kb.button(text=t("btn_back", lang), callback_data="m:menu")
    kb.adjust(1)

    try:
        await call.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("m:cat:"))
async def cb_tests_by_category(call: CallbackQuery, user: dict):
    """Список тестов в выбранном разделе."""
    lang = _resolve_lang(user)
    from handlers import categories
    arg = call.data.split(":")[2]
    if arg == "none":
        tests = categories.get_tests_without_category(lang)
        cat_title = "📭 Без раздела" if lang != "kz" else "📭 Бөлімсіз"
    else:
        try:
            cat_id = int(arg)
        except ValueError:
            await call.answer()
            return
        cat = db.fetchone("SELECT * FROM test_categories WHERE id=?", (cat_id,))
        if not cat:
            await call.answer("Раздел не найден.", show_alert=True)
            return
        tests = categories.get_tests_in_category(cat_id, lang)
        emoji = cat.get('emoji') or '📚'
        cat_title = f"{emoji} {cat['name']}"

    text = f"<b>{utils.escape_html(cat_title)}</b>"
    if tests:
        if lang == "kz":
            text += "\n\n👇 Тестті тап → ашылады карточка → «▶️ Тестті өту»"
        else:
            text += "\n\n👇 Тапни на тест → откроется карточка → «▶️ Пройти тест»"
    else:
        text += "\n\n<i>В этом разделе пока нет тестов.</i>"

    kb = InlineKeyboardBuilder()
    for tst in tests[:30]:
        t_title = (tst.get('title') or '—')[:50]
        prefix = "💎 " if tst.get('is_paid') else ""
        kb.button(text=f"{prefix}{t_title}", callback_data=f"opentest:{tst['id']}")
    kb.button(text="↩️ К разделам", callback_data="m:tests")
    kb.adjust(1)

    try:
        await call.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "m:private_tests")
async def cb_private_tests_list(call: CallbackQuery, user: dict):
    """Каталог приватных тестов конкретного юзера."""
    from handlers import private_access as _pa
    lang = _resolve_lang(user)
    private_tests = _pa.list_user_private_tests(call.from_user.id)

    if not private_tests:
        text = "🔐 <b>Закрытые тесты</b>\n\n<i>У вас нет доступа к закрытым тестам.</i>"
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="↩️ Назад", callback_data="m:tests")
        ]])
    else:
        text = (f"🔐 <b>Ваши закрытые тесты</b>\n\n"
                f"Доступно: <b>{len(private_tests)}</b>\n"
                f"Выберите тест для прохождения:")
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        kb_builder = InlineKeyboardBuilder()
        for tst in private_tests[:30]:
            title = (tst.get('title') or '—')[:50]
            kb_builder.button(text=f"🔐 {title}", callback_data=f"opentest:{tst['id']}")
        kb_builder.button(text="↩️ Назад", callback_data="m:tests")
        kb_builder.adjust(1)
        kb = kb_builder.as_markup()

    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("testspage:"))
async def cb_tests_page(call: CallbackQuery, user: dict):
    lang = _resolve_lang(user)
    try:
        page = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        page = 0
    tests = _list_active_tests(lang)
    try:
        await call.message.edit_reply_markup(reply_markup=tests_list_kb(tests, lang, page=page))
    except Exception:
        pass
    await call.answer()


async def show_test_card(bot: Bot, chat_id: int, user_tg_id: int, test_id: int, lang: str):
    """Показать карточку теста — QuizBot-стиль (как при шеринге)."""
    test = test_runner.get_test(test_id)
    if not test or test['status'] != 'active':
        await bot.send_message(chat_id, t("test_not_found", lang),
                               reply_markup=back_kb(lang, "m:tests"))
        return

    # Приватный тест — проверка доступа
    if test.get('is_private'):
        from handlers import private_access as _pa
        if not _pa.user_has_private_access(test['id'], user_tg_id):
            await bot.send_message(
                chat_id,
                "❌ <b>Тест не найден или у вас нет к нему доступа.</b>\n\n"
                "Возможно, тест приватный — обратитесь к администратору, "
                "если считаете, что доступ должен быть.",
                reply_markup=back_kb(lang, "m:tests"), parse_mode="HTML")
            return

    user = utils.get_user_by_tg(user_tg_id)
    has_access = utils.has_paid_access(user['id'], test_id=test['id']) if user else False

    # Платный тест без доступа — показываем покупку
    if test['is_paid'] and not has_access:
        from services import referral_service as _rs
        if not _rs.user_can_unlock_paid_test(user['id']):
            qcount = db.fetchone(
                "SELECT COUNT(*) AS c FROM questions WHERE test_id=?", (test['id'],))['c']
            short_card = (
                f"💰 <b>{utils.escape_html(test['title'])}</b>\n"
                f"📚 {qcount} вопросов · ⏱ {test['time_per_question']} сек\n\n"
                f"<i>Это платный тест.</i>"
            )
            await bot.send_message(chat_id, short_card)
            await bot.send_message(
                chat_id,
                t("paid_test_card", lang, price=test['price'], manager=config.MANAGER_USERNAME),
                reply_markup=paid_test_kb(test['id'], lang, config.MANAGER_USERNAME),
            )
            return

    # Бесплатный или есть доступ — QuizBot-style карточка
    is_admin = utils.is_admin(user_tg_id)
    card_text, card_kb = share_service.build_test_card(
        dict(test), in_bot=True, viewer_is_admin=is_admin)
    await bot.send_message(chat_id, card_text, reply_markup=card_kb,
                            parse_mode="HTML", disable_web_page_preview=True)

    # Короткая подсказка под карточкой — как пройти тест
    if lang == "kz":
        hint = ("👆 Тестті бастау үшін «<b>▶️ Тестті өту</b>» бас.\n"
                "Бот сұрақтарды бір-бірлеп жібереді, әр сұраққа таймер болады.")
    else:
        hint = ("👆 Чтобы пройти тест — тапни «<b>▶️ Пройти тест</b>».\n"
                "Бот будет присылать вопросы по одному с таймером.")
    try:
        await bot.send_message(chat_id, hint, parse_mode="HTML")
    except Exception:
        pass


@router.callback_query(F.data.startswith("test:"))
async def cb_test_card(call: CallbackQuery, user: dict):
    lang = _resolve_lang(user)
    try:
        test_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    try:
        await call.message.delete()
    except Exception:
        pass
    await show_test_card(call.bot, call.message.chat.id, call.from_user.id, test_id, lang)
    await call.answer()


@router.callback_query(F.data.startswith("checkacc:"))
async def cb_check_access(call: CallbackQuery, user: dict):
    lang = _resolve_lang(user)
    try:
        test_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    test = test_runner.get_test(test_id)
    if not test:
        await call.answer(t("test_not_found", lang), show_alert=True)
        return
    if utils.has_paid_access(user['id'], test_id=test['id']):
        await call.answer(t("access_granted", lang), show_alert=True)
        try:
            await call.message.delete()
        except Exception:
            pass
        await show_test_card(call.bot, call.message.chat.id, call.from_user.id,
                             test_id, lang)
    else:
        await call.answer(t("access_still_none", lang), show_alert=True)


@router.callback_query(F.data.startswith("opentest:"))
async def cb_open_test(call: CallbackQuery, user: dict):
    """Открыть карточку теста по ID — используется в уведомлениях о Premium и т.п."""
    lang = _resolve_lang(user)
    try:
        test_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    await show_test_card(call.bot, call.message.chat.id,
                          call.from_user.id, test_id, lang)
    await call.answer()


@router.callback_query(F.data.startswith("run:"))
async def cb_run_test(call: CallbackQuery, state: FSMContext, user: dict):
    lang = _resolve_lang(user)
    try:
        test_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    test = test_runner.get_test(test_id)
    if not test:
        await call.answer(t("test_not_found", lang), show_alert=True)
        return

    # В группе ли мы? Только в личке стандартный запуск
    if call.message.chat.type != "private":
        await call.answer(t("personal_chat_only", lang), show_alert=True)
        return

    # Приватный тест — проверка доступа
    if test.get('is_private'):
        from handlers import private_access as _pa
        if not _pa.user_has_private_access(test['id'], call.from_user.id):
            await call.answer(
                "❌ У вас нет доступа к этому тесту, или срок доступа истёк.",
                show_alert=True)
            return

    # Проверка доступа
    if test['is_paid'] and not utils.has_paid_access(user['id'], test_id=test['id']):
        # Возможно, открыл доступ через 10 приглашений
        from services import referral_service as _rs
        if not _rs.user_can_unlock_paid_test(user['id']):
            await call.answer(t("noaccess_test", lang, manager=config.MANAGER_USERNAME),
                              show_alert=True)
            return

    # Лимит попыток
    if test['attempts_limit']:
        used = test_runner.count_user_attempts(user['id'], test_id)
        if used >= test['attempts_limit']:
            await call.answer(t("attempts_exhausted", lang), show_alert=True)
            return

    # Проверка подписки на канал
    channel = subscription_service.get_required_channel_for_test(test_id)
    if channel:
        ok = await subscription_service.check_user_subscription(
            call.bot, channel, call.from_user.id)
        if not ok:
            await call.message.answer(
                t("must_subscribe", lang),
                reply_markup=subscription_kb(channel, lang, f"checksub:test:{test_id}")
            )
            await call.answer()
            return

    # Есть вопросы?
    qs = test_runner.get_test_questions(test_id)
    if not qs:
        await call.answer(t("no_questions", lang), show_alert=True)
        return

    # Создаём попытку
    attempt_id = test_runner.create_attempt(
        user['id'], test_id, lang, group_id=None,
        started_by_user_id=user['id'])

    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer(t("test_start_warning", lang))
    # Небольшая пауза перед стартом
    await asyncio.sleep(min(config.TEST_START_COOLDOWN_SECONDS, 3))
    await test_runner.send_current_question(call.bot, attempt_id, call.message.chat.id)
    await call.answer()


@router.callback_query(F.data.startswith("checksub:"))
async def cb_check_subscription(call: CallbackQuery, state: FSMContext, user: dict):
    """checksub:test:{id} или checksub:note:{id}"""
    lang = _resolve_lang(user)
    parts = call.data.split(":")
    if len(parts) < 3:
        await call.answer()
        return
    kind, obj_id_s = parts[1], parts[2]
    try:
        obj_id = int(obj_id_s)
    except ValueError:
        await call.answer()
        return

    if kind == "test":
        channel = subscription_service.get_required_channel_for_test(obj_id)
    elif kind == "note":
        channel = subscription_service.get_required_channel_for_note(obj_id)
    else:
        channel = None

    if not channel:
        await call.answer(t("sub_ok", lang), show_alert=True)
        return

    ok = await subscription_service.check_user_subscription(
        call.bot, channel, call.from_user.id)
    if ok:
        await call.answer(t("sub_ok", lang), show_alert=False)
        try:
            await call.message.delete()
        except Exception:
            pass
        # Имитируем повторное нажатие run/read
        if kind == "test":
            # Запустить тест
            fake_data = f"run:{obj_id}"
            call.data = fake_data
            await cb_run_test(call, state, user)
        elif kind == "note":
            from handlers.notes import show_note_card
            await show_note_card(call.bot, call.message.chat.id, call.from_user.id,
                                 obj_id, lang)
    else:
        await call.answer(t("sub_fail", lang), show_alert=True)


@router.callback_query(F.data == "m:share")
async def cb_share(call: CallbackQuery, user: dict):
    lang = _resolve_lang(user)
    tests = _list_active_tests(lang)
    if not tests:
        await call.answer(t("no_tests", lang), show_alert=True)
        return
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    kb = InlineKeyboardBuilder()
    # Быстрая кнопка: открыть нативный пикер чатов (выдаст список всех наших тестов)
    kb.row(InlineKeyboardButton(
        text="🚀 Открыть выбор чата (все тесты)",
        switch_inline_query="",
    ))
    for tst in tests[:20]:
        kb.button(text=f"📋 {tst['title'][:50]}", callback_data=f"sharetest:{tst['id']}")
    kb.button(text=t("btn_back", lang), callback_data="m:menu")
    kb.adjust(1)
    try:
        await call.message.edit_text(
            "📨 <b>Поделиться тестом</b>\n\n"
            "Выберите тест — получите карточку как у @QuizBot, "
            "с кнопками <b>«Пройти тест»</b>, <b>«Отправить в группу»</b>, "
            "<b>«Поделиться»</b>.\n\n"
            "Либо нажмите «Открыть выбор чата» — Telegram сразу покажет "
            "список ваших чатов для отправки.",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
    except Exception:
        await call.message.answer(
            "📨 <b>Поделиться тестом</b>\n\nВыберите тест:",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
    await call.answer()


@router.callback_query(F.data.startswith("sharetest:"))
async def cb_share_test(call: CallbackQuery, user: dict):
    """Показываем QuizBot-стиль карточку теста с тремя кнопками."""
    lang = _resolve_lang(user)
    try:
        test_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    test = test_runner.get_test(test_id)
    if not test:
        await call.answer(t("test_not_found", lang), show_alert=True)
        return
    is_admin = utils.is_admin(call.from_user.id)
    text, kb = share_service.build_test_card(
        dict(test), in_bot=True, viewer_is_admin=is_admin)
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer(text, reply_markup=kb, parse_mode="HTML",
                               disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data == "m:results")
async def cb_my_results(call: CallbackQuery, user: dict):
    lang = _resolve_lang(user)
    rows = db.fetchall(
        """SELECT a.id, a.score, a.correct_answers, a.wrong_answers, a.skipped_answers,
                  a.created_at, t.title
           FROM test_attempts a JOIN tests t ON t.id = a.test_id
           WHERE a.user_id=? AND a.status='finished'
           ORDER BY a.id DESC LIMIT 20""", (user['id'],))
    if not rows:
        text = t("my_attempts_empty", lang)
    else:
        lines = [t("my_attempts_title", lang), ""]
        for r in rows:
            total = r['correct_answers'] + r['wrong_answers'] + r['skipped_answers']
            pct = round(r['correct_answers'] / total * 100) if total else 0
            line = (f"• <b>{utils.escape_html(r['title'])}</b> — "
                    f"{r['correct_answers']}/{total} ({pct}%, {r['score']} б.)")
            lines.append(line)
        text = "\n".join(lines)
    try:
        await call.message.edit_text(text, reply_markup=back_kb(lang, "m:menu"))
    except Exception:
        await call.message.answer(text, reply_markup=back_kb(lang, "m:menu"))
    await call.answer()


# ============ Статистика теста (лидерборд с пагинацией) ============

@router.callback_query(F.data.startswith("stats:"))
async def cb_stats(call: CallbackQuery, user: dict):
    """Показать страницу лидерборда теста. Только админ/автор."""
    try:
        parts = call.data.split(":")
        test_id = int(parts[1])
        page = int(parts[2]) if len(parts) > 2 else 1
    except (ValueError, IndexError):
        await call.answer()
        return

    test = test_runner.get_test(test_id)
    if not test:
        await call.answer("Тест не найден.", show_alert=True)
        return

    # Доступ: админ бота или автор теста
    is_admin = utils.is_admin(call.from_user.id)
    is_author = bool(user) and test.get('created_by') == user['id']
    if not (is_admin or is_author):
        await call.answer(
            "❌ У вас нет доступа к статистике этого теста.",
            show_alert=True)
        return

    text, kb = group_quiz_service.build_stats_text(dict(test), page=page)
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML",
                                       disable_web_page_preview=True)
    except Exception:
        # Если редактирование не получилось (старое сообщение и т.п.) — отправим новое
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML",
                                    disable_web_page_preview=True)
    await call.answer()
