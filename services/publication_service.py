"""
Публикации теста в канал с ручным выбором вопросов.

Админ отмечает нужные вопросы (или вводит их номера через запятую), порядок
сохраняется ровно тот, что он задал. Подборка живёт отдельной записью:
храним и список id вопросов, и снимок их текста на момент публикации — если
позже поправить исходный тест, уже опубликованная подборка не поедет.

Случайный режим публикации (autopub_service) не трогаем — это второй,
независимый способ.
"""
import json
from datetime import datetime
from typing import Optional

import config
import database as db

MAX_QUESTIONS = 200          # разумный предел на одну публикацию


# ---------- Совместимость со старыми базами ----------

_table_ready = False


def ensure_table() -> None:
    """Таблицу создаёт database.py, но если его забыли обновить — не падаем."""
    global _table_ready
    if _table_ready:
        return
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS test_publications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT DEFAULT '',
                lesson_id INTEGER,
                test_id INTEGER NOT NULL,
                question_ids TEXT DEFAULT '[]',
                snapshot TEXT DEFAULT '',
                status TEXT DEFAULT 'draft',
                created_by INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                published_at TEXT,
                channel_id TEXT,
                channel_message_id INTEGER,
                intro_text TEXT DEFAULT ''
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_test_pub_status "
                   "ON test_publications(status, id DESC)")
        _table_ready = True
    except Exception:
        pass


# ---------- Разбор номеров вопросов ----------

def parse_numbers(raw: str, total: int) -> tuple:
    """«5, 7, 10» → ([5, 7, 10], [ошибки]).

    Принимаем запятые, пробелы, точки с запятой и диапазоны вида 5-9.
    Порядок сохраняем как ввёл админ, повторы убираем (первое вхождение).
    """
    if not raw:
        return [], []
    text = raw.replace(";", ",").replace("\n", ",")
    out, bad, seen = [], [], set()
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk and not chunk.startswith("-"):
            a, _, b = chunk.partition("-")
            if a.strip().isdecimal() and b.strip().isdecimal():
                lo, hi = int(a), int(b)
                lo, hi = max(1, lo), min(total, hi)     # не раскручиваем 1-999999999
                if lo > hi:
                    lo, hi = hi, lo
                for n in range(lo, hi + 1):
                    if 1 <= n <= total and n not in seen:
                        seen.add(n)
                        out.append(n)
                continue
        if chunk.isdecimal():
            n = int(chunk)
            if 1 <= n <= total:
                if n not in seen:
                    seen.add(n)
                    out.append(n)
            else:
                bad.append(chunk)
        else:
            bad.append(chunk)
    return out, bad


# ---------- Вопросы теста ----------

def test_questions(test_id: int) -> list:
    """Вопросы теста по порядку, с вариантами ответов и пометкой верного.

    Номер (№ 1, 2, 3…) — это позиция в тесте, её видит и админ, и по ней
    он вводит номера через запятую.
    """
    rows = db.fetchall(
        "SELECT id, text, explanation, web_image_path FROM questions "
        "WHERE test_id=? ORDER BY order_num, id", (test_id,))
    out = []
    for i, q in enumerate(rows, start=1):
        opts = db.fetchall(
            "SELECT id, text, is_correct FROM question_options "
            "WHERE question_id=? ORDER BY order_num, id", (q["id"],))
        out.append({
            "num": i,
            "id": q["id"],
            "text": q["text"] or "",
            "explanation": q["explanation"] or "",
            "image": q["web_image_path"] or "",
            "options": [{"id": o["id"], "text": o["text"] or "",
                         "is_correct": bool(o["is_correct"])} for o in opts],
        })
    return out


def make_snapshot(question_ids: list) -> str:
    """Снимок вопросов — чтобы правка исходного теста не меняла публикацию."""
    snap = []
    for qid in question_ids:
        q = db.fetchone("SELECT id, text, explanation FROM questions WHERE id=?", (qid,))
        if not q:
            continue
        opts = db.fetchall(
            "SELECT id, text, is_correct FROM question_options "
            "WHERE question_id=? ORDER BY order_num, id", (qid,))
        snap.append({
            "id": q["id"], "text": q["text"] or "", "explanation": q["explanation"] or "",
            "options": [{"id": o["id"], "text": o["text"] or "",
                         "is_correct": bool(o["is_correct"])} for o in opts],
        })
    return json.dumps(snap, ensure_ascii=False)


# ---------- Публикации ----------

def only_own_questions(test_id: int, question_ids: list) -> list:
    """Оставляет только вопросы этого теста, сохраняя порядок.

    Список приходит из формы, а форму можно подделать. Чужой вопрос попал бы
    ученику и записался в попытку с другим тестом — поэтому фильтруем и здесь,
    и при выдаче теста.
    """
    rows = db.fetchall("SELECT id FROM questions WHERE test_id=?", (test_id,))
    own = {r["id"] for r in rows}
    out, seen = [], set()
    for q in question_ids:
        try:
            qid = int(q)
        except (TypeError, ValueError):
            continue
        if qid in own and qid not in seen:
            seen.add(qid)
            out.append(qid)
    return out


def create(test_id: int, lesson_id: Optional[int], question_ids: list,
           title: str = "", admin_id: int = None, intro: str = "") -> int:
    ensure_table()
    ids = only_own_questions(test_id, question_ids)[:MAX_QUESTIONS]
    db.execute(
        "INSERT INTO test_publications (title, lesson_id, test_id, question_ids, "
        "status, created_by, intro_text) VALUES (?,?,?,?,'draft',?,?)",
        ((title or "").strip()[:200], lesson_id, test_id,
         json.dumps(ids), admin_id, (intro or "").strip()[:800]))
    return db.fetchone("SELECT last_insert_rowid() AS id")["id"]


def update_questions(pub_id: int, question_ids: list) -> None:
    """Менять состав можно, пока публикация не ушла в канал."""
    ensure_table()
    pub = get(pub_id)
    if not pub or pub["status"] == "published":
        return
    ids = only_own_questions(pub["test_id"], question_ids)[:MAX_QUESTIONS]
    db.execute("UPDATE test_publications SET question_ids=? WHERE id=?",
               (json.dumps(ids), pub_id))


def update_meta(pub_id: int, title: str = None, intro: str = None) -> None:
    ensure_table()
    if title is not None:
        db.execute("UPDATE test_publications SET title=? WHERE id=?",
                   ((title or "").strip()[:200], pub_id))
    if intro is not None:
        db.execute("UPDATE test_publications SET intro_text=? WHERE id=?",
                   ((intro or "").strip()[:800], pub_id))


def get(pub_id: int) -> Optional[dict]:
    ensure_table()
    try:
        row = db.fetchone("SELECT * FROM test_publications WHERE id=?", (pub_id,))
    except Exception:
        return None
    return dict(row) if row else None


def abort_attempt(pub_id: int, tg_id: int) -> None:
    """Сбросить незаконченную попытку подборки — кнопка «Начать заново»."""
    import utils
    user = utils.get_user_by_tg(tg_id)
    if not user:
        return
    db.execute(
        "UPDATE test_attempts SET status='aborted' "
        "WHERE user_id=? AND publication_id=? AND status='in_progress'",
        (user["id"], pub_id))


def live_count(pub: dict) -> int:
    """Сколько вопросов ученик реально получит.

    Считаем так же, как выдаём: у опубликованной берём снимок, у черновика —
    живые вопросы теста. Иначе в списке, в посте и в самом тесте оказывались
    разные числа.
    """
    ids = question_ids(pub)
    if pub.get("status") == "published" and pub.get("snapshot"):
        snap = {q["id"]: q for q in snapshot_questions(pub)}
        return len([i for i in ids if i in snap])
    rows = db.fetchall("SELECT id FROM questions WHERE test_id=?", (pub["test_id"],))
    own = {r["id"] for r in rows}
    alive = 0
    for qid in ids:
        if qid not in own:
            continue
        if db.fetchone("SELECT COUNT(*) AS c FROM question_options WHERE question_id=?",
                       (qid,))["c"] >= 2:
            alive += 1
    return alive


def question_ids(pub: dict) -> list:
    try:
        return [int(x) for x in json.loads(pub.get("question_ids") or "[]")]
    except (ValueError, TypeError):
        return []


def snapshot_questions(pub: dict) -> list:
    try:
        return json.loads(pub.get("snapshot") or "[]")
    except (ValueError, TypeError):
        return []


def all_publications(status: str = None) -> list:
    ensure_table()
    sql = ("SELECT p.*, t.title AS test_title, l.title AS lesson_title "
           "FROM test_publications p "
           "LEFT JOIN tests t ON t.id = p.test_id "
           "LEFT JOIN lessons l ON l.id = p.lesson_id ")
    args = ()
    if status:
        sql += "WHERE p.status=? "
        args = (status,)
    sql += "ORDER BY p.id DESC LIMIT 300"
    try:
        rows = db.fetchall(sql, args)
    except Exception:
        return []
    out = []
    for r in rows:
        d = dict(r)
        d["count"] = live_count(d)
        d["subject_title"], d["section_title"] = _titles_for(d.get("lesson_id"))
        out.append(d)
    return out


def _titles_for(lesson_id) -> tuple:
    if not lesson_id:
        return "", ""
    row = db.fetchone(
        "SELECT s.title AS subject, sec.title AS section FROM lessons l "
        "JOIN sections sec ON sec.id = l.section_id "
        "JOIN subjects s ON s.id = sec.subject_id WHERE l.id=?", (lesson_id,))
    return (row["subject"] or "", row["section"] or "") if row else ("", "")


def describe(pub: dict) -> dict:
    """Полные данные для предпросмотра и списка."""
    subject, section = _titles_for(pub.get("lesson_id"))
    lesson = db.fetchone("SELECT title FROM lessons WHERE id=?",
                         (pub.get("lesson_id"),)) if pub.get("lesson_id") else None
    test = db.fetchone("SELECT title FROM tests WHERE id=?", (pub["test_id"],))
    ids = question_ids(pub)

    # У опубликованной подборки берём снимок: он не меняется вслед за тестом
    if pub["status"] == "published" and pub.get("snapshot"):
        qs = snapshot_questions(pub)
        by_id = {q["id"]: q for q in qs}
        questions = [by_id[i] for i in ids if i in by_id] or qs
    else:
        all_q = {q["id"]: q for q in test_questions(pub["test_id"])}
        questions = [all_q[i] for i in ids if i in all_q]

    return {
        "pub": pub,
        "subject_title": subject,
        "section_title": section,
        "lesson_title": (lesson["title"] if lesson else ""),
        "test_title": (test["title"] if test else ""),
        "questions": questions,
        "count": len(questions),
    }


def mark_published(pub_id: int, channel_id: str = "", message_id: int = None) -> None:
    """Фиксируем публикацию и делаем снимок вопросов — потом он не поменяется."""
    ensure_table()
    pub = get(pub_id)
    if not pub:
        return
    snap = make_snapshot(question_ids(pub))
    db.execute(
        "UPDATE test_publications SET status='published', published_at=?, "
        "snapshot=?, channel_id=?, channel_message_id=? WHERE id=?",
        (datetime.utcnow().isoformat(timespec="seconds"), snap,
         str(channel_id or ""), message_id, pub_id))


def duplicate(pub_id: int, admin_id: int = None) -> Optional[int]:
    """Копия подборки — как черновик, чтобы поправить пару вопросов и отправить снова."""
    pub = get(pub_id)
    if not pub:
        return None
    return create(pub["test_id"], pub.get("lesson_id"), question_ids(pub),
                  title=(pub.get("title") or "") + " (копия)",
                  admin_id=admin_id, intro=pub.get("intro_text") or "")


def delete(pub_id: int) -> None:
    ensure_table()
    db.execute("DELETE FROM test_publications WHERE id=?", (pub_id,))


# ---------- Ссылки ----------

def miniapp_url(param: str) -> str:
    """Ссылка, открывающая мини-приложение сразу на нужном экране."""
    short = (getattr(config, "WEB_APP_SHORT_NAME", "") or "").strip()
    base = f"https://t.me/{config.WEB_BOT_USERNAME}"
    if short:
        base += f"/{short}"
    return f"{base}?startapp={param}" if param else base


def publication_link(pub_id: int) -> str:
    return miniapp_url(f"pub_{pub_id}")


def lesson_link(lesson_id: int) -> str:
    return miniapp_url(f"lesson_{lesson_id}")


def lesson_test_link(lesson_id: int) -> str:
    return miniapp_url(f"test_{lesson_id}")


# ---------- Текст поста ----------

def channel_text(pub: dict) -> str:
    d = describe(pub)
    topic = d["section_title"] or d["subject_title"] or d["test_title"]
    lines = ["📝 <b>Тест по теме: {}</b>".format(topic or "ЕНТ")]
    if d["lesson_title"]:
        lines.append("Урок: <b>{}</b>".format(d["lesson_title"]))
    lines.append("Количество вопросов: <b>{}</b>".format(d["count"]))
    intro = (pub.get("intro_text") or "").strip()
    if intro:
        lines.append("")
        lines.append(intro)
    lines.append("")
    lines.append("Пройти тест 👇")
    return "\n".join(lines)


# ---------- Прохождение подборки учеником ----------

def start_attempt(pub_id: int, tg_id: int) -> Optional[dict]:
    """Попытка ровно по выбранным вопросам и в порядке, заданном админом.

    Ограничения урока (платность, стоп-урок, минимальное время чтения) здесь
    не действуют: админ сам отобрал эти вопросы для публичного канала. Зато
    попытка помечается номером публикации — обычный тест урока её не подхватит
    и не покажет ученику урезанный набор.
    """
    import json as _json
    import utils

    pub = get(pub_id)
    if not pub:
        return None
    ids = question_ids(pub)
    if not ids:
        return None

    user = utils.get_user_by_tg(tg_id)
    if not user:
        return None
    user_id = user["id"]
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (pub["test_id"],))
    if not test:
        return None

    # Живые вопросы ЭТОГО теста, строго в порядке админа. Проверку по тесту
    # повторяем здесь: запись в базе могла остаться от старой версии.
    alive = []
    for qid in ids:
        row = db.fetchone("SELECT id FROM questions WHERE id=? AND test_id=?",
                          (qid, pub["test_id"]))
        if row and db.fetchone(
                "SELECT COUNT(*) AS c FROM question_options WHERE question_id=?",
                (qid,))["c"] >= 2:
            alive.append(qid)
    if not alive:
        return None

    existing = db.fetchone(
        "SELECT * FROM test_attempts WHERE user_id=? AND publication_id=? "
        "AND status IN ('in_progress', 'idle') ORDER BY id DESC LIMIT 1", (user_id, pub_id))
    if existing and existing["status"] == "idle":
        # Уборка брошенных попыток отметила тишину — человек вернулся,
        # тихо продолжаем с того же места.
        db.execute("UPDATE test_attempts SET status='in_progress' WHERE id=?",
                   (existing["id"],))
    if existing:
        return {"attempt_id": existing["id"],
                "q_ids": _json.loads(existing["question_order"] or "[]"),
                "options_order": _json.loads(existing["options_order"] or "{}"),
                "resume": True, "test": dict(test)}

    options_order = {}
    if test["shuffle_options"]:
        import random as _rnd
        for qid in alive:
            opts = db.fetchall(
                "SELECT id FROM question_options WHERE question_id=? ORDER BY order_num, id",
                (qid,))
            oids = [o["id"] for o in opts]
            _rnd.shuffle(oids)
            options_order[str(qid)] = oids

    db.execute(
        "INSERT INTO test_attempts (user_id, test_id, question_order, options_order, "
        "status, is_counted, publication_id) VALUES (?,?,?,?, 'in_progress', 0, ?)",
        (user_id, pub["test_id"], _json.dumps(alive), _json.dumps(options_order), pub_id))
    attempt_id = db.fetchone("SELECT last_insert_rowid() AS id")["id"]
    return {"attempt_id": attempt_id, "q_ids": alive,
            "options_order": options_order, "resume": False, "test": dict(test)}


def questions_for_run(pub: dict, q_ids: list, options_order: dict) -> list:
    """Вопросы для прохождения.

    У опубликованной подборки берём снимок: правки исходного теста не должны
    менять то, что уже ушло в канал (это отдельное требование). У черновика
    показываем живые вопросы — админ ещё редактирует.
    """
    if pub.get("status") != "published":
        return None                      # пусть соберёт обычный сборщик урока

    snap = {q["id"]: q for q in snapshot_questions(pub)}
    if not snap:
        return None

    out = []
    for qid in q_ids:
        q = snap.get(qid)
        if not q:
            continue
        opts = [{"id": o["id"], "text": o["text"]} for o in q.get("options") or []]
        order = (options_order or {}).get(str(qid))
        if order:
            by_id = {o["id"]: o for o in opts}
            opts = [by_id[i] for i in order if i in by_id]
        if len(opts) < 2:
            continue
        out.append({"id": qid, "text": q.get("text") or "",
                    "web_image_path": None, "options": opts})
    return out or None
