"""Хендлеры домашних заданий."""
import asyncio
import logging

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

import config
import utils
from locales import t
from keyboards import back_kb, main_menu_kb
from states import HomeworkStates
from services import notes_service, homework_service, test_runner

router = Router(name="homework")
log = logging.getLogger(__name__)


@router.callback_query(F.data.startswith("hw:"))
async def cb_hw(call: CallbackQuery, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    parts = call.data.split(":")
    if len(parts) != 2:
        await call.answer()
        return
    try:
        note_id = int(parts[1])
    except ValueError:
        await call.answer()
        return

    hw = notes_service.get_homework(note_id)
    if not hw:
        await call.answer(t("hw_not_available", lang), show_alert=True)
        return

    if hw['homework_type'] == 'test' and hw.get('test_id'):
        # Запускаем привязанный тест
        test_id = hw['test_id']
        test = test_runner.get_test(test_id)
        if not test:
            await call.answer(t("test_not_found", lang), show_alert=True)
            return
        attempt_id = test_runner.create_attempt(
            user['id'], test_id, lang, group_id=None,
            started_by_user_id=user['id'])
        await call.message.answer(t("hw_test_intro", lang))
        await asyncio.sleep(1.5)
        await test_runner.send_current_question(call.bot, attempt_id,
                                                  call.message.chat.id)
        await call.answer()
        return

    # Открытый ответ
    if hw['homework_type'] == 'open':
        await state.set_state(HomeworkStates.waiting_open_answer)
        await state.update_data(hw_note_id=note_id)
        task_text = hw.get('open_task_prompt') or ""
        await call.message.answer(
            t("hw_open_intro", lang, task=utils.escape_html(task_text)))
        await call.answer()
        return

    await call.answer(t("hw_not_available", lang), show_alert=True)


@router.message(HomeworkStates.waiting_open_answer)
async def msg_hw_open(message: Message, state: FSMContext, user: dict):
    lang = user.get('language') or 'ru'
    answer = (message.text or "").strip()
    if not answer:
        return
    data = await state.get_data()
    note_id = data.get('hw_note_id')
    if not note_id:
        await state.clear()
        return
    hw = notes_service.get_homework(note_id)
    if not hw:
        await state.clear()
        await message.answer(t("hw_not_available", lang))
        return
    matched, total, score = homework_service.check_open_answer(
        answer, hw.get('open_task_keywords') or "")
    homework_service.save_homework_result(user['id'], note_id, score, answer)
    await state.clear()
    await message.answer(
        t("hw_score", lang, score=score, matched=matched, total=total),
        reply_markup=main_menu_kb(lang, utils.is_admin(message.from_user.id)),
    )
