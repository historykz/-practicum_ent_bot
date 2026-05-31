"""
Резервное копирование и восстановление.
Экспорт: ZIP с backup.json (структура) + media/ (фото) + settings.json + users_access.json.
Импорт: разворачивает обратно. Режимы: заменить всё / добавить к существующим.
"""
import os
import io
import json
import time
import zipfile
import logging
from datetime import datetime
from typing import Optional

import database as db

log = logging.getLogger(__name__)

BACKUP_DIR = "/tmp/ent_backups"
os.makedirs(BACKUP_DIR, exist_ok=True)

# Таблицы которые выгружаем целиком (структура тестов)
STRUCTURE_TABLES = [
    "test_categories",
    "tests",
    "questions",
    "question_options",
]
ACCESS_TABLES = ["private_test_access"]


def _dump_table(table: str) -> list:
    try:
        return db.fetchall(f"SELECT * FROM {table}")
    except Exception as e:
        log.warning("dump %s: %s", table, e)
        return []


def collect_backup_data() -> dict:
    """Собрать всю структуру в dict."""
    data = {
        "version": 2,
        "created_at": datetime.utcnow().isoformat(),
        "tables": {},
    }
    for t in STRUCTURE_TABLES:
        data["tables"][t] = _dump_table(t)
    return data


def collect_access_data() -> dict:
    out = {"tables": {}}
    for t in ACCESS_TABLES:
        out["tables"][t] = _dump_table(t)
    return out


def collect_settings_data() -> dict:
    rows = _dump_table("settings")
    return {"settings": rows}


def backup_counts() -> dict:
    def cnt(sql):
        r = db.fetchone(sql)
        return (r['c'] if r else 0) or 0
    return {
        "categories": cnt("SELECT COUNT(*) AS c FROM test_categories"),
        "tests": cnt("SELECT COUNT(*) AS c FROM tests"),
        "questions": cnt("SELECT COUNT(*) AS c FROM questions"),
        "media": cnt("SELECT COUNT(*) AS c FROM questions WHERE photo_file_id IS NOT NULL"),
    }


async def create_backup_zip(bot) -> str:
    """
    Создаёт ZIP-файл бэкапа. Качает фото в media/. Возвращает путь к файлу.
    """
    data = collect_backup_data()
    access = collect_access_data()
    settings = collect_settings_data()

    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    path = os.path.join(BACKUP_DIR, f"backup_{ts}.zip")

    # Качаем фото вопросов
    media_map = {}  # question_id -> filename в архиве
    photos = db.fetchall(
        "SELECT id, photo_file_id FROM questions WHERE photo_file_id IS NOT NULL")

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Медиа
        for p in photos:
            fid = p['photo_file_id']
            qid = p['id']
            try:
                tg_file = await bot.get_file(fid)
                buf = io.BytesIO()
                await bot.download_file(tg_file.file_path, destination=buf)
                fname = f"media/q_{qid}.jpg"
                zf.writestr(fname, buf.getvalue())
                media_map[str(qid)] = fname
            except Exception as e:
                log.warning("backup media q%s: %s", qid, e)

        # Привязка media к вопросам
        data["media_map"] = media_map

        zf.writestr("backup.json",
                    json.dumps(data, ensure_ascii=False, indent=2))
        zf.writestr("users_access.json",
                    json.dumps(access, ensure_ascii=False, indent=2))
        zf.writestr("settings.json",
                    json.dumps(settings, ensure_ascii=False, indent=2))

    return path


def _insert_row(table: str, row: dict, keep_id: bool = True):
    """Вставить строку в таблицу. row — dict."""
    cols = list(row.keys())
    if not keep_id and 'id' in cols:
        cols.remove('id')
    placeholders = ",".join("?" for _ in cols)
    col_sql = ",".join(cols)
    vals = [row[c] for c in cols]
    db.execute(
        f"INSERT OR REPLACE INTO {table} ({col_sql}) VALUES ({placeholders})",
        tuple(vals))


async def restore_backup(bot, zip_path: str, mode: str = "replace") -> dict:
    """
    Восстановить из ZIP.
    mode='replace' — удалить текущие тесты и восстановить.
    mode='append'  — добавить к существующим (новые id).
    Возвращает отчёт.
    """
    report = {
        "categories": 0, "tests": 0, "questions": 0,
        "options": 0, "media": 0, "media_failed": 0,
        "access": 0, "errors": [],
    }
    try:
        zf = zipfile.ZipFile(zip_path, "r")
    except Exception as e:
        report["errors"].append(f"Не открыть архив: {e}")
        return report

    try:
        data = json.loads(zf.read("backup.json").decode("utf-8"))
    except Exception as e:
        report["errors"].append(f"Нет backup.json: {e}")
        return report

    try:
        access = json.loads(zf.read("users_access.json").decode("utf-8"))
    except Exception:
        access = {"tables": {}}
    try:
        settings = json.loads(zf.read("settings.json").decode("utf-8"))
    except Exception:
        settings = {"settings": []}

    tables = data.get("tables", {})
    media_map = data.get("media_map", {})

    # REPLACE — чистим текущее
    if mode == "replace":
        try:
            db.execute("DELETE FROM question_options")
            db.execute("DELETE FROM questions")
            db.execute("DELETE FROM tests")
            db.execute("DELETE FROM test_categories")
            db.execute("DELETE FROM private_test_access")
        except Exception as e:
            report["errors"].append(f"Очистка: {e}")

    keep_id = (mode == "replace")  # при replace сохраняем id, при append — новые

    # Если append — нужно перемапить id (категории, тесты, вопросы)
    cat_id_map = {}
    test_id_map = {}
    q_id_map = {}

    # 1. Категории
    for row in tables.get("test_categories", []):
        old_id = row.get('id')
        try:
            if keep_id:
                _insert_row("test_categories", dict(row), keep_id=True)
                cat_id_map[old_id] = old_id
            else:
                r2 = dict(row); r2.pop('id', None)
                cur = db.execute(
                    "INSERT INTO test_categories (name,emoji,sort_order,created_by,is_required) "
                    "VALUES (?,?,?,?,?)",
                    (r2.get('name'), r2.get('emoji','📚'), r2.get('sort_order',0),
                     r2.get('created_by'), r2.get('is_required',0)))
                cat_id_map[old_id] = cur.lastrowid
            report["categories"] += 1
        except Exception as e:
            report["errors"].append(f"Категория {old_id}: {e}")

    # 2. Тесты
    for row in tables.get("tests", []):
        old_id = row.get('id')
        r = dict(row)
        # перемап категории при append
        if not keep_id and r.get('category_id') in cat_id_map:
            r['category_id'] = cat_id_map[r['category_id']]
        try:
            if keep_id:
                _insert_row("tests", r, keep_id=True)
                test_id_map[old_id] = old_id
            else:
                r.pop('id', None)
                cols = list(r.keys())
                ph = ",".join("?" for _ in cols)
                cur = db.execute(
                    f"INSERT INTO tests ({','.join(cols)}) VALUES ({ph})",
                    tuple(r[c] for c in cols))
                test_id_map[old_id] = cur.lastrowid
            report["tests"] += 1
        except Exception as e:
            report["errors"].append(f"Тест {old_id}: {e}")

    # 3. Вопросы
    for row in tables.get("questions", []):
        old_id = row.get('id')
        r = dict(row)
        if not keep_id and r.get('test_id') in test_id_map:
            r['test_id'] = test_id_map[r['test_id']]
        try:
            if keep_id:
                _insert_row("questions", r, keep_id=True)
                q_id_map[old_id] = old_id
            else:
                r.pop('id', None)
                r.pop('serial_no', None)  # пересоздастся триггером
                cols = list(r.keys())
                ph = ",".join("?" for _ in cols)
                cur = db.execute(
                    f"INSERT INTO questions ({','.join(cols)}) VALUES ({ph})",
                    tuple(r[c] for c in cols))
                q_id_map[old_id] = cur.lastrowid
            report["questions"] += 1
        except Exception as e:
            report["errors"].append(f"Вопрос {old_id}: {e}")

    # 4. Варианты ответов
    for row in tables.get("question_options", []):
        r = dict(row)
        if not keep_id and r.get('question_id') in q_id_map:
            r['question_id'] = q_id_map[r['question_id']]
        try:
            if keep_id:
                _insert_row("question_options", r, keep_id=True)
            else:
                r.pop('id', None)
                cols = list(r.keys())
                ph = ",".join("?" for _ in cols)
                db.execute(
                    f"INSERT INTO question_options ({','.join(cols)}) VALUES ({ph})",
                    tuple(r[c] for c in cols))
            report["options"] += 1
        except Exception as e:
            report["errors"].append(f"Вариант: {e}")

    # 5. Доступы
    for row in access.get("tables", {}).get("private_test_access", []):
        r = dict(row)
        if not keep_id and r.get('test_id') in test_id_map:
            r['test_id'] = test_id_map[r['test_id']]
        try:
            r.pop('id', None)
            cols = list(r.keys())
            ph = ",".join("?" for _ in cols)
            db.execute(
                f"INSERT OR IGNORE INTO private_test_access ({','.join(cols)}) VALUES ({ph})",
                tuple(r[c] for c in cols))
            report["access"] += 1
        except Exception as e:
            report["errors"].append(f"Доступ: {e}")

    # 6. Настройки (только при replace)
    if mode == "replace":
        for row in settings.get("settings", []):
            try:
                db.execute(
                    "INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)",
                    (row.get('key'), row.get('value')))
            except Exception:
                pass

    # 7. Медиа — заливаем фото обратно (через админский чат для получения file_id)
    media_to_upload = []  # (target_qid, bytes)
    for qid_str, fname in media_map.items():
        try:
            content = zf.read(fname)
            target_qid = int(qid_str)
            if not keep_id and int(qid_str) in q_id_map:
                target_qid = q_id_map[int(qid_str)]
            media_to_upload.append((target_qid, content))
        except Exception as e:
            report["media_failed"] += 1
            report["errors"].append(f"Медиа q{qid_str}: чтение {e}")

    # Реальная заливка фото (нужен chat_id админа)
    admin_chat = getattr(restore_backup, "_admin_chat", None)
    if admin_chat and media_to_upload:
        from aiogram.types import BufferedInputFile
        for target_qid, content in media_to_upload:
            try:
                photo = BufferedInputFile(content, filename=f"q_{target_qid}.jpg")
                msg = await bot.send_photo(
                    admin_chat, photo,
                    caption=f"♻️ Восстановление фото для вопроса #{target_qid}")
                new_fid = msg.photo[-1].file_id
                db.execute("UPDATE questions SET photo_file_id=? WHERE id=?",
                            (new_fid, target_qid))
                report["media"] += 1
            except Exception as e:
                report["media_failed"] += 1
                report["errors"].append(f"Медиа q{target_qid}: заливка {e}")
    elif media_to_upload:
        # Нет chat — просто отметим что медиа есть но не залито
        report["media_failed"] += len(media_to_upload)

    zf.close()
    return report
