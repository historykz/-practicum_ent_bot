"""
Интерактивные задания из ZIP-архивов (формат SMART ЕНТ, версия 1).

Владелец не пишет код сам: архив ему собирает внешняя нейросеть (ChatGPT, Claude)
по шаблону, который скачивается в админке (services/interactive_template.py).
Здесь всё, что происходит с таким архивом на сайте:

  • разбор и проверка: manifest.json, пути, типы и размеры файлов, защита от
    «zip-бомб» и выхода за пределы папки; одно или много заданий в архиве;
  • раскладка: каждое задание получает СВОЮ папку uploads/interactive/pkg/<ключ>/
    и видит только свои файлы, код и картинки разных заданий не смешиваются;
  • версии: новая версия опубликованного задания сначала ждёт проверки
    (pending), прошлая хранится для отката (prev);
  • сборка документа задания: в начало <head> вставляются политика безопасности,
    мост SmartENT и <base>, чтобы относительные пути архива работали как есть.

Сколько в архиве заданий, определяет только manifest.json. Без манифеста архив
принимается, лишь если в нём ровно одна HTML-страница входа: тогда это одно задание.
"""
import io
import json
import logging
import os
import posixpath
import re
import secrets
import shutil
import time
import zipfile
from pathlib import Path
from typing import Optional

import config
import database as db
import utils
from services import interactive as it

log = logging.getLogger(__name__)

FORMAT = "smartent-interactive"
FORMAT_VERSION = 1                 # какую версию формата понимает сайт

ZIP_MAX = 80 * 1024 * 1024         # сам архив
UNZIP_MAX = 250 * 1024 * 1024      # всё распакованное
FILE_MAX = 40 * 1024 * 1024        # один файл
TASK_MAX = 120 * 1024 * 1024       # одно задание целиком
FILES_MAX = 4000                   # файлов в архиве
TASKS_MAX = 200                    # заданий в архиве
ENTRY_MAX = 3 * 1024 * 1024        # HTML-страница входа
RATIO_MAX = 250                    # во сколько раз файл может «раздуться» при распаковке

ALLOWED = {
    # страница и код
    ".html": "text/html", ".htm": "text/html", ".css": "text/css", ".js": "text/javascript",
    ".mjs": "text/javascript", ".json": "application/json", ".map": "application/json",
    ".wasm": "application/wasm", ".txt": "text/plain", ".md": "text/plain", ".csv": "text/csv",
    ".xml": "application/xml", ".vtt": "text/vtt",
    # картинки
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".avif": "image/avif", ".svg": "image/svg+xml", ".ico": "image/x-icon",
    ".bmp": "image/bmp",
    # звук и видео
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg", ".oga": "audio/ogg",
    ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac", ".opus": "audio/ogg",
    ".mp4": "video/mp4", ".webm": "video/webm", ".ogv": "video/ogg",
    # шрифты
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf",
    # 3D и анимации
    ".glb": "model/gltf-binary", ".gltf": "model/gltf+json", ".bin": "application/octet-stream",
    ".obj": "text/plain", ".mtl": "text/plain", ".fbx": "application/octet-stream",
    ".stl": "model/stl", ".lottie": "application/zip", ".atlas": "text/plain",
}
TEXT_EDITABLE = {".html", ".htm", ".css", ".js", ".mjs", ".json", ".txt", ".md", ".csv", ".svg", ".vtt", ".xml"}
IGNORED_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
ROOT_DOCS = re.compile(r"(?i)^(readme|license|licence|instructions?|инструкция)[^/]*$|\.md$|\.txt$")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
KEY_RE = re.compile(r"^[0-9a-f]{32}$")


# ───────────────────────── хранение файлов ─────────────────────────

def pkg_root() -> Path:
    d = Path(config.DB_PATH).resolve().parent / "uploads" / "interactive" / "pkg"
    d.mkdir(parents=True, exist_ok=True)
    return d


def key_dir(key: str) -> Optional[Path]:
    if not KEY_RE.match(key or ""):
        return None
    return pkg_root() / key


def delete_key(key: Optional[str]) -> None:
    d = key_dir(key or "")
    if d and d.exists():
        shutil.rmtree(d, ignore_errors=True)
    _key_cache.pop(key or "", None)


def copy_key(key: str) -> Optional[str]:
    src = key_dir(key)
    if not src or not src.exists():
        return None
    new = secrets.token_hex(16)
    shutil.copytree(src, pkg_root() / new)
    return new


_key_cache: dict = {}              # files_key -> (действует ли, когда проверено)


def key_known(key: str) -> bool:
    """Ключ принадлежит какому-то заданию (текущая, ожидающая или прошлая версия)."""
    if not KEY_RE.match(key or ""):
        return False
    now = time.time()
    hit = _key_cache.get(key)
    if hit and now - hit[1] < 60:
        return hit[0]
    row = db.fetchone("SELECT 1 FROM interactive_tasks WHERE files_key=? OR pending_files_key=? OR prev_files_key=?",
                      (key, key, key))
    ok = bool(row)
    if len(_key_cache) > 5000:
        _key_cache.clear()
    _key_cache[key] = (ok, now)
    return ok


def resolve_file(key: str, rel: str) -> Optional[Path]:
    """Файл задания по относительному пути. Выйти за пределы папки задания нельзя."""
    base = key_dir(key)
    if not base or not key_known(key):
        return None
    rel = (rel or "").replace("\\", "/")
    if not rel or rel.startswith("/") or "\x00" in rel or any(p in ("..", "") for p in rel.split("/")):
        return None
    p = (base / rel).resolve()
    try:
        p.relative_to(base.resolve())
    except ValueError:
        return None
    return p if p.is_file() else None


def mime_of(name: str) -> str:
    return ALLOWED.get(os.path.splitext(name)[1].lower(), "application/octet-stream")


def list_files(key: Optional[str]) -> list:
    base = key_dir(key or "")
    if not base or not base.exists():
        return []
    out = []
    for p in sorted(base.rglob("*")):
        if p.is_file():
            rel = p.relative_to(base).as_posix()
            out.append({"path": rel, "size": p.stat().st_size,
                        "editable": os.path.splitext(rel)[1].lower() in TEXT_EDITABLE})
    return out


def package_size(key: Optional[str]) -> int:
    return sum(f["size"] for f in list_files(key))


# ───────────────────────── разбор архива ─────────────────────────

def _member_name(info: zipfile.ZipInfo) -> str:
    """Имя файла в архиве. Архивы с Windows без UTF-8 флага читаем как UTF-8,
    если получается, иначе как есть."""
    name = info.filename
    if not (info.flag_bits & 0x800):
        try:
            name = name.encode("cp437").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return name.replace("\\", "/")


def _bad_path(name: str) -> bool:
    if not name or name.startswith("/") or "\x00" in name or re.match(r"^[A-Za-z]:", name):
        return True
    parts = name.split("/")
    return any(p in ("..",) for p in parts)


def _norm_folder(folder) -> str:
    f = str(folder or "").replace("\\", "/").strip().strip("/")
    if f in ("", "."):
        return ""
    f = posixpath.normpath(f)
    return "" if f == "." else f


def _read_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    """Читает файл, не доверяя размеру из оглавления архива."""
    out = bytearray()
    with zf.open(info) as fh:
        while True:
            chunk = fh.read(1 << 16)
            if not chunk:
                break
            out.extend(chunk)
            if len(out) > FILE_MAX or len(out) > info.file_size + 1024:
                raise ValueError(f"файл «{info.filename}» больше заявленного или больше {FILE_MAX // (1024 * 1024)} МБ")
    return bytes(out)


def _title_from_html(raw: bytes, fallback: str) -> str:
    m = re.search(rb"<title[^>]*>(.*?)</title>", raw[:200000], re.I | re.S)
    if m:
        t = re.sub(r"\s+", " ", m.group(1).decode("utf-8", "replace")).strip()
        if t:
            return t[:200]
    return fallback[:200]


def _as_key(value) -> tuple:
    """answer_key из манифеста: объект или строка JSON. Возвращает (строка, ошибка)."""
    if value is None or value == "":
        return "", "для оценки сервером нужен answer_key"
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    _k, err = it.parse_key(raw)
    if err:
        return "", "answer_key: " + err
    return json.dumps(json.loads(raw), ensure_ascii=False, indent=2), ""


def _clip(v, n: int) -> str:
    return re.sub(r"\s+", " ", str(v if v is not None else "")).strip()[:n]


def parse_archive(blob: bytes, file_name: str = "") -> dict:
    """Разобрать архив, ничего не сохраняя. Возвращает отчёт:
    {ok, error, title, format_version, tasks: [...], warnings: [...]}.
    У каждого задания: id, title, folder, entry, files, size, problems, warnings, meta."""
    rep = {"ok": False, "error": "", "title": "", "format_version": None, "tasks": [], "warnings": [],
           "file_name": file_name, "size": len(blob or b""), "has_manifest": False}
    if not blob:
        rep["error"] = "Файл пустой."
        return rep
    if len(blob) > ZIP_MAX:
        rep["error"] = f"Архив больше {ZIP_MAX // (1024 * 1024)} МБ."
        return rep
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
        infos = zf.infolist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError):
        rep["error"] = "Это не ZIP-архив или архив повреждён."
        return rep

    files, total = {}, 0
    for info in infos:
        name = _member_name(info)
        if info.is_dir() or name.endswith("/"):
            continue
        low = name.lower()
        if low.startswith("__macosx/") or "/__macosx/" in low or posixpath.basename(low) in IGNORED_NAMES \
                or posixpath.basename(name).startswith("._"):
            continue
        if _bad_path(name):
            rep["error"] = f"Опасный путь в архиве: «{name}». Такой архив не принимается."
            return rep
        mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            rep["error"] = f"В архиве ссылка на файл вне архива (symlink): «{name}». Такой архив не принимается."
            return rep
        if info.flag_bits & 0x1:
            rep["error"] = "Архив защищён паролем. Загрузите архив без пароля."
            return rep
        if info.file_size > FILE_MAX:
            rep["error"] = f"Файл «{name}» больше {FILE_MAX // (1024 * 1024)} МБ."
            return rep
        if info.compress_size and info.file_size > 1024 * 1024 and info.file_size / info.compress_size > RATIO_MAX:
            rep["error"] = f"Файл «{name}» сжат подозрительно сильно (похоже на zip-бомбу). Архив не принят."
            return rep
        total += info.file_size
        if name in files:
            rep["warnings"].append(f"Файл «{name}» встречается в архиве дважды, взят последний.")
        files[name] = info
    if len(files) > FILES_MAX:
        rep["error"] = f"В архиве больше {FILES_MAX} файлов."
        return rep
    if total > UNZIP_MAX:
        rep["error"] = f"После распаковки архив больше {UNZIP_MAX // (1024 * 1024)} МБ."
        return rep
    if not files:
        rep["error"] = "Архив пустой."
        return rep

    # Где корень: manifest.json в корне или в единственной папке верхнего уровня
    root = ""
    if "manifest.json" not in files:
        tops = {n.split("/", 1)[0] for n in files}
        if len(tops) == 1 and all("/" in n for n in files):
            only = next(iter(tops))
            if f"{only}/manifest.json" in files:
                root = only + "/"
    rel = {n[len(root):]: info for n, info in files.items() if n.startswith(root)}

    if "manifest.json" in rel:
        rep["has_manifest"] = True
        try:
            manifest = json.loads(_read_member(zf, rel["manifest.json"]).decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError) as e:
            rep["error"] = f"manifest.json не читается как JSON: {e}"
            return rep
        if not isinstance(manifest, dict):
            rep["error"] = "manifest.json должен быть объектом { … }."
            return rep
        fmt = manifest.get("format")
        if fmt and fmt != FORMAT:
            rep["error"] = f"manifest.json другого формата («{fmt}»). Нужен «{FORMAT}»: соберите архив по шаблону SMART ЕНТ."
            return rep
        if not fmt:
            rep["warnings"].append(f"В manifest.json нет \"format\": \"{FORMAT}\". Архив принят, но лучше собирать его по шаблону.")
        try:
            fv = int(manifest.get("format_version") or 1)
        except (TypeError, ValueError):
            fv = 0
        if fv < 1:
            rep["error"] = "format_version в manifest.json должен быть числом (1)."
            return rep
        if fv > FORMAT_VERSION:
            rep["error"] = (f"Архив сделан по шаблону версии {fv}, а сайт понимает версию {FORMAT_VERSION}. "
                            f"Скачайте шаблон заново в админке и попросите ИИ пересобрать архив.")
            return rep
        rep["format_version"] = fv
        rep["title"] = _clip(manifest.get("title"), 200)
        raw_tasks = manifest.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            rep["error"] = "В manifest.json нет списка заданий \"tasks\"."
            return rep
        if len(raw_tasks) > TASKS_MAX:
            rep["error"] = f"В одном архиве не больше {TASKS_MAX} заданий."
            return rep
        tasks = []
        for n, t in enumerate(raw_tasks, 1):
            tasks.append(_check_task(t, n, rel))
        _check_overlap(tasks)
        used = set()
        for t in tasks:
            if t["folder"] is not None:
                used.update(t["files"])
        # Файлы вне папок заданий в задания не попадают
        stray = [n for n in rel if n not in used and n != "manifest.json" and not ROOT_DOCS.search(n)]
        if stray:
            rep["warnings"].append(f"Файлы вне папок заданий не используются ({len(stray)}): "
                                   + ", ".join(stray[:8]) + (" …" if len(stray) > 8 else ""))
    else:
        if any(posixpath.basename(n) == "smartent-template.json" for n in files):
            rep["error"] = ("Это шаблон для нейросети, а не задания. Приложите его к чату с ChatGPT или Claude, "
                            "опишите идею и загрузите сюда архив, который пришлёт нейросеть.")
            return rep
        # Без манифеста: ровно одна страница входа — это одно задание
        htmls = [n for n in rel if n.lower().endswith((".html", ".htm"))]
        index = [n for n in htmls if posixpath.basename(n).lower() in ("index.html", "index.htm")]
        pick = index if len(index) == 1 else (htmls if len(htmls) == 1 else [])
        if not pick:
            if not htmls:
                rep["error"] = "В архиве нет manifest.json и нет HTML-страницы. Нечего запускать."
            else:
                rep["error"] = ("В архиве нет manifest.json, а HTML-страниц несколько: непонятно, сколько заданий. "
                                "Попросите ИИ собрать архив по шаблону SMART ЕНТ (с manifest.json).")
            return rep
        entry_full = pick[0]
        folder = posixpath.dirname(entry_full)
        raw_entry = _read_member(zf, rel[entry_full])
        fake = {"id": "task-1", "folder": folder, "entry": posixpath.basename(entry_full),
                "title": _title_from_html(raw_entry, os.path.splitext(file_name or "Задание")[0] or "Задание")}
        tasks = [_check_task(fake, 1, rel)]
        rep["format_version"] = FORMAT_VERSION
        rep["warnings"].append("В архиве нет manifest.json: загружено как одно задание. "
                               "Для нескольких заданий, оценки и описаний нужен manifest.json по шаблону.")
    rep["tasks"] = tasks
    rep["ok"] = any(not t["problems"] for t in tasks)
    if not rep["ok"]:
        rep["error"] = "Ни одно задание в архиве не прошло проверку. Причины указаны у каждого задания."
    rep["_zip"] = zf
    rep["_rel"] = rel
    return rep


def _check_task(t, n: int, rel: dict) -> dict:
    out = {"n": n, "id": "", "title": "", "folder": None, "entry": "", "files": [], "size": 0,
           "problems": [], "warnings": [], "meta": {}}
    if not isinstance(t, dict):
        out["problems"].append("описание задания должно быть объектом { … }")
        return out
    tid = str(t.get("id") or "").strip()
    if not TASK_ID_RE.match(tid):
        out["problems"].append("id задания: латиница, цифры, «-» и «_», до 64 символов")
    out["id"] = tid or f"task-{n}"
    out["title"] = _clip(t.get("title"), 200) or out["id"]
    folder = _norm_folder(t.get("folder") if t.get("folder") is not None else f"tasks/{out['id']}")
    if _bad_path(folder) or folder.startswith(".."):
        out["problems"].append(f"недопустимая папка «{folder}»")
        return out
    out["folder"] = folder
    prefix = folder + "/" if folder else ""
    entry = str(t.get("entry") or "index.html").replace("\\", "/").strip().lstrip("./")
    if _bad_path(entry) or not entry.lower().endswith((".html", ".htm")):
        out["problems"].append("entry (страница входа) должна быть .html внутри папки задания")
    out["entry"] = entry
    members = [name for name in rel if name.startswith(prefix)] if prefix else list(rel)
    if prefix == "":
        members = [m for m in members if m != "manifest.json"]
    if not members:
        out["problems"].append(f"папка «{folder or '/'}» пустая или её нет в архиве")
        return out
    if prefix + entry not in rel:
        out["problems"].append(f"нет страницы входа «{prefix + entry}»")
    kept, skipped, size = [], [], 0
    for m in members:
        ext = os.path.splitext(m)[1].lower()
        if ext not in ALLOWED:
            skipped.append(m[len(prefix):])
            continue
        kept.append(m)
        size += rel[m].file_size
    if skipped:
        out["warnings"].append("пропущены файлы недопустимого типа: " + ", ".join(skipped[:6])
                               + (" …" if len(skipped) > 6 else ""))
    if size > TASK_MAX:
        out["problems"].append(f"задание больше {TASK_MAX // (1024 * 1024)} МБ")
    if prefix + entry in rel and rel[prefix + entry].file_size > ENTRY_MAX:
        out["problems"].append(f"страница входа больше {ENTRY_MAX // (1024 * 1024)} МБ: вынесите код в .js-файлы")
    out["files"], out["size"] = kept, size

    grading = str(t.get("grading") or "practice").strip().lower()
    if grading not in (it.PRACTICE, it.SERVER):
        out["problems"].append("grading: practice (тренировка) или server (проверка сервером)")
        grading = it.PRACTICE
    key = ""
    if grading == it.SERVER:
        key, err = _as_key(t.get("answer_key"))
        if err:
            out["problems"].append(err)

    def num(name, default, lo, hi):
        try:
            return max(lo, min(hi, int(t.get(name) if t.get(name) is not None else default)))
        except (TypeError, ValueError):
            out["warnings"].append(f"{name} не число, взято {default}")
            return default

    vis = str(t.get("visibility") or "all").strip().lower()
    out["meta"] = {
        "title": out["title"], "subject": _clip(t.get("subject"), 120), "topic": _clip(t.get("topic"), 200),
        "description": str(t.get("description") or "").strip()[:2000], "grading": grading, "answer_key": key,
        "max_score": num("max_score", 10, 1, 1000),
        "allow_retry": 0 if t.get("allow_retry") is False else 1,
        "max_attempts": num("max_attempts", 0, 0, 100),
        "frame_height": num("height", 0, 0, 4000),
        "visibility": vis if vis in ("all", "lessons") else "all",
    }
    return out


def _check_overlap(tasks: list) -> None:
    """Папки разных заданий не должны совпадать и вкладываться друг в друга,
    иначе код и картинки одного задания попадут в другое."""
    seen_ids = {}
    for t in tasks:
        if t["id"] in seen_ids:
            t["problems"].append(f"id «{t['id']}» уже есть у задания №{seen_ids[t['id']]}")
        else:
            seen_ids[t["id"]] = t["n"]
    for i, a in enumerate(tasks):
        for b in tasks[i + 1:]:
            if a["folder"] is None or b["folder"] is None:
                continue
            fa, fb = a["folder"], b["folder"]
            if fa == fb or fa == "" or fb == "" or fb.startswith(fa + "/") or fa.startswith(fb + "/"):
                a["problems"].append(f"папка «{fa or '/'}» пересекается с папкой задания №{b['n']}: "
                                     "у каждого задания должна быть своя папка")
                b["problems"].append(f"папка «{fb or '/'}» пересекается с папкой задания №{a['n']}: "
                                     "у каждого задания должна быть своя папка")


# ───────────────────────── сохранение ─────────────────────────

def _extract(rep: dict, task: dict) -> str:
    """Разложить файлы одного задания в его собственную папку. Возвращает ключ."""
    zf, rel = rep["_zip"], rep["_rel"]
    key = secrets.token_hex(16)
    dest = pkg_root() / key
    prefix = task["folder"] + "/" if task["folder"] else ""
    written = 0
    try:
        for m in task["files"]:
            sub = m[len(prefix):]
            target = (dest / sub).resolve()
            target.relative_to(dest.resolve())
            data = _read_member(zf, rel[m])
            written += len(data)
            if written > TASK_MAX:
                raise ValueError("задание больше допустимого размера после распаковки")
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as f:
                f.write(data)
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return key


def public_report(rep: dict) -> dict:
    """Отчёт без внутренних объектов: для базы и страницы."""
    return {k: v for k, v in rep.items() if not k.startswith("_")}


def import_archive(blob: bytes, file_name: str, created_by: int) -> dict:
    """Загрузить архив: каждое прошедшее проверку задание становится отдельным
    черновиком библиотеки. Возвращает {package_id, report, task_ids, error}."""
    rep = parse_archive(blob, file_name)
    if not rep.get("_zip"):
        return {"package_id": None, "report": public_report(rep), "task_ids": [], "error": rep["error"]}
    cur = db.execute(
        "INSERT INTO interactive_packages (file_name, size, title, format_version, tasks_total, created_by) "
        "VALUES (?,?,?,?,?,?)",
        (os.path.basename(file_name or "")[:200], len(blob), rep.get("title") or "", rep.get("format_version") or 1,
         len(rep["tasks"]), int(created_by or 0)))
    package_id = cur.lastrowid
    ids = []
    for t in rep["tasks"]:
        if t["problems"]:
            continue
        try:
            key = _extract(rep, t)
        except Exception as e:
            t["problems"].append(f"не удалось распаковать: {e}")
            continue
        m = t["meta"]
        cur = db.execute(
            "INSERT INTO interactive_tasks (title, description, subject, topic, status, grading, answer_key, max_score, "
            "allow_retry, max_attempts, frame_height, visibility, source, files_key, entry, package_id, manifest_id, "
            "created_by) VALUES (?,?,?,?, 'draft', ?,?,?,?,?,?,?, 'zip', ?,?,?,?,?)",
            (m["title"], m["description"], m["subject"], m["topic"], m["grading"], m["answer_key"], m["max_score"],
             m["allow_retry"], m["max_attempts"], m["frame_height"], m["visibility"], key, t["entry"], package_id,
             t["id"], int(created_by or 0)))
        t["task_id"] = cur.lastrowid
        ids.append(cur.lastrowid)
    pub = public_report(rep)
    db.execute("UPDATE interactive_packages SET tasks_ok=?, report_json=? WHERE id=?",
               (len(ids), json.dumps(pub, ensure_ascii=False), package_id))
    return {"package_id": package_id, "report": pub, "task_ids": ids, "error": "" if ids else rep.get("error")}


def get_package(package_id: int) -> Optional[dict]:
    row = db.fetchone("SELECT * FROM interactive_packages WHERE id=?", (int(package_id),))
    if not row:
        return None
    p = dict(row)
    try:
        p["report"] = json.loads(p.get("report_json") or "{}")
    except ValueError:
        p["report"] = {}
    p["tasks"] = [dict(r) for r in db.fetchall(
        "SELECT t.*, (SELECT COUNT(*) FROM lesson_blocks b WHERE b.task_id=t.id) AS lessons_count "
        "FROM interactive_tasks t WHERE t.package_id=? ORDER BY t.id", (int(package_id),))]
    return p


def list_packages(limit: int = 30) -> list:
    return [dict(r) for r in db.fetchall(
        "SELECT p.*, (SELECT COUNT(*) FROM interactive_tasks t WHERE t.package_id=p.id) AS alive "
        "FROM interactive_packages p ORDER BY p.id DESC LIMIT ?", (int(limit),))]


# ───────────────────────── версии ─────────────────────────

def _apply_meta(task_id: int, meta: dict) -> None:
    if not meta:
        return
    db.execute(
        "UPDATE interactive_tasks SET title=?, description=?, subject=?, topic=?, grading=?, answer_key=?, "
        "max_score=?, allow_retry=?, max_attempts=?, frame_height=? WHERE id=?",
        (meta["title"], meta["description"], meta["subject"], meta["topic"], meta["grading"], meta["answer_key"],
         meta["max_score"], meta["allow_retry"], meta["max_attempts"], meta["frame_height"], int(task_id)))


def replace_archive(task_id: int, blob: bytes, file_name: str) -> dict:
    """Новая версия задания из архива. У опубликованного задания она ждёт проверки
    (ученики видят прежнюю, пока админ не нажмёт «Опубликовать новую версию»).
    У черновика заменяется сразу, прежняя версия остаётся для отката."""
    task = it.get(task_id)
    if not task:
        return {"error": "Задание не найдено."}
    rep = parse_archive(blob, file_name)
    if not rep.get("_zip"):
        return {"error": rep["error"], "report": public_report(rep)}
    good = [t for t in rep["tasks"] if not t["problems"]]
    pick = None
    if len(rep["tasks"]) == 1 and good:
        pick = good[0]
    else:
        same = [t for t in good if task.get("manifest_id") and t["id"] == task["manifest_id"]]
        if len(same) == 1:
            pick = same[0]
    if not pick:
        bad = [t for t in rep["tasks"] if t["problems"]]
        if len(rep["tasks"]) == 1 and bad:
            return {"error": "Задание в архиве не прошло проверку: " + "; ".join(bad[0]["problems"]),
                    "report": public_report(rep)}
        return {"error": (f"В архиве {len(rep['tasks'])} заданий. Для замены нужен архив с одним заданием "
                          f"или задание с тем же id «{task.get('manifest_id') or '—'}»."),
                "report": public_report(rep)}
    key = _extract(rep, pick)
    meta = pick["meta"]
    if task["status"] == it.PUBLISHED:
        delete_key(task.get("pending_files_key"))
        db.execute("UPDATE interactive_tasks SET pending_files_key=?, pending_entry=?, pending_note=?, updated_at=? "
                   "WHERE id=?", (key, pick["entry"], json.dumps(meta, ensure_ascii=False), utils.now_iso(), task_id))
        return {"ok": True, "pending": True, "report": public_report(rep)}
    _swap_in(task, key, pick["entry"], meta)
    return {"ok": True, "pending": False, "report": public_report(rep)}


def _swap_in(task: dict, key: str, entry: str, meta: Optional[dict]) -> None:
    """Сделать версию текущей; текущая становится прошлой (для отката), а та, что
    была прошлой, удаляется."""
    old_prev = task.get("prev_files_key")
    if old_prev and old_prev not in (key, task.get("files_key")):
        delete_key(old_prev)
    db.execute(
        "UPDATE interactive_tasks SET source='zip', prev_files_key=files_key, prev_entry=entry, files_key=?, entry=?, "
        "version=version+1, updated_at=? WHERE id=?",
        (key, entry, utils.now_iso(), task["id"]))
    _apply_meta(task["id"], meta or {})
    _key_cache.clear()


def publish_pending(task_id: int) -> str:
    task = it.get(task_id)
    if not task or not task.get("pending_files_key"):
        return "Новой версии нет."
    try:
        meta = json.loads(task.get("pending_note") or "{}")
    except ValueError:
        meta = {}
    _swap_in(task, task["pending_files_key"], task.get("pending_entry") or "index.html", meta)
    db.execute("UPDATE interactive_tasks SET pending_files_key=NULL, pending_entry=NULL, pending_note=NULL WHERE id=?",
               (task_id,))
    return ""


def discard_pending(task_id: int) -> None:
    task = it.get(task_id)
    if task and task.get("pending_files_key"):
        delete_key(task["pending_files_key"])
        db.execute("UPDATE interactive_tasks SET pending_files_key=NULL, pending_entry=NULL, pending_note=NULL "
                   "WHERE id=?", (task_id,))


def rollback(task_id: int) -> str:
    """Вернуть прошлую версию. Текущая становится прошлой: откат можно отменить."""
    task = it.get(task_id)
    if not task or not task.get("prev_files_key") or not key_dir(task["prev_files_key"]).exists():
        return "Прошлой версии нет."
    db.execute(
        "UPDATE interactive_tasks SET files_key=prev_files_key, entry=prev_entry, prev_files_key=?, prev_entry=?, "
        "version=version+1, updated_at=? WHERE id=?",
        (task["files_key"], task["entry"], utils.now_iso(), task_id))
    _key_cache.clear()
    return ""


def editable_key(task: dict) -> tuple:
    """Куда писать правку файла: в ожидающую версию, а у опубликованного задания
    без неё — в новую копию, чтобы ученики не увидели правку до публикации."""
    if task.get("pending_files_key"):
        return task["pending_files_key"], "pending"
    if task["status"] == it.PUBLISHED:
        new = copy_key(task["files_key"])
        if not new:
            return None, ""
        db.execute("UPDATE interactive_tasks SET pending_files_key=?, pending_entry=?, pending_note=NULL WHERE id=?",
                   (new, task.get("entry") or "index.html", task["id"]))
        _key_cache.clear()
        return new, "pending"
    return task["files_key"], "current"


def save_text_file(task_id: int, rel: str, content: str) -> dict:
    task = it.get(task_id)
    if not task or task.get("source") != "zip":
        return {"error": "Задание не найдено."}
    ext = os.path.splitext(rel or "")[1].lower()
    if ext not in TEXT_EDITABLE:
        return {"error": "Этот файл не текстовый, его можно только заменить архивом."}
    if len(content.encode("utf-8")) > FILE_MAX:
        return {"error": "Файл слишком большой."}
    key, where = editable_key(task)
    if not key:
        return {"error": "Файлы задания не найдены."}
    base = key_dir(key)
    rel = rel.replace("\\", "/").strip("/")
    if _bad_path(rel):
        return {"error": "Недопустимый путь."}
    target = (base / rel).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:
        return {"error": "Недопустимый путь."}
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    db.execute("UPDATE interactive_tasks SET updated_at=? WHERE id=?", (utils.now_iso(), task_id))
    return {"ok": True, "where": where}


def read_text_file(key: str, rel: str) -> Optional[str]:
    base = key_dir(key or "")
    if not base:
        return None
    rel = (rel or "").replace("\\", "/").strip("/")
    if _bad_path(rel) or os.path.splitext(rel)[1].lower() not in TEXT_EDITABLE:
        return None
    p = (base / rel).resolve()
    try:
        p.relative_to(base.resolve())
    except ValueError:
        return None
    if not p.is_file() or p.stat().st_size > FILE_MAX:
        return None
    return p.read_text(encoding="utf-8", errors="replace")


def delete_task_files(task: dict) -> None:
    for k in ("files_key", "pending_files_key", "prev_files_key"):
        if task.get(k):
            delete_key(task[k])


def duplicate_files(src: dict, new_id: int) -> None:
    """Копия задания из архива получает собственную копию файлов."""
    key = copy_key(src["files_key"]) if src.get("files_key") else None
    db.execute("UPDATE interactive_tasks SET source='zip', files_key=?, entry=?, manifest_id=? WHERE id=?",
               (key, src.get("entry"), src.get("manifest_id"), new_id))


# ───────────────────────── документ задания ─────────────────────────

def files_base(origin: str, key: str) -> str:
    return f"{origin}/interactive/files/{key}/"


def csp(origin: str, key: str) -> str:
    """Политика для задания из архива: файлы — только из его собственной папки
    (или с разрешённых CDN), запросы по сети — только к своим файлам."""
    files = files_base(origin, key)
    return ("default-src 'none'; "
            f"script-src 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' blob: {it.CDN_SCRIPT} {files}; "
            f"style-src 'unsafe-inline' {it.CDN_STYLE} {files}; "
            f"img-src {files} {origin}/interactive/asset/ data: blob: https:; "
            f"media-src {files} data: blob: https:; "
            f"font-src data: {it.CDN_FONT} {files}; "
            f"connect-src {files} data: blob:; worker-src blob: {files}; "
            f"form-action 'none'; base-uri {files}")


def read_entry(key: str, entry: str) -> Optional[str]:
    base = key_dir(key or "")
    if not base:
        return None
    entry = (entry or "index.html").replace("\\", "/").strip("/")
    p = (base / entry).resolve()
    try:
        p.relative_to(base.resolve())
    except ValueError:
        return None
    if not p.is_file() or p.stat().st_size > ENTRY_MAX:
        return None
    return p.read_bytes().decode("utf-8", "replace")


def build_document(task: dict, origin: str, init: Optional[dict] = None, which: str = "current") -> Optional[str]:
    """HTML-документ задания из архива. which: current | pending | prev (две последние — только админу)."""
    key, entry = {
        "pending": (task.get("pending_files_key"), task.get("pending_entry")),
        "prev": (task.get("prev_files_key"), task.get("prev_entry")),
    }.get(which, (task.get("files_key"), task.get("entry")))
    if not key:
        return None
    doc = read_entry(key, entry or "index.html")
    if doc is None:
        return None
    sub = posixpath.dirname((entry or "index.html").strip("/"))
    base_href = files_base(origin, key) + (sub + "/" if sub else "")
    head = (f'<meta charset="utf-8"><base href="{base_href}">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta http-equiv="Content-Security-Policy" content="{csp(origin, key)}">'
            + it.BRIDGE.replace("__SMARTENT_INIT__", it._init_json(dict(init or {}, base=base_href))))
    doc = doc.lstrip("﻿")
    m = re.search(r"<head(\s[^>]*)?>", doc, re.I)
    if m:
        return doc[:m.end()] + head + doc[m.end():]
    m = re.search(r"<html(\s[^>]*)?>", doc, re.I)
    if m:
        return doc[:m.end()] + "<head>" + head + "</head>" + doc[m.end():]
    m = re.match(r"\s*<!doctype[^>]*>", doc, re.I)
    if m:
        return doc[:m.end()] + "<html><head>" + head + "</head><body>" + doc[m.end():] + "</body></html>"
    return "<!DOCTYPE html><html><head>" + head + "</head><body>" + doc + "</body></html>"
