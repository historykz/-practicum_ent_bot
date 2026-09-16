"""
Проверка ответа на вопрос — одна на все места: бот, Mini App и Live.

Главная мысль: правильных вариантов может быть сколько угодно, и ученик об
этом заранее не знает. Он сам решает, сколько вариантов отметить, а система
сравнивает его набор с сохранённым набором целиком.

Ответ засчитывается, только если совпало ВСЁ: выбраны все правильные и ни
одного лишнего. Отметить наугад все варианты не получится.

Тип вопроса (один правильный или несколько) — внутреннее знание системы.
Наружу, ученику, он никогда не показывается.
"""
import json
import logging
from typing import Iterable, Optional

import database as db

log = logging.getLogger(__name__)

SINGLE = "single_choice"
MULTIPLE = "multiple_choice"


def correct_option_ids(question_id: int) -> list:
    """ID правильных вариантов вопроса, по порядку показа."""
    rows = db.fetchall(
        "SELECT id FROM question_options WHERE question_id=? AND is_correct=1 "
        "ORDER BY order_num, id", (question_id,))
    return [r["id"] for r in rows]


def question_type(question_id: int) -> str:
    """Тип вопроса вычисляем по числу правильных вариантов.

    Отдельного поля в базе нет и не нужно: сколько вариантов отмечено
    звёздочкой при импорте, столько правильных и сохранено. Это значение
    нужно только админке — ученику тип не показывается.
    """
    return MULTIPLE if len(correct_option_ids(question_id)) > 1 else SINGLE


def normalize(selected) -> list:
    """Приводит выбор ученика к списку чисел без повторов и пустых значений."""
    if selected is None:
        return []
    if isinstance(selected, (str, bytes)):
        try:
            selected = json.loads(selected)
        except (ValueError, TypeError):
            selected = [selected]
    if isinstance(selected, (int, float)):
        selected = [selected]
    out, seen = [], set()
    for item in (selected or []):
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def is_answer_correct(question_id: int, selected) -> bool:
    """Совпал ли набор ученика с правильным набором — полностью.

    Частичный ответ не засчитывается: выбрал два правильных из трёх — ошибка.
    Лишний вариант рядом с правильными — тоже ошибка.
    """
    chosen = set(normalize(selected))
    if not chosen:
        return False
    right = set(correct_option_ids(question_id))
    if not right:
        return False              # у вопроса не отмечен ни один правильный
    return chosen == right


def belongs_to_question(question_id: int, selected) -> list:
    """Оставляет только те варианты, которые действительно принадлежат вопросу.

    Список приходит от клиента, а клиенту верить нельзя: подставленный чужой
    id варианта не должен ни засчитаться, ни попасть в базу.
    """
    chosen = normalize(selected)
    if not chosen:
        return []
    rows = db.fetchall(
        "SELECT id FROM question_options WHERE question_id=?", (question_id,))
    own = {r["id"] for r in rows}
    return [oid for oid in chosen if oid in own]


def dump(selected) -> Optional[str]:
    """Набор вариантов для хранения в базе (JSON)."""
    ids = normalize(selected)
    return json.dumps(ids) if ids else None


def load(raw) -> list:
    """Обратное преобразование — из базы в список."""
    return normalize(raw)


def answer_letters(question_id: int, ids: Iterable = None) -> str:
    """Буквы вариантов (A, C, D) — для админки и разборов.

    Ученику это не показывается до подтверждения ответа.
    """
    rows = db.fetchall(
        "SELECT id FROM question_options WHERE question_id=? ORDER BY order_num, id",
        (question_id,))
    order = [r["id"] for r in rows]
    target = list(ids) if ids is not None else correct_option_ids(question_id)
    letters = []
    for oid in target:
        if oid in order:
            letters.append(chr(ord("A") + order.index(oid)))
    return ", ".join(letters)
