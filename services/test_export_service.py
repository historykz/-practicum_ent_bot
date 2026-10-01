"""
Экспорт/импорт тестов в JSON и txt файлы.
"""
import json
import io
import re
import logging
from typing import Optional

import database as db

log = logging.getLogger(__name__)


def export_test_to_dict(test_id: int) -> Optional[dict]:
    """Собрать полную структуру теста для экспорта."""
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))
    if not test:
        return None

    questions = db.fetchall(
        "SELECT * FROM questions WHERE test_id=? ORDER BY order_num, id",
        (test_id,))

    out = {
        "test": {
            "title": test.get('title') or '',
            "description": test.get('description') or '',
            "language": test.get('language') or 'ru',
            "time_per_question": test.get('time_per_question') or 30,
            "is_paid": bool(test.get('is_paid')),
            "price": test.get('price') or 0,
            "test_type": test.get('test_type') or 'regular',
        },
        "questions": []
    }
    for q in questions:
        options = db.fetchall(
            "SELECT * FROM question_options WHERE question_id=? ORDER BY order_num, id",
            (q['id'],))
        correct_idx = 0
        for i, opt in enumerate(options):
            if opt.get('is_correct'):
                correct_idx = i
                break
        out["questions"].append({
            "text": q.get('text') or '',
            "explanation": q.get('explanation') or '',
            "options": [opt.get('text') or '' for opt in options],
            "correct_index": correct_idx,
        })
    return out


def export_test_to_json_bytes(test_id: int) -> Optional[bytes]:
    data = export_test_to_dict(test_id)
    if not data:
        return None
    return json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')


def export_test_to_txt_bytes(test_id: int) -> Optional[bytes]:
    data = export_test_to_dict(test_id)
    if not data:
        return None
    lines = []
    test = data['test']
    lines.append(f"ТЕСТ: {test['title']}")
    lines.append(f"Описание: {test['description']}")
    lines.append(f"Язык: {test['language']}")
    lines.append(f"Время на вопрос: {test['time_per_question']} сек")
    lines.append(f"Тип: {test['test_type']}")
    lines.append(f"Платный: {'да' if test['is_paid'] else 'нет'}")
    if test['is_paid']:
        lines.append(f"Цена: {test['price']}")
    lines.append("")
    lines.append("=" * 60)
    lines.append(f"ВОПРОСЫ ({len(data['questions'])}):")
    lines.append("=" * 60)
    lines.append("")
    for i, q in enumerate(data['questions'], start=1):
        lines.append(f"{i}. {q['text']}")
        for j, opt in enumerate(q['options']):
            mark = "✓" if j == q['correct_index'] else "·"
            lines.append(f"   {mark} {chr(ord('A') + j)}) {opt}")
        if q.get('explanation'):
            lines.append(f"   💡 Объяснение: {q['explanation']}")
        lines.append("")
    return "\n".join(lines).encode('utf-8')


def import_test_from_dict(data: dict, created_by_tg: int) -> tuple[Optional[int], str]:
    """Создаёт новый тест из dict-структуры. Вернёт (test_id, сообщение)."""
    try:
        t = data.get('test') or {}
        title = (t.get('title') or '').strip()
        if not title:
            return None, "В файле нет названия теста (поле 'title')."
        questions = data.get('questions') or []
        if not questions:
            return None, "В файле нет вопросов."

        # Создаём тест
        cur = db.execute("""
            INSERT INTO tests (title, description, language, time_per_question,
                                is_paid, price, test_type, status, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
        """, (
            title,
            t.get('description') or '',
            t.get('language') or 'ru',
            int(t.get('time_per_question') or 30),
            1 if t.get('is_paid') else 0,
            int(t.get('price') or 0),
            t.get('test_type') or 'regular',
            created_by_tg,
        ))
        test_id = cur.lastrowid

        # Добавляем вопросы
        added = 0
        skipped = 0
        for i, q in enumerate(questions):
            text = (q.get('text') or '').strip()
            options = q.get('options') or []
            if not text or len(options) < 2:
                skipped += 1
                continue
            correct_idx = int(q.get('correct_index') or 0)
            if correct_idx >= len(options):
                correct_idx = 0
            qcur = db.execute("""
                INSERT INTO questions (test_id, text, explanation, order_num,
                                        source_type)
                VALUES (?, ?, ?, ?, 'imported')
            """, (test_id, text, q.get('explanation') or '', i))
            qid = qcur.lastrowid
            for j, opt_text in enumerate(options):
                db.execute("""
                    INSERT INTO question_options (question_id, text, is_correct, order_num)
                    VALUES (?, ?, ?, ?)
                """, (qid, str(opt_text), 1 if j == correct_idx else 0, j))
            added += 1

        msg = f"✅ Тест создан (ID: {test_id}). Добавлено {added} вопросов."
        if skipped:
            msg += f" Пропущено: {skipped}."
        return test_id, msg
    except Exception as e:
        log.exception("import test: %s", e)
        return None, f"Ошибка импорта: {e}"


def import_test_from_json_bytes(content: bytes, created_by_tg: int) -> tuple[Optional[int], str]:
    try:
        data = json.loads(content.decode('utf-8'))
    except Exception as e:
        return None, f"Некорректный JSON: {e}"
    return import_test_from_dict(data, created_by_tg)


# ==================== ФОРМАТ СО ЗВЁЗДОЧКОЙ ====================
# Каждый вопрос:
# 1. Текст вопроса
# A) вариант
# B) вариант *
# C) вариант
# D) вариант

def export_test_to_star_bytes(test_id: int) -> Optional[bytes]:
    """Экспорт в простой текстовый формат со звёздочкой возле правильного."""
    data = export_test_to_dict(test_id)
    if not data:
        return None
    lines = []
    for i, q in enumerate(data['questions'], start=1):
        # Чистый текст вопроса (без переносов)
        q_text = (q['text'] or '').strip()
        lines.append(f"{i}. {q_text}")
        for j, opt in enumerate(q['options']):
            letter = chr(ord('A') + j)
            mark = " *" if j == q['correct_index'] else ""
            # один вариант на строке
            opt_clean = str(opt).strip().replace('\n', ' ')
            lines.append(f"{letter}) {opt_clean}{mark}")
        lines.append("")  # пустая строка-разделитель
    # В конце по требованию
    lines.append("* — правильный ответ")
    return "\n".join(lines).encode('utf-8')


def parse_star_format(text: str) -> list[dict]:
    """
    Парсит текст в формате:
        1. Вопрос?
        A) вариант
        B) вариант *
        C) вариант
    Вернёт список dict: {text, options, correct_index}.
    Звёздочка может стоять как 'B) текст *', 'B) текст*', '*B) текст'.
    """
    # Убираем технические строки в конце
    text = text.replace('\r', '')
    # Удалим легендарную строку «* — правильный ответ»
    text = re.sub(r'^\s*\*\s*[—\-]\s*правильный\s*ответ\s*$',
                   '', text, flags=re.IGNORECASE | re.MULTILINE)

    questions = []
    cur_q = None  # dict с накопленными данными

    lines = text.split('\n')
    # Регулярки
    re_qstart = re.compile(r'^\s*(\d+)[\.\)]\s+(.*)$')
    re_opt = re.compile(r'^\s*\*?\s*([A-ZА-Я])[\)\.]\s*(.+?)\s*\*?\s*$')

    def flush():
        nonlocal cur_q
        if cur_q and cur_q['text'] and len(cur_q['options']) >= 2:
            # Если правильный не отмечен — берём первый
            if cur_q.get('correct_index') is None:
                cur_q['correct_index'] = 0
            questions.append(cur_q)
        cur_q = None

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        m_q = re_qstart.match(line)
        if m_q:
            flush()
            cur_q = {"text": m_q.group(2).strip(), "options": [],
                     "correct_index": None, "explanation": ""}
            continue
        m_opt = re_opt.match(line)
        if m_opt and cur_q is not None:
            letter = m_opt.group(1).upper()
            opt_text = m_opt.group(2).strip()
            # Был ли *  в исходной строке?
            has_star = '*' in line
            if has_star:
                # Уберём звёздочку из текста (на всякий)
                opt_text = opt_text.replace('*', '').strip()
                cur_q['correct_index'] = len(cur_q['options'])
            cur_q['options'].append(opt_text)
            continue
        # Если строка не похожа ни на что — игнорим
    flush()
    return questions


def import_test_from_star_text(text: str, created_by_tg: int,
                                 title: str = None,
                                 language: str = 'ru',
                                 time_per_question: int = 30) -> tuple[Optional[int], str]:
    """Импорт теста из текстового формата со звёздочкой."""
    qs_parsed = parse_star_format(text or "")
    if not qs_parsed:
        return None, "Не нашёл ни одного вопроса в файле."
    data = {
        "test": {
            "title": title or "Импортированный тест",
            "description": "",
            "language": language,
            "time_per_question": time_per_question,
            "is_paid": False,
            "price": 0,
            "test_type": "regular",
        },
        "questions": qs_parsed,
    }
    return import_test_from_dict(data, created_by_tg)


def append_questions_to_test(test_id: int, qs_parsed: list[dict]) -> tuple[int, int]:
    """Дописать вопросы в существующий тест. Вернёт (added, skipped)."""
    # Узнаём текущий max order_num
    row = db.fetchone(
        "SELECT COALESCE(MAX(order_num), -1) AS m FROM questions WHERE test_id=?",
        (test_id,))
    start_order = (row['m'] if row else -1) + 1
    added = 0
    skipped = 0
    for offset, q in enumerate(qs_parsed):
        text = (q.get('text') or '').strip()
        options = q.get('options') or []
        if not text or len(options) < 2:
            skipped += 1
            continue
        correct_idx = int(q.get('correct_index') or 0)
        if correct_idx >= len(options):
            correct_idx = 0
        qcur = db.execute("""
            INSERT INTO questions (test_id, text, explanation, order_num, source_type)
            VALUES (?, ?, ?, ?, 'imported')
        """, (test_id, text, q.get('explanation') or '', start_order + offset))
        qid = qcur.lastrowid
        for j, opt_text in enumerate(options):
            db.execute("""
                INSERT INTO question_options (question_id, text, is_correct, order_num)
                VALUES (?, ?, ?, ?)
            """, (qid, str(opt_text), 1 if j == correct_idx else 0, j))
        added += 1
    return added, skipped
