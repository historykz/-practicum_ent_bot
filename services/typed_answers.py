"""
Типы вопросов конструктора и серверная проверка ответов на них.

Раньше в системе были только вопросы с вариантами: один правильный или
несколько (тип вычисляется по числу отмеченных правильных, см. answer_check).
Теперь добавлены три типа, у которых вариантов в таблице question_options нет:

  text      ввод ответа: любой из допустимых ответов (регистр и пробелы не важны);
            числа можно сверять с допуском
  matching  сопоставление: каждому элементу слева нужен свой элемент справа
  ordering  последовательность: элементы нужно выстроить в правильном порядке

Данные таких вопросов лежат в questions.payload (JSON), тип — в questions.q_type.
У старых вопросов q_type пустой, они работают как раньше.

Все проверки выполняются на сервере. Клиенту уходит только то, что нужно для
показа вопроса: правильные ответы он получает после ответа, и только если админ
включил показ правильных ответов.
"""
import json
import random
import re
from typing import Optional

SINGLE, MULTIPLE = "single", "multiple"
TEXT, MATCHING, ORDERING = "text", "matching", "ordering"

CHOICE_TYPES = (SINGLE, MULTIPLE)
TYPED_TYPES = (TEXT, MATCHING, ORDERING)
ALL_TYPES = CHOICE_TYPES + TYPED_TYPES

LABELS = {
    SINGLE: "Один правильный ответ",
    MULTIPLE: "Несколько правильных ответов",
    TEXT: "Ввод ответа",
    MATCHING: "Сопоставление",
    ORDERING: "Последовательность",
}

MAX_ITEMS = 12            # пар в сопоставлении и элементов в последовательности
MAX_TEXT_ANSWERS = 20
MAX_LEN = 300             # длина одного элемента


def is_typed(q_type: Optional[str]) -> bool:
    return (q_type or "") in TYPED_TYPES


def question_kind(q: dict) -> str:
    """Тип вопроса: q_type, а у старых вопросов — по числу правильных вариантов."""
    kind = (q.get("q_type") or "").strip().lower()
    if kind in ALL_TYPES:
        return kind
    return ""        # старый вопрос: тип определяет answer_check


def load_payload(q: dict) -> dict:
    raw = q.get("payload")
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


# ───────────────────────── нормализация ─────────────────────────

_SPACES = re.compile(r"\s+")


def norm_text(value, case_sensitive: bool = False) -> str:
    """Для сравнения: без лишних пробелов и краёв, ё как е, регистр не важен."""
    s = _SPACES.sub(" ", str(value if value is not None else "")).strip()
    s = s.replace("ё", "е").replace("Ё", "Е")
    return s if case_sensitive else s.casefold()


def _clip(value) -> str:
    return _SPACES.sub(" ", str(value if value is not None else "")).strip()[:MAX_LEN]


def _to_number(value) -> Optional[float]:
    s = _SPACES.sub("", str(value if value is not None else "")).replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


# ───────────────────────── проверка данных вопроса (для конструктора) ─────────────────────────

def clean_payload(q_type: str, payload: dict) -> tuple:
    """Привести данные вопроса к каноническому виду и проверить их.

    Возвращает (payload, ошибка). Ошибка — понятная автору строка или "".
    """
    payload = payload if isinstance(payload, dict) else {}
    if q_type == TEXT:
        answers, seen = [], set()
        for a in payload.get("answers") or []:
            a = _clip(a)
            key = norm_text(a, bool(payload.get("case_sensitive")))
            if a and key not in seen:
                seen.add(key)
                answers.append(a)
        if not answers:
            return {}, "Укажите хотя бы один допустимый ответ."
        if len(answers) > MAX_TEXT_ANSWERS:
            return {}, f"Допустимых ответов не больше {MAX_TEXT_ANSWERS}."
        out = {"answers": answers, "case_sensitive": bool(payload.get("case_sensitive")),
               "numeric": bool(payload.get("numeric"))}
        if out["numeric"]:
            for a in answers:
                if _to_number(a) is None:
                    return {}, f"Режим «число»: «{a}» не число."
            try:
                tol = float(str(payload.get("tolerance") or 0).replace(",", "."))
            except ValueError:
                return {}, "Допуск должен быть числом."
            if tol < 0:
                return {}, "Допуск не может быть отрицательным."
            out["tolerance"] = tol
        return out, ""

    if q_type == MATCHING:
        pairs = []
        for p in payload.get("pairs") or []:
            left = _clip(p.get("l") if isinstance(p, dict) else (p[0] if len(p) > 0 else ""))
            right = _clip(p.get("r") if isinstance(p, dict) else (p[1] if len(p) > 1 else ""))
            if left and right:
                pairs.append({"l": left, "r": right})
            elif left or right:
                return {}, "В каждой строке сопоставления нужны обе части: слева и справа."
        if len(pairs) < 2:
            return {}, "Для сопоставления нужно минимум две пары."
        if len(pairs) > MAX_ITEMS:
            return {}, f"Пар не больше {MAX_ITEMS}."
        lefts = [norm_text(p["l"]) for p in pairs]
        if len(set(lefts)) != len(lefts):
            return {}, "Элементы слева не должны повторяться."
        extra = []
        for e in payload.get("extra") or []:
            e = _clip(e)
            if e:
                extra.append(e)
        if len(pairs) + len(extra) > MAX_ITEMS + 4:
            return {}, "Слишком много элементов справа."
        return {"pairs": pairs, "extra": extra}, ""

    if q_type == ORDERING:
        items = [_clip(i) for i in (payload.get("items") or [])]
        items = [i for i in items if i]
        if len(items) < 2:
            return {}, "Для последовательности нужно минимум два элемента."
        if len(items) > MAX_ITEMS:
            return {}, f"Элементов не больше {MAX_ITEMS}."
        if len({norm_text(i) for i in items}) < 2:
            return {}, "Все элементы одинаковые: порядок нечем проверить."
        return {"items": items}, ""

    return {}, ""


# ───────────────────────── перемешивание (хранится в попытке) ─────────────────────────

def shuffled_order(q: dict, rng: Optional[random.Random] = None) -> list:
    """Порядок показа элементов вопроса. Хранится в попытке (options_order),
    чтобы при продолжении или переходе на другой интерфейс он не менялся."""
    rng = rng or random.Random()
    kind = question_kind(q)
    p = load_payload(q)
    if kind == MATCHING:
        n = len(p.get("pairs") or []) + len(p.get("extra") or [])
    elif kind == ORDERING:
        n = len(p.get("items") or [])
    else:
        return []
    order = list(range(n))
    if n < 2:
        return order
    for _ in range(20):
        rng.shuffle(order)
        # последовательность не должна показываться уже в правильном порядке
        if kind != ORDERING or order != sorted(order):
            break
    return order


def _stable_order(q: dict) -> list:
    return shuffled_order(q, random.Random(int(q.get("id") or 0) * 7919 + 13))


# ───────────────────────── что видит ученик ─────────────────────────

def client_question(q: dict, order: Optional[list] = None) -> dict:
    """Описание вопроса для страницы ученика. Правильных ответов здесь нет."""
    kind = question_kind(q)
    p = load_payload(q)
    out = {"id": q["id"], "text": q.get("text") or "", "type": kind or "choice",
           "web_image_path": q.get("web_image_path")}
    if kind == TEXT:
        out["numeric"] = bool(p.get("numeric"))
    elif kind == MATCHING:
        pairs, extra = p.get("pairs") or [], p.get("extra") or []
        rights = [x["r"] for x in pairs] + list(extra)
        order = [i for i in (order or []) if isinstance(i, int) and 0 <= i < len(rights)]
        if sorted(order) != list(range(len(rights))):
            order = _stable_order(q)
        out["left"] = [{"i": i, "text": x["l"]} for i, x in enumerate(pairs)]
        out["right"] = [{"i": i, "text": rights[i]} for i in order]
    elif kind == ORDERING:
        items = p.get("items") or []
        order = [i for i in (order or []) if isinstance(i, int) and 0 <= i < len(items)]
        if sorted(order) != list(range(len(items))):
            order = _stable_order(q)
        out["items"] = [{"i": i, "text": items[i]} for i in order]
    return out


def correct_view(q: dict) -> dict:
    """Правильный ответ в виде, удобном для показа после ответа."""
    kind = question_kind(q)
    p = load_payload(q)
    if kind == TEXT:
        return {"type": TEXT, "answers": p.get("answers") or []}
    if kind == MATCHING:
        return {"type": MATCHING, "pairs": [[x["l"], x["r"]] for x in p.get("pairs") or []]}
    if kind == ORDERING:
        return {"type": ORDERING, "items": p.get("items") or []}
    return {}


# ───────────────────────── проверка ─────────────────────────

def parse_answer(q: dict, raw) -> tuple:
    """Привести ответ клиента к безопасному виду. Возвращает (answer, пусто_ли)."""
    kind = question_kind(q)
    p = load_payload(q)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {"text": raw}
    if not isinstance(raw, dict):
        return {}, True
    if kind == TEXT:
        text = _clip(raw.get("text"))
        return {"text": text}, not text
    if kind == MATCHING:
        pairs, extra = p.get("pairs") or [], p.get("extra") or []
        total_right = len(pairs) + len(extra)
        mapping, src = {}, raw.get("map") if isinstance(raw.get("map"), dict) else {}
        for k, v in src.items():
            try:
                li, ri = int(k), int(v)
            except (TypeError, ValueError):
                continue
            if 0 <= li < len(pairs) and 0 <= ri < total_right:
                mapping[str(li)] = ri
        return {"map": mapping}, not mapping
    if kind == ORDERING:
        items = p.get("items") or []
        order, seen = [], set()
        for v in raw.get("order") or []:
            try:
                i = int(v)
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(items) and i not in seen:
                seen.add(i)
                order.append(i)
        return {"order": order}, not order
    return {}, True


def is_correct(q: dict, answer: dict) -> bool:
    kind = question_kind(q)
    p = load_payload(q)
    if kind == TEXT:
        text = (answer or {}).get("text") or ""
        if not text:
            return False
        accepted = p.get("answers") or []
        if p.get("numeric"):
            got = _to_number(text)
            if got is None:
                return False
            tol = float(p.get("tolerance") or 0)
            return any(abs(got - n) <= tol + 1e-12 for n in (_to_number(a) for a in accepted) if n is not None)
        cs = bool(p.get("case_sensitive"))
        return norm_text(text, cs) in {norm_text(a, cs) for a in accepted}
    if kind == MATCHING:
        pairs, extra = p.get("pairs") or [], p.get("extra") or []
        rights = [x["r"] for x in pairs] + list(extra)
        mapping = (answer or {}).get("map") or {}
        if len(mapping) != len(pairs):
            return False                    # надо сопоставить каждый элемент слева
        for li, pair in enumerate(pairs):
            ri = mapping.get(str(li))
            if ri is None or norm_text(rights[ri]) != norm_text(pair["r"]):
                return False
        return True
    if kind == ORDERING:
        items = p.get("items") or []
        order = (answer or {}).get("order") or []
        if len(order) != len(items):
            return False
        return [norm_text(items[i]) for i in order] == [norm_text(x) for x in items]
    return False


def points_of(q: dict) -> int:
    """Баллы за вопрос (questions.score); пусто или ноль считаем одним баллом."""
    try:
        v = int(q.get("score") or 1)
    except (TypeError, ValueError):
        return 1
    return v if v > 0 else 1
