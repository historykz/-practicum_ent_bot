"""
Управление разделами (профильными предметами).

Раздел можно сделать приватным: тогда при выборе профильных предметов его
не видит никто, кроме админов и тех, кому доступ выдали поимённо. Полезно
для курсов, которые продаются отдельно или идут закрытой группой.

Здесь же — кто выбрал раздел, снятие выбора у одного человека и у всех сразу.
"""
from typing import Optional

import database as db
import utils

_ready = False


def ensure_schema() -> None:
    """Колонка и таблица создаются в database.py, но если его не обновили —
    делаем сами, чтобы экран не падал."""
    global _ready
    if _ready:
        return
    try:
        try:
            db.execute("ALTER TABLE test_categories ADD COLUMN is_private INTEGER DEFAULT 0")
        except Exception:
            pass
        db.execute("""
            CREATE TABLE IF NOT EXISTS category_access (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER NOT NULL,
                tg_id INTEGER NOT NULL,
                granted_by INTEGER,
                granted_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(category_id, tg_id)
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_category_access "
                   "ON category_access(category_id, tg_id)")
        _ready = True
    except Exception:
        pass


# ---------- Приватность ----------

def is_private(cat: dict) -> bool:
    try:
        return bool(cat.get("is_private"))
    except AttributeError:
        return False


def set_private(cat_id: int, private: bool) -> None:
    ensure_schema()
    db.execute("UPDATE test_categories SET is_private=? WHERE id=?",
               (1 if private else 0, cat_id))


def has_access(cat_id: int, tg_id: int) -> bool:
    """Виден ли раздел этому человеку."""
    ensure_schema()
    cat = db.fetchone("SELECT * FROM test_categories WHERE id=?", (cat_id,))
    if not cat:
        return False
    if not is_private(dict(cat)):
        return True                      # обычный раздел виден всем
    if utils.is_admin(tg_id) or utils.is_site_admin(tg_id):
        return True                      # админ видит всё
    row = db.fetchone(
        "SELECT id FROM category_access WHERE category_id=? AND tg_id=?",
        (cat_id, tg_id))
    return row is not None


def visible_categories(tg_id: int, only_optional: bool = True) -> list:
    """Разделы, которые человек вправе видеть при выборе профильных."""
    ensure_schema()
    sql = "SELECT * FROM test_categories"
    if only_optional:
        sql += " WHERE COALESCE(is_required,0)=0"
    sql += " ORDER BY sort_order, id"
    try:
        rows = [dict(r) for r in db.fetchall(sql)]
    except Exception:
        return []
    if utils.is_admin(tg_id) or utils.is_site_admin(tg_id):
        return rows
    allowed = _allowed_ids(tg_id)
    return [c for c in rows
            if not c.get("is_private") or c["id"] in allowed]


def _allowed_ids(tg_id: int) -> set:
    try:
        rows = db.fetchall("SELECT category_id FROM category_access WHERE tg_id=?",
                           (tg_id,))
    except Exception:
        return set()
    return {r["category_id"] for r in rows}


# ---------- Доступ поимённо ----------

def grant(cat_id: int, tg_id: int, admin_id: int = None) -> bool:
    ensure_schema()
    try:
        db.execute(
            "INSERT OR IGNORE INTO category_access (category_id, tg_id, granted_by) "
            "VALUES (?,?,?)", (cat_id, tg_id, admin_id))
        return True
    except Exception:
        return False


def revoke(cat_id: int, tg_id: int) -> None:
    ensure_schema()
    db.execute("DELETE FROM category_access WHERE category_id=? AND tg_id=?",
               (cat_id, tg_id))


def access_list(cat_id: int) -> list:
    """Кому выдан доступ к приватному разделу."""
    ensure_schema()
    try:
        rows = db.fetchall(
            "SELECT ca.tg_id, ca.granted_at, u.username, u.first_name "
            "FROM category_access ca LEFT JOIN users u ON u.tg_id = ca.tg_id "
            "WHERE ca.category_id=? ORDER BY ca.id DESC", (cat_id,))
    except Exception:
        return []
    return [dict(r) for r in rows]


# ---------- Кто выбрал раздел ----------

def _profile_list(raw) -> list:
    out = []
    for x in str(raw or "").split(","):
        x = x.strip()
        if x.isdigit():
            out.append(int(x))
        elif x:
            out.append(x)
    return out


def chosen_by(cat_id: int, limit: int = None, offset: int = 0) -> list:
    """Пользователи, у которых этот раздел стоит профильным.

    Выбор лежит строкой вида «3,7», поэтому фильтруем в Python: LIKE на такой
    строке ловил бы 13 и 37 вместе с 3 и 7.
    """
    rows = db.fetchall(
        "SELECT tg_id, username, first_name, profile_subjects, language "
        "FROM users WHERE profile_subjects LIKE ?", (f"%{cat_id}%",))
    out = []
    for r in rows:
        if cat_id in _profile_list(r["profile_subjects"]):
            out.append(dict(r))
    out.sort(key=lambda u: (u.get("first_name") or u.get("username") or ""))
    if limit is not None:
        return out[offset:offset + limit]
    return out


def chosen_count(cat_id: int) -> int:
    """Сколько человек выбрали раздел.

    Сначала грубо отсеиваем по LIKE (это делает база), и только совпадения
    разбираем точно — иначе на списке разделов пришлось бы каждый раз читать
    всю таблицу пользователей.
    """
    like = f"%{cat_id}%"
    try:
        rows = db.fetchall(
            "SELECT profile_subjects FROM users "
            "WHERE profile_subjects LIKE ?", (like,))
    except Exception:
        return 0
    return sum(1 for r in rows if cat_id in _profile_list(r["profile_subjects"]))


def remove_choice(cat_id: int, tg_id: int) -> bool:
    """Убрать раздел из профильных у одного человека."""
    row = db.fetchone("SELECT profile_subjects FROM users WHERE tg_id=?", (tg_id,))
    if not row:
        return False
    items = _profile_list(row["profile_subjects"])
    if cat_id not in items:
        return False
    items = [x for x in items if x != cat_id]
    db.execute("UPDATE users SET profile_subjects=? WHERE tg_id=?",
               (",".join(str(x) for x in items), tg_id))
    return True


def remove_choice_all(cat_id: int) -> int:
    """Снять этот раздел у всех. Возвращает, у скольких сняли.

    Пишем пачкой: на тысячах учеников поштучные запросы заметно тормозили бы.
    """
    rows = db.fetchall(
        "SELECT tg_id, profile_subjects FROM users "
        "WHERE profile_subjects IS NOT NULL AND profile_subjects <> ''")
    updates = []
    for r in rows:
        items = _profile_list(r["profile_subjects"])
        if cat_id not in items:
            continue
        left = [x for x in items if x != cat_id]
        updates.append((",".join(str(x) for x in left), r["tg_id"]))
    if not updates:
        return 0
    try:
        db.executemany("UPDATE users SET profile_subjects=? WHERE tg_id=?", updates)
    except Exception:
        for val, tg in updates:
            db.execute("UPDATE users SET profile_subjects=? WHERE tg_id=?", (val, tg))
    return len(updates)


# ---------- Общее ----------

def all_categories() -> list:
    ensure_schema()
    try:
        return [dict(r) for r in db.fetchall(
            "SELECT * FROM test_categories ORDER BY sort_order, id")]
    except Exception:
        return []


def get(cat_id: int) -> Optional[dict]:
    ensure_schema()
    row = db.fetchone("SELECT * FROM test_categories WHERE id=?", (cat_id,))
    return dict(row) if row else None


def rename(cat_id: int, name: str = None, emoji: str = None) -> tuple:
    """Переименовать раздел. Возвращает (получилось, сообщение)."""
    if name:
        clean = name.strip()[:64]
        busy = db.fetchone(
            "SELECT id FROM test_categories WHERE name=? COLLATE NOCASE AND id<>?",
            (clean, cat_id))
        if busy:
            return False, f"Раздел с названием «{clean}» уже есть."
        try:
            db.execute("UPDATE test_categories SET name=? WHERE id=?", (clean, cat_id))
        except Exception:
            return False, "Не удалось переименовать — возможно, имя занято."
    if emoji:
        db.execute("UPDATE test_categories SET emoji=? WHERE id=?",
                   (emoji.strip()[:8], cat_id))
    return True, "Готово."


def move(cat_id: int, direction: str) -> None:
    """Поменять раздел местами с соседом в списке."""
    cats = all_categories()
    ids = [c["id"] for c in cats]
    if cat_id not in ids:
        return
    i = ids.index(cat_id)
    j = i - 1 if direction == "up" else i + 1
    if j < 0 or j >= len(ids):
        return
    ids[i], ids[j] = ids[j], ids[i]
    for pos, cid in enumerate(ids, start=1):
        db.execute("UPDATE test_categories SET sort_order=? WHERE id=?", (pos, cid))


def stats(cat_id: int) -> dict:
    """Сводка по разделу для карточки в админке."""
    cat = get(cat_id) or {}
    tests = db.fetchone(
        "SELECT COUNT(*) AS c FROM tests WHERE category_id=?", (cat_id,))["c"]
    active = db.fetchone(
        "SELECT COUNT(*) AS c FROM tests WHERE category_id=? AND status='active'",
        (cat_id,))["c"]
    questions = db.fetchone(
        "SELECT COUNT(*) AS c FROM questions q JOIN tests t ON t.id=q.test_id "
        "WHERE t.category_id=?", (cat_id,))["c"]
    return {
        "tests": tests, "tests_active": active, "questions": questions,
        "chosen": chosen_count(cat_id),
        "access": len(access_list(cat_id)) if is_private(cat) else 0,
    }
