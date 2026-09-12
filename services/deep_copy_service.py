"""
Копирование предметов, разделов и уроков — всегда НЕЗАВИСИМАЯ копия.

Копия получает собственные разделы, уроки, конспекты (текст, страницы на
диске и в Telegram), видео, рабочие тетради, карточки, зачёты, тесты с
вопросами и вариантами, настройки режимов, цены вознаграждений и
обязательные каналы. Дальше оригинал и копия живут независимо: правка,
удаление или замена чего угодно в одном никак не трогают другой.

Откуда копия — запоминается (copied_from_*): в админке видно «Копия
предмета · создано на основе «X»», а правки внутри копии отмечаются сами
(триггеры в database.py → copy_modified_at). «Сравнить с оригиналом» —
services/copy_compare.py.

Личные данные учеников (прогресс, попытки, доступы, деньги) в новую копию
не копируются: это новая программа с чистой историей. Старые копии-ярлыки
переводятся в независимые с сохранением прогресса — services/copy_convert.py.

Строки копируются по списку колонок из самой базы (PRAGMA table_info):
новая колонка в схеме автоматически попадёт в копию. Если что-то сорвалось
посередине, всё созданное (строки и файлы) убирается — полукопии не будет.
"""
import logging
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import config
import database as db
from webapp import shortcuts as sc

log = logging.getLogger(__name__)

# Служебные колонки, которые копия получает заново
_SKIP_COLS = {"id", "created_at", "updated_at"}

# Своё у урока-ярлыка (то, что видел ученик): обложка и правила показа
_LESSON_SHELL = ("title", "description", "status", "is_paid", "sort_order",
                 "free_override", "price_stars")
# Свои настройки у предмета-ярлыка (если заданы)
_SUBJECT_SHELL = ("description", "status", "access_mode", "is_open", "is_private",
                  "min_read_min", "require_sequential", "pass_percent", "live_code_enabled",
                  "quizlet_url", "premium_ignored", "own_price", "unit_sale_enabled",
                  "unit_price_stars", "cover_url")
_SECTION_SHELL = ("title", "sort_order", "price_stars", "sale_enabled")


def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")


def _cols(table: str) -> list:
    return [r["name"] for r in db.fetchall(f"PRAGMA table_info({table})")]


def _insert_like(table: str, row: dict, **override) -> int:
    cols = [c for c in _cols(table) if c not in _SKIP_COLS]
    vals = [override[c] if c in override else row.get(c) for c in cols]
    db.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        vals)
    return db.fetchone("SELECT last_insert_rowid() AS id")["id"]


def _row(table: str, row_id) -> Optional[dict]:
    if not row_id:
        return None
    r = db.fetchone(f"SELECT * FROM {table} WHERE id=?", (row_id,))
    return dict(r) if r else None


class Job:
    """Что создано по ходу копирования — чтобы при сбое убрать за собой всё."""

    def __init__(self):
        self.files, self.tests, self.categories = [], [], []
        self.subjects, self.sections, self.lessons = [], [], []
        self.images, self.zachet, self.prices, self.channels = [], [], [], []

    def rollback(self) -> None:
        # Предмет удаляется каскадом вместе с разделами, уроками, страницами
        # и зачётами; тесты к урокам внешним ключом не привязаны — отдельно.
        for table, ids in (("subjects", self.subjects), ("sections", self.sections),
                           ("lessons", self.lessons), ("lesson_images", self.images),
                           ("zachet_questions", self.zachet), ("tests", self.tests),
                           ("required_channels", self.channels),
                           ("test_categories", self.categories)):
            for i in reversed(ids):
                try:
                    db.execute(f"DELETE FROM {table} WHERE id=?", (i,))
                except Exception as e:
                    log.warning("откат копии: %s %s: %s", table, i, e)
        for tid in self.tests:
            try:
                db.execute("DELETE FROM test_modes WHERE test_id=?", (tid,))
            except Exception:
                pass
        for sid, lid in self.prices:
            try:
                db.execute("DELETE FROM reward_prices WHERE subject_id=? AND lesson_id=?", (sid, lid))
            except Exception:
                pass
        for sid in self.subjects:
            for table in ("reward_prices", "required_channels"):
                try:
                    db.execute(f"DELETE FROM {table} WHERE subject_id=?", (sid,))
                except Exception:
                    pass
        for p in self.files:
            try:
                Path(p).unlink()
            except Exception:
                pass


# ───────────────────────── файлы ─────────────────────────

def _uploads_root() -> Path:
    return Path(config.DB_PATH).resolve().parent / "uploads"


def resolve_file(value: Optional[str], subdir: str) -> Optional[Path]:
    """Где лежит файл. Значение бывает именем, /uploads/<subdir>/имя,
    uploads/<subdir>/имя или абсолютным путём."""
    value = (value or "").strip()
    if not value:
        return None
    base = os.path.basename(value)
    root = _uploads_root()
    candidates = []
    if value.lstrip("/").startswith("uploads/"):
        candidates.append(root.parent / value.lstrip("/"))
    if os.path.isabs(value):
        candidates.append(Path(value))
    candidates += [root / value.lstrip("/"), root / subdir / base, Path(value)]
    return next((p for p in candidates if p.is_file()), None)


def dup_file(value: Optional[str], subdir: str, job: Job = None) -> Optional[str]:
    """Копия файла под новым именем рядом с исходным; ответ в том же формате.

    None, если файла нет: копия не должна ссылаться на чужой файл, иначе
    удаление в одном уроке унесло бы файл у другого.
    """
    value = (value or "").strip()
    src = resolve_file(value, subdir)
    if src is None:
        if value:
            log.warning("копия: файл не найден, копия без него: %s", value)
        return None
    new_name = f"{uuid.uuid4().hex}{src.suffix}"
    try:
        shutil.copy2(src, src.parent / new_name)
    except Exception as e:
        log.warning("копия файла %s: %s", src, e)
        return None
    if job is not None:
        job.files.append(str(src.parent / new_name))
    base = os.path.basename(value)
    return value[: len(value) - len(base)] + new_name if value.endswith(base) else new_name


_dup_file = dup_file          # прежнее имя — на случай старых вызовов


# ───────────────────────── тесты, страницы, зачёт ─────────────────────────

def copy_test(test_id, job: Job = None, maps: dict = None, category_id=None) -> Optional[int]:
    """Независимая копия теста: вопросы, варианты, картинки, настройки режимов.
    maps (если передан) заполняется соответствием старых и новых id."""
    test = _row("tests", test_id)
    if not test:
        return None
    new_tid = _insert_like("tests", test, category_id=category_id, copied_from_test_id=test["id"])
    if job is not None:
        job.tests.append(new_tid)
    for q in db.fetchall("SELECT * FROM questions WHERE test_id=? ORDER BY order_num, id",
                         (test["id"],)):
        q = dict(q)
        new_qid = _insert_like(
            "questions", q, test_id=new_tid, poll_id=None, serial_no=None,
            web_image_path=dup_file(q.get("web_image_path"), "questions", job),
            copied_from_question_id=q["id"])
        if maps is not None:
            maps.setdefault("q", {})[q["id"]] = new_qid
        for o in db.fetchall(
                "SELECT * FROM question_options WHERE question_id=? ORDER BY order_num, id",
                (q["id"],)):
            new_oid = _insert_like("question_options", dict(o), question_id=new_qid)
            if maps is not None:
                maps.setdefault("o", {})[o["id"]] = new_oid
    try:
        modes = db.fetchone("SELECT * FROM test_modes WHERE test_id=?", (test["id"],))
        if modes:
            _insert_like("test_modes", dict(modes), test_id=new_tid)
    except Exception as e:
        log.warning("копия настроек режимов теста %s: %s", test["id"], e)
    return new_tid


_copy_test = copy_test


def copy_lesson_images(src_lesson_id: int, dst_lesson_id: int, job: Job = None) -> int:
    """Страницы конспекта: с диска — копией файла, из Telegram — той же
    ссылкой (файл в Telegram неизменяем, удаление страницы его не трогает)."""
    n = 0
    for img in db.fetchall("SELECT * FROM lesson_images WHERE lesson_id=? ORDER BY sort_order, id",
                           (src_lesson_id,)):
        img = dict(img)
        if (img.get("storage") or "disk") == "telegram" or not (img.get("image_path") or "").strip():
            new_id = _insert_like("lesson_images", img, lesson_id=dst_lesson_id)
        else:
            path = dup_file(img.get("image_path"), "lessons", job)
            if not path:
                continue
            new_id = _insert_like("lesson_images", img, lesson_id=dst_lesson_id, image_path=path)
        if job is not None:
            job.images.append(new_id)
        n += 1
    return n


def copy_zachet_bank(src_lesson_id: int, dst_lesson_id: int, job: Job = None) -> dict:
    """Банк вопросов зачёта. Возвращает {старый id вопроса: новый}."""
    out = {}
    for zq in db.fetchall("SELECT * FROM zachet_questions WHERE lesson_id=? ORDER BY order_num, id",
                          (src_lesson_id,)):
        new_id = _insert_like("zachet_questions", dict(zq), lesson_id=dst_lesson_id)
        out[zq["id"]] = new_id
        if job is not None:
            job.zachet.append(new_id)
    return out


def _test_category(test_id, src_bot_cat, dst_bot_cat):
    """Тест лежал в разделе бота своего предмета — у копии он в разделе
    копии. Иначе вне каталога: копия не должна дублироваться в чужом разделе."""
    if not test_id:
        return None
    t = db.fetchone("SELECT category_id FROM tests WHERE id=?", (test_id,))
    if t and t["category_id"] and src_bot_cat and t["category_id"] == src_bot_cat:
        return dst_bot_cat
    return None


# ───────────────────────── урок, раздел ─────────────────────────

def _copy_lesson(row: dict, new_section_id: int, job: Job, cats=(None, None),
                 sort_order=None) -> int:
    """Урок целиком. Контент — тот, что видит ученик: у ярлыка — оригинала,
    с его собственной обложкой, карточками и рабочей тетрадью."""
    real_id = int(sc.orig_lesson_id(row["id"]))
    real = _row("lessons", real_id)
    if real and row.get("original_id"):
        merged = dict(real)
        for f in _LESSON_SHELL:
            if row.get(f) is not None:
                merged[f] = row[f]
        if (row.get("quizlet_url") or "").strip():
            merged["quizlet_url"] = row["quizlet_url"]
    else:
        merged = dict(real or row)          # оригинал удалён — копируем обложку
    content = real or {}
    wb_src = row if (row.get("workbook_path") or "").strip() else content
    wb = dup_file(wb_src.get("workbook_path"), "workbooks", job)
    tid = content.get("test_id")
    new_lid = _insert_like(
        "lessons", merged, section_id=new_section_id, original_id=None,
        sort_order=sort_order if sort_order is not None else (row.get("sort_order") or 0),
        test_id=copy_test(tid, job, category_id=_test_category(tid, *cats)) if tid else None,
        workbook_path=wb,
        workbook_name=wb_src.get("workbook_name") if wb else None,
        workbook_size=wb_src.get("workbook_size") if wb else None,
        video_url=dup_file(content.get("video_url"), "videos", job),
        copied_from_lesson_id=row["id"], copy_modified_at=None, legacy_lesson_id=None,
        # старые колонки цены v67 — не наследуем, цена копии в reward_prices
        reward_price=0, reward_required=1)
    job.lessons.append(new_lid)
    if real:
        copy_lesson_images(real_id, new_lid, job)
        copy_zachet_bank(real_id, new_lid, job)
    return new_lid


def _copy_section_row(sec: dict, new_subject_id: int, job: Job, sort_order=None) -> int:
    real = _row("sections", int(sc.orig_section_id(sec["id"])))
    merged = dict(real or sec)
    if sec.get("original_id"):
        for f in _SECTION_SHELL:
            if sec.get(f) is not None:
                merged[f] = sec[f]
    new_id = _insert_like(
        "sections", merged, subject_id=new_subject_id, original_id=None,
        sort_order=sort_order if sort_order is not None else (sec.get("sort_order") or 0),
        copied_from_section_id=sec["id"])
    job.sections.append(new_id)
    return new_id


def _section_lessons(section_id: int) -> list:
    return [dict(r) for r in db.fetchall(
        "SELECT * FROM lessons WHERE section_id=? ORDER BY sort_order, id", (section_id,))]


def _copy_prices(src_subject_id: int, dst_subject_id: int, lesson_map: dict, job: Job) -> None:
    """Цены вознаграждений и «обязательный» — такие же, но уже свои у копии."""
    from services import reward_service as rs
    pr = rs.prices(src_subject_id)
    rows = []
    for src_row_id, new_lid in lesson_map.items():
        p = pr.get(int(sc.orig_lesson_id(src_row_id)))
        if p:
            rows.append((dst_subject_id, new_lid, p[0], p[1], _now()))
            job.prices.append((dst_subject_id, new_lid))
    if rows:
        db.executemany("INSERT OR REPLACE INTO reward_prices (subject_id, lesson_id, price, required, "
                       "updated_at) VALUES (?,?,?,?,?)", rows)


def _copy_required_channels(src_subject_id: int, dst_subject_id: int) -> None:
    try:
        for ch in db.fetchall("SELECT * FROM required_channels WHERE subject_id=?", (src_subject_id,)):
            _insert_like("required_channels", dict(ch), subject_id=dst_subject_id)
    except Exception as e:
        log.warning("копия обязательных каналов предмета %s: %s", src_subject_id, e)


def _new_bot_category(title: str, created_by, job: Job, src_cat=None) -> Optional[int]:
    """Свой раздел в боте. У копии он всегда новый (общий раздел с оригиналом
    переименовывался бы вместе с копией), но с теми же настройками, что
    админ задал разделу оригинала в боте: значок, закрытость, обязательность
    и каналы, на которые нужно подписаться."""
    try:
        from webapp import learning as lg
        src = _row("test_categories", src_cat) or {}
        cid = _insert_like("test_categories", src, name=lg._free_category_name(title),
                           created_by=created_by or None,
                           sort_order=db.fetchone("SELECT COALESCE(MAX(sort_order),0)+1 AS n "
                                                  "FROM test_categories")["n"])
        job.categories.append(cid)
    except Exception as e:
        log.warning("раздел бота для копии «%s»: %s", title, e)
        return None
    if src_cat:
        try:
            for ch in db.fetchall("SELECT * FROM required_channels WHERE category_id=?", (src_cat,)):
                job.channels.append(_insert_like("required_channels", dict(ch), category_id=cid))
        except Exception as e:
            log.warning("каналы раздела бота для копии «%s»: %s", title, e)
    return cid


def _invalidate() -> None:
    try:
        from webapp import learning as lg
        lg.invalidate_catalog_cache()
    except Exception:
        pass
    try:
        from services import gamification as gm
        gm.invalidate()
    except Exception:
        pass


def _finish(subject_ids, lesson_ids, reset_subjects=()) -> None:
    """Приватность тестов в боте, снятие отметки «изменена» с только что
    созданного (само копирование изменением не считается), сброс кэшей."""
    try:
        from webapp import learning as lg
        for sid in subject_ids:
            lg._sync_subject_tests_privacy_sync(sid)
    except Exception as e:
        log.warning("приватность тестов копии: %s", e)
    for sid in reset_subjects:
        db.execute("UPDATE subjects SET copy_modified_at=NULL WHERE id=?", (sid,))
    ids = list(lesson_ids)
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        db.execute(f"UPDATE lessons SET copy_modified_at=NULL WHERE id IN ({','.join('?' * len(part))})",
                   tuple(part))
    _invalidate()


# ───────────────────────── точки входа ─────────────────────────

def copy_subject_full(subject_id: int, new_title: str = "", created_by=None) -> Optional[int]:
    """Независимая копия предмета целиком. Возвращает id нового предмета."""
    shell = _row("subjects", subject_id)
    if not shell:
        return None
    real = _row("subjects", int(sc.orig_subject_id(subject_id))) or shell
    merged = dict(real)
    for f in _SUBJECT_SHELL:
        if shell.get(f) is not None:
            merged[f] = shell[f]
    title = (new_title or "").strip() or f"{shell['title']} (копия)"
    now = _now()
    max_sort = db.fetchone("SELECT COALESCE(MAX(sort_order),0)+1 AS n FROM subjects")["n"]
    job = Job()
    try:
        new_sid = _insert_like(
            "subjects", merged, title=title, original_id=None, is_pinned=0, sort_order=max_sort,
            bot_category_id=None, copied_from_subject_id=shell["id"], copied_at=now,
            copy_modified_at=None, copy_group_id=None, converted_at=None,
            # новая программа: граница «до / после» вознаграждений — момент копии
            rewards_enabled_at=now if merged.get("rewards_enabled") else None,
            created_by=created_by or merged.get("created_by"))
        job.subjects.append(new_sid)
        src_cat = shell.get("bot_category_id") or real.get("bot_category_id")
        dst_cat = _new_bot_category(title, created_by or merged.get("created_by"), job, src_cat)
        if dst_cat:
            db.execute("UPDATE subjects SET bot_category_id=? WHERE id=?", (dst_cat, new_sid))
        cats = (src_cat, dst_cat)
        lesson_map = {}
        for sec in db.fetchall("SELECT * FROM sections WHERE subject_id=? ORDER BY sort_order, id",
                               (shell["id"],)):
            sec = dict(sec)
            new_sec = _copy_section_row(sec, new_sid, job)
            for les in _section_lessons(sec["id"]):
                lesson_map[les["id"]] = _copy_lesson(les, new_sec, job, cats)
        _copy_prices(shell["id"], new_sid, lesson_map, job)
        _copy_required_channels(shell["id"], new_sid)
    except Exception:
        job.rollback()
        raise
    _finish([new_sid], lesson_map.values(), reset_subjects=[new_sid])
    return new_sid


def copy_section_full(section_id: int, target_subject_id: int, created_by=None) -> Optional[int]:
    """Независимая копия раздела (со всеми уроками) в конец другого предмета."""
    sec = _row("sections", section_id)
    target = _row("subjects", target_subject_id)
    if not sec or not target:
        return None
    src_subject = _row("subjects", sec["subject_id"]) or {}
    order = db.fetchone("SELECT COALESCE(MAX(sort_order),-1)+1 AS n FROM sections WHERE subject_id=?",
                        (target_subject_id,))["n"]
    cats = (src_subject.get("bot_category_id"), target.get("bot_category_id"))
    job = Job()
    lesson_map = {}
    try:
        new_sec = _copy_section_row(sec, target_subject_id, job, sort_order=order)
        for les in _section_lessons(sec["id"]):
            lesson_map[les["id"]] = _copy_lesson(les, new_sec, job, cats)
        _copy_prices(sec["subject_id"], target_subject_id, lesson_map, job)
    except Exception:
        job.rollback()
        # Несостоявшееся копирование изменением предмета-получателя не считается
        db.execute("UPDATE subjects SET copy_modified_at=? WHERE id=?",
                   (target.get("copy_modified_at"), target_subject_id))
        raise
    _finish([target_subject_id], lesson_map.values())
    return new_sec


def copy_lesson_full(lesson_id: int, target_section_id: int, created_by=None) -> Optional[int]:
    """Независимая копия урока в конец другого раздела (любого предмета)."""
    les = _row("lessons", lesson_id)
    target_sec = _row("sections", target_section_id)
    if not les or not target_sec:
        return None
    src_sec = _row("sections", les["section_id"]) or {}
    src_subject = _row("subjects", src_sec.get("subject_id")) or {}
    target = _row("subjects", target_sec["subject_id"]) or {}
    order = db.fetchone("SELECT COALESCE(MAX(sort_order),-1)+1 AS n FROM lessons WHERE section_id=?",
                        (target_section_id,))["n"]
    cats = (src_subject.get("bot_category_id"), target.get("bot_category_id"))
    job = Job()
    try:
        new_lid = _copy_lesson(les, target_section_id, job, cats, sort_order=order)
        if src_sec.get("subject_id"):
            _copy_prices(src_sec["subject_id"], target_sec["subject_id"], {les["id"]: new_lid}, job)
    except Exception:
        job.rollback()
        db.execute("UPDATE subjects SET copy_modified_at=? WHERE id=?",
                   (target.get("copy_modified_at"), target_sec["subject_id"]))
        raise
    _finish([target_sec["subject_id"]], [new_lid])
    return new_lid


def summary(subject_id: int) -> dict:
    """Сколько чего у предмета — для сообщения админу после копирования."""
    n_sec = db.fetchone("SELECT COUNT(*) AS c FROM sections WHERE subject_id=?", (subject_id,))["c"]
    n_les = db.fetchone(
        "SELECT COUNT(*) AS c FROM lessons l JOIN sections s ON s.id=l.section_id "
        "WHERE s.subject_id=?", (subject_id,))["c"]
    n_q = db.fetchone(
        "SELECT COUNT(*) AS c FROM questions q JOIN lessons l ON l.test_id=q.test_id "
        "JOIN sections s ON s.id=l.section_id WHERE s.subject_id=?", (subject_id,))["c"]
    return {"sections": n_sec, "lessons": n_les, "questions": n_q}
