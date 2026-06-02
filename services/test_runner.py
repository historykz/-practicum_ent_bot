"""
Сервис прохождения теста.

Реализует:
- Создание попытки.
- Перемешивание вопросов и вариантов на уровне попытки.
- Отдельный таймер на каждый вопрос (asyncio task).
- Защиту от повторного ответа.
- Пауза при последовательных пропусках.
- Подсчёт результата + слабые темы.

Architecture note:
Активные таймеры хранятся в памяти процесса. При перезапуске бот восстанавливает
паузу (active=False) и предложит пользователю продолжить вручную.
"""
import asyncio
import json
import logging
import random
import time
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

import database as db
from config import (
    DEFAULT_TIME_PER_QUESTION,
    MAX_PAUSE_MISS_COUNT,
    PROTECT_CONTENT,
)
from keyboards import options_kb, pause_personal_kb
from locales import t
from utils import (
    build_question_text,
    escape_html,
    get_user_by_id,
    now_iso,
    percent_to_level,
)

logger = logging.getLogger(__name__)

# Активные таймеры: attempt_id -> asyncio.Task
_timers: dict[int, asyncio.Task] = {}
# Активные сообщения с вопросом: attempt_id -> (chat_id, message_id)
_active_messages: dict[int, tuple[int, int]] = {}
# Quiz Poll: poll_id -> {attempt_id, question_id, option_order}
# option_order — список option_id в том порядке, в котором показаны в poll
_poll_map: dict[str, dict] = {}
# attempt_id → [(chat_id, msg_id), ...] — для удаления Quiz Poll после завершения приватного теста
_private_poll_msgs: dict[int, list[tuple[int, int]]] = {}


def cancel_timer(attempt_id: int) -> None:
    """Отменить таймер вопроса."""
    task = _timers.pop(attempt_id, None)
    if task and not task.done():
        task.cancel()


async def _maybe_render_math(bot, chat_id: int, text: str) -> bool:
    """
    Если в тексте вопроса есть математика — отрисовать картинку и отправить.
    Кэширует file_id чтобы не рендерить повторно. Возвращает True если отрисовал.
    """
    try:
        from services import formula_service as _fs
    except Exception:
        return False
    if not _fs.has_math(text):
        return False
    # Кэш
    cached = _fs.get_cached_file_id(text)
    if cached:
        try:
            await bot.send_photo(chat_id=chat_id, photo=cached,
                                  protect_content=PROTECT_CONTENT)
            return True
        except Exception:
            pass  # file_id протух — перерендерим
    # Рендерим
    import os as _os, time as _t
    out = f"/tmp/q_math_{_t.time_ns()}.png"
    if not _fs.render_question_image(text, out):
        return False
    try:
        from aiogram.types import FSInputFile
        msg = await bot.send_photo(chat_id=chat_id, photo=FSInputFile(out),
                                    protect_content=PROTECT_CONTENT)
        # Кэшируем новый file_id
        if msg.photo:
            _fs.set_cached_file_id(text, msg.photo[-1].file_id)
        return True
    except Exception as e:
        log.warning("send math image: %s", e)
        return False
    finally:
        try:
            _os.remove(out)
        except Exception:
            pass


def get_test(test_id: int) -> Optional[dict]:
    row = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))
    return dict(row) if row else None


def get_test_questions(test_id: int) -> list[dict]:
    rows = db.fetchall(
        "SELECT * FROM questions WHERE test_id=? ORDER BY order_num, id",
        (test_id,),
    )
    return [dict(r) for r in rows]


def get_question(question_id: int) -> Optional[dict]:
    row = db.fetchone("SELECT * FROM questions WHERE id=?", (question_id,))
    return dict(row) if row else None


def get_question_options(question_id: int) -> list[dict]:
    rows = db.fetchall(
        "SELECT * FROM question_options WHERE question_id=? ORDER BY order_num, id",
        (question_id,),
    )
    return [dict(r) for r in rows]


def get_attempt(attempt_id: int) -> Optional[dict]:
    row = db.fetchone("SELECT * FROM test_attempts WHERE id=?", (attempt_id,))
    return dict(row) if row else None


def count_user_attempts(user_id: int, test_id: int) -> int:
    row = db.fetchone(
        "SELECT COUNT(*) AS c FROM test_attempts WHERE user_id=? AND test_id=? AND status IN ('finished','aborted')",
        (user_id, test_id),
    )
    return row["c"] if row else 0


def create_attempt(user_id: int, test_id: int, language: str,
                   group_id: Optional[int] = None,
                   started_by_user_id: Optional[int] = None) -> Optional[int]:
    """Создаёт попытку прохождения теста. Возвращает attempt_id или None."""
    # Проверка бана за ложные апелляции (только для личных тестов)
    if group_id is None:
        try:
            from services import appeal_service
            banned, until = appeal_service.is_user_banned(user_id)
            if banned:
                log.info("user %s banned until %s — block test start",
                         user_id, until)
                return None
        except Exception:
            pass

    test = get_test(test_id)
    if not test:
        return None
    questions = get_test_questions(test_id)
    if not questions:
        return None

    # Перемешать порядок вопросов
    qids = [q["id"] for q in questions]
    if test["shuffle_questions"]:
        random.shuffle(qids)

    # Перемешать варианты для каждого вопроса
    options_order: dict[str, list[int]] = {}
    if test["shuffle_options"]:
        for qid in qids:
            opts = get_question_options(qid)
            ids = [o["id"] for o in opts]
            random.shuffle(ids)
            options_order[str(qid)] = ids

    # Определяем номер попытки и засчитывается ли
    finished_count = count_user_attempts(user_id, test_id)
    attempt_num = finished_count + 1
    is_first = (finished_count == 0)
    is_counted = 1
    if test["first_attempt_only"] and not is_first:
        is_counted = 0

    db.execute(
        """INSERT INTO test_attempts
        (user_id, test_id, current_question_index, question_order, options_order,
         start_time, status, language, attempt_num, is_first_attempt, is_counted,
         group_id, started_by_user_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            user_id, test_id, 0,
            json.dumps(qids),
            json.dumps(options_order),
            now_iso(),
            "in_progress",
            language,
            attempt_num,
            1 if is_first else 0,
            is_counted,
            group_id,
            started_by_user_id,
        ),
    )
    row = db.fetchone("SELECT last_insert_rowid() AS id")
    return row["id"] if row else None


def create_redo_attempt(prev_attempt_id: int) -> Optional[int]:
    """
    Создать попытку ТОЛЬКО из вопросов где юзер ошибся в prev_attempt_id.
    Не засчитывается в статистику (is_counted=0).
    """
    prev = get_attempt(prev_attempt_id)
    if not prev:
        return None
    # Ошибочные вопросы (неправильные, не пропущенные)
    wrong_rows = db.fetchall(
        "SELECT DISTINCT question_id FROM attempt_answers "
        "WHERE attempt_id=? AND is_correct=0 AND COALESCE(skipped,0)=0",
        (prev_attempt_id,))
    qids = [r['question_id'] for r in wrong_rows]
    if not qids:
        return None
    test = get_test(prev['test_id'])
    if not test:
        return None
    random.shuffle(qids)
    # Перемешать варианты
    options_order = {}
    if test["shuffle_options"]:
        for qid in qids:
            opts = get_question_options(qid)
            ids = [o["id"] for o in opts]
            random.shuffle(ids)
            options_order[str(qid)] = ids
    db.execute(
        """INSERT INTO test_attempts
        (user_id, test_id, current_question_index, question_order, options_order,
         start_time, status, language, attempt_num, is_first_attempt, is_counted,
         group_id, started_by_user_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (prev['user_id'], prev['test_id'], 0,
          json.dumps(qids), json.dumps(options_order),
          now_iso(), "in_progress", prev['language'] or 'ru',
          999, 0, 0,  # attempt_num=999, не первая, НЕ засчитывается
          None, prev['user_id']))
    row = db.fetchone("SELECT last_insert_rowid() AS id")
    return row["id"] if row else None


def _get_ordered_options(question_id: int, attempt: dict) -> list[dict]:
    """Возвращает варианты в нужном для пользователя порядке."""
    opts = get_question_options(question_id)
    try:
        options_order = json.loads(attempt["options_order"] or "{}")
    except (ValueError, TypeError):
        options_order = {}
    order = options_order.get(str(question_id))
    if not order:
        return opts
    by_id = {o["id"]: o for o in opts}
    return [by_id[oid] for oid in order if oid in by_id]


def _can_use_quiz_poll(question_text: str, options: list[dict]) -> bool:
    """Проверка лимитов Telegram Quiz Poll."""
    if len(question_text) > 300:
        return False
    if not (2 <= len(options) <= 10):
        return False
    for o in options:
        if len(o["text"]) > 100:
            return False
    return True


async def send_current_question(bot: Bot, attempt_id: int, chat_id: int) -> None:
    """Отправляет в чат текущий вопрос. Запускает таймер."""
    attempt = get_attempt(attempt_id)
    if not attempt or attempt["status"] != "in_progress":
        return
    test = get_test(attempt["test_id"])
    if not test:
        return
    try:
        qids: list[int] = json.loads(attempt["question_order"] or "[]")
    except (ValueError, TypeError):
        qids = []
    idx = attempt["current_question_index"]
    if idx >= len(qids):
        # Все вопросы пройдены - финализируем
        await finalize_attempt(bot, attempt_id, chat_id)
        return
    qid = qids[idx]
    q = get_question(qid)
    if not q:
        # Пропуск битого вопроса
        db.execute(
            "UPDATE test_attempts SET current_question_index=current_question_index+1 WHERE id=?",
            (attempt_id,),
        )
        await send_current_question(bot, attempt_id, chat_id)
        return
    options = _get_ordered_options(qid, attempt)
    time_per_q = test["time_per_question"] or DEFAULT_TIME_PER_QUESTION
    # Telegram poll: open_period 5-600 сек
    poll_period = max(5, min(600, time_per_q))
    lang = attempt["language"] or "ru"

    # Заголовок (номер + общая сводка) шлём отдельным сообщением — не помещается в poll question.
    prefix = t("question_progress", lang, n=idx + 1, total=len(qids), sec=time_per_q)
    poll_question = q["text"]

    use_poll = _can_use_quiz_poll(poll_question, options)

    # Кнопка «🛑 СТОП» для прерывания — показывается с каждым заголовком вопроса
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    stop_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛑 СТОП", callback_data=f"abort:{attempt_id}")
    ]])

    msg = None
    try:
        if use_poll:
            # Шлём заголовок С КНОПКОЙ СТОП
            await bot.send_message(chat_id=chat_id, text=prefix,
                                    parse_mode="HTML", reply_markup=stop_kb)
            # Картинка: прикреплённая ИЛИ авто-рендер формулы из текста
            _photo = q.get("photo_file_id") or q.get("image_file_id")
            if _photo:
                try:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=_photo,
                        protect_content=PROTECT_CONTENT,
                    )
                except Exception:
                    pass
            else:
                # Авто-рендер математики если есть формулы
                _auto = await _maybe_render_math(bot, chat_id, q.get("text") or "")
                # (если отрисовали — картинка уже отправлена)
            # Находим индекс правильного варианта в текущем порядке
            correct_idx = 0
            for i, o in enumerate(options):
                if o.get("is_correct"):
                    correct_idx = i
                    break
            option_texts = [o["text"] for o in options]
            poll_msg = await bot.send_poll(
                chat_id=chat_id,
                question=poll_question[:300],
                options=option_texts,
                type="quiz",
                correct_option_id=correct_idx,
                is_anonymous=False,
                open_period=poll_period,
                explanation=(q.get("explanation") or "")[:200] or None,
                protect_content=PROTECT_CONTENT,
            )
            # Запомним связь poll_id -> attempt/question
            _poll_map[poll_msg.poll.id] = {
                "attempt_id": attempt_id,
                "question_id": qid,
                "option_order": [o["id"] for o in options],
                "correct_option_id_in_poll": correct_idx,
                "chat_id": chat_id,
                "msg_id": poll_msg.message_id,
                "sent_at": time.time(),
            }
            msg = poll_msg
            # Кнопки «Приостановить» и «Апелляция» — только в личке
            try:
                from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
                pause_kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="⏸ Приостановить",
                                         callback_data=f"tpz:{attempt_id}"),
                    InlineKeyboardButton(text="⚠️ Апелляция",
                                         callback_data=f"tap:{attempt_id}:{qid}"),
                ]])
                await bot.send_message(
                    chat_id=chat_id,
                    text="↑ Управление вопросом:",
                    reply_markup=pause_kb,
                )
            except Exception:
                pass
        else:
            # Fallback на inline-кнопки
            text = build_question_text(idx + 1, len(qids), q["text"], time_per_q, lang)
            _photo2 = q.get("photo_file_id") or q.get("image_file_id")
            if _photo2:
                msg = await bot.send_photo(
                    chat_id=chat_id,
                    photo=_photo2,
                    caption=text,
                    reply_markup=options_kb(attempt_id, qid, options),
                    parse_mode="HTML",
                    protect_content=PROTECT_CONTENT,
                )
            else:
                # Авто-рендер математики (картинка перед кнопками)
                await _maybe_render_math(bot, chat_id, q.get("text") or "")
                msg = await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=options_kb(attempt_id, qid, options),
                    parse_mode="HTML",
                    protect_content=PROTECT_CONTENT,
                )
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.warning("Не удалось отправить вопрос: %s", e)
        return

    if msg:
        _active_messages[attempt_id] = (chat_id, msg.message_id)
    # Запускаем таймер (он же определяет момент перехода к следующему вопросу)
    cancel_timer(attempt_id)
    _timers[attempt_id] = asyncio.create_task(
        _question_timeout(bot, attempt_id, qid, chat_id, time_per_q)
    )


async def _question_timeout(bot: Bot, attempt_id: int, question_id: int,
                            chat_id: int, seconds: int) -> None:
    """Таймер на вопрос. По истечении - пропуск."""
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:
        return
    # Проверяем что попытка ещё активна и на этом же вопросе
    attempt = get_attempt(attempt_id)
    if not attempt or attempt["status"] != "in_progress":
        return
    try:
        qids: list[int] = json.loads(attempt["question_order"] or "[]")
    except (ValueError, TypeError):
        qids = []
    idx = attempt["current_question_index"]
    if idx >= len(qids) or qids[idx] != question_id:
        return
    # Проверяем, не отвечал ли пользователь
    answered = db.fetchone(
        "SELECT id FROM attempt_answers WHERE attempt_id=? AND question_id=?",
        (attempt_id, question_id),
    )
    if answered:
        return
    # ДВОЙНАЯ проверка статуса прямо перед действиями (защита от гонки с паузой)
    re_check = get_attempt(attempt_id)
    if not re_check or re_check["status"] != "in_progress":
        return
    # Засчитываем пропуск
    db.execute(
        "INSERT INTO attempt_answers (attempt_id, question_id, skipped) VALUES (?,?,1)",
        (attempt_id, question_id),
    )
    db.execute(
        "UPDATE test_attempts SET skipped_answers=skipped_answers+1, "
        "missed_questions_counter=missed_questions_counter+1, "
        "current_question_index=current_question_index+1 WHERE id=?",
        (attempt_id,),
    )

    # Сообщение о таймауте
    lang = attempt["language"] or "ru"
    try:
        await bot.send_message(chat_id=chat_id, text=t("question_skipped", lang),
                               protect_content=PROTECT_CONTENT)
    except Exception:
        pass

    # Проверяем пауза или дальше
    attempt2 = get_attempt(attempt_id)
    if attempt2 and attempt2["missed_questions_counter"] >= MAX_PAUSE_MISS_COUNT:
        await pause_attempt(bot, attempt_id, chat_id)
        return
    await send_current_question(bot, attempt_id, chat_id)


async def process_answer(bot: Bot, attempt_id: int, question_id: int,
                        option_id: int, chat_id: int) -> str:
    """
    Обрабатывает ответ пользователя.
    Возвращает короткий код: 'ok', 'already', 'invalid', 'old'.
    """
    attempt = get_attempt(attempt_id)
    if not attempt:
        return "old"
    if attempt["status"] != "in_progress":
        return "old"

    try:
        qids: list[int] = json.loads(attempt["question_order"] or "[]")
    except (ValueError, TypeError):
        qids = []
    idx = attempt["current_question_index"]
    if idx >= len(qids):
        return "old"
    # Только текущий вопрос
    if qids[idx] != question_id:
        return "old"

    existing = db.fetchone(
        "SELECT id FROM attempt_answers WHERE attempt_id=? AND question_id=?",
        (attempt_id, question_id),
    )
    if existing:
        return "already"

    # Проверяем правильность
    opt = db.fetchone(
        "SELECT * FROM question_options WHERE id=? AND question_id=?",
        (option_id, question_id),
    )
    if not opt:
        return "invalid"
    is_correct = bool(opt["is_correct"])

    db.execute(
        "INSERT INTO attempt_answers (attempt_id, question_id, selected_option_id, is_correct) VALUES (?,?,?,?)",
        (attempt_id, question_id, option_id, 1 if is_correct else 0),
    )
    if is_correct:
        db.execute(
            "UPDATE test_attempts SET correct_answers=correct_answers+1, "
            "missed_questions_counter=0, current_question_index=current_question_index+1 WHERE id=?",
            (attempt_id,),
        )
    else:
        db.execute(
            "UPDATE test_attempts SET wrong_answers=wrong_answers+1, "
            "missed_questions_counter=0, current_question_index=current_question_index+1 WHERE id=?",
            (attempt_id,),
        )

    cancel_timer(attempt_id)
    # Следующий вопрос или финал
    attempt2 = get_attempt(attempt_id)
    if attempt2 and attempt2["current_question_index"] >= len(qids):
        await finalize_attempt(bot, attempt_id, chat_id)
    else:
        await send_current_question(bot, attempt_id, chat_id)
    return "ok"


async def process_poll_answer(bot: Bot, poll_id: str, option_ids: list[int],
                               user_tg_id: int) -> None:
    """
    Обработка ответа из Telegram Quiz Poll (poll_answer update).
    option_ids — индексы выбранных вариантов в poll (для quiz — всегда один).
    """
    info = _poll_map.get(poll_id)
    if not info:
        return
    if not option_ids:
        return
    poll_index = option_ids[0]
    order = info["option_order"]
    if poll_index < 0 or poll_index >= len(order):
        return
    option_id = order[poll_index]
    attempt = get_attempt(info["attempt_id"])
    if not attempt or attempt["status"] != "in_progress":
        return
    user_row = db.fetchone("SELECT id FROM users WHERE tg_id=?", (user_tg_id,))
    if not user_row or user_row["id"] != attempt["user_id"]:
        return
    await process_answer(bot, info["attempt_id"], info["question_id"],
                          option_id, info["chat_id"])

    # ── Защита приватных тестов: запоминаем msg_id для удаления после теста ──
    try:
        test = db.fetchone(
            "SELECT is_private FROM tests WHERE id=?", (attempt['test_id'],))
        if test and test.get('is_private'):
            # Сохраняем msg_id для последующего удаления
            attempt_id = info["attempt_id"]
            _private_poll_msgs.setdefault(attempt_id, []).append(
                (info["chat_id"], info["msg_id"]))
    except Exception:
        pass

    _poll_map.pop(poll_id, None)


async def pause_attempt(bot: Bot, attempt_id: int, chat_id: int) -> None:
    """Ставит тест на паузу."""
    db.execute(
        "UPDATE test_attempts SET status='paused', pause_time=? WHERE id=?",
        (now_iso(), attempt_id),
    )
    cancel_timer(attempt_id)
    attempt = get_attempt(attempt_id)
    if not attempt:
        return
    lang = attempt["language"] or "ru"
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=t("paused_personal", lang),
            reply_markup=pause_personal_kb(attempt_id, lang),
            protect_content=PROTECT_CONTENT,
        )
    except Exception:
        pass


async def resume_attempt(bot: Bot, attempt_id: int, chat_id: int) -> None:
    db.execute(
        "UPDATE test_attempts SET status='in_progress', pause_time=NULL, "
        "missed_questions_counter=0 WHERE id=?",
        (attempt_id,),
    )
    await send_current_question(bot, attempt_id, chat_id)


async def abort_attempt(bot: Bot, attempt_id: int, chat_id: int) -> None:
    """Завершить досрочно."""
    cancel_timer(attempt_id)
    db.execute(
        "UPDATE test_attempts SET status='aborted', end_time=? WHERE id=?",
        (now_iso(), attempt_id),
    )
    await finalize_attempt(bot, attempt_id, chat_id, aborted=True)


async def finalize_attempt(bot: Bot, attempt_id: int, chat_id: int,
                           aborted: bool = False) -> None:
    """Подсчёт и отправка результатов."""
    cancel_timer(attempt_id)
    attempt = get_attempt(attempt_id)
    if not attempt:
        return
    test = get_test(attempt["test_id"])
    if not test:
        return
    try:
        qids: list[int] = json.loads(attempt["question_order"] or "[]")
    except (ValueError, TypeError):
        qids = []
    total = len(qids)
    correct = attempt["correct_answers"]
    wrong = attempt["wrong_answers"]
    skipped = attempt["skipped_answers"]
    # Всё что не отвечено - в skipped (если abort посередине)
    answered_total = correct + wrong + skipped
    if answered_total < total:
        extra_skipped = total - answered_total
        skipped += extra_skipped
        db.execute(
            "UPDATE test_attempts SET skipped_answers=? WHERE id=?",
            (skipped, attempt_id),
        )

    percent = round((correct / total) * 100, 1) if total else 0.0
    score = correct  # 1 балл за вопрос для простоты; можно домножить на question.score

    status = "aborted" if aborted else "finished"
    db.execute(
        "UPDATE test_attempts SET status=?, end_time=?, score=? WHERE id=?",
        (status, now_iso(), score, attempt_id),
    )

    # === Сохраняем в test_statistics для лидерборда ===
    try:
        from services import group_quiz_service as _gqs
        user_row = db.fetchone("SELECT tg_id, username, first_name, last_name FROM users WHERE id=?",
                                (attempt['user_id'],))
        if user_row and (correct + wrong + skipped) > 0:
            # КРИТИЧНО: sqlite3.Row не имеет .get() — конвертируем в dict
            user_dict = dict(user_row)
            attempt_dict = dict(attempt)
            full_name = " ".join(filter(None, [
                user_dict.get('first_name') or '',
                user_dict.get('last_name') or ''
            ])).strip() or "Игрок"
            # Длительность в секундах
            duration_sec = 0
            if attempt_dict.get('start_time'):
                try:
                    from datetime import datetime as _dt
                    st = _dt.fromisoformat(attempt_dict['start_time'])
                    duration_sec = int((_dt.utcnow() - st).total_seconds())
                except Exception:
                    pass
            _gqs.save_private_attempt_to_statistics(
                test_id=test['id'],
                user_id=attempt_dict['user_id'],
                tg_id=user_dict.get('tg_id'),
                username=user_dict.get('username') or "",
                full_name=full_name,
                correct=correct,
                wrong=wrong,
                skipped=skipped,
                total_questions=total,
                total_time_seconds=duration_sec,
                started_at=attempt_dict.get('start_time') or now_iso(),
                finished_at=now_iso(),
            )
            logger.info("Сохранено в test_statistics: test_id=%s user_id=%s score=%s",
                         test['id'], attempt_dict['user_id'], correct)
    except Exception as e:
        logger.warning("Не удалось сохранить в test_statistics: %s", e, exc_info=True)

    lang = attempt["language"] or "ru"

    # Слабые темы
    weak = compute_weak_topics(attempt_id)
    if weak:
        weak_text = "\n".join(f"• {escape_html(w)}" for w in weak)
    else:
        weak_text = t("no_weak_topics", lang)

    level = percent_to_level(percent, lang)
    counted_label = t("attempt_counted", lang) if attempt["is_counted"] else t("attempt_not_counted", lang)
    result_text = t(
        "test_results", lang,
        correct=correct, wrong=wrong, skipped=skipped,
        score=correct, total=total, percent=percent,
        attempt_num=attempt["attempt_num"], counted=counted_label,
        level=level,
    )
    result_text += f"\n\n<b>{t('weak_topics_label', lang)}:</b>\n{weak_text}"

    # Кнопки в результате
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    rows = []
    # Сколько ошибок было
    wrong_count = db.fetchone(
        "SELECT COUNT(*) AS c FROM attempt_answers "
        "WHERE attempt_id=? AND is_correct=0 AND COALESCE(skipped,0)=0",
        (attempt_id,))
    n_wrong = (wrong_count['c'] if wrong_count else 0) or 0
    if not aborted and n_wrong > 0:
        rows.append([InlineKeyboardButton(
            text=f"🔁 Повторить ошибки ({n_wrong})",
            callback_data=f"redoerr:{attempt_id}")])
    # Поделиться
    if not aborted and not test.get('is_private'):
        share_query = f"share_{test['id']}_{correct}_{total}"
        rows.append([InlineKeyboardButton(
            text="📤 Поделиться результатом",
            switch_inline_query=share_query)])
    # Каталог тестов
    rows.append([InlineKeyboardButton(
        text="📚 Каталог тестов", callback_data="m:tests")])
    result_kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

    try:
        await bot.send_message(chat_id=chat_id, text=result_text, parse_mode="HTML",
                               reply_markup=result_kb,
                               protect_content=PROTECT_CONTENT)
    except Exception:
        try:
            await bot.send_message(chat_id=chat_id, text=result_text,
                                     parse_mode="HTML")
        except Exception:
            pass

    # Опционально: показать правильные ответы и объяснения
    if test["show_correct"] or test["show_explanation"]:
        await _send_answer_review(bot, chat_id, attempt_id, test, lang)

    # Обновляем стрик для daily, если это был daily-тест
    if test["test_type"] == "daily" and not aborted:
        try:
            from services.daily_service import update_streak_after_daily
            update_streak_after_daily(attempt["user_id"], percent)
        except Exception as e:
            logger.exception("update_streak error: %s", e)

    # ── Защита приватных тестов: удаляем все Quiz Poll через 5 минут после теста ──
    if test.get('is_private'):
        msgs_to_del = _private_poll_msgs.pop(attempt_id, [])
        if msgs_to_del:
            async def _delete_after_delay():
                try:
                    await asyncio.sleep(300)  # 5 минут
                    for chat_id_msg, msg_id in msgs_to_del:
                        try:
                            await bot.delete_message(chat_id_msg, msg_id)
                        except Exception:
                            pass
                except Exception:
                    pass
            asyncio.create_task(_delete_after_delay())

    # Если идёт анонс тестирования в чате — предложить перейти (после личного теста)
    if not aborted and not attempt.get("group_id"):
        try:
            from services import autopub_service as _aps
            ann = _aps.get_bot_announce()
            if ann and ann.get('invite'):
                from aiogram.types import (InlineKeyboardMarkup,
                                            InlineKeyboardButton)
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="🚀 Перейти к тестированию",
                        url=ann['invite'])
                ]])
                txt = ("📣 Сейчас идёт тестирование в чате!\n"
                       "Присоединяйся 👇" if lang == "ru"
                       else "📣 Қазір чатта тестілеу жүріп жатыр!\nҚосыл 👇")
                await bot.send_message(chat_id, txt, reply_markup=kb)
        except Exception as e:
            logger.warning("post-test announce offer: %s", e)


def compute_weak_topics(attempt_id: int) -> list[str]:
    """Возвращает темы, по которым процент правильных ниже 60%."""
    rows = db.fetchall(
        """SELECT q.topic, AVG(aa.is_correct) AS acc, COUNT(*) AS cnt
           FROM attempt_answers aa
           JOIN questions q ON aa.question_id = q.id
           WHERE aa.attempt_id=? AND q.topic <> ''
           GROUP BY q.topic
           HAVING cnt >= 2 AND acc < 0.6
           ORDER BY acc ASC""",
        (attempt_id,),
    )
    return [r["topic"] for r in rows]


async def _send_answer_review(bot: Bot, chat_id: int, attempt_id: int,
                              test: dict, lang: str) -> None:
    """Отправляет разбор по каждому вопросу (по одному сообщению на 5 вопросов)."""
    rows = db.fetchall(
        """SELECT q.id AS qid, q.text AS qtext, q.explanation,
                  qo.text AS user_opt, qo.is_correct AS user_correct,
                  (SELECT text FROM question_options WHERE question_id=q.id AND is_correct=1 LIMIT 1) AS correct_opt,
                  aa.skipped
           FROM attempt_answers aa
           JOIN questions q ON aa.question_id = q.id
           LEFT JOIN question_options qo ON aa.selected_option_id = qo.id
           WHERE aa.attempt_id=?
           ORDER BY aa.id""",
        (attempt_id,),
    )
    chunk: list[str] = []
    counter = 0
    for r in rows:
        counter += 1
        if r["skipped"]:
            mark = "⏱"
        elif r["user_correct"]:
            mark = "✅"
        else:
            mark = "❌"
        block = f"{mark} <b>{counter}.</b> {escape_html(r['qtext'])}"
        if test["show_correct"] and r["correct_opt"]:
            block += f"\n<b>{t('correct_answer_label', lang)}:</b> {escape_html(r['correct_opt'])}"
        if test["show_explanation"] and r["explanation"]:
            block += f"\n<i>{t('explanation', lang)}: {escape_html(r['explanation'])}</i>"
        chunk.append(block)
        if len(chunk) >= 5:
            try:
                await bot.send_message(chat_id=chat_id, text="\n\n".join(chunk),
                                       parse_mode="HTML", protect_content=PROTECT_CONTENT)
            except Exception:
                pass
            chunk = []
    if chunk:
        try:
            await bot.send_message(chat_id=chat_id, text="\n\n".join(chunk),
                                   parse_mode="HTML", protect_content=PROTECT_CONTENT)
        except Exception:
            pass
