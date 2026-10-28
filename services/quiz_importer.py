"""
Сервис импорта Quiz Poll.

ВАЖНО:
Telegram Bot API при пересылке Quiz Poll НЕ гарантирует, что в Poll-объекте
будет correct_option_id. Если правильный ответ не доступен:
1) Сохраняем вопрос как черновик (question_drafts).
2) Просим админа выбрать правильный ответ вручную.

Если correct_option_id есть - сразу создаём вопрос в questions.
"""
import json
import logging
from typing import Optional

from aiogram.types import Poll

import database as db

logger = logging.getLogger(__name__)


def is_quiz_poll(poll: Poll) -> bool:
    """Проверяет, является ли poll викториной."""
    return getattr(poll, "type", None) == "quiz"


def save_poll_as_question(test_id: int, poll: Poll, imported_by: int) -> tuple[str, Optional[int]]:
    """
    Сохраняет Quiz Poll в базу.

    Возвращает (status, id):
      status = 'ok'    -> вопрос создан, id = question_id
      status = 'draft' -> сохранён черновик (нужен ручной выбор), id = draft_id
      status = 'err'   -> ошибка, id = None
    """
    if not is_quiz_poll(poll):
        return "err", None

    question_text = poll.question or ""
    options_texts = [opt.text for opt in (poll.options or [])]
    if not question_text or len(options_texts) < 2:
        return "err", None

    explanation = getattr(poll, "explanation", "") or ""
    correct_option_id = getattr(poll, "correct_option_id", None)

    # Запоминаем сам факт импорта
    raw = {
        "question": question_text,
        "options": options_texts,
        "correct_option_id": correct_option_id,
        "explanation": explanation,
        "poll_id": poll.id,
    }
    try:
        db.execute(
            """INSERT INTO imported_polls (test_id, poll_id, question_text, raw_data,
                                          correct_option_id, needs_manual_correct_answer, imported_by)
               VALUES (?,?,?,?,?,?,?)""",
            (test_id, poll.id, question_text, json.dumps(raw, ensure_ascii=False),
             correct_option_id,
             0 if correct_option_id is not None else 1,
             imported_by),
        )
    except Exception as e:
        logger.warning("Ошибка записи imported_polls: %s", e)

    # Если правильный ответ известен - сразу создаём вопрос
    if correct_option_id is not None:
        # Узнаём текущий max order
        row = db.fetchone(
            "SELECT COALESCE(MAX(order_num), 0) AS m FROM questions WHERE test_id=?",
            (test_id,),
        )
        cur_order = (row["m"] if row else 0) + 1
        db.execute(
            """INSERT INTO questions (test_id, text, explanation, source_type,
                                      poll_id, order_num)
               VALUES (?,?,?,?,?,?)""",
            (test_id, question_text, explanation, "poll_import", poll.id, cur_order),
        )
        qrow = db.fetchone("SELECT last_insert_rowid() AS id")
        qid = qrow["id"]
        for i, opt_text in enumerate(options_texts):
            db.execute(
                "INSERT INTO question_options (question_id, text, is_correct, order_num) VALUES (?,?,?,?)",
                (qid, opt_text, 1 if i == correct_option_id else 0, i),
            )
        return "ok", qid

    # Иначе - черновик
    db.execute(
        """INSERT INTO question_drafts (test_id, source_type, question_text, raw_options,
                                        status, created_by)
           VALUES (?,?,?,?,?,?)""",
        (test_id, "poll_forwarded", question_text,
         json.dumps(options_texts, ensure_ascii=False), "pending", imported_by),
    )
    drow = db.fetchone("SELECT last_insert_rowid() AS id")
    return "draft", drow["id"]


def save_poll_dict_as_question(test_id: int, p: dict, imported_by: int) -> str:
    """
    Сохраняет poll (приходящий как dict из FSM-буфера).
    Возвращает 'ok' | 'draft' | 'err'.
    """
    question_text = (p.get("question") or "").strip()
    options_texts = p.get("options") or []
    if not question_text or len(options_texts) < 2:
        return "err"
    correct_option_id = p.get("correct_option_id")
    explanation = p.get("explanation") or ""
    poll_id = p.get("id") or ""

    try:
        db.execute(
            """INSERT INTO imported_polls (test_id, poll_id, question_text, raw_data,
                                          correct_option_id, needs_manual_correct_answer, imported_by)
               VALUES (?,?,?,?,?,?,?)""",
            (test_id, poll_id, question_text, json.dumps(p, ensure_ascii=False),
             correct_option_id,
             0 if correct_option_id is not None else 1,
             imported_by),
        )
    except Exception as e:
        logger.warning("imported_polls insert: %s", e)

    if correct_option_id is not None:
        row = db.fetchone(
            "SELECT COALESCE(MAX(order_num), 0) AS m FROM questions WHERE test_id=?",
            (test_id,),
        )
        cur_order = (row["m"] if row else 0) + 1
        db.execute(
            """INSERT INTO questions (test_id, text, explanation, source_type,
                                      poll_id, order_num)
               VALUES (?,?,?,?,?,?)""",
            (test_id, question_text, explanation, "poll_import", poll_id, cur_order),
        )
        qid = db.fetchone("SELECT last_insert_rowid() AS id")["id"]
        for i, opt_text in enumerate(options_texts):
            db.execute(
                "INSERT INTO question_options (question_id, text, is_correct, order_num) VALUES (?,?,?,?)",
                (qid, opt_text, 1 if i == correct_option_id else 0, i),
            )
        return "ok"

    db.execute(
        """INSERT INTO question_drafts (test_id, source_type, question_text, raw_options,
                                        status, created_by)
           VALUES (?,?,?,?,?,?)""",
        (test_id, "poll_forwarded", question_text,
         json.dumps(options_texts, ensure_ascii=False), "pending", imported_by),
    )
    return "draft"


def list_drafts(test_id: int) -> list[dict]:
    rows = db.fetchall(
        "SELECT * FROM question_drafts WHERE test_id=? AND status='pending' ORDER BY id",
        (test_id,),
    )
    return [dict(r) for r in rows]


def get_draft(draft_id: int) -> Optional[dict]:
    row = db.fetchone("SELECT * FROM question_drafts WHERE id=?", (draft_id,))
    return dict(row) if row else None


def finalize_draft(draft_id: int, correct_index: int) -> bool:
    """Превращает черновик в полноценный вопрос."""
    draft = get_draft(draft_id)
    if not draft or draft["status"] != "pending":
        return False
    try:
        options = json.loads(draft["raw_options"])
    except (ValueError, TypeError):
        return False
    if correct_index < 0 or correct_index >= len(options):
        return False

    row = db.fetchone(
        "SELECT COALESCE(MAX(order_num), 0) AS m FROM questions WHERE test_id=?",
        (draft["test_id"],),
    )
    cur_order = (row["m"] if row else 0) + 1
    db.execute(
        """INSERT INTO questions (test_id, text, source_type, order_num)
           VALUES (?,?,?,?)""",
        (draft["test_id"], draft["question_text"], "poll_forwarded", cur_order),
    )
    qrow = db.fetchone("SELECT last_insert_rowid() AS id")
    qid = qrow["id"]
    for i, opt_text in enumerate(options):
        db.execute(
            "INSERT INTO question_options (question_id, text, is_correct, order_num) VALUES (?,?,?,?)",
            (qid, opt_text, 1 if i == correct_index else 0, i),
        )
    db.execute(
        "UPDATE question_drafts SET status='completed', draft_correct_option=? WHERE id=?",
        (correct_index, draft_id),
    )
    return True


def delete_draft(draft_id: int) -> None:
    db.execute("DELETE FROM question_drafts WHERE id=?", (draft_id,))


# ═════════════════ Импорт QuizPoll через временный буфер (v79) ═════════════════
# Пока админ шлёт QuizPoll-вопросы, они лежат в буфере (poll_import_buffer, своя
# строка на каждого админа) и в тест НЕ попадают. В базу тестов вопросы
# записываются одной операцией — только по кнопке «✅ Сохранить». Буфер живёт в
# базе, а не в памяти: перезапуск бота между вопросами его не теряет.

import re as _re
from datetime import datetime as _dt


def _norm(text: str) -> str:
    return _re.sub(r"\s+", " ", (text or "").strip()).casefold()


def poll_to_item(poll) -> tuple:
    """QuizPoll из сообщения → элемент буфера. (item, None) или (None, причина).
    Тексты вопроса и вариантов берутся как есть — без правок и сокращений."""
    if poll is None:
        return None, "not_poll"
    if getattr(poll, "type", None) != "quiz":
        return None, "not_quiz"
    question = (poll.question or "").strip()
    options = [(o.text or "") for o in (poll.options or [])]
    if not question:
        return None, "no_question"
    if len(options) < 2:
        return None, "no_options"
    correct = getattr(poll, "correct_option_id", None)
    if correct is None or not 0 <= int(correct) < len(options):
        return None, "no_correct"
    return {"poll_id": poll.id, "question": poll.question, "options": options, "correct": int(correct),
            "explanation": getattr(poll, "explanation", None) or "",
            "added_at": _dt.utcnow().strftime("%Y-%m-%dT%H:%M:%S")}, None


def item_tag(item: dict) -> str:
    """Короткая метка вопроса для кнопок: удалять именно тот вопрос, который видел админ."""
    import hashlib as _h
    return _h.sha1((_norm(item.get("question")) + "|" + "|".join(_norm(o) for o in item.get("options") or [])).encode()).hexdigest()[:6]


def find_duplicate(items: list, item: dict) -> int:
    """Такой же вопрос (текст + варианты) уже в буфере? Индекс или -1."""
    key = (_norm(item["question"]), tuple(_norm(o) for o in item["options"]))
    for i, it in enumerate(items):
        if (_norm(it.get("question")), tuple(_norm(o) for o in it.get("options") or [])) == key:
            return i
    return -1


def buffer_get(tg_id) -> Optional[dict]:
    row = db.fetchone("SELECT * FROM poll_import_buffer WHERE tg_id=?", (int(tg_id),))
    if not row:
        return None
    try:
        target = json.loads(row["target"] or "null")
        items = json.loads(row["items"] or "[]")
    except (ValueError, TypeError):
        target, items = None, []
    return {"target": target, "items": items if isinstance(items, list) else [], "updated_at": row["updated_at"]}


def buffer_set(tg_id, target: Optional[dict], items: list) -> None:
    db.execute("INSERT INTO poll_import_buffer (tg_id, target, items, updated_at) VALUES (?,?,?,?) "
               "ON CONFLICT(tg_id) DO UPDATE SET target=excluded.target, items=excluded.items, "
               "updated_at=excluded.updated_at",
               (int(tg_id), json.dumps(target, ensure_ascii=False) if target else None,
                json.dumps(items, ensure_ascii=False), _dt.utcnow().strftime("%Y-%m-%dT%H:%M:%S")))


def buffer_clear(tg_id) -> None:
    db.execute("DELETE FROM poll_import_buffer WHERE tg_id=?", (int(tg_id),))


def target_label(target: Optional[dict]) -> str:
    if not target:
        return "место не выбрано"
    return target.get("label") or (f"тест #{target['test_id']}" if target.get("test_id") else "новый тест")


def commit_buffer(tg_id, user_id: int) -> dict:
    """Записать буфер в тест. Новый тест создаётся только здесь и только с
    вопросами — пустой тест не появляется. Порядок вопросов — как отправляли."""
    buf = buffer_get(tg_id)
    if not buf or not buf["items"]:
        return {"ok": False, "error": "empty"}
    target = buf.get("target") or {}
    items = buf["items"]
    test_id = target.get("test_id")
    if test_id:
        if not db.fetchone("SELECT id FROM tests WHERE id=?", (int(test_id),)):
            return {"ok": False, "error": "test_missing"}
    elif not (target.get("new_title") or "").strip():
        return {"ok": False, "error": "no_target"}
    # Забираем буфер за собой одной командой: второе нажатие «Сохранить» (двойной клик,
    # повтор запроса) его уже не увидит и вопросы не запишутся дважды
    cur = db.execute("DELETE FROM poll_import_buffer WHERE tg_id=? AND updated_at IS ?",
                     (int(tg_id), buf.get("updated_at")))
    if not cur.rowcount:
        return {"ok": False, "error": "empty"}
    try:
        return _commit_items(tg_id, user_id, target, items)
    except Exception:
        buffer_set(tg_id, target, items)          # ничего не потеряно — можно нажать ещё раз
        raise


def _commit_items(tg_id, user_id: int, target: dict, items: list) -> dict:
    test_id = target.get("test_id")
    created = False
    if not test_id:
        title = (target.get("new_title") or "").strip()
        cat = target.get("category_id")
        db.execute(
            "INSERT INTO tests (title, description, subject, grade, category, category_id, language, test_type, "
            "time_per_question, attempts_limit, first_attempt_only, is_paid, price, shuffle_questions, "
            "shuffle_options, show_correct, show_explanation, required_channel, allow_duel, status, created_by, "
            "created_at) VALUES (?,'','',0,'',?,?,'regular',30,NULL,1,0,0,1,1,0,0,NULL,1,'active',?,?)",
            (title[:200], int(cat) if cat else None, target.get("language") or "ru", int(user_id),
             _dt.utcnow().isoformat(timespec="seconds")))
        test_id = db.fetchone("SELECT last_insert_rowid() AS id")["id"]
        created = True
    row = db.fetchone("SELECT COALESCE(MAX(order_num), 0) AS m FROM questions WHERE test_id=?", (int(test_id),))
    order = (row["m"] if row else 0)
    saved = 0
    for it in items:
        options = list(it.get("options") or [])
        correct = int(it.get("correct", -1))
        if not (it.get("question") or "").strip() or len(options) < 2 or not 0 <= correct < len(options):
            continue
        order += 1
        db.execute("INSERT INTO questions (test_id, text, explanation, source_type, poll_id, order_num) "
                   "VALUES (?,?,?,?,?,?)",
                   (int(test_id), it["question"], it.get("explanation") or "", "poll_import", it.get("poll_id"), order))
        qid = db.fetchone("SELECT last_insert_rowid() AS id")["id"]
        db.executemany("INSERT INTO question_options (question_id, text, is_correct, order_num) VALUES (?,?,?,?)",
                       [(qid, o, 1 if i == correct else 0, i) for i, o in enumerate(options)])
        try:
            db.execute("INSERT INTO imported_polls (test_id, poll_id, question_text, raw_data, correct_option_id, "
                       "needs_manual_correct_answer, imported_by) VALUES (?,?,?,?,?,0,?)",
                       (int(test_id), it.get("poll_id"), it["question"], json.dumps(it, ensure_ascii=False),
                        correct, int(user_id)))
        except Exception as e:
            logger.warning("imported_polls: %s", e)
        saved += 1
    t = db.fetchone("SELECT title FROM tests WHERE id=?", (int(test_id),))
    return {"ok": True, "test_id": int(test_id), "saved": saved, "created": created,
            "title": (t or {}).get("title") or ""}


# ── дерево «предмет → раздел → тема/тест» для выбора места ──

def categories_with_counts() -> list:
    rows = db.fetchall("SELECT c.*, (SELECT COUNT(*) FROM tests t WHERE t.category_id=c.id AND t.status='active') AS n "
                       "FROM test_categories c ORDER BY c.sort_order, c.id")
    return [dict(r) for r in rows]


def uncategorized_count() -> int:
    r = db.fetchone("SELECT COUNT(*) AS n FROM tests WHERE category_id IS NULL AND status='active'")
    return int(r["n"] if r else 0)


def category_sections(category_id) -> list:
    """Разделы сайта у предмета, привязанного к разделу бота (только с тестами)."""
    if not category_id:
        return []
    rows = db.fetchall(
        "SELECT sec.id, sec.title, sub.title AS subject_title, "
        "(SELECT COUNT(*) FROM lessons l WHERE l.section_id=sec.id AND l.test_id IS NOT NULL) AS n "
        "FROM sections sec JOIN subjects sub ON sub.id=sec.subject_id "
        "WHERE sub.bot_category_id=? AND sub.status='active' ORDER BY sub.id, sec.sort_order, sec.id",
        (int(category_id),))
    return [dict(r) for r in rows if r["n"]]


def section_tests(section_id) -> list:
    rows = db.fetchall(
        "SELECT l.id AS lesson_id, l.title AS lesson_title, l.status AS lesson_status, t.id AS test_id, t.title, "
        "(SELECT COUNT(*) FROM questions q WHERE q.test_id=t.id) AS n "
        "FROM lessons l JOIN tests t ON t.id=l.test_id WHERE l.section_id=? ORDER BY l.sort_order, l.id",
        (int(section_id),))
    return [dict(r) for r in rows]


def category_tests(category_id) -> list:
    sql = ("SELECT t.id AS test_id, t.title, (SELECT COUNT(*) FROM questions q WHERE q.test_id=t.id) AS n "
           "FROM tests t WHERE t.status='active' AND ")
    if category_id:
        rows = db.fetchall(sql + "t.category_id=? ORDER BY t.id DESC", (int(category_id),))
    else:
        rows = db.fetchall(sql + "t.category_id IS NULL ORDER BY t.id DESC")
    return [dict(r) for r in rows]


def test_target(test_id) -> Optional[dict]:
    t = db.fetchone("SELECT t.id, t.title, t.category_id, c.name AS cat FROM tests t "
                    "LEFT JOIN test_categories c ON c.id=t.category_id WHERE t.id=?", (int(test_id),))
    if not t:
        return None
    return {"test_id": int(t["id"]), "category_id": t["category_id"], "new_title": None,
            "label": (f"{t['cat']} → " if t["cat"] else "") + f"«{t['title']}»"}
