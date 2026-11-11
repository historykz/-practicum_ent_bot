"""
Режим «Заучивание» — пользователь пишет ответ текстом.
Проверка с нормализацией, попытки, показ ответа, пропуск, фото, результаты.
Сессии в БД (mode_sessions).
"""
import json
import logging
import time

from aiogram import Router, F, Bot
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                            InlineKeyboardButton)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database as db
import utils
from services import modes_service as ms

router = Router(name="learning")
log = logging.getLogger(__name__)

PROTECT = True

# Анти-дабл: user_tg_id -> timestamp обработки
_processing: dict = {}

# Сообщения для удаления при «Продолжить»: sess_id -> [(user_msg_id, bot_msg_id)]
_cleanup: dict = {}


def _remember_cleanup(sess_id: int, user_msg_id: int, bot_msg_id: int):
    _cleanup.setdefault(sess_id, []).append((user_msg_id, bot_msg_id))


def _get_session(user_tg_id: int):
    try:
        return db.fetchone(
            "SELECT * FROM mode_sessions WHERE user_tg_id=? AND mode='learning' "
            "AND status='active' ORDER BY id DESC LIMIT 1", (user_tg_id,))
    except Exception:
        # Таблица ещё не создана (старая БД) — режим просто не активен
        return None


def _question_by_id(qid: int):
    return db.fetchone("SELECT * FROM questions WHERE id=?", (qid,))


def _kb_question() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💡 Показать ответ", callback_data="ln:show")
    kb.button(text="⏭ Пропустить", callback_data="ln:skip")
    kb.button(text="🚪 Завершить", callback_data="ln:finish")
    kb.adjust(1)
    return kb.as_markup()


async def _delete_photo(bot: Bot, sess):
    pmid = sess.get('photo_message_id') if isinstance(sess, dict) else None
    if pmid:
        try:
            await bot.delete_message(sess['user_tg_id'], pmid)
        except Exception:
            pass


async def start_learning(bot: Bot, chat_id: int, user_tg_id: int,
                          test_id: int, question_ids: list,
                          is_redo: bool = False):
    db.execute(
        "UPDATE mode_sessions SET status='finished' "
        "WHERE user_tg_id=? AND mode='learning' AND status='active'",
        (user_tg_id,))
    cur = db.execute(
        """INSERT INTO mode_sessions
           (user_tg_id, test_id, mode, question_ids, current_index, statuses,
            answers, is_redo, status)
           VALUES (?,?,?,?,0,?,?,?, 'active')""",
        (user_tg_id, test_id, 'learning', json.dumps(question_ids),
         json.dumps({}), json.dumps({}), 1 if is_redo else 0))
    sess = db.fetchone("SELECT * FROM mode_sessions WHERE id=?", (cur.lastrowid,))
    await _render_question(bot, chat_id, sess, new=True)


async def _render_question(bot: Bot, chat_id: int, sess, new: bool = False):
    qids = json.loads(sess['question_ids'])
    idx = sess['current_index']
    if idx >= len(qids):
        await _finish(bot, chat_id, sess)
        return
    qid = qids[idx]
    q = _question_by_id(qid)
    statuses = json.loads(sess['statuses'] or '{}')
    correct_first = sum(1 for v in statuses.values() if v == 'correct_first')
    correct_retry = sum(1 for v in statuses.values() if v == 'correct_retry')
    wrong = sum(1 for v in statuses.values() if v in ('wrong', 'shown', 'skipped'))

    text = (f"<b>Вопрос {idx+1}</b>\n\n"
            f"{utils.escape_html(q.get('text') or '')}\n\n"
            f"{idx+1} / {len(qids)}\n"
            f"✅ Правильно: {correct_first + correct_retry}   ❌ Ошибки: {wrong}\n\n"
            f"✍️ <i>Напиши ответ сообщением:</i>")

    # Фото
    photo = q.get('photo_file_id') or q.get('image_file_id')
    await _delete_photo(bot, sess)
    new_photo_id = None
    if photo:
        try:
            pm = await bot.send_photo(chat_id, photo, caption="Фото к заданию",
                                       protect_content=PROTECT)
            new_photo_id = pm.message_id
        except Exception:
            pass

    main_id = sess.get('main_message_id')
    if new or not main_id:
        m = await bot.send_message(chat_id, text, reply_markup=_kb_question(),
                                    parse_mode="HTML", protect_content=PROTECT)
        main_id = m.message_id
    else:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=main_id,
                                         reply_markup=_kb_question(),
                                         parse_mode="HTML")
        except Exception:
            m = await bot.send_message(chat_id, text, reply_markup=_kb_question(),
                                        parse_mode="HTML", protect_content=PROTECT)
            main_id = m.message_id

    db.execute(
        "UPDATE mode_sessions SET main_message_id=?, photo_message_id=?, "
        "last_action_at=CURRENT_TIMESTAMP WHERE id=?",
        (main_id, new_photo_id, sess['id']))


@router.message(F.text & ~F.text.startswith("/"))
async def on_text_answer(message: Message, bot: Bot, state=None):
    """Приём текстового ответа в режиме заучивания."""
    # КРИТИЧНО: если у юзера активное FSM-состояние (создаёт тест, вводит
    # цену и т.д.) — НЕ перехватываем, пропускаем дальше другим хендлерам.
    try:
        from aiogram.fsm.context import FSMContext
        if state is not None:
            cur_state = await state.get_state()
            if cur_state is not None:
                return  # юзер что-то заполняет — не наш случай
    except Exception:
        pass

    sess = _get_session(message.from_user.id)
    if not sess:
        return  # не наш режим — пропускаем дальше
    if message.chat.type != "private":
        return
    # Анти-дабл
    now = time.time()
    last = _processing.get(message.from_user.id, 0)
    if now - last < 1.5:
        return
    _processing[message.from_user.id] = now

    qids = json.loads(sess['question_ids'])
    idx = sess['current_index']
    if idx >= len(qids):
        return
    qid = qids[idx]
    q = _question_by_id(qid)
    answers = json.loads(sess['answers'] or '{}')
    qkey = str(qid)
    answers.setdefault(qkey, [])
    answers[qkey].append(message.text)

    result = ms.check_answer(message.text, q)
    statuses = json.loads(sess['statuses'] or '{}')
    attempts = len(answers[qkey])

    # Кнопки после ответа: Продолжить / Завершить
    def _after_kb(retry=False):
        rows = [[InlineKeyboardButton(text="➡️ Продолжить", callback_data="ln:cont"),
                 InlineKeyboardButton(text="🚪 Завершить", callback_data="ln:finish")]]
        if retry:
            rows.insert(0, [InlineKeyboardButton(
                text="🔁 Попробовать ещё раз", callback_data="ln:retry")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    if result['correct']:
        status = 'correct_first' if attempts == 1 else 'correct_retry'
        if statuses.get(qkey) == 'shown':
            status = 'shown'
        statuses[qkey] = status
        db.execute(
            "UPDATE mode_sessions SET answers=?, statuses=? WHERE id=?",
            (json.dumps(answers), json.dumps(statuses), sess['id']))
        fb = await message.answer(
            f"✅ <b>Правильно!</b>\nВаш ответ: {utils.escape_html(message.text)}",
            reply_markup=_after_kb(), parse_mode="HTML")
        # Запоминаем id сообщений для удаления при «Продолжить»
        db.execute(
            "UPDATE mode_sessions SET photo_message_id=photo_message_id, "
            "answers=? WHERE id=?", (json.dumps(answers), sess['id']))
        _remember_cleanup(sess['id'], message.message_id, fb.message_id)
    elif result['close']:
        db.execute("UPDATE mode_sessions SET answers=? WHERE id=?",
                   (json.dumps(answers), sess['id']))
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Засчитать", callback_data="ln:accept"),
            InlineKeyboardButton(text="❌ Ошибка", callback_data="ln:reject")]])
        fb = await message.answer(
            f"🤔 Ответ «{utils.escape_html(message.text)}» очень похож на "
            f"правильный. Засчитать?", reply_markup=kb, parse_mode="HTML")
        _remember_cleanup(sess['id'], message.message_id, fb.message_id)
    else:
        # Неправильно
        if statuses.get(qkey) != 'shown':
            statuses[qkey] = 'wrong'
        db.execute(
            "UPDATE mode_sessions SET answers=?, statuses=? WHERE id=?",
            (json.dumps(answers), json.dumps(statuses), sess['id']))
        fb = await message.answer(
            f"❌ <b>Неправильно</b>\nВаш ответ: {utils.escape_html(message.text)}\n\n"
            f"<b>Правильный ответ:</b>\n{utils.escape_html(result['correct_text'])}",
            reply_markup=_after_kb(retry=True), parse_mode="HTML")
        _remember_cleanup(sess['id'], message.message_id, fb.message_id)


@router.callback_query(F.data == "ln:accept")
async def cb_accept(call: CallbackQuery, bot: Bot):
    sess = _get_session(call.from_user.id)
    if not sess:
        await call.answer("Сессия завершена.", show_alert=True)
        return
    qids = json.loads(sess['question_ids'])
    idx = sess['current_index']
    qkey = str(qids[idx])
    statuses = json.loads(sess['statuses'] or '{}')
    answers = json.loads(sess['answers'] or '{}')
    attempts = len(answers.get(qkey, []))
    statuses[qkey] = 'correct_first' if attempts == 1 else 'correct_retry'
    db.execute("UPDATE mode_sessions SET statuses=?, current_index=? WHERE id=?",
               (json.dumps(statuses), idx + 1, sess['id']))
    try:
        await call.message.edit_text("✅ Засчитано!")
    except Exception:
        pass
    await call.answer()
    s2 = db.fetchone("SELECT * FROM mode_sessions WHERE id=?", (sess['id'],))
    await _render_question(bot, call.message.chat.id, s2)


@router.callback_query(F.data == "ln:reject")
async def cb_reject(call: CallbackQuery, bot: Bot):
    sess = _get_session(call.from_user.id)
    if not sess:
        await call.answer()
        return
    qids = json.loads(sess['question_ids'])
    idx = sess['current_index']
    q = _question_by_id(qids[idx])
    qkey = str(qids[idx])
    statuses = json.loads(sess['statuses'] or '{}')
    statuses[qkey] = 'wrong'
    db.execute("UPDATE mode_sessions SET statuses=? WHERE id=?",
               (json.dumps(statuses), sess['id']))
    correct = ms.get_correct_answers(q)
    ct = correct[0] if correct else "—"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➡️ Следующий", callback_data="ln:next"),
        InlineKeyboardButton(text="🔁 Ещё раз", callback_data="ln:retry")]])
    try:
        await call.message.edit_text(
            f"❌ Засчитано как ошибка.\n\n<b>Правильный ответ:</b>\n"
            f"{utils.escape_html(ct)}", reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data == "ln:cont")
async def cb_cont(call: CallbackQuery, bot: Bot):
    """Продолжить: удалить ответ юзера и фидбэк бота, показать следующий вопрос."""
    sess = _get_session(call.from_user.id)
    if not sess:
        await call.answer("Сессия завершена.", show_alert=True)
        return
    await call.answer()
    # Удаляем накопленные сообщения (ответы юзера + фидбэк), чтобы не спамить
    pairs = _cleanup.pop(sess['id'], [])
    for user_mid, bot_mid in pairs:
        for mid in (user_mid, bot_mid):
            try:
                await bot.delete_message(call.message.chat.id, mid)
            except Exception:
                pass
    # Переходим к следующему вопросу
    idx = sess['current_index']
    db.execute("UPDATE mode_sessions SET current_index=? WHERE id=?",
               (idx + 1, sess['id']))
    s2 = db.fetchone("SELECT * FROM mode_sessions WHERE id=?", (sess['id'],))
    await _render_question(bot, call.message.chat.id, s2, new=True)


@router.callback_query(F.data == "ln:next")
async def cb_next(call: CallbackQuery, bot: Bot):
    sess = _get_session(call.from_user.id)
    if not sess:
        await call.answer("Сессия завершена.", show_alert=True)
        return
    idx = sess['current_index']
    db.execute("UPDATE mode_sessions SET current_index=? WHERE id=?",
               (idx + 1, sess['id']))
    await call.answer()
    s2 = db.fetchone("SELECT * FROM mode_sessions WHERE id=?", (sess['id'],))
    await _render_question(bot, call.message.chat.id, s2)


@router.callback_query(F.data == "ln:retry")
async def cb_retry(call: CallbackQuery, bot: Bot):
    sess = _get_session(call.from_user.id)
    if not sess:
        await call.answer("Сессия завершена.", show_alert=True)
        return
    await call.answer("Напиши ответ ещё раз")
    s2 = db.fetchone("SELECT * FROM mode_sessions WHERE id=?", (sess['id'],))
    await _render_question(bot, call.message.chat.id, s2)


@router.callback_query(F.data == "ln:show")
async def cb_show(call: CallbackQuery, bot: Bot):
    sess = _get_session(call.from_user.id)
    if not sess:
        await call.answer("Сессия завершена.", show_alert=True)
        return
    qids = json.loads(sess['question_ids'])
    idx = sess['current_index']
    q = _question_by_id(qids[idx])
    qkey = str(qids[idx])
    statuses = json.loads(sess['statuses'] or '{}')
    statuses[qkey] = 'shown'  # просмотр = ошибка
    db.execute("UPDATE mode_sessions SET statuses=? WHERE id=?",
               (json.dumps(statuses), sess['id']))
    correct = ms.get_correct_answers(q)
    ct = correct[0] if correct else "—"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➡️ Следующий", callback_data="ln:next"),
        InlineKeyboardButton(text="🔁 Попробовать", callback_data="ln:retry")]])
    await call.message.answer(
        f"<b>Правильный ответ:</b>\n{utils.escape_html(ct)}",
        reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "ln:skip")
async def cb_skip(call: CallbackQuery, bot: Bot):
    sess = _get_session(call.from_user.id)
    if not sess:
        await call.answer("Сессия завершена.", show_alert=True)
        return
    qids = json.loads(sess['question_ids'])
    idx = sess['current_index']
    q = _question_by_id(qids[idx])
    qkey = str(qids[idx])
    statuses = json.loads(sess['statuses'] or '{}')
    statuses[qkey] = 'skipped'
    db.execute("UPDATE mode_sessions SET statuses=?, current_index=? WHERE id=?",
               (json.dumps(statuses), idx + 1, sess['id']))
    correct = ms.get_correct_answers(q)
    ct = correct[0] if correct else "—"
    await call.answer("⏭ Пропущено")
    await call.message.answer(
        f"⏭ Пропущено.\n<b>Правильный ответ:</b>\n{utils.escape_html(ct)}",
        parse_mode="HTML")
    import asyncio
    await asyncio.sleep(0.5)
    s2 = db.fetchone("SELECT * FROM mode_sessions WHERE id=?", (sess['id'],))
    await _render_question(bot, call.message.chat.id, s2)


@router.callback_query(F.data == "ln:finish")
async def cb_finish(call: CallbackQuery, bot: Bot):
    sess = _get_session(call.from_user.id)
    if not sess:
        await call.answer()
        return
    await call.answer()
    await _finish(bot, call.message.chat.id, sess)


async def _finish(bot: Bot, chat_id: int, sess):
    await _delete_photo(bot, sess)
    # Удаляем главное сообщение вопроса + накопленные ответы (чат чистый)
    main_id = sess.get('main_message_id')
    if main_id:
        try:
            await bot.delete_message(chat_id, main_id)
        except Exception:
            pass
    # Удаляем оставшиеся сообщения-ответы из cleanup
    pairs = _cleanup.pop(sess['id'], [])
    for user_mid, bot_mid in pairs:
        for mid in (user_mid, bot_mid):
            try:
                await bot.delete_message(chat_id, mid)
            except Exception:
                pass
    qids = json.loads(sess['question_ids'])
    statuses = json.loads(sess['statuses'] or '{}')
    answers = json.loads(sess['answers'] or '{}')
    total = len(qids)
    cf = sum(1 for v in statuses.values() if v == 'correct_first')
    cr = sum(1 for v in statuses.values() if v == 'correct_retry')
    wrong = sum(1 for v in statuses.values() if v == 'wrong')
    shown = sum(1 for v in statuses.values() if v == 'shown')
    skipped = sum(1 for v in statuses.values() if v == 'skipped')
    wrong_total = wrong + shown + skipped
    pct = round(cf / total * 100) if total else 0

    db.execute("UPDATE mode_sessions SET status='finished' WHERE id=?",
               (sess['id'],))

    # Ошибочные id для повтора
    err_ids = [int(k) for k, v in statuses.items()
               if v in ('wrong', 'shown', 'skipped', 'correct_retry')]
    db.execute(
        """INSERT INTO mode_results
           (user_tg_id, test_id, mode, total, correct_first, correct_retry,
            wrong_count, skipped_count, details, is_redo)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (sess['user_tg_id'], sess['test_id'], 'learning', total, cf, cr,
         wrong, skipped, json.dumps({'err_ids': err_ids, 'answers': answers,
                                      'statuses': statuses}),
         sess.get('is_redo') or 0))

    remaining = ms.remaining_passes(sess['user_tg_id'], sess['test_id'], 'learning')
    redo_price = ms.price_for(sess['test_id'], 'learning', 'redo')

    text = (f"🧠 <b>Заучивание завершено!</b>\n\n"
            f"Всего вопросов: {total}\n"
            f"✅ С первой попытки: {cf}\n"
            f"🔁 После повтора: {cr}\n"
            f"❌ Ошибки: {wrong}\n"
            f"💡 Показан ответ: {shown}\n"
            f"⏭ Пропущено: {skipped}\n\n"
            f"Результат: {pct}% с первой попытки\n"
            f"Осталось прохождений: {remaining}")
    kb = InlineKeyboardBuilder()
    if wrong_total > 0 or cr > 0:
        kb.button(text=f"🔁 Повторить ошибки — {redo_price} ⭐️",
                  callback_data=f"lnredo:{sess['test_id']}")
    else:
        text += "\n\n🎉 Отлично! Все ответы верны с первой попытки."
    kb.button(text="🧠 Пройти заново", callback_data=f"mode:ln:{sess['test_id']}")
    kb.button(text="📋 К тесту", callback_data=f"opentest:{sess['test_id']}")
    kb.button(text="🏠 Меню", callback_data="m:menu")
    kb.adjust(1)
    await bot.send_message(chat_id, text, reply_markup=kb.as_markup(),
                            parse_mode="HTML")


def close_user_sessions(user_tg_id: int):
    db.execute(
        "UPDATE mode_sessions SET status='finished' "
        "WHERE user_tg_id=? AND mode='learning' AND status='active'",
        (user_tg_id,))
