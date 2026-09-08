"""
Антифлуд для групповых чатов.
- 5 сообщений за 7 секунд = флуд
- 3 одинаковых сообщения подряд = спам-повтор
Реакция по нарастающей: удалить+тихо → удалить+⚠️ → мут 10 минут.
Состояние в памяти (сбрасывается при рестарте — это норма).
"""
import time
import logging
from collections import defaultdict, deque

log = logging.getLogger(__name__)

# Настройки
FLOOD_COUNT = 5            # сообщений
FLOOD_WINDOW = 7          # за столько секунд
REPEAT_LIMIT = 3          # одинаковых подряд
MUTE_SECONDS = 600        # 10 минут
WARN_RESET = 120          # через сколько секунд тишины сбрасываются предупреждения

# (chat_id, user_id) -> deque[timestamps]
_msg_times: dict = defaultdict(lambda: deque(maxlen=FLOOD_COUNT + 2))
# (chat_id, user_id) -> [last_text, repeat_count]
_last_text: dict = defaultdict(lambda: ["", 0])
# (chat_id, user_id) -> [warns, last_warn_ts]
_warns: dict = defaultdict(lambda: [0, 0.0])


def _key(chat_id: int, user_id: int) -> tuple:
    return (chat_id, user_id)


def register_message(chat_id: int, user_id: int, text: str) -> dict:
    """
    Зафиксировать сообщение. Возвращает решение:
    {'violation': bool, 'reason': str, 'action': 'none'|'delete'|'warn'|'mute',
     'warns': int}
    """
    now = time.time()
    k = _key(chat_id, user_id)

    # Сброс предупреждений если давно молчал
    if _warns[k][1] and now - _warns[k][1] > WARN_RESET:
        _warns[k] = [0, 0.0]

    violation = False
    reason = ""

    # 1. Частота
    dq = _msg_times[k]
    dq.append(now)
    recent = [t for t in dq if now - t <= FLOOD_WINDOW]
    if len(recent) >= FLOOD_COUNT:
        violation = True
        reason = "flood"

    # 2. Повтор одинакового текста
    txt = (text or "").strip().lower()
    if txt:
        if _last_text[k][0] == txt:
            _last_text[k][1] += 1
        else:
            _last_text[k] = [txt, 1]
        if _last_text[k][1] >= REPEAT_LIMIT:
            violation = True
            reason = reason or "repeat"

    if not violation:
        return {'violation': False, 'reason': '', 'action': 'none',
                'warns': _warns[k][0]}

    # Нарушение — наращиваем предупреждения
    _warns[k][0] += 1
    _warns[k][1] = now
    w = _warns[k][0]
    # Сбрасываем счётчики чтобы не триггерить на каждом сообщении
    dq.clear()
    _last_text[k] = ["", 0]

    if w == 1:
        action = 'delete'        # тихо удалить
    elif w == 2:
        action = 'warn'          # удалить + публичное предупреждение
    else:
        action = 'mute'          # мут 10 минут
        _warns[k] = [0, 0.0]     # после мута сброс

    return {'violation': True, 'reason': reason, 'action': action, 'warns': w}


def reset(chat_id: int, user_id: int):
    k = _key(chat_id, user_id)
    _msg_times.pop(k, None)
    _last_text.pop(k, None)
    _warns.pop(k, None)
