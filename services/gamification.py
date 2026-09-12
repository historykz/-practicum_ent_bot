"""
Геймификация по предметам: баллы, место в рейтинге, прогресс, достижения,
ежедневная мотивация.

Это НЕ вторая система баллов. Считаем по тем же данным, что уже есть:

  баллы      — test_attempts.score (их ставит тот же код, что и раньше);
  прогресс   — webapp.learning._lesson_completed_sync, ровно как у стоп-уроков;
  достижения — таблица user_achievements, та же, что у рефералов;
  активность — study_events, те же события, что смотрит контроль обучения.

Своих таблиц с баллами и рейтингом здесь нет и не заводится.

Важное отличие от общего рейтинга (services/rating_service.py): там место
считается по СУММЕ баллов всех попыток, здесь по предмету берётся ЛУЧШАЯ
попытка каждого теста. Иначе балл рос бы от простого перепрохождения одного
и того же теста. Общий рейтинг мы при этом не трогаем — он живёт как жил.
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import database as db
import utils
from webapp import shortcuts as sc

log = logging.getLogger(__name__)

ALMATY = timezone(timedelta(hours=5))

# Кэш тяжёлых выборок: страница предмета открывается часто, а состав
# учеников и их баллы меняются медленно.
_CACHE_TTL = 60
_cache: dict = {}


def _cached(key, builder):
    now = time.time()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    value = builder()
    if len(_cache) > 400:
        _cache.clear()
    _cache[key] = (now + _CACHE_TTL, value)
    return value


def invalidate(subject_id=None) -> None:
    """Сбросить кэш — после правок каталога или новой сдачи теста.
    Сбрасываем всё: рейтинг у копий предмета общий, и запись одного предмета
    меняет доску всех остальных в его группе."""
    _cache.clear()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace(" ", "T").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------- Из чего состоит предмет ----------

def subject_lessons(subject_id: int) -> list:
    """Открытые уроки предмета — тем же способом, что и стоп-уроки."""
    from webapp import learning as lg
    real = sc.orig_subject_id(int(subject_id))
    return [l for l in lg._flatten_subject_lessons_sync(real)
            if (l.get("status") or "open") == "open"]


def _event_lesson_ids(subject_id) -> list:
    """Уроки предмета для учебных событий (серия, последняя активность, темы).
    У копии, переведённой из ярлыка (v69), события до перевода записаны на
    урок-оригинал — без его id серия и активность копии обнулились бы."""
    ids = []
    for l in subject_lessons(subject_id):
        ids.append(l["id"])
        if l.get("legacy_lesson_id"):
            ids.append(l["legacy_lesson_id"])
    return ids


def rating_group(subject_id) -> list:
    """Предметы ОДНОГО рейтинга: предмет и все его копии (витрины, потоки,
    бывшие ярлыки) — это один и тот же предмет, и ученик видит своё место
    среди всех, кто нажал «Начать обучение» в любой из копий. Иначе у каждой
    копии был бы свой список: «1 из 1», «2 из 2», «3 из 4». Копию можно
    отделить галочкой «Отдельный рейтинг» в её настройках."""
    real = int(sc.orig_subject_id(int(subject_id)))

    def _build():
        try:
            rows = {r["id"]: dict(r) for r in db.fetchall(
                "SELECT id, copied_from_subject_id, copy_group_id, separate_rating FROM subjects")}
        except Exception:
            return [real]

        def root(sid):
            seen, cur = set(), sid
            while cur in rows and cur not in seen:
                seen.add(cur)
                r = rows[cur]
                parent = r.get("copy_group_id") or r.get("copied_from_subject_id")
                if r.get("separate_rating") or not parent or parent not in rows:
                    return cur
                cur = parent
            return cur

        mine = root(real)
        return sorted(sid for sid in rows if root(sid) == mine) or [real]

    return _cached(("group", real), _build)


def _lesson_keys() -> dict:
    """{урок: ключ}. Копии одного урока (полная копия, бывший ярлык) — один
    ключ: одна домашка считается один раз, сколько бы копий её ни было."""
    def _build():
        try:
            rows = {r["id"]: (r["cf"], r["lg"]) for r in db.fetchall(
                "SELECT id, copied_from_lesson_id AS cf, legacy_lesson_id AS lg FROM lessons")}
        except Exception:
            return {}
        out = {}
        for lid in rows:
            seen, cur = set(), lid
            while cur in rows and cur not in seen:
                seen.add(cur)
                cf, lg = rows[cur]
                parent = lg or cf
                if not parent or parent not in rows:
                    break
                cur = parent
            out[lid] = cur
        return out

    return _cached(("lesson_keys",), _build)


def _test_keys(subject_id) -> dict:
    """{тест урока: «какой это урок»} по всей группе рейтинга предмета.
    Урок и его копии — один ключ: это одна домашка, лучший балл берётся один."""
    keys = _lesson_keys()
    out = {}
    for sid in rating_group(subject_id):
        for l in subject_lessons(sid):
            if l.get("test_id"):
                out.setdefault(l["test_id"], keys.get(l["id"], l["id"]))
    return out


def subject_test_ids(subject_id: int) -> list:
    """Тесты предмета. Один тест на два урока считаем один раз."""
    seen, out = set(), []
    for l in subject_lessons(subject_id):
        tid = l.get("test_id")
        if tid and tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out


# ---------- Баллы ----------

def _points_by_user(subject_id: int) -> dict:
    """{user_id: баллы} по всем ученикам предмета — одним запросом.

    Берём лучшую попытку каждого теста: перепрохождение не накручивает балл.
    Черновики подборок из канала (publication_id) и бесплатный повтор ошибок
    (attempt_num=999) в счёт не идут — как и в остальной статистике.
    """
    keys = _test_keys(subject_id)
    if not keys:
        return {}
    ph = ",".join("?" * len(keys))
    rows = db.fetchall(
        f"SELECT user_id, test_id, MAX(COALESCE(score,0)) AS best FROM test_attempts "
        f"WHERE test_id IN ({ph}) AND status='finished' "
        f"AND COALESCE(publication_id,0)=0 AND COALESCE(attempt_num,1)<>999 "
        f"GROUP BY user_id, test_id", tuple(keys))
    # Один урок — один лучший балл, даже если у него два теста (урок и его
    # бывший ярлык в том же предмете): иначе старая попытка считалась дважды
    best = {}
    for r in rows:
        k = (r["user_id"], keys[r["test_id"]])
        best[k] = max(best.get(k, 0), int(r["best"] or 0))
    out = {}
    for (uid, _key), v in best.items():
        out[uid] = out.get(uid, 0) + v
    return out


def points(user_id: int, subject_id: int) -> int:
    """Баллы одного человека по предмету."""
    if not user_id:
        return 0
    table = _cached(("pts", int(sc.orig_subject_id(int(subject_id)))),
                    lambda: _points_by_user(subject_id))
    return int(table.get(user_id, 0))


# ---------- Кто ученик предмета ----------

def _students_of(subject_id: int) -> dict:
    """{user_id: tg_id} — у кого сейчас есть доступ именно к этому предмету.

    Правило то же, что в контроле обучения: доступ, выданный на предмет, или
    общий Премиум, если предмет его принимает. Премиум сам по себе не делает
    человека учеником предмета, который продаётся отдельно.
    """
    from services import study_subjects as sj
    out = {}
    for s in sj.subject_students(subject_id):
        if s.get("user_id"):
            out[s["user_id"]] = s["tg_id"]
    return out


def _participant_ids(subject_id: int) -> list:
    """Участники рейтинга предмета — кто нажал «✨ НАЧАТЬ ОБУЧЕНИЕ ✨» в нём
    или в любой его копии и не потерял доступ (services/reward_service.py,
    таблица subject_participants). Один человек — один раз."""
    group = rating_group(subject_id)
    try:
        rows = db.fetchall(f"SELECT DISTINCT user_id FROM subject_participants "
                           f"WHERE subject_id IN ({','.join('?' * len(group))}) AND is_active=1",
                           tuple(group))
    except Exception:
        return []
    return [r["user_id"] for r in rows]


def leaderboard(subject_id: int) -> list:
    """[(user_id, баллы)] по убыванию — участники рейтинга предмета.

    Правило владельца: в рейтинг попадает Премиум-ученик, который нажал
    «Начать обучение». Он встаёт сразу со всеми своими баллами (лучшая
    попытка каждого теста), даже если прошёл уроки задолго до кнопки.
    Добавлять вручную никого не нужно; истёк Премиум — выбывает сам.
    Рейтинг общий на предмет и все его копии (rating_group).
    """
    real = int(sc.orig_subject_id(int(subject_id)))

    def _build():
        pts = _points_by_user(real)
        rows = [(uid, int(pts.get(uid, 0))) for uid in _participant_ids(real)]
        rows.sort(key=lambda x: (-x[1], x[0]))
        return rows

    return _cached(("board", real), _build)


def position(user_id: int, subject_id: int) -> dict:
    """Место среди участников предмета: {place, total, points, to_next, in_rating}.

    Место спортивное: сколько участников строго выше по баллам, плюс один.
    Не нажал «Начать обучение» — места нет (place=0), но число участников видно.
    """
    board = leaderboard(subject_id)
    my = points(user_id, subject_id)
    in_rating = any(uid == user_id for uid, _p in board)
    higher = [p for _uid, p in board if p > my]
    place = (len(higher) + 1) if in_rating else 0
    to_next = (min(higher) - my) if (higher and in_rating) else 0
    return {"place": place, "total": len(board), "points": my, "to_next": to_next,
            "in_rating": in_rating}


# ---------- Серия занятий ----------

def streak_days(tg_id: int, subject_id=None) -> int:
    """Сколько дней подряд человек занимался (по учебным событиям).

    Считаем по календарным дням Алматы. Сегодняшний день не обязателен:
    если вчера занимался, серия ещё жива — иначе она «рвалась» бы каждое утро.
    """
    if not tg_id:
        return 0
    params = [tg_id]
    where = "tg_id=? AND event IN ('note_open','hw_start','hw_done')"
    if subject_id:
        lids = _event_lesson_ids(subject_id)
        if not lids:
            return 0
        where += f" AND lesson_id IN ({','.join('?' * len(lids))})"
        params += lids
    try:
        rows = db.fetchall(
            f"SELECT DISTINCT date(created_at, '+5 hours') AS d FROM study_events "
            f"WHERE {where} ORDER BY d DESC LIMIT 400", tuple(params))
    except Exception:
        return 0
    days = [r["d"] for r in rows if r["d"]]
    if not days:
        return 0
    today = _now().astimezone(ALMATY).date()
    try:
        first = datetime.strptime(days[0], "%Y-%m-%d").date()
    except ValueError:
        return 0
    if (today - first).days > 1:
        return 0                       # серия уже прервалась
    streak, prev = 1, first
    for raw in days[1:]:
        try:
            cur = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            continue
        if (prev - cur).days == 1:
            streak += 1
            prev = cur
        elif (prev - cur).days > 1:
            break
    return streak


def last_activity(tg_id: int, subject_id=None) -> Optional[str]:
    params = [tg_id]
    where = "tg_id=? AND event IN ('note_open','hw_start','hw_done')"
    if subject_id:
        lids = _event_lesson_ids(subject_id)
        if not lids:
            return None
        where += f" AND lesson_id IN ({','.join('?' * len(lids))})"
        params += lids
    try:
        row = db.fetchone(
            f"SELECT MAX(created_at) AS m FROM study_events WHERE {where}", tuple(params))
        return row["m"] if row else None
    except Exception:
        return None


# ---------- Достижения (таблица user_achievements — та же, что была) ----------

# code → (эмодзи, название, как получить). Проверка — по реальным действиям.
CATALOG = [
    ("first_lesson",   "🌱", "Первый шаг",        "Закрыт первый урок"),
    ("lessons_5",      "📗", "Пятёрка",           "Закрыто 5 уроков"),
    ("lessons_10",     "📘", "Десятка",           "Закрыто 10 уроков"),
    ("lessons_25",     "📚", "Двадцать пять",     "Закрыто 25 уроков"),
    ("lessons_50",     "🎓", "Полсотни",          "Закрыто 50 уроков"),
    ("lessons_100",    "🏛", "Сотня",             "Закрыто 100 уроков"),
    ("first_test",     "✍️", "Первое ДЗ",         "Сдано первое домашнее задание"),
    ("perfect_test",   "💯", "Без единой ошибки", "Тест сдан на 100%"),
    ("tests_10",       "🎯", "Десять ДЗ",         "Сдано 10 домашних заданий"),
    ("tests_50",       "🏹", "Полсотни ДЗ",       "Сдано 50 домашних заданий"),
    ("streak_3",       "🔥", "Три дня подряд",    "3 дня занятий подряд"),
    ("streak_7",       "🔥", "Неделя подряд",     "7 дней занятий подряд"),
    ("streak_30",      "⚡️", "Месяц подряд",      "30 дней занятий подряд"),
    ("subject_half",   "🥈", "Половина предмета", "Пройдена половина предмета"),
    ("subject_done",   "👑", "Предмет закрыт",    "Предмет пройден полностью"),
    ("points_500",     "⭐", "500 баллов",        "Набрано 500 баллов"),
    ("points_1000",    "🌟", "1000 баллов",       "Набрано 1000 баллов"),
    ("points_5000",    "💫", "5000 баллов",       "Набрано 5000 баллов"),
    ("early_bird",     "🌅", "Ранняя пташка",     "Занятие до 8 утра"),
    ("night_owl",      "🦉", "Сова",              "Занятие после полуночи"),
    ("referral_first", "🎁", "Первый друг",       "Приглашён первый друг"),
]
TITLES = {code: (icon, title, how) for code, icon, title, how in CATALOG}
TOTAL_ACHIEVEMENTS = len(CATALOG)


def earned(user_id: int) -> list:
    """Полученные достижения: [{code, icon, title, how, at}]."""
    if not user_id:
        return []
    try:
        rows = db.fetchall(
            "SELECT code, created_at FROM user_achievements WHERE user_id=? "
            "ORDER BY created_at, id", (user_id,))
    except Exception:
        return []
    out = []
    for r in rows:
        code = r["code"]
        icon, title, how = TITLES.get(code, ("🏅", code, ""))
        out.append({"code": code, "icon": icon, "title": title, "how": how,
                    "at": r["created_at"]})
    return out


def locked(user_id: int) -> list:
    """Ещё не полученные — их видит и ученик, и админ."""
    have = {a["code"] for a in earned(user_id)}
    return [{"code": c, "icon": i, "title": t, "how": h}
            for c, i, t, h in CATALOG if c not in have]


def _grant(user_id: int, code: str) -> bool:
    try:
        db.execute("INSERT OR IGNORE INTO user_achievements (user_id, code) VALUES (?,?)",
                   (user_id, code))
        return True
    except Exception as e:
        log.warning("достижение %s для %s: %s", code, user_id, e)
        return False


def award(tg_id: int, user_id: int = None) -> list:
    """Выдать все заслуженные достижения. Возвращает новые (для показа).

    Вызывается после реальных действий (сдал ДЗ, закрыл урок). Повторная
    выдача невозможна: UNIQUE(user_id, code) в таблице.
    """
    if not user_id:
        u = utils.get_user_by_tg(tg_id) if tg_id else None
        user_id = u["id"] if u else None
    if not user_id:
        return []
    have = {a["code"] for a in earned(user_id)}
    fresh = []

    def give(code):
        if code not in have and _grant(user_id, code):
            have.add(code)
            fresh.append(code)

    # Сданные ДЗ и лучший результат
    # Тест урока, переведённого из ярлыка (v69), — та же домашка, что у
    # оригинала: пересдача в копии не делает её «новой» для «Десять ДЗ»
    row = db.fetchone(
        "SELECT COUNT(DISTINCT COALESCE((SELECT t.copied_from_test_id FROM tests t "
        "JOIN lessons l ON l.test_id=t.id WHERE t.id=ta.test_id AND l.legacy_lesson_id IS NOT NULL "
        "LIMIT 1), ta.test_id)) AS n, MAX(CASE WHEN (ta.correct_answers+ta.wrong_answers)>0 "
        "THEN ta.correct_answers*100.0/(ta.correct_answers+ta.wrong_answers) ELSE 0 END) AS best "
        "FROM test_attempts ta WHERE ta.user_id=? AND ta.status='finished' "
        "AND COALESCE(ta.publication_id,0)=0 AND ta.cloned_from_attempt_id IS NULL", (user_id,))
    tests_done = int((row or {}).get("n") or 0)
    best_pct = float((row or {}).get("best") or 0)
    if tests_done >= 1:
        give("first_test")
    if tests_done >= 10:
        give("tests_10")
    if tests_done >= 50:
        give("tests_50")
    if best_pct >= 100:
        give("perfect_test")

    # Закрытые уроки по всем предметам
    done_total, subj_flags = 0, {"half": False, "full": False}
    total_points = 0
    for s in _dedup(_user_subjects(tg_id, user_id)):
        done_total += s["lessons_done"]
        total_points += s["points"]
        if s["lessons_total"]:
            if s["lessons_done"] >= s["lessons_total"]:
                subj_flags["full"] = True
            elif s["lessons_done"] * 2 >= s["lessons_total"]:
                subj_flags["half"] = True
    for need, code in ((1, "first_lesson"), (5, "lessons_5"), (10, "lessons_10"),
                       (25, "lessons_25"), (50, "lessons_50"), (100, "lessons_100")):
        if done_total >= need:
            give(code)
    if subj_flags["half"] or subj_flags["full"]:
        give("subject_half")
    if subj_flags["full"]:
        give("subject_done")
    for need, code in ((500, "points_500"), (1000, "points_1000"), (5000, "points_5000")):
        if total_points >= need:
            give(code)

    # Серия занятий
    st = streak_days(tg_id)
    for need, code in ((3, "streak_3"), (7, "streak_7"), (30, "streak_30")):
        if st >= need:
            give(code)

    # Во сколько занимается
    try:
        hours = db.fetchall(
            "SELECT DISTINCT CAST(strftime('%H', created_at, '+5 hours') AS INTEGER) AS h "
            "FROM study_events WHERE tg_id=? AND event IN ('note_open','hw_start','hw_done')",
            (tg_id,))
        hs = {r["h"] for r in hours if r["h"] is not None}
        if any(h < 8 for h in hs):
            give("early_bird")
        if any(h < 5 for h in hs):
            give("night_owl")
    except Exception:
        pass
    return fresh


# ---------- Сводка по предмету и по человеку ----------

def _user_subjects(tg_id: int, user_id: int) -> list:
    """Предметы, которые человек реально изучает (есть доступ и есть предмет)."""
    from webapp import learning as lg
    out = []
    try:
        rows = db.fetchall("SELECT * FROM subjects WHERE status='active'")
    except Exception:
        return out
    for s in rows:
        s = dict(s)
        sid = s["id"]
        if sc.orig_subject_id(sid) != sid:
            continue                    # копия-витрина: считаем на оригинале
        if not lg._has_subject_access_sync(sid, tg_id) and not _premium_covers(sid, user_id):
            continue
        lessons = subject_lessons(sid)
        if not lessons:
            continue
        state = lg._completion_state_sync(user_id, tg_id)
        pass_pct = s["pass_percent"] or 0
        done = sum(1 for l in lessons
                   if lg._lesson_completed_sync(user_id, tg_id, l, pass_pct, state))
        out.append({"subject_id": sid, "title": s["title"],
                    "lessons_total": len(lessons), "lessons_done": done,
                    "points": points(user_id, sid),
                    "group": s.get("copy_group_id") or sid})
    return out


def _dedup(subjects: list) -> list:
    """Для общих сумм (достижения, общий балл): оригинал и его бывшие витрины,
    переведённые в копии (v69), — одна программа с общей историей. Её уроки и
    баллы считаем один раз, по лучшему из этих предметов, — иначе у ученика
    с доступом к обоим всё удвоилось бы."""
    best = {}
    for s in subjects:
        k = s.get("group") or s["subject_id"]
        if k not in best or (s["lessons_done"], s["points"]) > (best[k]["lessons_done"], best[k]["points"]):
            best[k] = s
    return list(best.values())


def _premium_covers(subject_id: int, user_id: int) -> bool:
    """Открывает ли общий Премиум этот предмет."""
    if not user_id or not utils.is_premium(user_id):
        return False
    subj = db.fetchone("SELECT * FROM subjects WHERE id=?", (subject_id,))
    if not subj:
        return False
    subj = dict(subj)
    return (sc.subject_mode(subj) in (sc.OPEN, sc.PREMIUM)
            and not sc.premium_ignored(subj))


def has_access(tg_id: int, user_id: int, subject_id: int) -> bool:
    """Показывать ли этому человеку геймификацию этого предмета.

    Только тем, у кого предмет реально открыт: выданный доступ или Премиум,
    который этот предмет принимает. Остальные игровых элементов не видят вообще.
    """
    if not tg_id or not user_id:
        return False
    from webapp import learning as lg
    if lg._has_subject_access_sync(subject_id, tg_id):
        return True
    return _premium_covers(sc.orig_subject_id(int(subject_id)), user_id)


def subject_card(tg_id: int, user_id: int, subject_id: int,
                 lessons_done: int = None, lessons_total: int = None) -> Optional[dict]:
    """Всё, что видит ученик по предмету. None — если предмет ему не открыт."""
    if not has_access(tg_id, user_id, subject_id):
        return None
    real = sc.orig_subject_id(int(subject_id))
    if lessons_total is None or lessons_done is None:
        from webapp import learning as lg
        subj = db.fetchone("SELECT pass_percent FROM subjects WHERE id=?", (real,))
        lessons = subject_lessons(real)
        state = lg._completion_state_sync(user_id, tg_id)
        pass_pct = (subj["pass_percent"] if subj else 0) or 0
        lessons_total = len(lessons)
        lessons_done = sum(1 for l in lessons
                           if lg._lesson_completed_sync(user_id, tg_id, l, pass_pct, state))
    pos = position(user_id, real)
    left = max(0, (lessons_total or 0) - (lessons_done or 0))
    percent = round(lessons_done * 100 / lessons_total) if lessons_total else 0
    card = {
        "subject_id": real,
        "points": pos["points"],
        "place": pos["place"],
        "total_students": pos["total"],
        "in_rating": pos["in_rating"],
        "to_next": pos["to_next"],
        "lessons_done": lessons_done,
        "lessons_total": lessons_total,
        "lessons_left": left,
        "percent": percent,
        "streak": streak_days(tg_id, real),
        "achievements_earned": len(earned(user_id)),
        "achievements_total": TOTAL_ACHIEVEMENTS,
    }
    card["motivation"] = daily_motivation(tg_id, card)
    return card


def overall(tg_id: int, user_id: int) -> dict:
    """Общий балл по всем предметам ученика + разбивка."""
    subjects = _user_subjects(tg_id, user_id)
    subjects.sort(key=lambda s: -s["points"])
    uniq = _dedup(subjects)
    return {
        "subjects": subjects,
        "total_points": sum(s["points"] for s in uniq),
        "lessons_done": sum(s["lessons_done"] for s in uniq),
        "lessons_total": sum(s["lessons_total"] for s in uniq),
        "achievements_earned": len(earned(user_id)),
        "achievements_total": TOTAL_ACHIEVEMENTS,
        "streak": streak_days(tg_id),
    }


# ---------- Ежедневная мотивация ----------

def daily_motivation(tg_id: int, card: dict = None) -> str:
    """Фраза дня — из мотивашек, загруженных админом.

    Берём ровно то, что админ прислал файлом («🔥 Мотивация» в контроле
    обучения): одна строка файла — одна мотивашка. Свои фразы бот здесь не
    придумывает. Если админ ничего не загрузил или всё выключил, на странице
    просто нет этого блока.

    Каждый день показывается следующая строка, у разных учеников — разные:
    так один и тот же текст не висит неделями и не совпадает у всех сразу.
    Внутри суток фраза не меняется, чтобы страница не «прыгала» при каждом
    обновлении. Отправки отстающим это никак не задевает: сюда мы только
    читаем, в журнал рассылки ничего не пишем.
    """
    from services import motivation_service as ms
    try:
        items = [i for i in ms.all_items(only_enabled=True)
                 if (i.get("text") or "").strip()]
    except Exception as e:
        log.warning("мотивации для страницы предмета: %s", e)
        return ""
    if not items:
        return ""
    day = _now().astimezone(ALMATY).toordinal()
    idx = (day + int(tg_id or 0)) % len(items)
    return (items[idx].get("text") or "").strip()


# ---------- Аналитика для админа ----------

def top_opened(tg_id: int, subject_id=None, limit: int = 8) -> list:
    """Какие темы человек открывает чаще всего. Это НЕ прохождение урока —
    отдельная метрика: сколько раз заходил в тему."""
    params = [tg_id]
    where = "tg_id=? AND event='note_open' AND lesson_id IS NOT NULL"
    if subject_id:
        lids = _event_lesson_ids(subject_id)
        if not lids:
            return []
        where += f" AND lesson_id IN ({','.join('?' * len(lids))})"
        params += lids
    try:
        rows = db.fetchall(
            f"SELECT lesson_id, COUNT(*) AS n FROM study_events WHERE {where} "
            f"GROUP BY lesson_id", tuple(params))
    except Exception:
        return []
    # Открытия до перевода ярлыка (v69) записаны на урок-оригинал — считаем их
    # тем же уроком копии, а не отдельной строкой
    alias = {}
    if subject_id:
        alias = {l["legacy_lesson_id"]: l["id"] for l in subject_lessons(subject_id)
                 if l.get("legacy_lesson_id")}
    counts = {}
    for r in rows:
        lid = alias.get(r["lesson_id"], r["lesson_id"])
        counts[lid] = counts.get(lid, 0) + r["n"]
    out = []
    for lid, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:limit]:
        les = db.fetchone("SELECT title FROM lessons WHERE id=?", (lid,))
        out.append({"lesson_id": lid, "title": (les["title"] if les else f"Урок {lid}"),
                    "opens": n})
    return out


EVENT_TITLES = {
    "visit": "зашёл в бота",
    "note_open": "открыл тему",
    "hw_start": "начал ДЗ",
    "hw_done": "завершил урок",
}


def recent_activity(tg_id: int, subject_id=None, limit: int = 12) -> list:
    """Последние действия ученика — для карточки в админке."""
    params = [tg_id]
    where = "tg_id=?"
    if subject_id:
        lids = _event_lesson_ids(subject_id)
        if not lids:
            return []
        where += f" AND lesson_id IN ({','.join('?' * len(lids))})"
        params += lids
    try:
        rows = db.fetchall(
            f"SELECT event, lesson_id, test_id, created_at FROM study_events "
            f"WHERE {where} ORDER BY id DESC LIMIT ?", tuple(params + [limit]))
    except Exception:
        return []
    out = []
    for r in rows:
        title = ""
        if r["lesson_id"]:
            les = db.fetchone("SELECT title FROM lessons WHERE id=?", (r["lesson_id"],))
            title = les["title"] if les else ""
        out.append({"event": r["event"],
                    "title_event": EVENT_TITLES.get(r["event"], r["event"]),
                    "lesson_title": title, "at": r["created_at"]})
    return out


def admin_subject_stats(tg_id: int, user_id: int, subject_id: int) -> dict:
    """Полная статистика ученика по предмету — для админки."""
    from webapp import learning as lg
    from services import study_subjects as sj
    real = sc.orig_subject_id(int(subject_id))
    subj = db.fetchone("SELECT id, title, pass_percent FROM subjects WHERE id=?", (real,))
    lessons = subject_lessons(real)
    state = lg._completion_state_sync(user_id, tg_id)
    pass_pct = (subj["pass_percent"] if subj else 0) or 0
    done = sum(1 for l in lessons
               if lg._lesson_completed_sync(user_id, tg_id, l, pass_pct, state))
    opened = sum(1 for l in lessons if l["id"] in state["viewed"])
    keys = _test_keys(real)
    tests_done = attempts = 0
    if keys:
        ph = ",".join("?" * len(keys))
        where = (f"WHERE user_id=? AND test_id IN ({ph}) AND status='finished' "
                 f"AND COALESCE(publication_id,0)=0")
        done_tids = [r["test_id"] for r in db.fetchall(
            f"SELECT DISTINCT test_id FROM test_attempts {where}", (user_id, *keys))]
        tests_done = len({keys[t] for t in done_tids})
        # Перенесённая попытка и её источник — одна попытка
        row = db.fetchone(f"SELECT COUNT(DISTINCT COALESCE(cloned_from_attempt_id, id)) AS a "
                          f"FROM test_attempts {where}", (user_id, *keys))
        attempts = int((row or {}).get("a") or 0)
    track = db.fetchone(
        "SELECT plan_start, expected_done, lag, status, access_until FROM study_tracking "
        "WHERE user_tg_id=? AND subject_id=?", (tg_id, real)) or {}
    pos = position(user_id, real)
    return {
        "subject_id": real,
        "title": subj["title"] if subj else f"Предмет {real}",
        "lessons_total": len(lessons), "lessons_done": done, "lessons_opened": opened,
        "lessons_left": max(0, len(lessons) - done),
        "percent": round(done * 100 / len(lessons)) if lessons else 0,
        "points": pos["points"], "place": pos["place"], "total_students": pos["total"],
        "tests_done": tests_done, "attempts": attempts,
        # Дата начала — по первому реальному действию в предмете. План
        # (plan_start) может опираться на дату выдачи Премиума, а она к началу
        # именно этого предмета отношения не имеет.
        "started_at": _started_at(tg_id, user_id, real) or track.get("plan_start"),
        "expected_done": track.get("expected_done"),
        "lag": track.get("lag"), "status": track.get("status"),
        "status_title": sj.STATUS_TITLES.get(track.get("status") or "", ""),
        "access_until": track.get("access_until"),
        "last_activity": last_activity(tg_id, real),
        "streak": streak_days(tg_id, real),
        "top_opened": top_opened(tg_id, real, 5),
    }


def _started_at(tg_id: int, user_id: int, subject_id: int) -> Optional[str]:
    """Когда человек начал этот предмет — первое действие в нём."""
    lids = [l["id"] for l in subject_lessons(subject_id)]
    if not lids:
        return None
    ph = ",".join("?" * len(lids))
    stamps = []
    for sql, params in (
        (f"SELECT MIN(created_at) AS a FROM study_events WHERE tg_id=? "
         f"AND lesson_id IN ({ph})", (tg_id, *lids)),
        (f"SELECT MIN(viewed_at) AS a FROM lesson_progress WHERE user_tg_id=? "
         f"AND lesson_id IN ({ph})", (tg_id, *lids)),
    ):
        try:
            r = db.fetchone(sql, params)
            if r and r["a"]:
                stamps.append(str(r["a"]))
        except Exception:
            pass
    return min(stamps) if stamps else None


def admin_user_card(tg_id: int) -> dict:
    """Сводка по ученику для админки: Премиум, предметы, баллы, достижения."""
    user = utils.get_user_by_tg(tg_id)
    if not user:
        return {}
    user_id = user["id"]
    info = utils.get_premium_info(user_id) or {}
    subjects = _user_subjects(tg_id, user_id)
    subjects.sort(key=lambda s: -s["points"])
    uniq = _dedup(subjects)
    lessons_done = sum(s["lessons_done"] for s in uniq)
    lessons_total = sum(s["lessons_total"] for s in uniq)
    return {
        "tg_id": tg_id, "user_id": user_id,
        "name": user.get("first_name") or "", "username": user.get("username") or "",
        "is_premium": utils.is_premium(user_id),
        "premium_since": info.get("granted_at"),
        "premium_until": info.get("expires_at"),
        "started_at": _first_activity(tg_id),
        "last_activity": last_activity(tg_id),
        "subjects": subjects,
        "subjects_count": len(subjects),
        "total_points": sum(s["points"] for s in uniq),
        "lessons_done": lessons_done, "lessons_total": lessons_total,
        "percent": round(lessons_done * 100 / lessons_total) if lessons_total else 0,
        "streak": streak_days(tg_id),
        "achievements": earned(user_id),
        "achievements_total": TOTAL_ACHIEVEMENTS,
    }


def _first_activity(tg_id: int) -> Optional[str]:
    try:
        row = db.fetchone(
            "SELECT MIN(created_at) AS a FROM study_events WHERE tg_id=?", (tg_id,))
        return row["a"] if row else None
    except Exception:
        return None
