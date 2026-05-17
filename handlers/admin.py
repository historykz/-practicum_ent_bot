"""Хендлеры админ-панели."""
import asyncio
import csv
import io
import json
import logging

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (Message, CallbackQuery, BufferedInputFile,
                            Poll, ReplyKeyboardRemove)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import database as db
import utils
from filters import IsAdmin
from locales import t
from keyboards import (admin_menu_kb, admin_lang_kb, test_type_kb,
                        admin_tests_list_kb, admin_test_actions_kb,
                        import_done_kb, draft_fix_kb, note_access_kb,
                        note_pages_done_kb, yes_no_kb, back_kb, main_menu_kb,
                        cancel_kb)
from states import (TestCreateStates, TextImportStates, PollImportStates,
                     GrantAccessStates, PremiumStates, BlockStates,
                     ChannelStates, NoteCreateStates, DraftFixStates)
from services import (text_import_service, quiz_importer, notes_service)

router = Router(name="admin")
log = logging.getLogger(__name__)


@router.message(Command("admin"), IsAdmin())
async def cmd_admin(message: Message, state: FSMContext, user: dict):
    await state.clear()
    lang = user.get('language') or 'ru'
    await message.answer(t("admin_menu", lang), reply_markup=admin_menu_kb(lang))


@router.message(Command("admin"))
async def cmd_admin_denied(message: Message, user: dict):
    lang = user.get('language') or 'ru'
    await message.answer(t("admin_no_rights", lang))


@router.callback_query(F.data == "m:admin", IsAdmin())
async def cb_admin_menu(call: CallbackQuery, state: FSMContext, user: dict):
    await state.clear()
    lang = user.get('language') or 'ru'
    try:
        await call.message.edit_text(t("admin_menu", lang), reply_markup=admin_menu_kb(lang))
    except Exception:
        await call.message.answer(t("admin_menu", lang), reply_markup=admin_menu_kb(lang))
    await call.answer()


# =================================
# Создание теста (мастер)
# =================================

@router.callback_query(F.data == "adm:create_test", IsAdmin())
async def cb_create_test(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    await state.set_state(TestCreateStates.title)
    await call.message.answer(t("ask_test_title", lang), reply_markup=cancel_kb(lang))
    await call.answer()


@router.message(TestCreateStates.title, IsAdmin())
async def s_title(message: Message, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    await state.update_data(title=message.text.strip()[:200])
    await state.set_state(TestCreateStates.description)
    await message.answer(t("ask_test_description", lang), reply_markup=cancel_kb(lang))


@router.message(TestCreateStates.description, IsAdmin())
async def s_descr(message: Message, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    await state.update_data(description=message.text.strip()[:1000])
    await state.set_state(TestCreateStates.subject)
    await message.answer(t("ask_test_subject", lang))


@router.message(TestCreateStates.subject, IsAdmin())
async def s_subject(message: Message, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    await state.update_data(subject=message.text.strip()[:100])
    await state.set_state(TestCreateStates.grade)
    await message.answer(t("ask_test_grade", lang))


@router.message(TestCreateStates.grade, IsAdmin())
async def s_grade(message: Message, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    grade = message.text.strip()
    if grade.isdigit():
        grade = int(grade)
    else:
        grade = 0
    await state.update_data(grade=grade)
    await state.set_state(TestCreateStates.category)
    await message.answer(t("ask_test_category", lang))


@router.message(TestCreateStates.category, IsAdmin())
async def s_category(message: Message, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    await state.update_data(category=message.text.strip()[:100])
    await state.set_state(TestCreateStates.language)
    await message.answer(t("ask_test_lang", lang), reply_markup=admin_lang_kb("newtest_lang"))


@router.callback_query(TestCreateStates.language, F.data.startswith("newtest_lang:"), IsAdmin())
async def s_language(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    tl = call.data.split(":")[1]
    await state.update_data(language=tl)
    await state.set_state(TestCreateStates.test_type)
    await call.message.answer(t("ask_test_type", lang), reply_markup=test_type_kb())
    await call.answer()


@router.callback_query(TestCreateStates.test_type, F.data.startswith("newtest_type:"), IsAdmin())
async def s_test_type(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    tt = call.data.split(":")[1]
    await state.update_data(test_type=tt)
    await state.set_state(TestCreateStates.time_per_question)
    await call.message.answer(t("ask_test_time_per_q", lang))
    await call.answer()


@router.message(TestCreateStates.time_per_question, IsAdmin())
async def s_time(message: Message, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    try:
        tpq = max(5, min(600, int(message.text.strip())))
    except ValueError:
        tpq = config.DEFAULT_TIME_PER_QUESTION
    await state.update_data(time_per_question=tpq)
    await state.set_state(TestCreateStates.attempts_limit)
    await message.answer(t("ask_test_attempts", lang))


@router.message(TestCreateStates.attempts_limit, IsAdmin())
async def s_attempts(message: Message, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    txt = message.text.strip()
    try:
        al = int(txt)
        if al <= 0:
            al = None
    except ValueError:
        al = None
    await state.update_data(attempts_limit=al)
    await state.set_state(TestCreateStates.first_attempt_only)
    await message.answer(t("ask_test_first_only", lang), reply_markup=yes_no_kb("newtest_first", lang))


@router.callback_query(TestCreateStates.first_attempt_only, F.data.startswith("newtest_first:"), IsAdmin())
async def s_first(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    val = call.data.split(":")[1] == "1"
    await state.update_data(first_attempt_only=val)
    await state.set_state(TestCreateStates.is_paid)
    await call.message.answer(t("ask_test_paid", lang), reply_markup=yes_no_kb("newtest_paid", lang))
    await call.answer()


@router.callback_query(TestCreateStates.is_paid, F.data.startswith("newtest_paid:"), IsAdmin())
async def s_paid(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    val = call.data.split(":")[1] == "1"
    await state.update_data(is_paid=val)
    if val:
        await state.set_state(TestCreateStates.price)
        await call.message.answer(t("ask_test_price", lang))
    else:
        await state.update_data(price=0)
        await state.set_state(TestCreateStates.shuffle_questions)
        await call.message.answer(t("ask_test_shuffle_q", lang),
                                  reply_markup=yes_no_kb("newtest_shufq", lang))
    await call.answer()


@router.message(TestCreateStates.price, IsAdmin())
async def s_price(message: Message, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    try:
        p = max(0, int(message.text.strip()))
    except ValueError:
        p = 0
    await state.update_data(price=p)
    await state.set_state(TestCreateStates.shuffle_questions)
    await message.answer(t("ask_test_shuffle_q", lang), reply_markup=yes_no_kb("newtest_shufq", lang))


@router.callback_query(TestCreateStates.shuffle_questions, F.data.startswith("newtest_shufq:"), IsAdmin())
async def s_shufq(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    val = call.data.split(":")[1] == "1"
    await state.update_data(shuffle_questions=val)
    await state.set_state(TestCreateStates.shuffle_options)
    await call.message.answer(t("ask_test_shuffle_o", lang),
                              reply_markup=yes_no_kb("newtest_shufo", lang))
    await call.answer()


@router.callback_query(TestCreateStates.shuffle_options, F.data.startswith("newtest_shufo:"), IsAdmin())
async def s_shufo(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    val = call.data.split(":")[1] == "1"
    await state.update_data(shuffle_options=val)
    await state.set_state(TestCreateStates.show_correct)
    await call.message.answer(t("ask_test_show_answers", lang),
                              reply_markup=yes_no_kb("newtest_showa", lang))
    await call.answer()


@router.callback_query(TestCreateStates.show_correct, F.data.startswith("newtest_showa:"), IsAdmin())
async def s_showa(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    val = call.data.split(":")[1] == "1"
    await state.update_data(show_correct=val)
    await state.set_state(TestCreateStates.show_explanation)
    await call.message.answer(t("ask_test_show_explain", lang),
                              reply_markup=yes_no_kb("newtest_showe", lang))
    await call.answer()


@router.callback_query(TestCreateStates.show_explanation, F.data.startswith("newtest_showe:"), IsAdmin())
async def s_showe(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    val = call.data.split(":")[1] == "1"
    await state.update_data(show_explanation=val)
    await state.set_state(TestCreateStates.required_channel)
    kb = InlineKeyboardBuilder()
    kb.button(text=t("btn_skip", lang), callback_data="newtest_chskip")
    kb.button(text=t("btn_cancel", lang), callback_data="cancel")
    kb.adjust(1)
    await call.message.answer(t("ask_test_channel", lang), reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(TestCreateStates.required_channel, F.data == "newtest_chskip", IsAdmin())
async def s_channel_skip(call: CallbackQuery, state: FSMContext, user: dict):
    await state.update_data(required_channel=None)
    await _finish_create_test(call.bot, call.message.chat.id, state, user)
    await call.answer()


@router.message(TestCreateStates.required_channel, IsAdmin())
async def s_channel(message: Message, state: FSMContext, user: dict):
    ch = message.text.strip()
    if not ch.startswith("@"):
        ch = "@" + ch.lstrip("@")
    await state.update_data(required_channel=ch)
    await _finish_create_test(message.bot, message.chat.id, state, user)


async def _finish_create_test(bot: Bot, chat_id: int, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    data = await state.get_data()
    db.execute(
        """INSERT INTO tests
           (title, description, subject, grade, category, language, test_type,
            time_per_question, attempts_limit, first_attempt_only, is_paid, price,
            shuffle_questions, shuffle_options, show_correct, show_explanation,
            required_channel, status, allow_in_group, allow_daily, allow_duel,
            allow_tournament, display_mode, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active',
                   1, 0, 0, 0, 'inline', ?, ?)""",
        (data.get('title'), data.get('description'), data.get('subject'),
         data.get('grade', 0), data.get('category'), data.get('language', 'ru'),
         data.get('test_type', 'regular'),
         data.get('time_per_question', config.DEFAULT_TIME_PER_QUESTION),
         data.get('attempts_limit'),
         1 if data.get('first_attempt_only') else 0,
         1 if data.get('is_paid') else 0,
         data.get('price', 0),
         1 if data.get('shuffle_questions') else 0,
         1 if data.get('shuffle_options') else 0,
         1 if data.get('show_correct') else 0,
         1 if data.get('show_explanation') else 0,
         data.get('required_channel'),
         user['id'], utils.now_iso())
    )
    test_id = db.fetchone("SELECT last_insert_rowid() AS id")['id']
    await state.clear()
    await bot.send_message(chat_id, t("test_created", lang, id=test_id),
                           reply_markup=admin_test_actions_kb(test_id, lang))


# =================================
# Список тестов админа
# =================================

@router.callback_query(F.data == "adm:my_tests", IsAdmin())
async def cb_my_tests(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    rows = db.fetchall("SELECT * FROM tests ORDER BY id DESC LIMIT 30")
    if not rows:
        await call.message.answer(t("no_admin_tests", lang),
                                  reply_markup=back_kb(lang, "m:admin"))
    else:
        await call.message.answer(t("admin_tests_list", lang),
                                  reply_markup=admin_tests_list_kb([dict(r) for r in rows], lang))
    await call.answer()


@router.callback_query(F.data.startswith("admtest:"), IsAdmin())
async def cb_admtest(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    try:
        tid = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (tid,))
    if not test:
        await call.answer(t("test_not_found", lang), show_alert=True)
        return
    qcount = db.fetchone("SELECT COUNT(*) AS c FROM questions WHERE test_id=?", (tid,))['c']
    status_label = t(f"test_status_{test['status']}", lang)
    text = (f"<b>{utils.escape_html(test['title'])}</b>\n\n"
            f"ID: {test['id']}\n"
            f"Тип: {test['test_type']}\n"
            f"Язык: {test['language']}\n"
            f"Статус: {status_label}\n"
            f"Вопросов: {qcount}\n"
            f"Платный: {'да' if test['is_paid'] else 'нет'}")
    try:
        await call.message.edit_text(text, reply_markup=admin_test_actions_kb(tid, lang))
    except Exception:
        await call.message.answer(text, reply_markup=admin_test_actions_kb(tid, lang))
    await call.answer()


@router.callback_query(F.data.startswith("admdel:"), IsAdmin())
async def cb_admdel(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    try:
        tid = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    db.execute("DELETE FROM tests WHERE id=?", (tid,))
    db.execute("DELETE FROM questions WHERE test_id=?", (tid,))
    await call.answer(t("test_deleted", lang), show_alert=True)
    try:
        await call.message.delete()
    except Exception:
        pass


# =================================
# Импорт вопросов текстом
# =================================

@router.callback_query(F.data.startswith("admimport_text:"), IsAdmin())
async def cb_import_text(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    try:
        tid = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    await state.set_state(TextImportStates.waiting_questions)
    await state.update_data(import_test_id=tid)
    await call.message.answer(t("import_text_instruction", lang),
                              reply_markup=import_done_kb(lang))
    await call.answer()


@router.callback_query(F.data == "adm:import_text", IsAdmin())
async def cb_import_text_root(call: CallbackQuery, user: dict):
    """Без выбранного теста — попросить выбрать тест."""
    lang = user.get('language') or 'ru'
    rows = db.fetchall(
        "SELECT * FROM tests WHERE created_by=? OR ?=1 ORDER BY id DESC LIMIT 30",
        (user['id'], 1))
    if not rows:
        await call.answer(t("no_admin_tests", lang), show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"{r['id']}. {r['title'][:40]}",
                  callback_data=f"admimport_text:{r['id']}")
    kb.button(text=t("btn_back", lang), callback_data="m:admin")
    kb.adjust(1)
    await call.message.answer("Выберите тест для импорта:", reply_markup=kb.as_markup())
    await call.answer()


@router.message(TextImportStates.waiting_questions, IsAdmin())
async def msg_import_text(message: Message, state: FSMContext, user: dict):
    """Накапливает текст. Сохранение — по кнопке «Сохранить»."""
    lang = user.get('language') or 'ru'
    data = await state.get_data()
    tid = data.get('import_test_id')
    if not tid:
        await state.clear()
        await message.answer(t("error_generic", lang))
        return
    raw = message.text or message.caption or ""
    if not raw.strip():
        return
    buf = data.get('text_buffer', '')
    if buf:
        buf += "\n\n" + raw
    else:
        buf = raw
    await state.update_data(text_buffer=buf)
    # Считаем приблизительно вопросы (по пустым строкам)
    chunks = [c for c in buf.split("\n\n") if c.strip()]
    await message.answer(
        f"📥 В буфере: <b>{len(chunks)}</b> вопросов.\n"
        f"Можете слать ещё. Когда закончите — нажмите «✅ Сохранить».",
        reply_markup=import_done_kb(lang)
    )


@router.callback_query(F.data == "import:done", TextImportStates.waiting_questions, IsAdmin())
async def cb_import_text_done(call: CallbackQuery, state: FSMContext, user: dict):
    """Финальное сохранение текстового импорта."""
    lang = user.get('language') or 'ru'
    data = await state.get_data()
    tid = data.get('import_test_id')
    buf = data.get('text_buffer', '')
    if not tid or not buf.strip():
        await state.clear()
        await call.message.answer("Нечего сохранять.", reply_markup=admin_menu_kb(lang))
        await call.answer()
        return
    added, errors = text_import_service.import_text_questions(tid, buf)
    err_text = "\n".join(errors[:10]) if errors else "—"
    await call.message.answer(
        t("import_report", lang, added=added, errors=err_text)
    )
    await state.clear()
    await call.message.answer(t("admin_menu", lang), reply_markup=admin_menu_kb(lang))
    await call.answer()


@router.callback_query(F.data == "import:done", IsAdmin())
async def cb_import_done(call: CallbackQuery, state: FSMContext, user: dict):
    """Общий fallback — если нажали кнопку без активного импорта."""
    lang = user.get('language') or 'ru'
    await state.clear()
    await call.message.answer(t("admin_menu", lang), reply_markup=admin_menu_kb(lang))
    await call.answer()


# =================================
# Импорт Quiz Poll
# =================================

@router.callback_query(F.data.startswith("admimport_poll:"), IsAdmin())
async def cb_import_poll(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    try:
        tid = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    await state.set_state(PollImportStates.waiting_polls)
    await state.update_data(import_poll_test_id=tid)
    await call.message.answer(t("import_poll_instruction", lang),
                              reply_markup=import_done_kb(lang))
    await call.answer()


@router.callback_query(F.data == "adm:import_poll", IsAdmin())
async def cb_import_poll_root(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    rows = db.fetchall("SELECT * FROM tests ORDER BY id DESC LIMIT 30")
    if not rows:
        await call.answer(t("no_admin_tests", lang), show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"{r['id']}. {r['title'][:40]}",
                  callback_data=f"admimport_poll:{r['id']}")
    kb.button(text=t("btn_back", lang), callback_data="m:admin")
    kb.adjust(1)
    await call.message.answer("Выберите тест для импорта опросов:",
                              reply_markup=kb.as_markup())
    await call.answer()


@router.message(PollImportStates.waiting_polls, IsAdmin())
async def msg_import_poll(message: Message, state: FSMContext, user: dict):
    """Принимаем любые сообщения в этом state и проверяем poll внутри —
    так ловим пересылки из ВСЕХ типов чатов (личка, каналы, группы)."""
    lang = user.get('language') or 'ru'
    data = await state.get_data()
    tid = data.get('import_poll_test_id')
    if not tid:
        await state.clear()
        return

    # Если не poll — подсказка
    if message.poll is None:
        cnt = len(data.get('poll_buffer') or [])
        await message.answer(
            f"📥 В буфере: <b>{cnt}</b> опросов.\n\n"
            f"Пересылайте сюда Quiz Poll из любого чата или канала. "
            f"Когда закончите — нажмите «✅ Сохранить».",
            reply_markup=import_done_kb(lang)
        )
        return

    poll = message.poll
    # Принимаем ВСЕ типы polls (quiz/regular) — Telegram при пересылке
    # часто меняет тип на 'regular' и не передаёт правильный ответ.
    # Такие вопросы пойдут в 📋 Черновики на ручную проверку.

    buf = data.get('poll_buffer') or []
    # Защита от случайных дубликатов
    if any(p.get('id') == poll.id for p in buf):
        await message.answer(
            f"⚠️ Этот опрос уже в буфере. Всего: <b>{len(buf)}</b>",
            reply_markup=import_done_kb(lang)
        )
        return

    buf.append({
        'id': poll.id,
        'question': poll.question,
        'options': [opt.text for opt in poll.options],
        'correct_option_id': getattr(poll, 'correct_option_id', None),
        'explanation': getattr(poll, 'explanation', None) or "",
    })
    await state.update_data(poll_buffer=buf)

    indicator = ""
    if poll.correct_option_id is None:
        indicator = " ⚠️ (правильный ответ не виден — пойдёт в черновики)"

    await message.answer(
        f"📥 В буфере: <b>{len(buf)}</b> опросов{indicator}.\n"
        f"Шлите ещё или нажмите «✅ Сохранить».",
        reply_markup=import_done_kb(lang)
    )


@router.callback_query(F.data == "import:done", PollImportStates.waiting_polls, IsAdmin())
async def cb_import_poll_done(call: CallbackQuery, state: FSMContext, user: dict):
    """Сохраняем все буферизованные polls."""
    lang = user.get('language') or 'ru'
    data = await state.get_data()
    tid = data.get('import_poll_test_id')
    buf = data.get('poll_buffer') or []
    if not tid or not buf:
        await state.clear()
        await call.message.answer("В буфере пусто.", reply_markup=admin_menu_kb(lang))
        await call.answer()
        return

    saved = 0
    drafts = 0
    errors = 0
    for p in buf:
        try:
            ok = quiz_importer.save_poll_dict_as_question(tid, p, user['id'])
            if ok == 'ok':
                saved += 1
            elif ok == 'draft':
                drafts += 1
            else:
                errors += 1
        except Exception:
            errors += 1

    await call.message.answer(
        f"✅ Сохранено вопросов: <b>{saved}</b>\n"
        f"📋 В черновиках (нужен ручной правильный ответ): <b>{drafts}</b>\n"
        f"❌ Ошибок: <b>{errors}</b>"
    )
    await state.clear()
    await call.message.answer(t("admin_menu", lang), reply_markup=admin_menu_kb(lang))
    await call.answer()


# =================================
# Черновики (Quiz Poll без correct_option_id)
# =================================

@router.callback_query(F.data.startswith("admdrafts:"), IsAdmin())
async def cb_drafts(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    try:
        tid = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    drafts = quiz_importer.list_drafts(tid)
    if not drafts:
        await call.message.answer(t("drafts_empty", lang),
                                  reply_markup=back_kb(lang, f"admtest:{tid}"))
        await call.answer()
        return
    kb = InlineKeyboardBuilder()
    for d in drafts[:20]:
        kb.button(text=f"#{d['id']}: {d['question_text'][:40]}",
                  callback_data=f"draft:{d['id']}")
    kb.button(text=t("btn_back", lang), callback_data=f"admtest:{tid}")
    kb.adjust(1)
    await call.message.answer(t("drafts_list", lang), reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("draft:"), IsAdmin())
async def cb_draft(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    try:
        draft_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    draft = quiz_importer.get_draft(draft_id)
    if not draft:
        await call.answer(t("error_generic", lang), show_alert=True)
        return
    opts = json.loads(draft['raw_options'])
    qtext = (f"{draft['question_text']}\n\n" +
             "\n".join(f"{chr(ord('A')+i)}) {o}" for i, o in enumerate(opts)))
    await call.message.answer(
        t("draft_choose_correct", lang, q=utils.escape_html(qtext)),
        reply_markup=draft_fix_kb(draft_id, len(opts))
    )
    await call.answer()


@router.callback_query(F.data.startswith("draftpick:"), IsAdmin())
async def cb_draft_pick(call: CallbackQuery, user: dict):
    """draftpick:{draft_id}:{index}"""
    lang = user.get('language') or 'ru'
    parts = call.data.split(":")
    if len(parts) != 3:
        await call.answer()
        return
    try:
        draft_id = int(parts[1])
        idx = int(parts[2])
    except ValueError:
        await call.answer()
        return
    quiz_importer.finalize_draft(draft_id, idx)
    await call.answer(t("draft_saved", lang), show_alert=True)
    try:
        await call.message.delete()
    except Exception:
        pass


@router.callback_query(F.data.startswith("draftdel:"), IsAdmin())
async def cb_draft_del(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    try:
        draft_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    quiz_importer.delete_draft(draft_id)
    await call.answer(t("draft_deleted", lang), show_alert=True)
    try:
        await call.message.delete()
    except Exception:
        pass


# =================================
# Просмотр вопросов теста
# =================================

@router.callback_query(F.data.startswith("admquestions:"), IsAdmin())
async def cb_admquestions(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    try:
        tid = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    rows = db.fetchall(
        "SELECT * FROM questions WHERE test_id=? ORDER BY order_num LIMIT 30", (tid,))
    if not rows:
        await call.message.answer("Вопросов нет.", reply_markup=back_kb(lang, f"admtest:{tid}"))
    else:
        lines = []
        for r in rows:
            txt = r['text'][:60]
            lines.append(f"#{r['id']}. {utils.escape_html(txt)}")
        await call.message.answer(
            "<b>Вопросы:</b>\n\n" + "\n".join(lines),
            reply_markup=back_kb(lang, f"admtest:{tid}")
        )
    await call.answer()


# =================================
# Выдача доступа к платному тесту
# =================================

@router.callback_query(F.data == "adm:grant", IsAdmin())
async def cb_grant(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    await state.set_state(GrantAccessStates.waiting_user)
    await call.message.answer(t("grant_ask_user_id", lang), reply_markup=cancel_kb(lang))
    await call.answer()


@router.message(GrantAccessStates.waiting_user, IsAdmin())
async def s_grant_user(message: Message, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    target = utils.find_user_by_arg(message.text.strip())
    if not target:
        await message.answer(t("grant_user_not_found", lang))
        return
    await state.update_data(grant_user_id=target['id'])
    await state.set_state(GrantAccessStates.waiting_test)
    await message.answer(t("grant_ask_test_id", lang))


@router.message(GrantAccessStates.waiting_test, IsAdmin())
async def s_grant_test(message: Message, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    try:
        tid = int(message.text.strip())
    except ValueError:
        await message.answer(t("error_generic", lang))
        return
    data = await state.get_data()
    uid = data.get('grant_user_id')
    utils.grant_paid_access(uid, granted_by=user['id'], test_id=tid)
    await state.clear()
    await message.answer(t("grant_done", lang), reply_markup=admin_menu_kb(lang))


# =================================
# Premium
# =================================

@router.callback_query(F.data == "adm:premium", IsAdmin())
async def cb_premium(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    await state.set_state(PremiumStates.waiting_user)
    await call.message.answer(t("premium_ask_user", lang), reply_markup=cancel_kb(lang))
    await call.answer()


@router.message(PremiumStates.waiting_user, IsAdmin())
async def s_premium_user(message: Message, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    target = utils.find_user_by_arg(message.text.strip())
    if not target:
        await message.answer(t("premium_user_not_found", lang))
        return
    await state.update_data(premium_user_id=target['id'])
    await state.set_state(PremiumStates.waiting_days)
    await message.answer(t("premium_ask_days", lang))


@router.message(PremiumStates.waiting_days, IsAdmin())
async def s_premium_days(message: Message, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    try:
        days = int(message.text.strip())
    except ValueError:
        days = 30
    data = await state.get_data()
    uid = data.get('premium_user_id')
    utils.grant_premium(uid, days, message.from_user.id)
    await state.clear()
    await message.answer(t("premium_granted", lang, days=days),
                         reply_markup=admin_menu_kb(lang))


# =================================
# Блокировка
# =================================

@router.callback_query(F.data == "adm:block", IsAdmin())
async def cb_block(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    await state.set_state(BlockStates.waiting_user)
    await call.message.answer("Введите @username или tg_id для блокировки/разблокировки:",
                              reply_markup=cancel_kb(lang))
    await call.answer()


@router.message(BlockStates.waiting_user, IsAdmin())
async def s_block(message: Message, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    target = utils.find_user_by_arg(message.text.strip())
    if not target:
        await message.answer(t("premium_user_not_found", lang))
        return
    new_state = 0 if target.get('is_blocked') else 1
    utils.set_blocked(target['id'], bool(new_state))
    await state.clear()
    msg = "✅ Заблокирован." if new_state else "✅ Разблокирован."
    await message.answer(msg, reply_markup=admin_menu_kb(lang))


# =================================
# Каналы (обязательная подписка)
# =================================

@router.callback_query(F.data == "adm:channels", IsAdmin())
async def cb_channels(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    rows = db.fetchall("SELECT * FROM required_channels WHERE is_global=1")
    if rows:
        lines = [t("channels_list", lang)]
        for r in rows:
            lines.append(f"• {r['channel_username']}")
        text = "\n".join(lines)
    else:
        text = t("channels_empty", lang)
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить", callback_data="adm:channel_add")
    if rows:
        for r in rows:
            kb.button(text=f"🗑 {r['channel_username']}", callback_data=f"chdel:{r['id']}")
    kb.button(text=t("btn_back", lang), callback_data="m:admin")
    kb.adjust(1)
    await call.message.answer(text, reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data == "adm:channel_add", IsAdmin())
async def cb_channel_add(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    await state.set_state(ChannelStates.waiting_username)
    await call.message.answer(t("channel_add_ask", lang), reply_markup=cancel_kb(lang))
    await call.answer()


@router.message(ChannelStates.waiting_username, IsAdmin())
async def s_channel(message: Message, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    ch = message.text.strip()
    if not ch:
        await message.answer(t("channel_invalid", lang))
        return
    if not ch.startswith("@"):
        ch = "@" + ch.lstrip("@")
    db.execute(
        """INSERT INTO required_channels (channel_username, is_global, created_at)
           VALUES (?, 1, ?)""", (ch, utils.now_iso()))
    await state.clear()
    await message.answer(t("channel_added", lang), reply_markup=admin_menu_kb(lang))


@router.callback_query(F.data.startswith("chdel:"), IsAdmin())
async def cb_chdel(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    try:
        cid = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    db.execute("DELETE FROM required_channels WHERE id=?", (cid,))
    await call.answer(t("channel_deleted", lang), show_alert=True)
    try:
        await call.message.delete()
    except Exception:
        pass


# =================================
# Конспекты — создание
# =================================

@router.callback_query(F.data == "adm:notes", IsAdmin())
async def cb_admin_notes(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    rows = db.fetchall("SELECT * FROM notes ORDER BY id DESC LIMIT 20")
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Новый конспект", callback_data="adm:note_new")
    for r in rows:
        kb.button(text=f"{r['id']}. {r['title'][:30]}",
                  callback_data=f"admnotedel:{r['id']}")
    kb.button(text=t("btn_back", lang), callback_data="m:admin")
    kb.adjust(1)
    await call.message.answer("📖 Управление конспектами:", reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data == "adm:note_new", IsAdmin())
async def cb_admin_note_new(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    await state.set_state(NoteCreateStates.title)
    await call.message.answer(t("ask_note_title", lang), reply_markup=cancel_kb(lang))
    await call.answer()


@router.message(NoteCreateStates.title, IsAdmin())
async def n_title(message: Message, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    await state.update_data(note_title=message.text.strip()[:200])
    await state.set_state(NoteCreateStates.description)
    await message.answer(t("ask_note_descr", lang))


@router.message(NoteCreateStates.description, IsAdmin())
async def n_descr(message: Message, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    await state.update_data(note_description=message.text.strip()[:500])
    await state.set_state(NoteCreateStates.subject)
    await message.answer(t("ask_note_subject", lang))


@router.message(NoteCreateStates.subject, IsAdmin())
async def n_subject(message: Message, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    await state.update_data(note_subject=message.text.strip()[:100])
    await state.set_state(NoteCreateStates.category)
    await message.answer(t("ask_note_category", lang))


@router.message(NoteCreateStates.category, IsAdmin())
async def n_category(message: Message, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    await state.update_data(note_category=message.text.strip()[:100])
    await state.set_state(NoteCreateStates.language)
    await message.answer(t("ask_note_lang", lang), reply_markup=admin_lang_kb("note_lang"))


@router.callback_query(NoteCreateStates.language, F.data.startswith("note_lang:"), IsAdmin())
async def n_lang(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    l = call.data.split(":")[1]
    await state.update_data(note_language=l)
    await state.set_state(NoteCreateStates.access_type)
    await call.message.answer(t("ask_note_access", lang),
                              reply_markup=note_access_kb(lang))
    await call.answer()


@router.callback_query(NoteCreateStates.access_type, F.data.startswith("note_access:"), IsAdmin())
async def n_access(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    at = call.data.split(":")[1]
    await state.update_data(note_access=at)
    if at == 'paid':
        await state.set_state(NoteCreateStates.price)
        await call.message.answer(t("ask_note_price", lang))
    else:
        await state.update_data(note_price=0)
        await state.set_state(NoteCreateStates.pages)
        await call.message.answer(t("ask_note_page", lang),
                                  reply_markup=note_pages_done_kb(lang))
    await call.answer()


@router.message(NoteCreateStates.price, IsAdmin())
async def n_price(message: Message, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    try:
        p = max(0, int(message.text.strip()))
    except ValueError:
        p = 0
    await state.update_data(note_price=p)
    await state.set_state(NoteCreateStates.pages)
    await state.update_data(pages_collected=[])
    await message.answer(t("ask_note_page", lang),
                         reply_markup=note_pages_done_kb(lang))


@router.message(NoteCreateStates.pages, IsAdmin())
async def n_pages(message: Message, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    text = message.text or message.caption or ""
    image_file_id = None
    if message.photo:
        image_file_id = message.photo[-1].file_id
    if not text and not image_file_id:
        return
    data = await state.get_data()
    pages = data.get('pages_collected', [])
    pages.append({'content': text[:4000], 'image_file_id': image_file_id})
    await state.update_data(pages_collected=pages)
    await message.answer(t("note_page_saved", lang, n=len(pages)))


@router.callback_query(NoteCreateStates.pages, F.data == "note:pages_done", IsAdmin())
async def n_pages_done(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    data = await state.get_data()
    pages = data.get('pages_collected', [])
    if not pages:
        await call.answer(t("note_at_least_one_page", lang), show_alert=True)
        return
    note_id = notes_service.create_note(
        title=data.get('note_title', ''),
        description=data.get('note_description', ''),
        subject=data.get('note_subject', ''),
        category=data.get('note_category', ''),
        language=data.get('note_language', lang),
        access_type=data.get('note_access', 'free'),
        price=data.get('note_price', 0),
        created_by=user['id'],
    )
    for i, p in enumerate(pages, start=1):
        notes_service.add_page(note_id, p['content'], i, p['image_file_id'])
    await state.clear()
    await call.message.answer(t("note_created", lang, id=note_id),
                              reply_markup=admin_menu_kb(lang))
    await call.answer()


@router.callback_query(F.data.startswith("admnotedel:"), IsAdmin())
async def cb_admnote_del(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    try:
        nid = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    notes_service.delete_note(nid)
    await call.answer("🗑 Удалено", show_alert=True)
    try:
        await call.message.delete()
    except Exception:
        pass


# =================================
# Статистика
# =================================

@router.callback_query(F.data == "adm:stats", IsAdmin())
async def cb_stats(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    users = db.fetchone("SELECT COUNT(*) AS c FROM users")['c']
    tests = db.fetchone("SELECT COUNT(*) AS c FROM tests")['c']
    attempts = db.fetchone("SELECT COUNT(*) AS c FROM test_attempts")['c']
    notes = db.fetchone("SELECT COUNT(*) AS c FROM notes")['c']
    premium = db.fetchone("SELECT COUNT(*) AS c FROM premium_users")['c']
    duels = db.fetchone("SELECT COUNT(*) AS c FROM duels WHERE status='finished'")['c']
    text = t("stats_text", lang,
             users=users, tests=tests, attempts=attempts,
             notes=notes, premium=premium, duels=duels)
    try:
        await call.message.edit_text(text, reply_markup=back_kb(lang, "m:admin"))
    except Exception:
        await call.message.answer(text, reply_markup=back_kb(lang, "m:admin"))
    await call.answer()


# =================================
# Экспорт CSV
# =================================

@router.callback_query(F.data == "adm:export", IsAdmin())
async def cb_export(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    rows = db.fetchall("""
        SELECT u.tg_id, u.username, u.first_name, u.language, u.school, u.city,
               u.current_streak, u.best_streak,
               (SELECT COUNT(*) FROM test_attempts a WHERE a.user_id=u.id AND a.status='finished') AS attempts,
               (SELECT COALESCE(SUM(score),0) FROM test_attempts a WHERE a.user_id=u.id AND a.is_counted=1) AS total_score
        FROM users u
    """)
    if not rows:
        await call.answer(t("export_no_data", lang), show_alert=True)
        return
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['tg_id', 'username', 'first_name', 'language', 'school', 'city',
                'current_streak', 'best_streak', 'attempts', 'total_score'])
    for r in rows:
        w.writerow([r['tg_id'], r['username'] or '', r['first_name'] or '',
                    r['language'], r['school'] or '', r['city'] or '',
                    r['current_streak'], r['best_streak'],
                    r['attempts'], r['total_score']])
    data = buf.getvalue().encode('utf-8')
    file = BufferedInputFile(data, filename="users.csv")
    await call.message.answer_document(file, caption=t("export_caption", lang))
    await call.answer()
