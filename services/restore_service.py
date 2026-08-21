"""
Восстановление платформы из бэкапа — общий движок для сайта и бота.

Бэкап один на всё: файл базы (в нём и бот, и сайт) плюс загруженные файлы.
Телеграм не принимает файлы больше 20 МБ, поэтому большой архив режется на
части ≤19 МБ. Часть — это обычный ZIP, внутри которого кусок архива и паспорт
с контрольными суммами: по нему видно, из какого бэкапа часть, какая по счёту
и не побилась ли она по дороге.

Приём частей идёт сессиями. Сессия привязана к ID администратора и к ID
бэкапа: части двух разных копий или двух разных админов никогда не смешаются.
Приём можно прервать и продолжить с последней принятой части.
"""
import hashlib
import io
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import config
import database as db

log = logging.getLogger(__name__)

PART_FORMAT = "smartent-backup-part"
PART_FORMAT_VERSION = 1
META_NAME = "smartent_part.json"
CHUNK_NAME = "chunk.bin"

MAX_PART_BYTES = 19 * 1024 * 1024      # запас под лимит Telegram в 20 МБ
SESSION_TTL_HOURS = 24                  # через сколько чистить брошенные части

# Без этих таблиц архив — не бэкап нашей платформы
REQUIRED_TABLES = ("users", "tests", "questions", "subjects", "sections", "lessons")

# Что показываем в отчёте: таблица -> человеческое имя
REPORT_TABLES = [
    ("users", "Пользователи"),
    ("subjects", "Предметы"),
    ("sections", "Разделы"),
    ("lessons", "Уроки"),
    ("lesson_images", "Страницы конспектов"),
    ("tests", "Тесты"),
    ("questions", "Вопросы"),
    ("question_options", "Варианты ответов"),
    ("test_attempts", "Прохождения тестов"),
    ("subject_access", "Доступы к предметам"),
    ("lesson_access", "Доступы к урокам"),
    ("premium_users", "Подписки Премиум"),
    ("purchases", "Платежи"),
    ("settings", "Настройки"),
    ("note_views", "Журнал просмотра конспектов"),
]


# ===================== пути =====================

def _data_root() -> Path:
    return Path(config.DB_PATH).resolve().parent


# --- Своя маленькая база под сессии и журнал -------------------------------
# Держать их в bot.db нельзя: восстановление заменяет этот файл целиком, и
# журнал вместе с недособранными сессиями исчезал бы ровно в тот момент, когда
# он нужнее всего. Поэтому — отдельный файл рядом, его бэкап не трогает.

_state_ready = False


def state_db_path() -> str:
    return str(_data_root() / "restore_state.db")


def _state_conn():
    global _state_ready
    con = sqlite3.connect(state_db_path(), timeout=30)
    con.row_factory = sqlite3.Row
    if not _state_ready:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS restore_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_tg_id INTEGER NOT NULL,
                backup_id TEXT DEFAULT '',
                state TEXT DEFAULT 'collecting',
                total_parts INTEGER DEFAULT 0,
                whole_sha256 TEXT DEFAULT '',
                whole_size INTEGER DEFAULT 0,
                assembled_path TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS restore_parts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                part_no INTEGER NOT NULL,
                file_path TEXT NOT NULL,
                size INTEGER DEFAULT 0,
                sha256 TEXT DEFAULT '',
                orig_name TEXT DEFAULT '',
                received_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(session_id, part_no)
            );
            CREATE TABLE IF NOT EXISTS restore_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_tg_id INTEGER,
                session_id INTEGER,
                stage TEXT,
                result TEXT,
                details TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        con.commit()
        _state_ready = True
    return con


def _sx(sql: str, params=()):
    con = _state_conn()
    try:
        cur = con.execute(sql, params)
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def _sone(sql: str, params=()):
    con = _state_conn()
    try:
        row = con.execute(sql, params).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def _sall(sql: str, params=()):
    con = _state_conn()
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def sessions_root() -> Path:
    """Временное хранилище частей. Лежит рядом с базой, наружу не отдаётся."""
    p = _data_root() / "restore_sessions"
    p.mkdir(parents=True, exist_ok=True)
    return p


def session_dir(admin_tg_id: int, backup_id: str) -> Path:
    safe = "".join(ch for ch in (backup_id or "single") if ch.isalnum() or ch in "-_")[:40]
    p = sessions_root() / f"{int(admin_tg_id)}_{safe or 'single'}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


# ===================== нарезка бэкапа на части =====================

def split_backup(zip_path: str, out_dir: str = "",
                 max_bytes: int = MAX_PART_BYTES) -> list:
    """Разрезать готовый архив на части ≤max_bytes.

    Если архив и так помещается — возвращаем его одной частью как есть,
    чтобы не заворачивать маленький бэкап в лишнюю обёртку.
    """
    size = os.path.getsize(zip_path)
    if size <= max_bytes:
        return [zip_path]

    out = Path(out_dir or os.path.dirname(zip_path))
    out.mkdir(parents=True, exist_ok=True)
    whole_sha = _sha256_file(zip_path)
    backup_id = whole_sha[:16]
    base = Path(zip_path).stem
    created = datetime.now().isoformat(timespec="seconds")

    total = (size + max_bytes - 1) // max_bytes
    parts = []
    with open(zip_path, "rb") as src:
        for idx in range(1, total + 1):
            chunk = src.read(max_bytes)
            if not chunk:
                break
            meta = {
                "format": PART_FORMAT,
                "v": PART_FORMAT_VERSION,
                "backup_id": backup_id,
                "part": idx,
                "total": total,
                "chunk_sha256": hashlib.sha256(chunk).hexdigest(),
                "chunk_size": len(chunk),
                "whole_sha256": whole_sha,
                "whole_size": size,
                "created": created,
                "source": base,
            }
            part_path = out / f"{base}.part{idx:02d}of{total:02d}.zip"
            with zipfile.ZipFile(part_path, "w", zipfile.ZIP_STORED) as zf:
                zf.writestr(META_NAME, json.dumps(meta, ensure_ascii=False, indent=2))
                zf.writestr(CHUNK_NAME, chunk)
            parts.append(str(part_path))
    return parts


# ===================== разбор присланного файла =====================

def read_part(path: str) -> dict:
    """Что нам прислали. Возвращает dict с полем kind:

    part   — часть многотомного бэкапа (есть паспорт)
    whole  — целый бэкап одним файлом (внутри лежит bot.db)
    bad    — не ZIP / не наш формат, в reason причина
    """
    if not zipfile.is_zipfile(path):
        return {"kind": "bad",
                "reason": "это не ZIP-архив. Пришлите файл бэкапа как есть, "
                          "не распаковывая и не переименовывая"}
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if META_NAME in names and CHUNK_NAME in names:
                try:
                    meta = json.loads(zf.read(META_NAME).decode("utf-8"))
                except Exception:
                    return {"kind": "bad", "reason": "паспорт части не читается"}
                if meta.get("format") != PART_FORMAT:
                    return {"kind": "bad", "reason": "часть от другой программы"}
                if int(meta.get("v", 0)) > PART_FORMAT_VERSION:
                    return {"kind": "bad",
                            "reason": "часть сделана более новой версией платформы — "
                                      "обновите бота"}
                chunk = zf.read(CHUNK_NAME)
                if hashlib.sha256(chunk).hexdigest() != meta.get("chunk_sha256"):
                    return {"kind": "bad",
                            "reason": "часть повреждена (не сходится контрольная сумма) — "
                                      "перешлите именно этот файл заново"}
                meta["kind"] = "part"
                meta["_chunk"] = chunk
                return meta
            # целый бэкап одним файлом
            for n in names:
                if n.endswith("bot.db") and ".." not in n and "__MACOSX" not in n:
                    return {"kind": "whole", "db_name": n}
            return {"kind": "bad",
                    "reason": "в архиве нет ни bot.db, ни паспорта части — "
                              "это не бэкап платформы"}
    except zipfile.BadZipFile:
        return {"kind": "bad", "reason": "архив повреждён и не открывается"}


# ===================== сессия приёма частей =====================

def start_session(admin_tg_id: int, backup_id: str) -> int:
    """Открыть (или найти уже открытую) сессию приёма для этого админа."""
    row = _sone(
        "SELECT id FROM restore_sessions WHERE admin_tg_id=? AND backup_id=? "
        "AND state='collecting'", (admin_tg_id, backup_id))
    if row:
        return row["id"]
    return _sx(
        "INSERT INTO restore_sessions (admin_tg_id, backup_id, state) VALUES (?,?,'collecting')",
        (admin_tg_id, backup_id))


def active_session(admin_tg_id: int):
    """Незакрытая сессия админа — чтобы продолжить с последней части."""
    return _sone(
        "SELECT * FROM restore_sessions WHERE admin_tg_id=? AND state='collecting' "
        "ORDER BY id DESC LIMIT 1", (admin_tg_id,))


def get_session(session_id: int):
    """Одна сессия приёма — для экранов бота и сайта."""
    return _sone("SELECT * FROM restore_sessions WHERE id=?", (session_id,))


def session_parts(session_id: int) -> list:
    return _sall("SELECT * FROM restore_parts WHERE session_id=? ORDER BY part_no",
                 (session_id,))


def add_part(session_id: int, admin_tg_id: int, backup_id: str, info: dict,
             src_path: str, orig_name: str) -> dict:
    """Принять часть. Возвращает {ok, replaced, part_no, total, count, error}."""
    part_no = int(info.get("part") or 1)
    total = int(info.get("total") or 0)
    dest_dir = session_dir(admin_tg_id, backup_id)
    dest = dest_dir / f"part{part_no:03d}.bin"

    exists = _sone(
        "SELECT * FROM restore_parts WHERE session_id=? AND part_no=?",
        (session_id, part_no))
    if info["kind"] == "part":
        with open(dest, "wb") as f:
            f.write(info["_chunk"])
        size = len(info["_chunk"])
        sha = info.get("chunk_sha256", "")
    else:
        shutil.copyfile(src_path, dest)
        size = os.path.getsize(dest)
        sha = _sha256_file(dest)
        total = 1

    if exists:
        _sx("UPDATE restore_parts SET file_path=?, size=?, sha256=?, orig_name=?, "
            "received_at=CURRENT_TIMESTAMP WHERE id=?",
            (str(dest), size, sha, orig_name, exists["id"]))
    else:
        _sx("INSERT INTO restore_parts (session_id, part_no, file_path, size, sha256, "
            "orig_name) VALUES (?,?,?,?,?,?)",
            (session_id, part_no, str(dest), size, sha, orig_name))

    if total:
        _sx("UPDATE restore_sessions SET total_parts=? WHERE id=?", (total, session_id))
    if info.get("whole_sha256"):
        _sx("UPDATE restore_sessions SET whole_sha256=?, whole_size=? WHERE id=?",
            (info["whole_sha256"], int(info.get("whole_size") or 0), session_id))

    count = len(session_parts(session_id))
    return {"ok": True, "replaced": bool(exists), "part_no": part_no,
            "total": total, "count": count, "size": size}


def check_complete(session_id: int) -> dict:
    """Все ли части на месте. Возвращает {ok, missing, total, count, error}."""
    sess = _sone("SELECT * FROM restore_sessions WHERE id=?", (session_id,))
    if not sess:
        return {"ok": False, "error": "Сессия восстановления не найдена."}
    parts = session_parts(session_id)
    if not parts:
        return {"ok": False, "error": "Не прислано ни одной части."}
    nums = sorted(p["part_no"] for p in parts)
    total = sess["total_parts"] or max(nums)
    missing = [n for n in range(1, total + 1) if n not in nums]
    if missing:
        return {"ok": False, "missing": missing, "total": total, "count": len(parts),
                "error": ("Не хватает частей: " +
                          ", ".join(f"№{n}" for n in missing[:12]) +
                          (" …" if len(missing) > 12 else "") +
                          ".\nДошлите именно их — остальные уже приняты.")}
    extra = [n for n in nums if n > total]
    if extra:
        return {"ok": False, "total": total, "count": len(parts),
                "error": f"Лишние части: {', '.join('№' + str(n) for n in extra)}. "
                         "Похоже, смешались файлы двух разных бэкапов."}
    return {"ok": True, "total": total, "count": len(parts)}


def assemble(session_id: int) -> dict:
    """Склеить части в один архив и проверить его целиком."""
    ready = check_complete(session_id)
    if not ready.get("ok"):
        return ready
    sess = _sone("SELECT * FROM restore_sessions WHERE id=?", (session_id,))
    parts = session_parts(session_id)

    out = Path(tempfile.mkdtemp(prefix="restore_join_")) / "backup.zip"
    with open(out, "wb") as dst:
        for p in parts:
            if not os.path.exists(p["file_path"]):
                return {"ok": False,
                        "error": f"Часть №{p['part_no']} пропала из временного хранилища. "
                                 "Пришлите её заново."}
            with open(p["file_path"], "rb") as src:
                shutil.copyfileobj(src, dst)

    if sess["whole_sha256"]:
        got = _sha256_file(out)
        if got != sess["whole_sha256"]:
            return {"ok": False,
                    "error": "Склеенный архив не сходится с контрольной суммой. "
                             "Скорее всего одна из частей — от другого бэкапа "
                             "или пришла не полностью."}
    if not zipfile.is_zipfile(out):
        return {"ok": False,
                "error": "Склеенный архив не читается как ZIP — какая-то часть битая."}
    _sx("UPDATE restore_sessions SET assembled_path=? WHERE id=?",
        (str(out), session_id))
    return {"ok": True, "path": str(out), "size": os.path.getsize(out)}


# ===================== паспорт бэкапа =====================

def inspect_backup(zip_path: str) -> dict:
    """Что внутри архива: дата, размер, версия схемы, сколько чего лежит."""
    info = {"ok": False, "size": os.path.getsize(zip_path)}
    try:
        zf = zipfile.ZipFile(zip_path)
    except Exception as e:
        info["error"] = f"Архив не открывается: {e}"
        return info
    with zf:
        db_name = None
        uploads = 0
        for n in zf.namelist():
            if ".." in n or "__MACOSX" in n:
                continue
            if n.endswith("bot.db") and db_name is None:
                db_name = n
            elif "uploads/" in n and not n.endswith("/"):
                uploads += 1
        if not db_name:
            info["error"] = "В архиве нет bot.db — это не бэкап платформы."
            return info
        tmp = Path(tempfile.mkdtemp(prefix="inspect_")) / "bot.db"
        try:
            with zf.open(db_name) as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
        except Exception:
            info["error"] = "База внутри архива повреждена и не распаковывается."
            return info
        zi = zf.getinfo(db_name)
        info["created"] = "%04d-%02d-%02d %02d:%02d" % zi.date_time[:5]
        info["uploads"] = uploads
        info["db_path"] = str(tmp)

    try:
        con = sqlite3.connect(str(tmp))
        chk = con.execute("PRAGMA integrity_check").fetchone()
        if not chk or chk[0] != "ok":
            con.close()
            info["error"] = "База в архиве повреждена (не проходит проверку целостности)."
            return info
        have = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in REQUIRED_TABLES if t not in have]
        if missing:
            con.close()
            info["error"] = ("Архив не подходит этой платформе — нет таблиц: "
                             + ", ".join(missing))
            return info
        counts = []
        for table, label in REPORT_TABLES:
            if table not in have:
                continue
            try:
                n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except Exception:
                continue
            if n:
                counts.append((label, n))
        info["counts"] = counts
        info["tables"] = len(have)
        con.close()
    except Exception as e:
        info["error"] = f"База в архиве не читается: {e}"
        return info

    info["ok"] = True
    return info


# ===================== журнал =====================

def log_event(admin_tg_id: int, session_id, stage: str, result: str,
              details: str = "") -> None:
    try:
        _sx("INSERT INTO restore_log (admin_tg_id, session_id, stage, result, details) "
            "VALUES (?,?,?,?,?)",
            (admin_tg_id, session_id, stage, result, (details or "")[:2000]))
    except Exception as e:
        log.warning("restore_log: %s", e)


def journal(limit: int = 100) -> list:
    return _sall("SELECT * FROM restore_log ORDER BY id DESC LIMIT ?", (int(limit),))


# ===================== само восстановление =====================

def current_counts() -> list:
    out = []
    for table, label in REPORT_TABLES:
        try:
            n = db.fetchone(f"SELECT COUNT(*) AS c FROM {table}")["c"]
        except Exception:
            continue
        if n:
            out.append((label, n))
    return out


def make_safety_copy() -> str:
    """Снимок текущего состояния — чтобы было куда откатиться."""
    root = _data_root() / "backups"
    root.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = root / f"before_restore_{ts}.db"
    db.snapshot_to(str(path))
    return str(path)


def set_maintenance(on: bool) -> None:
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('maintenance_mode', ?)",
               ("1" if on else "0",))


def restore_from_zip(zip_path: str, admin_tg_id: int, session_id=None,
                     progress=None) -> dict:
    """Восстановить всё из архива. Синхронно, с откатом при любой ошибке.

    progress(percent, stage) — необязательный колбэк для показа этапов.
    """
    def step(pct, stage):
        if progress:
            try:
                progress(pct, stage)
            except Exception:
                pass

    report = {"ok": False, "stages": [], "restored": [], "skipped_files": 0,
              "safety_path": "", "error": ""}

    step(5, "Проверяю архив")
    info = inspect_backup(zip_path)
    if not info.get("ok"):
        report["error"] = info.get("error", "Архив не подходит.")
        log_event(admin_tg_id, session_id, "check", "error", report["error"])
        return report
    log_event(admin_tg_id, session_id, "check", "ok",
              f"размер {info['size']}, файлов uploads {info.get('uploads', 0)}")

    step(15, "Включаю режим обслуживания")
    try:
        set_maintenance(True)
    except Exception as e:
        log.warning("maintenance on: %s", e)

    safety = ""
    try:
        step(25, "Делаю копию текущего состояния для отката")
        safety = make_safety_copy()
        report["safety_path"] = safety
        log_event(admin_tg_id, session_id, "safety_copy", "ok", safety)

        step(45, "Распаковываю базу и файлы")
        tmp = Path(tempfile.mkdtemp(prefix="restore_apply_"))
        db_local = tmp / "bot.db"
        skipped = 0
        with zipfile.ZipFile(zip_path) as zf:
            db_name = None
            for n in zf.namelist():
                if n.endswith("bot.db") and ".." not in n and "__MACOSX" not in n:
                    db_name = n
                    break
            with zf.open(db_name) as src, open(db_local, "wb") as dst:
                shutil.copyfileobj(src, dst)
            data_root = _data_root()
            for n in zf.namelist():
                if ".." in n or n.endswith("/") or "__MACOSX" in n:
                    continue
                idx = n.find("uploads/")
                if idx == -1:
                    continue
                target = data_root / n[idx:]
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(n) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                except Exception:
                    skipped += 1
        report["skipped_files"] = skipped

        step(70, "Заменяю базу данных")
        # Замена файла целиком — поэтому дубликатов не возникает в принципе:
        # старые записи не досыпаются к новым, база становится ровно такой,
        # какой была в момент бэкапа. Сайт и бот читают один файл, значит
        # синхронизируются сами.
        db.replace_database(str(db_local))

        step(85, "Догоняю схему до текущей версии")
        try:
            db.init_db()
        except Exception as e:
            log.warning("init_db после восстановления: %s", e)

        step(95, "Считаю, что получилось")
        report["restored"] = current_counts()
        report["ok"] = True
        log_event(admin_tg_id, session_id, "restore", "ok",
                  f"пропущено файлов: {skipped}")
    except Exception as e:
        log.exception("restore: %s", e)
        report["error"] = str(e)
        # Откат: возвращаем снимок, сделанный перед восстановлением
        if safety and os.path.exists(safety):
            try:
                db.replace_database(safety)
                db.init_db()
                report["rolled_back"] = True
                log_event(admin_tg_id, session_id, "rollback", "ok", safety)
            except Exception as e2:
                log.exception("rollback: %s", e2)
                report["rolled_back"] = False
                log_event(admin_tg_id, session_id, "rollback", "error", str(e2))
        log_event(admin_tg_id, session_id, "restore", "error", str(e))
    finally:
        try:
            set_maintenance(False)
        except Exception:
            pass
        step(100, "Готово")
    return report


# ===================== уборка =====================

def finish_session(session_id: int, state: str = "done") -> None:
    sess = _sone("SELECT * FROM restore_sessions WHERE id=?", (session_id,))
    if sess:
        _wipe_session_files(sess)
    _sx("UPDATE restore_sessions SET state=?, finished_at=CURRENT_TIMESTAMP "
        "WHERE id=?", (state, session_id))
    _sx("DELETE FROM restore_parts WHERE session_id=?", (session_id,))


def _wipe_session_files(sess: dict) -> None:
    try:
        d = session_dir(sess["admin_tg_id"], sess["backup_id"] or "single")
        shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass
    ap = sess.get("assembled_path")
    if ap:
        try:
            shutil.rmtree(os.path.dirname(ap), ignore_errors=True)
        except Exception:
            pass


def cleanup_stale(hours: int = SESSION_TTL_HOURS) -> int:
    """Убрать брошенные сессии и их файлы. Возвращает число убранных."""
    edge = (datetime.utcnow() - timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = _sall(
        "SELECT * FROM restore_sessions WHERE state='collecting' AND created_at < ?",
        (edge,))
    n = 0
    for r in rows:
        _wipe_session_files(r)
        _sx("DELETE FROM restore_parts WHERE session_id=?", (r["id"],))
        _sx("UPDATE restore_sessions SET state='expired', "
            "finished_at=CURRENT_TIMESTAMP WHERE id=?", (r["id"],))
        n += 1
    return n


def human_size(n) -> str:
    n = float(n or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit in ("Б", "КБ") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ГБ"
