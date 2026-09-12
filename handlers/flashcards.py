"""
Режим «Карточки» (Quizlet-style).
Одно редактируемое сообщение, переворот, навигация, фото, защита от слива.
Сессии хранятся в БД (mode_sessions) — переживают рестарт.
"""
import json
import logging

from aiogram import Router, F, Bot
from aiogram.types import (CallbackQuery, InlineKeyboardMarkup,
                            InlineKeyboardButton)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database as db
import utils
from services import modes_service as ms

router = Router(name="flashcards")
log = logging.getLogger(__name__)

PROTECT = True  # запрет пересылки/скринов


def _kb_front(has_back: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Не знаю", callback_data="fc:dont")
    if has_back:
        kb.button(text="⬅️ Назад", callback_data="fc:back")
    kb.button(text="✅ Знаю", callback_data="fc:know")
    kb.button(text="🔄 Повернуть", callback_data="fc:flip")
    kb.button(text="🚪 Завершить", callback_data="fc:finish")
    if has_back:
        kb.adjust(3, 1, 1)
    else:
        kb.adjust(2, 1, 1)
    return kb.as_markup()


def _kb_back_side() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Показать вопрос", callback_data="fc:flip")
    kb.button(text="⬅️ Назад", callback_data="fc:back")
    kb.button(text="❌ Не знаю", callback_data="fc:dont")
    kb.button(text="✅ Знаю", callback_data="fc:know")
    kb.button(text="🚪 Завершить", callback_data="fc:finish")
    kb.adjust(1, 3, 1)
    return kb.as_markup()


def _get_session(user_tg_id: int):
    try:
        return db.fetchone(
            "SELECT * FROM mode_sessions WHERE user_tg_id=? AND mode='flashcards' "
            "AND status='active' ORDER BY id DESC LIMIT 1", (user_tg_id,))
    except Exception:
        return None


def _question_by_id(qid: int):
    return db.fetchone("SELECT * FROM questions WHERE id=?", (qid,))


def _correct_text(q: dict) -> str:
    """Текст правильного ответа с буквой варианта."""
    opts = db.fetchall(
        "SELECT text, is_correct FROM question_options WHERE question_id=? "
        "ORDER BY order_num, id", (q['id'],))
    letters = "ABCDE"
    for i, o in enumerate(opts):
        if o.get('is_correct'):
            letter = letters[i] if i < len(letters) else ''
            return f"{letter}) {o['text']}" if letter else o['text']
    if q.get('correct_answer'):
        return q['correct_answer']
    return "—"


async def _delete_photo(bot: Bot, sess):
    """Удалить сообщение с фото если было."""
    pmid = sess.get('photo_message_id') if isinstance(sess, dict) else None
    if pmid:
        try:
            await bot.delete_message(sess['user_tg_id'], pmid)
        except Exception:
            pass


async def start_flashcards(bot: Bot, chat_id: int, user_tg_id: int,
                            test_id: int, question_ids: list,
                            is_redo: bool = False):
    """Запустить новую сессию карточек."""
    # Закрыть старые активные
    db.execute(
        "UPDATE mode_sessions SET status='finished' "
        "WHERE user_tg_id=? AND mode='flashcards' AND status='active'",
        (user_tg_id,))
    cur = db.execute(
        """INSERT INTO mode_sessions
           (user_tg_id, test_id, mode, question_ids, current_index, statuses,
            side, is_redo, status)
           VALUES (?,?,?,?,0,?,?,?, 'active')""",
        (user_tg_id, test_id, 'flashcards', json.dumps(question_ids),
         json.dumps({}), 'question', 1 if is_redo else 0))
    sess_id = cur.lastrowid
    sess = db.fetchone("SELECT * FROM mode_sessions WHERE id=?", (sess_id,))
    await _render(bot, chat_id, sess, new=True)


async def _render(bot: Bot, chat_id: int, sess, new: bool = False):
    """Отрисовать текущую карточку (front)."""
    qids = json.loads(sess['question_ids'])
    idx = sess['current_index']
    if idx >= len(qids):
        await _finish(bot, chat_id, sess)
        return
    qid = qids[idx]
    q = _question_by_id(qid)
    statuses = json.loads(sess['statuses'] or '{}')
    know = sum(1 for v in statuses.values() if v == 'know')
    dont = sum(1 for v in statuses.values() if v == 'dont_know')

    text = (f"<b>Вопрос {idx+1}</b>\n\n"
            f"{utils.escape_html(q.get('text') or '')}\n\n"
            f"{idx+1} / {len(qids)}\n"
            f"❌ Не знаю: {dont}   ✅ Знаю: {know}")
    has_back = idx > 0

    # Фото
    photo = q.get('photo_file_id') or q.get('image_file_id')
    await _delete_photo(bot, sess)
    new_photo_id = None
    if photo:
        try:
            pm = await bot.send_photo(chat_id, photo, caption="Фото к заданию",
                                       protect_content=PROTECT)
            new_photo_id = pm.message_id
        except Exception as e:
            log.warning("fc photo: %s", e)

    # Основное сообщение
    main_id = sess.get('main_message_id')
    if new or not main_id:
        m = await bot.send_message(chat_id, text, reply_markup=_kb_front(has_back),
                                    parse_mode="HTML", protect_content=PROTECT)
        main_id = m.message_id
    else:
        try:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=main_id,
                reply_markup=_kb_front(has_back), parse_mode="HTML")
        except Exception:
            m = await bot.send_message(chat_id, text,
                                        reply_markup=_kb_front(has_back),
                                        parse_mode="HTML", protect_content=PROTECT)
            main_id = m.message_id

    db.execute(
        "UPDATE mode_sessions SET main_message_id=?, photo_message_id=?, "
        "side='question', last_action_at=CURRENT_TIMESTAMP WHERE id=?",
        (main_id, new_photo_id, sess['id']))


@router.callback_query(F.data == "fc:flip")
async def cb_flip(call: CallbackQuery, bot: Bot):
    sess = _get_session(call.from_user.id)
    if not sess:
        await call.answer("Эта сессия карточек уже завершена. Откройте тест заново.",
                          show_alert=True)
        return
    qids = json.loads(sess['question_ids'])
    idx = sess['current_index']
    qid = qids[idx]
    q = _question_by_id(qid)
    statuses = json.loads(sess['statuses'] or '{}')
    know = sum(1 for v in statuses.values() if v == 'know')
    dont = sum(1 for v in statuses.values() if v == 'dont_know')
    new_side = 'answer' if sess['side'] == 'question' else 'question'

    if new_side == 'answer':
        expl = ""
        if q.get('explanation'):
            expl = f"\n\n💡 {utils.escape_html(q['explanation'])}"
        text = (f"<b>Вопрос {idx+1}</b>\n\n"
                f"{utils.escape_html(q.get('text') or '')}\n\n"
                f"<b>Правильный ответ:</b>\n{utils.escape_html(_correct_text(q))}"
                f"{expl}\n\n"
                f"{idx+1} / {len(qids)}\n"
                f"❌ Не знаю: {dont}   ✅ Знаю: {know}")
        kb = _kb_back_side()
    else:
        text = (f"<b>Вопрос {idx+1}</b>\n\n"
                f"{utils.escape_html(q.get('text') or '')}\n\n"
                f"{idx+1} / {len(qids)}\n"
                f"❌ Не знаю: {dont}   ✅ Знаю: {know}")
        kb = _kb_front(idx > 0)

    try:
        await bot.edit_message_text(
            text, chat_id=call.message.chat.id,
            message_id=sess['main_message_id'], reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass
    db.execute("UPDATE mode_sessions SET side=? WHERE id=?", (new_side, sess['id']))
    await call.answer()


async def _set_status_and_advance(call: CallbackQuery, bot: Bot, status: str):
    sess = _get_session(call.from_user.id)
    if not sess:
        await call.answer("Эта сессия карточек уже завершена. Откройте тест заново.",
                          show_alert=True)
        return
    qids = json.loads(sess['question_ids'])
    idx = sess['current_index']
    qid = str(qids[idx])
    statuses = json.loads(sess['statuses'] or '{}')
    statuses[qid] = status  # перезапись если уже было
    new_idx = idx + 1
    db.execute(
        "UPDATE mode_sessions SET statuses=?, current_index=?, side='question' "
        "WHERE id=?", (json.dumps(statuses), new_idx, sess['id']))
    await call.answer("✅ Знаю" if status == 'know' else "❌ Не знаю")
    sess = db.fetchone("SELECT * FROM mode_sessions WHERE id=?", (sess['id'],))
    await _render(bot, call.message.chat.id, sess)


@router.callback_query(F.data == "fc:know")
async def cb_know(call: CallbackQuery, bot: Bot):
    await _set_status_and_advance(call, bot, 'know')


@router.callback_query(F.data == "fc:dont")
async def cb_dont(call: CallbackQuery, bot: Bot):
    await _set_status_and_advance(call, bot, 'dont_know')


@router.callback_query(F.data == "fc:back")
async def cb_back(call: CallbackQuery, bot: Bot):
    sess = _get_session(call.from_user.id)
    if not sess:
        await call.answer("Сессия завершена.", show_alert=True)
        return
    idx = sess['current_index']
    if idx <= 0:
        await call.answer("Это первая карточка.")
        return
    db.execute("UPDATE mode_sessions SET current_index=?, side='question' "
               "WHERE id=?", (idx - 1, sess['id']))
    await call.answer()
    sess = db.fetchone("SELECT * FROM mode_sessions WHERE id=?", (sess['id'],))
    await _render(bot, call.message.chat.id, sess)


@router.callback_query(F.data == "fc:finish")
async def cb_finish(call: CallbackQuery, bot: Bot):
    sess = _get_session(call.from_user.id)
    if not sess:
        await call.answer()
        return
    await call.answer()
    await _finish(bot, call.message.chat.id, sess)


async def _finish(bot: Bot, chat_id: int, sess):
    """Завершить карточки, показать результат."""
    await _delete_photo(bot, sess)
    # Удаляем главное сообщение карточки (чат не засоряется)
    main_id = sess.get('main_message_id')
    if main_id:
        try:
            await bot.delete_message(chat_id, main_id)
        except Exception:
            pass
    qids = json.loads(sess['question_ids'])
    statuses = json.loads(sess['statuses'] or '{}')
    know = sum(1 for v in statuses.values() if v == 'know')
    dont = sum(1 for v in statuses.values() if v == 'dont_know')
    total = len(qids)
    db.execute("UPDATE mode_sessions SET status='finished' WHERE id=?",
               (sess['id'],))

    # Сохранить результат в историю
    dont_ids = [int(k) for k, v in statuses.items() if v == 'dont_know']
    db.execute(
        """INSERT INTO mode_results
           (user_tg_id, test_id, mode, total, know_count, dontknow_count,
            details, is_redo)
           VALUES (?,?,?,?,?,?,?,?)""",
        (sess['user_tg_id'], sess['test_id'], 'flashcards', total, know, dont,
         json.dumps({'dont_ids': dont_ids}), sess.get('is_redo') or 0))

    redo_price = ms.price_for(sess['test_id'], 'flashcards', 'redo')
    remaining = ms.remaining_passes(sess['user_tg_id'], sess['test_id'], 'flashcards')

    text = (f"🃏 <b>Карточки завершены!</b>\n\n"
            f"Всего: {total}\n"
            f"✅ Знаю: {know}\n"
            f"❌ Не знаю: {dont}\n\n"
            f"Осталось прохождений: {remaining}")
    kb = InlineKeyboardBuilder()
    if dont > 0:
        kb.button(text=f"🔁 Повторить «Не знаю» — {redo_price} ⭐️",
                  callback_data=f"fcredo:{sess['test_id']}")
    else:
        text += "\n\n🎉 Отлично! Вы отметили все карточки как знакомые."
    kb.button(text="🃏 Пройти заново", callback_data=f"mode:fc:{sess['test_id']}")
    kb.button(text="📋 К тесту", callback_data=f"opentest:{sess['test_id']}")
    kb.button(text="🏠 Меню", callback_data="m:menu")
    kb.adjust(1)
    await bot.send_message(chat_id, text, reply_markup=kb.as_markup(),
                            parse_mode="HTML")


def close_user_sessions(user_tg_id: int):
    """Закрыть все активные сессии карточек (вызывать при /start, меню)."""
    db.execute(
        "UPDATE mode_sessions SET status='finished' "
        "WHERE user_tg_id=? AND mode='flashcards' AND status='active'",
        (user_tg_id,))
