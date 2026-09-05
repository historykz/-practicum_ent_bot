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
from keyboards import pause_personal_kb, question_controls_kb
from locales import t
from utils import (

    escape_html,
    get_user_by_id,
    now_iso,
    percent_to_level,
)

logger = logging.getLogger(__name__)

# Активные таймеры: attempt_id -> asyncio.Task
_timers: dict[int, asyncio.Task] = {}
# Блокировки против гонки при быстром нажатии (attempt_id -> Lock)
_answer_locks: dict[int, asyncio.Lock] = {}
# Время последнего ответа (attempt_id -> timestamp) для задержки 1 сек
_last_answer_time: dict[int, float] = {}


def _get_answer_lock(attempt_id: int) -> asyncio.Lock:
    lk = _answer_locks.get(attempt_id)
    if lk is None:
        lk = asyncio.Lock()
        _answer_locks[attempt_id] = lk
    return lk
# Активные сообщения с вопросом: attempt_id -> (chat_id, message_id)
_active_messages: dict[int, tuple[int, int]] = {}
# Quiz Poll: poll_id -> {attempt_id, question_id, option_order}
# option_order — список option_id в том порядке, в котором показаны в poll
_poll_map: dict[str, dict] = {}
# attempt_id → [(chat_id, msg_id), ...] — для удаления Quiz Poll после завершения приватного теста
_private_poll_msgs: dict[int, list[tuple[int, int]]] = {}


def cancel_timer(attempt_id: int) -> None:
    """Отменить таймер вопроса.

    Сам таймер, дойдя до конца, двигает тест дальше и тоже попадает сюда —
    себя он не отменяет, иначе следующий вопрос не ушёл бы.
    """
    task = _timers.pop(attempt_id, None)
    if task and not task.done():
        try:
            if task is asyncio.current_task():
                return
        except RuntimeError:
            pass
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
        logger.warning("send math image: %s", e)
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
                logger.info("user %s banned until %s — block test start",
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


def get_ordered_options(attempt_id: int, question_id: int) -> list:
    """Варианты вопроса в том же порядке, в каком их видит ученик."""
    attempt = get_attempt(attempt_id)
    if not attempt:
        return []
    return _get_ordered_options(question_id, attempt)


# Лимиты Telegram Poll (Bot API): вопрос 1–300 символов, вариант 1–100, 2–10 вариантов
POLL_QUESTION_LIMIT = 300
POLL_OPTION_LIMIT = 100
POLL_MAX_OPTIONS = 10


def _poll_payload(question_text: str, options: list[dict], n: int, total: int,
                  lang: str) -> tuple[Optional[str], str, list[str], bool]:
    """Подгоняет вопрос под лимиты родного опроса Telegram.

    Возвращает (доп. сообщение HTML или None, текст вопроса для опроса,
    тексты вариантов для опроса, помечены_ли_варианты_буквами).

    Обычный вопрос целиком помещается в опрос. Если текст длиннее 300
    символов или какой-то вариант длиннее 100 — полный текст уходит обычным
    сообщением над опросом, а в опросе остаются короткие подписи «A) …».
    Выбор ответа в любом случае делается только внутри самого опроса.
    """
    qtext = (question_text or "").strip() or "…"
    texts = [(" ".join((o.get("text") or "").split())) or "—" for o in options]
    lettered = any(len(x) > POLL_OPTION_LIMIT for x in texts)
    long_question = len(qtext) > POLL_QUESTION_LIMIT

    extra = None
    if long_question or lettered:
        parts = [f"<b>{n}/{total}.</b> {escape_html(qtext)}"]
        if lettered:
            parts.append("\n".join(
                f"<b>{chr(ord('A') + i)})</b> {escape_html(x)}" for i, x in enumerate(texts)))
        extra = "\n\n".join(parts)
        if len(extra) > 4000:
            extra = extra[:3990] + "…"

    poll_q = t("poll_question_ref", lang, n=n, total=total) if long_question else qtext
    if lettered:
        poll_opts = []
        for i, x in enumerate(texts):
            prefix = f"{chr(ord('A') + i)}) "
            room = POLL_OPTION_LIMIT - len(prefix)
            poll_opts.append(prefix + (x if len(x) <= room else x[:room - 1] + "…"))
    else:
        poll_opts = texts
    return extra, poll_q, poll_opts, lettered


async def _stop_active_poll(bot: Optional[Bot], attempt_id: int,
                            question_id: Optional[int] = None,
                            close: bool = True) -> None:
    """Закрыть открытый опрос попытки (если есть) и забыть его.

    Один вопрос — один активный опрос: после ответа, таймаута, паузы или
    прерывания старый опрос голоса больше не принимает.
    """
    for poll_id, info in list(_poll_map.items()):
        if info.get("attempt_id") != attempt_id:
            continue
        if question_id is not None and info.get("question_id") != question_id:
            continue
        _poll_map.pop(poll_id, None)
        if close and bot is not None:
            try:
                await bot.stop_poll(info["chat_id"], info["msg_id"])
            except Exception:
                pass    # уже закрыт по open_period или удалён — не страшно


async def _safe_advance(bot: Bot, attempt_id: int, chat_id: int, coro_factory) -> None:
    """Обёртка над send_current_question/finalize_attempt: если где-то внутри
    (например, в самом первом db.execute до внутренней защиты) вылетит
    неожиданная ошибка — тест не зависает молча, юзер получает сообщение."""
    try:
        await coro_factory()
    except Exception as e:
        logger.exception("Сбой при переходе к следующему шагу теста attempt_id=%s: %s",
                          attempt_id, e)
        try:
            await bot.send_message(
                chat_id=chat_id,
                text="⚠️ Временный сбой. Попробуйте продолжить тест ещё раз "
                     "через «⏸ Приостановить» → «▶️ Продолжить», либо начните заново.",
            )
        except Exception:
            pass


async def send_current_question(bot: Bot, attempt_id: int, chat_id: int) -> None:
    """Отправляет текущий вопрос родным опросом Telegram и запускает таймер.

    Один правильный вариант → Quiz Poll: Telegram сам подсвечивает ответ.
    Несколько правильных → обычный Poll с allows_multiple_answers: ученик
    отмечает варианты и жмёт встроенную кнопку «Проголосовать», а бот сверяет
    набор целиком. Инлайн-кнопок для выбора ответа нет ни в одном случае.
    """
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
    options = _get_ordered_options(qid, attempt) if q else []
    if not q or not (2 <= len(options) <= POLL_MAX_OPTIONS):
        # Битый вопрос (удалён или число вариантов не влезает в опрос) — пропуск
        logger.warning("Вопрос %s пропущен: нет в базе или %s вариантов", qid, len(options))
        db.execute(
            "UPDATE test_attempts SET current_question_index=current_question_index+1 WHERE id=?",
            (attempt_id,),
        )
        await send_current_question(bot, attempt_id, chat_id)
        return

    # Старый опрос этой попытки, если вдруг остался открытым, закрываем:
    # одновременно может быть только один активный вопрос.
    await _stop_active_poll(bot, attempt_id)

    time_per_q = int(test["time_per_question"] or DEFAULT_TIME_PER_QUESTION)
    seconds = max(5, time_per_q)                      # Telegram: open_period ≥ 5 сек
    open_period = seconds if seconds <= 600 else None # дольше 600 — закроем сами
    lang = attempt["language"] or "ru"
    n, total = idx + 1, len(qids)

    correct_positions = [i for i, o in enumerate(options) if o.get("is_correct")]
    is_multi = len(correct_positions) != 1
    extra_text, poll_q, poll_opts, lettered = _poll_payload(
        q.get("text") or "", options, n, total, lang)
    protected = bool(test.get("is_private") or test.get("is_paid"))
    sent_ids: list[int] = []

    header = t("question_progress", lang, n=n, total=total, sec=seconds)
    try:
        m = await bot.send_message(
            chat_id=chat_id, text=header, parse_mode="HTML",
            reply_markup=question_controls_kb(attempt_id, qid, lang),
            protect_content=PROTECT_CONTENT)
        sent_ids.append(m.message_id)

        # Картинка: прикреплённая (с водяным знаком для защищённых тестов)
        # или авто-рендер формул
        photo = q.get("photo_file_id") or q.get("image_file_id")
        if photo:
            pm = None
            if protected:
                try:
                    from services import watermark_service as _wm
                    u_w = db.fetchone(
                        "SELECT tg_id, username FROM users WHERE id=?",
                        (attempt["user_id"],))
                    pm = await _wm.send_watermarked_photo(
                        bot, chat_id, photo,
                        (u_w.get("username") if u_w else "") or "",
                        (u_w.get("tg_id") if u_w else 0) or 0,
                        protect=PROTECT_CONTENT)
                except Exception:
                    pm = None
            if pm is None:
                try:
                    pm = await bot.send_photo(chat_id=chat_id, photo=photo,
                                              protect_content=PROTECT_CONTENT)
                except Exception:
                    pm = None
            if pm is not None:
                sent_ids.append(pm.message_id)
        else:
            await _maybe_render_math(bot, chat_id, q.get("text") or "")

        if extra_text:
            m = await bot.send_message(chat_id=chat_id, text=extra_text,
                                       parse_mode="HTML",
                                       protect_content=PROTECT_CONTENT)
            sent_ids.append(m.message_id)

        if is_multi:
            # Telegram Quiz Poll умеет только один правильный ответ, поэтому
            # для нескольких — обычный опрос с чекбоксами и кнопкой
            # «Проголосовать»; правильность проверяем сами (answer_check).
            poll_msg = await bot.send_poll(
                chat_id=chat_id,
                question=poll_q,
                options=poll_opts,
                type="regular",
                allows_multiple_answers=True,
                is_anonymous=False,
                open_period=open_period,
                protect_content=PROTECT_CONTENT,
            )
        else:
            poll_msg = await bot.send_poll(
                chat_id=chat_id,
                question=poll_q,
                options=poll_opts,
                type="quiz",
                correct_option_id=correct_positions[0],
                is_anonymous=False,
                open_period=open_period,
                explanation=(q.get("explanation") or "")[:200] or None,
                protect_content=PROTECT_CONTENT,
            )
    except TelegramForbiddenError as e:
        logger.warning("Не удалось отправить вопрос (бот заблокирован): %s", e)
        return
    except TelegramBadRequest as e:
        logger.warning("Telegram отклонил вопрос %s: %s", qid, e)
        try:
            await bot.send_message(
                chat_id=chat_id,
                text="⚠️ Не удалось показать вопрос. Тест поставлен на паузу — "
                     "нажмите «Продолжить», чтобы попробовать ещё раз.",
            )
        except Exception:
            pass
        await pause_attempt(bot, attempt_id, chat_id)
        return
    except Exception as e:
        # Раньше любая другая ошибка (сеть, задержка БД и т.п.) тут падала без
        # обработки — тест зависал навсегда без сообщения и без таймера.
        logger.exception("Неожиданная ошибка при отправке вопроса attempt_id=%s: %s", attempt_id, e)
        try:
            await bot.send_message(
                chat_id=chat_id,
                text="⚠️ Не удалось отправить следующий вопрос из-за временного сбоя. "
                     "Попробуйте продолжить тест ещё раз через «⏸ Приостановить» → «▶️ Продолжить», "
                     "либо начните тест заново.",
            )
        except Exception:
            pass
        return

    sent_ids.append(poll_msg.message_id)
    # Связь poll_id → попытка/вопрос. option_order — id вариантов в порядке
    # показа, чтобы индексы из poll_answer превратить обратно в option_id.
    _poll_map[poll_msg.poll.id] = {
        "attempt_id": attempt_id,
        "question_id": qid,
        "option_order": [o["id"] for o in options],
        "correct_ids": [options[i]["id"] for i in correct_positions],
        "labels": [chr(ord("A") + i) if lettered else poll_opts[i]
                   for i in range(len(options))],
        "is_multi": is_multi,
        "explanation": (q.get("explanation") or "").strip(),
        "protected": protected,
        "chat_id": chat_id,
        "msg_id": poll_msg.message_id,
        "sent_at": time.time(),
    }
    _active_messages[attempt_id] = (chat_id, poll_msg.message_id)
    if protected:
        # Защищённые тесты: все сообщения вопроса удалим после завершения
        _private_poll_msgs.setdefault(attempt_id, []).extend(
            (chat_id, mid) for mid in sent_ids)

    # Таймер (он же переводит к следующему вопросу по истечении времени).
    # Старый отменяем — на вопрос всегда ровно один таймер.
    cancel_timer(attempt_id)
    _timers[attempt_id] = asyncio.create_task(
        _question_timeout(bot, attempt_id, qid, chat_id, seconds)
    )


async def _question_timeout(bot: Bot, attempt_id: int, question_id: int,
                            chat_id: int, seconds: int) -> None:
    """Таймер на вопрос. По истечении — опрос закрывается, вопрос пропущен."""
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:
        return
    # Тот же замок, что и у ответа: голос, пришедший в последнюю секунду, и
    # таймаут не могут обработаться одновременно — сработает что-то одно.
    async with _get_answer_lock(attempt_id):
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
        # Закрываем опрос: голос после истечения времени не принимается
        await _stop_active_poll(bot, attempt_id, question_id)
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

        lang = attempt["language"] or "ru"
        try:
            await bot.send_message(chat_id=chat_id, text=t("question_skipped", lang),
                                   protect_content=PROTECT_CONTENT)
        except Exception:
            pass

        # Пауза после серии пропусков или сразу следующий вопрос
        attempt2 = get_attempt(attempt_id)
        if attempt2 and attempt2["missed_questions_counter"] >= MAX_PAUSE_MISS_COUNT:
            await pause_attempt(bot, attempt_id, chat_id)
            return
        await _safe_advance(bot, attempt_id, chat_id,
                            lambda: send_current_question(bot, attempt_id, chat_id))


async def process_answer(bot: Bot, attempt_id: int, question_id: int,
                        option_id, chat_id: int, after_record=None) -> str:
    """
    Обрабатывает ответ пользователя.

    option_id — один вариант или список отмеченных вариантов: ответ
    засчитывается только при полном совпадении с правильным набором.
    Возвращает короткий код: 'ok', 'already', 'invalid', 'old'.
    Защита от гонки: блокировка на время обработки одного ответа.
    Анти-дабл привязан к КОНКРЕТНОМУ вопросу (не блокирует следующий).
    """
    lock = _get_answer_lock(attempt_id)
    async with lock:
        # Анти-дабл только для ПОВТОРНОГО тапа по ТОМУ ЖЕ вопросу.
        # Ключ — (attempt_id, question_id), чтобы новый вопрос не блокировался.
        import time as _t
        now = _t.time()
        key = (attempt_id, question_id)
        last = _last_answer_time.get(key, 0)
        if now - last < 0.4:
            return "already"
        _last_answer_time[key] = now
        return await _process_answer_inner(bot, attempt_id, question_id,
                                            option_id, chat_id, after_record)


async def _process_answer_inner(bot: Bot, attempt_id: int, question_id: int,
                                 option_id, chat_id: int, after_record=None) -> str:
    attempt = get_attempt(attempt_id)
    if not attempt:
        return "old"
    if attempt["status"] == "idle":
        # Уборка брошенных попыток (services/stats_service.py) пометила эту
        # попытку «примолкшей» из-за долгой тишины — а человек вот прямо
        # сейчас отвечает. Тихо возвращаем её в работу, ничего не обнуляя:
        # для ученика это должно выглядеть так, будто паузы и не было.
        db.execute("UPDATE test_attempts SET status='in_progress' WHERE id=?",
                   (attempt_id,))
        attempt = dict(attempt)
        attempt["status"] = "in_progress"
    elif attempt["status"] != "in_progress":
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

    # Проверяем правильность. Ответ — это НАБОР отмеченных вариантов:
    # засчитываем, только если он совпал с правильным набором целиком.
    from services import answer_check as _ac
    chosen = _ac.belongs_to_question(question_id, option_id)
    if not chosen:
        return "invalid"
    is_correct = _ac.is_answer_correct(question_id, chosen)

    db.execute(
        "INSERT INTO attempt_answers (attempt_id, question_id, selected_option_id, "
        "selected_option_ids, is_correct) VALUES (?,?,?,?,?)",
        (attempt_id, question_id, chosen[0], _ac.dump(chosen),
         1 if is_correct else 0),
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
    # Что нужно сделать до перехода дальше (закрыть опрос, показать «верно/неверно»)
    if after_record is not None:
        try:
            await after_record(is_correct)
        except Exception as e:
            logger.warning("after_record attempt_id=%s: %s", attempt_id, e)
    # Следующий вопрос или финал
    attempt2 = get_attempt(attempt_id)
    if attempt2 and attempt2["current_question_index"] >= len(qids):
        await _safe_advance(bot, attempt_id, chat_id,
                            lambda: finalize_attempt(bot, attempt_id, chat_id))
    else:
        await _safe_advance(bot, attempt_id, chat_id,
                            lambda: send_current_question(bot, attempt_id, chat_id))
    return "ok"


async def process_poll_answer(bot: Bot, poll_id: str, option_ids: list[int],
                               user_tg_id: int) -> None:
    """Ответ из родного опроса Telegram (poll_answer).

    option_ids — индексы выбранных вариантов в опросе: для Quiz Poll один,
    для опроса с несколькими ответами — весь набор, который ученик отметил
    перед нажатием «Проголосовать». Пустой список — отзыв голоса, это ещё
    не ответ.
    """
    info = _poll_map.get(poll_id)
    if not info:
        return
    if not option_ids:
        return
    order = info["option_order"]
    chosen = [order[i] for i in option_ids
              if isinstance(i, int) and 0 <= i < len(order)]
    if not chosen:
        return
    attempt_id, question_id, chat_id = info["attempt_id"], info["question_id"], info["chat_id"]
    attempt = get_attempt(attempt_id)
    if not attempt or attempt["status"] not in ("in_progress", "idle"):
        return
    user_row = db.fetchone("SELECT id FROM users WHERE tg_id=?", (user_tg_id,))
    if not user_row or user_row["id"] != attempt["user_id"]:
        return
    lang = attempt["language"] or "ru"

    async def _after_record(is_correct: bool) -> None:
        # Ответ записан — этот опрос больше ничего не принимает
        _poll_map.pop(poll_id, None)
        if not info.get("is_multi"):
            return      # Quiz Poll: Telegram сам показал, верно или нет
        try:
            await bot.stop_poll(chat_id, info["msg_id"])
        except Exception:
            pass
        # Обычный опрос правильность не показывает — говорим сами
        if is_correct:
            text = t("multi_answer_correct", lang)
        else:
            right = set(info.get("correct_ids") or [])
            labels = [lab for oid, lab in zip(order, info.get("labels") or [])
                      if oid in right]
            text = t("multi_answer_wrong", lang,
                     answers=escape_html(", ".join(labels)) or "—")
        expl = info.get("explanation")
        if expl:
            text += f"\n💡 {escape_html(expl[:500])}"
        try:
            fm = await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                                        protect_content=PROTECT_CONTENT)
            if info.get("protected"):
                _private_poll_msgs.setdefault(attempt_id, []).append(
                    (chat_id, fm.message_id))
        except Exception:
            pass

    result = await process_answer(bot, attempt_id, question_id, chosen, chat_id,
                                  after_record=_after_record)
    if result != "ok":
        # 'already' / 'old': ответ уже учтён или вопрос сменился — повторное
        # событие Telegram второй раз не считаем и дальше не двигаем
        _poll_map.pop(poll_id, None)


async def pause_attempt(bot: Bot, attempt_id: int, chat_id: int) -> None:
    """Ставит тест на паузу."""
    db.execute(
        "UPDATE test_attempts SET status='paused', pause_time=? WHERE id=?",
        (now_iso(), attempt_id),
    )
    cancel_timer(attempt_id)
    await _stop_active_poll(bot, attempt_id)
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
    await _safe_advance(bot, attempt_id, chat_id,
                        lambda: send_current_question(bot, attempt_id, chat_id))


async def abort_attempt(bot: Bot, attempt_id: int, chat_id: int) -> None:
    """Завершить досрочно."""
    cancel_timer(attempt_id)
    await _stop_active_poll(bot, attempt_id)
    db.execute(
        "UPDATE test_attempts SET status='aborted', end_time=? WHERE id=?",
        (now_iso(), attempt_id),
    )
    await finalize_attempt(bot, attempt_id, chat_id, aborted=True)


async def finalize_attempt(bot: Bot, attempt_id: int, chat_id: int,
                           aborted: bool = False) -> None:
    """Подсчёт и отправка результатов."""
    cancel_timer(attempt_id)
    await _stop_active_poll(bot, attempt_id, close=False)
    # Чистим блокировки ответов (ключи вида (attempt_id, question_id))
    _answer_locks.pop(attempt_id, None)
    for k in list(_last_answer_time.keys()):
        if isinstance(k, tuple) and k[0] == attempt_id:
            _last_answer_time.pop(k, None)
        elif k == attempt_id:
            _last_answer_time.pop(k, None)
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

    # История попыток в скобках: (№1 - 8б) (№2 - 9б) ...
    try:
        past_attempts = db.fetchall(
            """SELECT attempt_num, correct_answers
               FROM test_attempts
               WHERE user_id=? AND test_id=? AND status='finished'
                 AND attempt_num < 999
               ORDER BY attempt_num""",
            (attempt['user_id'], attempt['test_id']))
        if len(past_attempts) > 1:
            parts = []
            for pa in past_attempts:
                parts.append(f"(№{pa['attempt_num']} - {pa['correct_answers']}б)")
            result_text += ("\n\n📈 <b>История попыток:</b>\n" + " ".join(parts))
    except Exception:
        pass

    # Шапка: кто прошёл (@username + id) и сколько повторов
    u_row = db.fetchone("SELECT tg_id, username FROM users WHERE id=?",
                         (attempt['user_id'],))
    redos_used = db.fetchone(
        "SELECT COUNT(*) AS c FROM test_attempts "
        "WHERE user_id=? AND test_id=? AND attempt_num=999",
        (attempt['user_id'], attempt['test_id']))
    n_redos = (redos_used['c'] if redos_used else 0) or 0
    if u_row and u_row.get('tg_id'):
        who = (f"@{u_row['username']}" if u_row.get('username') else "")
        result_text = (f"👤 {who} (id:{u_row['tg_id']})\n" + result_text
                       if who else
                       f"👤 id:{u_row['tg_id']}\n" + result_text)
    result_text += f"\n🔁 Повторов использовано: {n_redos}"

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
        if n_redos == 0:
            rows.append([InlineKeyboardButton(
                text=f"🔁 Повторить ошибки ({n_wrong}) — бесплатно",
                callback_data=f"redoerr:{attempt_id}")])
        else:
            from services import payment_service as _pms
            rows.append([InlineKeyboardButton(
                text=f"🔁 Повторить ошибки ({n_wrong}) — {_pms.REDO_PRICE_STARS} ⭐️",
                callback_data=f"buyredo:{attempt_id}")])
    # Поделиться (в т.ч. приватные — юзер сам решает делиться ли результатом)
    if not aborted:
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
    if test.get('is_private') or test.get('is_paid'):
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
                  (SELECT GROUP_CONCAT(text, ' / ') FROM question_options
                    WHERE question_id=q.id AND is_correct=1) AS correct_opt,
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
