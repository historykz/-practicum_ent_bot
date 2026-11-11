"""«Память и место» (v99): сколько занимают бот, база, файлы и временный мусор.

Railway берёт деньги за память (RAM) и отдельно за место на диске. Здесь видно и то,
и другое, чтобы решать по цифрам, а не наугад. Уборка трогает только мусор:
временные файлы бэкапов, прошлые версии заданий и видео старше недели (для отката)
и папки заданий, на которые не ссылается ни одно задание.
"""
import logging
import os
import resource
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

import config
import database as db

log = logging.getLogger(__name__)
PREV_KEEP_DAYS = 7          # прошлую версию задания держим неделю для отката
ORPHAN_MIN_AGE = 24 * 3600  # папку без задания трогаем, только если ей больше суток


def human(n) -> str:
    n = float(n or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return (f"{n:.0f} {unit}" if unit in ("Б", "КБ") else f"{n:.1f} {unit}").replace(".", ",")
        n /= 1024
    return f"{n:.1f} ГБ"


def _data_root() -> Path:
    return Path(config.DB_PATH).resolve().parent


def dir_size(path) -> int:
    total = 0
    p = Path(path)
    if not p.exists():
        return 0
    if p.is_file():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def memory() -> dict:
    """Память процесса и контейнера. На Railway (Linux) есть cgroup: там видно, сколько
    контейнер держит всего и сколько из этого — файловый кэш (недавно записанные
    и прочитанные файлы, например бэкапы)."""
    out = {"rss": None, "peak": None, "container": None, "limit": None, "file_cache": None, "anon": None}
    status = _read("/proc/self/status")
    if status:
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                out["rss"] = int(line.split()[1]) * 1024
            elif line.startswith("VmHWM:"):
                out["peak"] = int(line.split()[1]) * 1024
    if out["rss"] is None:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        out["peak"] = peak if sys.platform == "darwin" else peak * 1024
    cur = _read("/sys/fs/cgroup/memory.current")                    # cgroup v2
    if cur and cur.strip().isdigit():
        out["container"] = int(cur.strip())
        lim = (_read("/sys/fs/cgroup/memory.max") or "").strip()
        out["limit"] = int(lim) if lim.isdigit() else None
        stat = _read("/sys/fs/cgroup/memory.stat") or ""
        for line in stat.splitlines():
            k, _, v = line.partition(" ")
            if k == "file" and v.strip().isdigit():
                out["file_cache"] = int(v)
            elif k == "anon" and v.strip().isdigit():
                out["anon"] = int(v)
    else:                                                             # cgroup v1
        cur = _read("/sys/fs/cgroup/memory/memory.usage_in_bytes")
        if cur and cur.strip().isdigit():
            out["container"] = int(cur.strip())
            stat = _read("/sys/fs/cgroup/memory/memory.stat") or ""
            for line in stat.splitlines():
                k, _, v = line.partition(" ")
                if k == "cache" and v.strip().isdigit():
                    out["file_cache"] = int(v)
                elif k == "rss" and v.strip().isdigit():
                    out["anon"] = int(v)
    return out


def _task_keys() -> set:
    keys = set()
    for r in db.fetchall("SELECT files_key, prev_files_key, pending_files_key FROM interactive_tasks"):
        for k in (r["files_key"], r["prev_files_key"], r["pending_files_key"]):
            if k:
                keys.add(k)
    return keys


def _old_prev_rows() -> list:
    """Задания, у которых прошлая версия (для отката) старше недели."""
    cutoff = time.time() - PREV_KEEP_DAYS * 86400
    from services import interactive_pkg as pkg
    rows = []
    for r in db.fetchall("SELECT id, title, prev_files_key, files_key, pending_files_key FROM interactive_tasks "
                         "WHERE prev_files_key IS NOT NULL AND prev_files_key<>''"):
        k = r["prev_files_key"]
        if k in (r["files_key"], r["pending_files_key"]):
            continue
        d = pkg.key_dir(k)
        if not d or not d.exists():
            continue
        try:
            if d.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        rows.append({"id": r["id"], "title": r["title"], "key": k, "size": dir_size(d)})
    return rows


def _orphan_dirs() -> list:
    from services import interactive_pkg as pkg
    root = pkg.pkg_root()
    keys = _task_keys()
    now = time.time()
    out = []
    try:
        names = os.listdir(root)
    except OSError:
        return out
    for name in names:
        d = root / name
        if name in keys or not d.is_dir() or not pkg.KEY_RE.match(name):
            continue
        try:
            if now - d.stat().st_mtime < ORPHAN_MIN_AGE:
                continue
        except OSError:
            continue
        out.append({"key": name, "size": dir_size(d)})
    return out


def _tables() -> list:
    """Самые большие таблицы базы (если SQLite собран с dbstat — по месту, иначе по строкам)."""
    try:
        rows = db.fetchall("SELECT name, SUM(pgsize) AS size FROM dbstat GROUP BY name ORDER BY size DESC LIMIT 12")
        return [{"name": r["name"], "size": r["size"], "rows": None} for r in rows]
    except Exception:
        pass
    out = []
    for r in db.fetchall("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        try:
            n = db.fetchone(f'SELECT COUNT(*) AS c FROM "{r["name"]}"')["c"]
        except Exception:
            continue
        out.append({"name": r["name"], "size": None, "rows": n})
    out.sort(key=lambda x: -(x["rows"] or 0))
    return out[:12]


def report() -> dict:
    from services import backup_service as bs
    root = _data_root()
    dbp = Path(config.DB_PATH).resolve()
    db_size = sum(dir_size(str(dbp) + s) for s in ("", "-wal", "-shm"))
    uploads = root / "uploads"
    parts = []
    if uploads.exists():
        for child in sorted(uploads.iterdir()):
            if child.is_dir() and child.name == "interactive":
                for sub in sorted(child.iterdir()):
                    parts.append({"name": f"interactive/{sub.name}", "size": dir_size(sub)})
            else:
                parts.append({"name": child.name, "size": dir_size(child)})
    parts.sort(key=lambda x: -x["size"])
    uploads_total = sum(p["size"] for p in parts)
    trash_size = dir_size(root / "trash_files")
    tmp = bs.temp_files_size()
    prev = _old_prev_rows()
    orphans = _orphan_dirs()
    try:
        du = shutil.disk_usage(str(root))
        disk = {"total": du.total, "used": du.used, "free": du.free}
    except OSError:
        disk = None
    return {
        "memory": memory(), "disk": disk, "db_size": db_size, "uploads": parts, "uploads_total": uploads_total,
        "trash_size": trash_size, "temp": tmp, "prev": prev, "prev_size": sum(p["size"] for p in prev),
        "orphans": orphans, "orphans_size": sum(o["size"] for o in orphans), "tables": _tables(),
        "backup_last": bs._backup_last(), "backup_hour": bs.DAILY_BACKUP_HOUR,
    }


def clean_temp() -> int:
    from services import backup_service as bs
    before = bs.temp_files_size()["bytes"]
    bs.cleanup_temp(300)
    return max(0, before - bs.temp_files_size()["bytes"])


def clean_old_versions() -> dict:
    """Удалить прошлые версии старше недели и папки заданий без задания."""
    from services import interactive_pkg as pkg
    freed, n = 0, 0
    for r in _old_prev_rows():
        db.execute("UPDATE interactive_tasks SET prev_files_key=NULL, prev_entry=NULL WHERE id=? AND prev_files_key=?",
                   (r["id"], r["key"]))
        pkg.delete_key(r["key"])
        freed += r["size"]; n += 1
    for o in _orphan_dirs():
        if o["key"] in _task_keys():
            continue
        pkg.delete_key(o["key"])
        freed += o["size"]; n += 1
    try:
        pkg._key_cache.clear()
    except Exception:
        pass
    log.info("storage cleanup: %d folders, %d bytes", n, freed)
    return {"folders": n, "bytes": freed}
