"""
Старые копии-ярлыки → независимые копии, без потери прогресса учеников.

До v69 «копия» предмета, раздела или урока была ярлыком: своя обложка, а
конспект, тест, видео, прогресс, доступы и деньги — у оригинала. Поэтому
копию нельзя было править отдельно. Здесь каждый ярлык один раз становится
полноценной записью — на том же id, так что ссылки учеников не меняются:

  • контент — ровно тот, что ученик видел: свой у копии, если админ его
    задавал (обложка, рабочая тетрадь, карточки, страницы из бота), иначе —
    копией с оригинала; тест и банк зачёта — отдельными копиями;
  • прогресс дублируется на копию, чтобы ничего не обнулилось: результаты
    тестов (с пометкой cloned_from_attempt_id и is_counted=0 — в общий
    рейтинг, достижения и статистику дубли не попадают), просмотры уроков,
    сданные зачёты, доступы к урокам, разделам и предмету, послабления
    стоп-уроков;
  • деньги и место в рейтинге у ученика в одном месте: если он учился через
    витрину (доступа к самому оригиналу у него нет), участие, начисления и
    грамота переезжают в копию. Уже оплаченные уроки переезжают вместе с
    отметкой — второй раз их не оплатят. Остальные остаются в оригинале;
    нажмут «Начать обучение» в копии — баланс переедет туда же
    (reward_service.start → transfer_participant).

Запускается из database.init_db — при первом старте v69 и после
восстановления старого бэкапа. Повторный запуск ничего не дублирует.
Перед первым переводом рядом с базой кладётся её страховочная копия.
"""
import json
import logging
import os
import shutil
import time
from datetime import datetime

import config
import database as db
import utils
from services import deep_copy_service as dc
from webapp import shortcuts as sc

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")


def _cols(table: str) -> list:
    return [r["name"] for r in db.fetchall(f"PRAGMA table_info({table})")]


def _row(table: str, row_id):
    if not row_id:
        return None
    r = db.fetchone(f"SELECT * FROM {table} WHERE id=?", (row_id,))
    return dict(r) if r else None


def _add(stats: dict, more: dict) -> None:
    for k, v in (more or {}).items():
        if isinstance(v, int) and not isinstance(v, bool):
            stats[k] = stats.get(k, 0) + v


def pending() -> dict:
    return {t: db.fetchone(f"SELECT COUNT(*) AS c FROM {t} WHERE original_id IS NOT NULL")["c"]
            for t in ("subjects", "sections", "lessons")}


def _snapshot():
    """Страховочная копия базы перед первым переводом (если хватает места)."""
    try:
        src = os.path.abspath(config.DB_PATH)
        folder = os.path.dirname(src)
        if shutil.disk_usage(folder).free < os.path.getsize(src) * 3:
            log.warning("перевод копий: мало места для страховочной копии базы — без неё")
            return None
        dest = os.path.join(folder, f"before_copy_convert_{time.strftime('%Y%m%d_%H%M%S')}.db")
        db.snapshot_to(dest)
        return dest
    except Exception as e:
        log.warning("перевод копий: страховочная копия базы не сделана: %s", e)
        return None


def _invalidate() -> None:
    dc._invalidate()


def convert_all() -> dict:
    """Все оставшиеся ярлыки: сначала предметы, потом разделы, потом уроки."""
    stats = {"subjects": 0, "sections": 0, "lessons": 0, "attempts": 0,
             "participants_moved": 0, "errors": 0}
    if not any(pending().values()):
        return stats
    stats["snapshot"] = _snapshot()
    for kind, fn in (("subjects", convert_subject), ("sections", convert_section),
                     ("lessons", convert_lesson)):
        for r in db.fetchall(f"SELECT id FROM {kind} WHERE original_id IS NOT NULL ORDER BY id"):
            try:
                _add(stats, fn(r["id"]))
            except Exception:
                stats["errors"] += 1
                log.exception("перевод копии: %s %s", kind, r["id"])
    _invalidate()
    return stats


# ───────────────────────── урок ─────────────────────────

def convert_lesson(lesson_id: int, inside_subject: bool = False, lesson_map: dict = None) -> dict:
    row = _row("lessons", lesson_id)
    if not row or not row.get("original_id"):
        return {}
    L2 = row["id"]
    L = int(sc.orig_lesson_id(L2))
    orig = _row("lessons", L) if L != L2 else None
    host = db.fetchone("SELECT subject_id FROM sections WHERE id=?", (row["section_id"],))
    if not orig:
        # Оригинал удалён — ученик видел пустую обложку, она и остаётся
        db.execute("UPDATE lessons SET original_id=NULL, copied_from_lesson_id=? WHERE id=?",
                   (row["original_id"], L2))
        return {"lessons": 1}
    job = dc.Job()
    try:
        upd = {}
        if not (row.get("content_html") or "").strip():
            upd["content_html"] = orig.get("content_html")
        if not db.fetchone("SELECT 1 FROM lesson_images WHERE lesson_id=? LIMIT 1", (L2,)):
            dc.copy_lesson_images(L, L2, job)
        if not row.get("video_file_id"):
            upd["video_file_id"] = orig.get("video_file_id")
            upd["video_file_unique"] = orig.get("video_file_unique")
        upd["video_url"] = dc.dup_file(orig.get("video_url"), "videos", job)
        upd["youtube_id"] = orig.get("youtube_id")
        if not (row.get("workbook_path") or "").strip():
            wb = dc.dup_file(orig.get("workbook_path"), "workbooks", job)
            upd.update(workbook_path=wb,
                       workbook_name=orig.get("workbook_name") if wb else None,
                       workbook_size=orig.get("workbook_size") if wb else None)
        if not (row.get("quizlet_url") or "").strip():
            upd["quizlet_url"] = orig.get("quizlet_url")
        for f in ("is_zachet", "zachet_per_attempt", "zachet_topic_threshold",
                  "zachet_pass_percent", "free_override"):
            upd[f] = orig.get(f)
        if row.get("price_stars") is None:
            upd["price_stars"] = orig.get("price_stars")
        new_tid = dc.copy_test(orig.get("test_id"), job) if orig.get("test_id") else None
        upd["test_id"] = new_tid
        zmap = {}
        if not db.fetchone("SELECT 1 FROM zachet_questions WHERE lesson_id=? LIMIT 1", (L2,)):
            zmap = dc.copy_zachet_bank(L, L2, job)
        # Прогресс — на копию, пока она ещё ярлык: сорвётся — откатится всё
        attempts = _clone_attempts(orig["test_id"], new_tid) if new_tid else 0
        _clone_zachet_attempts(L, L2, zmap)
        _clone_rows("lesson_progress", "lesson_id", L, L2)
        _clone_rows("lesson_access", "lesson_id", L, L2)
        upd.update(original_id=None, copied_from_lesson_id=L, legacy_lesson_id=L,
                   reward_price=0, reward_required=1)
        have = set(_cols("lessons"))
        upd = {k: v for k, v in upd.items() if k in have}
        db.execute(f"UPDATE lessons SET {', '.join(f'{k}=?' for k in upd)} WHERE id=?",
                   (*upd.values(), L2))
    except Exception:
        try:
            db.execute("DELETE FROM zachet_attempts WHERE lesson_id=? AND cloned_from_id IS NOT NULL", (L2,))
        except Exception:
            pass
        job.rollback()
        raise
    _rekey_notes(L, L2)
    if not inside_subject and host:
        _rekey_in_host(host["subject_id"], L, L2)
    if lesson_map is not None:
        lesson_map[L2] = L
    db.execute("UPDATE lessons SET copy_modified_at=NULL WHERE id=?", (L2,))
    return {"lessons": 1, "attempts": attempts}


def _clone_attempts(src_test: int, dst_test: int) -> int:
    """Результаты тестов — на тест копии, со всеми датами. is_counted=0:
    общий рейтинг, достижения и статистика дубли не видят."""
    have = _cols("test_attempts")
    if "cloned_from_attempt_id" not in have:
        return 0
    keep = [c for c in have if c not in ("id", "test_id", "is_counted", "cloned_from_attempt_id")]
    cur = db.execute(
        f"INSERT OR IGNORE INTO test_attempts ({', '.join(keep)}, test_id, is_counted, "
        f"cloned_from_attempt_id) SELECT {', '.join(keep)}, ?, 0, id FROM test_attempts "
        f"WHERE test_id=? AND status IN ('finished','aborted') AND COALESCE(publication_id,0)=0",
        (dst_test, src_test))
    return cur.rowcount or 0


def _remap_list(raw, m: dict):
    try:
        v = json.loads(raw or "[]")
        return json.dumps([m.get(int(x), x) if str(x).isdigit() else x for x in v]) \
            if isinstance(v, list) else raw
    except (ValueError, TypeError):
        return raw


def _remap_keys(raw, m: dict):
    try:
        v = json.loads(raw or "{}")
        return json.dumps({(str(m.get(int(k), k)) if str(k).isdigit() else k): x
                           for k, x in v.items()}, ensure_ascii=False) if isinstance(v, dict) else raw
    except (ValueError, TypeError):
        return raw


def _clone_zachet_attempts(src_lid: int, dst_lid: int, zmap: dict) -> int:
    have = _cols("zachet_attempts")
    if "cloned_from_id" not in have:
        return 0
    keep = [c for c in have if c not in ("id", "lesson_id", "cloned_from_id",
                                         "question_ids", "answers_json")]
    cols = keep + ["lesson_id", "question_ids", "answers_json", "cloned_from_id"]
    n = 0
    for r in db.fetchall("SELECT * FROM zachet_attempts WHERE lesson_id=? AND status='finished'",
                         (src_lid,)):
        r = dict(r)
        vals = [r.get(c) for c in keep] + [dst_lid, _remap_list(r.get("question_ids"), zmap),
                                           _remap_keys(r.get("answers_json"), zmap), r["id"]]
        cur = db.execute(f"INSERT OR IGNORE INTO zachet_attempts ({', '.join(cols)}) "
                         f"VALUES ({', '.join('?' * len(cols))})", vals)
        n += cur.rowcount or 0
    return n


def _clone_rows(table: str, col: str, src, dst) -> int:
    """Строки ученика «про этот урок/раздел» — такие же, но на копию."""
    have = [c for c in _cols(table) if c != "id"]
    if col not in have:
        return 0
    sel = ", ".join("?" if c == col else c for c in have)
    cur = db.execute(f"INSERT OR IGNORE INTO {table} ({', '.join(have)}) "
                     f"SELECT {sel} FROM {table} WHERE {col}=?", (dst, src))
    return cur.rowcount or 0


def _rekey_notes(L: int, L2: int) -> None:
    """Видео, отправленное через копию, бот помнил под id оригинала. Если в
    том же чате есть сообщения этого урока под id копии, значит, видео ушло
    через копию — и удаляться по сроку доступа оно должно вместе с ними."""
    try:
        have = set(_cols("note_messages"))
        who = "chat_id" if "chat_id" in have else ("user_tg_id" if "user_tg_id" in have else None)
        if not who or "lesson_id" not in have:
            return
        db.execute(f"UPDATE note_messages SET lesson_id=? WHERE lesson_id=? AND {who} IN "
                   f"(SELECT {who} FROM note_messages WHERE lesson_id=?)", (L2, L, L2))
    except Exception as e:
        log.warning("перевод копии: сообщения конспектов урока %s: %s", L2, e)


def _rekey_in_host(B: int, L: int, L2: int) -> None:
    """Ярлык урока в обычном предмете B: цена, оплата, «пройден до старта»
    и стоп-уроки в B были записаны под id оригинала — переводим на копию."""
    from services import reward_service as rs
    shared = db.fetchone(
        "SELECT COUNT(*) AS c FROM lessons l JOIN sections s ON s.id=l.section_id "
        "WHERE s.subject_id=? AND l.id<>? AND (l.id=? OR l.original_id=?)", (B, L2, L, L))["c"] > 0
    db.execute("INSERT OR IGNORE INTO reward_prices (subject_id, lesson_id, price, required, updated_at) "
               "SELECT subject_id, ?, price, required, updated_at FROM reward_prices "
               "WHERE subject_id=? AND lesson_id=?", (L2, B, L))
    for tx in db.fetchall("SELECT * FROM reward_transactions WHERE subject_id=? AND type='reward' "
                          "AND ref=?", (B, f"lesson:{L}")):
        tx = dict(tx)
        if shared:
            # Оригинал тоже в этом предмете: его строка остаётся, копии — отметка без денег
            rs._insert_tx(tx, "reward", 0, f"lesson:{L2}", lesson_id=L2,
                          reason=f"{tx.get('reason') or 'Урок'} — уже учтён", meta={"copy_of": L},
                          now=utils.parse_utc_naive(tx.get("created_at")))
        else:
            db.execute("UPDATE OR IGNORE reward_transactions SET lesson_id=?, ref=? WHERE id=?",
                       (L2, f"lesson:{L2}", tx["id"]))
    for p in db.fetchall("SELECT id, prior_lessons FROM subject_participants WHERE subject_id=?", (B,)):
        prior = set(rs._json_list(p["prior_lessons"]))
        if L in prior and L2 not in prior:
            prior.add(L2)
            if not shared:
                prior.discard(L)
            db.execute("UPDATE subject_participants SET prior_lessons=? WHERE id=?",
                       (json.dumps(sorted(prior)), p["id"]))
    if not shared:
        db.execute("UPDATE OR IGNORE stop_lesson_overrides SET lesson_id=? WHERE subject_id=? "
                   "AND lesson_id=?", (L2, B, L))


# ───────────────────────── раздел ─────────────────────────

def convert_section(section_id: int, inside_subject: bool = False, lesson_map: dict = None) -> dict:
    sec = _row("sections", section_id)
    if not sec or not sec.get("original_id"):
        return {}
    orig_sec = int(sc.orig_section_id(section_id))
    stats = {"sections": 1}
    if orig_sec != section_id:
        # Купившие раздел-оригинал открывали и эту копию — открывают и дальше
        _clone_rows("section_access", "section_id", orig_sec, section_id)
    for les in db.fetchall("SELECT id FROM lessons WHERE section_id=? AND original_id IS NOT NULL "
                           "ORDER BY sort_order, id", (section_id,)):
        _add(stats, convert_lesson(les["id"], inside_subject, lesson_map))
    db.execute("UPDATE sections SET original_id=NULL, copied_from_section_id=? WHERE id=?",
               (orig_sec if orig_sec != section_id else sec["original_id"], section_id))
    return stats


# ───────────────────────── предмет ─────────────────────────

def convert_subject(subject_id: int) -> dict:
    c = _row("subjects", subject_id)
    if not c or not c.get("original_id"):
        return {}
    from services import reward_service as rs
    C = c["id"]
    S = int(sc.orig_subject_id(C))
    s = _row("subjects", S) if S != C else None
    upd = _converted_settings(c, s)
    # Кто учился через витрину — решаем ДО перевода, пока витрина ещё часть
    # предмета, но по тем правилам доступа, которые у копии будут ПОСЛЕ него:
    # иначе ученик переехал бы туда, где сразу оказался бы вне рейтинга
    movers = _vitrina_students(s, {**c, **upd}) if s else []
    stats = {"subjects": 1, "participants_moved": 0}
    lesson_map = {}
    for sec in db.fetchall("SELECT id, original_id FROM sections WHERE subject_id=? "
                           "ORDER BY sort_order, id", (C,)):
        if sec["original_id"]:
            _add(stats, convert_section(sec["id"], True, lesson_map))
        else:
            for les in db.fetchall("SELECT id FROM lessons WHERE section_id=? AND original_id IS NOT NULL",
                                   (sec["id"],)):
                _add(stats, convert_lesson(les["id"], True, lesson_map))
    upd.update(original_id=None, copied_from_subject_id=S if s else c["original_id"],
               copied_at=c.get("created_at") or _now(), converted_at=_now(),
               copy_group_id=S if s else None)
    have = set(_cols("subjects"))
    upd = {k: v for k, v in upd.items() if k in have}
    db.execute(f"UPDATE subjects SET {', '.join(f'{k}=?' for k in upd)} WHERE id=?", (*upd.values(), C))
    try:
        if s:
            keys = lesson_keys(C)
            # Каждый шаг — отдельно: сбой в одном не должен оставить остальные
            # несделанными (повторно предмет уже не переводится — он не ярлык).
            for name, step in (
                ("цены", lambda: _copy_prices_by_key(S, C, keys)),
                ("доступ к предмету", lambda: _merge_subject_access(S, C)),
                ("стоп-уроки", lambda: _copy_stop_overrides(S, C, keys)),
            ):
                try:
                    step()
                except Exception:
                    log.exception("перевод копии %s: %s", C, name)
            for p in movers:
                try:
                    if transfer_participant(p, C):
                        stats["participants_moved"] += 1
                except Exception:
                    log.exception("перевод копии %s: участник %s", C, p.get("id"))
            _move_study_control(S, C)
            try:
                rs.rank_subject(S)
                rs.rank_subject(C)
            except Exception as e:
                log.warning("перевод копии: места в рейтинге: %s", e)
    finally:
        # Сам перевод изменением копии не считается
        db.execute("UPDATE subjects SET copy_modified_at=NULL WHERE id=?", (C,))
        db.execute("UPDATE lessons SET copy_modified_at=NULL WHERE section_id IN "
                   "(SELECT id FROM sections WHERE subject_id=?)", (C,))
    return stats


def _copy_prices_by_key(S: int, C: int, keys: dict) -> None:
    """Цены уроков оригинала — на уроки копии (по «общему ключу» урока)."""
    for lid, key in keys.items():
        db.execute("INSERT OR IGNORE INTO reward_prices (subject_id, lesson_id, price, required, "
                   "updated_at) SELECT ?, ?, price, required, updated_at FROM reward_prices "
                   "WHERE subject_id=? AND lesson_id=?", (C, lid, S, key))


def _converted_settings(c: dict, s) -> dict:
    """Настройки, с которыми витрина становится самостоятельной: то, что
    ученик видел на деле (часть правил жила у оригинала)."""
    upd = {}
    if not s:
        return upd
    if sc.subject_mode(s) == sc.OPEN:
        # Витрина открытого предмета была открыта всем — остаётся открытой
        upd.update(access_mode=sc.OPEN, is_open=1, is_private=0)
    elif sc.subject_mode(c) == sc.OPEN:
        # Витрина «открыт всем» над закрытым или премиум-оригиналом пускала
        # внутрь всех, а платные уроки открывала только Премиумом или
        # доступом. Это и есть «премиум»: «открыт» раздал бы их бесплатно.
        upd.update(access_mode=sc.PREMIUM, is_open=0, is_private=0)
    if not c.get("pass_percent"):
        # У витрины своего порога не было: баллы, деньги и контроль
        # обучения считались по порогу оригинала — он и остаётся
        upd["pass_percent"] = s.get("pass_percent")
    upd["premium_ignored"] = 1 if (c.get("premium_ignored") or s.get("premium_ignored")) else 0
    if not c.get("unit_sale_enabled") and s.get("unit_sale_enabled"):
        upd.update(unit_sale_enabled=1, unit_price_stars=s.get("unit_price_stars"))
    upd.update(rewards_enabled=s.get("rewards_enabled") or 0,
               rewards_enabled_at=s.get("rewards_enabled_at"),
               reward_pay_prior=s.get("reward_pay_prior") or 0)
    if not (c.get("quizlet_url") or "").strip():
        upd["quizlet_url"] = s.get("quizlet_url")
    return upd


def _covers(user_premium: bool, subj: dict) -> bool:
    return bool(user_premium and sc.subject_mode(subj) in (sc.OPEN, sc.PREMIUM)
                and not sc.premium_ignored(subj))


def _vitrina_students(s: dict, c: dict) -> list:
    """Участники оригинала, у которых доступ только через эту витрину.

    Если ученику открыты несколько витрин одного оригинала, угадывать, через
    какую он учился, не беремся: он остаётся в оригинале, а «Начать обучение»
    в нужной витрине сам перенесёт туда его баланс."""
    from services import reward_service as rs
    sibs = [dict(r) for r in db.fetchall("SELECT * FROM subjects WHERE id<>? AND "
                                         "(original_id=? OR copy_group_id=?)", (c["id"], s["id"], s["id"]))]
    out = []
    for p in db.fetchall("SELECT * FROM subject_participants WHERE subject_id=? AND status<>'annulled'",
                         (s["id"],)):
        p = dict(p)
        prem = utils.is_premium(p["user_id"])
        alone = rs._live_subject_access(p["tg_id"], [s["id"]]) or _covers(prem, s)
        via = rs._live_subject_access(p["tg_id"], [c["id"]]) or _covers(prem, c)
        other = any(rs._live_subject_access(p["tg_id"], [x["id"]]) or _covers(prem, x) for x in sibs)
        if via and not alone and not other:
            out.append(p)
    return out


def _later(a, b):
    """Более поздний срок из двух; пустой — бессрочно (побеждает)."""
    if not a or not b:
        return None
    da, dbb = utils.parse_utc_naive(a), utils.parse_utc_naive(b)
    if not da or not dbb:
        return a or b
    return a if da >= dbb else b


def _merge_subject_access(S: int, C: int) -> None:
    """Выданный на оригинал доступ открывал и витрину — теперь он есть у копии."""
    have = [c for c in _cols("subject_access") if c != "id"]
    for sa in db.fetchall("SELECT * FROM subject_access WHERE subject_id=?", (S,)):
        sa = dict(sa)
        ex = db.fetchone("SELECT id, expires_at FROM subject_access WHERE subject_id=? AND user_tg_id=?",
                         (C, sa["user_tg_id"]))
        if not ex:
            vals = [C if col == "subject_id" else sa.get(col) for col in have]
            db.execute(f"INSERT OR IGNORE INTO subject_access ({', '.join(have)}) "
                       f"VALUES ({', '.join('?' * len(have))})", vals)
        else:
            later = _later(ex["expires_at"], sa.get("expires_at"))
            if later != ex["expires_at"]:
                db.execute("UPDATE subject_access SET expires_at=? WHERE id=?", (later, ex["id"]))


def _copy_stop_overrides(S: int, C: int, keys: dict) -> None:
    """Послабления стоп-уроков, выданные на странице витрины, хранились под
    оригиналом. «Открыто до урока X» переводим на урок копии."""
    by_key = {}
    for lid, key in sorted(keys.items()):
        by_key.setdefault(key, []).append(lid)
    have = [c for c in _cols("stop_lesson_overrides") if c != "id"]
    for o in db.fetchall("SELECT * FROM stop_lesson_overrides WHERE subject_id=?", (S,)):
        o = dict(o)
        lid = o.get("lesson_id") or 0
        new_lid = 0
        if lid:
            if lid not in by_key:
                continue                 # такого урока в копии нет
            new_lid = by_key[lid][0]
        vals = [C if col == "subject_id" else (new_lid if col == "lesson_id" else o.get(col))
                for col in have]
        db.execute(f"INSERT OR IGNORE INTO stop_lesson_overrides ({', '.join(have)}) "
                   f"VALUES ({', '.join('?' * len(have))})", vals)


def _move_study_control(S: int, C: int) -> None:
    """Контроль обучения подключали ссылкой на витрину — он переезжает к копии."""
    try:
        from services import study_subjects as sj
        row = db.fetchone("SELECT * FROM study_subjects WHERE subject_id=?", (S,))
        if not row or sj.parse_subject_link(row["link"] or "") != C:
            return
        if db.fetchone("SELECT 1 FROM study_subjects WHERE subject_id=?", (C,)):
            return
        db.execute("UPDATE study_subjects SET subject_id=? WHERE id=?", (C, row["id"]))
        db.execute("UPDATE OR IGNORE study_tracking SET subject_id=? WHERE subject_id=?", (C, S))
    except Exception as e:
        log.warning("перевод копии: контроль обучения %s → %s: %s", S, C, e)


# ───────────────────────── перенос участника ─────────────────────────

def lesson_keys(subject_id: int) -> dict:
    """{урок предмета: «общий ключ»}. У урока переведённой копии ключ — урок
    оригинала (legacy_lesson_id), у остальных — он сам. По ключу находятся
    «один и тот же» урок в оригинале и в его бывших витринах."""
    rows = db.fetchall("SELECT l.id, l.legacy_lesson_id FROM lessons l JOIN sections s "
                       "ON s.id=l.section_id WHERE s.subject_id=? ORDER BY s.sort_order, s.id, "
                       "l.sort_order, l.id", (subject_id,))
    return {r["id"]: (r["legacy_lesson_id"] or r["id"]) for r in rows}


def transfer_participant(p: dict, dst_subject_id: int, note: bool = True) -> bool:
    """Участие ученика (баланс, операции, грамота, место) — в другой предмет
    той же программы. Оплаченные уроки переезжают с отметкой: второй раз
    их не оплатят."""
    from services import reward_service as rs
    uid, src = p["user_id"], p["subject_id"]
    if src == dst_subject_id or db.fetchone(
            "SELECT 1 FROM subject_participants WHERE user_id=? AND subject_id=?", (uid, dst_subject_id)):
        return False
    src_keys = lesson_keys(src)
    by_key = {}
    for lid, key in lesson_keys(dst_subject_id).items():
        by_key.setdefault(key, []).append(lid)
    db.execute("UPDATE subject_participants SET subject_id=? WHERE id=?", (dst_subject_id, p["id"]))
    db.execute("UPDATE OR IGNORE reward_transactions SET subject_id=? WHERE user_id=? AND subject_id=?",
               (dst_subject_id, uid, src))
    for tx in db.fetchall("SELECT * FROM reward_transactions WHERE user_id=? AND subject_id=? "
                          "AND type='reward' AND ref LIKE 'lesson:%'", (uid, dst_subject_id)):
        tx = dict(tx)
        try:
            old = int(tx["ref"].split(":", 1)[1])
        except (ValueError, IndexError):
            continue
        targets = by_key.get(src_keys.get(old, old))
        if not targets or old in targets:
            continue
        db.execute("UPDATE OR IGNORE reward_transactions SET lesson_id=?, ref=? WHERE id=?",
                   (targets[0], f"lesson:{targets[0]}", tx["id"]))
        for extra in targets[1:]:
            rs._insert_tx({"user_id": uid, "tg_id": tx["tg_id"], "subject_id": dst_subject_id},
                          "reward", 0, f"lesson:{extra}", lesson_id=extra,
                          reason=f"{tx.get('reason') or 'Урок'} — уже учтён", meta={"copy_of": old},
                          now=utils.parse_utc_naive(tx.get("created_at")))
    prior = rs._json_list(p.get("prior_lessons"))
    new_prior = sorted({t for x in prior for t in by_key.get(src_keys.get(x, x), [])})
    db.execute("UPDATE subject_participants SET prior_lessons=? WHERE id=?",
               (json.dumps(new_prior), p["id"]))
    db.execute("UPDATE OR IGNORE reward_certificates SET subject_id=? WHERE user_id=? AND subject_id=?",
               (dst_subject_id, uid, src))
    if note:
        dst = _row("subjects", dst_subject_id) or {}
        what = ("прогресс, баланс и место в рейтинге" if dst.get("rewards_enabled")
                else "прогресс и место в рейтинге")
        rs.queue(p["tg_id"], f"🔁 Ваши {what} перенесены в предмет «{dst.get('title', '')}» — "
                             f"теперь вы учитесь в нём. Ничего не потеряно.",
                 dedup=f"transfer:{p['id']}:{dst_subject_id}")
    return True
