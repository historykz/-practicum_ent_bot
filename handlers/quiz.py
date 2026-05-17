"""Хендлеры тестирования: ответы, пауза, возобновление, прерывание, группы."""
import asyncio
import json
import logging
import random

from aiogram import Router, F, Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message, CallbackQuery, PollAnswer
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext

import config
import database as db
import utils
from locales import t
from keyboards import (group_options_kb, group_join_kb, pause_group_kb,
                       main_menu_kb)
from services import test_runner, subscription_service

router = Router(name="quiz")
log = logging.getLogger(__name__)


# ============================
# Личные ответы
# ============================

@router.callback_query(F.data.startswith("ans:"))
async def cb_answer(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    parts = call.data.split(":")
    if len(parts) != 4:
        await call.answer()
        return
    try:
        attempt_id = int(parts[1])
        question_id = int(parts[2])
        option_id = int(parts[3])
    except ValueError:
        await call.answer()
        return

    # Принадлежит ли попытка этому пользователю?
    a = test_runner.get_attempt(attempt_id)
    if not a or a['user_id'] != user['id']:
        await call.answer(t("old_button", lang), show_alert=True)
        return

    result = await test_runner.process_answer(
        call.bot, attempt_id, question_id, option_id, call.message.chat.id)

    if result == 'ok':
        # Удалим клавиатуру предыдущего вопроса
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


@router.callback_query(F.data.startswith("resume:"))
async def cb_resume(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    try:
        attempt_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    a = test_runner.get_attempt(attempt_id)
    if not a or a['user_id'] != user['id']:
        await call.answer(t("old_button", lang), show_alert=True)
        return
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer(t("test_resumed", lang))
    await test_runner.resume_attempt(call.bot, attempt_id, call.message.chat.id)
    await call.answer()


@router.callback_query(F.data.startswith("abort:"))
async def cb_abort(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    try:
        attempt_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    a = test_runner.get_attempt(attempt_id)
    if not a or a['user_id'] != user['id']:
        await call.answer(t("old_button", lang), show_alert=True)
        return
    try:
        await call.message.delete()
    except Exception:
        pass
    await test_runner.abort_attempt(call.bot, attempt_id, call.message.chat.id)
    await call.message.answer(t("test_aborted", lang),
                              reply_markup=main_menu_kb(lang, utils.is_admin(call.from_user.id)))
    await call.answer()


@router.callback_query(F.data.startswith("retake:"))
async def cb_retake(call: CallbackQuery, user: dict):
    """Повторное прохождение того же теста."""
    lang = user.get('language') or 'ru'
    try:
        test_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    test = test_runner.get_test(test_id)
    if not test:
        await call.answer(t("test_not_found", lang), show_alert=True)
        return
    # Те же проверки, что и run:
    if test['is_paid'] and not utils.has_paid_access(user['id'], test_id=test['id']):
        await call.answer(t("noaccess_test", lang, manager=config.MANAGER_USERNAME),
                          show_alert=True)
        return
    if test['attempts_limit']:
        used = test_runner.count_user_attempts(user['id'], test_id)
        if used >= test['attempts_limit']:
            await call.answer(t("attempts_exhausted", lang), show_alert=True)
            return
    attempt_id = test_runner.create_attempt(
        user['id'], test_id, lang, group_id=None, started_by_user_id=user['id'])
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer(t("test_start_warning", lang))
    await asyncio.sleep(2)
    await test_runner.send_current_question(call.bot, attempt_id, call.message.chat.id)
    await call.answer()


@router.callback_query(F.data.startswith("review:"))
async def cb_review(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    try:
        attempt_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    a = test_runner.get_attempt(attempt_id)
    if not a or a['user_id'] != user['id']:
        await call.answer(t("old_button", lang), show_alert=True)
        return
    await call.answer()
    await test_runner._send_answer_review(call.bot, call.message.chat.id, attempt_id, lang)


# ============================
# Групповая викторина
# ============================

@router.message(Command("quiz"))
async def cmd_group_quiz(message: Message, command: CommandObject, user: dict):
    """В группе: /quiz <test_id> — начать набор группы для теста."""
    lang = user.get('language') or 'ru'
    if message.chat.type == "private":
        await message.answer(t("group_chat_only", lang))
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Использование: /quiz <ID_теста>")
        return
    test_id = int(arg)
    test = test_runner.get_test(test_id)
    if not test or test['status'] != 'active' or not test['allow_in_group']:
        await message.answer(t("test_not_found", lang))
        return

    # Проверим, что бот — админ группы (или хотя бы участник)
    try:
        me = await message.bot.get_me()
        my_member = await message.bot.get_chat_member(message.chat.id, me.id)
        if my_member.status not in ("administrator", "creator", "member"):
            await message.answer(t("bot_not_in_group", lang))
            return
    except Exception:
        await message.answer(t("bot_not_in_group", lang))
        return

    # Создать group_quiz
    qs = test_runner.get_test_questions(test_id)
    if not qs:
        await message.answer(t("no_questions", lang))
        return
    qids = [q['id'] for q in qs]
    if test['shuffle_questions']:
        random.shuffle(qids)
    db.execute(
        """INSERT INTO group_quizzes (chat_id, test_id, question_order, current_question_index,
                                       status, started_by_user_id, language, created_at)
           VALUES (?, ?, ?, 0, 'collecting', ?, ?, ?)""",
        (message.chat.id, test_id, json.dumps(qids),
         message.from_user.id, lang, utils.now_iso()))
    gqid = db.fetchone("SELECT last_insert_rowid() AS id")['id']

    text = t("group_card", lang,
             title=utils.escape_html(test['title']),
             qcount=len(qids),
             time_per_q=test['time_per_question'])
    sent = await message.answer(text, reply_markup=group_join_kb(gqid, lang))

    # Автостарт через таймаут
    asyncio.create_task(_group_autostart(message.bot, gqid, sent.chat.id, sent.message_id))


async def _group_autostart(bot: Bot, gqid: int, chat_id: int, msg_id: int):
    await asyncio.sleep(config.GROUP_JOIN_TIMEOUT)
    gq = db.fetchone("SELECT * FROM group_quizzes WHERE id=?", (gqid,))
    if not gq or gq['status'] != 'collecting':
        return
    # Проверим количество участников
    parts = db.fetchall(
        "SELECT * FROM group_quiz_participants WHERE group_quiz_id=?", (gqid,))
    if len(parts) < config.GROUP_MIN_PLAYERS:
        db.execute("UPDATE group_quizzes SET status='aborted' WHERE id=?", (gqid,))
        try:
            await bot.send_message(chat_id, t("group_too_few_players", gq['language']))
        except Exception:
            pass
        return
    await _start_group_quiz(bot, gqid, chat_id)


@router.callback_query(F.data.startswith("gjoin:"))
async def cb_gjoin(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    try:
        gqid = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    gq = db.fetchone("SELECT * FROM group_quizzes WHERE id=?", (gqid,))
    if not gq or gq['status'] != 'collecting':
        await call.answer(t("old_button", lang), show_alert=True)
        return
    existing = db.fetchone(
        "SELECT id FROM group_quiz_participants WHERE group_quiz_id=? AND user_id=?",
        (gqid, user['id']))
    if existing:
        await call.answer(t("group_already_joined", lang), show_alert=True)
        return
    db.execute(
        """INSERT INTO group_quiz_participants (group_quiz_id, user_id, joined_at)
           VALUES (?, ?, ?)""", (gqid, user['id'], utils.now_iso()))
    await call.answer(t("group_joined", lang))


@router.callback_query(F.data.startswith("gstart:"))
async def cb_gstart(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    try:
        gqid = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    gq = db.fetchone("SELECT * FROM group_quizzes WHERE id=?", (gqid,))
    if not gq or gq['status'] != 'collecting':
        await call.answer(t("old_button", lang), show_alert=True)
        return
    parts = db.fetchall(
        "SELECT * FROM group_quiz_participants WHERE group_quiz_id=?", (gqid,))
    if len(parts) < config.GROUP_MIN_PLAYERS:
        await call.answer(t("group_too_few_players", lang), show_alert=True)
        return
    await call.answer(t("group_starting", lang))
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _start_group_quiz(call.bot, gqid, call.message.chat.id)


async def _start_group_quiz(bot: Bot, gqid: int, chat_id: int):
    db.execute("UPDATE group_quizzes SET status='running' WHERE id=?", (gqid,))
    await _send_group_question(bot, gqid, chat_id)


async def _send_group_question(bot: Bot, gqid: int, chat_id: int):
    gq = db.fetchone("SELECT * FROM group_quizzes WHERE id=?", (gqid,))
    if not gq or gq['status'] != 'running':
        return
    qids = json.loads(gq['question_order'])
    idx = gq['current_question_index']
    if idx >= len(qids):
        await _finalize_group(bot, gqid, chat_id)
        return

    qid = qids[idx]
    q = db.fetchone("SELECT * FROM questions WHERE id=?", (qid,))
    if not q:
        db.execute("UPDATE group_quizzes SET current_question_index=? WHERE id=?",
                   (idx + 1, gqid))
        await _send_group_question(bot, gqid, chat_id)
        return

    test = test_runner.get_test(gq['test_id'])
    opts_rows = db.fetchall(
        "SELECT * FROM question_options WHERE question_id=? ORDER BY order_num", (qid,))
    options = [{'id': o['id'], 'text': o['text']} for o in opts_rows]
    if test and test['shuffle_options']:
        random.shuffle(options)

    text = utils.build_question_text(idx + 1, len(qids), q['text'],
                                      test['time_per_question'] if test else 30,
                                      gq['language'])
    try:
        if q['image_file_id']:
            await bot.send_photo(chat_id, q['image_file_id'], caption=text,
                                 reply_markup=group_options_kb(gqid, qid, options),
                                 protect_content=config.PROTECT_CONTENT)
        else:
            await bot.send_message(chat_id, text,
                                   reply_markup=group_options_kb(gqid, qid, options),
                                   protect_content=config.PROTECT_CONTENT)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        log.warning("Group quiz send failed: %s", e)
        return

    # Таймер
    asyncio.create_task(_group_question_timeout(bot, gqid, chat_id, idx, qid,
                                                  test['time_per_question'] if test else 30))


async def _group_question_timeout(bot: Bot, gqid: int, chat_id: int, idx: int,
                                    qid: int, seconds: int):
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:
        return
    gq = db.fetchone("SELECT * FROM group_quizzes WHERE id=?", (gqid,))
    if not gq or gq['status'] != 'running' or gq['current_question_index'] != idx:
        return
    # Перейти к следующему вопросу
    db.execute("UPDATE group_quizzes SET current_question_index=? WHERE id=?",
               (idx + 1, gqid))
    try:
        await bot.send_message(chat_id, t("question_skipped", gq['language']))
    except Exception:
        pass
    await asyncio.sleep(0.5)
    await _send_group_question(bot, gqid, chat_id)


@router.callback_query(F.data.startswith("gans:"))
async def cb_gans(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    parts = call.data.split(":")
    if len(parts) != 4:
        await call.answer()
        return
    try:
        gqid = int(parts[1])
        question_id = int(parts[2])
        option_id = int(parts[3])
    except ValueError:
        await call.answer()
        return

    gq = db.fetchone("SELECT * FROM group_quizzes WHERE id=?", (gqid,))
    if not gq or gq['status'] != 'running':
        await call.answer(t("old_button", lang), show_alert=True)
        return

    # Текущий ли это вопрос?
    qids = json.loads(gq['question_order'])
    idx = gq['current_question_index']
    if idx >= len(qids) or qids[idx] != question_id:
        await call.answer(t("old_button", lang), show_alert=True)
        return

    # Участник зарегистрирован?
    part = db.fetchone(
        "SELECT * FROM group_quiz_participants WHERE group_quiz_id=? AND user_id=?",
        (gqid, user['id']))
    if not part:
        # Авто-добавим
        db.execute(
            """INSERT INTO group_quiz_participants (group_quiz_id, user_id, joined_at)
               VALUES (?, ?, ?)""", (gqid, user['id'], utils.now_iso()))

    # Уникальность ответа
    existing = db.fetchone(
        """SELECT id FROM group_quiz_answers
           WHERE group_quiz_id=? AND user_id=? AND question_id=?""",
        (gqid, user['id'], question_id))
    if existing:
        await call.answer(t("already_answered", lang), show_alert=True)
        return

    opt = db.fetchone(
        "SELECT is_correct FROM question_options WHERE id=? AND question_id=?",
        (option_id, question_id))
    if not opt:
        await call.answer(t("error_generic", lang), show_alert=True)
        return

    is_correct = bool(opt['is_correct'])
    db.execute(
        """INSERT INTO group_quiz_answers (group_quiz_id, user_id, question_id,
                                            option_id, is_correct, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (gqid, user['id'], question_id, option_id, is_correct, utils.now_iso()))
    # Обновим счётчик
    if is_correct:
        db.execute(
            """UPDATE group_quiz_participants SET correct_count = correct_count + 1
                WHERE group_quiz_id=? AND user_id=?""", (gqid, user['id']))
    else:
        db.execute(
            """UPDATE group_quiz_participants SET wrong_count = wrong_count + 1
                WHERE group_quiz_id=? AND user_id=?""", (gqid, user['id']))

    await call.answer(t("answer_recorded", lang))

    # Все ли ответили?
    total_parts = db.fetchone(
        "SELECT COUNT(*) AS c FROM group_quiz_participants WHERE group_quiz_id=?",
        (gqid,))['c']
    answers_now = db.fetchone(
        """SELECT COUNT(*) AS c FROM group_quiz_answers
           WHERE group_quiz_id=? AND question_id=?""",
        (gqid, question_id))['c']
    if answers_now >= total_parts and total_parts > 0:
        # Переходим к следующему
        db.execute("UPDATE group_quizzes SET current_question_index=? WHERE id=?",
                   (idx + 1, gqid))
        await asyncio.sleep(0.5)
        await _send_group_question(call.bot, gqid, call.message.chat.id)


async def _finalize_group(bot: Bot, gqid: int, chat_id: int):
    gq = db.fetchone("SELECT * FROM group_quizzes WHERE id=?", (gqid,))
    if not gq:
        return
    db.execute(
        "UPDATE group_quizzes SET status='finished', finished_at=? WHERE id=?",
        (utils.now_iso(), gqid))

    parts = db.fetchall(
        """SELECT p.*, u.username, u.first_name, u.tg_id
           FROM group_quiz_participants p
           JOIN users u ON u.id = p.user_id
           WHERE p.group_quiz_id=?
           ORDER BY p.correct_count DESC, p.wrong_count ASC""", (gqid,))
    lang = gq['language']
    if not parts:
        try:
            await bot.send_message(chat_id, t("group_results", lang, results="—"))
        except Exception:
            pass
        return

    medals = ['🥇', '🥈', '🥉']
    lines = []
    for i, p in enumerate(parts):
        name = p['first_name'] or p['username'] or str(p['tg_id'])
        prefix = medals[i] if i < 3 else f"{i+1}."
        lines.append(
            f"{prefix} {utils.escape_html(name)} — "
            f"✅ {p['correct_count']} / ❌ {p['wrong_count']}")
    results_text = "\n".join(lines)
    try:
        await bot.send_message(chat_id, t("group_results", lang, results=results_text))
    except Exception:
        pass


@router.poll_answer()
async def on_poll_answer(poll_answer: PollAnswer):
    """Ответ из Quiz Poll — личный тест."""
    try:
        # У PollAnswer есть bot из контекста, но безопаснее взять из Router
        from aiogram import Bot
        bot = poll_answer.bot
        if bot is None:
            return
        await test_runner.process_poll_answer(
            bot,
            poll_answer.poll_id,
            poll_answer.option_ids,
            poll_answer.user.id,
        )
    except Exception as e:
        log.warning("poll_answer handler error: %s", e)
