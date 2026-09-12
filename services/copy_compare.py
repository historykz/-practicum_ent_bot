"""
«Сравнить с оригиналом» и отметки о копии в админке. Только чтение —
ничего не меняет и ни на что не влияет.

Сравниваются текущие версии копии и оригинала: цена и настройки предмета,
разделы (добавлены / удалены / переименованы / порядок), уроки (добавлены /
удалены / что изменено: название, конспект, страницы, видео, рабочая
тетрадь, карточки, тест и его настройки, зачёт, доступ, цены). Файлы
сравниваются по содержимому: у копии свои файлы с другими именами.
"""
import hashlib
from typing import Optional

import database as db
from services import deep_copy_service as dc
from webapp import shortcuts as sc

SUBJECT_FIELDS = (
    ("title", "Название"),
    ("description", "Описание"),
    ("access_mode", "Режим доступа"),
    ("status", "Показ в каталоге"),
    ("premium_ignored", "Продаётся отдельно (Премиум не открывает)"),
    ("own_price", "Своя цена"),
    ("unit_sale_enabled", "Продажа уроков и разделов за Stars"),
    ("unit_price_stars", "Цена урока по умолчанию, Stars"),
    ("pass_percent", "Порог сдачи теста, %"),
    ("min_read_min", "Минимум минут чтения"),
    ("require_sequential", "Уроки строго по порядку"),
    ("live_code_enabled", "Live-код"),
    ("quizlet_url", "Карточки Quizlet предмета"),
    ("rewards_enabled", "Система вознаграждений"),
    ("reward_pay_prior", "Оплата уроков, пройденных до старта"),
)
_BOOL = {"premium_ignored", "unit_sale_enabled", "require_sequential", "live_code_enabled",
         "rewards_enabled", "reward_pay_prior"}

LESSON_ASPECTS = (
    ("title", "название"), ("description", "описание"), ("content", "текст конспекта"),
    ("pages", "страницы конспекта"), ("video", "видео"), ("workbook", "рабочая тетрадь"),
    ("quizlet", "карточки Quizlet"), ("test", "вопросы теста"), ("test_settings", "настройки теста"),
    ("zachet", "зачёт"), ("access", "открыт / платный"), ("price_stars", "цена в Stars"),
    ("reward", "вознаграждение"), ("modes", "карточки и заучивание (режимы, цены)"),
)
# Всё, что триггеры «Копия изменена» считают содержимым теста, — сравнивается тоже
TEST_SETTINGS = ("title", "description", "attempts_limit", "first_attempt_only", "deadline",
                 "shuffle_questions", "shuffle_options", "show_correct", "show_explanation",
                 "show_results", "time_per_question", "allow_retry_wrong", "display_mode",
                 "is_paid", "price", "price_stars", "required_subscription", "required_channel",
                 "allow_in_group", "allow_duel", "allow_daily", "allow_tournament", "status")
MODE_FIELDS = ("flashcards_enabled", "learning_enabled", "fc_price_1", "fc_price_10",
               "fc_price_redo", "ln_price_1", "ln_price_10", "ln_price_redo", "is_free")
BIG_FILE = 8 << 20          # файлы больше — по краям и размеру, а не целиком

_hash_cache: dict = {}


def _fmt_dt(value) -> str:
    from services import reward_service as rs
    d = rs.local_dt(value)
    return d.strftime("%d.%m.%Y в %H:%M") if d else "—"


def _show(field, value) -> str:
    if value is None or value == "":
        return "—"
    if field in _BOOL:
        return "да" if int(value or 0) else "нет"
    if field == "access_mode":
        return sc.MODE_TITLES.get(value, str(value))
    return str(value)


def _norm(field, value):
    if field in _BOOL:
        return int(value or 0)
    return "" if value is None else str(value).strip()


def _subject(sid) -> Optional[dict]:
    if not sid:
        return None
    r = db.fetchone("SELECT * FROM subjects WHERE id=?", (sid,))
    return dict(r) if r else None


# ───────────────────────── отметки ─────────────────────────

def info(subject: dict) -> Optional[dict]:
    """«Копия предмета · создано на основе «X»» и «Копия изменена · дата»."""
    if not subject or not subject.get("copied_from_subject_id"):
        return None
    src = db.fetchone("SELECT id, title FROM subjects WHERE id=?", (subject["copied_from_subject_id"],))
    return {"src_id": subject["copied_from_subject_id"], "src_exists": bool(src),
            "src_title": src["title"] if src else None,
            "copied_at": _fmt_dt(subject.get("copied_at")),
            "modified": bool(subject.get("copy_modified_at")),
            "modified_at": _fmt_dt(subject.get("copy_modified_at")),
            "converted": bool(subject.get("converted_at"))}


def lesson_info(lesson: dict) -> Optional[dict]:
    src_id = (lesson or {}).get("copied_from_lesson_id")
    if not src_id:
        return None
    src = db.fetchone("SELECT l.id, l.title, s.subject_id, sub.title AS subject_title FROM lessons l "
                      "LEFT JOIN sections s ON s.id=l.section_id LEFT JOIN subjects sub "
                      "ON sub.id=s.subject_id WHERE l.id=?", (src_id,))
    return {"src_id": src_id, "src_exists": bool(src),
            "src_title": src["title"] if src else None,
            "src_subject": src["subject_title"] if src else None,
            "src_subject_id": src["subject_id"] if src else None,
            "modified": bool(lesson.get("copy_modified_at")),
            "modified_at": _fmt_dt(lesson.get("copy_modified_at"))}


# ───────────────────────── отпечатки ─────────────────────────

def _file_hash(value, subdir: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    p = dc.resolve_file(value, subdir)
    if p is None:
        return "нет файла"
    try:
        st = p.stat()
    except OSError:
        return "нет файла"
    key = (str(p), st.st_mtime_ns, st.st_size)
    h = _hash_cache.get(key)
    if h is None:
        hh = hashlib.sha256(str(st.st_size).encode())
        with open(p, "rb") as f:
            if st.st_size > BIG_FILE:
                # Видео на сотни мегабайт целиком не читаем: размер плюс
                # начало и конец файла — для информационного сравнения хватает
                hh.update(f.read(1 << 20))
                f.seek(-(1 << 20), 2)
                hh.update(f.read(1 << 20))
            else:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    hh.update(chunk)
        h = hh.hexdigest()
        if len(_hash_cache) > 5000:
            _hash_cache.clear()
        _hash_cache[key] = h
    return h


def _text_hash(text) -> str:
    t = (text or "").strip()
    return hashlib.sha256(t.encode("utf-8")).hexdigest() if t else ""


def _pages(lesson_id) -> tuple:
    out = []
    for r in db.fetchall("SELECT * FROM lesson_images WHERE lesson_id=? ORDER BY sort_order, id",
                         (lesson_id,)):
        r = dict(r)
        if (r.get("storage") or "disk") == "telegram":
            out.append(("tg", r.get("file_unique_id") or r.get("file_id") or ""))
        else:
            out.append(("disk", _file_hash(r.get("image_path"), "lessons")))
    return tuple(out)


def _questions(test_id) -> list:
    if not test_id:
        return []
    return [dict(q) for q in db.fetchall("SELECT * FROM questions WHERE test_id=? ORDER BY order_num, id",
                                         (test_id,))]


def _question_fp(q: dict) -> tuple:
    opts = tuple((o["text"] or "", int(o["is_correct"] or 0)) for o in db.fetchall(
        "SELECT text, is_correct FROM question_options WHERE question_id=? ORDER BY order_num, id",
        (q["id"],)))
    img = (_file_hash(q.get("web_image_path"), "questions") if (q.get("web_image_path") or "").strip()
           else (q.get("image_file_id") or q.get("photo_file_id") or ""))
    return (q.get("text") or "", q.get("explanation") or "", q.get("score"), q.get("topic") or "",
            q.get("difficulty"), q.get("accepted_answers") or "", img, opts)


def _test_settings(test_id) -> tuple:
    t = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,)) if test_id else None
    if not t:
        return ()
    t = dict(t)
    return tuple((f, t.get(f)) for f in TEST_SETTINGS if f in t)


def _modes(test_id) -> tuple:
    if not test_id:
        return ()
    try:
        m = db.fetchone("SELECT * FROM test_modes WHERE test_id=?", (test_id,))
    except Exception:
        return ()
    if not m:
        return ()
    m = dict(m)
    return tuple((f, m.get(f)) for f in MODE_FIELDS if f in m)


def test_fingerprint(test_id) -> tuple:
    """Отпечаток теста целиком (вопросы, настройки, режимы) — для проверки,
    изменился ли тест на самом деле, например после восстановления бэкапа."""
    return (tuple(_question_fp(q) for q in _questions(test_id)), _test_settings(test_id),
            _modes(test_id))


def _channels(subject_id) -> tuple:
    try:
        return tuple(sorted((r["channel_username"] or "", int(r["is_active"] if r["is_active"] is not None else 1))
                            for r in db.fetchall("SELECT channel_username, is_active FROM required_channels "
                                                 "WHERE subject_id=?", (subject_id,))))
    except Exception:
        return ()


def _zachet(lesson: dict) -> tuple:
    bank = tuple((r["topic"] or "", r["question"] or "", r["answer"] or "") for r in db.fetchall(
        "SELECT topic, question, answer FROM zachet_questions WHERE lesson_id=? ORDER BY order_num, id",
        (lesson["id"],)))
    return (tuple(lesson.get(f) for f in ("is_zachet", "zachet_per_attempt",
                                          "zachet_topic_threshold", "zachet_pass_percent")), bank)


def lesson_fp(lesson: dict, reward=(0, 1)) -> dict:
    return {
        "title": (lesson.get("title") or "").strip(),
        "description": (lesson.get("description") or "").strip(),
        "content": _text_hash(lesson.get("content_html")),
        "pages": _pages(lesson["id"]),
        "video": (_file_hash(lesson.get("video_url"), "videos"), lesson.get("youtube_id") or "",
                  lesson.get("video_file_unique") or lesson.get("video_file_id") or ""),
        "workbook": _file_hash(lesson.get("workbook_path"), "workbooks"),
        "quizlet": (lesson.get("quizlet_url") or "").strip(),
        "test": tuple(_question_fp(q) for q in _questions(lesson.get("test_id"))),
        "test_settings": _test_settings(lesson.get("test_id")),
        "modes": _modes(lesson.get("test_id")),
        "zachet": _zachet(lesson),
        "access": ((lesson.get("status") or "open"), int(lesson.get("is_paid") or 0)),
        "price_stars": lesson.get("price_stars"),
        "reward": tuple(reward),
    }


def _test_detail(copy_tid, src_tid) -> dict:
    src_qs = {q["id"]: _question_fp(q) for q in _questions(src_tid)}
    added = changed = 0
    seen = set()
    for q in _questions(copy_tid):
        s = q.get("copied_from_question_id")
        if s in src_qs:
            seen.add(s)
            if _question_fp(q) != src_qs[s]:
                changed += 1
        else:
            added += 1
    return {"added": added, "removed": len(set(src_qs) - seen), "changed": changed}


def _content_row(row: dict) -> dict:
    """Урок так, как его видит ученик (старый ярлык — с контентом оригинала)."""
    return sc.resolve_lesson(row) if row.get("original_id") else row


def lesson_changes(copy_l: dict, src_l: dict, copy_reward=(0, 1), src_reward=(0, 1)) -> list:
    a, b = lesson_fp(_content_row(src_l), src_reward), lesson_fp(copy_l, copy_reward)
    return [title for key, title in LESSON_ASPECTS if a[key] != b[key]]


def _home_subject(lesson_id):
    r = db.fetchone("SELECT s.subject_id FROM lessons l JOIN sections s ON s.id=l.section_id "
                    "WHERE l.id=?", (lesson_id,))
    return r["subject_id"] if r else None


def lesson_diff(lesson_id: int) -> Optional[dict]:
    """Что в этой копии урока отличается от урока, с которого она сделана."""
    from services import reward_service as rs
    l = db.fetchone("SELECT * FROM lessons WHERE id=?", (lesson_id,))
    if not l or not l["copied_from_lesson_id"]:
        return None
    l = dict(l)
    s = db.fetchone("SELECT * FROM lessons WHERE id=?", (l["copied_from_lesson_id"],))
    if not s:
        return {"src_exists": False, "changes": []}
    s = dict(s)
    cs, ss = _home_subject(l["id"]), _home_subject(s["id"])
    cr = rs.prices(cs).get(l["id"], (0, 1)) if cs else (0, 1)
    sr = rs.prices(ss).get(int(sc.orig_lesson_id(s["id"])), (0, 1)) if ss else (0, 1)
    return {"src_exists": True, "changes": lesson_changes(l, s, cr, sr)}


# ───────────────────────── сравнение предмета ─────────────────────────

def _sections(sid) -> list:
    return [dict(r) for r in db.fetchall("SELECT * FROM sections WHERE subject_id=? "
                                         "ORDER BY sort_order, id", (sid,))]


def _lessons(sid) -> list:
    return [dict(r) for r in db.fetchall(
        "SELECT l.*, s.title AS section_title, s.copied_from_section_id AS section_copied_from "
        "FROM lessons l JOIN sections s ON s.id=l.section_id WHERE s.subject_id=? "
        "ORDER BY s.sort_order, s.id, l.sort_order, l.id", (sid,))]


def compare(copy_sid: int) -> Optional[dict]:
    from services import reward_service as rs
    copy = _subject(copy_sid)
    if not copy:
        return None
    src = _subject(copy.get("copied_from_subject_id"))
    # Ключ не «copy»: в шаблоне c.copy — это метод словаря dict.copy
    out = {"subject": copy, "src": src, "info": info(copy), "settings": [],
           "sections": {"added": [], "removed": [], "changed": [], "reordered": False},
           "lessons": {"added": [], "removed": [], "changed": [], "reordered": False},
           "total": 0, "same_lessons": 0}
    if not src:
        return out
    for f, label in SUBJECT_FIELDS:
        if _norm(f, src.get(f)) != _norm(f, copy.get(f)):
            out["settings"].append({"label": label, "orig": _show(f, src.get(f)),
                                    "mine": _show(f, copy.get(f))})
    ch_src, ch_copy = _channels(src["id"]), _channels(copy_sid)
    if ch_src != ch_copy:
        fmt_ch = lambda chs: ", ".join(c for c, on in chs if on) or "—"
        out["settings"].append({"label": "Обязательные каналы", "orig": fmt_ch(ch_src),
                                "mine": fmt_ch(ch_copy)})
    # Разделы
    src_secs, copy_secs = _sections(src["id"]), _sections(copy_sid)
    src_by_id = {s["id"]: s for s in src_secs}
    used, order_copy = set(), []
    for c in copy_secs:
        s = src_by_id.get(c.get("copied_from_section_id"))
        if not s:
            out["sections"]["added"].append(c["title"])
            continue
        used.add(s["id"])
        order_copy.append(s["id"])
        ch = []
        if (s["title"] or "").strip() != (c["title"] or "").strip():
            ch.append(f"название: «{s['title']}» → «{c['title']}»")
        if (s.get("price_stars"), int(s.get("sale_enabled") or 0)) != \
                (c.get("price_stars"), int(c.get("sale_enabled") or 0)):
            ch.append("цена раздела")
        if ch:
            out["sections"]["changed"].append({"title": c["title"], "changes": ch})
    out["sections"]["removed"] = [s["title"] for s in src_secs if s["id"] not in used]
    out["sections"]["reordered"] = order_copy != [s["id"] for s in src_secs if s["id"] in used]
    # Уроки
    src_prices, copy_prices = rs.prices(src["id"]), rs.prices(copy_sid)
    src_les, copy_les = _lessons(src["id"]), _lessons(copy_sid)
    src_les_by_id = {l["id"]: l for l in src_les}
    used_l, order_l = set(), []
    for l in copy_les:
        s = src_les_by_id.get(l.get("copied_from_lesson_id"))
        if not s:
            out["lessons"]["added"].append({"id": l["id"], "title": l["title"],
                                            "section": l["section_title"]})
            continue
        used_l.add(s["id"])
        order_l.append(s["id"])
        content = _content_row(s)
        ch = lesson_changes(l, s, copy_prices.get(l["id"], (0, 1)),
                            src_prices.get(int(sc.orig_lesson_id(s["id"])), (0, 1)))
        if l.get("section_copied_from") and l["section_copied_from"] != s["section_id"]:
            ch.append("перенесён в другой раздел")
        if ch:
            out["lessons"]["changed"].append({
                "id": l["id"], "title": l["title"], "orig_title": s["title"], "changes": ch,
                "test": (_test_detail(l.get("test_id"), content.get("test_id"))
                         if "вопросы теста" in ch else None)})
        else:
            out["same_lessons"] += 1
    out["lessons"]["removed"] = [{"title": s["title"], "section": s["section_title"]}
                                 for s in src_les if s["id"] not in used_l]
    out["lessons"]["reordered"] = order_l != [s["id"] for s in src_les if s["id"] in used_l]
    sec, les = out["sections"], out["lessons"]
    out["total"] = (len(out["settings"]) + len(sec["added"]) + len(sec["removed"]) + len(sec["changed"])
                    + int(sec["reordered"]) + len(les["added"]) + len(les["removed"])
                    + len(les["changed"]) + int(les["reordered"]))
    return out
