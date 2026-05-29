"""
Хендлер апелляций и приостановки тестов.

- ⏸ Приостановить — заморозить таймер, показать Продолжить/Завершить.
- ⚠️ Апелляция — ввести текст → соглашение → отправка админам.
- Админ видит апелляцию, может одобрить/отклонить, редактировать вопрос.
- /findq Q-NNNN — поиск вопроса по серийнику.
"""
import logging
from typing import Optional

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (CallbackQuery, Message, InlineKeyboardMarkup,
                            InlineKeyboardButton)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database as db
import utils
import config
from filters import IsAdmin
from services import appeal_service

router = Router(name="appeals")
log = logging.getLogger(__name__)


class AppealStates(StatesGroup):
    waiting_text = State()
    waiting_confirm = State()


def _get_admin_ids() -> list[int]:
    """Список Telegram ID админов из конфига."""
    ids = getattr(config, 'ADMIN_IDS', None) or []
    try:
        return [int(x) for x in ids]
    except Exception:
        return []


# ===================== ПРИОСТАНОВКА =====================

@router.callback_query(F.data.startswith("tpz:"))
async def cb_pause(call: CallbackQuery):
    """Юзер тапнул «Приостановить» — заморозить таймер."""
    try:
        attempt_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    # Проверяем что юзер — владелец attempt
    a = db.fetchone("SELECT * FROM test_attempts WHERE id=?", (attempt_id,))
    if not a or a.get('user_tg_id') != call.from_user.id:
        await call.answer()
        return

    # Сохраним в БД флаг паузы (если есть колонка) или просто пошлём сообщение
    try:
        db.execute(
            "UPDATE test_attempts SET paused_at=CURRENT_TIMESTAMP WHERE id=?",
            (attempt_id,))
    except Exception:
        pass  # колонки нет — ничего страшного, кнопки покажем всё равно

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="▶️ Продолжить",
                             callback_data=f"tps:resume:{attempt_id}"),
        InlineKeyboardButton(text="🏁 Завершить",
                             callback_data=f"tps:finish:{attempt_id}"),
    ]])
    try:
        await call.message.answer(
            "⏸ <b>Тест приостановлен.</b>\n\n"
            "Время заморожено. Когда будешь готов — выбери:",
            reply_markup=kb,
            parse_mode="HTML")
    except Exception:
        pass
    await call.answer("⏸ Пауза")


@router.callback_query(F.data.startswith("tps:resume:"))
async def cb_resume(call: CallbackQuery):
    try:
        attempt_id = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    a = db.fetchone("SELECT * FROM test_attempts WHERE id=?", (attempt_id,))
    if not a or a.get('user_tg_id') != call.from_user.id:
        await call.answer()
        return
    try:
        db.execute("UPDATE test_attempts SET paused_at=NULL WHERE id=?",
                    (attempt_id,))
    except Exception:
        pass
    try:
        await call.message.edit_text(
            "▶️ <b>Продолжаем!</b>\n\n"
            "Следующий вопрос придёт автоматически.",
            parse_mode="HTML")
    except Exception:
        pass
    await call.answer("▶️")


@router.callback_query(F.data.startswith("tps:finish:"))
async def cb_finish(call: CallbackQuery):
    try:
        attempt_id = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    a = db.fetchone("SELECT * FROM test_attempts WHERE id=?", (attempt_id,))
    if not a or a.get('user_tg_id') != call.from_user.id:
        await call.answer()
        return
    try:
        db.execute(
            "UPDATE test_attempts SET status='finished', "
            "finished_at=CURRENT_TIMESTAMP WHERE id=?",
            (attempt_id,))
    except Exception:
        pass
    try:
        await call.message.edit_text(
            "🏁 <b>Тест завершён.</b>\n\n"
            "Текущие баллы сохранены. Открой профиль чтобы посмотреть статистику.",
            parse_mode="HTML")
    except Exception:
        pass
    await call.answer("🏁 Завершено")


# ===================== АПЕЛЛЯЦИЯ =====================

@router.callback_query(F.data.startswith("tap:"))
async def cb_appeal_start(call: CallbackQuery, state: FSMContext):
    """Юзер тапнул «Апелляция»."""
    try:
        _, attempt_id, qid = call.data.split(":")
        attempt_id = int(attempt_id)
        qid = int(qid)
    except (ValueError, IndexError):
        await call.answer()
        return
    a = db.fetchone("SELECT * FROM test_attempts WHERE id=?", (attempt_id,))
    if not a or a.get('user_tg_id') != call.from_user.id:
        await call.answer()
        return
    q = db.fetchone("SELECT * FROM questions WHERE id=?", (qid,))
    if not q:
        await call.answer("Вопрос не найден.", show_alert=True)
        return
    # Проверим бан
    banned, until = appeal_service.is_user_banned(call.from_user.id)
    if banned:
        await call.answer(
            "Ты временно отстранён от тестов. Подавать апелляции пока нельзя.",
            show_alert=True)
        return

    # Приостанавливаем
    try:
        db.execute(
            "UPDATE test_attempts SET paused_at=CURRENT_TIMESTAMP WHERE id=?",
            (attempt_id,))
    except Exception:
        pass

    serial = q.get('serial_no') or f"Q-{qid:04d}"
    await state.set_state(AppealStates.waiting_text)
    await state.update_data(appeal_qid=qid, appeal_attempt=attempt_id)
    await call.message.answer(
        f"⚠️ <b>Апелляция на вопрос {serial}</b>\n\n"
        f"Тест приостановлен на время апелляции.\n\n"
        f"📝 <b>Напиши:</b>\n"
        f"• Правильное решение (на твой взгляд)\n"
        f"• Страница в учебнике\n"
        f"• Автор и класс учебника\n\n"
        f"<i>Пример:</i>\n"
        f"<i>«Правильный — B. Учебник Алмаатинов А., 10 класс, стр. 145»</i>\n\n"
        f"Отправь сообщение или /cancel для отмены.",
        parse_mode="HTML")
    await call.answer()


@router.message(AppealStates.waiting_text)
async def msg_appeal_text(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        # Снимаем паузу
        data = await state.get_data()
        try:
            db.execute("UPDATE test_attempts SET paused_at=NULL WHERE id=?",
                        (data.get('appeal_attempt'),))
        except Exception:
            pass
        await message.answer("❌ Апелляция отменена. Возвращайся к тесту.")
        return
    text = (message.text or "").strip()
    if len(text) < 10:
        await message.answer(
            "Слишком короткое сообщение. Опиши подробнее (минимум 10 символов).")
        return
    if len(text) > 1500:
        await message.answer("Слишком длинное (макс 1500 символов).")
        return

    data = await state.get_data()
    qid = data.get('appeal_qid')
    await state.update_data(appeal_text=text)
    await state.set_state(AppealStates.waiting_confirm)

    cur_warn = appeal_service.get_user_warnings(message.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Согласен, отправить",
                              callback_data="apl:confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="apl:cancel")],
    ])
    await message.answer(
        f"📋 <b>Перед отправкой:</b>\n\n"
        f"Если апелляция окажется ложной, ты получишь предупреждение "
        f"(<b>{cur_warn + 1}/{appeal_service.MAX_WARNINGS}</b>).\n\n"
        f"При <b>{appeal_service.MAX_WARNINGS}</b> ложных апелляциях — "
        f"временное отстранение от тестов на <b>{appeal_service.BAN_DAYS} "
        f"{'день' if appeal_service.BAN_DAYS == 1 else 'дня'}</b>.\n\n"
        f"Твоих предупреждений сейчас: <b>{cur_warn}/{appeal_service.MAX_WARNINGS}</b>",
        reply_markup=kb,
        parse_mode="HTML")


@router.callback_query(F.data == "apl:cancel", AppealStates.waiting_confirm)
async def cb_appeal_cancel(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    try:
        db.execute("UPDATE test_attempts SET paused_at=NULL WHERE id=?",
                    (data.get('appeal_attempt'),))
    except Exception:
        pass
    await state.clear()
    try:
        await call.message.edit_text(
            "❌ Апелляция отменена. Возвращайся к тесту.")
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data == "apl:confirm", AppealStates.waiting_confirm)
async def cb_appeal_confirm(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    qid = data.get('appeal_qid')
    text = data.get('appeal_text') or ''
    attempt_id = data.get('appeal_attempt')
    await state.clear()
    if not qid:
        await call.answer()
        return

    # Создаём апелляцию
    try:
        appeal_id = appeal_service.create_appeal(
            qid, call.from_user.id, text)
    except Exception as e:
        log.exception("create_appeal: %s", e)
        await call.message.answer("⚠️ Не смог сохранить апелляцию. Попробуй позже.")
        return

    # Снимаем паузу с теста
    try:
        db.execute("UPDATE test_attempts SET paused_at=NULL WHERE id=?",
                    (attempt_id,))
    except Exception:
        pass

    try:
        await call.message.edit_text(
            "✅ <b>Апелляция отправлена!</b>\n\n"
            "Админ рассмотрит её в ближайшее время. "
            "Можешь продолжать тест — следующий вопрос придёт.",
            parse_mode="HTML")
    except Exception:
        pass
    await call.answer("✅")

    # Уведомление админам
    await _notify_admins_about_appeal(bot, appeal_id)


# ===================== АДМИН-ИНТЕРФЕЙС =====================

async def _notify_admins_about_appeal(bot: Bot, appeal_id: int):
    """Отправить апелляцию всем админам."""
    appeal = appeal_service.get_appeal(appeal_id)
    if not appeal:
        return
    q = db.fetchone("SELECT * FROM questions WHERE id=?", (appeal['question_id'],))
    if not q:
        return
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (q['test_id'],))
    user = db.fetchone("SELECT * FROM users WHERE tg_id=?", (appeal['user_tg_id'],))
    serial = q.get('serial_no') or f"Q-{q['id']:04d}"
    stats = appeal_service.get_question_stats(q['id'])

    text = (
        f"⚠️ <b>НОВАЯ АПЕЛЛЯЦИЯ #{appeal_id}</b>\n\n"
        f"От: @{utils.escape_html(user.get('username') or '—') if user else '—'} "
        f"({utils.escape_html(user.get('first_name') or '—') if user else '—'})\n"
        f"Вопрос: <code>{serial}</code> (тест «{utils.escape_html(test.get('title') or '—')}»)\n\n"
        f"<b>Текст пользователя:</b>\n"
        f"<i>{utils.escape_html(appeal['user_text'])}</i>\n\n"
        f"📊 <b>Статистика по вопросу:</b>\n"
        f"• Прошли: {stats['passes']}\n"
        f"• Правильных ответов: {stats['correct']} ({stats['correct_pct']}%)\n"
        f"• Апелляций до этого: {stats['appeals_total'] - 1}"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Карточка вопроса",
              callback_data=f"qe:view:{q['id']}")
    kb.button(text="✏️ Заменить вопрос",
              callback_data=f"qe:edit:{q['id']}")
    kb.button(text="🔄 Изменить правильный ответ",
              callback_data=f"qe:correct:{q['id']}")
    kb.button(text="🗑 Удалить вопрос",
              callback_data=f"qe:del:{q['id']}")
    kb.button(text="✅ Апелляция верна — одобрить",
              callback_data=f"apladm:approve:{appeal_id}")
    kb.button(text="❌ Ложная апелляция",
              callback_data=f"apladm:reject:{appeal_id}")
    kb.adjust(1)

    admins = _get_admin_ids()
    for admin_id in admins:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML",
                                     reply_markup=kb.as_markup())
            # Quiz Poll с вопросом для удобства
            opts = db.fetchall(
                "SELECT * FROM question_options WHERE question_id=? ORDER BY order_num, id",
                (q['id'],))
            if 2 <= len(opts) <= 10:
                correct_idx = 0
                for i, o in enumerate(opts):
                    if o.get('is_correct'):
                        correct_idx = i
                        break
                try:
                    await bot.send_poll(
                        admin_id,
                        question=q['text'][:300],
                        options=[o['text'][:100] for o in opts],
                        type='quiz',
                        correct_option_id=correct_idx,
                        is_anonymous=True,
                        explanation=(q.get('explanation') or '')[:200] or None,
                    )
                except Exception:
                    pass
        except Exception as e:
            log.warning("notify admin %s: %s", admin_id, e)


@router.callback_query(F.data.startswith("apladm:approve:"), IsAdmin())
async def cb_admin_approve(call: CallbackQuery, bot: Bot):
    try:
        appeal_id = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    appeal = appeal_service.get_appeal(appeal_id)
    if not appeal:
        await call.answer("Апелляция не найдена.", show_alert=True)
        return
    if appeal['status'] != 'pending':
        await call.answer(f"Уже {appeal['status']}.", show_alert=True)
        return
    appeal_service.approve_appeal(appeal_id, call.from_user.id)
    q = db.fetchone("SELECT * FROM questions WHERE id=?", (appeal['question_id'],))
    serial = (q.get('serial_no') if q else None) or f"Q-{appeal['question_id']:04d}"

    # Уведомить юзера
    try:
        await bot.send_message(
            appeal['user_tg_id'],
            f"✅ <b>Твоя апелляция по вопросу {serial} одобрена!</b>\n\n"
            f"Балл засчитан тебе как правильный.\n"
            f"Спасибо за внимательность 💪",
            parse_mode="HTML")
    except Exception as e:
        log.warning("notify user approve: %s", e)

    try:
        await call.message.edit_text(
            (call.message.html_text or call.message.text or '') +
            f"\n\n✅ <b>ОДОБРЕНО админом</b>",
            parse_mode="HTML")
    except Exception:
        pass
    await call.answer("✅ Одобрено")


@router.callback_query(F.data.startswith("apladm:reject:"), IsAdmin())
async def cb_admin_reject(call: CallbackQuery, bot: Bot):
    try:
        appeal_id = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    appeal = appeal_service.get_appeal(appeal_id)
    if not appeal:
        await call.answer("Апелляция не найдена.", show_alert=True)
        return
    if appeal['status'] != 'pending':
        await call.answer(f"Уже {appeal['status']}.", show_alert=True)
        return
    warnings, banned = appeal_service.reject_appeal(appeal_id, call.from_user.id)
    q = db.fetchone("SELECT * FROM questions WHERE id=?", (appeal['question_id'],))
    serial = (q.get('serial_no') if q else None) or f"Q-{appeal['question_id']:04d}"

    # Уведомить юзера
    try:
        if banned:
            await bot.send_message(
                appeal['user_tg_id'],
                f"⛔️ <b>Твоя апелляция по вопросу {serial} отклонена.</b>\n\n"
                f"Это было твоё <b>{appeal_service.MAX_WARNINGS}-е</b> "
                f"ложное обращение.\n\n"
                f"🚫 Ты <b>временно отстранён</b> от тестов на "
                f"<b>{appeal_service.BAN_DAYS} {'день' if appeal_service.BAN_DAYS == 1 else 'дня'}</b>.",
                parse_mode="HTML")
        else:
            await bot.send_message(
                appeal['user_tg_id'],
                f"⚠️ <b>Твоя апелляция по вопросу {serial} отклонена.</b>\n\n"
                f"Предупреждений: <b>{warnings}/{appeal_service.MAX_WARNINGS}</b>\n\n"
                f"При {appeal_service.MAX_WARNINGS} ложных апелляциях — "
                f"отстранение от тестов на {appeal_service.BAN_DAYS} "
                f"{'день' if appeal_service.BAN_DAYS == 1 else 'дня'}.",
                parse_mode="HTML")
    except Exception as e:
        log.warning("notify user reject: %s", e)

    try:
        await call.message.edit_text(
            (call.message.html_text or call.message.text or '') +
            f"\n\n❌ <b>ОТКЛОНЕНО</b> · юзеру предупреждение {warnings}/3"
            + (f" + БАН на {appeal_service.BAN_DAYS} д." if banned else ""),
            parse_mode="HTML")
    except Exception:
        pass
    await call.answer("❌ Отклонено")


# ===================== ПОИСК ПО СЕРИЙНОМУ НОМЕРУ =====================

@router.message(Command("findq"), IsAdmin())
async def cmd_find_question(message: Message):
    parts = (message.text or '').split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "🔍 <b>Поиск вопроса по серийному номеру</b>\n\n"
            "Использование: <code>/findq Q-1247</code>\n"
            "или просто число: <code>/findq 1247</code>",
            parse_mode="HTML")
        return
    serial = parts[1].strip()
    q = appeal_service.find_question_by_serial(serial)
    if not q:
        await message.answer(
            f"⚠️ Вопрос <code>{utils.escape_html(serial)}</code> не найден.",
            parse_mode="HTML")
        return
    await _show_question_card(message, q)


async def _show_question_card(message: Message, q: dict):
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (q['test_id'],))
    cat = None
    if test and test.get('category_id'):
        cat = db.fetchone("SELECT name FROM test_categories WHERE id=?",
                           (test['category_id'],))
    stats = appeal_service.get_question_stats(q['id'])
    opts = db.fetchall(
        "SELECT * FROM question_options WHERE question_id=? ORDER BY order_num, id",
        (q['id'],))
    correct_opt = next((o for o in opts if o.get('is_correct')), None)
    serial = q.get('serial_no') or f"Q-{q['id']:04d}"

    text = (
        f"🔍 <b>Вопрос {serial}</b>\n\n"
        f"<b>Тест:</b> «{utils.escape_html(test.get('title') if test else '—')}»\n"
        + (f"<b>Раздел:</b> {utils.escape_html(cat['name'])}\n" if cat else "")
        + f"\n<b>Текст:</b> {utils.escape_html(q['text'])}\n\n"
    )
    if correct_opt:
        text += f"✅ <b>Правильный:</b> {utils.escape_html(correct_opt['text'])}\n\n"
    text += (
        f"📊 <b>Статистика:</b>\n"
        f"• Прошли вопрос: {stats['passes']} раз\n"
        f"• Правильно: {stats['correct']} ({stats['correct_pct']}%)\n"
        f"• Неправильно: {stats['wrong']}\n"
        f"• Апелляций всего: {stats['appeals_total']}\n"
        f"   ✅ Одобрено: {stats['appeals_approved']}\n"
        f"   ❌ Отклонено: {stats['appeals_rejected']}\n"
        f"   ⏳ В ожидании: {stats['appeals_pending']}"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Открыть редактор вопроса",
              callback_data=f"qe:view:{q['id']}")
    kb.adjust(1)
    await message.answer(text, reply_markup=kb.as_markup(),
                          parse_mode="HTML")


# ===================== СПИСОК АПЕЛЛЯЦИЙ =====================

@router.callback_query(F.data == "adm:appeals", IsAdmin())
async def cb_list_appeals(call: CallbackQuery):
    rows = appeal_service.list_pending_appeals(limit=20)
    if not rows:
        try:
            await call.message.edit_text(
                "⚠️ <b>Апелляции на рассмотрение</b>\n\n"
                "Сейчас нет ни одной апелляции в ожидании.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="↩️ В админ-меню",
                                         callback_data="m:admin")
                ]]),
                parse_mode="HTML")
        except Exception:
            pass
        await call.answer()
        return
    text = f"⚠️ <b>Апелляций в ожидании: {len(rows)}</b>\n\n"
    kb = InlineKeyboardBuilder()
    for r in rows[:15]:
        q = db.fetchone("SELECT serial_no, text FROM questions WHERE id=?",
                         (r['question_id'],))
        serial = (q.get('serial_no') if q else None) or f"Q-{r['question_id']:04d}"
        preview = (q['text'][:30] if q and q.get('text') else '—')
        kb.button(text=f"#{r['id']} · {serial} · {preview}",
                  callback_data=f"apladm:open:{r['id']}")
    kb.button(text="↩️ В админ-меню", callback_data="m:admin")
    kb.adjust(1)
    try:
        await call.message.edit_text(text, reply_markup=kb.as_markup(),
                                       parse_mode="HTML")
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("apladm:open:"), IsAdmin())
async def cb_open_appeal(call: CallbackQuery, bot: Bot):
    try:
        appeal_id = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    # Просто шлём карточку заново как при уведомлении
    await _notify_admins_about_appeal(bot, appeal_id)
    await call.answer("Карточка отправлена 👇")
