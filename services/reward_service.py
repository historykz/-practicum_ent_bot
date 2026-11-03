"""
Денежные вознаграждения, штрафы, рейтинг участников и итоговая грамота —
отдельно по каждому предмету.

Центральная точка — кнопка «✨ НАЧАТЬ ОБУЧЕНИЕ ✨» на странице предмета
(start()). Одно нажатие:
  • фиксирует дату и время начала (subject_participants.started_at);
  • делает ученика участником рейтинга предмета (одна запись на пару
    «ученик + предмет», повторное нажатие ничего не дублирует);
  • сохраняет весь старый прогресс: пройденные уроки, баллы, достижения
    остаются как были, в рейтинг он встаёт сразу со своими баллами;
  • запускает вознаграждения за уроки и контроль пропусков.

Деньги хранятся не одним числом, а операциями (reward_transactions):
reward, penalty, bonus, manual_adjustment, correction. Баланс — их сумма.
Суммы — в тиынах (1 ₸ = 100), чтобы штраф 62,50 ₸ считался точно.
Уникальный ref операции не даёт дважды оплатить урок или дважды
оштрафовать за один день — ни при двойном клике, ни при повторе запроса,
ни при повторном проходе фоновой задачи.

Дни считаются по Астане (UTC+5).
"""
import asyncio
import hashlib
import hmac
import json
import logging
import random
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Optional

import database as db
import utils
from webapp import shortcuts as sc

log = logging.getLogger(__name__)

ALMATY = timezone(timedelta(hours=5))
PENALTY_BASE = 5000                   # 50 ₸ — штраф за 3-й день подряд
PENALTY_GROWTH = Decimal("1.25")      # каждый следующий день +25%
GRACE_DAYS = 2                        # первые два дня без штрафа
PENALTY_CAP = 10 ** 15                # техническая граница целого числа в SQLite
WARN_FROM_HOUR, WARN_TO_HOUR = 10, 21 # предупреждения — днём по Астане

TYPES = ("reward", "penalty", "bonus", "manual_adjustment", "correction")
TYPE_TITLES = {
    "reward": "Вознаграждение за урок",
    "penalty": "Штраф за пропуск",
    "bonus": "Бонус",
    "manual_adjustment": "Корректировка системы",
    "correction": "Корректировка",
}
STATUS_TITLES = {"active": "учится", "completed": "курс завершён", "annulled": "аннулировано"}
PAUSED_TITLE = "пауза: предмет сейчас недоступен"


def is_paused(p: dict) -> bool:
    return bool(p and p.get("paused_at") and not p.get("is_active") and p.get("status") != "annulled")


def status_title(p: dict) -> str:
    return PAUSED_TITLE if is_paused(p) else STATUS_TITLES.get(p.get("status"), p.get("status") or "")


# Все денежные изменения одного участника — строго по очереди. Урок, сданный
# в ту же секунду, когда фоновая проверка обнуляла баланс за истёкший Премиум,
# иначе записывался уже после обнуления и оставался на счету.
_locks_guard = threading.Lock()
_locks: dict = {}


def _lock(user_id, subject_id) -> threading.RLock:
    key = (int(user_id), root_id(subject_id))        # один замок на всю программу
    with _locks_guard:
        lk = _locks.get(key)
        if lk is None:
            lk = _locks[key] = threading.RLock()
        return lk


# ───────────────────────── время и деньги ─────────────────────────

def _utcnow() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def local_dt(value) -> Optional[datetime]:
    dt = utils.parse_utc_naive(value)
    return dt.replace(tzinfo=timezone.utc).astimezone(ALMATY) if dt else None


def local_date(value) -> Optional[date]:
    d = local_dt(value)
    return d.date() if d else None


def today_local(now: datetime = None) -> date:
    return (now or _utcnow()).replace(tzinfo=timezone.utc).astimezone(ALMATY).date()


def fmt_date(value) -> str:
    d = local_dt(value)
    return d.strftime("%d.%m.%Y") if d else "—"


def fmt_dt(value) -> str:
    d = local_dt(value)
    return d.strftime("%d.%m.%Y %H:%M") if d else "—"


MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря")


def fmt_day_words(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]}"


def to_tiyn(value) -> int:
    """«70», «62,5», «1 250» → тиыны. ValueError — если не число или меньше нуля."""
    s = str(value if value is not None else "").strip().replace(" ", "").replace(" ", "")
    s = s.replace(",", ".").replace("₸", "")
    if not s:
        return 0
    v = (Decimal(s) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if v < 0:
        raise ValueError("сумма не может быть отрицательной")
    return int(v)


def fmt(tiyn, sign: bool = False) -> str:
    """Тиыны → «3 950 ₸», «62,50 ₸», «−150 ₸», «+70 ₸»."""
    tiyn = int(tiyn or 0)
    whole, frac = divmod(abs(tiyn), 100)
    s = f"{whole:,}".replace(",", " ")
    if frac:
        s += f",{frac:02d}"
    prefix = "−" if tiyn < 0 else ("+" if sign and tiyn > 0 else "")
    return f"{prefix}{s} ₸"


def penalty_amount(day_in_row: int) -> int:
    """Штраф за N-й день пропуска подряд (в тиынах). Дни 1–2 — ноль,
    3-й — 50 ₸, дальше каждый на 25% больше предыдущего, без потолка."""
    if day_in_row <= GRACE_DAYS:
        return 0
    n = day_in_row - GRACE_DAYS - 1
    if n > 400:                          # давно за технической границей
        return PENALTY_CAP
    # Точность с запасом: при стандартных 28 знаках округление с 254-го дня
    # падало (InvalidOperation), и штрафы такого ученика вставали навсегда.
    with localcontext() as ctx:
        ctx.prec = 120
        v = Decimal(PENALTY_BASE) * (PENALTY_GROWTH ** n)
        if v >= PENALTY_CAP:
            return PENALTY_CAP
        return int(v.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# ───────────────────────── предмет и его настройки ─────────────────────────

def real_subject_id(subject_id) -> int:
    return int(sc.orig_subject_id(int(subject_id)))


def subject_row(subject_id) -> Optional[dict]:
    row = db.fetchone("SELECT * FROM subjects WHERE id=?", (real_subject_id(subject_id),))
    return dict(row) if row else None


# ── Одна программа = предмет и все его копии (витрины, потоки, бывшие ярлыки;
# services/gamification.rating_group). У программы одни настройки системы,
# одни цены уроков и один счёт ученика, где бы он ни нажал кнопку и в какой
# копии ни учился. Иначе «включил на оригинале — на витрине не работает»,
# «место из 2» и кнопка «Начать обучение» при уже имеющемся месте.

def group_ids(subject_id) -> list:
    from services import gamification as gm
    return gm.rating_group(real_subject_id(subject_id))


def root_id(subject_id) -> int:
    from services import gamification as gm
    return gm.rating_root(real_subject_id(subject_id))


def settings_row(subject_id) -> dict:
    """Настройки системы программы (включена, дата включения, оплата уроков,
    пройденных до старта) — хранятся у корня группы."""
    return subject_row(root_id(subject_id)) or {}


def is_enabled(subject_id) -> bool:
    return bool(settings_row(subject_id).get("rewards_enabled"))


def _keys(subject_id) -> dict:
    from services import gamification as gm
    return gm.group_lesson_keys(real_subject_id(subject_id))


def key_of(lesson_id) -> int:
    """«Какой это урок» для строки урока из любого предмета программы."""
    row = db.fetchone("SELECT s.subject_id FROM lessons l JOIN sections s ON s.id=l.section_id "
                      "WHERE l.id=?", (int(lesson_id),))
    if not row:
        return int(lesson_id)
    return int(_keys(row["subject_id"]).get(int(lesson_id), int(lesson_id)))


def _lessons(subject_id) -> list:
    """Открытые уроки ПРОГРАММЫ, по одному на ключ (у каждого поле key):
    сначала корень, потом остальные копии в порядке их id."""
    from services import gamification as gm
    keys = _keys(subject_id)
    seen, out = set(), []
    for sid in group_ids(subject_id):
        for l in gm.subject_lessons(sid):
            k = int(keys.get(l["id"], l["id"]))
            if k in seen:
                continue
            seen.add(k)
            l = dict(l)
            l["key"] = k
            out.append(l)
    return out


def own_lessons(subject_id) -> list:
    """Открытые уроки ИМЕННО этого предмета с ключами — для «следующего урока»."""
    from services import gamification as gm
    keys = _keys(subject_id)
    out = []
    for l in gm.subject_lessons(real_subject_id(subject_id)):
        l = dict(l)
        l["key"] = int(keys.get(l["id"], l["id"]))
        out.append(l)
    return out


def _all_lesson_keys(subject_id) -> list:
    """Ключи всех уроков предмета, включая закрытые: цену «на весь предмет»
    ставим и им — иначе урок, открытый позже, оказался бы бесплатным."""
    from webapp import learning as lg
    keys = _keys(subject_id)
    return sorted({int(keys.get(l["id"], l["id"]))
                   for l in lg._flatten_subject_lessons_sync(real_subject_id(subject_id))})


def prices(subject_id) -> dict:
    """{ключ урока: (цена в тиынах, обязателен ли)} — цены программы (у корня).

    Цена хранится на паре «корень программы + ключ урока» (reward_prices):
    задал на оригинале — действует и в витрине, и в бывшем ярлыке. Уроки
    другой программы (даже ярлыки того же урока) цену не наследуют."""
    root = root_id(subject_id)
    return {int(r["lesson_id"]): (int(r["price"] or 0), 1 if r["required"] is None else int(r["required"]))
            for r in db.fetchall("SELECT lesson_id, price, required FROM reward_prices "
                                 "WHERE subject_id=?", (root,))}


def lesson_setting(lesson_id) -> tuple:
    """(цена, обязателен) для строки урока — по ключу и корню его программы."""
    row = db.fetchone("SELECT s.subject_id FROM lessons l JOIN sections s ON s.id=l.section_id "
                      "WHERE l.id=?", (int(lesson_id),))
    if not row:
        return (0, 1)
    key = int(_keys(row["subject_id"]).get(int(lesson_id), int(lesson_id)))
    return prices(row["subject_id"]).get(key, (0, 1))


def priced_lessons_count(subject_id) -> int:
    pr = prices(subject_id)
    return sum(1 for l in _lessons(subject_id) if pr.get(l["key"], (0, 1))[0] > 0)


def _lesson_home(lesson_id) -> tuple:
    """(корень программы, ключ урока) для строки урока из админки."""
    row = db.fetchone("SELECT s.subject_id FROM lessons l JOIN sections s ON s.id=l.section_id "
                      "WHERE l.id=?", (int(lesson_id),))
    if not row:
        return None, int(lesson_id)
    return root_id(row["subject_id"]), int(_keys(row["subject_id"]).get(int(lesson_id), int(lesson_id)))


def lesson_price(lesson_id, subject_id=None) -> int:
    """Цена урока; с subject_id — цена этого урока в программе указанного
    предмета (старый ярлык чужого урока получает цену той программы, где стоит)."""
    if subject_id is None:
        return lesson_setting(lesson_id)[0]
    key = int(_keys(subject_id).get(int(lesson_id), int(lesson_id)))
    return prices(subject_id).get(key, (0, 1))[0]


def set_enabled(subject_id, on: bool, now: datetime = None) -> tuple:
    """Включить/выключить систему у программы. Включить можно только после цен уроков."""
    real = root_id(subject_id)
    now = now or _utcnow()
    if on and priced_lessons_count(real) == 0:
        return False, "Сначала задайте цену хотя бы одному уроку предмета."
    if on:
        if is_enabled(real):
            return True, "Система вознаграждений уже включена."
        # Дата включения — начало отсчёта пропусков и граница «до / после»:
        # уроки, пройденные раньше неё, не оплачиваются (если админ не разрешил
        # это отдельно) — reconcile() сверяет время прохождения урока. Флаг и
        # дата пишутся одной строкой, поэтому начисление, случившееся в ту же
        # секунду, уже видит новую границу. Меняется только при настоящем
        # включении: повторный клик не прощает идущие серии пропусков.
        db.execute("UPDATE subjects SET rewards_enabled=1, rewards_enabled_at=? WHERE id=?",
                   (_iso(now), real))
        _invalidate()
        return True, "Система вознаграждений включена."
    db.execute("UPDATE subjects SET rewards_enabled=0 WHERE id=?", (real,))
    _invalidate()
    return True, "Система вознаграждений выключена — ученики её больше не видят."


def _invalidate() -> None:
    """Настройки уроков поменялись — сбросить кэши, чтобы ученики увидели сразу."""
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


def _required_ids(subject_id, keys: list) -> list:
    """Обязательные для завершения уроки (по ключам) — прямо из базы. Список
    уроков кэшируется на 30 секунд, а галочку «обязательный» админ мог
    поменять только что: из кэша курс не завершился бы, хотя всё сдано."""
    if not keys:
        return []
    pr = prices(subject_id)
    return [k for k in keys if pr.get(k, (0, 1))[1]]


def set_pay_prior(subject_id, on: bool) -> None:
    db.execute("UPDATE subjects SET reward_pay_prior=? WHERE id=?", (1 if on else 0, root_id(subject_id)))
    _invalidate()


def _set_many(root, keys: list, field: str, value: int) -> int:
    """Записать цену или обязательность урокам программы (по ключам, у корня)."""
    assert field in ("price", "required")
    if not root or not keys:
        return 0
    stamp = _iso(_utcnow())
    db.executemany("INSERT OR IGNORE INTO reward_prices (subject_id, lesson_id, updated_at) "
                   "VALUES (?,?,?)", [(root, k, stamp) for k in keys])
    db.executemany(f"UPDATE reward_prices SET {field}=?, updated_at=? "
                   f"WHERE subject_id=? AND lesson_id=?", [(value, stamp, root, k) for k in keys])
    _invalidate()
    return len(keys)


def _section_scope(section_id) -> tuple:
    """(корень программы, ключи уроков) раздела — собственных строк раздела."""
    sec = db.fetchone("SELECT subject_id FROM sections WHERE id=?", (int(section_id),))
    if not sec:
        return None, []
    keys = _keys(sec["subject_id"])
    rows = db.fetchall("SELECT id FROM lessons WHERE section_id=?", (int(section_id),))
    return root_id(sec["subject_id"]), sorted({int(keys.get(r["id"], r["id"])) for r in rows})


def set_price_lesson(lesson_id, tiyn: int) -> None:
    root, key = _lesson_home(lesson_id)
    _set_many(root, [key], "price", max(0, int(tiyn)))


def set_price_section(section_id, tiyn: int) -> int:
    root, keys = _section_scope(section_id)
    return _set_many(root, keys, "price", max(0, int(tiyn)))


def set_price_all(subject_id, tiyn: int) -> int:
    return _set_many(root_id(subject_id), _all_lesson_keys(subject_id), "price", max(0, int(tiyn)))


def set_required_lesson(lesson_id, required: bool) -> None:
    root, key = _lesson_home(lesson_id)
    _set_many(root, [key], "required", 1 if required else 0)


def set_required_section(section_id, required: bool) -> int:
    root, keys = _section_scope(section_id)
    return _set_many(root, keys, "required", 1 if required else 0)


# ───────────────────────── прогресс ученика ─────────────────────────

def _json_list(raw) -> list:
    try:
        v = json.loads(raw or "[]")
        return [int(x) for x in v] if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def completed_lesson_ids(tg_id, user_id, subject_id) -> list:
    """КЛЮЧИ уроков программы, пройденных по тем же правилам, что и везде на
    платформе: тест сдан на порог предмета, зачёт сдан, урок без теста
    прочитан. Урок засчитан, если пройден в любой копии предмета."""
    from webapp import learning as lg
    from services import gamification as gm
    keys = _keys(subject_id)
    state = lg._completion_state_sync(user_id, tg_id)
    done = set()
    for sid in group_ids(subject_id):
        pass_pct = (subject_row(sid) or {}).get("pass_percent") or 0
        for l in gm.subject_lessons(sid):
            k = int(keys.get(l["id"], l["id"]))
            if k not in done and lg._lesson_completed_sync(user_id, tg_id, l, pass_pct, state):
                done.add(k)
    order = [l["key"] for l in _lessons(subject_id)]
    return [k for k in order if k in done] + sorted(done - set(order))


def learning_events(tg_id, user_id, subject_id) -> list:
    """Новые учебные действия по программе. Каждое засчитывается ОДИН раз за всё время:

      • open    — конспект урока открыт ВПЕРВЫЕ;
      • attempt — ДЗ/тест урока выполнен ВПЕРВЫЕ (хотя бы один ответ, даже неудачно);
      • done    — урок пройден ВПЕРВЫЕ (тест сдан на порог, зачёт сдан, конспект
                  без теста прочитан).

    НЕ новые действия: повторное открытие того же конспекта, повторное
    прохождение того же теста, повторная отправка того же ДЗ, тест, завершённый
    без ответов, проваленный зачёт, просто вход в бота или на сайт, профиль,
    рейтинг, главная страница. Они не сбрасывают счётчик пропусков и не
    приносят денег. Урок и все его копии в программе — одно действие.
    До v74 днём учёбы считался любой тест с ответами, в том числе повтор:
    один и тот же старый тест каждый день обходил штрафы."""
    from services import gamification as gm
    keys = _keys(subject_id)
    lessons = [l for sid in group_ids(subject_id) for l in gm.subject_lessons(sid)]
    if not lessons:
        return []
    key_of = {}
    for l in lessons:
        k = int(keys.get(l["id"], l["id"]))
        key_of[int(l["id"])] = k
        if l.get("legacy_lesson_id"):
            key_of.setdefault(int(l["legacy_lesson_id"]), k)   # просмотры до перевода ярлыка (v69)
    first = {}

    def put(kind, key, t, lesson_id=None):
        if not t:
            return
        cur = first.get((kind, key))
        if cur is None or (_earliest(t, cur[0]) == t and t != cur[0]):
            first[(kind, key)] = (t, lesson_id)

    try:
        if tg_id:
            for r in _fetch_in("SELECT lesson_id, MIN(COALESCE(first_opened_at, viewed_at)) AS t "
                               "FROM lesson_progress WHERE user_tg_id=? AND lesson_id IN ({ph}) "
                               "GROUP BY lesson_id", sorted(key_of), head=(tg_id,)):
                put("open", key_of[r["lesson_id"]], r["t"], r["lesson_id"])
        by_test = {}
        for l in lessons:
            if l.get("test_id") and not l.get("is_zachet"):
                by_test.setdefault(int(l["test_id"]), set()).add(key_of[int(l["id"])])
        if user_id and by_test:
            for r in _fetch_in("SELECT test_id, MIN(COALESCE(end_time, start_time)) AS t FROM test_attempts "
                               "WHERE user_id=? AND status='finished' AND COALESCE(publication_id,0)=0 "
                               "AND COALESCE(attempt_num,1)<>999 "
                               "AND (COALESCE(correct_answers,0)+COALESCE(wrong_answers,0))>0 "
                               "AND test_id IN ({ph}) GROUP BY test_id", sorted(by_test), head=(user_id,)):
                for k in by_test.get(r["test_id"], ()):
                    put("attempt", k, r["t"])
    except Exception as e:
        log.debug("новые учебные действия: %s", e)
    for k, (t, _src, _rid) in _completion_details(tg_id, user_id, subject_id, set(key_of.values())).items():
        put("done", k, t)
    out = [{"t": t, "kind": kind, "key": key, "lesson_id": lid} for (kind, key), (t, lid) in first.items()]
    out.sort(key=lambda e: utils.parse_utc_naive(e["t"]) or datetime.min)
    return out


def activity(tg_id, user_id, subject_id) -> tuple:
    """(дни по Астане, когда было НОВОЕ учебное действие, время последнего такого действия).
    Что считается новым действием — learning_events."""
    days, last = set(), None
    for e in learning_events(tg_id, user_id, subject_id):
        dt = utils.parse_utc_naive(e["t"])
        if not dt:
            continue
        days.add(dt.replace(tzinfo=timezone.utc).astimezone(ALMATY).date())
        if last is None or dt > last:
            last = dt
    return days, last


def _homework_done(user_id, subject_id) -> int:
    from services import gamification as gm
    keys = gm._test_keys(real_subject_id(subject_id))
    if not keys or not user_id:
        return 0
    ph = ",".join("?" * len(keys))
    done = db.fetchall(
        f"SELECT DISTINCT test_id FROM test_attempts WHERE user_id=? "
        f"AND status='finished' AND COALESCE(publication_id,0)=0 "
        f"AND COALESCE(attempt_num,1)<>999 AND test_id IN ({ph})",
        (user_id, *keys))
    return len({keys[r["test_id"]] for r in done})      # урок и его бывший ярлык — одна домашка


# ───────────────────────── участники ─────────────────────────

def get_participant(user_id, subject_id) -> Optional[dict]:
    """Счёт ученика в программе — один на предмет и все его копии."""
    if not user_id:
        return None
    group = group_ids(subject_id)
    row = db.fetchone(
        f"SELECT * FROM subject_participants WHERE user_id=? AND subject_id IN ({','.join('?' * len(group))}) "
        f"ORDER BY is_active DESC, (status='active') DESC, (status='completed') DESC, id DESC LIMIT 1",
        (int(user_id), *group))
    return dict(row) if row else None


def participant_by_id(pid) -> Optional[dict]:
    row = db.fetchone("SELECT * FROM subject_participants WHERE id=?", (int(pid),))
    return dict(row) if row else None


def participants(subject_id, active_only: bool = False) -> list:
    """Участники программы (предмет и все его копии)."""
    group = group_ids(subject_id)
    sql = f"SELECT * FROM subject_participants WHERE subject_id IN ({','.join('?' * len(group))})"
    if active_only:
        sql += " AND is_active=1"
    return [dict(r) for r in db.fetchall(sql + " ORDER BY id", tuple(group))]


def user_participations(tg_id) -> list:
    rows = db.fetchall(
        "SELECT p.*, s.title AS subject_title FROM subject_participants p "
        "LEFT JOIN subjects s ON s.id=p.subject_id WHERE p.tg_id=? ORDER BY p.id", (int(tg_id),))
    return [dict(r) for r in rows]


def _first_activity(tg_id, user_id, subject_id) -> Optional[str]:
    days, _ = activity(tg_id, user_id, subject_id)
    if not days:
        return None
    d = min(days)
    return _iso(datetime(d.year, d.month, d.day) - timedelta(hours=5))


def start(tg_id: int, user_id: int, subject_id: int, now: datetime = None) -> dict:
    """«✨ НАЧАТЬ ОБУЧЕНИЕ ✨». Повторное нажатие ничего не создаёт и не сбрасывает.
    Счёт один на всю программу: нажал в витрине — он же виден и в оригинале."""
    now = now or _utcnow()
    real = real_subject_id(subject_id)
    if not eligible(tg_id, user_id, real, here=True):
        return {"ok": False, "error": "no_access"}
    with _lock(user_id, real):
        return _start_locked(tg_id, user_id, real, now)


def _start_locked(tg_id: int, user_id: int, real: int, now: datetime) -> dict:
    p = get_participant(user_id, real)
    if p and p["is_active"]:
        return {"ok": True, "result": "already", "participant": p}
    if is_paused(p):
        # Доступ вернулся раньше фоновой проверки — продолжаем с тем же балансом
        return {"ok": True, "result": "resumed", "participant": resume(p, now)}
    done = completed_lesson_ids(tg_id, user_id, real)
    first = _first_activity(tg_id, user_id, real)
    stamp = _iso(now)
    # Денежная акция закрыта для новых — новому ученику только рейтинг
    off = _money_off_for(user_id, tg_id, real)
    if p is None:
        db.execute(
            "INSERT OR IGNORE INTO subject_participants (user_id, tg_id, subject_id, status, "
            "is_active, started_at, first_started_at, joined_rating_at, prior_lessons, "
            "prior_first_activity_at, lessons_completed, last_updated_at, money_off) "
            "VALUES (?,?,?,'active',1,?,?,?,?,?,?,?,?)",
            (user_id, tg_id, real, stamp, stamp, stamp, json.dumps(sorted(done)), first,
             len(done), stamp, off))
        result = "created"
    else:
        # Вернулся после аннулирования (Премиум закончился и снова куплен):
        # прошлые начисления не возвращаются, отсчёт — заново с этого момента.
        db.execute(
            "UPDATE subject_participants SET status='active', is_active=1, started_at=?, "
            "joined_rating_at=?, prior_lessons=?, annulled_at=NULL, annul_reason=NULL, "
            "completed_at=NULL, completion_rank=NULL, paused_at=NULL, days_checked_through=NULL, "
            "absence_run=0, money_off=?, money_since=NULL, last_updated_at=? WHERE id=?",
            (stamp, stamp, json.dumps(sorted(done)), off, stamp, p["id"]))
        result = "reactivated"
    p = get_participant(user_id, real)
    reconcile(p, now)
    p = refresh(p, now)
    return {"ok": True, "result": result, "participant": p}


def _insert_tx(p: dict, kind: str, amount: int, ref: str, lesson_id=None, reason: str = "",
               meta: dict = None, created_by=None, now: datetime = None) -> Optional[dict]:
    """Записать операцию. Одна инструкция INSERT ... SELECT: баланс «до» и
    «после» считается в момент записи, двойной запрос не проскочит между
    чтением и записью. Повтор (тот же ref) отбрасывается уникальным индексом
    базы — и при двойном клике, и при повторе запроса, и после перезапуска."""
    assert kind in TYPES
    stamp = _iso(now or _utcnow())
    cur = db.execute(
        "INSERT OR IGNORE INTO reward_transactions (user_id, tg_id, subject_id, lesson_id, type, "
        "amount, ref, reason, meta, created_by, created_at, balance_before, balance_after) "
        "SELECT ?,?,?,?,?,?,?,?,?,?,?, b.s, b.s + ? FROM (SELECT COALESCE(SUM(amount),0) AS s "
        "FROM reward_transactions WHERE user_id=? AND subject_id=?) AS b",
        (p["user_id"], p["tg_id"], p["subject_id"], lesson_id, kind, int(amount), ref,
         reason or "", json.dumps(meta or {}, ensure_ascii=False), created_by, stamp,
         int(amount), p["user_id"], p["subject_id"]))
    if not cur.rowcount:
        return None
    return {"id": cur.lastrowid, "type": kind, "amount": int(amount), "ref": ref, "reason": reason,
            "lesson_id": lesson_id, "created_at": stamp}


def _earliest(a, b):
    """Более раннее из двух времён. Сравниваем датами, не строками: в базе
    встречаются «2026-09-13 10:00:00» и «2026-09-13T09:00:00», а пробел в
    строке «меньше» буквы T — строковое сравнение путало порядок."""
    if not a:
        return b
    if not b:
        return a
    da, dbb = utils.parse_utc_naive(a), utils.parse_utc_naive(b)
    if da is None:
        return b
    if dbb is None:
        return a
    return a if da <= dbb else b


def _latest(a, b):
    if not a:
        return b
    if not b:
        return a
    return b if _earliest(a, b) == a else a


def _fetch_in(sql: str, ids, head=()) -> list:
    """SELECT … IN ({ph}) кусками: у SQLite предел на число параметров."""
    ids = list(ids)
    out = []
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        out += db.fetchall(sql.format(ph=",".join("?" * len(part))), tuple(head) + tuple(part))
    return out


def _completion_details(tg_id, user_id, subject_id, keys_wanted) -> dict:
    """{ключ урока: (когда впервые пройден — UTC-строка, чем: test|zachet|notes,
    id попытки теста / попытки зачёта / строки просмотра)}. Все копии урока
    в программе. Повторные прохождения сюда не попадают — берётся ПЕРВОЕ."""
    from services import gamification as gm
    want = {int(k) for k in keys_wanted}
    keys = _keys(subject_id)
    out = {}

    def put(key, t, src, rid):
        if t and (key not in out or _earliest(t, out[key][0]) == t and t != out[key][0]):
            out[key] = (t, src, rid)

    for sid in group_ids(subject_id):
        pass_pct = float((subject_row(sid) or {}).get("pass_percent") or 0)
        lessons = [l for l in gm.subject_lessons(sid) if int(keys.get(l["id"], l["id"])) in want]
        if not lessons:
            continue
        zmap = {l["id"]: int(keys.get(l["id"], l["id"])) for l in lessons if l.get("is_zachet")}
        by_test = {}
        for l in lessons:
            if not l.get("is_zachet") and l.get("test_id"):
                by_test.setdefault(l["test_id"], set()).add(int(keys.get(l["id"], l["id"])))
        nmap = {l["id"]: int(keys.get(l["id"], l["id"])) for l in lessons
                if not l.get("is_zachet") and not l.get("test_id")}
        if zmap and tg_id:
            # MIN() в SQLite отдаёт остальные колонки из той же строки — id первой сдачи
            for r in _fetch_in("SELECT lesson_id, id, MIN(finished_at) AS t FROM zachet_attempts "
                               "WHERE user_tg_id=? AND passed=1 AND lesson_id IN ({ph}) "
                               "GROUP BY lesson_id", zmap, head=(tg_id,)):
                put(zmap[r["lesson_id"]], r["t"], "zachet", r["id"])
        if by_test and user_id:
            # Тот же процент, что в _completion_state_sync (там он округлён до 0,1)
            for r in _fetch_in(
                    "SELECT test_id, id, MIN(COALESCE(end_time, start_time)) AS t FROM test_attempts "
                    "WHERE user_id=? AND status='finished' AND COALESCE(publication_id,0)=0 "
                    "AND COALESCE(attempt_num,1)<>999 "
                    "AND (CASE WHEN (correct_answers+wrong_answers)>0 "
                    "THEN correct_answers*100.0/(correct_answers+wrong_answers) ELSE 0 END) >= ? "
                    "AND test_id IN ({ph}) GROUP BY test_id", sorted(by_test),
                    head=(user_id, pass_pct - 0.05)):
                for key in by_test.get(r["test_id"], ()):
                    put(key, r["t"], "test", r["id"])
        if nmap and tg_id:
            for r in _fetch_in("SELECT lesson_id, id, COALESCE(first_opened_at, viewed_at) AS t "
                               "FROM lesson_progress WHERE user_tg_id=? AND lesson_id IN ({ph})",
                               nmap, head=(tg_id,)):
                put(nmap[r["lesson_id"]], r["t"], "notes", r["id"])
    return out


def _completion_moments(tg_id, user_id, subject_id, keys_wanted) -> dict:
    """{ключ урока: когда он впервые стал пройденным} (UTC-строка).

    По этому времени решается «до старта или после». В v67 это решал список
    уроков, пройденных на момент кнопки, — и урок, закрытый в тот момент,
    добавленный ярлыком позже или «досданный» снижением порога, оплачивался
    за учёбу задолго до старта."""
    return {k: v[0] for k, v in _completion_details(tg_id, user_id, subject_id, keys_wanted).items()}


def reconcile(p: dict, now: datetime = None) -> list:
    """Начислить за уроки, пройденные после старта и ещё не оплаченные.

    Цена — этого предмета и на момент прохождения; дальше не пересчитывается:
    урок получает ровно одну запись — даже с нулевой суммой, если цены у него
    тогда не было (поднятая потом цена прошлые уроки не оплачивает). Урок,
    пройденный раньше старта (или раньше включения системы), тоже получает
    запись — с нулём: так его уже ничто не оплатит задним числом.
    """
    if not p or not p.get("is_active") or p.get("status") == "annulled":
        return []
    subj = subject_row(p["subject_id"]) or {}
    cfg = settings_row(p["subject_id"])
    if not cfg.get("rewards_enabled"):
        return []
    if int(p.get("money_off") or 0):
        return []          # новый ученик после закрытия денежной акции: только рейтинг
    if has_certificate(p["user_id"], p["subject_id"]):
        return []          # грамота выдана — итог закрыт, новых начислений нет
    pay_prior = bool(cfg.get("reward_pay_prior"))
    prior = set() if pay_prior else set(_json_list(p.get("prior_lessons")))
    titles = {l["key"]: l.get("title") or "" for l in _lessons(p["subject_id"])}
    # Уже учтённые уроки — одним запросом. Раньше фоновая сверка раз в 5 минут
    # пыталась заново записать начисление за КАЖДЫЙ пройденный урок каждого
    # участника: тысячи лишних захватов записи в SQLite на каждом проходе.
    done_refs = {r["ref"] for r in db.fetchall(
        "SELECT ref FROM reward_transactions WHERE user_id=? AND subject_id=? AND type='reward'",
        (p["user_id"], p["subject_id"]))}
    todo = [lid for lid in completed_lesson_ids(p["tg_id"], p["user_id"], p["subject_id"])
            if lid not in prior and f"lesson:{lid}" not in done_refs]
    if not todo:
        return []
    cutoff = None
    if not pay_prior:
        marks = [dt for dt in (utils.parse_utc_naive(p.get("started_at")),
                               utils.parse_utc_naive(cfg.get("rewards_enabled_at")),
                               utils.parse_utc_naive(p.get("money_since"))) if dt]
        cutoff = max(marks) if marks else None
    moments = _completion_moments(p["tg_id"], p["user_id"], p["subject_id"], todo) if cutoff else {}
    pr = prices(p["subject_id"])
    new = []
    for lid in todo:
        when = utils.parse_utc_naive(moments.get(lid)) if cutoff else None
        before = lid in prior or bool(cutoff and when and when < cutoff)
        price = 0 if before else pr.get(lid, (0, 1))[0]
        tx = _insert_tx(p, "reward", price, f"lesson:{lid}", lesson_id=lid,
                        reason=f"Урок «{titles.get(lid, lid)}»" + (" — пройден до старта" if before else ""),
                        meta={"before_start": True} if before else None, now=now)
        if tx and price > 0:
            new.append(tx)
            queue(p["tg_id"], f"💰 Вам начислено {fmt(price, sign=True)} — урок "
                              f"«{titles.get(lid, '')}» ({subj.get('title', '')})",
                  dedup=f"reward:{p['id']}:{lid}", now=now)
    return new


def money(p: dict) -> dict:
    rows = db.fetchall("SELECT type, COALESCE(SUM(amount),0) AS s FROM reward_transactions "
                       "WHERE user_id=? AND subject_id=? GROUP BY type",
                       (p["user_id"], p["subject_id"]))
    by = {r["type"]: int(r["s"]) for r in rows}
    earned = by.get("reward", 0)
    penalties = by.get("penalty", 0)
    adjustments = by.get("bonus", 0) + by.get("manual_adjustment", 0) + by.get("correction", 0)
    balance = earned + penalties + adjustments
    return {"earned": earned, "penalties": penalties, "adjustments": adjustments,
            "balance": balance, "payout": max(0, balance)}


def refresh(p: dict, now: datetime = None, rank: bool = True) -> dict:
    """Пересчитать участника: уроки, ДЗ, баллы, деньги, дни, завершение курса."""
    from services import gamification as gm
    now = now or _utcnow()
    if not p:
        return p
    sid, uid, tg = p["subject_id"], p["user_id"], p["tg_id"]
    subj = subject_row(sid) or {}
    cfg = settings_row(sid)
    lessons = _lessons(sid)                                     # вся программа, по ключам
    done = set(completed_lesson_ids(tg, uid, sid))
    required = _required_ids(sid, [l["key"] for l in lessons])
    days, last = activity(tg, uid, sid)
    start_d = local_date(p["started_at"])
    active_days = len([d for d in days if start_d and d >= start_d])
    m = money(p)
    fields = {
        "lessons_completed": len(done), "lessons_total": len(lessons),
        "homework_completed": _homework_done(uid, sid),
        "total_points": int(gm.points(uid, sid)),
        "achievements": len(gm.earned(uid)),
        "earned": m["earned"], "penalties": m["penalties"], "adjustments": m["adjustments"],
        "balance": m["balance"], "learning_days": active_days,
        "last_activity_at": _iso(last) if last else p.get("last_activity_at"),
        "last_updated_at": _iso(now),
    }
    completed_now = (p["status"] == "active" and p["is_active"] and cfg.get("rewards_enabled")
                     and not int(p.get("money_off") or 0)
                     and required and all(r in done for r in required))
    sets = ", ".join(f"{k}=?" for k in fields)
    db.execute(f"UPDATE subject_participants SET {sets} WHERE id=?",
               tuple(fields.values()) + (p["id"],))
    if completed_now:
        # Статус меняем только у действующего участника: аннулированного
        # завершение курса не «воскрешает», даже если снимок p устарел.
        cur = db.execute("UPDATE subject_participants SET status='completed', completed_at=? "
                         "WHERE id=? AND status='active' AND is_active=1", (_iso(now), p["id"]))
        completed_now = bool(cur.rowcount)
    if rank:
        rank_subject(sid)
    p = participant_by_id(p["id"])
    if completed_now:
        db.execute("UPDATE subject_participants SET completion_rank=? WHERE id=?",
                   (p["current_rank"], p["id"]))
        p = participant_by_id(p["id"])
        queue(tg, f"🎉 Поздравляем! Курс «{subj.get('title', '')}» завершён.\n"
                  f"Итоговое вознаграждение: {fmt(max(0, p['balance']))}. "
                  f"Заберите грамоту на странице предмета.",
              dedup=f"completed:{p['id']}:{p['completed_at']}", now=now)
    try:
        sync_items(p, now)
    except Exception as e:
        log.warning("учёт заданий, участник %s: %s", p.get("id"), e)
    return p


def rank_subject(subject_id) -> None:
    """Места в рейтинге предмета: только участники, спортивная система
    (одинаковые баллы — одно место). Рейтинг общий на предмет и все его
    копии (gamification.rating_group): места пишутся всем сразу."""
    from services import gamification as gm
    real = real_subject_id(subject_id)
    group = gm.rating_group(real)
    ph = ",".join("?" * len(group))
    ps = [dict(r) for r in db.fetchall(
        f"SELECT * FROM subject_participants WHERE subject_id IN ({ph}) AND is_active=1 ORDER BY id",
        tuple(group))]
    pts = gm._points_by_user(real)
    scored = [(p, int(pts.get(p["user_id"], 0))) for p in ps]
    total = len({p["user_id"] for p, _s in scored})
    ordered = sorted((s for _p, s in scored), reverse=True)
    first_pos = {}
    for i, s in enumerate(ordered, start=1):
        first_pos.setdefault(s, i)                   # спортивное место: первое вхождение
    updates = []
    for p, mine in scored:
        place = first_pos[mine]
        # Пишем только изменившиеся строки: после сдачи одного теста обычно
        # меняются места единиц, а не всех участников предмета.
        if (p["total_points"], p["current_rank"], p["rank_total"]) != (mine, place, total):
            updates.append((mine, place, total, p["id"]))
    if updates:
        db.executemany("UPDATE subject_participants SET total_points=?, current_rank=?, "
                       "rank_total=? WHERE id=?", updates)
    db.execute(f"UPDATE subject_participants SET current_rank=0, rank_total=? "
               f"WHERE subject_id IN ({ph}) AND is_active=0 AND (current_rank<>0 OR rank_total<>?)",
               (total, *group, total))
    gm.invalidate()


def subjects_for(lesson_id=None, test_id=None) -> list:
    """Предметы-оригиналы, в которых лежит этот урок или тест."""
    lids = set()
    if lesson_id:
        lids.add(int(lesson_id))
        lids.add(int(sc.orig_lesson_id(int(lesson_id))))
    if test_id:
        lids |= {r["id"] for r in db.fetchall("SELECT id FROM lessons WHERE test_id=?", (int(test_id),))}
    if not lids:
        return []
    ph = ",".join("?" * len(lids))
    rows = db.fetchall(
        f"SELECT DISTINCT s.subject_id FROM lessons l JOIN sections s ON s.id=l.section_id "
        f"WHERE l.id IN ({ph}) OR l.original_id IN ({ph})", tuple(lids) * 2)
    return sorted({root_id(r["subject_id"]) for r in rows})     # одна программа — один счёт


def on_learning_event(tg_id, user_id=None, lesson_id=None, test_id=None,
                      now: datetime = None) -> None:
    """Урок, ДЗ или зачёт завершены — начислить и пересчитать. Не бросает исключений:
    учебный процесс не должен падать из-за денежной части."""
    try:
        from services import gamification as gm
        if not user_id:
            u = utils.get_user_by_tg(tg_id)
            user_id = u["id"] if u else None
        if not user_id:
            return
        gm.invalidate()
        for sid in subjects_for(lesson_id, test_id):
            with _lock(user_id, sid):
                p = get_participant(user_id, sid)          # свежая строка — уже под замком
                if p and p["is_active"]:
                    reconcile(p, now)
                    refresh(p, now)
    except Exception as e:
        log.exception("вознаграждение: событие tg=%s урок=%s тест=%s: %s", tg_id, lesson_id, test_id, e)


# ───────────────────────── пропуски и штрафы ─────────────────────────

def _anchor(p: dict, subj: dict) -> Optional[date]:
    """С какого дня идёт отсчёт пропусков: день старта или включения системы."""
    days = [d for d in (local_date(p.get("started_at")), local_date(subj.get("rewards_enabled_at")),
                        local_date(p.get("money_since"))) if d]
    return max(days) if days else None


def absence_streak(days: set, anchor: date, until: date) -> int:
    """Сколько дней подряд без учёбы, считая назад от until включительно."""
    n, d = 0, until
    while d > anchor and d not in days:
        n += 1
        d -= timedelta(days=1)
    return n


def _parse_day(value) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _available_since(subject_id, keys_wanted) -> dict:
    """{ключ урока: с какого момента он доступен в программе (UTC)} — самая
    ранняя из его ОТКРЫТЫХ копий: создан открытым — с создания, закрыт и
    открыт позже — с открытия (lessons.opened_at ставит триггер базы)."""
    keys = _keys(subject_id)
    want = {int(k) for k in keys_wanted}
    ids = [lid for lid, k in keys.items() if int(k) in want]
    out = {}
    for r in _fetch_in("SELECT id, created_at, opened_at, status FROM lessons WHERE id IN ({ph})", ids):
        if (r["status"] or "open") != "open":
            continue
        t = utils.parse_utc_naive(r["opened_at"] or r["created_at"])
        k = int(keys.get(r["id"], r["id"]))
        if t and (k not in out or t < out[k]):
            out[k] = t
    return out


def _pending_checker(p: dict):
    """fn(день) → было ли у ученика в этот день доступное, но ещё не
    выполненное обязательное задание.

    Штрафуем только тогда. Всё выполнено, а новых уроков админ ещё не открыл,
    урок закрыт, платный урок ученику не открыт — делать физически нечего,
    день не пропуск. Урок, открытый в течение дня, считается с СЛЕДУЮЩЕГО дня:
    у ученика должен быть целый день на него."""
    from webapp import learning as lg
    sid, tg, uid = p["subject_id"], p["tg_id"], p["user_id"]
    own = own_lessons(sid)
    required = set(_required_ids(sid, [l["key"] for l in own]))
    cand = {}
    for l in own:
        k = l["key"]
        if k in required and k not in cand:
            try:
                ok = lg._has_lesson_paid_access_sync(l, tg)
            except Exception:
                ok = True
            if ok:
                cand[k] = l
    if not cand:
        return lambda d: False
    avail = _available_since(sid, cand)
    comp = _completion_moments(tg, uid, sid, cand)
    spans = []
    for k in cand:
        a = avail.get(k)
        a_day = a.replace(tzinfo=timezone.utc).astimezone(ALMATY).date() if a else None
        spans.append((a_day, local_date(comp.get(k))))

    def pending(d: date) -> bool:
        return any((a is None or a < d) and (c is None or c >= d) for a, c in spans)
    return pending


def _streak_back(days: set, pending, anchor: date, until: date) -> int:
    """Серия дней без новых действий (при наличии задания), назад от until включительно."""
    n, d = 0, until
    while d > anchor and d not in days and pending(d):
        n += 1
        d -= timedelta(days=1)
    return n


def penalize(p: dict, now: datetime = None) -> list:
    """Штрафы за завершившиеся дни без НОВОЙ учебной активности (догоняет и после простоя).

    День — пропуск, только если за него не было ни одного нового учебного
    действия (learning_events: впервые открытый конспект, впервые выполненное
    ДЗ/тест, впервые пройденный урок) И у ученика было доступное невыполненное
    обязательное задание (_pending_checker). Делать было нечего — день не
    считается, серия обрывается. Первые GRACE_DAYS дней серии — без штрафа,
    с 3-го — 50 ₸ и +25% каждый следующий день. Каждый день пишется в журнал
    одной строкой (ref absence:<дата>), в том числе нулевой — чтобы админ
    видел, почему баланс изменился или не изменился.

    Каждый прошедший день оценивается ОДИН раз и больше не пересматривается
    (days_checked_through + длина текущей серии absence_run). В v67 вся
    история с начала пересчитывалась каждые 5 минут по текущему каталогу:
    перезалитый тест или закрытый урок превращали дни настоящей учёбы в
    пропуски, и ученику задним числом приходила пачка штрафов.
    """
    now = now or _utcnow()
    if not p or p.get("status") != "active" or not p.get("is_active"):
        return []
    subj = subject_row(p["subject_id"]) or {}
    cfg = settings_row(p["subject_id"])
    if not cfg.get("rewards_enabled") or int(p.get("money_off") or 0):
        return []
    anchor = _anchor(p, cfg)
    end = today_local(now) - timedelta(days=1)          # только прошедшие дни
    if not anchor:
        return []
    checked = _parse_day(p.get("days_checked_through"))
    streak = int(p.get("absence_run") or 0)
    if checked is None or checked < anchor:
        checked, streak = anchor, 0     # с начала — или с нового старта / включения
    if end <= checked:
        return []
    days, _ = activity(p["tg_id"], p["user_id"], p["subject_id"])
    pending = _pending_checker(p)
    new, d = [], checked + timedelta(days=1)
    while d <= end:
        ref = f"absence:{d.isoformat()}"
        if d in days:
            streak = 0
        elif not pending(d):
            streak = 0
            _insert_tx(p, "penalty", 0, ref,
                       reason="Нет доступных новых заданий — день не считается",
                       meta={"date": d.isoformat(), "no_tasks": True}, now=now)
        else:
            streak += 1
            amount = penalty_amount(streak)
            if amount:
                tx = _insert_tx(p, "penalty", -amount, ref, reason=f"{streak}-й день отсутствия",
                                meta={"day": streak, "date": d.isoformat()}, now=now)
                if tx:
                    new.append(tx)
                    queue(p["tg_id"], f"⚠️ Начислен штраф {fmt(-amount)} — {streak}-й день без новых "
                                      f"учебных действий по предмету «{subj.get('title', '')}» "
                                      f"({fmt_day_words(d)}).",
                          dedup=f"penalty:{p['id']}:{d.isoformat()}", now=now)
            else:
                _insert_tx(p, "penalty", 0, ref, reason=f"{streak}-й день отсутствия — без штрафа",
                           meta={"day": streak, "date": d.isoformat(), "grace": True}, now=now)
        d += timedelta(days=1)
    db.execute("UPDATE subject_participants SET days_checked_through=?, absence_run=? WHERE id=?",
               (end.isoformat(), streak, p["id"]))
    if new:
        p = refresh(p, now, rank=False)
        if p["balance"] < 0:
            queue(p["tg_id"], f"🔴 Баланс по предмету «{subj.get('title', '')}»: {fmt(p['balance'])}.\n"
                              f"Это не долг — просто продолжайте учиться, новые начисления перекроют минус.",
                  dedup=f"negative:{p['id']}:{today_local(now).isoformat()}", now=now)
    return new


def warn(p: dict, now: datetime = None) -> bool:
    """«Вы не выполняли новых заданий уже 2 дня» — днём третьего дня, один раз.
    Только если сегодня есть что делать и ничего нового ещё не сделано."""
    now = now or _utcnow()
    if not p or p.get("status") != "active" or not p.get("is_active"):
        return False
    subj = subject_row(p["subject_id"]) or {}
    cfg = settings_row(p["subject_id"])
    if not cfg.get("rewards_enabled") or int(p.get("money_off") or 0):
        return False
    hour = now.replace(tzinfo=timezone.utc).astimezone(ALMATY).hour
    if not (WARN_FROM_HOUR <= hour < WARN_TO_HOUR):     # с 10:00 до 21:00
        return False
    today = today_local(now)
    yesterday = today - timedelta(days=1)
    anchor = _anchor(p, cfg)
    if not anchor:
        return False
    days, _ = activity(p["tg_id"], p["user_id"], p["subject_id"])
    if today in days:
        return False
    pending = _pending_checker(p)
    if not pending(today):
        return False
    if _parse_day(p.get("days_checked_through")) == yesterday and anchor <= yesterday:
        # Серия уже посчитана проходом штрафов — историю заново не перебираем
        if int(p.get("absence_run") or 0) != GRACE_DAYS:
            return False
    elif _streak_back(days, pending, anchor, yesterday) != GRACE_DAYS:
        return False
    return queue(p["tg_id"], f"⚠️ Вы не выполняли новых заданий уже {GRACE_DAYS} дня по предмету "
                             f"«{subj.get('title', '')}». Вернитесь сегодня, чтобы избежать штрафа "
                             f"{fmt(PENALTY_BASE)}: откройте новый конспект, сдайте новое ДЗ или пройдите "
                             f"новый урок. Повтор уже пройденного не засчитывается.",
                 dedup=f"warn:{p['id']}:{today.isoformat()}", now=now)


# ───────────────────────── потеря доступа ─────────────────────────

def _family(subject_id, whole: bool = True) -> list:
    """whole=True — вся программа (предмет и его копии) плюс старые
    ярлыки-витрины: право на любой из них — право на программу (счёт,
    рейтинг, аннулирование). whole=False — только этот предмет и его
    ярлыки: так решается, показывать ли блоки на ЭТОЙ странице."""
    ids = group_ids(subject_id) if whole else [real_subject_id(subject_id)]
    ph = ",".join("?" * len(ids))
    return [dict(r) for r in db.fetchall(
        f"SELECT * FROM subjects WHERE id IN ({ph}) OR (original_id IN ({ph}) AND status='active')",
        tuple(ids) * 2)]


def _live_subject_access(tg_id, subject_ids) -> bool:
    if not tg_id or not subject_ids:
        return False
    ph = ",".join("?" * len(subject_ids))
    for r in db.fetchall(f"SELECT expires_at FROM subject_access WHERE user_tg_id=? "
                         f"AND subject_id IN ({ph})", (int(tg_id), *subject_ids)):
        if not r["expires_at"] or utils.deadline_active(r["expires_at"]):
            return True
    return False


def eligible(tg_id, user_id, subject_id, here: bool = False) -> bool:
    """Может ли человек участвовать в рейтинге и вознаграждениях предмета.
    here=True — есть ли у него право именно на ЭТОТ предмет (страница,
    кнопка, уроки); иначе — на программу в целом (счёт, аннулирование).

    Только по своему праву на предмет: выданный доступ (на оригинал или его
    копию-витрину) или Премиум, который этот предмет принимает (сам предмет
    или его витрина в режиме «открыт» / «премиум» и не «продаётся отдельно»).
    Режим «открыт всем» сам по себе права не даёт — иначе деньги получал бы
    любой, а отзыв Премиума никого бы не аннулировал.
    """
    if not tg_id or not user_id:
        return False
    fam = _family(subject_id, whole=not here)
    if _live_subject_access(tg_id, [s["id"] for s in fam]):
        return True
    if not utils.is_premium(user_id):
        return False
    return any(sc.subject_mode(s) in (sc.OPEN, sc.PREMIUM) and not sc.premium_ignored(s)
               for s in fam)


def _own_entitlement(tg_id, user_id, subject_id) -> bool:
    """Есть ли у самого ученика действующее право (Премиум или выданный доступ).
    Если есть, а участвовать нельзя, — значит, поменялись настройки предмета."""
    if user_id and utils.is_premium(user_id):
        return True
    return _live_subject_access(tg_id, [s["id"] for s in _family(subject_id)])


def _lost_reason(tg_id, user_id, subject_id) -> str:
    row = db.fetchone("SELECT expires_at FROM premium_users WHERE user_id=?", (user_id,))
    if row is not None and not utils.deadline_active(row["expires_at"]):
        return "срок Премиума закончился"
    ids = [s["id"] for s in _family(subject_id)]
    ph = ",".join("?" * len(ids))
    if ids and db.fetchone(f"SELECT 1 FROM subject_access WHERE user_tg_id=? "
                           f"AND subject_id IN ({ph}) LIMIT 1", (int(tg_id), *ids)):
        return "срок доступа к предмету закончился"
    if row is None:
        return "доступ отозван администратором"
    return "доступ к предмету закончился"


def has_certificate(user_id, subject_id) -> bool:
    group = group_ids(subject_id)
    return db.fetchone(f"SELECT id FROM reward_certificates WHERE user_id=? AND revoked=0 "
                       f"AND subject_id IN ({','.join('?' * len(group))})", (user_id, *group)) is not None


def annul(p: dict, reason: str, now: datetime = None) -> dict:
    """Премиум закончился или отозван: вознаграждение аннулируется (баланс в ноль
    одной операцией), ученик выходит из рейтинга. История операций остаётся."""
    now = now or _utcnow()
    m = money(p)
    if m["balance"]:
        # Одно обнуление на каждый старт: повторная проверка его не дублирует
        _insert_tx(p, "manual_adjustment", -m["balance"], f"annul:{p.get('started_at') or _iso(now)}",
                   reason=f"Аннулировано: {reason}", now=now)
    db.execute("UPDATE subject_participants SET status='annulled', is_active=0, paused_at=NULL, "
               "annulled_at=?, annul_reason=?, current_rank=0, last_updated_at=? WHERE id=?",
               (_iso(now), reason, _iso(now), p["id"]))
    p = refresh(participant_by_id(p["id"]), now, rank=False)
    rank_subject(p["subject_id"])
    subj = subject_row(p["subject_id"]) or {}
    head, title = reason[:1].upper() + reason[1:], subj.get("title", "")
    # Пока система выключена, о деньгах ученик ничего не знает — и здесь о них ни слова
    what = (f"вознаграждение по предмету «{title}» аннулировано, вы исключены из рейтинга предмета"
            if money_on(p) else f"вы исключены из рейтинга предмета «{title}»")
    queue(p["tg_id"], f"⛔️ {head}: {what}.\nПройденные уроки и баллы сохранены. Чтобы вернуться, "
                      f"откройте доступ и снова нажмите «Начать обучение».",
          dedup=f"annul:{p['id']}:{p['annulled_at']}", now=now)
    return p


def pause(p: dict, now: datetime = None) -> dict:
    """Доступ пропал из-за настроек предмета (например, «продаётся отдельно»),
    а не у самого ученика: деньги не обнуляем, штрафов нет, из рейтинга —
    временно. Вернётся доступ — участие продолжится с тем же балансом."""
    now = now or _utcnow()
    db.execute("UPDATE subject_participants SET is_active=0, paused_at=?, current_rank=0, "
               "last_updated_at=? WHERE id=? AND is_active=1", (_iso(now), _iso(now), p["id"]))
    rank_subject(p["subject_id"])
    subj = subject_row(p["subject_id"]) or {}
    if money_on(p):
        queue(p["tg_id"], f"⏸ Предмет «{subj.get('title', '')}» сейчас вам недоступен: изменились условия "
                          f"доступа. Вознаграждение сохранено, штрафы не начисляются. Когда доступ "
                          f"откроется, вы вернётесь в рейтинг автоматически.",
              dedup=f"pause:{p['id']}:{_iso(now)}", now=now)
    return participant_by_id(p["id"])


def resume(p: dict, now: datetime = None) -> dict:
    """Доступ вернулся — снова в рейтинге. Дни паузы пропусками не считаются."""
    now = now or _utcnow()
    yesterday = today_local(now) - timedelta(days=1)
    db.execute("UPDATE subject_participants SET is_active=1, paused_at=NULL, days_checked_through=?, "
               "absence_run=0, last_updated_at=? WHERE id=?", (yesterday.isoformat(), _iso(now), p["id"]))
    p = participant_by_id(p["id"])
    reconcile(p, now)
    p = refresh(p, now)
    subj = subject_row(p["subject_id"]) or {}
    if money_on(p):
        queue(p["tg_id"], f"▶️ Доступ к предмету «{subj.get('title', '')}» снова открыт — вы вернулись "
                          f"в рейтинг, вознаграждение сохранено.",
              dedup=f"resume:{p['id']}:{_iso(now)}", now=now)
    return p


def check_access(p: dict, now: datetime = None) -> bool:
    """True — если участник аннулирован сейчас. Вызывать под _lock участника.

    Аннулирование — только когда закончилось или отозвано право самого
    ученика (Премиум, выданный доступ). Если право есть, а предмет перестал
    его принимать (админ поменял настройки), — пауза без потери денег.
    """
    if not p:
        return False
    paused = is_paused(p)
    if not p.get("is_active") and not paused:
        return False
    if has_certificate(p["user_id"], p["subject_id"]):
        return False                     # курс закрыт грамотой — итог окончательный
    if eligible(p["tg_id"], p["user_id"], p["subject_id"]):
        if paused:
            resume(p, now)
        return False
    if _own_entitlement(p["tg_id"], p["user_id"], p["subject_id"]):
        if not paused:
            pause(p, now)
        return False
    annul(p, _lost_reason(p["tg_id"], p["user_id"], p["subject_id"]), now)
    return True


def check_user(tg_id, now: datetime = None) -> int:
    """Сразу после отзыва Премиума админом — не ждать фоновой проверки."""
    n = 0
    for r in user_participations(tg_id):
        try:
            with locked_participant(r["id"]) as p:
                n += 1 if (p and check_access(p, now)) else 0
        except Exception as e:
            log.warning("проверка доступа участника %s: %s", r.get("id"), e)
    return n


# ───────────────────────── грамота ─────────────────────────

_TRANSLIT = {"А": "A", "Ә": "A", "Б": "B", "В": "V", "Г": "G", "Ғ": "G", "Д": "D", "Е": "E",
             "Ё": "E", "Ж": "Z", "З": "Z", "И": "I", "Й": "I", "К": "K", "Қ": "K", "Л": "L",
             "М": "M", "Н": "N", "Ң": "N", "О": "O", "Ө": "O", "П": "P", "Р": "R", "С": "S",
             "Т": "T", "У": "U", "Ұ": "U", "Ү": "U", "Ф": "F", "Х": "H", "Һ": "H", "Ц": "C",
             "Ч": "C", "Ш": "S", "Щ": "S", "Ы": "Y", "І": "I", "Э": "E", "Ю": "U", "Я": "Y"}


def serial_prefix(title: str) -> str:
    words = [w for w in "".join(ch if ch.isalpha() else " " for ch in (title or "")).split() if w]
    letters = ""
    for w in words[:2]:
        ch = w[0].upper()
        letters += _TRANSLIT.get(ch, ch if "A" <= ch <= "Z" else "")
    return (letters or "SE")[:3]


def make_serial(title: str, year: int) -> str:
    prefix = serial_prefix(title)
    for _ in range(50):
        serial = f"{prefix}-{year}-{random.randint(0, 99_999_999):08d}"
        if not db.fetchone("SELECT id FROM reward_certificates WHERE serial=?", (serial,)):
            return serial
    raise RuntimeError("не удалось подобрать уникальный номер грамоты")


def get_certificate(user_id, subject_id) -> Optional[dict]:
    group = group_ids(subject_id)
    row = db.fetchone(f"SELECT * FROM reward_certificates WHERE user_id=? "
                      f"AND subject_id IN ({','.join('?' * len(group))}) ORDER BY id LIMIT 1",
                      (user_id, *group))
    return dict(row) if row else None


def completion_summary(p: dict) -> dict:
    s, c = local_date(p.get("started_at")), local_date(p.get("completed_at"))
    m = money(p)
    return {
        "started": fmt_date(p.get("started_at")), "completed": fmt_date(p.get("completed_at")),
        "days": ((c - s).days + 1) if (s and c) else 0,
        "lessons": p.get("lessons_completed") or 0, "lessons_total": p.get("lessons_total") or 0,
        "earned": m["earned"], "penalties": m["penalties"], "adjustments": m["adjustments"],
        "balance": m["balance"], "payout": m["payout"],
        "rank": p.get("completion_rank") or p.get("current_rank") or 0,
        "rank_total": p.get("rank_total") or 0,
    }


def clean_name(raw: str) -> str:
    name = " ".join((raw or "").replace("\n", " ").split())
    if not (3 <= len(name) <= 80):
        raise ValueError("Укажите имя и фамилию полностью (от 3 до 80 символов).")
    if not all(ch.isalpha() or ch in " -'.ʼ" for ch in name) or not any(ch.isalpha() for ch in name):
        raise ValueError("В имени можно использовать только буквы, пробел и дефис.")
    return name


def claim(tg_id: int, user_id: int, subject_id: int, full_name: str, now: datetime = None) -> dict:
    """«🏆 Забрать грамоту и вознаграждение». Повторно — та же грамота."""
    with _lock(user_id, subject_id):
        return _claim_locked(tg_id, user_id, subject_id, full_name, now)


def _claim_locked(tg_id: int, user_id: int, subject_id: int, full_name: str,
                  now: datetime = None) -> dict:
    now = now or _utcnow()
    real = real_subject_id(subject_id)
    existing = get_certificate(user_id, real)
    if existing:
        return {"ok": True, "certificate": existing, "result": "already"}
    p = get_participant(user_id, real)
    if not p or p["status"] != "completed" or not p["is_active"]:
        return {"ok": False, "error": "not_completed"}
    if not is_enabled(real):
        return {"ok": False, "error": "disabled"}
    if not eligible(tg_id, user_id, real, here=True):
        return {"ok": False, "error": "no_access"}
    _pm = get_participant(user_id, real)
    if _pm and int(_pm.get("money_off") or 0):
        return {"ok": False, "error": "disabled"}
    try:
        name = clean_name(full_name)
    except ValueError as e:
        return {"ok": False, "error": "bad_name", "message": str(e)}
    p = refresh(p, now)
    summ = completion_summary(p)
    subj = subject_row(p["subject_id"]) or {}
    serial = make_serial(subj.get("title", ""), local_date(p["completed_at"]).year)
    db.execute(
        "INSERT OR IGNORE INTO reward_certificates (serial, user_id, tg_id, subject_id, full_name, "
        "subject_title, started_at, completed_at, days, lessons, earned, penalties, adjustments, "
        "payout, rank, rank_total, issued_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (serial, user_id, tg_id, p["subject_id"], name, subj.get("title", ""), p["started_at"],
         p["completed_at"], summ["days"], summ["lessons"], summ["earned"], summ["penalties"],
         summ["adjustments"], summ["payout"], summ["rank"], summ["rank_total"], _iso(now)))
    cert = get_certificate(user_id, real)
    queue(tg_id, f"🏆 Грамота по предмету «{subj.get('title', '')}» готова.\n"
                 f"Серийный номер: {cert['serial']}. Итоговое вознаграждение: {fmt(cert['payout'])}.",
          dedup=f"certificate:{cert['serial']}", now=now)
    return {"ok": True, "certificate": cert, "result": "created"}


def verify(query: str) -> list:
    """Грамоты по серийному номеру или Telegram ID."""
    q = (query or "").strip()
    if not q:
        return []
    if q.isdigit():
        rows = db.fetchall("SELECT * FROM reward_certificates WHERE tg_id=? ORDER BY id", (int(q),))
    else:
        rows = db.fetchall("SELECT * FROM reward_certificates WHERE UPPER(serial)=UPPER(?)", (q,))
    return [dict(r) for r in rows]


def _secret() -> bytes:
    from services import workbook_mark_service as wm
    return hashlib.sha256(b"certificate::" + wm._secret()).digest()


def cert_token(serial: str, tg_id: int, ttl: int = 6 * 3600) -> str:
    exp = int(time.time()) + ttl
    sig = hmac.new(_secret(), f"{serial}:{int(tg_id)}:{exp}".encode(), hashlib.sha256).hexdigest()[:24]
    return f"{int(tg_id)}.{exp}.{sig}"


def parse_cert_token(serial: str, token: str) -> Optional[int]:
    try:
        tg_s, exp_s, sig = (token or "").split(".")
        tg, exp = int(tg_s), int(exp_s)
    except (ValueError, AttributeError):
        return None
    if exp < time.time():
        return None
    good = hmac.new(_secret(), f"{serial}:{tg}:{exp}".encode(), hashlib.sha256).hexdigest()[:24]
    return tg if hmac.compare_digest(good, sig) else None


# ───────────────────────── админ: ручные операции ─────────────────────────

def admin_adjust(p: dict, kind: str, tiyn: int, reason: str, admin_tg: int,
                 now: datetime = None, op_key: str = None) -> dict:
    """+ бонус / − корректировка. Причина обязательна, всё пишется в историю."""
    reason = " ".join((reason or "").split())
    if kind not in ("bonus", "correction"):
        raise ValueError("неизвестный вид операции")
    if len(reason) < 3:
        raise ValueError("Укажите причину — хотя бы несколько слов.")
    tiyn = abs(int(tiyn))
    if tiyn <= 0:
        raise ValueError("Сумма должна быть больше нуля.")
    amount = tiyn if kind == "bonus" else -tiyn
    # op_key — один на весь диалог «сумма → причина»: два быстрых сообщения
    # подряд не запишут операцию дважды, второе просто не пройдёт уникальность.
    ref = f"manual:{op_key or uuid.uuid4().hex}"
    with locked_participant(p["id"]) as cur:
        if cur is None:
            raise ValueError("Участник не найден.")
        p = cur                                  # свежая строка — счёт мог переехать
        tx = _insert_tx(p, kind, amount, ref, reason=reason, created_by=admin_tg, now=now)
        if tx is None:
            raise ValueError("Эта операция уже записана.")
        p = refresh(participant_by_id(p["id"]), now, rank=False)
        _sync_certificate(p)
    subj = subject_row(p["subject_id"]) or {}
    label = "🎁 Бонус" if kind == "bonus" else "✏️ Корректировка"
    queue(p["tg_id"], f"{label} {fmt(amount, sign=True)} по предмету «{subj.get('title', '')}».\n"
                      f"Причина: {reason}", dedup=f"manual:{tx['ref']}", now=now)
    return tx


def _sync_certificate(p: dict) -> None:
    """После выдачи грамоты итог меняет только админ (бонус/корректировка) —
    и тогда грамота, проверка по QR и страница ученика показывают одну сумму."""
    cert = get_certificate(p["user_id"], p["subject_id"])
    if not cert:
        return
    m = money(p)
    db.execute("UPDATE reward_certificates SET earned=?, penalties=?, adjustments=?, payout=? "
               "WHERE id=?", (m["earned"], m["penalties"], m["adjustments"], m["payout"], cert["id"]))


@contextmanager
def locked_participant(pid):
    """Строка участника под её замком. Замок — по предмету, где счёт лежит
    СЕЙЧАС: после переезда в другой предмет программы (v69) замок старого
    предмета его уже не защищает — перечитываем и при смене берём заново."""
    for _ in range(3):
        p = participant_by_id(pid)
        if not p:
            yield None
            return
        with _lock(p["user_id"], p["subject_id"]):
            fresh = participant_by_id(pid)
            if fresh and fresh["subject_id"] == p["subject_id"]:
                yield fresh
                return
    yield None


def refresh_participant(pid, rank: bool = False) -> Optional[dict]:
    """Пересчёт одного участника снаружи (карточка в боте) — под его замком."""
    with locked_participant(pid) as p:
        return refresh(p, rank=rank) if p else None


def transactions(p: dict, kinds: tuple = None, limit: int = 500) -> list:
    sql = "SELECT * FROM reward_transactions WHERE user_id=? AND subject_id=?"
    args = [p["user_id"], p["subject_id"]]
    if kinds:
        sql += f" AND type IN ({','.join('?' * len(kinds))})"
        args += list(kinds)
    rows = db.fetchall(sql + " AND amount<>0 ORDER BY created_at DESC, id DESC LIMIT ?",
                       tuple(args) + (limit,))
    out = []
    for r in rows:
        r = dict(r)
        try:
            r["meta"] = json.loads(r.get("meta") or "{}")
        except ValueError:
            r["meta"] = {}
        r["amount_fmt"] = fmt(r["amount"], sign=True)
        r["type_title"] = TYPE_TITLES.get(r["type"], r["type"])
        day = r["meta"].get("date") if r["type"] == "penalty" else None
        r["when"] = (fmt_day_words(date.fromisoformat(day)) if day else fmt_dt(r["created_at"]))
        out.append(r)
    return out


# ───────────────────────── что видит ученик ─────────────────────────

def subject_view(tg_id, subject_id) -> Optional[dict]:
    """Блоки страницы предмета. None — предмет не открыт (Премиума нет):
    тогда ни денег, ни рейтинга, ни кнопки старта ученик не видит.
    Счёт один на программу: нажал в витрине — здесь тоже «начато»."""
    if not tg_id:
        return None
    u = utils.get_user_by_tg(tg_id)
    if not u:
        return None
    real = real_subject_id(subject_id)
    p = get_participant(u["id"], real)
    cert = get_certificate(u["id"], real) if p else None
    # Грамота уже выдана — её страница доступна и после окончания Премиума
    if not eligible(tg_id, u["id"], real, here=True) and not (cert and p["is_active"]):
        return None
    active = bool(p and p["is_active"])
    system_on = is_enabled(real)
    # Деньги — только тем, кому идёт денежная акция. Закрыта для новых —
    # новому ученику ни сумм, ни штрафов, ни грамоты; рейтинг — как у всех
    has_money = system_on and ((not int(p.get("money_off") or 0)) if active
                               else money_allowed(tg_id, u["id"], real))
    view = {"eligible": True, "started": active, "enabled": bool(has_money), "system_on": system_on,
            "money_closed": bool(system_on and not has_money),
            "subject_id": real, "status": p["status"] if active else None,
            "participant": p if active else None, "elsewhere": None}
    if active:
        start_d = local_date(p["started_at"])
        view["started_label"] = fmt_date(p["started_at"])
        view["day_n"] = ((today_local() - start_d).days + 1) if start_d else 1
        view["completed"] = p["status"] == "completed"
        view["certificate"] = cert
        done = set(completed_lesson_ids(tg_id, u["id"], real))
        nxt = next((l for l in own_lessons(real) if l["key"] not in done), None)
        view["next_lesson"] = ({"title": nxt.get("title") or "",
                                "url": f"/learn/lesson/{sc.lesson_url_id(nxt)}"} if nxt else None)
        if view["enabled"]:
            m = money(p)
            view.update({k: m[k] for k in m})
            view.update({f"{k}_fmt": fmt(m[k]) for k in m})
            view["penalties_count"] = db.fetchone(
                "SELECT COUNT(*) AS c FROM reward_transactions WHERE user_id=? AND subject_id=? "
                "AND type='penalty' AND amount<>0", (u["id"], p["subject_id"]))["c"]
    return view


def unify_ledgers() -> dict:
    """Одна программа — один счёт: свести то, что до v73 лежало по копиям.

    • цены и «обязательный» — к корню группы, по ключам уроков;
    • ссылки начислений lesson:<урок> — на ключи (урок и его копия — одно);
    • два счёта одного ученика в одной программе — в один (операции и
      грамота переезжают, лишняя строка удаляется);
    • система, включённая на любой копии, — включена у корня.
    Повторный запуск ничего не меняет."""
    from services import gamification as gm
    gm.invalidate()
    stats = {"prices": 0, "refs": 0, "merged": 0, "enabled": 0}
    try:
        roots = {}
        for r in db.fetchall("SELECT id FROM subjects"):
            roots.setdefault(gm.rating_root(r["id"]), set()).add(r["id"])
    except Exception as e:
        log.warning("объединение счетов программ: %s", e)
        return stats
    for root, members in roots.items():
        if len(members) < 2:
            continue
        keys = gm.group_lesson_keys(root)
        ph = ",".join("?" * len(members))
        for r in db.fetchall(f"SELECT * FROM reward_prices WHERE subject_id IN ({ph})", tuple(members)):
            key = int(keys.get(r["lesson_id"], r["lesson_id"]))
            if r["subject_id"] == root and key == r["lesson_id"]:
                continue
            cur = db.execute("INSERT OR IGNORE INTO reward_prices (subject_id, lesson_id, price, required, "
                             "updated_at) VALUES (?,?,?,?,?)",
                             (root, key, r["price"], r["required"], r["updated_at"]))
            stats["prices"] += cur.rowcount or 0
        for r in db.fetchall(f"SELECT id, lesson_id, ref FROM reward_transactions WHERE type='reward' "
                             f"AND subject_id IN ({ph}) AND ref LIKE 'lesson:%'", tuple(members)):
            try:
                lid = int(r["ref"].split(":", 1)[1])
            except (ValueError, IndexError):
                continue
            key = int(keys.get(lid, lid))
            if key != lid:
                cur = db.execute("UPDATE OR IGNORE reward_transactions SET ref=?, lesson_id=? WHERE id=?",
                                 (f"lesson:{key}", key, r["id"]))
                stats["refs"] += cur.rowcount or 0
        for r in db.fetchall(f"SELECT id, prior_lessons FROM subject_participants WHERE subject_id IN ({ph})",
                             tuple(members)):
            prior = _json_list(r["prior_lessons"])
            fixed = sorted({int(keys.get(x, x)) for x in prior})
            if fixed != sorted(prior):
                db.execute("UPDATE subject_participants SET prior_lessons=? WHERE id=?",
                           (json.dumps(fixed), r["id"]))
        for u in db.fetchall(f"SELECT user_id FROM subject_participants WHERE subject_id IN ({ph}) "
                             f"GROUP BY user_id HAVING COUNT(*) > 1", tuple(members)):
            rows = [dict(x) for x in db.fetchall(
                f"SELECT * FROM subject_participants WHERE user_id=? AND subject_id IN ({ph}) "
                f"ORDER BY is_active DESC, (status='active') DESC, (status='completed') DESC, id DESC",
                (u["user_id"], *members))]
            keep, extra = rows[0], rows[1:]
            for e in extra:
                db.execute("UPDATE OR IGNORE reward_transactions SET subject_id=? WHERE user_id=? AND subject_id=?",
                           (keep["subject_id"], e["user_id"], e["subject_id"]))
                db.execute("UPDATE OR IGNORE reward_certificates SET subject_id=? WHERE user_id=? AND subject_id=?",
                           (keep["subject_id"], e["user_id"], e["subject_id"]))
                db.execute("DELETE FROM subject_participants WHERE id=?", (e["id"],))
                stats["merged"] += 1
        on = db.fetchone(f"SELECT MIN(rewards_enabled_at) AS at, MAX(reward_pay_prior) AS pp FROM subjects "
                         f"WHERE id IN ({ph}) AND COALESCE(rewards_enabled,0)=1", tuple(members))
        rt = subject_row(root) or {}
        if on and on["at"] is not None and not rt.get("rewards_enabled"):
            db.execute("UPDATE subjects SET rewards_enabled=1, rewards_enabled_at=?, reward_pay_prior=? WHERE id=?",
                       (on["at"], on["pp"] or 0, root))
            stats["enabled"] += 1
    gm.invalidate()
    if any(stats.values()):
        log.info("счета программ объединены: %s", stats)
    return stats


# ───────────────────────── денежная акция для новых учеников ─────────────────────────
# Админ может закрыть денежную часть для НОВЫХ учеников программы. Старые —
# кто до закрытия уже нажимал «Начать обучение», получал Премиум или доступ к
# предмету — продолжают получать деньги и штрафы как раньше. Новые получают
# рейтинг, геймификацию, мотивации и «Начать обучение», но без денег, без
# штрафов и без грамоты (subject_participants.money_off = 1).

def new_closed_at(subject_id) -> Optional[str]:
    return settings_row(subject_id).get("reward_new_closed_at") or None


def _before(value, cutoff: datetime) -> bool:
    t = utils.parse_utc_naive(value)
    return bool(t and t < cutoff)


def is_old_user(user_id, tg_id, subject_id, closed_at=None) -> bool:
    """Был ли ученик с программой ДО закрытия денежной акции."""
    closed_at = closed_at or new_closed_at(subject_id)
    cut = utils.parse_utc_naive(closed_at) if closed_at else None
    if not cut:
        return True
    group = group_ids(subject_id)
    ph = ",".join("?" * len(group))
    r = db.fetchone(f"SELECT MIN(COALESCE(first_started_at, started_at)) AS t FROM subject_participants "
                    f"WHERE user_id=? AND subject_id IN ({ph})", (int(user_id), *group))
    if r and _before(r["t"], cut):
        return True
    r = db.fetchone("SELECT granted_at AS t FROM premium_users WHERE user_id=?", (int(user_id),))
    if r and _before(r["t"], cut):
        return True
    try:
        r = db.fetchone("SELECT MIN(created_at) AS t FROM premium_grants WHERE user_id=?", (int(user_id),))
        if r and _before(r["t"], cut):
            return True
    except Exception:
        pass
    if tg_id:
        fam = [x["id"] for x in _family(subject_id)]
        ph = ",".join("?" * len(fam))
        r = db.fetchone(f"SELECT MIN(granted_at) AS t FROM subject_access WHERE user_tg_id=? "
                        f"AND subject_id IN ({ph})", (int(tg_id), *fam))
        if r and _before(r["t"], cut):
            return True
    return False


def _money_off_for(user_id, tg_id, subject_id) -> int:
    closed = new_closed_at(subject_id)
    return 0 if (not closed or is_old_user(user_id, tg_id, subject_id, closed)) else 1


def money_on(p: dict) -> bool:
    """Идут ли у участника деньги (награды, штрафы, грамота)."""
    return bool(p) and is_enabled(p["subject_id"]) and not int(p.get("money_off") or 0)


def money_allowed(tg_id, user_id, subject_id) -> bool:
    """До нажатия «Начать обучение»: будут ли у этого ученика деньги."""
    if not is_enabled(subject_id):
        return False
    return not _money_off_for(user_id, tg_id, subject_id)


def apply_money_rule(subject_id, now: datetime = None) -> int:
    """Пересчитать, кому из участников программы идут деньги. Возвращает,
    скольким деньги включены заново: им отсчёт — с этой минуты (уроки,
    пройденные без денег, задним числом не оплачиваются, дни без денег
    пропусками не считаются)."""
    now = now or _utcnow()
    turned_on = 0
    for row in participants(subject_id):
        with locked_participant(row["id"]) as p:
            if not p:
                continue
            off = _money_off_for(p["user_id"], p["tg_id"], p["subject_id"])
            if off == int(p.get("money_off") or 0):
                continue
            if off:
                db.execute("UPDATE subject_participants SET money_off=1 WHERE id=?", (p["id"],))
            else:
                db.execute("UPDATE subject_participants SET money_off=0, money_since=?, "
                           "days_checked_through=NULL, absence_run=0 WHERE id=?", (_iso(now), p["id"]))
                turned_on += 1
    return turned_on


def set_new_users_closed(subject_id, closed: bool, now: datetime = None) -> tuple:
    """Закрыть/открыть денежную акцию для новых учеников программы."""
    now = now or _utcnow()
    root = root_id(subject_id)
    current = new_closed_at(root)
    if closed and not current:
        db.execute("UPDATE subjects SET reward_new_closed_at=? WHERE id=?", (_iso(now), root))
    elif not closed and current:
        db.execute("UPDATE subjects SET reward_new_closed_at=NULL WHERE id=?", (root,))
    _invalidate()
    turned_on = apply_money_rule(root, now)
    off = sum(1 for x in participants(root) if int(x.get("money_off") or 0))
    if closed:
        return True, (f"Денежная акция закрыта для новых учеников с {fmt_dt(new_closed_at(root))}. "
                      f"Кто был до закрытия — продолжает получать деньги. Сейчас без денег: {off} уч.")
    return True, ("Денежная акция снова открыта для всех"
                  + (f": деньги включены ещё {turned_on} уч., отсчёт — с этой минуты" if turned_on else "") + ".")


def _all_roots() -> list:
    from services import gamification as gm
    return sorted({gm.rating_root(r["id"]) for r in db.fetchall("SELECT id FROM subjects")})


def set_new_users_closed_all(closed: bool, now: datetime = None) -> tuple:
    now = now or _utcnow()
    roots = _all_roots()
    for root in roots:
        set_new_users_closed(root, closed, now)
    return len(roots), ("Денежная акция закрыта для новых учеников во всех предметах. Старые ученики "
                        "продолжают получать деньги." if closed else
                        "Денежная акция открыта для всех во всех предметах.")


def promo_overview() -> dict:
    rows = [subject_row(r) or {} for r in _all_roots()]
    on = [x for x in rows if x.get("rewards_enabled")]
    return {"programs": len(on), "closed": sum(1 for x in on if x.get("reward_new_closed_at")),
            "closed_any": sum(1 for x in rows if x.get("reward_new_closed_at"))}


# ───────────────────────── учёт заданий ученика ─────────────────────────
# reward_items: одна строка на «ученик + урок» (урок и его копии — одно):
# когда конспект открыт впервые и в последний раз, сколько раз; когда ДЗ
# выполнено впервые, сколько попыток; когда принято (впервые пройдено);
# начислено ли вознаграждение и сколько. Строится из первичных данных
# (просмотры, попытки, операции) — это витрина для админа, а не второй
# источник денег: деньги решает reward_transactions с уникальным ref.

ITEM_COLS = ("user_id", "tg_id", "subject_id", "lesson_key", "lesson_id", "test_id", "kind",
             "first_opened_at", "last_opened_at", "opens", "first_attempt_at", "attempts",
             "completed_at", "status", "reward_granted", "reward_amount", "reward_tx_id",
             "reward_at", "reward_note")
KIND_TITLES = {"test": "ДЗ (тест)", "zachet": "Зачёт", "notes": "Конспект"}
ITEM_STATUS = {"completed": "✅ выполнено и принято", "in_progress": "✍️ выполнялось, ещё не принято",
               "opened": "👁 только открыто"}


def sync_items(p: dict, now: datetime = None) -> int:
    """Обновить учёт заданий участника. Пишет только изменившиеся строки.
    Заодно помечает попытку, принёсшую деньги (test_attempts/zachet_attempts.reward_tx_id)."""
    if not p:
        return 0
    now = now or _utcnow()
    sid, uid, tg = p["subject_id"], p["user_id"], p["tg_id"]
    lessons = _lessons(sid)
    if not lessons:
        return 0
    keys = _keys(sid)
    by_key = {l["key"]: l for l in lessons}
    key_of = {int(lid): int(k) for lid, k in keys.items() if int(k) in by_key}
    ids = sorted(key_of)
    meta = {r["id"]: dict(r) for r in _fetch_in("SELECT id, test_id, is_zachet FROM lessons WHERE id IN ({ph})", ids)}
    opens = {}
    if tg:
        for r in _fetch_in("SELECT lesson_id, MIN(COALESCE(first_opened_at, viewed_at)) AS f, "
                           "MAX(COALESCE(last_opened_at, viewed_at)) AS l, SUM(COALESCE(open_count,1)) AS n "
                           "FROM lesson_progress WHERE user_tg_id=? AND lesson_id IN ({ph}) GROUP BY lesson_id",
                           ids, head=(tg,)):
            o = opens.setdefault(key_of[r["lesson_id"]], [None, None, 0])
            o[0], o[1], o[2] = _earliest(o[0], r["f"]), _latest(o[1], r["l"]), o[2] + int(r["n"] or 0)
    tkeys = {}
    for i in ids:
        m = meta.get(i) or {}
        if m.get("test_id") and not m.get("is_zachet"):
            tkeys.setdefault(int(m["test_id"]), set()).add(key_of[i])
    att = {}
    if uid and tkeys:
        for r in _fetch_in("SELECT test_id, MIN(COALESCE(end_time, start_time)) AS f, COUNT(*) AS n "
                           "FROM test_attempts WHERE user_id=? AND status='finished' "
                           "AND COALESCE(publication_id,0)=0 AND COALESCE(attempt_num,1)<>999 "
                           "AND (COALESCE(correct_answers,0)+COALESCE(wrong_answers,0))>0 "
                           "AND test_id IN ({ph}) GROUP BY test_id", sorted(tkeys), head=(uid,)):
            for k in tkeys[r["test_id"]]:
                a = att.setdefault(k, [None, 0])
                a[0], a[1] = _earliest(a[0], r["f"]), a[1] + int(r["n"] or 0)
    zids = [i for i in ids if (meta.get(i) or {}).get("is_zachet")]
    if tg and zids:
        for r in _fetch_in("SELECT lesson_id, MIN(finished_at) AS f, COUNT(*) AS n FROM zachet_attempts "
                           "WHERE user_tg_id=? AND status='finished' AND lesson_id IN ({ph}) "
                           "GROUP BY lesson_id", zids, head=(tg,)):
            a = att.setdefault(key_of[r["lesson_id"]], [None, 0])
            a[0], a[1] = _earliest(a[0], r["f"]), a[1] + int(r["n"] or 0)
    comp = _completion_details(tg, uid, sid, list(by_key))
    txs = {}
    for r in _fetch_in("SELECT id, ref, amount, created_at, reason FROM reward_transactions "
                       "WHERE user_id=? AND type='reward' AND ref IN ({ph})",
                       [f"lesson:{k}" for k in by_key], head=(uid,)):
        try:
            txs[int(r["ref"].split(":", 1)[1])] = dict(r)
        except (ValueError, IndexError):
            continue
    root = root_id(sid)
    want = {}
    for k, l in by_key.items():
        o, a, c, tx = opens.get(k), att.get(k), comp.get(k), txs.get(k)
        if not (o or a or c or tx):
            continue
        kind = "zachet" if l.get("is_zachet") else ("test" if l.get("test_id") else "notes")
        granted = 1 if tx and int(tx["amount"] or 0) > 0 else 0
        note = ""
        if tx and not granted:
            reason = tx.get("reason") or ""
            note = reason.split(" — ", 1)[1] if " — " in reason else "у урока не было цены"
        first_attempt = (a[0] if a else None) if kind != "notes" else (o[0] if o else None)
        want[k] = (int(uid), int(tg), int(root), int(k), int(l["id"]), l.get("test_id"), kind,
                   o[0] if o else None, o[1] if o else None, int(o[2]) if o else 0,
                   first_attempt, int(a[1]) if a else 0,
                   c[0] if c else None, "completed" if c else ("in_progress" if a else "opened"),
                   granted, int(tx["amount"] or 0) if tx else 0, tx["id"] if tx else None,
                   tx["created_at"] if tx else None, note)
    if not want:
        return 0
    have = {r["lesson_key"]: tuple(r[c] for c in ITEM_COLS) for r in _fetch_in(
        "SELECT * FROM reward_items WHERE user_id=? AND lesson_key IN ({ph})", list(want), head=(uid,))}
    changed = [k for k, v in want.items() if have.get(k) != v]
    if not changed:
        return 0
    stamp = _iso(now)
    db.executemany(
        f"INSERT INTO reward_items ({', '.join(ITEM_COLS)}, updated_at) "
        f"VALUES ({', '.join('?' * (len(ITEM_COLS) + 1))}) ON CONFLICT(user_id, lesson_key) DO UPDATE SET "
        + ", ".join(f"{c}=excluded.{c}" for c in ITEM_COLS[1:]) + ", updated_at=excluded.updated_at",
        [want[k] + (stamp,) for k in changed])
    marks_t, marks_z = [], []
    for k in changed:
        tx, c = txs.get(k), comp.get(k)
        if tx and int(tx["amount"] or 0) > 0 and c:
            (marks_t if c[1] == "test" else marks_z if c[1] == "zachet" else []).append((tx["id"], c[2]))
    if marks_t:
        db.executemany("UPDATE test_attempts SET reward_tx_id=? WHERE id=? AND reward_tx_id IS NULL", marks_t)
    if marks_z:
        db.executemany("UPDATE zachet_attempts SET reward_tx_id=? WHERE id=? AND reward_tx_id IS NULL", marks_z)
    return len(changed)


def items_for(user_id, subject_id) -> list:
    """Учёт заданий ученика по программе — для админки."""
    lessons = _lessons(subject_id)
    order = {l["key"]: i for i, l in enumerate(lessons)}
    titles = {l["key"]: l.get("title") or "" for l in lessons}
    rows = [dict(r) for r in db.fetchall("SELECT * FROM reward_items WHERE user_id=? AND subject_id=?",
                                         (int(user_id), root_id(subject_id)))]
    out = []
    for r in sorted(rows, key=lambda r: (order.get(r["lesson_key"], 10 ** 9), r["lesson_key"])):
        r["title"] = titles.get(r["lesson_key"]) or f"урок {r['lesson_key']}"
        r["kind_title"] = KIND_TITLES.get(r["kind"], r["kind"] or "")
        r["status_title"] = ITEM_STATUS.get(r["status"], r["status"] or "")
        for c in ("first_opened_at", "last_opened_at", "first_attempt_at", "completed_at", "reward_at"):
            r[c + "_fmt"] = fmt_dt(r[c]) if r[c] else "—"
        if r["reward_granted"]:
            r["reward_fmt"] = "да, " + fmt(r["reward_amount"])
        elif r["reward_tx_id"]:
            r["reward_fmt"] = "нет (0 ₸" + (f": {r['reward_note']}" if r["reward_note"] else "") + ")"
        else:
            r["reward_fmt"] = "нет"
        out.append(r)
    return out


# ───────────────────────── журнал наград и штрафов ─────────────────────────

JOURNAL_KINDS = {
    "reward": ("Награды", "t.type='reward' AND t.amount>0"),
    "penalty": ("Штрафы", "t.type='penalty' AND t.amount<>0"),
    "zero": ("Нулевые записи (без штрафа, без оплаты)", "t.amount=0"),
    "manual": ("Бонусы и корректировки", "t.type IN ('bonus','manual_adjustment','correction')"),
}


def _journal_action(r: dict, meta: dict) -> tuple:
    """(действие, тип) для строки журнала."""
    t, amount = r["type"], int(r["amount"] or 0)
    if t == "reward":
        what = "Зачёт" if r.get("lesson_is_zachet") else ("ДЗ (тест)" if r.get("lesson_test_id") else "Конспект")
        title = r.get("lesson_title") or f"урок {r.get('lesson_id')}"
        return f"{what} «{title}» — впервые выполнено", ("награда" if amount > 0 else "без оплаты")
    if t == "penalty":
        if meta.get("no_tasks"):
            return "Нет доступных новых заданий — день не считается", "без штрафа"
        n = meta.get("day") or "?"
        if amount == 0:
            return f"Нет учебной активности — {n}-й день", "без штрафа"
        return f"Штраф за {n}-й день без учебной активности", "штраф"
    if t == "manual_adjustment":
        return ("Аннулирование" if (r.get("ref") or "").startswith("annul:") else "Системная корректировка"), "корректировка"
    return TYPE_TITLES.get(t, t), ("бонус" if t == "bonus" else "корректировка")


def journal(subject_id=None, user_id=None, kind=None, date_from: date = None, date_to: date = None,
            limit: int = 500) -> list:
    """История операций для админа: дата, ученик, действие, тип, за что,
    урок/тест, сумма, баланс до и после. Новые сверху."""
    sql = ("SELECT t.*, u.first_name, u.username, s.title AS subject_title, l.title AS lesson_title, "
           "l.test_id AS lesson_test_id, l.is_zachet AS lesson_is_zachet FROM reward_transactions t "
           "LEFT JOIN users u ON u.id=t.user_id LEFT JOIN subjects s ON s.id=t.subject_id "
           "LEFT JOIN lessons l ON l.id=t.lesson_id WHERE 1=1")
    args = []
    if subject_id:
        group = group_ids(subject_id)
        sql += f" AND t.subject_id IN ({','.join('?' * len(group))})"
        args += group
    if user_id:
        sql += " AND t.user_id=?"
        args.append(int(user_id))
    if kind in JOURNAL_KINDS:
        sql += " AND " + JOURNAL_KINDS[kind][1]
    if date_from:
        sql += " AND t.created_at >= ?"
        args.append((date_from - timedelta(days=2)).isoformat())
    rows = db.fetchall(sql + " ORDER BY t.id DESC LIMIT ?", tuple(args) + (limit * 4 if date_from or date_to else limit,))
    out = []
    for r in rows:
        r = dict(r)
        try:
            meta = json.loads(r.get("meta") or "{}") or {}
        except ValueError:
            meta = {}
        day = _parse_day(meta.get("date")) if r["type"] == "penalty" else None
        day = day or local_date(r["created_at"])
        if (date_from and day and day < date_from) or (date_to and day and day > date_to):
            continue
        action, label = _journal_action(r, meta)
        who = r.get("first_name") or ""
        if r.get("username"):
            who += f" @{r['username']}"
        out.append({
            "id": r["id"], "day": day, "date": day.strftime("%d.%m.%Y") if day else "—",
            "created": fmt_dt(r["created_at"]), "who": who.strip() or f"id{r['tg_id']}", "tg_id": r["tg_id"],
            "user_id": r["user_id"], "subject_id": r["subject_id"], "subject_title": r.get("subject_title") or "",
            "action": action, "type_label": label, "reason": r.get("reason") or "",
            "lesson_id": r.get("lesson_id"), "test_id": r.get("lesson_test_id") if r["type"] == "reward" else None,
            "amount": int(r["amount"] or 0), "amount_fmt": fmt(r["amount"], sign=True),
            "before_fmt": fmt(r["balance_before"]) if r.get("balance_before") is not None else "—",
            "after_fmt": fmt(r["balance_after"]) if r.get("balance_after") is not None else "—",
            "negative": int(r["amount"] or 0) < 0,
        })
        if len(out) >= limit:
            break
    return out


# ───────────────────────── перенос данных v74 (идемпотентно) ─────────────────────────

def _flag(key: str) -> bool:
    row = db.fetchone("SELECT value FROM settings WHERE key=?", (key,))
    return bool(row and row["value"])


def _set_flag(key: str) -> None:
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, '1')", (key,))


def backfill_v74() -> dict:
    """• просмотры: первое/последнее открытие и число открытий из старой отметки;
    • попытки тестов: первая (1) или повтор (0) — is_first_attempt;
    • операции: баланс до и после — по порядку записи.
    Балансы и история не меняются, только дописываются пояснения."""
    stats = {}
    cur = db.execute("UPDATE lesson_progress SET first_opened_at=COALESCE(first_opened_at, viewed_at), "
                     "last_opened_at=COALESCE(last_opened_at, viewed_at), open_count=COALESCE(open_count, 1) "
                     "WHERE first_opened_at IS NULL OR last_opened_at IS NULL OR open_count IS NULL")
    stats["opens"] = cur.rowcount
    if not _flag("reward_v74_first_attempts"):
        cur = db.execute(
            "UPDATE test_attempts SET is_first_attempt = CASE "
            "WHEN COALESCE(attempt_num,1)=999 OR COALESCE(publication_id,0)<>0 "
            "OR (COALESCE(correct_answers,0)+COALESCE(wrong_answers,0))=0 THEN 0 "
            "WHEN EXISTS (SELECT 1 FROM test_attempts t WHERE t.user_id=test_attempts.user_id "
            "AND t.test_id=test_attempts.test_id AND t.id<test_attempts.id AND t.status='finished' "
            "AND COALESCE(t.attempt_num,1)<>999 AND COALESCE(t.publication_id,0)=0 "
            "AND (COALESCE(t.correct_answers,0)+COALESCE(t.wrong_answers,0))>0) THEN 0 ELSE 1 END "
            "WHERE status='finished'")
        stats["attempts"] = cur.rowcount
        _set_flag("reward_v74_first_attempts")
    if not _flag("reward_v74_balances"):
        run, upd = {}, []
        for r in db.fetchall("SELECT id, user_id, subject_id, amount FROM reward_transactions "
                             "ORDER BY user_id, subject_id, id"):
            k = (r["user_id"], r["subject_id"])
            b = run.get(k, 0)
            upd.append((b, b + int(r["amount"] or 0), r["id"]))
            run[k] = b + int(r["amount"] or 0)
        if upd:
            db.executemany("UPDATE reward_transactions SET balance_before=?, balance_after=? WHERE id=?", upd)
        stats["balances"] = len(upd)
        _set_flag("reward_v74_balances")
    if any(stats.values()):
        log.info("учёт наград v74: %s", stats)
    return stats


# ───────────────────────── уведомления ─────────────────────────

def queue(tg_id, text: str, dedup: str = None, now: datetime = None) -> bool:
    if not tg_id or not text:
        return False
    cur = db.execute("INSERT OR IGNORE INTO reward_notifications (tg_id, text, dedup, created_at) "
                     "VALUES (?,?,?,?)", (int(tg_id), text, dedup, _iso(now or _utcnow())))
    return bool(cur.rowcount)


async def flush(bot, limit: int = 40) -> int:
    from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramBadRequest
    rows = await asyncio.to_thread(
        db.fetchall, "SELECT * FROM reward_notifications WHERE sent_at IS NULL AND attempts<5 "
                     "ORDER BY id LIMIT ?", (limit,))
    sent = 0
    for r in rows:
        try:
            # Простым текстом: у бота по умолчанию HTML, а в названиях уроков и
            # причинах бывают «<», «>», «&» — Telegram такое сообщение отверг бы.
            await bot.send_message(r["tg_id"], r["text"], parse_mode=None,
                                   disable_web_page_preview=True)
            status = "sent"
        except TelegramRetryAfter as e:
            await asyncio.sleep(min(60, e.retry_after + 1))
            break
        except (TelegramForbiddenError, TelegramBadRequest):
            status = "gone"              # закрыл бота — больше не пытаемся
        except Exception as e:
            log.warning("уведомление %s: %s", r["id"], e)
            await asyncio.to_thread(db.execute, "UPDATE reward_notifications SET attempts=attempts+1 "
                                                "WHERE id=?", (r["id"],))
            continue
        await asyncio.to_thread(db.execute, "UPDATE reward_notifications SET sent_at=?, "
                                            "attempts=attempts+1 WHERE id=?",
                                (_iso(_utcnow()), r["id"]))
        sent += 1 if status == "sent" else 0
        await asyncio.sleep(0.05)
    return sent


# ───────────────────────── фоновая работа ─────────────────────────

def sweep(now: datetime = None) -> dict:
    """Страховка раз в несколько минут: доступ, начисления, штрафы, предупреждения, места."""
    now = now or _utcnow()
    res = {"annulled": 0, "rewards": 0, "penalties": 0, "warnings": 0}
    subjects = set()
    rows = db.fetchall("SELECT id, user_id, subject_id FROM subject_participants "
                       "WHERE is_active=1 OR (paused_at IS NOT NULL AND status<>'annulled')")
    for r in rows:
        try:
            with locked_participant(r["id"]) as p:
                if not p:
                    continue
                if check_access(p, now):
                    res["annulled"] += 1
                    continue
                p = participant_by_id(r["id"])
                if not p or not p["is_active"]:
                    continue                                 # на паузе
                res["rewards"] += len(reconcile(p, now))
                p = refresh(p, now, rank=False)
                res["penalties"] += len(penalize(p, now))
                res["warnings"] += 1 if warn(participant_by_id(p["id"]), now) else 0
                subjects.add(p["subject_id"])
        except Exception as e:
            log.warning("вознаграждения, участник %s: %s", r["id"], e)
    for sid in subjects:
        rank_subject(sid)
    return res


async def loop(bot) -> None:
    await asyncio.sleep(30)
    last_sweep = 0.0
    while True:
        try:
            await flush(bot)
        except Exception as e:
            log.warning("очередь уведомлений: %s", e)
        if time.time() - last_sweep >= 300:
            last_sweep = time.time()
            try:
                res = await asyncio.to_thread(sweep)
                if any(res.values()):
                    log.info("вознаграждения: %s", res)
            except Exception as e:
                log.warning("вознаграждения, проход: %s", e)
        await asyncio.sleep(15)
