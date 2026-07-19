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
    "test_modes",
]
ACCESS_TABLES = ["private_test_access", "premium_users", "paid_access"]
# Пользователи и их прогресс (достижения, баны, рефералы, результаты)
USER_TABLES = [
    "users",
    "referrals",
    "user_achievements",
    "test_attempts",
    "attempt_answers",
    "chat_moderation",
    "purchases",
    "mode_passes",
    "mode_results",
]


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


def collect_users_data() -> dict:
    """Пользователи и весь их прогресс."""
    out = {"tables": {}}
    for t in USER_TABLES:
        out["tables"][t] = _dump_table(t)
    return out


def backup_counts() -> dict:
    def cnt(sql):
        r = db.fetchone(sql)
        return (r['c'] if r else 0) or 0
    return {
        "categories": cnt("SELECT COUNT(*) AS c FROM test_categories"),
        "tests": cnt("SELECT COUNT(*) AS c FROM tests"),
        "questions": cnt("SELECT COUNT(*) AS c FROM questions"),
        "media": cnt("SELECT COUNT(*) AS c FROM questions WHERE photo_file_id IS NOT NULL"),
        "users": cnt("SELECT COUNT(*) AS c FROM users"),
    }


async def create_backup_zip(bot, include_media: bool = False) -> str:
    """
    Создаёт ZIP-файл бэкапа.
    include_media=False (по умолчанию) — БЕЗ картинок в архиве (лёгкий, до 1МБ).
        file_id картинок сохраняются в данных вопросов, картинки подтянутся
        по file_id при использовании (они хранятся на серверах Telegram).
    include_media=True — качает картинки в архив (тяжёлый, может превысить
        лимит Telegram 20МБ при восстановлении).
    """
    data = collect_backup_data()
    access = collect_access_data()
    settings = collect_settings_data()
    users = collect_users_data()

    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    suffix = "_full" if include_media else ""
    path = os.path.join(BACKUP_DIR, f"backup_{ts}{suffix}.zip")

    # Качаем фото вопросов только если include_media
    media_map = {}  # question_id -> filename в архиве
    photos = []
    if include_media:
        photos = db.fetchall(
            "SELECT id, photo_file_id FROM questions WHERE photo_file_id IS NOT NULL")

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Медиа (только в полном режиме)
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
        zf.writestr("users.json",
                    json.dumps(users, ensure_ascii=False, indent=2))

    return path


# ===================== МНОГОТОМНЫЙ БЭКАП (с картинками, части ≤19МБ) =====================

MAX_PART_BYTES = 19 * 1024 * 1024  # 19 МБ запас под лимит Telegram 20МБ


async def create_backup_parts(bot, progress_cb=None) -> list:
    """
    Создаёт бэкап с КАРТИНКАМИ, разбитый на части ≤19МБ.
    Возвращает список путей к ZIP-частям.

    part1 содержит все данные (тесты, вопросы, пользователи, доступы,
    покупки, прохождения режимов) + начало картинок.
    Остальные части — только картинки.
    Все части нужны для полного восстановления.
    """
    data = collect_backup_data()
    access = collect_access_data()
    settings = collect_settings_data()
    users = collect_users_data()

    ts = datetime.now().strftime("%Y-%m-%d_%H%M")

    # Все фото вопросов
    photos = db.fetchall(
        "SELECT id, photo_file_id FROM questions WHERE photo_file_id IS NOT NULL")

    # Скачиваем все картинки в память (qid -> bytes)
    media_blobs = {}  # qid -> bytes
    media_map = {}    # str(qid) -> filename
    total = len(photos)
    for i, p in enumerate(photos):
        fid = p['photo_file_id']
        qid = p['id']
        try:
            tg_file = await bot.get_file(fid)
            buf = io.BytesIO()
            await bot.download_file(tg_file.file_path, destination=buf)
            media_blobs[qid] = buf.getvalue()
            media_map[str(qid)] = f"media/q_{qid}.jpg"
        except Exception as e:
            log.warning("backup media q%s: %s", qid, e)
        if progress_cb and total and (i + 1) % 20 == 0:
            try:
                await progress_cb(i + 1, total)
            except Exception:
                pass

    data["media_map"] = media_map

    # JSON-данные (обычно небольшие)
    json_blobs = {
        "backup.json": json.dumps(data, ensure_ascii=False, indent=2).encode(),
        "users_access.json": json.dumps(access, ensure_ascii=False, indent=2).encode(),
        "settings.json": json.dumps(settings, ensure_ascii=False, indent=2).encode(),
        "users.json": json.dumps(users, ensure_ascii=False, indent=2).encode(),
    }
    json_size = sum(len(v) for v in json_blobs.values())

    parts = []
    part_idx = 1

    def _new_part():
        path = os.path.join(BACKUP_DIR, f"backup_{ts}_part{part_idx}.zip")
        return path, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED)

    # Часть 1 — JSON + сколько влезет картинок
    cur_path, zf = _new_part()
    cur_size = json_size
    for name, blob in json_blobs.items():
        zf.writestr(name, blob)

    qids_sorted = sorted(media_blobs.keys())
    for qid in qids_sorted:
        blob = media_blobs[qid]
        # Если не влезает в текущую часть — новая часть
        if cur_size + len(blob) > MAX_PART_BYTES and cur_size > json_size:
            zf.close()
            parts.append(cur_path)
            part_idx += 1
            cur_path, zf = _new_part()
            cur_size = 0
        zf.writestr(f"media/q_{qid}.jpg", blob)
        cur_size += len(blob)
    zf.close()
    parts.append(cur_path)

    return parts


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


async def restore_backup(bot, zip_path, mode: str = "replace") -> dict:
    """
    Восстановить из ZIP.
    zip_path — путь к ZIP, ИЛИ список путей (части многотомного бэкапа).
    mode='replace' — удалить текущие тесты и восстановить.
    mode='append'  — добавить к существующим (новые id).
    Возвращает отчёт.
    """
    report = {
        "categories": 0, "tests": 0, "questions": 0,
        "options": 0, "media": 0, "media_failed": 0,
        "access": 0, "errors": [],
    }
    # Поддержка нескольких частей: первая часть содержит JSON,
    # картинки могут быть распределены по всем частям.
    if isinstance(zip_path, (list, tuple)):
        part_paths = list(zip_path)
    else:
        part_paths = [zip_path]

    # Открываем все части; ищем ту что содержит backup.json
    open_zips = []
    main_zf = None
    try:
        for pp in part_paths:
            try:
                z = zipfile.ZipFile(pp, "r")
                open_zips.append(z)
                if "backup.json" in z.namelist():
                    main_zf = z
            except Exception as e:
                report["errors"].append(f"Не открыть часть {pp}: {e}")
        if main_zf is None:
            report["errors"].append("Ни в одной части нет backup.json")
            for z in open_zips:
                z.close()
            return report
        zf = main_zf
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
    try:
        users_data = json.loads(zf.read("users.json").decode("utf-8"))
    except Exception:
        users_data = {"tables": {}}

    tables = data.get("tables", {})
    media_map = data.get("media_map", {})
    # Диагностика: сколько фото заявлено в бэкапе
    report["media_in_map"] = len(media_map)
    # Считаем сколько РЕАЛЬНО есть в архиве (файлы media/)
    media_files_in_zip = 0
    for z in open_zips:
        for name in z.namelist():
            if name.startswith("media/") and name.endswith(".jpg"):
                media_files_in_zip += 1
    report["media_files_in_zip"] = media_files_in_zip

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
                # При append — если категория с таким именем уже есть, переиспользуем её
                existing = db.fetchone(
                    "SELECT id FROM test_categories WHERE name=?",
                    (row.get('name'),))
                if existing:
                    cat_id_map[old_id] = existing['id']
                else:
                    r2 = dict(row); r2.pop('id', None)
                    cur = db.execute(
                        "INSERT INTO test_categories (name,emoji,sort_order,created_by,is_required) "
                        "VALUES (?,?,?,?,?)",
                        (r2.get('name'), r2.get('emoji', '📚'),
                         r2.get('sort_order', 0), r2.get('created_by'),
                         r2.get('is_required', 0)))
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

    # 5б. Премиум-доступы
    for row in access.get("tables", {}).get("premium_users", []):
        r = dict(row)
        try:
            r.pop('id', None)
            cols = list(r.keys())
            ph = ",".join("?" for _ in cols)
            db.execute(
                f"INSERT OR REPLACE INTO premium_users ({','.join(cols)}) VALUES ({ph})",
                tuple(r[c] for c in cols))
            report["premium"] = report.get("premium", 0) + 1
        except Exception as e:
            report["errors"].append(f"Премиум: {e}")

    # 5в. Платные доступы (paid_access) — кто купил тесты
    for row in access.get("tables", {}).get("paid_access", []):
        r = dict(row)
        if not keep_id and r.get('test_id') in test_id_map:
            r['test_id'] = test_id_map[r['test_id']]
        try:
            r.pop('id', None)
            cols = list(r.keys())
            ph = ",".join("?" for _ in cols)
            db.execute(
                f"INSERT OR IGNORE INTO paid_access ({','.join(cols)}) VALUES ({ph})",
                tuple(r[c] for c in cols))
            report["paid_access"] = report.get("paid_access", 0) + 1
        except Exception as e:
            report["errors"].append(f"Платный доступ: {e}")

    # 5г. Настройки режимов (test_modes) — цены, вкл/выкл карточек/заучивания
    for row in data.get("tables", {}).get("test_modes", []):
        r = dict(row)
        if not keep_id and r.get('test_id') in test_id_map:
            r['test_id'] = test_id_map[r['test_id']]
        try:
            cols = list(r.keys())
            ph = ",".join("?" for _ in cols)
            db.execute(
                f"INSERT OR REPLACE INTO test_modes ({','.join(cols)}) VALUES ({ph})",
                tuple(r[c] for c in cols))
            report["test_modes"] = report.get("test_modes", 0) + 1
        except Exception as e:
            report["errors"].append(f"Режимы теста: {e}")

    # 6. Настройки (только при replace)
    if mode == "replace":
        for row in settings.get("settings", []):
            try:
                db.execute(
                    "INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)",
                    (row.get('key'), row.get('value')))
            except Exception:
                pass

    # 6б. Пользователи и их прогресс
    # users восстанавливаем ВСЕГДА (INSERT OR REPLACE по tg_id),
    # чтобы вернуть достижения, баны, стрики, профильные.
    ut = users_data.get("tables", {})
    user_restored = 0
    for row in ut.get("users", []):
        try:
            r = dict(row)
            cols = list(r.keys())
            ph = ",".join("?" for _ in cols)
            db.execute(
                f"INSERT OR REPLACE INTO users ({','.join(cols)}) VALUES ({ph})",
                tuple(r[c] for c in cols))
            user_restored += 1
        except Exception as e:
            report["errors"].append(f"Юзер: {e}")
    report["users"] = user_restored

    # Прочие user-таблицы (рефералы, достижения, результаты, модерация,
    # покупки, прохождения режимов карточки/заучивание)
    for tname in ["referrals", "user_achievements", "test_attempts",
                  "attempt_answers", "chat_moderation", "purchases",
                  "mode_passes", "mode_results"]:
        rows_t = ut.get(tname, [])
        n = 0
        for row in rows_t:
            try:
                r = dict(row)
                # id оставляем как есть (replace-режим), при append убираем
                if not keep_id:
                    r.pop('id', None)
                cols = list(r.keys())
                ph = ",".join("?" for _ in cols)
                db.execute(
                    f"INSERT OR REPLACE INTO {tname} ({','.join(cols)}) VALUES ({ph})",
                    tuple(r[c] for c in cols))
                n += 1
            except Exception:
                pass
        report[tname] = n

    # 7. Медиа — заливаем фото обратно (через админский чат для получения file_id)
    media_to_upload = []  # (target_qid, bytes)
    for qid_str, fname in media_map.items():
        try:
            # Картинка может быть в любой из частей — ищем
            content = None
            for z in open_zips:
                if fname in z.namelist():
                    content = z.read(fname)
                    break
            if content is None:
                report["media_failed"] += 1
                continue
            target_qid = int(qid_str)
            if not keep_id and int(qid_str) in q_id_map:
                target_qid = q_id_map[int(qid_str)]
            media_to_upload.append((target_qid, content))
        except Exception as e:
            report["media_failed"] += 1
            report["errors"].append(f"Медиа q{qid_str}: чтение {e}")

    # Заливка фото: шлём тихо, сразу удаляем, показываем ТОЛЬКО прогресс.
    # С задержкой между фото чтобы Telegram не блокировал (flood control).
    admin_chat = getattr(restore_backup, "_admin_chat", None)
    progress_msg_id = getattr(restore_backup, "_progress_msg_id", None)
    if admin_chat and media_to_upload:
        import asyncio as _asyncio
        from aiogram.types import BufferedInputFile
        from aiogram.exceptions import TelegramRetryAfter
        total_media = len(media_to_upload)
        done = 0
        for target_qid, content in media_to_upload:
            # До 5 попыток на каждое фото (на случай flood control)
            saved_ok = False
            for attempt in range(5):
                try:
                    photo = BufferedInputFile(content, filename=f"q_{target_qid}.jpg")
                    msg = await bot.send_photo(admin_chat, photo)
                    new_fid = msg.photo[-1].file_id
                    # Проверяем что вопрос существует перед записью
                    q_exists = db.fetchone("SELECT id FROM questions WHERE id=?",
                                            (target_qid,))
                    if q_exists:
                        db.execute("UPDATE questions SET photo_file_id=? WHERE id=?",
                                    (new_fid, target_qid))
                        saved_ok = True
                    else:
                        log.warning("restore: вопрос %s не найден для фото", target_qid)
                    # Удаляем сообщение ТОЛЬКО после успешной записи в БД
                    if saved_ok:
                        try:
                            await bot.delete_message(admin_chat, msg.message_id)
                        except Exception:
                            pass
                    report["media"] += 1
                    done += 1
                    break  # успех — выходим из retry
                except TelegramRetryAfter as e:
                    # Telegram просит подождать N секунд — ждём и повторяем
                    wait = getattr(e, 'retry_after', 5)
                    log.warning("restore flood, ждём %s сек", wait)
                    if progress_msg_id:
                        try:
                            await bot.edit_message_text(
                                f"♻️ Восстановление…\n"
                                f"📷 Загружено: {done} из {total_media}\n"
                                f"⏳ Telegram просит паузу {wait} сек…",
                                chat_id=admin_chat, message_id=progress_msg_id)
                        except Exception:
                            pass
                    await _asyncio.sleep(wait + 1)
                except Exception as e:
                    # Сетевая ошибка/таймаут — подождём и попробуем ещё
                    if attempt < 4:
                        await _asyncio.sleep(2)
                        continue
                    report["media_failed"] += 1
                    break  # после 5 попыток — пропускаем это фото
            # Задержка между фото (защита от flood): ~0.3 сек
            await _asyncio.sleep(0.35)
            # Обновляем прогресс каждые 10 фото
            if progress_msg_id and (done % 10 == 0 or done == total_media):
                try:
                    await bot.edit_message_text(
                        f"♻️ Восстановление…\n"
                        f"📷 Загружено фото: {done} из {total_media}",
                        chat_id=admin_chat, message_id=progress_msg_id)
                except Exception:
                    pass
    elif media_to_upload:
        report["media_failed"] += len(media_to_upload)

    for z in open_zips:
        try:
            z.close()
        except Exception:
            pass
    return report


# ===================== АВТО-БЭКАП КАЖДЫЙ ДЕНЬ =====================

async def daily_backup_loop(bot):
    """
    Каждый день отправляет лёгкий бэкап главному админу в личку.
    Главный админ — первый в config._HARDCODED_ADMIN_IDS.
    """
    import asyncio
    import config
    from aiogram.types import FSInputFile

    # Главный админ
    admin_id = None
    try:
        if config._HARDCODED_ADMIN_IDS:
            admin_id = config._HARDCODED_ADMIN_IDS[0]
    except Exception:
        pass
    if not admin_id:
        log.warning("daily_backup: главный админ не найден, автобэкап выключен")
        return

    # Ждём минуту после старта, потом раз в 24 часа
    await asyncio.sleep(60)
    while True:
        try:
            # Полный бэкап с картинками, разбитый на части ≤19МБ
            parts = await create_backup_parts(bot)
            import os as _os
            from datetime import datetime as _dt
            today = _dt.now().strftime("%d.%m.%Y")
            n = len(parts)
            for i, path in enumerate(parts, 1):
                size_mb = _os.path.getsize(path) / 1024 / 1024
                if n == 1:
                    cap = (f"🗓 <b>Авто-бэкап за {today}</b> ({size_mb:.1f} МБ)\n\n"
                           "Полная копия: тесты, картинки, пользователи с "
                           "доступом, покупки, прохождения режимов.")
                else:
                    cap = (f"🗓 <b>Авто-бэкап за {today} — часть {i} из {n}</b> "
                           f"({size_mb:.1f} МБ)\n\n"
                           f"⚠️ Сохрани ВСЕ {n} частей для восстановления.")
                await bot.send_document(admin_id, FSInputFile(path),
                                         caption=cap, parse_mode="HTML")
            log.info("daily_backup (%d частей) отправлен админу %s", n, admin_id)
        except Exception as e:
            log.warning("daily_backup ошибка: %s", e)
        # Следующий бэкап через 24 часа
        await asyncio.sleep(24 * 3600)
