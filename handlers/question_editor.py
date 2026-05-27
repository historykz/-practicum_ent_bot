"""
Редактор вопросов — Quiz Bot-стиль.
Админ открывает тест → список вопросов → тапает вопрос →
видит карточку с кнопками: заменить, удалить, объяснение, двиг. влево/вправо.
"""
import logging
from typing import Optional

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (CallbackQuery, Message, InlineKeyboardMarkup,
                            InlineKeyboardButton, PollAnswer)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database as db
import utils
from filters import IsAdmin

router = Router(name="question_editor")
log = logging.getLogger(__name__)


class QEditStates(StatesGroup):
    waiting_new_text = State()       # ввод нового текста вопроса
    waiting_explanation = State()    # ввод объяснения
    waiting_pretext = State()        # ввод текста перед вопросом
    waiting_correct_answer = State() # ожидание ответа через Poll


# ===================== ХЕЛПЕРЫ =====================

def _get_test(test_id: int) -> Optional[dict]:
    return db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))


def _get_question(qid: int) -> Optional[dict]:
    return db.fetchone("SELECT * FROM questions WHERE id=?", (qid,))


def _get_options(qid: int) -> list:
    return db.fetchall(
        "SELECT * FROM question_options WHERE question_id=? ORDER BY order_num, id",
        (qid,))


def _get_test_questions(test_id: int) -> list:
    return db.fetchall(
        "SELECT * FROM questions WHERE test_id=? ORDER BY order_num, id",
        (test_id,))


def _question_position(test_id: int, qid: int) -> tuple[int, int]:
    """Вернёт (1-based позиция, всего)."""
    qs = _get_test_questions(test_id)
    total = len(qs)
    for i, q in enumerate(qs, start=1):
        if q['id'] == qid:
            return i, total
    return 0, total


def _get_question_by_position(test_id: int, position: int) -> Optional[dict]:
    """1-based позиция."""
    qs = _get_test_questions(test_id)
    if 1 <= position <= len(qs):
        return qs[position - 1]
    return None


# ===================== СПИСОК ВОПРОСОВ =====================

def _list_questions_text(test: dict, qs: list) -> str:
    lines = [f"📝 <b>Редактирование теста</b>",
              f"<b>{utils.escape_html(test['title'])}</b>",
              f"Вопросов: {len(qs)}",
              "",
              "👇 Тапни на вопрос чтобы изменить:"]
    return "\n".join(lines)


def _list_questions_kb(test_id: int, qs: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for i, q in enumerate(qs[:50], start=1):
        # Превью текста — первые 40 символов
        preview = q['text'][:40].replace('\n', ' ')
        if len(q['text']) > 40:
            preview += '…'
        kb.button(text=f"{i}. {preview}",
                  callback_data=f"qe:view:{q['id']}")
    kb.button(text="↩️ К тесту", callback_data=f"qe:back_test:{test_id}")
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(F.data.startswith("qe:list:"), IsAdmin())
async def cb_list_questions(call: CallbackQuery):
    """Открыть список вопросов теста."""
    try:
        test_id = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    test = _get_test(test_id)
    if not test:
        await call.answer("Тест не найден.", show_alert=True)
        return
    qs = _get_test_questions(test_id)
    if not qs:
        await call.message.edit_text(
            f"📝 <b>{utils.escape_html(test['title'])}</b>\n\n"
            f"<i>В тесте пока нет вопросов.</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="↩️ К тесту",
                                      callback_data=f"qe:back_test:{test_id}")
            ]]))
        await call.answer()
        return
    try:
        await call.message.edit_text(
            _list_questions_text(test, qs),
            reply_markup=_list_questions_kb(test_id, qs))
    except Exception:
        await call.message.answer(
            _list_questions_text(test, qs),
            reply_markup=_list_questions_kb(test_id, qs))
    await call.answer()


@router.callback_query(F.data.startswith("qe:back_test:"), IsAdmin())
async def cb_back_to_test(call: CallbackQuery):
    """Назад к карточке теста в админке."""
    try:
        test_id = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    from handlers.admin import _show_test_admin_card
    try:
        await call.message.delete()
    except Exception:
        pass
    try:
        await _show_test_admin_card(call.bot, call.message.chat.id, test_id)
    except Exception as e:
        log.warning("back_test: %s", e)
        try:
            await call.message.answer(f"Тест ID {test_id}")
        except Exception:
            pass
    await call.answer()


# ===================== КАРТОЧКА ВОПРОСА =====================

def _question_card_text(q: dict, options: list, pos: int, total: int) -> str:
    """Текст карточки одного вопроса."""
    lines = [f"📝 <b>Вопрос {pos}/{total}</b>", ""]
    lines.append(f"<b>{utils.escape_html(q['text'])}</b>")
    lines.append("")
    for i, opt in enumerate(options, start=1):
        mark = "✅" if opt['is_correct'] else "▫️"
        lines.append(f"{mark} {i}. {utils.escape_html(opt['text'])}")
    if q.get('explanation'):
        lines.append("")
        lines.append(f"💡 <i>{utils.escape_html(q['explanation'])}</i>")
    return "\n".join(lines)


def _question_card_kb(q: dict, test_id: int, pos: int, total: int) -> InlineKeyboardMarkup:
    """Меню действий с вопросом."""
    kb = InlineKeyboardBuilder()
    # Навигация по вопросам — слева/справа
    if pos > 1:
        kb.button(text="◀️ Пред.", callback_data=f"qe:nav:{test_id}:{pos-1}")
    else:
        kb.button(text="·  ·  ·", callback_data="qe:noop")
    kb.button(text=f"{pos}/{total}", callback_data="qe:noop")
    if pos < total:
        kb.button(text="След. ▶️", callback_data=f"qe:nav:{test_id}:{pos+1}")
    else:
        kb.button(text="·  ·  ·", callback_data="qe:noop")
    # Перемещение
    if pos > 1:
        kb.button(text="⬅️ Двиг. влево", callback_data=f"qe:move:{q['id']}:up")
    else:
        kb.button(text=" ", callback_data="qe:noop")
    if pos < total:
        kb.button(text="Двиг. вправо ➡️", callback_data=f"qe:move:{q['id']}:down")
    else:
        kb.button(text=" ", callback_data="qe:noop")
    # Действия
    kb.button(text="✏️ Заменить вопрос", callback_data=f"qe:edit:{q['id']}")
    kb.button(text="🔄 Изменить прав. ответ", callback_data=f"qe:correct:{q['id']}")
    kb.button(text="💡 Добавить объяснение", callback_data=f"qe:expl:{q['id']}")
    kb.button(text="🗑 Удалить вопрос", callback_data=f"qe:del:{q['id']}")
    # Низ
    kb.button(text="📋 К списку вопросов", callback_data=f"qe:list:{test_id}")
    # Расставляем: 3 в навигации, 2 в перемещении, по 1 в действиях
    kb.adjust(3, 2, 1, 1, 1, 1, 1)
    return kb.as_markup()


@router.callback_query(F.data.startswith("qe:view:"), IsAdmin())
async def cb_view_question(call: CallbackQuery, state: FSMContext):
    """Показать карточку одного вопроса."""
    await state.clear()
    try:
        qid = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    q = _get_question(qid)
    if not q:
        await call.answer("Вопрос не найден.", show_alert=True)
        return
    options = _get_options(qid)
    pos, total = _question_position(q['test_id'], qid)
    text = _question_card_text(q, options, pos, total)
    kb = _question_card_kb(q, q['test_id'], pos, total)
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("qe:nav:"), IsAdmin())
async def cb_nav_question(call: CallbackQuery, state: FSMContext):
    """Навигация по вопросам — вперёд/назад."""
    await state.clear()
    try:
        _, _, test_id, pos = call.data.split(":")
        test_id = int(test_id)
        pos = int(pos)
    except (ValueError, IndexError):
        await call.answer()
        return
    q = _get_question_by_position(test_id, pos)
    if not q:
        await call.answer()
        return
    options = _get_options(q['id'])
    _, total = _question_position(test_id, q['id'])
    text = _question_card_text(q, options, pos, total)
    kb = _question_card_kb(q, test_id, pos, total)
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "qe:noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


# ===================== ПЕРЕМЕЩЕНИЕ ВОПРОСА =====================

@router.callback_query(F.data.startswith("qe:move:"), IsAdmin())
async def cb_move_question(call: CallbackQuery):
    """Переместить вопрос вверх/вниз в порядке."""
    try:
        _, _, qid, direction = call.data.split(":")
        qid = int(qid)
    except (ValueError, IndexError):
        await call.answer()
        return
    q = _get_question(qid)
    if not q:
        await call.answer()
        return
    test_id = q['test_id']
    qs = _get_test_questions(test_id)
    idx = next((i for i, x in enumerate(qs) if x['id'] == qid), -1)
    if idx == -1:
        await call.answer()
        return

    if direction == "up" and idx > 0:
        other = qs[idx - 1]
    elif direction == "down" and idx < len(qs) - 1:
        other = qs[idx + 1]
    else:
        await call.answer()
        return

    # Меняем order_num
    a, b = q['order_num'] or 0, other['order_num'] or 0
    # Если одинаковые — назначим уникальные
    if a == b:
        # Перенумеруем весь тест
        for i, x in enumerate(qs):
            db.execute("UPDATE questions SET order_num=? WHERE id=?",
                        (i, x['id']))
        # Повторим
        qs = _get_test_questions(test_id)
        idx = next((i for i, x in enumerate(qs) if x['id'] == qid), -1)
        if direction == "up":
            other = qs[idx - 1]
        else:
            other = qs[idx + 1]
        a, b = qs[idx]['order_num'], other['order_num']

    db.execute("UPDATE questions SET order_num=? WHERE id=?", (b, qid))
    db.execute("UPDATE questions SET order_num=? WHERE id=?", (a, other['id']))

    # Перерисовываем
    q = _get_question(qid)
    options = _get_options(qid)
    pos, total = _question_position(test_id, qid)
    text = _question_card_text(q, options, pos, total)
    kb = _question_card_kb(q, test_id, pos, total)
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass
    await call.answer("✅ Перемещено")


# ===================== УДАЛЕНИЕ =====================

@router.callback_query(F.data.startswith("qe:del:"), IsAdmin())
async def cb_delete_question_ask(call: CallbackQuery):
    """Подтверждение удаления."""
    try:
        qid = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить", callback_data=f"qe:del_yes:{qid}")
    kb.button(text="❌ Отмена", callback_data=f"qe:view:{qid}")
    kb.adjust(1)
    try:
        await call.message.edit_text(
            "🗑 <b>Удалить этот вопрос?</b>\n\n"
            "Действие необратимо. Вопрос пропадёт из теста.",
            reply_markup=kb.as_markup())
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("qe:del_yes:"), IsAdmin())
async def cb_delete_question_confirm(call: CallbackQuery):
    """Удаление с подтверждением."""
    try:
        qid = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    q = _get_question(qid)
    if not q:
        await call.answer("Уже удалён.", show_alert=True)
        return
    test_id = q['test_id']
    try:
        db.execute("DELETE FROM question_options WHERE question_id=?", (qid,))
        db.execute("DELETE FROM questions WHERE id=?", (qid,))
    except Exception as e:
        log.exception("delete question: %s", e)
        await call.answer("Ошибка удаления.", show_alert=True)
        return
    await call.answer("✅ Вопрос удалён")
    # Возвращаемся к списку
    qs = _get_test_questions(test_id)
    test = _get_test(test_id)
    if not qs:
        try:
            await call.message.edit_text(
                f"📝 <b>{utils.escape_html(test['title'])}</b>\n\n"
                f"<i>В тесте больше нет вопросов.</i>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="↩️ К тесту",
                                          callback_data=f"qe:back_test:{test_id}")
                ]]))
        except Exception:
            pass
        return
    try:
        await call.message.edit_text(
            _list_questions_text(test, qs),
            reply_markup=_list_questions_kb(test_id, qs))
    except Exception:
        pass


# ===================== ЗАМЕНА ТЕКСТА ВОПРОСА =====================

@router.callback_query(F.data.startswith("qe:edit:"), IsAdmin())
async def cb_edit_question_ask(call: CallbackQuery, state: FSMContext):
    """Запросить новый текст вопроса."""
    try:
        qid = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    q = _get_question(qid)
    if not q:
        await call.answer()
        return
    await state.set_state(QEditStates.waiting_new_text)
    await state.update_data(qid=qid)
    await call.message.answer(
        f"✏️ <b>Введи новый текст вопроса.</b>\n\n"
        f"Текущий:\n<i>{utils.escape_html(q['text'])}</i>\n\n"
        f"Отправь сообщение с новым текстом, или /cancel для отмены.")
    await call.answer()


@router.message(QEditStates.waiting_new_text, IsAdmin())
async def msg_edit_question(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    new_text = (message.text or "").strip()
    if not new_text:
        await message.answer("Текст не может быть пустым. Попробуй ещё раз.")
        return
    if len(new_text) > 1000:
        await message.answer("Слишком длинный (макс 1000 символов).")
        return
    data = await state.get_data()
    qid = data.get('qid')
    await state.clear()
    db.execute("UPDATE questions SET text=? WHERE id=?", (new_text, qid))
    await message.answer("✅ Вопрос обновлён.")
    # Покажем карточку заново
    q = _get_question(qid)
    options = _get_options(qid)
    pos, total = _question_position(q['test_id'], qid)
    text = _question_card_text(q, options, pos, total)
    kb = _question_card_kb(q, q['test_id'], pos, total)
    await message.answer(text, reply_markup=kb)


# ===================== ДОБАВИТЬ ОБЪЯСНЕНИЕ =====================

@router.callback_query(F.data.startswith("qe:expl:"), IsAdmin())
async def cb_explanation_ask(call: CallbackQuery, state: FSMContext):
    try:
        qid = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    q = _get_question(qid)
    if not q:
        await call.answer()
        return
    await state.set_state(QEditStates.waiting_explanation)
    await state.update_data(qid=qid)
    current = q.get('explanation') or ""
    await call.message.answer(
        f"💡 <b>Введи объяснение к ответу.</b>\n\n"
        f"Текущее:\n<i>{utils.escape_html(current) if current else '(пусто)'}</i>\n\n"
        f"Отправь новое объяснение, или /cancel чтобы отменить.\n"
        f"Отправь «-» чтобы удалить объяснение.")
    await call.answer()


@router.message(QEditStates.waiting_explanation, IsAdmin())
async def msg_set_explanation(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    new_text = (message.text or "").strip()
    if new_text == "-":
        new_text = ""
    if len(new_text) > 1000:
        await message.answer("Слишком длинно (макс 1000).")
        return
    data = await state.get_data()
    qid = data.get('qid')
    await state.clear()
    db.execute("UPDATE questions SET explanation=? WHERE id=?", (new_text, qid))
    if new_text:
        await message.answer("✅ Объяснение обновлено.")
    else:
        await message.answer("✅ Объяснение удалено.")
    q = _get_question(qid)
    options = _get_options(qid)
    pos, total = _question_position(q['test_id'], qid)
    text = _question_card_text(q, options, pos, total)
    kb = _question_card_kb(q, q['test_id'], pos, total)
    await message.answer(text, reply_markup=kb)


# ===================== ИЗМЕНИТЬ ПРАВИЛЬНЫЙ ОТВЕТ ЧЕРЕЗ POLL =====================

# Маппинг poll_id -> qid для отслеживания ответа админа
_correct_poll_map: dict[str, int] = {}


@router.callback_query(F.data.startswith("qe:correct:"), IsAdmin())
async def cb_change_correct(call: CallbackQuery, state: FSMContext):
    """Отправить опрос — админ тапнет правильный вариант."""
    try:
        qid = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    q = _get_question(qid)
    if not q:
        await call.answer()
        return
    options = _get_options(qid)
    if len(options) < 2:
        await call.answer("В вопросе нет вариантов ответа.", show_alert=True)
        return
    if len(options) > 10:
        await call.answer("Слишком много вариантов (макс 10).", show_alert=True)
        return

    # Telegram poll допускает максимум 100 символов в варианте
    poll_opts = [(opt['text'][:100] if len(opt['text']) > 100 else opt['text'])
                  for opt in options]

    # Находим текущий правильный
    current_correct_idx = 0
    for i, opt in enumerate(options):
        if opt['is_correct']:
            current_correct_idx = i
            break

    await call.message.answer(
        "🔄 <b>Тапни правильный вариант ниже:</b>\n\n"
        f"Текущий правильный: <b>{current_correct_idx + 1}. "
        f"{utils.escape_html(options[current_correct_idx]['text'][:80])}</b>"
    )
    poll_msg = await call.message.answer_poll(
        question=q['text'][:300],
        options=poll_opts,
        type='quiz',
        correct_option_id=current_correct_idx,
        is_anonymous=False,
        explanation="Тапни тот вариант, который хочешь сделать правильным.",
    )
    if poll_msg.poll:
        _correct_poll_map[poll_msg.poll.id] = qid
    await state.set_state(QEditStates.waiting_correct_answer)
    await state.update_data(qid=qid, poll_id=poll_msg.poll.id if poll_msg.poll else None)
    await call.answer()


@router.poll_answer()
async def on_poll_answer_admin(poll_answer: PollAnswer, bot: Bot):
    """Когда админ голосует — сохраняем выбранный вариант как правильный."""
    poll_id = poll_answer.poll_id
    qid = _correct_poll_map.pop(poll_id, None)
    if qid is None:
        return  # не наш poll
    user_id = poll_answer.user.id
    if not utils.is_admin(user_id):
        return
    if not poll_answer.option_ids:
        return
    chosen_idx = poll_answer.option_ids[0]

    options = _get_options(qid)
    if chosen_idx >= len(options):
        return

    # Сбрасываем все is_correct и ставим новый
    try:
        for opt in options:
            db.execute(
                "UPDATE question_options SET is_correct=? WHERE id=?",
                (1 if opt['id'] == options[chosen_idx]['id'] else 0, opt['id']))
    except Exception as e:
        log.exception("set correct: %s", e)
        return

    # Уведомим админа
    try:
        await bot.send_message(
            user_id,
            f"✅ <b>Правильный ответ изменён!</b>\n\n"
            f"Теперь правильный: <b>{chosen_idx + 1}. "
            f"{utils.escape_html(options[chosen_idx]['text'][:80])}</b>")
    except Exception as e:
        log.warning("notify admin failed: %s", e)
