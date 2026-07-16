"""Сервис дуэлей 1 на 1."""
import asyncio
import json
import random
import time
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

import config
import database as db
import utils
from locales import t
from keyboards import duel_options_kb


# Очередь ожидания: {subject_or_none: [(user_id, lang, joined_at, future)]}
_queue: list[dict] = []
_queue_lock = asyncio.Lock()

# Активные дуэли: {duel_id: {'p1': uid, 'p2': uid, 'questions': [...], 'idx': int,
#                            'sent_at': float, 'lang1': str, 'lang2': str,
#                            'chat1': int, 'chat2': int, 'task': asyncio.Task | None,
#                            'score1': int, 'score2': int}}
_active: dict[int, dict] = {}
_active_lock = asyncio.Lock()
# Маппинг poll_id -> duel_id (для обработки ответов через Quiz Poll)
_poll_to_duel: dict[str, int] = {}


async def join_queue(bot: Bot, user_id: int, chat_id: int, lang: str,
                     category_id=None) -> Optional[int]:
    """Поставить в очередь. Матчит по языку И разделу. Возвращает duel_id или None."""
    async with _queue_lock:
        for i, w in enumerate(_queue):
            if w['user_id'] == user_id:
                return None
            if w.get('lang') != lang:
                continue
            # Матчим по разделу: оба должны хотеть один раздел (или оба «все»)
            if w.get('category_id') != category_id:
                continue
            opponent = _queue.pop(i)
            duel_id = await _start_duel(bot, opponent['user_id'], opponent['chat_id'],
                                        opponent['lang'], user_id, chat_id, lang,
                                        category_id=category_id)
            return duel_id
        # Никого подходящего — добавляем себя
        _queue.append({
            'user_id': user_id,
            'chat_id': chat_id,
            'lang': lang,
            'category_id': category_id,
            'joined_at': time.time(),
        })
    return None


async def leave_queue(user_id: int) -> bool:
    async with _queue_lock:
        for i, w in enumerate(_queue):
            if w['user_id'] == user_id:
                _queue.pop(i)
                return True
    return False


# ===================== ДУЭЛЬ ПО ССЫЛКЕ (приглашение) =====================
# Комнаты: code -> {'host': uid, 'host_chat': chat, 'lang': str, 'created': ts, 'duel_id': None}
_invite_rooms: dict[str, dict] = {}
_invite_lock = asyncio.Lock()


def _gen_code() -> str:
    import secrets
    return secrets.token_hex(4)  # 8 символов


async def create_invite(user_tg_id: int, chat_id: int, lang: str,
                         category_id=None) -> str:
    """Создать приглашение в БД (переживает рестарт). Возвращает код."""
    # Удаляем старые незавершённые приглашения этого хоста
    db.execute(
        "DELETE FROM duel_invites WHERE host_tg_id=? AND status='waiting'",
        (user_tg_id,))
    code = _gen_code()
    db.execute(
        """INSERT INTO duel_invites
           (code, host_tg_id, host_chat_id, host_lang, category_id, status)
           VALUES (?,?,?,?,?, 'waiting')""",
        (code, user_tg_id, chat_id, lang, category_id))
    return code


async def join_invite(bot: Bot, code: str, user_tg_id: int, chat_id: int,
                       lang: str) -> str:
    """
    Присоединиться по коду (из БД).
    Статусы: 'started' | 'already_full' | 'not_found' | 'host_waiting' | 'self'
    """
    inv = db.fetchone("SELECT * FROM duel_invites WHERE code=?", (code,))
    if not inv or inv['status'] != 'waiting':
        return 'not_found'
    if inv['host_tg_id'] == user_tg_id:
        return 'host_waiting'
    if inv.get('guest_tg_id') or inv.get('duel_id'):
        return 'already_full'

    # Помечаем что гость зашёл
    db.execute(
        "UPDATE duel_invites SET guest_tg_id=?, status='started' WHERE code=?",
        (user_tg_id, code))

    host_u = utils.get_user_by_tg(inv['host_tg_id'])
    guest_u = utils.get_user_by_tg(user_tg_id)
    if not host_u or not guest_u:
        return 'not_found'

    duel_id = await _start_duel(
        bot, host_u['id'], inv['host_chat_id'], inv['host_lang'],
        guest_u['id'], chat_id, lang, category_id=inv.get('category_id'))
    db.execute("UPDATE duel_invites SET duel_id=? WHERE code=?",
               (duel_id, code))
    return 'started'


def cleanup_invite(code: str):
    db.execute("DELETE FROM duel_invites WHERE code=?", (code,))



def _pick_questions(count: int, lang: str = 'ru', category_id=None) -> list[int]:
    """
    Случайные вопросы из бесплатных НЕприватных активных тестов.
    Если задан category_id — только из тестов этого раздела.
    """
    cat_filter = ""
    params = [lang]
    if category_id is not None:
        cat_filter = "AND t.category_id=? "
        params.append(category_id)

    rows = db.fetchall(f"""
        SELECT q.id FROM questions q
        JOIN tests t ON t.id = q.test_id
        WHERE t.status='active' AND t.is_paid=0
          AND COALESCE(t.is_private,0)=0
          AND t.language=? {cat_filter}
    """, tuple(params))
    qids = [r['id'] for r in rows]
    if len(qids) < count:
        return qids
    return random.sample(qids, count)


def get_duel_categories(lang: str = 'ru') -> list:
    """Разделы где есть бесплатные доступные вопросы для дуэли."""
    rows = db.fetchall("""
        SELECT DISTINCT c.id, c.name, c.emoji,
               COUNT(q.id) AS qcount
        FROM test_categories c
        JOIN tests t ON t.category_id = c.id
        JOIN questions q ON q.test_id = t.id
        WHERE t.status='active' AND t.is_paid=0
          AND COALESCE(t.is_private,0)=0 AND t.language=?
        GROUP BY c.id, c.name, c.emoji
        HAVING qcount >= 1
        ORDER BY c.sort_order, c.name
    """, (lang,))
    return [dict(r) for r in rows]


async def _start_duel(bot: Bot, uid1: int, chat1: int, lang1: str,
                      uid2: int, chat2: int, lang2: str,
                      category_id=None) -> Optional[int]:
    qids = _pick_questions(config.DUEL_QUESTIONS_COUNT, lang1,
                           category_id=category_id)
    if not qids:
        try:
            await bot.send_message(chat1, t("duel_no_questions", lang1))
            await bot.send_message(chat2, t("duel_no_questions", lang2))
        except Exception:
            pass
        return None

    db.execute(
        """INSERT INTO duels (player1_id, player2_id, question_ids, status,
                              category_id, created_at)
           VALUES (?, ?, ?, 'active', ?, ?)""",
        (uid1, uid2, json.dumps(qids), category_id, utils.now_iso())
    )
    duel_id = db.fetchone("SELECT last_insert_rowid() AS id")['id']

    state = {
        'p1': uid1, 'p2': uid2,
        'chat1': chat1, 'chat2': chat2,
        'lang1': lang1, 'lang2': lang2,
        'questions': qids,
        'idx': 0,
        'sent_at': 0.0,
        'task': None,
        'score1': 0, 'score2': 0,
        'answered1': False, 'answered2': False,
    }
    async with _active_lock:
        _active[duel_id] = state

    # Получим имена
    u1 = utils.get_user_by_id(uid1)
    u2 = utils.get_user_by_id(uid2)
    name1 = utils.escape_html(u1['username'] or u1['first_name'] or str(uid1))
    name2 = utils.escape_html(u2['username'] or u2['first_name'] or str(uid2))

    try:
        await bot.send_message(chat1, t("duel_found", lang1, opponent=name2))
        await bot.send_message(chat2, t("duel_found", lang2, opponent=name1))
    except Exception:
        pass

    await asyncio.sleep(1.5)
    await _send_duel_question(bot, duel_id)
    return duel_id


async def _send_duel_question(bot: Bot, duel_id: int):
    state = _active.get(duel_id)
    if not state:
        return
    idx = state['idx']
    if idx >= len(state['questions']):
        await _finalize_duel(bot, duel_id)
        return

    qid = state['questions'][idx]
    q = db.fetchone("SELECT * FROM questions WHERE id=?", (qid,))
    if not q:
        state['idx'] += 1
        await _send_duel_question(bot, duel_id)
        return

    opts = db.fetchall("SELECT * FROM question_options WHERE question_id=? ORDER BY order_num", (qid,))
    options = [{'id': o['id'], 'text': o['text']} for o in opts]
    random.shuffle(options)
    # Индекс правильного варианта после перемешивания
    correct_idx = 0
    for i, o in enumerate(options):
        orig = db.fetchone("SELECT is_correct FROM question_options WHERE id=?", (o['id'],))
        if orig and orig.get('is_correct'):
            correct_idx = i
            break

    # Время из теста
    test_for_time = db.fetchone(
        "SELECT time_per_question FROM tests t JOIN questions q ON q.test_id=t.id WHERE q.id=?",
        (qid,))
    time_sec = (test_for_time['time_per_question']
                if test_for_time and test_for_time.get('time_per_question')
                else config.DUEL_TIME_PER_QUESTION)
    open_period = max(5, min(600, time_sec))

    state['answered1'] = False
    state['answered2'] = False
    state['current_options'] = options
    state['current_correct_idx'] = correct_idx
    state['sent_at'] = time.time()
    state['current_time_sec'] = time_sec

    # Заголовок вопроса
    header = f"Вопрос {idx+1}/{len(state['questions'])}"
    poll_question = f"{header}\n\n{q['text']}"[:300]
    poll_options = [o['text'][:100] for o in options]

    # Фото — отдельным сообщением ПЕРЕД poll (обоим игрокам)
    photo = q.get('photo_file_id') or q.get('image_file_id')
    state['poll_ids'] = {}
    state.setdefault('poll_msgs', [])  # (chat_id, message_id) для удаления
    for pkey, chat in (('p1', state['chat1']), ('p2', state['chat2'])):
        try:
            if photo:
                await bot.send_photo(chat, photo, caption="📷 Фото к вопросу",
                                     protect_content=config.PROTECT_CONTENT)
            # Кнопка завершить дуэль
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            end_kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🏳 Завершить дуэль",
                                      callback_data=f"duelquit:{duel_id}")]])
            msg = await bot.send_poll(
                chat_id=chat,
                question=poll_question,
                options=poll_options,
                type="quiz",
                correct_option_id=correct_idx,
                is_anonymous=False,
                open_period=open_period,
                explanation=(q.get('explanation') or "")[:200] or None,
                protect_content=config.PROTECT_CONTENT,
                reply_markup=end_kb,
            )
            state['poll_ids'][msg.poll.id] = pkey
            state['poll_msgs'].append((chat, msg.message_id))
            _poll_to_duel[msg.poll.id] = duel_id
        except (TelegramBadRequest, TelegramForbiddenError):
            await _finalize_duel(bot, duel_id, technical=True)
            return
        except Exception as e:
            log.warning("duel poll send: %s", e)

    # Таймер — по времени текущего вопроса
    if state.get('task'):
        try:
            state['task'].cancel()
        except Exception:
            pass
    state['task'] = asyncio.create_task(_duel_timeout(bot, duel_id, idx, open_period + 1))


async def _duel_timeout(bot: Bot, duel_id: int, idx: int, time_sec: int = None):
    try:
        await asyncio.sleep(time_sec or config.DUEL_TIME_PER_QUESTION)
    except asyncio.CancelledError:
        return
    state = _active.get(duel_id)
    if not state or state['idx'] != idx:
        return
    # Кто не ответил — 0 очков
    state['idx'] += 1
    try:
        if not state['answered1']:
            await bot.send_message(state['chat1'], t("duel_timeout", state['lang1']))
        if not state['answered2']:
            await bot.send_message(state['chat2'], t("duel_timeout", state['lang2']))
    except Exception:
        pass
    await _send_intermediate(bot, duel_id)
    await asyncio.sleep(1.0)
    await _send_duel_question(bot, duel_id)


async def process_duel_answer(bot: Bot, duel_id: int, user_id: int,
                              question_id: int, option_id: int) -> str:
    state = _active.get(duel_id)
    if not state:
        return 'old'
    # Проверим что это текущий вопрос
    if state['idx'] >= len(state['questions']):
        return 'old'
    if state['questions'][state['idx']] != question_id:
        return 'old'

    is_p1 = user_id == state['p1']
    is_p2 = user_id == state['p2']
    if not (is_p1 or is_p2):
        return 'invalid'
    if is_p1 and state['answered1']:
        return 'already'
    if is_p2 and state['answered2']:
        return 'already'

    # Проверим правильность
    opt = db.fetchone("SELECT is_correct FROM question_options WHERE id=? AND question_id=?",
                      (option_id, question_id))
    if not opt:
        return 'invalid'

    elapsed = time.time() - state['sent_at']
    speed_factor = max(0.0, 1.0 - elapsed / config.DUEL_TIME_PER_QUESTION)
    score = 0
    if opt['is_correct']:
        score = config.DUEL_SCORE_PER_QUESTION + int(config.DUEL_SPEED_BONUS_MAX * speed_factor)

    if is_p1:
        state['answered1'] = True
        state['score1'] += score
        db.execute("""INSERT INTO duel_answers (duel_id, user_id, question_id, selected_option_id,
                                                is_correct, response_time_ms, score, created_at)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                   (duel_id, user_id, question_id, option_id, opt['is_correct'],
                    int(elapsed * 1000), score, utils.now_iso()))
    else:
        state['answered2'] = True
        state['score2'] += score
        db.execute("""INSERT INTO duel_answers (duel_id, user_id, question_id, selected_option_id,
                                                is_correct, response_time_ms, score, created_at)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                   (duel_id, user_id, question_id, option_id, opt['is_correct'],
                    int(elapsed * 1000), score, utils.now_iso()))

    # Если оба ответили — переходим
    if state['answered1'] and state['answered2']:
        state['idx'] += 1
        if state.get('task'):
            try:
                state['task'].cancel()
            except Exception:
                pass
        await _send_intermediate(bot, duel_id)
        await asyncio.sleep(1.0)
        await _send_duel_question(bot, duel_id)

    return 'ok'


async def handle_poll_answer(bot: Bot, poll_id: str, user_tg_id: int,
                              option_ids: list) -> None:
    """
    Обработка ответа игрока через Quiz Poll.
    Определяет по poll_id какая дуэль, начисляет очки, ждёт второго.
    Кто первый правильно — получает бонус скорости.
    """
    duel_id = _poll_to_duel.get(poll_id)
    if duel_id is None:
        return
    state = _active.get(duel_id)
    if not state:
        return
    if not option_ids:
        return
    chosen_idx = option_ids[0]

    # Кто это?
    u = utils.get_user_by_tg(user_tg_id)
    if not u:
        return
    user_id = u['id']
    is_p1 = user_id == state['p1']
    is_p2 = user_id == state['p2']
    if not (is_p1 or is_p2):
        return
    if is_p1 and state.get('answered1'):
        return
    if is_p2 and state.get('answered2'):
        return

    correct_idx = state.get('current_correct_idx', 0)
    is_correct = (chosen_idx == correct_idx)
    elapsed = time.time() - state['sent_at']
    time_sec = state.get('current_time_sec') or config.DUEL_TIME_PER_QUESTION
    speed_factor = max(0.0, 1.0 - elapsed / time_sec)
    score = 0
    if is_correct:
        score = config.DUEL_SCORE_PER_QUESTION + int(config.DUEL_SPEED_BONUS_MAX * speed_factor)

    qid = state['questions'][state['idx']]
    if is_p1:
        state['answered1'] = True
        state['score1'] += score
    else:
        state['answered2'] = True
        state['score2'] += score
    try:
        db.execute("""INSERT INTO duel_answers (duel_id, user_id, question_id,
                        selected_option_id, is_correct, response_time_ms, score, created_at)
                      VALUES (?,?,?,?,?,?,?,?)""",
                   (duel_id, user_id, qid, chosen_idx, 1 if is_correct else 0,
                    int(elapsed * 1000), score, utils.now_iso()))
    except Exception:
        pass

    # Оба ответили — следующий вопрос
    if state.get('answered1') and state.get('answered2'):
        state['idx'] += 1
        if state.get('task'):
            try:
                state['task'].cancel()
            except Exception:
                pass
        await _send_intermediate(bot, duel_id)
        await asyncio.sleep(1.0)
        await _send_duel_question(bot, duel_id)


async def quit_duel(bot: Bot, duel_id: int, user_tg_id: int) -> bool:
    """Игрок сдаётся — дуэль завершается, второй объявляется победителем."""
    state = _active.get(duel_id)
    if not state:
        return False
    u = utils.get_user_by_tg(user_tg_id)
    if not u:
        return False
    # Тот кто вышел — проигрывает
    if u['id'] == state['p1']:
        state['score1'] = -1  # проигрыш
    elif u['id'] == state['p2']:
        state['score2'] = -1
    else:
        return False
    if state.get('task'):
        try:
            state['task'].cancel()
        except Exception:
            pass
    await _finalize_duel(bot, duel_id, quit_by=u['id'])
    return True


async def _send_intermediate(bot: Bot, duel_id: int):
    state = _active.get(duel_id)
    if not state:
        return
    try:
        msg1 = t("duel_intermediate", state['lang1'], score=state['score1'], opp_score=state['score2'])
        msg2 = t("duel_intermediate", state['lang2'], score=state['score2'], opp_score=state['score1'])
        await bot.send_message(state['chat1'], msg1)
        await bot.send_message(state['chat2'], msg2)
    except Exception:
        pass


async def _finalize_duel(bot: Bot, duel_id: int, technical: bool = False,
                          quit_by: int = None):
    state = _active.pop(duel_id, None)
    if not state:
        return
    if state.get('task'):
        try:
            state['task'].cancel()
        except Exception:
            pass
    # Чистим маппинг poll_id для этой дуэли
    for pid in list(_poll_to_duel.keys()):
        if _poll_to_duel.get(pid) == duel_id:
            _poll_to_duel.pop(pid, None)
    # Удаляем сообщения с вопросами-опросами (чат чистый)
    for chat, mid in state.get('poll_msgs', []):
        try:
            await bot.delete_message(chat, mid)
        except Exception:
            pass

    s1, s2 = state['score1'], state['score2']
    if s1 > s2:
        winner = state['p1']
    elif s2 > s1:
        winner = state['p2']
    else:
        winner = None

    db.execute("""UPDATE duels SET status='finished', score1=?, score2=?, winner_id=?,
                                   finished_at=? WHERE id=?""",
               (s1, s2, winner, utils.now_iso(), duel_id))

    # Запишем очки в test_attempts для общего рейтинга? Нет, оставим отдельно.
    # Отправим результат
    u1 = utils.get_user_by_id(state['p1'])
    u2 = utils.get_user_by_id(state['p2'])
    name1 = utils.escape_html(u1['username'] or u1['first_name'] or str(state['p1']))
    name2 = utils.escape_html(u2['username'] or u2['first_name'] or str(state['p2']))

    if winner is None:
        verdict1 = t("duel_draw", state['lang1'])
        verdict2 = t("duel_draw", state['lang2'])
    elif winner == state['p1']:
        verdict1 = t("duel_win", state['lang1'])
        verdict2 = t("duel_lose", state['lang2'])
    else:
        verdict1 = t("duel_lose", state['lang1'])
        verdict2 = t("duel_win", state['lang2'])

    try:
        await bot.send_message(state['chat1'],
            t("duel_result", state['lang1'], verdict=verdict1,
              your_score=s1, opp_score=s2, you=name1, opp=name2))
        await bot.send_message(state['chat2'],
            t("duel_result", state['lang2'], verdict=verdict2,
              your_score=s2, opp_score=s1, you=name2, opp=name1))
    except Exception:
        pass


async def get_active_duel_for(user_id: int) -> Optional[tuple[int, dict]]:
    for did, st in _active.items():
        if st['p1'] == user_id or st['p2'] == user_id:
            return did, st
    return None


async def abort_duel_by_user(bot: Bot, user_id: int):
    found = await get_active_duel_for(user_id)
    if not found:
        return
    duel_id, st = found
    # Противник побеждает технически
    await _finalize_duel(bot, duel_id, technical=True)


def get_duels_stats(user_id: int) -> dict:
    wins = db.fetchone("SELECT COUNT(*) AS c FROM duels WHERE winner_id=? AND status='finished'",
                       (user_id,))['c']
    total = db.fetchone("""SELECT COUNT(*) AS c FROM duels
                            WHERE (player1_id=? OR player2_id=?) AND status='finished'""",
                        (user_id, user_id))['c']
    losses = total - wins
    return {'wins': wins, 'losses': losses, 'total': total}
