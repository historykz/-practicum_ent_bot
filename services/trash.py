"""Единая корзина сайта, бота и мини-приложения (v92).

Как устроено. Любое удаление учебного материала идёт через move(): в одной
транзакции сервер снимает полный снимок объекта (строки всех таблиц, которые
уйдут вместе с ним по связям базы, и поля, которые обнуляются), записывает его
в trash_items и только потом удаляет. Файлы объекта переезжают в закрытую папку
trash_files рядом с базой. Через 48 часов снимок и файлы стираются фоновой
задачей сайта (она же досчищает всё просроченное после перезапуска сервера).

Восстановление вставляет строки обратно с теми же номерами. Поэтому объект
встаёт ровно туда, где был (раздел, урок, порядок в списке), и к нему снова
подцепляются ссылки, которые на него указывали: результаты учеников, оплаты,
избранное, журналы. Обычные списки сайта и бота при этом не меняются: в них
удалённого объекта просто нет, пока его не вернут.

Триггеры базы (награды за первую попытку, отметки об изменении копий) на время
восстановления выключаются, чтобы возврат строк не начислил награды повторно.
"""
import base64
import json
import logging
import shutil
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import config
import database as db
import utils

log = logging.getLogger(__name__)

RETENTION_HOURS = 48
ALMATY = timezone(timedelta(hours=5))

KINDS = {
    "subject": "Предмет",
    "section": "Раздел",
    "lesson": "Урок",
    "conspect": "Конспект",
    "test": "Тест",
    "question": "Вопрос",
    "interactive": "Интерактивное задание",
    "block": "Блок урока",
    "image": "Страница конспекта",
    "workbook": "Рабочая тетрадь",
    "music": "Музыка",
    "note": "Конспект ЕНТ (бот)",
    "category": "Раздел каталога (бот)",
    "asset": "Картинка задания",
    "video": "Видео урока",
    "zachet": "Банк вопросов зачёта",
    "premium": "Premium ученика",
    "results": "Результаты тестов",
    "ad": "Реклама после тестов",
}

# Одинаковые слова на сайте и в боте
ASK_MSG = "Переместить объект в корзину? Вы сможете восстановить его в течение 48 часов."
MOVED_MSG = "Объект перемещён в корзину. Он будет автоматически удалён через 48 часов."
SOURCES = {"site": "сайт", "bot": "бот", "miniapp": "мини-приложение", "api": "API"}

# Куда можно вернуть объект, если его прежнее место удалено навсегда:
# таблица корня, колонка-родитель, таблица родителя, колонка порядка
NEW_PLACE = {
    "lessons": ("section_id", "sections", "sort_order"),
    "sections": ("subject_id", "subjects", "sort_order"),
    "lesson_blocks": ("lesson_id", "lessons", "sort_order"),
    "lesson_images": ("lesson_id", "lessons", "sort_order"),
    "questions": ("test_id", "tests", "order_num"),
}

# Ссылки без объявленной связи в базе: при восстановлении с новым номером их тоже
# надо поправить (номер теста урока, теста домашнего задания и т. п.)
_IMPLICIT = {"test_id": "tests", "lesson_id": "lessons", "section_id": "sections",
             "subject_id": "subjects", "question_id": "questions", "task_id": "interactive_tasks",
             "note_id": "notes", "category_id": "test_categories"}


def _enc(v):
    """Значение строки для JSON: двоичные данные — base64."""
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"__b64__": base64.b64encode(bytes(v)).decode()}
    return v


def _dec(v):
    if isinstance(v, dict) and "__b64__" in v:
        return base64.b64decode(v["__b64__"])
    return v


# Для этих видов «другое место» не имеет смысла: они возвращаются в тот же урок
NO_NEW_PLACE = {"conspect", "workbook", "video", "zachet", "test", "block_text"}


def _root_dir() -> Path:
    return Path(config.DB_PATH).resolve().parent


def _trash_dir(item_id: int) -> Path:
    return _root_dir() / "trash_files" / str(int(item_id))


def _now() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# ───────────────────────── связи между таблицами ─────────────────────────

_fk_cache = {}


def _fk_maps(conn) -> tuple:
    """children[parent] = [(child, child_col, parent_col, on_delete)], parents[child] = [(col, parent, parent_col)]"""
    if _fk_cache.get("v"):
        return _fk_cache["v"]
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
    children, parents = {}, {}
    for t in tables:
        for fk in conn.execute(f'PRAGMA foreign_key_list("{t}")').fetchall():
            parent, child_col, parent_col, on_delete = fk[2], fk[3], fk[4] or "id", (fk[6] or "NO ACTION").upper()
            children.setdefault(parent, []).append((t, child_col, parent_col, on_delete))
            parents.setdefault(t, []).append((child_col, parent, parent_col))
    _fk_cache["v"] = (children, parents, set(tables))
    return _fk_cache["v"]


def _cols(conn, table: str) -> list:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]


def _pk_cols(conn, table: str) -> list:
    info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    pk = sorted([(r[5], r[1]) for r in info if r[5]])
    return [c for _, c in pk]


# ───────────────────────── файлы объектов ─────────────────────────

def _file_specs():
    """(таблица, колонка) -> как найти файл. Пути — относительно папки базы."""
    def rel(v):
        v = (v or "").strip().lstrip("/")
        return v or None

    def under(sub):
        return lambda v: f"uploads/{sub}/{v.strip()}" if (v or "").strip() and "/" not in v.strip() else rel(v)

    return {
        ("lesson_images", "image_path"): lambda v, row: (rel(v) if "/" in (v or "") else
                                                       (f"uploads/lessons/{v.strip()}" if (v or "").strip() else None))
        if (row.get("storage") or "disk") == "disk" else None,
        ("lesson_blocks", "image_path"): lambda v, row: under("blocks")(v),
        ("lessons", "workbook_path"): lambda v, row: under("workbooks")(v),
        ("lessons", "video_url"): lambda v, row: rel(v) if (v or "").startswith("/uploads/videos/") else None,
        ("questions", "web_image_path"): lambda v, row: rel(v) if "/" in (v or "") else under("questions")(v),
        ("question_options", "image_path"): lambda v, row: rel(v) if (v or "").startswith("/uploads/questions/") else None,
        ("interactive_assets", "stored_name"): lambda v, row: under("interactive")(v),
        ("music_tracks", "filename"): lambda v, row: under("music")(v),
        ("ads", "file_name"): lambda v, row: under("ads")(v),
        ("interactive_tasks", "files_key"): lambda v, row: under("interactive/pkg")(v),
        ("interactive_tasks", "pending_files_key"): lambda v, row: under("interactive/pkg")(v),
        ("interactive_tasks", "prev_files_key"): lambda v, row: under("interactive/pkg")(v),
    }


def _shared(conn, table: str, col: str, value, exclude_rowids: list) -> bool:
    """Тот же файл нужен живой строке, которая не удаляется?"""
    cols = [col]
    if table == "interactive_tasks":
        cols = ["files_key", "pending_files_key", "prev_files_key"]
    for c in cols:
        q = f'SELECT rowid FROM "{table}" WHERE "{c}"=?'
        for r in conn.execute(q, (value,)).fetchall():
            if r[0] not in exclude_rowids:
                return True
    return False


def _collect_files(conn, snap_rows: dict) -> list:
    specs = _file_specs()
    out, seen = [], set()
    for (table, col), fn in specs.items():
        if table not in snap_rows:
            continue
        cols, rows = snap_rows[table]["cols"], snap_rows[table]["rows"]
        if col not in cols:
            continue
        rowids = [r[0] for r in rows]
        for r in rows:
            row = dict(zip(cols, r[1:]))
            v = row.get(col)
            if not v:
                continue
            try:
                relp = fn(v, row)
            except Exception:
                relp = None
            if not relp or relp in seen or ".." in relp:
                continue
            p = _root_dir() / relp
            if not p.exists():
                continue
            if _shared(conn, table, col, v, rowids):
                continue
            seen.add(relp)
            out.append({"rel": relp, "dir": p.is_dir(), "moved": False})
    return out


def _size(p: Path) -> int:
    try:
        if p.is_dir():
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        return p.stat().st_size
    except OSError:
        return 0


# ───────────────────────── снимок перед удалением ─────────────────────────

def _capture(conn, roots: list, extra: list) -> tuple:
    """Строки корней и всё, что уйдёт с ними каскадом. Порядок — от родителя к детям."""
    children, _, tables = _fk_maps(conn)
    order, data, seen = [], {}, set()

    def add(table, where, params):
        if table not in tables:
            return
        rows = conn.execute(f'SELECT rowid AS __rowid__, * FROM "{table}" WHERE {where}', params).fetchall()
        new = [r for r in rows if (table, r[0]) not in seen]
        if not new:
            return
        cols = list(new[0].keys())[1:]
        if table not in data:
            data[table] = {"cols": cols, "rows": []}
            order.append(table)
        for r in new:
            seen.add((table, r[0]))
            data[table]["rows"].append([r[0]] + [_enc(r[k]) for k in cols])
        for child, ccol, pcol, on_delete in children.get(table, []):
            if on_delete != "CASCADE":
                continue
            vals = sorted({r[pcol] for r in new if r[pcol] is not None})
            for i in range(0, len(vals), 500):
                part = vals[i:i + 500]
                add(child, f'"{ccol}" IN ({",".join("?" * len(part))})', part)

    for table, rid in roots:
        add(table, "rowid=?", (int(rid),))
    for table, where, params in extra:
        add(table, where, params)
    return order, data


def _row_title(conn, table: str, rid) -> str:
    try:
        r = conn.execute(f'SELECT * FROM "{table}" WHERE rowid=?', (rid,)).fetchone()
    except Exception:
        return ""
    if not r:
        return ""
    keys = r.keys()
    for k in ("title", "name", "text", "question"):
        if k in keys and r[k]:
            return str(r[k])
    return ""


def move(kind: str, title: str, actor: Optional[int], source: str, roots: list, *,
         extra: tuple = (), updates: tuple = (), meta: Optional[dict] = None,
         actor_name: str = "", group_id: str = "", note: str = "") -> int:
    """Переместить объект в корзину и удалить его из живых таблиц.

    roots   — [(таблица, rowid)] — что удаляется (вместе со всем, что уйдёт каскадом);
    extra   — [(таблица, условие, параметры)] — строки, которые удаляются вручную
              помимо каскада (например, прогресс учеников по конспекту бота);
    updates — [(таблица, rowid, {колонка: новое значение})] — поля, которые
              обнуляются (конспект урока, привязка теста к уроку), их прежние
              значения сохраняются и возвращаются при восстановлении;
    meta    — subject_id, subject_title, location, parent_table, parent_id.
    Возвращает номер записи в корзине. При ошибке ничего не удаляется."""
    meta = meta or {}
    now = _now()
    if actor and not actor_name:
        u = db.fetchone("SELECT username, first_name FROM users WHERE tg_id=?", (actor,))
        if u:
            actor_name = ("@" + u["username"]) if u["username"] else (u["first_name"] or "")
    with db.db_lock() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            order, data = _capture(conn, roots, list(extra))
            upd = []
            for table, rid, new_vals in updates:
                r = conn.execute(f'SELECT * FROM "{table}" WHERE rowid=?', (int(rid),)).fetchone()
                if not r:
                    continue
                old = {c: _enc(r[c]) for c in new_vals}
                upd.append({"table": table, "rowid": int(rid), "old": old, "new": new_vals})
            if not order and not upd:
                conn.execute("ROLLBACK")
                return 0
            files = _collect_files(conn, data)
            # Файлы, на которые указывают обнуляемые поля (рабочая тетрадь урока)
            specs = _file_specs()
            for u in upd:
                for col, oldv in u["old"].items():
                    fn = specs.get((u["table"], col))
                    if fn and oldv:
                        relp = fn(oldv, u["old"])
                        if relp and (_root_dir() / relp).exists() and not any(f["rel"] == relp for f in files):
                            files.append({"rel": relp, "dir": (_root_dir() / relp).is_dir(), "moved": False})
            snapshot = {"v": 1, "order": order, "rows": data, "updates": upd, "files": files,
                        "roots": [[t, int(r)] for t, r in roots]}
            rows_count = sum(len(data[t]["rows"]) for t in order)
            size = sum(_size(_root_dir() / f["rel"]) for f in files)
            cur = conn.execute(
                "INSERT INTO trash_items (kind, title, subject_id, subject_title, location, root_table, root_id, "
                "parent_table, parent_id, group_id, deleted_by, deleted_by_name, source, deleted_at, purge_at, "
                "status, snapshot, rows_count, files_count, size_bytes, note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'trashed', ?,?,?,?,?)",
                (kind, (title or KINDS.get(kind, kind))[:300], meta.get("subject_id"), meta.get("subject_title"),
                 (meta.get("location") or "")[:500], roots[0][0] if roots else None,
                 int(roots[0][1]) if roots else None,
                 meta.get("parent_table"), meta.get("parent_id"), group_id or None, actor, actor_name or None,
                 source, _iso(now), _iso(now + timedelta(hours=RETENTION_HOURS)),
                 json.dumps(snapshot, ensure_ascii=False, default=str), rows_count, len(files), size, note or None))
            item_id = cur.lastrowid
            for u in upd:
                sets = ", ".join(f'"{c}"=?' for c in u["new"])
                conn.execute(f'UPDATE "{u["table"]}" SET {sets} WHERE rowid=?',
                             list(u["new"].values()) + [u["rowid"]])
            for table, where, params in extra:
                conn.execute(f'DELETE FROM "{table}" WHERE {where}', params)
            for table, rid in roots:
                conn.execute(f'DELETE FROM "{table}" WHERE rowid=?', (int(rid),))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    _move_files_in(item_id, snapshot)
    audit(actor, actor_name, source, "delete", kind, title, item_id,
          (meta.get("location") or "") + (f" · {note}" if note else ""))
    _changed()
    return item_id


def _changed() -> None:
    """Списки уроков на сайте кешируются: удаление из бота должно быть видно сразу."""
    try:
        from webapp import learning as _l
        _l.invalidate_catalog_cache()
    except Exception:
        pass


def _move_files_in(item_id: int, snapshot: dict) -> None:
    changed = False
    for f in snapshot["files"]:
        src = _root_dir() / f["rel"]
        dst = _trash_dir(item_id) / f["rel"]
        try:
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                f["moved"] = True
                changed = True
        except OSError as e:
            log.warning("trash: файл %s не перенесён: %s", f["rel"], e)
    if changed:
        db.execute("UPDATE trash_items SET snapshot=? WHERE id=?",
                   (json.dumps(snapshot, ensure_ascii=False, default=str), item_id))


# ───────────────────────── журнал ─────────────────────────

def audit(actor, actor_name, source, action, kind, title, item_id=None, details="") -> None:
    try:
        if actor and not actor_name:
            u = db.fetchone("SELECT username, first_name FROM users WHERE tg_id=?", (actor,))
            if u:
                actor_name = ("@" + u["username"]) if u.get("username") else (u.get("first_name") or "")
        db.execute("INSERT INTO admin_audit (ts, actor_tg, actor_name, source, action, kind, title, item_id, details) "
                   "VALUES (?,?,?,?,?,?,?,?,?)",
                   (utils.now_iso(), actor, actor_name or None, source, action, kind, (title or "")[:300],
                    item_id, (details or "")[:1000]))
    except Exception as e:
        log.warning("audit: %s", e)


# ───────────────────────── восстановление ─────────────────────────

class RestoreError(Exception):
    def __init__(self, message: str, code: str = "", parent: Optional[tuple] = None):
        super().__init__(message)
        self.code = code
        self.parent = parent


def _load(item_id: int) -> Optional[dict]:
    r = db.fetchone("SELECT * FROM trash_items WHERE id=?", (int(item_id),))
    return dict(r) if r else None


def _snap(item: dict) -> dict:
    return json.loads(item.get("snapshot") or "{}") or {}


def _exists(conn, table: str, col: str, value) -> bool:
    try:
        return conn.execute(f'SELECT 1 FROM "{table}" WHERE "{col}"=? LIMIT 1', (value,)).fetchone() is not None
    except sqlite3.Error:
        return False


def _missing_parents(conn, snap: dict) -> list:
    """Родители корней, которых нет ни в базе, ни в самом снимке: [(таблица, колонка, номер)]."""
    _, parents, _ = _fk_maps(conn)
    roots = {(t, int(r)) for t, r in snap.get("roots", [])}
    missing = []
    for t in snap["order"]:
        cols, rows = snap["rows"][t]["cols"], snap["rows"][t]["rows"]
        for r in rows:
            if (t, r[0]) not in roots:
                continue
            for col, ptable, pcol in parents.get(t, []):
                if col not in cols:
                    continue
                v = r[cols.index(col) + 1]
                if v is None:
                    continue
                in_snap = False
                if ptable in snap["rows"] and pcol in snap["rows"][ptable]["cols"]:
                    j = snap["rows"][ptable]["cols"].index(pcol) + 1
                    in_snap = any(x[j] == v for x in snap["rows"][ptable]["rows"])
                if not in_snap and not _exists(conn, ptable, pcol, v):
                    missing.append((ptable, pcol, v))
    for u in snap.get("updates", []):
        if not conn.execute(f'SELECT 1 FROM "{u["table"]}" WHERE rowid=?', (u["rowid"],)).fetchone():
            missing.append((u["table"], "id", u["rowid"]))
    return sorted(set(missing))


def _find_parent_item(table: str, col: str, value) -> Optional[int]:
    """Запись корзины, в снимке которой есть нужный родитель."""
    cands = db.fetchall("SELECT id, snapshot FROM trash_items WHERE status='trashed' AND purge_at>? "
                        "AND snapshot LIKE ? ORDER BY id DESC", (utils.now_iso(), f'%"{table}"%'))
    for c in cands:
        try:
            s = json.loads(c["snapshot"])
            part = s["rows"].get(table)
            if not part:
                continue
            k = part["cols"].index(col) + 1
            if any(r[k] == value for r in part["rows"]):
                return c["id"]
        except Exception:
            continue
    return None


def _insert_rows(conn, snap: dict, new_place: Optional[tuple]) -> tuple:
    """Вставить строки снимка. Возвращает (карта новых номеров, сколько строк пропущено)."""
    _, parents, _ = _fk_maps(conn)
    remap, skipped = {}, 0
    for t in snap["order"]:
        cols, rows = snap["rows"][t]["cols"], snap["rows"][t]["rows"]
        pk = _pk_cols(conn, t)
        int_pk = pk == ["id"] and "id" in cols
        fk_cols = {c: (p, pc) for c, p, pc in parents.get(t, [])}
        for r in rows:
            vals = {c: _dec(v) for c, v in zip(cols, r[1:])}
            # ссылки на строки, которые получили новые номера
            for c in list(vals):
                target = fk_cols.get(c, (_IMPLICIT.get(c), "id"))[0]
                if target and (target, vals[c]) in remap:
                    vals[c] = remap[(target, vals[c])]
            if new_place and t == snap.get("root_table") and r[0] == snap.get("root_rowid"):
                pcol, ptable, ocol, pid = new_place
                vals[pcol] = pid
                if ocol in vals:
                    mx = conn.execute(f'SELECT COALESCE(MAX("{ocol}"),0) FROM "{t}" WHERE "{pcol}"=?', (pid,)).fetchone()[0]
                    vals[ocol] = (mx or 0) + 1
            # Родителя нет (удалён навсегда) — эта связь не возвращается
            orphan = False
            for c, (ptable, pcol) in fk_cols.items():
                if vals.get(c) is not None and not _exists(conn, ptable, pcol, vals[c]):
                    orphan = True
                    break
            if orphan:
                skipped += 1
                continue
            names = ", ".join(f'"{c}"' for c in vals)
            marks = ", ".join("?" * len(vals))
            try:
                conn.execute(f'INSERT INTO "{t}" ({names}) VALUES ({marks})', list(vals.values()))
            except sqlite3.IntegrityError:
                if int_pk:
                    cur = conn.execute(f'SELECT * FROM "{t}" WHERE id=?', (vals["id"],)).fetchone()
                    if cur and all(cur[c] == vals[c] for c in vals if c in cur.keys()):
                        continue                    # уже на месте (повторное восстановление)
                    if cur:                         # номер занят другим объектом: даём новый
                        old = vals.pop("id")
                        names = ", ".join(f'"{c}"' for c in vals)
                        c2 = conn.execute(f'INSERT OR IGNORE INTO "{t}" ({names}) VALUES ({", ".join("?" * len(vals))})',
                                          list(vals.values()))
                        if c2.rowcount:
                            remap[(t, old)] = c2.lastrowid
                        else:
                            skipped += 1
                        continue
                c3 = conn.execute(f'INSERT OR IGNORE INTO "{t}" ({names}) VALUES ({marks})', list(vals.values()))
                if not c3.rowcount:
                    skipped += 1
    return remap, skipped


def _apply_updates(conn, snap: dict) -> list:
    """Вернуть обнулённые поля. Если на месте уже новое значение — не трогаем и сообщаем."""
    conflicts = []
    for u in snap.get("updates", []):
        r = conn.execute(f'SELECT * FROM "{u["table"]}" WHERE rowid=?', (u["rowid"],)).fetchone()
        if not r:
            conflicts.append("объекта, куда возвращать, больше нет")
            continue
        busy = [c for c in u["old"] if r[c] not in (None, "", 0) and r[c] != u["new"].get(c)]
        if busy:
            conflicts.append("на этом месте уже новое содержимое")
            continue
        sets = ", ".join(f'"{c}"=?' for c in u["old"])
        conn.execute(f'UPDATE "{u["table"]}" SET {sets} WHERE rowid=?',
                     [_dec(v) for v in u["old"].values()] + [u["rowid"]])
    return conflicts


def restore(item_id: int, actor: Optional[int], *, new_parent: Optional[int] = None,
            source: str = "site", _depth: int = 0) -> dict:
    """Вернуть объект из корзины на прежнее место. Повторный вызов ничего не дублирует."""
    item = _load(item_id)
    if not item:
        raise RestoreError("Запись корзины не найдена.")
    if item["status"] == "restored":
        return {"ok": True, "already": True, "title": item["title"], "parents": []}
    if item["status"] != "trashed":
        raise RestoreError("Объект уже удалён навсегда.", "purged")
    if (item.get("purge_at") or "") <= utils.now_iso():
        raise RestoreError("Срок хранения истёк: объект будет удалён навсегда.", "expired")
    snap = _snap(item)
    snap["root_table"] = item["root_table"]
    snap["root_rowid"] = item["root_id"]
    restored_parents = []
    new_place = None
    if new_parent:
        spec = NEW_PLACE.get(item["root_table"] or "")
        if not spec or item["kind"] in NO_NEW_PLACE:
            raise RestoreError("Для этого объекта нельзя выбрать другое место.")
        pcol, ptable, ocol = spec
        if not db.fetchone(f'SELECT 1 FROM "{ptable}" WHERE id=?', (int(new_parent),)):
            raise RestoreError("Выбранное место не найдено.")
        new_place = (pcol, ptable, ocol, int(new_parent))
    with db.db_lock() as conn:
        missing = _missing_parents(conn, snap)
    if new_place:
        missing = [m for m in missing if m[0] != new_place[1]]
    for ptable, pcol, value in missing:
        pid = _find_parent_item(ptable, pcol, value)
        if pid and pid != item_id and _depth < 6:
            res = restore(pid, actor, source=source, _depth=_depth + 1)
            restored_parents.append(res.get("title") or "")
            restored_parents.extend(res.get("parents") or [])
        else:
            what = {"sections": "раздел", "subjects": "предмет", "lessons": "урок", "tests": "тест",
                    "interactive_tasks": "интерактивное задание", "notes": "конспект"}.get(ptable, ptable)
            spec = NEW_PLACE.get(item["root_table"] or "")
            if spec and spec[1] == ptable and item["kind"] not in NO_NEW_PLACE:
                raise RestoreError(f"Прежнее место ({what}) удалено навсегда. Выберите, куда вернуть.",
                                   "parent_missing", (ptable, value))
            raise RestoreError(f"Нельзя вернуть: связанный объект ({what}) удалён навсегда.", "gone", (ptable, value))
    with db.db_lock() as conn:
        conn.execute("BEGIN IMMEDIATE")
        saved = []
        try:
            cur_item = conn.execute("SELECT status FROM trash_items WHERE id=?", (item_id,)).fetchone()
            if cur_item["status"] != "trashed":
                conn.execute("ROLLBACK")
                return {"ok": True, "already": True, "title": item["title"], "parents": restored_parents}
            touched = set(snap["order"]) | {u["table"] for u in snap.get("updates", [])}
            saved = conn.execute(
                f"SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name IN "
                f"({','.join('?' * len(touched))})", sorted(touched)).fetchall() if touched else []
            for name, _ in saved:
                conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
            _, skipped = _insert_rows(conn, snap, new_place)
            conflicts = _apply_updates(conn, snap)
            if conflicts and not snap["order"]:
                raise RestoreError("Не восстановлено: " + conflicts[0] + ".", "conflict")
            for _, sql in saved:
                conn.execute(sql)
            conn.execute("UPDATE trash_items SET status='restored', restored_at=?, restored_by=? WHERE id=?",
                         (utils.now_iso(), actor, item_id))
            conn.execute("COMMIT")
        except RestoreError:
            conn.execute("ROLLBACK")
            raise
        except sqlite3.Error as e:
            conn.execute("ROLLBACK")
            log.exception("trash restore %s", item_id)
            raise RestoreError(f"Не удалось восстановить: {e}", "db")
    _move_files_out(item_id, snap)
    _changed()
    if item["kind"] == "premium":
        _premium_back(snap)
    audit(actor, "", source, "restore", item["kind"], item["title"], item_id,
          (item.get("location") or "") + (" · новое место" if new_place else ""))
    return {"ok": True, "title": item["title"], "parents": restored_parents,
            "conflicts": conflicts if snap.get("updates") else [], "skipped": skipped}


def _premium_back(snap: dict) -> None:
    """Premium вернулся: вознаграждение по предметам пересчитывается, как при отзыве."""
    try:
        part = snap["rows"]["premium_users"]
        uid = part["rows"][0][part["cols"].index("user_id") + 1]
        u = db.fetchone("SELECT tg_id FROM users WHERE id=?", (uid,))
        if u:
            from services import reward_service as _rs
            _rs.check_user(u["tg_id"])
    except Exception as e:
        log.warning("trash premium: %s", e)


def _move_files_out(item_id: int, snap: dict) -> None:
    for f in snap.get("files", []):
        if not f.get("moved"):
            continue
        src = _trash_dir(item_id) / f["rel"]
        dst = _root_dir() / f["rel"]
        try:
            if src.exists() and not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
        except OSError as e:
            log.warning("trash: файл %s не возвращён: %s", f["rel"], e)
    shutil.rmtree(_trash_dir(item_id), ignore_errors=True)


def purge_many(ids: list, actor: Optional[int], source: str = "site") -> int:
    return sum(1 for i in ids if purge(int(i), actor, "manual", source))


def restore_many(ids: list, actor: Optional[int], source: str = "site") -> dict:
    """Восстановить несколько: сначала более поздние удаления (раздел раньше своих уроков)."""
    items = [_load(i) for i in ids]
    items = sorted([i for i in items if i], key=lambda x: x["id"], reverse=True)
    ok, errors, already = [], [], 0
    for it in items:
        try:
            res = restore(it["id"], actor, source=source)
            if res.get("already"):
                already += 1
            else:
                ok.append(it["title"])
        except RestoreError as e:
            errors.append({"id": it["id"], "title": it["title"], "error": str(e), "code": e.code})
    return {"ok": ok, "errors": errors, "already": already}


# ───────────────────────── удаление навсегда ─────────────────────────

def purge(item_id: int, actor: Optional[int], reason: str = "manual", source: str = "site") -> bool:
    item = _load(item_id)
    if not item or item["status"] != "trashed":
        return False
    snap = _snap(item)
    # Файлы, которые не удалось перенести в корзину, но строк на них больше нет
    for f in snap.get("files", []):
        if f.get("moved"):
            continue
        p = _root_dir() / f["rel"]
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()
        except OSError:
            pass
    shutil.rmtree(_trash_dir(item_id), ignore_errors=True)
    db.execute("UPDATE trash_items SET status='purged', snapshot=NULL, purged_at=?, purged_by=?, purge_reason=? "
               "WHERE id=? AND status='trashed'", (utils.now_iso(), actor, reason, item_id))
    audit(actor, "" if actor else "автоочистка", source if actor else "server",
          "purge" if reason == "manual" else "auto_purge", item["kind"], item["title"], item_id,
          item.get("location") or "")
    return True


def purge_expired() -> int:
    """Стереть всё, у чего истёк срок. Вызывается фоновой задачей и при старте сервера."""
    rows = db.fetchall("SELECT id FROM trash_items WHERE status='trashed' AND purge_at<=? ORDER BY id",
                       (utils.now_iso(),))
    n = 0
    for r in rows:
        try:
            if purge(r["id"], None, reason="auto"):
                n += 1
        except Exception as e:
            log.warning("trash purge %s: %s", r["id"], e)
    return n


# ───────────────────────── права ─────────────────────────

PERMS = {
    "delete": "Удалять материалы (в корзину)",
    "trash_view": "Видеть корзину",
    "trash_restore_own": "Восстанавливать своё",
    "trash_restore_any": "Восстанавливать чужое",
    "trash_mass": "Восстанавливать всё сразу",
    "trash_purge": "Удалять навсегда",
    "trash_history": "Смотреть журнал удалений",
}


def perms(tg_id: Optional[int]) -> dict:
    """Права человека на корзину. Владелец может всё."""
    if not tg_id:
        return {k: False for k in PERMS} | {"subjects": []}
    if utils.is_owner(tg_id):
        return {k: True for k in PERMS} | {"subjects": [], "owner": True}
    row = db.fetchone("SELECT * FROM site_admins WHERE tg_id=?", (tg_id,))
    if not row:
        return {k: False for k in PERMS} | {"subjects": []}
    row = dict(row)
    out = {}
    for k in PERMS:
        v = row.get("perm_" + k)
        out[k] = True if (v is None and k in ("delete", "trash_view", "trash_restore_own")) else bool(v)
    out["subjects"] = [int(x) for x in str(row.get("trash_subjects") or "").replace(" ", "").split(",") if x.isdigit()]
    return out


def can_delete(tg_id: Optional[int]) -> bool:
    """Удалять (в корзину) могут все, кто мог удалять раньше, пока владелец не снял это право."""
    if not tg_id or utils.is_owner(tg_id):
        return True
    row = db.fetchone("SELECT perm_delete FROM site_admins WHERE tg_id=?", (tg_id,))
    return True if not row or row["perm_delete"] is None else bool(row["perm_delete"])


def visible_to(item: dict, p: dict) -> bool:
    if not p.get("trash_view"):
        return False
    if p.get("subjects") and item.get("subject_id") not in p["subjects"]:
        return False
    return True


def may_restore(item: dict, tg_id: int, p: dict) -> bool:
    if not visible_to(item, p):
        return False
    if p.get("trash_restore_any"):
        return True
    return bool(p.get("trash_restore_own")) and item.get("deleted_by") == tg_id


# ───────────────────────── список для страницы корзины ─────────────────────────

def _fmt_local(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).astimezone(ALMATY)
        return dt.strftime("%d.%m.%Y, %H:%M")
    except Exception:
        return iso or ""


def left_text(purge_at: str) -> str:
    try:
        left = datetime.fromisoformat(purge_at) - _now()
    except Exception:
        return ""
    s = int(left.total_seconds())
    if s <= 0:
        return "удаляется"
    d, h, m = s // 86400, (s % 86400) // 3600, (s % 3600) // 60
    if d:
        return f"{d} д {h} ч"
    if h:
        return f"{h} ч {m} мин"
    return f"{max(m, 1)} мин"


def list_items(tg_id: int, *, q: str = "", kind: str = "", subject: str = "", who: str = "",
               day: str = "", status: str = "trashed", limit: int = 500) -> list:
    p = perms(tg_id)
    where, args = ["status=?"], [status]
    if status == "trashed":
        where.append("purge_at>?")
        args.append(utils.now_iso())
    if q:
        where.append("(title LIKE ? OR location LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if kind:
        where.append("kind=?")
        args.append(kind)
    if subject.isdigit():
        where.append("subject_id=?")
        args.append(int(subject))
    if who.isdigit():
        where.append("deleted_by=?")
        args.append(int(who))
    if day:
        where.append("substr(deleted_at,1,10)=?")
        args.append(day)
    rows = db.fetchall(
        "SELECT id, kind, title, subject_id, subject_title, location, root_table, deleted_by, deleted_by_name, "
        "source, deleted_at, purge_at, rows_count, files_count, size_bytes, status, restored_at, purged_at, "
        "purge_reason, note FROM trash_items WHERE " + " AND ".join(where) + " ORDER BY id DESC LIMIT ?",
        args + [limit])
    out = []
    for r in rows:
        it = dict(r)
        if not visible_to(it, p):
            continue
        it["kind_title"] = KINDS.get(it["kind"], it["kind"])
        it["source_title"] = SOURCES.get(it["source"] or "", it["source"] or "")
        it["deleted_local"] = _fmt_local(it["deleted_at"])
        it["purge_local"] = _fmt_local(it["purge_at"])
        it["left"] = left_text(it["purge_at"])
        it["can_restore"] = may_restore(it, tg_id, p)
        it["new_place"] = (it["root_table"] or "") in NEW_PLACE and it["kind"] not in NO_NEW_PLACE
        out.append(it)
    return out


def filters_ctx() -> dict:
    subjects = db.fetchall("SELECT DISTINCT subject_id, subject_title FROM trash_items "
                           "WHERE status='trashed' AND subject_id IS NOT NULL ORDER BY subject_title")
    people = db.fetchall("SELECT DISTINCT deleted_by, deleted_by_name FROM trash_items "
                         "WHERE status='trashed' AND deleted_by IS NOT NULL")
    return {"subjects": [dict(s) for s in subjects], "people": [dict(p) for p in people],
            "kinds": KINDS}


def places_for(root_table: str) -> list:
    """Куда можно вернуть объект, если его прежнее место удалено навсегда."""
    spec = NEW_PLACE.get(root_table)
    if not spec:
        return []
    ptable = spec[1]
    if ptable == "subjects":
        return [{"id": r["id"], "title": r["title"]} for r in
                db.fetchall("SELECT id, title FROM subjects ORDER BY sort_order, id")]
    if ptable == "sections":
        return [{"id": r["id"], "title": f"{r['st']} › {r['title']}"} for r in db.fetchall(
            "SELECT s.id, s.title, sb.title AS st FROM sections s JOIN subjects sb ON sb.id=s.subject_id "
            "ORDER BY sb.sort_order, sb.id, s.sort_order, s.id")]
    if ptable == "lessons":
        return [{"id": r["id"], "title": f"{r['st']} › {r['sct']} › {r['title']}"} for r in db.fetchall(
            "SELECT l.id, l.title, s.title AS sct, sb.title AS st FROM lessons l JOIN sections s ON s.id=l.section_id "
            "JOIN subjects sb ON sb.id=s.subject_id ORDER BY sb.sort_order, sb.id, s.sort_order, s.id, l.sort_order, l.id")]
    if ptable == "tests":
        return [{"id": r["id"], "title": r["title"]} for r in db.fetchall(
            "SELECT id, title FROM tests ORDER BY id DESC LIMIT 1000")]
    return []


def journal(limit: int = 300, item_id: Optional[int] = None) -> list:
    if item_id:
        rows = db.fetchall("SELECT * FROM admin_audit WHERE item_id=? ORDER BY id DESC", (item_id,))
    else:
        rows = db.fetchall("SELECT * FROM admin_audit ORDER BY id DESC LIMIT ?", (limit,))
    out = []
    names = {"delete": "в корзину", "restore": "восстановлено", "purge": "удалено навсегда",
             "auto_purge": "автоочистка через 48 ч", "restore_failed": "не восстановлено",
             "login_ok": "вход владельца", "login_fail": "неудачный вход", "password_changed": "смена пароля",
             "sessions_ended": "завершены другие сеансы", "settings": "настройки",
             "block": "блокировка", "unblock": "разблокировка", "reset_sessions": "сброс сеансов",
             "premium": "Premium выдан", "premium_revoke": "Premium отозван", "subject_grant": "выдан предмет",
             "subject_revoke": "снят предмет", "test_grant": "выдан тест", "test_revoke": "снят тест",
             "link_revoke": "отозвана ссылка", "site_add": "добавлен админ сайта", "site_remove": "убран админ сайта",
             "site_perms": "изменены права", "bot_add": "добавлен админ бота", "bot_level": "изменён уровень",
             "bot_remove": "убран админ бота", "broadcast": "рассылка", "export": "выгрузка",
             "create": "создано", "save": "изменено", "ia_access": "доступ к интерактиву",
             "ia_grant": "выдан интерактив", "ia_revoke": "снят интерактив"}
    for r in rows:
        d = dict(r)
        d["ts_local"] = _fmt_local(d["ts"])
        d["action_title"] = names.get(d["action"], d["action"])
        d["kind_title"] = KINDS.get(d.get("kind") or "", d.get("kind") or "")
        d["source_title"] = SOURCES.get(d.get("source") or "", d.get("source") or "")
        out.append(d)
    return out


def count_trashed(tg_id: int) -> int:
    return len(list_items(tg_id, limit=2000))


# ───────────────────────── описание места для людей ─────────────────────────

def lesson_place(lesson_id: int) -> dict:
    r = db.fetchone("SELECT l.title AS lt, s.id AS sid, s.title AS st, sb.id AS subj, sb.title AS subt "
                    "FROM lessons l JOIN sections s ON s.id=l.section_id JOIN subjects sb ON sb.id=s.subject_id "
                    "WHERE l.id=?", (int(lesson_id),))
    if not r:
        return {}
    return {"subject_id": r["subj"], "subject_title": r["subt"],
            "location": f"{r['subt']} › {r['st']} › {r['lt']}", "lesson_title": r["lt"]}


def section_place(section_id: int) -> dict:
    r = db.fetchone("SELECT s.title AS st, sb.id AS subj, sb.title AS subt FROM sections s "
                    "JOIN subjects sb ON sb.id=s.subject_id WHERE s.id=?", (int(section_id),))
    if not r:
        return {}
    return {"subject_id": r["subj"], "subject_title": r["subt"], "location": f"{r['subt']} › {r['st']}"}


def test_place(test_id: int) -> dict:
    l = db.fetchone("SELECT id FROM lessons WHERE test_id=?", (int(test_id),))
    if l:
        p = lesson_place(l["id"])
        if p:
            p["location"] = p["location"] + " · тест урока"
            return p
    t = db.fetchone("SELECT title, subject FROM tests WHERE id=?", (int(test_id),))
    return {"location": f"Тест «{t['title']}»" if t else "", "subject_title": (t["subject"] if t else "") or None}


# ───────────────────────── готовые операции для сайта и бота ─────────────────────────

def _lesson_test_roots(lesson_ids: list) -> list:
    if not lesson_ids:
        return []
    rows = db.fetchall(f"SELECT DISTINCT test_id FROM lessons WHERE id IN ({','.join('?' * len(lesson_ids))}) "
                       "AND test_id IS NOT NULL", lesson_ids)
    return [("tests", r["test_id"]) for r in rows
            if db.fetchone("SELECT 1 FROM tests WHERE id=?", (r["test_id"],))]


def trash_subject(subject_id: int, actor, source="site") -> int:
    s = db.fetchone("SELECT title FROM subjects WHERE id=?", (int(subject_id),))
    if not s:
        return 0
    lessons = [r["id"] for r in db.fetchall(
        "SELECT l.id FROM lessons l JOIN sections s ON s.id=l.section_id WHERE s.subject_id=?", (int(subject_id),))]
    return move("subject", s["title"], actor, source, [("subjects", int(subject_id))] + _lesson_test_roots(lessons),
                meta={"subject_id": int(subject_id), "subject_title": s["title"], "location": "Обучение"})


def trash_section(section_id: int, actor, source="site") -> int:
    s = db.fetchone("SELECT title, subject_id FROM sections WHERE id=?", (int(section_id),))
    if not s:
        return 0
    lessons = [r["id"] for r in db.fetchall("SELECT id FROM lessons WHERE section_id=?", (int(section_id),))]
    place = section_place(section_id)
    sub = db.fetchone("SELECT title FROM subjects WHERE id=?", (s["subject_id"],))
    return move("section", s["title"], actor, source, [("sections", int(section_id))] + _lesson_test_roots(lessons),
                meta={"subject_id": s["subject_id"], "subject_title": sub["title"] if sub else None,
                      "location": sub["title"] if sub else place.get("location"),
                      "parent_table": "subjects", "parent_id": s["subject_id"]})


def trash_lesson(lesson_id: int, actor, source="site") -> int:
    l = db.fetchone("SELECT title, section_id FROM lessons WHERE id=?", (int(lesson_id),))
    if not l:
        return 0
    place = lesson_place(lesson_id)
    loc = place.get("location", "")
    return move("lesson", l["title"], actor, source, [("lessons", int(lesson_id))] + _lesson_test_roots([int(lesson_id)]),
                meta={"subject_id": place.get("subject_id"), "subject_title": place.get("subject_title"),
                      "location": loc.rsplit(" › ", 1)[0] if loc else "",
                      "parent_table": "sections", "parent_id": l["section_id"]})


def trash_lesson_test(lesson_id: int, actor, source="site") -> int:
    l = db.fetchone("SELECT title, test_id FROM lessons WHERE id=?", (int(lesson_id),))
    if not l or not l["test_id"] or not db.fetchone("SELECT 1 FROM tests WHERE id=?", (l["test_id"],)):
        if l and l["test_id"]:
            db.execute("UPDATE lessons SET test_id=NULL WHERE id=?", (int(lesson_id),))
        return 0
    t = db.fetchone("SELECT title FROM tests WHERE id=?", (l["test_id"],))
    place = lesson_place(lesson_id)
    return move("test", (t["title"] if t else "") or f"Тест урока «{l['title']}»", actor, source,
                [("tests", l["test_id"])], updates=[("lessons", int(lesson_id), {"test_id": None})],
                meta={"subject_id": place.get("subject_id"), "subject_title": place.get("subject_title"),
                      "location": place.get("location", "") + " · тест урока",
                      "parent_table": "lessons", "parent_id": int(lesson_id)})


def trash_test(test_id: int, actor, source="site", note: str = "") -> int:
    t = db.fetchone("SELECT title FROM tests WHERE id=?", (int(test_id),))
    if not t:
        return 0
    place = test_place(test_id)
    links = [("lessons", r["id"], {"test_id": None}) for r in
             db.fetchall("SELECT id FROM lessons WHERE test_id=?", (int(test_id),))]
    return move("test", t["title"], actor, source, [("tests", int(test_id))], updates=links, note=note,
                meta={"subject_id": place.get("subject_id"), "subject_title": place.get("subject_title"),
                      "location": place.get("location", "")})


def trash_question(question_id: int, actor, source="site") -> Optional[int]:
    q = db.fetchone("SELECT test_id, text, order_num FROM questions WHERE id=?", (int(question_id),))
    if not q:
        return None
    place = test_place(q["test_id"])
    t = db.fetchone("SELECT title FROM tests WHERE id=?", (q["test_id"],))
    title = " ".join((q["text"] or "").split())[:120] or "Вопрос"
    move("question", title, actor, source, [("questions", int(question_id))],
         meta={"subject_id": place.get("subject_id"), "subject_title": place.get("subject_title"),
               "location": (f"Тест «{t['title']}»" if t else "") +
                           (f" · {place.get('location')}" if place.get("location") and t and
                            "тест урока" in place.get("location", "") else "") +
                           f" · вопрос № {(q['order_num'] or 0) + 1}",
               "parent_table": "tests", "parent_id": q["test_id"]})
    return q["test_id"]


def trash_interactive(task_id: int, actor, source="site", group_id: str = "") -> int:
    t = db.fetchone("SELECT title FROM interactive_tasks WHERE id=?", (int(task_id),))
    if not t:
        return 0
    lessons = db.fetchall("SELECT DISTINCT lesson_id FROM lesson_blocks WHERE task_id=?", (int(task_id),))
    loc = "Библиотека интерактивов"
    meta = {}
    if lessons:
        p = lesson_place(lessons[0]["lesson_id"])
        meta = {"subject_id": p.get("subject_id"), "subject_title": p.get("subject_title")}
        loc += " · в уроке: " + p.get("location", "") + (f" и ещё {len(lessons) - 1}" if len(lessons) > 1 else "")
    meta["location"] = loc
    return move("interactive", t["title"], actor, source, [("interactive_tasks", int(task_id))],
                meta=meta, group_id=group_id)


def trash_block(block_id: int, actor, source="site") -> Optional[int]:
    b = db.fetchone("SELECT lesson_id, kind, title, body, sort_order, task_id FROM lesson_blocks WHERE id=?",
                    (int(block_id),))
    if not b:
        return None
    place = lesson_place(b["lesson_id"])
    kinds = {"text": "Текст", "interactive": "Интерактив", "image": "Картинка", "video": "Видео"}
    title = b["title"] or (" ".join((b["body"] or "").split())[:80]) or kinds.get(b["kind"], "Блок")
    if b["kind"] == "interactive" and b["task_id"]:
        t = db.fetchone("SELECT title FROM interactive_tasks WHERE id=?", (b["task_id"],))
        title = "Интерактив «" + (t["title"] if t else "") + "» в уроке"
    move("block", title, actor, source, [("lesson_blocks", int(block_id))],
         meta={"subject_id": place.get("subject_id"), "subject_title": place.get("subject_title"),
               "location": place.get("location", "") + f" · блок № {b['sort_order'] or 0}",
               "parent_table": "lessons", "parent_id": b["lesson_id"]})
    return b["lesson_id"]


def trash_lesson_image(image_id: int, actor, source="site") -> Optional[int]:
    r = db.fetchone("SELECT lesson_id, sort_order FROM lesson_images WHERE id=?", (int(image_id),))
    if not r:
        return None
    place = lesson_place(r["lesson_id"])
    move("image", f"Страница конспекта № {r['sort_order'] or ''}".strip(), actor, source,
         [("lesson_images", int(image_id))],
         meta={"subject_id": place.get("subject_id"), "subject_title": place.get("subject_title"),
               "location": place.get("location", ""), "parent_table": "lessons", "parent_id": r["lesson_id"]})
    return r["lesson_id"]


def trash_conspect(lesson_id: int, actor, source="site", with_pages: bool = True, with_text: bool = True) -> int:
    """Конспект урока: текст (content_html) и, если просят, страницы-картинки."""
    l = db.fetchone("SELECT title, content_html FROM lessons WHERE id=?", (int(lesson_id),))
    if not l:
        return 0
    has_text = with_text and bool((l["content_html"] or "").strip())
    pages = db.fetchall("SELECT id FROM lesson_images WHERE lesson_id=?", (int(lesson_id),)) if with_pages else []
    if not has_text and not pages:
        return 0
    place = lesson_place(lesson_id)
    parts = []
    if has_text:
        parts.append("текст")
    if pages:
        parts.append(f"страниц: {len(pages)}")
    return move("conspect", f"Конспект урока «{l['title']}»", actor, source,
                [("lesson_images", p["id"]) for p in pages],
                updates=[("lessons", int(lesson_id), {"content_html": ""})] if has_text else [],
                meta={"subject_id": place.get("subject_id"), "subject_title": place.get("subject_title"),
                      "location": place.get("location", ""), "parent_table": "lessons", "parent_id": int(lesson_id)},
                note=", ".join(parts))


def trash_workbook(lesson_id: int, actor, source="site") -> int:
    l = db.fetchone("SELECT title, workbook_path, workbook_name FROM lessons WHERE id=?", (int(lesson_id),))
    if not l or not (l["workbook_path"] or "").strip():
        return 0
    place = lesson_place(lesson_id)
    return move("workbook", l["workbook_name"] or f"Рабочая тетрадь урока «{l['title']}»", actor, source, [],
                updates=[("lessons", int(lesson_id), {"workbook_path": None, "workbook_name": None, "workbook_size": 0})],
                meta={"subject_id": place.get("subject_id"), "subject_title": place.get("subject_title"),
                      "location": place.get("location", ""), "parent_table": "lessons", "parent_id": int(lesson_id)})


def trash_video(lesson_id: int, actor, source="site") -> int:
    l = db.fetchone("SELECT title, video_url, youtube_id FROM lessons WHERE id=?", (int(lesson_id),))
    if not l or not ((l["video_url"] or "").strip() or (l["youtube_id"] or "").strip()):
        return 0
    place = lesson_place(lesson_id)
    return move("video", f"Видео урока «{l['title']}»", actor, source, [],
                updates=[("lessons", int(lesson_id), {"video_url": None, "youtube_id": None})],
                meta={"subject_id": place.get("subject_id"), "subject_title": place.get("subject_title"),
                      "location": place.get("location", ""), "parent_table": "lessons", "parent_id": int(lesson_id)})


def trash_music(track_id: int, actor, source="site") -> int:
    t = db.fetchone("SELECT title FROM music_tracks WHERE id=?", (int(track_id),))
    if not t:
        return 0
    return move("music", t["title"], actor, source, [("music_tracks", int(track_id))],
                meta={"location": "Музыка на тестах"})


def trash_note(note_id: int, actor, source="bot") -> int:
    n = db.fetchone("SELECT title, subject FROM notes WHERE id=?", (int(note_id),))
    if not n:
        return 0
    return move("note", n["title"], actor, source, [("notes", int(note_id))],
                extra=(("user_notes_progress", "note_id=?", (int(note_id),)),
                       ("paid_access", "note_id=?", (int(note_id),))),
                meta={"subject_title": n["subject"] or None, "location": "Бот · Конспекты ЕНТ"})


def trash_category(cat_id: int, actor, source="bot") -> int:
    c = db.fetchone("SELECT name FROM test_categories WHERE id=?", (int(cat_id),))
    if not c:
        return 0
    links = [("tests", r["id"], {"category_id": None}) for r in
             db.fetchall("SELECT id FROM tests WHERE category_id=?", (int(cat_id),))]
    try:
        from services import category_service as _cs
        for u in db.fetchall("SELECT rowid AS rid, profile_subjects FROM users "
                             "WHERE profile_subjects IS NOT NULL AND profile_subjects <> ''"):
            items = _cs._profile_list(u["profile_subjects"])
            if int(cat_id) in items:
                links.append(("users", u["rid"], {"profile_subjects": ",".join(str(x) for x in items if x != int(cat_id))}))
    except Exception as e:
        log.warning("trash category profiles: %s", e)
    return move("category", c["name"], actor, source, [("test_categories", int(cat_id))],
                extra=(("category_access", "category_id=?", (int(cat_id),)),),
                updates=links, meta={"location": "Бот · Разделы каталога"})


def trash_asset(asset_id: int, actor, source="site") -> Optional[int]:
    a = db.fetchone("SELECT task_id, orig_name, stored_name FROM interactive_assets WHERE id=?", (int(asset_id),))
    if not a:
        return None
    t = db.fetchone("SELECT title FROM interactive_tasks WHERE id=?", (a["task_id"],))
    move("asset", a["orig_name"] or a["stored_name"], actor, source, [("interactive_assets", int(asset_id))],
         meta={"location": "Интерактив «" + (t["title"] if t else "") + "»",
               "parent_table": "interactive_tasks", "parent_id": a["task_id"]})
    return a["task_id"]


def trash_zachet_bank(lesson_id: int, actor, source="site") -> int:
    if not db.fetchone("SELECT 1 FROM zachet_questions WHERE lesson_id=? LIMIT 1", (int(lesson_id),)):
        return 0
    place = lesson_place(lesson_id)
    return move("zachet", "Банк вопросов зачёта", actor, source, [],
                extra=(("zachet_questions", "lesson_id=?", (int(lesson_id),)),),
                meta={"subject_id": place.get("subject_id"), "subject_title": place.get("subject_title"),
                      "location": place.get("location", ""), "parent_table": "lessons", "parent_id": int(lesson_id)})


def trash_premium(user_id: int, actor, source="site") -> int:
    """Отзыв Premium: запись можно вернуть вместе с прежним сроком."""
    u = db.fetchone("SELECT tg_id, username, first_name FROM users WHERE id=?", (int(user_id),))
    who = ("@" + u["username"]) if u and u["username"] else (u["first_name"] if u else f"id{user_id}")
    return move("premium", f"Premium: {who}", actor, source, [],
                extra=(("premium_users", "user_id=?", (int(user_id),)),),
                meta={"location": "Подписки"})


def trash_results(scope: str, actor, source="bot") -> dict:
    """Удаление результатов из бота. Баллы и награды не трогаются, как и раньше."""
    if scope == "private":
        cond = "test_id IN (SELECT id FROM tests WHERE is_private=1)"
        title = "Результаты приватных тестов"
    else:
        cond, title = "1=1", "Все результаты тестов"
    st = db.fetchone(f"SELECT COUNT(*) AS c FROM test_statistics WHERE {cond}")["c"]
    at = db.fetchone(f"SELECT COUNT(*) AS c FROM test_attempts WHERE {cond}")["c"]
    an = db.fetchone(f"SELECT COUNT(*) AS c FROM attempt_answers WHERE attempt_id IN "
                     f"(SELECT id FROM test_attempts WHERE {cond})")["c"]
    if st or at:
        move("results", title, actor, source, [],
             extra=(("test_statistics", cond, ()), ("test_attempts", cond, ())),
             meta={"location": "Статистика"}, note=f"попыток: {at}, статистики: {st}")
    return {"stat": st, "att": at, "ans": an}
