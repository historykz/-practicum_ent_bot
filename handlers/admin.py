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
                     ChannelStates, NoteCreateStates, DraftFixStates,
                     AdminMgmtStates)
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
@router.callback_query(F.data == "adm:menu", IsAdmin())
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
    await state.clear()
    await state.set_state(TestCreateStates.title)
    await call.message.answer(t("ask_test_title", lang), reply_markup=cancel_kb(lang))
    await call.answer()


@router.message(TestCreateStates.title, IsAdmin())
async def s_title(message: Message, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    await state.update_data(title=message.text.strip()[:200])
    # Сразу спрашиваем язык — описание, предмет, класс, категория, тип пропускаем
    await state.set_state(TestCreateStates.language)
    await message.answer(t("ask_test_lang", lang), reply_markup=admin_lang_kb("newtest_lang"))


@router.callback_query(TestCreateStates.language, F.data.startswith("newtest_lang:"), IsAdmin())
async def s_language(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    tl = call.data.split(":")[1]
    await state.update_data(language=tl)
    # Выбор раздела
    cats = db.fetchall("SELECT * FROM test_categories ORDER BY sort_order, id")
    await state.set_state(TestCreateStates.category_select)
    kb = InlineKeyboardBuilder()
    if cats:
        for c in cats[:20]:
            emoji = c.get('emoji') or '📚'
            kb.button(text=f"{emoji} {c['name']}", callback_data=f"newcat:{c['id']}")
    kb.button(text="📭 Без раздела", callback_data="newcat:none")
    kb.button(text=t("btn_cancel", lang), callback_data="cancel")
    kb.adjust(1)
    if not cats:
        await call.message.answer(
            "📂 <b>Выбор раздела</b>\n\n"
            "У вас пока нет ни одного раздела. Тест будет без раздела.\n\n"
            "<i>Чтобы добавить разделы — отмените создание теста и перейдите "
            "в /admin → 📂 Разделы.</i>",
            reply_markup=kb.as_markup(), parse_mode="HTML")
    else:
        await call.message.answer(
            "📂 <b>Выберите раздел для теста:</b>",
            reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(TestCreateStates.category_select, F.data.startswith("newcat:"), IsAdmin())
async def s_category_choice(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    arg = call.data.split(":")[1]
    if arg == "none":
        await state.update_data(category_id=None)
    else:
        try:
            await state.update_data(category_id=int(arg))
        except ValueError:
            pass
    # Дальше — время
    await state.set_state(TestCreateStates.time_per_question)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏱ 30 сек", callback_data="newtime:30")
    kb.button(text="⏱ 45 сек", callback_data="newtime:45")
    kb.button(text="⏱ 60 сек", callback_data="newtime:60")
    kb.button(text="⏱ 5 мин", callback_data="newtime:300")
    kb.button(text="✏️ Вручную", callback_data="newtime:custom")
    kb.button(text=t("btn_cancel", lang), callback_data="cancel")
    kb.adjust(2)
    await call.message.answer("⏱ Сколько секунд на каждый вопрос?",
                                reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(TestCreateStates.time_per_question, F.data.startswith("newtime:"), IsAdmin())
async def s_time_btn(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    arg = call.data.split(":")[1]
    if arg == "custom":
        await call.message.answer("Введите количество секунд (5–600):",
                                    reply_markup=cancel_kb(lang))
        await call.answer()
        return
    try:
        tpq = max(5, min(600, int(arg)))
    except ValueError:
        tpq = config.DEFAULT_TIME_PER_QUESTION
    await state.update_data(time_per_question=tpq)
    await _ask_paid(call.message, state, lang)
    await call.answer()


@router.message(TestCreateStates.time_per_question, IsAdmin())
async def s_time_manual(message: Message, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    try:
        tpq = max(5, min(600, int(message.text.strip())))
    except ValueError:
        tpq = config.DEFAULT_TIME_PER_QUESTION
    await state.update_data(time_per_question=tpq)
    await _ask_paid(message, state, lang)


async def _ask_paid(msg, state: FSMContext, lang: str):
    await state.set_state(TestCreateStates.is_paid)
    await msg.answer(t("ask_test_paid", lang),
                       reply_markup=yes_no_kb("newtest_paid", lang))


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
        # Всё, финализируем — остальные настройки берём по умолчанию
        await _finish_create_test(call.bot, call.message.chat.id, state, user)
    await call.answer()


@router.message(TestCreateStates.price, IsAdmin())
async def s_price(message: Message, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    try:
        p = max(0, int(message.text.strip()))
    except ValueError:
        p = 0
    await state.update_data(price=p)
    await _finish_create_test(message.bot, message.chat.id, state, user)


async def _finish_create_test(bot: Bot, chat_id: int, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    data = await state.get_data()
    # Применяем дефолты для всего что не спрашивали
    data.setdefault('description', '')
    data.setdefault('subject', '')
    data.setdefault('grade', 0)
    data.setdefault('category', '')
    data.setdefault('test_type', 'regular')  # обычный тест
    data.setdefault('attempts_limit', None)
    data.setdefault('first_attempt_only', True)   # ← по умолчанию ДА
    data.setdefault('shuffle_questions', True)    # ← по умолчанию ДА
    data.setdefault('shuffle_options', True)      # ← по умолчанию ДА
    data.setdefault('show_correct', False)        # ← по умолчанию НЕТ
    data.setdefault('show_explanation', False)    # ← по умолчанию НЕТ
    data.setdefault('required_channel', None)
    data.setdefault('is_paid', False)
    data.setdefault('price', 0)
    db.execute(
        """INSERT INTO tests
           (title, description, subject, grade, category, category_id, language, test_type,
            time_per_question, attempts_limit, first_attempt_only, is_paid, price,
            shuffle_questions, shuffle_options, show_correct, show_explanation,
            required_channel, allow_duel, status, created_by, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'active',?,?)""",
        (
         data['title'], data['description'], data['subject'], data['grade'],
         data['category'], data.get('category_id'),
         data['language'], data['test_type'],
         data['time_per_question'], data['attempts_limit'],
         1 if data['first_attempt_only'] else 0,
         1 if data['is_paid'] else 0, data['price'],
         1 if data['shuffle_questions'] else 0,
         1 if data['shuffle_options'] else 0,
         1 if data['show_correct'] else 0,
         1 if data['show_explanation'] else 0,
         data['required_channel'],
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
    """Карточка теста для админа. Crash-proof."""
    lang = (user or {}).get('language') or 'ru'
    try:
        tid = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return

    # Получаем тест
    test_dict = {}
    try:
        row = db.fetchone("SELECT * FROM tests WHERE id=?", (tid,))
        if row:
            test_dict = dict(row)
    except Exception as e:
        log.warning("admtest fetch error: %s", e)

    if not test_dict:
        await call.answer("⚠️ Тест не найден.", show_alert=True)
        return

    # Подсчёт вопросов
    qcount = 0
    try:
        r = db.fetchone("SELECT COUNT(*) AS c FROM questions WHERE test_id=?", (tid,))
        qcount = (r and r['c']) or 0
    except Exception:
        pass

    # Статус (перевод не обязателен)
    status_raw = test_dict.get('status') or 'unknown'
    status_label = status_raw
    try:
        from locales import t as _t
        translated = _t(f"test_status_{status_raw}", lang)
        if translated and not translated.startswith("[?"):
            status_label = translated
    except Exception:
        pass

    is_private = bool(test_dict.get('is_private') or 0)
    title = test_dict.get('title') or '—'

    # Безопасный escape
    try:
        title_safe = utils.escape_html(title)
    except Exception:
        title_safe = str(title).replace("<", "&lt;").replace(">", "&gt;")

    text = (
        f"<b>{title_safe}</b>\n\n"
        f"ID: {tid}\n"
        f"Тип: {test_dict.get('test_type') or '—'}\n"
        f"Язык: {test_dict.get('language') or '—'}\n"
        f"Статус: {status_label}\n"
        f"Вопросов: {qcount}\n"
        f"Платный: {'да' if test_dict.get('is_paid') else 'нет'}\n"
        f"Приватный: {'🔐 ДА' if is_private else 'нет'}"
    )

    # Клавиатура — тоже crash-proof
    kb = None
    try:
        kb = admin_test_actions_kb(tid, lang, is_private=is_private)
    except Exception as e:
        log.warning("admin_test_actions_kb error: %s", e)
        try:
            kb = admin_test_actions_kb(tid, lang)
        except Exception:
            pass

    # Отправляем (всё в try)
    sent = False
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        sent = True
    except Exception as e1:
        log.info("admtest edit failed: %s", e1)
        try:
            await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
            sent = True
        except Exception as e2:
            log.warning("admtest answer failed: %s", e2)
            try:
                # Последняя попытка — без HTML
                await call.message.answer(
                    f"Тест #{tid}: {title}\nСтатус: {status_label}\nВопросов: {qcount}",
                    reply_markup=kb)
                sent = True
            except Exception as e3:
                log.error("admtest TOTAL FAIL: %s", e3)

    try:
        await call.answer()
    except Exception:
        pass


@router.callback_query(F.data.startswith("admpriv:"), IsAdmin())
async def cb_admpriv(call: CallbackQuery, user: dict):
    """Переключение приватности теста."""
    try:
        parts = call.data.split(":")
        tid = int(parts[1])
        new_value = int(parts[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    try:
        test = db.fetchone("SELECT * FROM tests WHERE id=?", (tid,))
    except Exception as e:
        await call.answer(f"Ошибка БД: {e}", show_alert=True)
        return
    if not test:
        await call.answer("Тест не найден.", show_alert=True)
        return
    try:
        db.execute("UPDATE tests SET is_private=? WHERE id=?", (new_value, tid))
    except Exception as e:
        await call.answer(
            f"⚠️ Колонка is_private отсутствует в БД. "
            f"Перезапустите сервис, чтобы применилась миграция. ({e})",
            show_alert=True)
        return
    if new_value:
        await call.answer(
            "✅ Тест теперь ПРИВАТНЫЙ. Видны только тем, кому выдали доступ через /opens",
            show_alert=True)
    else:
        await call.answer("✅ Приватный режим снят. Тест снова публичный.", show_alert=True)
    call.data = f"admtest:{tid}"
    await cb_admtest(call, user)


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
    """Меню Premium-управления."""
    lang = user.get('language') or 'ru'
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Выдать Premium", callback_data="adm:premium:grant")
    kb.button(text="📋 Список Premium", callback_data="adm:premium:list")
    kb.button(text="🗑 Удалить Premium", callback_data="adm:premium:revoke")
    kb.button(text="↩️ Назад", callback_data="adm:menu")
    kb.adjust(1)
    await call.message.answer(
        "👑 <b>Управление Premium</b>\n\nВыберите действие:",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data == "adm:premium:grant", IsAdmin())
async def cb_premium_grant(call: CallbackQuery, state: FSMContext, user: dict):
    """Начало выдачи Premium."""
    lang = user.get('language') or 'ru'
    await state.set_state(PremiumStates.waiting_user)
    await call.message.answer(t("premium_ask_user", lang), reply_markup=cancel_kb(lang))
    await call.answer()


@router.callback_query(F.data == "adm:premium:list", IsAdmin())
async def cb_premium_list(call: CallbackQuery, user: dict):
    """Список всех Premium-пользователей."""
    from datetime import datetime
    rows = db.fetchall("""
        SELECT p.user_id, p.granted_at, p.expires_at, p.granted_by_admin,
               u.tg_id, u.username, u.first_name
        FROM premium_users p
        JOIN users u ON u.id = p.user_id
        ORDER BY p.granted_at DESC
        LIMIT 100
    """)
    if not rows:
        await call.message.answer(
            "📋 Сейчас нет активных Premium-пользователей.",
            reply_markup=admin_menu_kb(user.get('language') or 'ru'))
        await call.answer()
        return

    now = datetime.utcnow()
    active_lines = []
    expired_lines = []
    for r in rows:
        uname = ("@" + r['username']) if r['username'] else (r['first_name'] or f"id{r['tg_id']}")
        granted = (r['granted_at'] or "")[:10]
        exp = r['expires_at']

        # Статус и оставшиеся дни
        if not exp:
            status = "♾ бессрочно"
            days_left_str = "—"
            is_active = True
        else:
            try:
                exp_dt = datetime.fromisoformat(exp)
                if exp_dt > now:
                    days_left = (exp_dt - now).days
                    hours_left = ((exp_dt - now).seconds // 3600)
                    if days_left > 0:
                        days_left_str = f"{days_left} дн."
                    else:
                        days_left_str = f"{hours_left} ч."
                    status = f"✅ до {exp[:10]}"
                    is_active = True
                else:
                    status = f"❌ истёк {exp[:10]}"
                    days_left_str = "—"
                    is_active = False
            except Exception:
                status = "?"
                days_left_str = "?"
                is_active = False

        # Сколько дней УЖЕ прошло с момента выдачи
        try:
            granted_dt = datetime.fromisoformat(r['granted_at'])
            days_passed = (now - granted_dt).days
            passed_str = f"{days_passed} дн. назад"
        except Exception:
            passed_str = "—"

        line = (f"<b>{uname}</b>  (tg_id: <code>{r['tg_id']}</code>)\n"
                f"  📅 Выдано: {granted} ({passed_str})\n"
                f"  ⏳ Осталось: {days_left_str}\n"
                f"  📌 Статус: {status}")
        if is_active:
            active_lines.append(line)
        else:
            expired_lines.append(line)

    parts = []
    if active_lines:
        parts.append(f"✅ <b>Активные ({len(active_lines)}):</b>\n\n" + "\n\n".join(active_lines))
    if expired_lines:
        parts.append(f"\n❌ <b>Истёкшие ({len(expired_lines)}):</b>\n\n" + "\n\n".join(expired_lines))

    # Делим на куски по ~3500 симв, чтобы Telegram не ругался
    full = "\n\n".join(parts) if parts else "Пусто."
    chunks = []
    cur = ""
    for line in full.split("\n\n"):
        if len(cur) + len(line) + 2 > 3500:
            chunks.append(cur)
            cur = line
        else:
            cur = (cur + "\n\n" + line) if cur else line
    if cur:
        chunks.append(cur)

    for chunk in chunks:
        await call.message.answer(chunk, parse_mode="HTML")

    await call.message.answer(
        f"Всего: <b>{len(rows)}</b> | активных: <b>{len(active_lines)}</b> | "
        f"истёкших: <b>{len(expired_lines)}</b>",
        reply_markup=admin_menu_kb(user.get('language') or 'ru'),
    )
    await call.answer()


@router.callback_query(F.data == "adm:premium:revoke", IsAdmin())
async def cb_premium_revoke(call: CallbackQuery, state: FSMContext, user: dict):
    """Удаление Premium."""
    lang = user.get('language') or 'ru'
    await state.set_state(PremiumStates.waiting_revoke_user)
    await call.message.answer(
        "Введите @username или tg_id пользователя, у которого нужно убрать Premium:",
        reply_markup=cancel_kb(lang))
    await call.answer()


@router.message(PremiumStates.waiting_revoke_user, IsAdmin())
async def s_premium_revoke(message: Message, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    target = utils.find_user_by_arg(message.text.strip())
    if not target:
        await message.answer(t("premium_user_not_found", lang))
        return
    info = utils.get_premium_info(target['id'])
    if not info:
        await message.answer(
            f"❗️ У пользователя нет Premium — нечего удалять.",
            reply_markup=admin_menu_kb(lang))
        await state.clear()
        return
    utils.revoke_premium(target['id'])
    uname = ("@" + target['username']) if target.get('username') else f"id{target['tg_id']}"
    await message.answer(
        f"✅ Premium удалён у пользователя <b>{uname}</b>.",
        reply_markup=admin_menu_kb(lang))
    await state.clear()


@router.message(PremiumStates.waiting_user, IsAdmin())
async def s_premium_user(message: Message, state: FSMContext, user: dict):
    """Шаг 1: ввели юзера для выдачи Premium."""
    lang = user.get('language') or 'ru'
    target = utils.find_user_by_arg(message.text.strip())
    if not target:
        await message.answer(t("premium_user_not_found", lang))
        return
    await state.update_data(premium_user_id=target['id'])

    # Покажем текущий статус
    info = utils.get_premium_info(target['id'])
    uname = ("@" + target['username']) if target.get('username') else f"id{target['tg_id']}"
    status_text = ""
    if info:
        exp = info.get('expires_at')
        from datetime import datetime
        now = datetime.utcnow()
        if not exp:
            status_text = f"⚠️ У <b>{uname}</b> уже есть <b>бессрочный Premium</b>.\nНовое значение перезапишет старое.\n\n"
        else:
            try:
                exp_dt = datetime.fromisoformat(exp)
                if exp_dt > now:
                    days_left = (exp_dt - now).days
                    status_text = (
                        f"⚠️ У <b>{uname}</b> уже есть Premium до <b>{exp[:10]}</b> "
                        f"(осталось {days_left} дн.).\n"
                        f"Новый срок <b>перезапишет</b> старый (не прибавится к нему).\n\n"
                    )
                else:
                    status_text = f"ℹ️ У {uname} был Premium до {exp[:10]} — истёк. Можно выдать заново.\n\n"
            except Exception:
                pass

    await state.set_state(PremiumStates.waiting_days)
    await message.answer(
        f"{status_text}{t('premium_ask_days', lang)}",
        parse_mode="HTML")


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

    # Информация для админа
    info = utils.get_premium_info(uid)
    target = db.fetchone("SELECT tg_id, username, language FROM users WHERE id=?", (uid,))
    uname = ("@" + target['username']) if target and target['username'] else f"id{target['tg_id'] if target else '?'}"
    if days == 0:
        result_text = f"✅ <b>{uname}</b> получил <b>бессрочный</b> Premium."
        until_str = "бессрочно"
    else:
        exp = info.get('expires_at') if info else None
        until_str = exp[:10] if exp else "—"
        result_text = (f"✅ <b>{uname}</b> получил Premium на <b>{days} дн.</b>\n"
                       f"📅 Действует до: <b>{until_str}</b>")
    await message.answer(result_text, parse_mode="HTML",
                         reply_markup=admin_menu_kb(lang))

    # ── Уведомление самому пользователю + список платных тестов ──
    if target and target.get('tg_id'):
        try:
            # Список ВСЕХ платных тестов (без фильтра по языку, чтобы точно показать)
            paid_tests = db.fetchall(
                """SELECT id, title, subject, time_per_question
                   FROM tests
                   WHERE is_paid=1 AND status='active'
                     AND COALESCE(is_private,0)=0
                   ORDER BY id DESC LIMIT 30""")

            duration_text = ("♾ <b>Бессрочно</b>" if days == 0
                              else f"⏱ <b>{days} дн.</b> (до <b>{until_str}</b>)")

            congrats = (
                "🎉 <b>Поздравляем! Вы получили Premium-доступ!</b>\n\n"
                f"💎 Срок действия: {duration_text}\n\n"
                "<b>Ваши новые привилегии:</b>\n"
                "✅ Доступ ко всем платным тестам\n"
                "✅ Полные разделы и материалы для подготовки\n"
                "✅ Quiz-формат с таймером (как на ЕНТ)\n"
                "✅ Новые тесты сразу после добавления\n"
                "✅ Расширенная статистика результатов\n"
                "✅ Приоритетная поддержка\n\n"
            )

            from aiogram.utils.keyboard import InlineKeyboardBuilder
            kb = InlineKeyboardBuilder()

            if paid_tests:
                congrats += f"🔓 <b>Платных тестов открыто: {len(paid_tests)}</b>\n"
                congrats += "Выберите тест, чтобы начать:"
                for tst in paid_tests[:15]:  # макс 15 кнопок
                    title_short = (tst['title'] or '—')[:45]
                    kb.button(text=f"💎 {title_short}",
                              callback_data=f"opentest:{tst['id']}")
                kb.button(text="📚 Все тесты", callback_data="m:tests")
            else:
                congrats += "📚 Откройте каталог тестов в главном меню."
                kb.button(text="📚 Главное меню", callback_data="m:menu")

            kb.adjust(1)
            await message.bot.send_message(
                target['tg_id'], congrats,
                reply_markup=kb.as_markup(), parse_mode="HTML")
        except Exception as e:
            log.warning("Premium notify error: %s", e)
            await message.answer(f"⚠️ Premium выдан, но уведомление не дошло: {e}")


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
    """Меню экспорта + удаления результатов."""
    lang = user.get('language') or 'ru'
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Список пользователей", callback_data="adm:export:users")
    kb.button(text="📊 Результаты тестов", callback_data="adm:export:results")
    kb.button(text="🔐 Результаты приватных тестов", callback_data="adm:export:privresults")
    kb.button(text="🗑 Удалить ВСЕ результаты", callback_data="adm:delresults:all")
    kb.button(text="🗑 Удалить результаты приватных", callback_data="adm:delresults:private")
    kb.button(text="↩️ Назад", callback_data="adm:menu")
    kb.adjust(1)
    await call.message.answer(
        "📤 <b>Экспорт и управление результатами</b>",
        reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "adm:export:privresults", IsAdmin())
async def cb_export_private_results(call: CallbackQuery, user: dict):
    """Экспорт только результатов приватных тестов."""
    import csv, io
    rows = db.fetchall("""
        SELECT ts.*, t.title AS test_title, u.username, u.first_name
        FROM test_statistics ts
        JOIN tests t ON t.id = ts.test_id
        LEFT JOIN users u ON u.tg_id = ts.tg_id
        WHERE t.is_private=1
        ORDER BY ts.finished_at DESC LIMIT 5000""")
    if not rows:
        await call.answer("Нет результатов по приватным тестам.", show_alert=True)
        return
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['test_id', 'test_title', 'tg_id', 'username', 'first_name',
                 'score', 'correct', 'wrong', 'total_questions',
                 'time_seconds', 'finished_at'])
    for r in rows:
        w.writerow([r.get('test_id'), r.get('test_title'),
                     r.get('tg_id'), r.get('username') or '',
                     r.get('first_name') or '',
                     r.get('score'), r.get('correct'), r.get('wrong'),
                     r.get('total_questions'), r.get('total_time_seconds'),
                     r.get('finished_at')])
    from aiogram.types import BufferedInputFile
    file = BufferedInputFile(buf.getvalue().encode('utf-8'),
                               filename="private_results.csv")
    await call.message.answer_document(file,
                                          caption=f"🔐 Результаты приватных тестов: {len(rows)} строк")
    await call.answer()


@router.callback_query(F.data.startswith("adm:delresults:"), IsAdmin())
async def cb_del_results_confirm(call: CallbackQuery, user: dict):
    """Подтверждение удаления результатов."""
    scope = call.data.split(":")[2]  # all или private
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить", callback_data=f"adm:delresults:confirm:{scope}")
    kb.button(text="❌ Отмена", callback_data="adm:export")
    kb.adjust(1)
    label = "ВСЕ результаты" if scope == "all" else "результаты приватных тестов"
    await call.message.answer(
        f"⚠️ Вы точно хотите удалить <b>{label}</b>?\n\n"
        f"Это действие <b>необратимо</b>!",
        reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("adm:delresults:confirm:"), IsAdmin())
async def cb_del_results_apply(call: CallbackQuery, user: dict):
    """Выполняем удаление."""
    scope = call.data.split(":")[3]
    if scope == "private":
        # Удаляем только статистику и попытки по приватным тестам
        deleted_stat = db.execute("""
            DELETE FROM test_statistics
            WHERE test_id IN (SELECT id FROM tests WHERE is_private=1)
        """).rowcount
        deleted_att = db.execute("""
            DELETE FROM test_attempts
            WHERE test_id IN (SELECT id FROM tests WHERE is_private=1)
        """).rowcount
        await call.message.answer(
            f"✅ <b>Результаты приватных тестов удалены</b>\n\n"
            f"📊 Статистики: {deleted_stat}\n"
            f"📝 Попыток: {deleted_att}",
            parse_mode="HTML")
    else:
        deleted_stat = db.execute("DELETE FROM test_statistics").rowcount
        deleted_att = db.execute("DELETE FROM test_attempts").rowcount
        deleted_ans = db.execute("DELETE FROM attempt_answers").rowcount
        await call.message.answer(
            f"✅ <b>Все результаты удалены</b>\n\n"
            f"📊 Статистики: {deleted_stat}\n"
            f"📝 Попыток: {deleted_att}\n"
            f"💬 Ответов: {deleted_ans}",
            parse_mode="HTML")
    await call.answer("✅")


@router.callback_query(F.data == "adm:export:users", IsAdmin())
async def cb_export_users(call: CallbackQuery, user: dict):
    """Экспорт пользователей."""
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
    data = buf.getvalue().encode('utf-8-sig')  # BOM для корректной открытия в Excel
    file = BufferedInputFile(data, filename="users.csv")
    await call.message.answer_document(file, caption=f"👥 Экспорт пользователей: {len(rows)} строк")
    await call.answer()


@router.callback_query(F.data == "adm:export:results", IsAdmin())
async def cb_export_results(call: CallbackQuery, user: dict):
    """
    Экспорт результатов тестов: ник, tg_id, тест, балл, правильные, неправильные,
    пропущенные, проценты, длительность, начало, конец.
    """
    rows = db.fetchall("""
        SELECT
            a.id AS attempt_id,
            u.tg_id,
            u.username,
            u.first_name,
            u.school,
            t.id AS test_id,
            t.title AS test_title,
            t.subject,
            t.language AS test_lang,
            t.is_paid,
            a.start_time,
            a.end_time,
            a.score,
            a.correct_answers,
            a.wrong_answers,
            a.skipped_answers,
            a.status,
            a.is_counted
        FROM test_attempts a
        JOIN users u ON u.id = a.user_id
        JOIN tests t ON t.id = a.test_id
        WHERE a.status IN ('finished', 'aborted')
        ORDER BY a.start_time DESC
        LIMIT 5000
    """)
    if not rows:
        await call.answer("Нет данных для экспорта", show_alert=True)
        return

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=';')  # ; для русского Excel
    w.writerow([
        'attempt_id', 'tg_id', 'username', 'first_name', 'school',
        'test_id', 'test_title', 'subject', 'language', 'is_paid',
        'start_time', 'end_time', 'duration_seconds',
        'score', 'correct', 'wrong', 'skipped', 'total_questions',
        'percent', 'status', 'is_counted',
    ])

    from datetime import datetime as _dt

    for r in rows:
        # Длительность в секундах
        duration = ""
        if r['start_time'] and r['end_time']:
            try:
                st = _dt.fromisoformat(r['start_time'])
                en = _dt.fromisoformat(r['end_time'])
                duration = int((en - st).total_seconds())
            except Exception:
                duration = ""
        correct = r['correct_answers'] or 0
        wrong = r['wrong_answers'] or 0
        skipped = r['skipped_answers'] or 0
        total = correct + wrong + skipped
        percent = round(correct * 100 / total, 1) if total > 0 else 0

        w.writerow([
            r['attempt_id'],
            r['tg_id'],
            r['username'] or '',
            r['first_name'] or '',
            r['school'] or '',
            r['test_id'],
            r['test_title'] or '',
            r['subject'] or '',
            r['test_lang'] or '',
            'да' if r['is_paid'] else 'нет',
            r['start_time'] or '',
            r['end_time'] or '',
            duration,
            r['score'] or 0,
            correct,
            wrong,
            skipped,
            total,
            percent,
            r['status'] or '',
            'да' if r['is_counted'] else 'нет',
        ])

    data = buf.getvalue().encode('utf-8-sig')  # BOM для Excel
    file = BufferedInputFile(data, filename="test_results.csv")
    await call.message.answer_document(
        file,
        caption=(
            f"📊 <b>Результаты тестов</b>\n"
            f"Всего записей: <b>{len(rows)}</b>\n\n"
            f"Колонки: ник, tg_id, школа, тест, балл, %, правильные/неправильные/пропущенные, "
            f"длительность, время начала/конца."
        ),
        parse_mode="HTML")
    await call.answer()


# ============ Управление админами ============

def _is_super_admin(tg_id: int) -> bool:
    """Супер-админ = указанный в переменной ADMIN_IDS (config.py).
    Только супер-админ может добавлять/удалять других админов."""
    return tg_id in (config.ADMIN_IDS or [])


@router.callback_query(F.data == "adm:admins", IsAdmin())
async def cb_admins_menu(call: CallbackQuery, state: FSMContext, user: dict):
    """Меню управления админами."""
    await state.clear()  # сбрасываем любой залипший FSM
    if not _is_super_admin(call.from_user.id):
        await call.answer(
            "⛔ Управлять админами может только главный администратор "
            "(указанный в настройках сервера).",
            show_alert=True)
        return

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить админа", callback_data="adm:admins:add")
    kb.button(text="📋 Список админов", callback_data="adm:admins:list")
    kb.button(text="🗑 Убрать админа", callback_data="adm:admins:remove")
    kb.button(text="↩️ Назад", callback_data="adm:menu")
    kb.adjust(1)

    text = (
        "🛠 <b>Управление администраторами</b>\n\n"
        "Здесь можно выдавать и забирать админ-доступ другим пользователям. "
        "У всех админов одинаковые права: создание тестов, выдача Premium, "
        "запуск тестов в группах, статистика и т.д.\n\n"
        "ℹ️ Главный админ (вы) указан в настройках сервера и не может быть "
        "убран отсюда."
    )
    try:
        await call.message.edit_text(text, reply_markup=kb.as_markup(),
                                       parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb.as_markup(),
                                    parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "adm:admins:list", IsAdmin())
async def cb_admins_list(call: CallbackQuery, state: FSMContext, user: dict):
    """Список всех админов: и из ADMIN_IDS, и из таблицы admins."""
    await state.clear()
    if not _is_super_admin(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return

    lines = ["🛠 <b>Список администраторов</b>\n"]

    # Супер-админы из ADMIN_IDS
    lines.append("👑 <b>Главные (из настроек сервера):</b>")
    if config.ADMIN_IDS:
        for tg_id in config.ADMIN_IDS:
            # Попробуем найти username/имя
            u = db.fetchone(
                "SELECT username, first_name FROM users WHERE tg_id=?", (tg_id,))
            label = f"<code>{tg_id}</code>"
            if u:
                if u['username']:
                    label = f"@{u['username']} ({tg_id})"
                elif u['first_name']:
                    label = f"{u['first_name']} ({tg_id})"
            lines.append(f"  • {label}")
    else:
        lines.append("  <i>(нет)</i>")

    # Рантайм-админы из таблицы
    rows = db.fetchall(
        """SELECT a.tg_id, a.granted_by, a.created_at, u.username, u.first_name
           FROM admins a
           LEFT JOIN users u ON u.tg_id = a.tg_id
           ORDER BY a.created_at DESC""")
    lines.append("\n🛠 <b>Добавленные через бот:</b>")
    if rows:
        for r in rows:
            label = f"<code>{r['tg_id']}</code>"
            if r['username']:
                label = f"@{r['username']} ({r['tg_id']})"
            elif r['first_name']:
                label = f"{r['first_name']} ({r['tg_id']})"
            date = (r['created_at'] or "")[:10]
            lines.append(f"  • {label}  <i>· {date}</i>")
    else:
        lines.append("  <i>(никого пока не добавляли)</i>")

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ Назад", callback_data="adm:admins")
    kb.adjust(1)

    await call.message.answer("\n".join(lines),
                                reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "adm:admins:add", IsAdmin())
async def cb_admins_add(call: CallbackQuery, state: FSMContext, user: dict):
    """Начало добавления админа."""
    if not _is_super_admin(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return
    await state.set_state(AdminMgmtStates.waiting_add_user)
    await call.message.answer(
        "👤 Введите <b>@username</b> или <b>tg_id</b> пользователя, "
        "которому хотите дать админ-права.\n\n"
        "ℹ️ Пользователь должен <b>хотя бы раз написать</b> боту (/start), "
        "иначе мы не сможем его найти.",
        parse_mode="HTML")
    await call.answer()


@router.message(AdminMgmtStates.waiting_add_user, IsAdmin())
async def s_admin_add(message: Message, state: FSMContext, user: dict):
    if not _is_super_admin(message.from_user.id):
        await state.clear()
        return
    arg = (message.text or "").strip()
    target = utils.find_user_by_arg(arg)
    if not target:
        # Если ввели чистый tg_id — добавим даже если нет в users
        if arg.isdigit():
            tg_id = int(arg)
            existing_admin = db.fetchone(
                "SELECT id FROM admins WHERE tg_id=?", (tg_id,))
            if existing_admin:
                await message.answer(
                    f"ℹ️ Пользователь с tg_id <code>{tg_id}</code> уже админ.",
                    parse_mode="HTML")
                await state.clear()
                return
            if tg_id in (config.ADMIN_IDS or []):
                await message.answer(
                    f"ℹ️ <code>{tg_id}</code> уже главный админ (из настроек сервера).",
                    parse_mode="HTML")
                await state.clear()
                return
            db.execute(
                "INSERT INTO admins (tg_id, granted_by) VALUES (?,?)",
                (tg_id, message.from_user.id))
            await message.answer(
                f"✅ Админ-права выданы по tg_id <code>{tg_id}</code>.\n\n"
                f"⚠️ Пользователь не писал боту — мы не знаем его @username. "
                f"После того как он напишет /start, его данные подтянутся.",
                parse_mode="HTML",
                reply_markup=admin_menu_kb(user.get('language') or 'ru'))
            await state.clear()
            # Попробуем уведомить
            try:
                await message.bot.send_message(
                    tg_id,
                    "🎉 <b>Вам выдали права администратора в боте!</b>\n\n"
                    "Откройте админ-панель командой /admin",
                    parse_mode="HTML")
            except Exception:
                pass
            return

        await message.answer(
            "❌ Пользователь не найден.\n"
            "Убедитесь что он написал боту /start, "
            "либо введите его tg_id числом.")
        return

    # Пользователь найден в users
    if target['tg_id'] in (config.ADMIN_IDS or []):
        await message.answer(
            f"ℹ️ Этот пользователь уже главный админ (из настроек сервера).")
        await state.clear()
        return

    existing = db.fetchone(
        "SELECT id FROM admins WHERE tg_id=?", (target['tg_id'],))
    if existing:
        await message.answer(
            f"ℹ️ Пользователь уже админ.")
        await state.clear()
        return

    db.execute(
        "INSERT INTO admins (tg_id, granted_by) VALUES (?,?)",
        (target['tg_id'], message.from_user.id))

    uname = ("@" + target['username']) if target.get('username') else (
        target.get('first_name') or f"id{target['tg_id']}")
    await message.answer(
        f"✅ <b>{utils.escape_html(uname)}</b> теперь администратор бота.\n"
        f"tg_id: <code>{target['tg_id']}</code>\n\n"
        f"Уведомление отправлено пользователю.",
        parse_mode="HTML",
        reply_markup=admin_menu_kb(user.get('language') or 'ru'))
    await state.clear()

    # Уведомление новому админу
    try:
        await message.bot.send_message(
            target['tg_id'],
            "🎉 <b>Вам выдали права администратора в боте!</b>\n\n"
            "Теперь вы можете:\n"
            "• Создавать тесты\n"
            "• Импортировать вопросы\n"
            "• Выдавать Premium-доступ\n"
            "• Запускать тесты в группах\n"
            "• Смотреть статистику\n"
            "• И многое другое\n\n"
            "Откройте админ-панель командой /admin",
            parse_mode="HTML")
    except Exception:
        pass


@router.callback_query(F.data == "adm:admins:remove", IsAdmin())
async def cb_admins_remove(call: CallbackQuery, state: FSMContext, user: dict):
    """Начало удаления админа."""
    if not _is_super_admin(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return
    await state.set_state(AdminMgmtStates.waiting_remove_user)
    await call.message.answer(
        "🗑 Введите <b>@username</b> или <b>tg_id</b> админа, "
        "которого хотите убрать.\n\n"
        "ℹ️ Главных админов (из настроек сервера) убрать нельзя.",
        parse_mode="HTML")
    await call.answer()


@router.message(AdminMgmtStates.waiting_remove_user, IsAdmin())
async def s_admin_remove(message: Message, state: FSMContext, user: dict):
    if not _is_super_admin(message.from_user.id):
        await state.clear()
        return
    arg = (message.text or "").strip()

    tg_id = None
    if arg.isdigit():
        tg_id = int(arg)
    else:
        target = utils.find_user_by_arg(arg)
        if target:
            tg_id = target['tg_id']

    if tg_id is None:
        await message.answer("❌ Не нашёл такого пользователя.")
        return

    if tg_id in (config.ADMIN_IDS or []):
        await message.answer(
            "⛔ Это главный админ (из настроек сервера) — его нельзя убрать здесь. "
            "Удалите его из переменной ADMIN_IDS на Railway.")
        await state.clear()
        return

    existing = db.fetchone(
        "SELECT id FROM admins WHERE tg_id=?", (tg_id,))
    if not existing:
        await message.answer(
            f"ℹ️ Пользователь <code>{tg_id}</code> и так не админ.",
            parse_mode="HTML")
        await state.clear()
        return

    db.execute("DELETE FROM admins WHERE tg_id=?", (tg_id,))
    await message.answer(
        f"✅ Админ-права у <code>{tg_id}</code> убраны.",
        parse_mode="HTML",
        reply_markup=admin_menu_kb(user.get('language') or 'ru'))
    await state.clear()

    try:
        await message.bot.send_message(
            tg_id, "ℹ️ Ваши права администратора в боте были отозваны.")
    except Exception:
        pass


# ============ Fallback для admin callbacks ============
# Ловит любой adm:* callback который не отловлен выше — снимает «загрузку».
# Это safety-net чтобы пользователь не висел с «Загрузка...» если хендлер забыли.

@router.callback_query(F.data.startswith("adm:"))
async def cb_admin_fallback(call: CallbackQuery, state: FSMContext, user: dict):
    """Если ни один хендлер не отловил adm:* callback."""
    try:
        await state.clear()
    except Exception:
        pass
    log.warning("Unhandled admin callback: %s from %s",
                call.data, call.from_user.id)
    await call.answer(
        f"⚠️ Эта функция временно недоступна.\nДанные: {call.data}",
        show_alert=False)
