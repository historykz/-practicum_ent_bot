"""
Сервис режимов «Карточки» и «Заучивание».
Доступ, цены, покупки прохождений, нормализация ответов, сессии, результаты.
"""
import json
import logging
import re
from difflib import SequenceMatcher

import database as db

log = logging.getLogger(__name__)

# Цены по умолчанию
DEFAULTS = {
    'fc_price_1': 5, 'fc_price_10': 10, 'fc_price_redo': 2,
    'ln_price_1': 5, 'ln_price_10': 10, 'ln_price_redo': 2,
}


# ===================== НАСТРОЙКИ РЕЖИМОВ =====================

def get_modes(test_id: int) -> dict:
    """Настройки режимов теста (создаёт дефолтные если нет)."""
    try:
        row = db.fetchone("SELECT * FROM test_modes WHERE test_id=?", (test_id,))
        if not row:
            db.execute("INSERT OR IGNORE INTO test_modes (test_id) VALUES (?)",
                        (test_id,))
            row = db.fetchone("SELECT * FROM test_modes WHERE test_id=?", (test_id,))
        return row
    except Exception:
        # Таблица ещё не создана — вернём дефолты
        return {'test_id': test_id, 'flashcards_enabled': 1,
                'learning_enabled': 1, 'is_free': 0,
                'fc_price_1': 5, 'fc_price_10': 10, 'fc_price_redo': 2,
                'ln_price_1': 5, 'ln_price_10': 10, 'ln_price_redo': 2}


def set_mode_enabled(test_id: int, mode: str, enabled: bool):
    get_modes(test_id)
    col = 'flashcards_enabled' if mode == 'flashcards' else 'learning_enabled'
    db.execute(f"UPDATE test_modes SET {col}=? WHERE test_id=?",
                (1 if enabled else 0, test_id))


def set_free(test_id: int, free: bool):
    get_modes(test_id)
    db.execute("UPDATE test_modes SET is_free=? WHERE test_id=?",
                (1 if free else 0, test_id))


def set_prices(test_id: int, mode: str, p1: int, p10: int, predo: int):
    get_modes(test_id)
    pref = 'fc' if mode == 'flashcards' else 'ln'
    db.execute(
        f"UPDATE test_modes SET {pref}_price_1=?, {pref}_price_10=?, "
        f"{pref}_price_redo=? WHERE test_id=?",
        (p1, p10, predo, test_id))


def price_for(test_id: int, mode: str, what: str) -> int:
    """what: '1' | '10' | 'redo'."""
    m = get_modes(test_id)
    pref = 'fc' if mode == 'flashcards' else 'ln'
    return m.get(f"{pref}_price_{what}") or DEFAULTS[f"{pref}_price_{what}"]


# ===================== ДОСТУП =====================

def is_mode_enabled(test_id: int, mode: str) -> bool:
    m = get_modes(test_id)
    col = 'flashcards_enabled' if mode == 'flashcards' else 'learning_enabled'
    return bool(m.get(col))


def free_access(test_id: int, user_tg_id: int, user_id: int) -> bool:
    """
    Бесплатный доступ к режиму:
    - админ
    - премиум
    - тест помечен 'бесплатные режимы' (is_free)
    """
    import utils
    if utils.is_admin(user_tg_id):
        return True
    try:
        if utils.is_premium(user_id):
            return True
    except Exception:
        pass
    m = get_modes(test_id)
    if m.get('is_free'):
        return True
    return False


# ===================== ПРОХОЖДЕНИЯ (ПАКЕТЫ) =====================

def get_passes(user_tg_id: int, test_id: int, mode: str) -> dict:
    row = db.fetchone(
        "SELECT * FROM mode_passes WHERE user_tg_id=? AND test_id=? AND mode=?",
        (user_tg_id, test_id, mode))
    if not row:
        return {'purchased': 0, 'used': 0, 'remaining': 0}
    rem = (row['purchased'] or 0) - (row['used'] or 0)
    return {'purchased': row['purchased'] or 0, 'used': row['used'] or 0,
            'remaining': max(0, rem)}


def remaining_passes(user_tg_id: int, test_id: int, mode: str) -> int:
    return get_passes(user_tg_id, test_id, mode)['remaining']


def add_passes(user_tg_id: int, test_id: int, mode: str, count: int,
               charge_id: str = None):
    """Начислить купленные прохождения."""
    row = db.fetchone(
        "SELECT * FROM mode_passes WHERE user_tg_id=? AND test_id=? AND mode=?",
        (user_tg_id, test_id, mode))
    if row:
        db.execute(
            "UPDATE mode_passes SET purchased=purchased+?, charge_id=? "
            "WHERE id=?", (count, charge_id, row['id']))
    else:
        db.execute(
            "INSERT INTO mode_passes (user_tg_id, test_id, mode, purchased, "
            "charge_id) VALUES (?,?,?,?,?)",
            (user_tg_id, test_id, mode, count, charge_id))


def use_one_pass(user_tg_id: int, test_id: int, mode: str) -> bool:
    """Списать одно прохождение. True если удалось."""
    row = db.fetchone(
        "SELECT * FROM mode_passes WHERE user_tg_id=? AND test_id=? AND mode=?",
        (user_tg_id, test_id, mode))
    if not row:
        return False
    rem = (row['purchased'] or 0) - (row['used'] or 0)
    if rem <= 0:
        return False
    db.execute("UPDATE mode_passes SET used=used+1 WHERE id=?", (row['id'],))
    return True


def grant_free_passes(user_tg_id: int, test_id: int, mode: str, count: int):
    """Админ выдаёт бесплатные прохождения."""
    add_passes(user_tg_id, test_id, mode, count, charge_id='admin_grant')


# ===================== НОРМАЛИЗАЦИЯ ОТВЕТА (Заучивание) =====================

def normalize_answer(text: str) -> str:
    """Нормализация для сравнения ответов."""
    if not text:
        return ""
    s = text.strip().lower()
    # ё → е
    s = s.replace('ё', 'е')
    # убрать букву варианта в начале: "c) ", "c. ", "c "
    s = re.sub(r'^[a-eа-е][\)\.]\s*', '', s)
    s = re.sub(r'^[a-eа-е]\s+', '', s)
    # кавычки
    s = s.replace('"', '').replace('«', '').replace('»', '').replace("'", '')
    # несколько пробелов → один
    s = re.sub(r'\s+', ' ', s)
    # убрать знаки в конце
    s = s.rstrip('.,:;!?')
    return s.strip()


def get_correct_answers(question: dict) -> list:
    """Список допустимых ответов: правильный вариант + accepted_answers."""
    answers = []
    # accepted_answers (JSON)
    acc = question.get('accepted_answers')
    if acc:
        try:
            extra = json.loads(acc)
            if isinstance(extra, list):
                answers.extend(extra)
        except Exception:
            pass
    # Правильный вариант из question_options
    opts = db.fetchall(
        "SELECT text, is_correct FROM question_options WHERE question_id=?",
        (question['id'],))
    for o in opts:
        if o.get('is_correct'):
            answers.append(o['text'])
    # Если вариантов нет — может правильный в самом вопросе (open)
    if not answers and question.get('correct_answer'):
        answers.append(question['correct_answer'])
    return [a for a in answers if a]


def check_answer(user_text: str, question: dict) -> dict:
    """
    Проверить ответ. Возвращает:
    {'correct': bool, 'close': bool, 'correct_text': str}
    close=True → похоже, но не точно (можно спросить подтверждение).
    """
    correct_list = get_correct_answers(question)
    correct_display = correct_list[0] if correct_list else "—"
    un = normalize_answer(user_text)
    if not un:
        return {'correct': False, 'close': False, 'correct_text': correct_display}

    norm_correct = [normalize_answer(c) for c in correct_list]

    # Точное совпадение после нормализации
    if un in norm_correct:
        return {'correct': True, 'close': False, 'correct_text': correct_display}

    # Fuzzy только для длинных ответов (не числа/формулы)
    for nc in norm_correct:
        if not nc:
            continue
        # короткие/числовые — строго
        if len(nc) <= 4 or nc.replace('.', '').replace(',', '').isdigit():
            continue
        ratio = SequenceMatcher(None, un, nc).ratio()
        if ratio >= 0.92:
            return {'correct': True, 'close': False,
                    'correct_text': correct_display}
        if ratio >= 0.82:
            return {'correct': False, 'close': True,
                    'correct_text': correct_display}

    return {'correct': False, 'close': False, 'correct_text': correct_display}


# ===================== ВОПРОСЫ ТЕСТА =====================

def get_question_ids(test_id: int, shuffle: bool = False) -> list:
    rows = db.fetchall(
        "SELECT id FROM questions WHERE test_id=? ORDER BY order_num, id",
        (test_id,))
    ids = [r['id'] for r in rows]
    if shuffle:
        import random
        random.shuffle(ids)
    return ids


# ===================== СТАТИСТИКА ДОХОДА =====================

def modes_revenue() -> dict:
    def total(sql):
        r = db.fetchone(sql)
        return (r['s'] if r and r['s'] else 0)
    fc = total("SELECT SUM(purchased) AS s FROM mode_passes WHERE mode='flashcards'")
    ln = total("SELECT SUM(purchased) AS s FROM mode_passes WHERE mode='learning'")
    return {'flashcards_passes': fc, 'learning_passes': ln}
