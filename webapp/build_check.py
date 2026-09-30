"""
Все ли файлы на сервере — из одной версии.

Чаще всего после обновления «всё ломается» не из-за кода, а потому что до
сервера дошла только часть файлов: например, папка webapp новая, а корневой
utils.py остался от старой версии (GitHub через сайт принимает не больше
100 файлов за раз, а в проекте их больше). При сборке архива рядом с этим
файлом кладётся build_manifest.json — отпечатки всех файлов версии. При
запуске сайт сверяет их с тем, что реально лежит на сервере, и показывает
администратору расхождения: плашкой на сайте, на странице ошибки, в
/health/ready и сообщением в боте.
"""
import hashlib
import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = Path(__file__).resolve().parent / "build_manifest.json"
_result: Optional[dict] = None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def check(force: bool = False) -> dict:
    """{version, ok, stale, missing} — считается один раз за запуск процесса."""
    global _result
    if _result is not None and not force:
        return _result
    empty = {"version": None, "ok": True, "stale": [], "missing": [], "manifest": False}
    try:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        files = dict(data.get("files") or {})
    except FileNotFoundError:
        _result = empty
        return _result
    except Exception as e:
        log.warning("build_manifest.json не читается: %s", e)
        _result = empty
        return _result
    stale, missing = [], []
    for rel, digest in sorted(files.items()):
        path = ROOT / rel
        try:
            if not path.is_file():
                missing.append(rel)
            elif _sha256(path) != digest:
                stale.append(rel)
        except OSError:
            missing.append(rel)
    _result = {"version": data.get("version"), "ok": not stale and not missing,
               "stale": stale, "missing": missing, "manifest": True}
    if not _result["ok"]:
        log.error("Файлы на сервере не из версии %s. Отличаются: %s. Нет на сервере: %s. "
                  "Загрузите папку ent_bot целиком.", _result["version"],
                  ", ".join(stale) or "—", ", ".join(missing) or "—")
    return _result


def summary(limit: int = 12) -> str:
    """Одна строка для администратора; пустая — всё совпадает."""
    r = check()
    if r["ok"]:
        return ""
    bad = r["stale"] + r["missing"]
    names = ", ".join(bad[:limit]) + (f" и ещё {len(bad) - limit}" if len(bad) > limit else "")
    return (f"⚠️ На сервере файлы из разных версий (ожидается {r['version']}): {names}. "
            f"Загрузите папку ent_bot целиком — отдельными файлами легко пропустить корневые.")
