"""
Сервис групповых квизов (QuizBot-style).

Поток:
1. start_lobby(test, chat, admin_tg) → отправляет стартовую карточку, лобби
2. join_player(group_quiz_id, user) → +1 в Готовые. При ≥2 — countdown_and_start
3. countdown_and_start → редактируем лобби, шлём 3..2..1, удаляем, отправляем 1-й вопрос
4. send_next_question → следующий Quiz Poll с open_period
5. on_poll_answer → засчитываем ответ
6. on_question_timeout → следующий вопрос или финал
7. finalize → лидерборд + сохранение в test_statistics
"""
import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import config
import database as db
from utils import now_iso, escape_html

logger = logging.getLogger(__name__)

# Конфиг
MIN_PLAYERS = 2
COUNTDOWN_SECONDS = 3
LOBBY_TIMEOUT_SECONDS = 10 * 60  # 10 минут

# Активные таймеры на следующий вопрос: group_quiz_id -> Task
_question_timers: dict[int, asyncio.Task] = {}
# Активные таймеры лобби (5-мин авто-отмена): group_quiz_id -> Task
_lobby_timers: dict[int, asyncio.Task] = {}
# Активные countdown-таски
_countdown_tasks: dict[int, asyncio.Task] = {}
# Карта poll_id -> group_quiz_id (для on_poll_answer)
_poll_to_gq: dict[str, int] = {}


# ============ ПУБЛИЧНЫЕ API ============

async def start_lobby(bot: Bot, test: dict, chat_id: int,
                      admin_tg_id: int, language: str = "ru") -> tuple[bool, str, Optional[int]]:
    """
    Возвращает (ok, message_or_error, group_quiz_id).
    """
    # Защита: один активный тест на группу
    existing = db.fetchone(
        "SELECT id FROM group_quizzes WHERE chat_id=? AND status IN ('lobby','running')",
        (chat_id,))
    if existing:
        return False, "already_running", None

    # Создаём сессию
    db.execute(
        """INSERT INTO group_quizzes (chat_id, test_id, started_by, status, language)
           VALUES (?,?,?,?,?)""",
        (chat_id, test['id'], admin_tg_id, 'lobby', language))
    gq_id = db.fetchone("SELECT last_insert_rowid() AS id")['id']

    # Считаем кол-во вопросов
    qcount_row = db.fetchone(
        "SELECT COUNT(*) AS c FROM questions WHERE test_id=?", (test['id'],))
    qcount = qcount_row['c'] if qcount_row else 0
    time_per_q = test.get('time_per_question') or 30

    author = config.SHARE_AUTHOR_LABEL or "—"
    title = escape_html(test.get('title') or '—')

    text = (
        f"🎲 <b>Приготовьтесь пройти тест «{title}»</b>\n\n"
        f"Автор: {escape_html(author)}\n"
        f"🖊 {qcount} вопросов\n"
        f"⏱ {time_per_q} секунд на вопрос\n\n"
        f"🏁 Вопросы появятся, когда хотя бы {MIN_PLAYERS} человека нажмут «Пройти тест».\n"
        f"💡 <b>Важно:</b> отвечать может каждый! Даже если не нажал «Пройти тест» — "
        f"твои ответы засчитаются, и ты попадёшь в топ-20.\n\n"
        f"Чтобы остановить тест, отправьте /stop\n\n"
        f"👥 <b>Готовы: 0/{MIN_PLAYERS}</b>"
    )
    kb = _lobby_kb(gq_id, 0)

    try:
        msg = await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.warning("Не удалось отправить стартовую карточку в %s: %s", chat_id, e)
        db.execute("UPDATE group_quizzes SET status='cancelled' WHERE id=?", (gq_id,))
        return False, str(e), None

    db.execute("UPDATE group_quizzes SET lobby_message_id=? WHERE id=?",
                (msg.message_id, gq_id))

    # Запускаем 5-минутный таймер на авто-отмену
    _lobby_timers[gq_id] = asyncio.create_task(_lobby_timeout(bot, gq_id))

    return True, "ok", gq_id


async def join_player(bot: Bot, group_quiz_id: int, user) -> tuple[bool, str]:
    """
    Игрок нажал «Пройти тест». user — aiogram User.
    Возвращает (ok, message_key).
    """
    gq = db.fetchone("SELECT * FROM group_quizzes WHERE id=?", (group_quiz_id,))
    if not gq:
        return False, "not_found"
    if gq['status'] != 'lobby':
        if gq['status'] == 'running':
            return False, "already_running"
        return False, "finished"

    # Уже в списке?
    existing = db.fetchone(
        "SELECT id FROM group_quiz_players WHERE group_quiz_id=? AND tg_id=?",
        (group_quiz_id, user.id))
    if existing:
        return False, "already_in"

    full_name = " ".join(filter(None, [user.first_name, user.last_name])) or "Игрок"
    db.execute(
        """INSERT INTO group_quiz_players (group_quiz_id, tg_id, username, full_name)
           VALUES (?,?,?,?)""",
        (group_quiz_id, user.id, user.username or "", full_name))

    # Обновим карточку лобби
    await _refresh_lobby_card(bot, group_quiz_id)

    # Проверяем достаточно ли игроков
    cnt = _count_players(group_quiz_id)
    if cnt >= MIN_PLAYERS and gq['status'] == 'lobby':
        # Перепроверим — вдруг countdown уже запущен
        cur = db.fetchone("SELECT status FROM group_quizzes WHERE id=?", (group_quiz_id,))
        if cur and cur['status'] == 'lobby' and group_quiz_id not in _countdown_tasks:
            _countdown_tasks[group_quiz_id] = asyncio.create_task(
                _countdown_and_start(bot, group_quiz_id))

    return True, "joined"


async def stop_quiz(bot: Bot, chat_id: int, requester_tg_id: int) -> tuple[bool, str]:
    """
    /stop — остановить тест в группе.
    Возвращает (ok, key).
    """
    gq = db.fetchone(
        "SELECT * FROM group_quizzes WHERE chat_id=? AND status IN ('lobby','running')",
        (chat_id,))
    if not gq:
        return False, "no_active"

    # Проверка прав: админ бота (хардкод + рантайм) или тот, кто запускал
    import utils as _utils
    is_admin_bot = _utils.is_admin(requester_tg_id)
    is_starter = gq['started_by'] == requester_tg_id

    can_stop = is_admin_bot or is_starter
    if not can_stop:
        # Запрос от имени канала/чата — requester_tg_id может быть None или ID канала.
        # Разрешаем если сообщение пришло из самого чата (sender_chat).
        # Проверим, админ ли в группе
        try:
            member = await bot.get_chat_member(chat_id, requester_tg_id)
            if member.status in ("creator", "administrator"):
                can_stop = True
        except Exception:
            pass

    if not can_stop:
        return False, "no_rights"

    await _cancel_quiz_timers(gq['id'])

    if gq['status'] == 'lobby':
        # Тест ещё не начался — просто отмена
        db.execute("UPDATE group_quizzes SET status='cancelled', finished_at=? WHERE id=?",
                    (now_iso(), gq['id']))
        try:
            await bot.send_message(chat_id, "⏹ <b>Тест отменён администратором.</b>",
                                    parse_mode="HTML")
        except Exception:
            pass
        return True, "cancelled"

    # Идёт тест — финализируем
    await _finalize(bot, gq['id'], aborted=True)
    return True, "stopped"


# ============ ВНУТРЕННИЕ ============

def _lobby_kb(gq_id: int, ready_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"▶️ Пройти тест ({ready_count})",
            callback_data=f"gq:join:{gq_id}",
        )
    ]])


def _count_players(gq_id: int) -> int:
    row = db.fetchone(
        "SELECT COUNT(*) AS c FROM group_quiz_players WHERE group_quiz_id=?", (gq_id,))
    return row['c'] if row else 0


async def _refresh_lobby_card(bot: Bot, gq_id: int) -> None:
    gq = db.fetchone("SELECT * FROM group_quizzes WHERE id=?", (gq_id,))
    if not gq or gq['status'] != 'lobby':
        return
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (gq['test_id'],))
    if not test:
        return
    qcount = db.fetchone(
        "SELECT COUNT(*) AS c FROM questions WHERE test_id=?", (test['id'],))['c']
    players = db.fetchall(
        "SELECT username, full_name FROM group_quiz_players WHERE group_quiz_id=? ORDER BY id",
        (gq_id,))
    cnt = len(players)
    title = escape_html(test['title'] or '—')
    author = escape_html(config.SHARE_AUTHOR_LABEL or "—")
    time_per_q = test.get('time_per_question') or 30

    players_list = ""
    if players:
        rendered = []
        for p in players[:10]:
            if p['username']:
                rendered.append(f"• @{p['username']}")
            else:
                rendered.append(f"• {escape_html(p['full_name'] or 'Игрок')}")
        players_list = "\n" + "\n".join(rendered)
        if len(players) > 10:
            players_list += f"\n• …ещё {len(players) - 10}"

    text = (
        f"🎲 <b>Приготовьтесь пройти тест «{title}»</b>\n\n"
        f"Автор: {author}\n"
        f"🖊 {qcount} вопросов\n"
        f"⏱ {time_per_q} секунд на вопрос\n"
        f"📄 Ответы видны участникам группы и автору теста\n\n"
        f"🏁 Вопросы появятся, когда хотя бы {MIN_PLAYERS} человека будут готовы. "
        f"Чтобы остановить — /stop\n\n"
        f"👥 <b>Готовы: {cnt}/{MIN_PLAYERS}</b>"
        f"{players_list}"
    )
    try:
        await bot.edit_message_text(
            text,
            chat_id=gq['chat_id'],
            message_id=gq['lobby_message_id'],
            reply_markup=_lobby_kb(gq_id, cnt),
            parse_mode="HTML",
        )
    except Exception:
        pass


async def _lobby_timeout(bot: Bot, gq_id: int):
    """Если за 10 мин не набралось игроков — отмена + открыть чат."""
    try:
        await asyncio.sleep(LOBBY_TIMEOUT_SECONDS)
        gq = db.fetchone("SELECT * FROM group_quizzes WHERE id=?", (gq_id,))
        if not gq or gq['status'] != 'lobby':
            return
        db.execute("UPDATE group_quizzes SET status='cancelled', finished_at=? WHERE id=?",
                    (now_iso(), gq_id))
        try:
            await bot.delete_message(gq['chat_id'], gq['lobby_message_id'])
        except Exception:
            pass
        try:
            await bot.send_message(
                gq['chat_id'],
                f"😴 Тест отменён — за {LOBBY_TIMEOUT_SECONDS // 60} мин не набралось "
                f"≥{MIN_PLAYERS} игроков.")
        except Exception:
            pass
        # Открываем чат если был закрыт сериями + двигаем серию
        try:
            from services import autopub_service
            await autopub_service.on_series_test_finished(
                bot, gq['test_id'], gq['chat_id'])
        except Exception as e:
            logger.warning("cancel open chat: %s", e)
    except asyncio.CancelledError:
        pass
    finally:
        _lobby_timers.pop(gq_id, None)


async def _cancel_quiz_timers(gq_id: int):
    for d in (_lobby_timers, _question_timers, _countdown_tasks):
        task = d.pop(gq_id, None)
        if task and not task.done():
            task.cancel()


async def _countdown_and_start(bot: Bot, gq_id: int):
    """Обратный отсчёт и старт первого вопроса."""
    try:
        gq = db.fetchone("SELECT * FROM group_quizzes WHERE id=?", (gq_id,))
        if not gq or gq['status'] != 'lobby':
            return

        # Отменяем lobby timeout
        lobby_task = _lobby_timers.pop(gq_id, None)
        if lobby_task and not lobby_task.done():
            lobby_task.cancel()

        chat_id = gq['chat_id']

        # Шлём countdown
        countdown_msgs = []
        for n in range(COUNTDOWN_SECONDS, 0, -1):
            try:
                m = await bot.send_message(chat_id, f"⏳ <b>{n}...</b>", parse_mode="HTML")
                countdown_msgs.append(m.message_id)
            except Exception:
                break
            await asyncio.sleep(1)

        # Удаляем лобби и countdown
        try:
            await bot.delete_message(chat_id, gq['lobby_message_id'])
        except Exception:
            pass
        for mid in countdown_msgs:
            try:
                await bot.delete_message(chat_id, mid)
            except Exception:
                pass

        # Сообщение о старте
        try:
            await bot.send_message(
                chat_id,
                f"🚀 <b>Тест начался!</b> Удачи всем участникам.",
                parse_mode="HTML")
        except Exception:
            pass

        # Переводим в running, обнуляем индекс
        db.execute(
            "UPDATE group_quizzes SET status='running', started_at=?, current_question_index=0 WHERE id=?",
            (now_iso(), gq_id))

        await _send_question(bot, gq_id)
    except asyncio.CancelledError:
        pass
    finally:
        _countdown_tasks.pop(gq_id, None)


def _list_question_ids(test_id: int) -> list[int]:
    rows = db.fetchall(
        "SELECT id FROM questions WHERE test_id=? ORDER BY COALESCE(order_num, id)",
        (test_id,))
    return [r['id'] for r in rows]


async def _send_question(bot: Bot, gq_id: int):
    """Отправить текущий вопрос как Quiz Poll."""
    gq = db.fetchone("SELECT * FROM group_quizzes WHERE id=?", (gq_id,))
    if not gq or gq['status'] != 'running':
        return

    test = db.fetchone("SELECT * FROM tests WHERE id=?", (gq['test_id'],))
    if not test:
        return

    qids = _list_question_ids(test['id'])
    idx = gq['current_question_index']

    if idx >= len(qids):
        await _finalize(bot, gq_id, aborted=False)
        return

    qid = qids[idx]
    question = db.fetchone("SELECT * FROM questions WHERE id=?", (qid,))
    if not question:
        # Пропуск битого вопроса
        db.execute(
            "UPDATE group_quizzes SET current_question_index=current_question_index+1 WHERE id=?",
            (gq_id,))
        await _send_question(bot, gq_id)
        return

    options_rows = db.fetchall(
        "SELECT id, text, is_correct FROM question_options WHERE question_id=? "
        "ORDER BY COALESCE(order_num, id)",
        (qid,))
    options = [o['text'] for o in options_rows]
    correct_idx = next((i for i, o in enumerate(options_rows) if o['is_correct']), 0)

    time_per_q = test['time_per_question'] or 30
    open_period = max(5, min(600, time_per_q))

    total = len(qids)
    qtext = (question['text'] or "")[:290]
    if len(qtext) > 290:
        qtext = qtext[:287] + "..."

    poll_question = f"[{idx + 1}/{total}] {qtext}"
    if len(poll_question) > 300:
        poll_question = poll_question[:297] + "..."

    # Telegram лимиты: вопрос ≤ 300, варианты ≤ 100
    if any(len(o) > 100 for o in options) or not (2 <= len(options) <= 10):
        # Этот вопрос нельзя отправить как Quiz Poll — пропускаем
        try:
            await bot.send_message(
                gq['chat_id'],
                f"⚠️ Вопрос {idx + 1}/{total} пропущен (не подходит под формат Quiz Poll).")
        except Exception:
            pass
        db.execute(
            "UPDATE group_quizzes SET current_question_index=current_question_index+1 WHERE id=?",
            (gq_id,))
        await _send_question(bot, gq_id)
        return

    try:
        msg = await bot.send_poll(
            chat_id=gq['chat_id'],
            question=poll_question,
            options=options,
            type="quiz",
            correct_option_id=correct_idx,
            is_anonymous=False,
            open_period=open_period,
            explanation=(question.get('explanation') or "")[:200] or None,
        )
    except Exception as e:
        logger.warning("Не удалось отправить poll: %s", e)
        # Пропускаем вопрос
        db.execute(
            "UPDATE group_quizzes SET current_question_index=current_question_index+1 WHERE id=?",
            (gq_id,))
        await _send_question(bot, gq_id)
        return

    poll_id = msg.poll.id
    _poll_to_gq[poll_id] = gq_id

    db.execute(
        """UPDATE group_quizzes SET
              current_poll_id=?,
              current_poll_message_id=?,
              current_poll_correct_index=?,
              current_poll_options=?,
              current_question_started_at=?
           WHERE id=?""",
        (poll_id, msg.message_id, correct_idx,
         json.dumps(options, ensure_ascii=False),
         now_iso(), gq_id))

    # Запускаем таймер на следующий вопрос
    existing = _question_timers.pop(gq_id, None)
    if existing and not existing.done():
        existing.cancel()
    _question_timers[gq_id] = asyncio.create_task(
        _question_timeout(bot, gq_id, open_period + 1))


async def _question_timeout(bot: Bot, gq_id: int, sleep_seconds: int):
    """После open_period шлём следующий вопрос."""
    try:
        await asyncio.sleep(sleep_seconds)
        gq = db.fetchone("SELECT * FROM group_quizzes WHERE id=?", (gq_id,))
        if not gq or gq['status'] != 'running':
            return
        # Чистим маппинг старого poll
        if gq['current_poll_id']:
            _poll_to_gq.pop(gq['current_poll_id'], None)
        # Следующий
        db.execute(
            "UPDATE group_quizzes SET current_question_index=current_question_index+1 WHERE id=?",
            (gq_id,))
        await _send_question(bot, gq_id)
    except asyncio.CancelledError:
        pass
    finally:
        _question_timers.pop(gq_id, None)


async def on_poll_answer(bot: Bot, poll_id: str, option_ids: list[int],
                          user) -> None:
    """
    Обработка poll_answer для групповых квизов.
    Игрок засчитывается АВТОМАТИЧЕСКИ при первом ответе,
    даже если не нажимал «Пройти тест».
    """
    gq_id = _poll_to_gq.get(poll_id)
    if not gq_id:
        return  # это не групповой
    gq = db.fetchone("SELECT * FROM group_quizzes WHERE id=?", (gq_id,))
    if not gq or gq['status'] != 'running':
        return
    if not option_ids:
        # Сняли голос — игнор
        return

    # Игрок зарегистрирован? Если нет — регистрируем на лету
    player = db.fetchone(
        "SELECT * FROM group_quiz_players WHERE group_quiz_id=? AND tg_id=?",
        (gq_id, user.id))
    if not player:
        # Авто-регистрация на лету с правильными полями
        fname = getattr(user, 'first_name', None) or ''
        lname = getattr(user, 'last_name', None) or ''
        full_name = " ".join(filter(None, [fname, lname])) or "Игрок"
        uname = getattr(user, 'username', None) or ""
        try:
            db.execute(
                "INSERT INTO group_quiz_players "
                "(group_quiz_id, tg_id, username, full_name) VALUES (?,?,?,?)",
                (gq_id, user.id, uname, full_name))
        except Exception as e:
            logger.warning("auto-register player: %s", e)
        player = db.fetchone(
            "SELECT * FROM group_quiz_players WHERE group_quiz_id=? AND tg_id=?",
            (gq_id, user.id))
        if not player:
            return

    chosen = option_ids[0]
    correct_idx = gq['current_poll_correct_index']

    # Время ответа
    answer_time = 0
    if gq['current_question_started_at']:
        try:
            started = datetime.fromisoformat(gq['current_question_started_at'])
            answer_time = int((datetime.utcnow() - started).total_seconds())
        except Exception:
            pass

    if chosen == correct_idx:
        db.execute(
            "UPDATE group_quiz_players SET correct_answers=correct_answers+1, "
            "total_time_seconds=total_time_seconds+? WHERE id=?",
            (answer_time, player['id']))
    else:
        db.execute(
            "UPDATE group_quiz_players SET wrong_answers=wrong_answers+1, "
            "total_time_seconds=total_time_seconds+? WHERE id=?",
            (answer_time, player['id']))


async def _finalize(bot: Bot, gq_id: int, aborted: bool = False):
    """Завершить групповой тест: лидерборд + сохранение в test_statistics."""
    gq = db.fetchone("SELECT * FROM group_quizzes WHERE id=?", (gq_id,))
    if not gq or gq['status'] == 'finished':
        return

    # Чистим таймеры
    await _cancel_quiz_timers(gq_id)
    if gq['current_poll_id']:
        _poll_to_gq.pop(gq['current_poll_id'], None)

    db.execute(
        "UPDATE group_quizzes SET status='finished', finished_at=? WHERE id=?",
        (now_iso(), gq_id))

    test = db.fetchone("SELECT * FROM tests WHERE id=?", (gq['test_id'],))
    title = escape_html((test['title'] if test else '—'))
    qids_count = db.fetchone(
        "SELECT COUNT(*) AS c FROM questions WHERE test_id=?", (gq['test_id'],))['c']

    # Считаем skipped для всех игроков (qids - answered)
    answered_count = gq['current_question_index']
    players = db.fetchall(
        """SELECT * FROM group_quiz_players
           WHERE group_quiz_id=?
           ORDER BY correct_answers DESC, total_time_seconds ASC""",
        (gq_id,))

    # Записываем skipped
    for p in players:
        ans = p['correct_answers'] + p['wrong_answers']
        skipped = max(0, answered_count - ans)
        if skipped != p['skipped_answers']:
            db.execute("UPDATE group_quiz_players SET skipped_answers=? WHERE id=?",
                        (skipped, p['id']))

    # Сохраняем в test_statistics
    _save_to_statistics(gq, players, qids_count, source_type='group')

    # Лидерборд
    text = _build_leaderboard_text(title, qids_count, players, aborted=aborted)
    kb = _final_kb(test['id'] if test else None)

    try:
        await bot.send_message(gq['chat_id'], text, reply_markup=kb,
                                parse_mode="HTML",
                                disable_web_page_preview=True)
    except Exception as e:
        logger.warning("Не удалось отправить лидерборд: %s", e)

    # Хук: уведомляем автопубликацию что тест из серии завершён —
    # чтобы СРАЗУ запустить следующий тест серии или открыть чат.
    try:
        from services import autopub_service
        await autopub_service.on_series_test_finished(bot, gq['test_id'],
                                                       gq['chat_id'])
    except Exception as e:
        logger.warning("autopub hook: %s", e)


def _build_leaderboard_text(title: str, qcount: int, players: list[dict],
                              aborted: bool = False, limit: int = 20) -> str:
    lines = []
    if aborted:
        lines.append(f"⏹ Тест <b>«{title}»</b> остановлен.")
    else:
        lines.append(f"🏁 Тест <b>«{title}»</b> завершён!")
    lines.append("")
    lines.append(f"📚 {qcount} вопросов · 👥 Участников: {len(players)}")
    lines.append("")

    medals = ["🥇", "🥈", "🥉"]
    for i, p in enumerate(players[:limit]):
        if i < 3:
            prefix = medals[i]
        else:
            prefix = f"{i + 1}."
        name = ("@" + p['username']) if p['username'] else (p['full_name'] or 'Игрок')
        name = escape_html(name)
        time_str = _format_time(p['total_time_seconds'])
        lines.append(f"{prefix} {name} — <b>{p['correct_answers']}</b> ({time_str})")

    lines.append("")
    if players:
        lines.append("🏆 <b>Поздравляем победителей!</b>")
    else:
        lines.append("Никто не успел ответить.")
    return "\n".join(lines)


def _final_kb(test_id: Optional[int]) -> InlineKeyboardMarkup:
    bu = config.BOT_USERNAME or "bot"
    rows = []
    if test_id:
        rows.append([InlineKeyboardButton(
            text="▶️ Пройти тест в ЛС",
            url=f"https://t.me/{bu}?start=test_{test_id}",
        )])
        rows.append([InlineKeyboardButton(
            text="📤 Поделиться тестом",
            switch_inline_query=f"test:{test_id}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_time(seconds: int) -> str:
    if not seconds or seconds <= 0:
        return "0 сек"
    mins, secs = divmod(int(seconds), 60)
    if mins > 0:
        return f"{mins} мин {secs} сек"
    return f"{secs} сек"


def _save_to_statistics(gq: dict, players: list[dict], qcount: int, source_type: str):
    """Сохраняет результат каждого игрока в test_statistics."""
    for p in players:
        user_row = db.fetchone("SELECT id FROM users WHERE tg_id=?", (p['tg_id'],))
        if not user_row:
            continue
        user_id = user_row['id']
        total_answered = p['correct_answers'] + p['wrong_answers'] + p['skipped_answers']
        if total_answered == 0:
            continue  # не учитываем тех, кто не ответил
        percentage = round(p['correct_answers'] * 100 / qcount, 1) if qcount else 0
        avg_time = round(p['total_time_seconds'] / total_answered, 2) if total_answered else 0

        # is_first_attempt: первая ли это попытка по этому тесту?
        prev = db.fetchone(
            "SELECT id FROM test_statistics WHERE test_id=? AND user_id=?",
            (gq['test_id'], user_id))
        is_first = 0 if prev else 1

        db.execute(
            """INSERT INTO test_statistics
                (test_id, user_id, tg_id, username, full_name, score,
                 total_questions, correct_answers, wrong_answers, skipped_answers,
                 percentage, total_time_seconds, average_answer_time,
                 source_type, group_chat_id, group_quiz_id,
                 started_at, finished_at, is_first_attempt)
               VALUES (?,?,?,?,?,?, ?,?,?,?, ?,?,?, ?,?,?, ?,?,?)""",
            (gq['test_id'], user_id, p['tg_id'], p['username'], p['full_name'],
             p['correct_answers'],
             qcount, p['correct_answers'], p['wrong_answers'], p['skipped_answers'],
             percentage, p['total_time_seconds'], avg_time,
             source_type, gq['chat_id'], gq['id'],
             gq['started_at'], gq['finished_at'] or now_iso(), is_first))


def save_private_attempt_to_statistics(test_id: int, user_id: int, tg_id: int,
                                         username: str, full_name: str,
                                         correct: int, wrong: int, skipped: int,
                                         total_questions: int,
                                         total_time_seconds: int,
                                         started_at: str, finished_at: str):
    """Внешний API: сохранить результат личного прохождения."""
    if (correct + wrong + skipped) == 0:
        return
    percentage = round(correct * 100 / total_questions, 1) if total_questions else 0
    avg_time = round(total_time_seconds / max(1, correct + wrong + skipped), 2)
    prev = db.fetchone(
        "SELECT id FROM test_statistics WHERE test_id=? AND user_id=?",
        (test_id, user_id))
    is_first = 0 if prev else 1
    db.execute(
        """INSERT INTO test_statistics
            (test_id, user_id, tg_id, username, full_name, score,
             total_questions, correct_answers, wrong_answers, skipped_answers,
             percentage, total_time_seconds, average_answer_time,
             source_type, started_at, finished_at, is_first_attempt)
           VALUES (?,?,?,?,?,?, ?,?,?,?, ?,?,?, ?,?,?,?)""",
        (test_id, user_id, tg_id, username or "", full_name or "",
         correct,
         total_questions, correct, wrong, skipped,
         percentage, total_time_seconds, avg_time,
         'private', started_at, finished_at, is_first))


# ============ ПАГИНАЦИЯ ЛИДЕРБОРДА ============

PAGE_SIZE = 20


def get_leaderboard_page(test_id: int, page: int = 1) -> tuple[list[dict], int, int]:
    """
    Возвращает (rows, total_users, total_pages).
    Только first_attempt, отсортировано по score DESC, total_time ASC.
    """
    total_row = db.fetchone(
        "SELECT COUNT(*) AS c FROM test_statistics "
        "WHERE test_id=? AND is_first_attempt=1",
        (test_id,))
    total = total_row['c'] if total_row else 0
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * PAGE_SIZE

    rows = db.fetchall(
        """SELECT username, full_name, score, total_time_seconds, finished_at
           FROM test_statistics
           WHERE test_id=? AND is_first_attempt=1
           ORDER BY score DESC, total_time_seconds ASC
           LIMIT ? OFFSET ?""",
        (test_id, PAGE_SIZE, offset))
    return [dict(r) for r in rows], total, total_pages


def build_stats_text(test: dict, page: int = 1) -> tuple[str, InlineKeyboardMarkup]:
    """Собирает текст и клавиатуру для страницы статистики."""
    rows, total_users, total_pages = get_leaderboard_page(test['id'], page)
    qcount_row = db.fetchone(
        "SELECT COUNT(*) AS c FROM questions WHERE test_id=?", (test['id'],))
    qcount = qcount_row['c'] if qcount_row else 0

    title = escape_html(test.get('title') or '—')
    time_per_q = test.get('time_per_question') or 30

    lines = [
        f"🏆 <b>Список лучших результатов для теста «{title}»</b>",
        "",
        f"🖊 {qcount} вопросов",
        f"⏱ {time_per_q} секунд на вопрос",
        f"🤓 тест прошли {total_users} человек",
        "",
    ]

    medals = ["🥇", "🥈", "🥉"]
    offset = (page - 1) * PAGE_SIZE
    for i, r in enumerate(rows):
        rank = offset + i + 1
        if rank <= 3:
            prefix = medals[rank - 1]
        else:
            prefix = f"{rank}."
        name = ("@" + r['username']) if r['username'] else (r['full_name'] or 'Игрок')
        name = escape_html(name)
        t_str = _format_time(r['total_time_seconds'])
        lines.append(f"{prefix} {name} — <b>{r['score']}</b> ({t_str})")

    if not rows:
        lines.append("<i>Пока никто не проходил тест.</i>")

    text = "\n".join(lines)

    # Пагинация
    kb_rows = []
    if total_pages > 1:
        nav = _build_pagination_buttons(test['id'], page, total_pages)
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton(
        text="« К тесту", callback_data=f"opentest:{test['id']}")])
    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)


def _build_pagination_buttons(test_id: int, page: int, total: int) -> list[InlineKeyboardButton]:
    """Pagination: <prev> 1 ·2· 3 4 last>"""
    buttons = []
    # Стрелка влево
    if page > 1:
        buttons.append(InlineKeyboardButton(
            text="«", callback_data=f"stats:{test_id}:{page - 1}"))
    # Номера страниц — показываем максимум 5
    if total <= 5:
        page_nums = list(range(1, total + 1))
    else:
        if page <= 3:
            page_nums = [1, 2, 3, 4, 5]
        elif page >= total - 2:
            page_nums = list(range(total - 4, total + 1))
        else:
            page_nums = [page - 2, page - 1, page, page + 1, page + 2]

    for p in page_nums:
        text = f"·{p}·" if p == page else str(p)
        buttons.append(InlineKeyboardButton(
            text=text, callback_data=f"stats:{test_id}:{p}"))

    if page < total:
        buttons.append(InlineKeyboardButton(
            text="»", callback_data=f"stats:{test_id}:{total}"))
    return buttons
