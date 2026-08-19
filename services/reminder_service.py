"""
Кампании сообщений о конспектах: ручная кнопка и автонапоминания.

Здесь живут две кампании с одинаковым устройством (текст + подпись кнопки +
ссылка), но разной судьбой:

* notes_manual   — что бот отвечает на кнопку «КОНСПЕКТЫ ЕНТ». Работает всегда
                   и для всех, Премиум не проверяется.
* notes_reminder — автонапоминание. Его можно выключить, у него есть частота,
                   и оно никогда не приходит в неподходящий момент.

Тексты не зашиты в код: админ меняет их в панели, и следующее же сообщение
уходит новым. Всё лежит в основной базе, поэтому едет в бэкап и переживает
рестарт — повторной рассылки после восстановления не будет.
"""
import logging
from datetime import datetime, timedelta

import database as db

log = logging.getLogger(__name__)

MANUAL = "notes_manual"
REMINDER = "notes_reminder"

DEFAULT_URL = ("https://t.me/practicum_ent_bot/practicumentbotproductionupra"
               "?startapp=subj_10")
DEFAULT_BUTTON = "📚 ОТКРЫТЬ КОНСПЕКТЫ"

DEFAULT_MANUAL_TEXT = (
    "📚 <b>Конспекты ЕНТ</b>\n\n"
    "🇰🇿 Все конспекты по Истории Казахстана уже доступны!\n\n"
    "Изучай темы прямо в приложении SmartENT: удобно, последовательно "
    "и в одном месте ✨\n\n"
    "👇 Нажми кнопку ниже, чтобы открыть конспекты:")

DEFAULT_REMINDER_TEXT = (
    "📚 Конспекты по Истории Казахстана уже доступны! 🇰🇿\n\n"
    "Повторяй темы, читай конспекты и готовься к ЕНТ прямо в SmartENT ✨\n\n"
    "👇 Открыть конспекты:")

_DEFAULTS = {
    MANUAL: (DEFAULT_MANUAL_TEXT, 1),
    REMINDER: (DEFAULT_REMINDER_TEXT, 0),   # автонапоминания по умолчанию выключены
}


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


# ===================== кампании =====================

def get_campaign(key: str) -> dict:
    """Действующая кампания. Если её ещё нет — заводим со значениями по умолчанию."""
    row = db.fetchone(
        "SELECT * FROM reminder_campaigns WHERE campaign_key=? AND status='active' "
        "ORDER BY version DESC, id DESC LIMIT 1", (key,))
    if row:
        return dict(row)
    text, enabled = _DEFAULTS.get(key, (DEFAULT_REMINDER_TEXT, 0))
    db.execute(
        "INSERT INTO reminder_campaigns (campaign_key, version, enabled, message_text, "
        "button_text, button_url) VALUES (?,1,?,?,?,?)",
        (key, enabled, text, DEFAULT_BUTTON, DEFAULT_URL))
    return dict(db.fetchone(
        "SELECT * FROM reminder_campaigns WHERE campaign_key=? AND status='active' "
        "ORDER BY id DESC LIMIT 1", (key,)))


def update_campaign(key: str, **fields) -> dict:
    """Поправить текущую версию кампании. Новую версию не создаём —
    для этого есть отдельная кнопка «Новая кампания»."""
    camp = get_campaign(key)
    allowed = ("enabled", "message_text", "button_text", "button_url",
               "cooldown_seconds", "safe_delay_seconds")
    sets, args = [], []
    for f in allowed:
        if f in fields and fields[f] is not None:
            sets.append(f"{f}=?")
            args.append(fields[f])
    if not sets:
        return camp
    sets.append("updated_at=?")
    args += [_now(), camp["id"]]
    db.execute(f"UPDATE reminder_campaigns SET {', '.join(sets)} WHERE id=?", tuple(args))
    return get_campaign(key)


def new_version(key: str, admin_tg_id: int = 0) -> dict:
    """Начать новую кампанию: та же настройка, но следующая версия.

    Прежние отправки остаются в истории привязанными к старой версии, поэтому
    после смены версии люди снова попадают в очередь — но по обычному правилу
    частоты, без мгновенной массовой рассылки.
    """
    camp = get_campaign(key)
    db.execute("UPDATE reminder_campaigns SET status='archived', updated_at=? WHERE id=?",
               (_now(), camp["id"]))
    db.execute(
        "INSERT INTO reminder_campaigns (campaign_key, version, enabled, message_text, "
        "button_text, button_url, cooldown_seconds, safe_delay_seconds, created_by) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (key, (camp["version"] or 1) + 1, camp["enabled"], camp["message_text"],
         camp["button_text"], camp["button_url"], camp["cooldown_seconds"],
         camp["safe_delay_seconds"], admin_tg_id))
    return get_campaign(key)


def campaign_label(key: str) -> str:
    c = get_campaign(key)
    return f"{key}_v{c['version']}"


# ===================== активность пользователя =====================

def touch_activity(tg_id: int, kind: str = "bot") -> None:
    """Отметить, что человек только что что-то делал в SmartENT."""
    now = _now()
    try:
        if kind == "test":
            db.execute("UPDATE users SET last_activity_at=?, last_test_activity_at=? "
                       "WHERE tg_id=?", (now, now, tg_id))
        else:
            db.execute("UPDATE users SET last_activity_at=? WHERE tg_id=?", (now, tg_id))
    except Exception as e:
        log.debug("touch_activity: %s", e)


def busy_reason(tg_id: int, safe_delay: int) -> str:
    """Почему сейчас нельзя писать человеку. Пустая строка — можно.

    Telegram не показывает боту, онлайн человек или нет, поэтому смотрим на
    свои следы: незакрытый тест, живая дуэль, недавнее действие.
    """
    user = db.fetchone("SELECT * FROM users WHERE tg_id=?", (tg_id,))
    if not user:
        return "no_user"
    if user.get("bot_blocked"):
        return "bot_blocked"
    if user.get("is_blocked"):
        return "user_blocked"

    # незавершённая попытка теста
    row = db.fetchone(
        "SELECT ta.id FROM test_attempts ta WHERE ta.user_id=? AND ta.status='in_progress' "
        "LIMIT 1", (user["id"],))
    if row:
        return "active_test"

    # живая дуэль
    try:
        row = db.fetchone(
            "SELECT id FROM duels WHERE (player1_id=? OR player2_id=?) "
            "AND status IN ('waiting','active','in_progress') LIMIT 1",
            (user["id"], user["id"]))
        if row:
            return "active_duel"
    except Exception:
        pass

    # идёт Live-игра
    try:
        row = db.fetchone(
            "SELECT lp.id FROM live_players lp JOIN live_rooms lr ON lr.id=lp.room_id "
            "WHERE lp.tg_id=? AND lr.status IN ('waiting','running') LIMIT 1", (tg_id,))
        if row:
            return "active_live"
    except Exception:
        pass

    # только что что-то делал — даём договорить, не встреваем
    last = user.get("last_activity_at")
    if last and safe_delay > 0:
        try:
            if (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds() < safe_delay:
                return "recent_activity"
        except ValueError:
            pass
    return ""


def has_open_fsm(tg_id: int, states: dict) -> bool:
    """Есть ли у человека незакрытый пошаговый сценарий.

    states — снимок хранилища FSM, который передаёт вызывающая сторона
    (сервис не лезет в диспетчер напрямую, чтобы не зависеть от aiogram).
    """
    return bool(states.get(tg_id))


# ===================== состояние отправок =====================

def user_state(tg_id: int, campaign_id: int) -> dict:
    row = db.fetchone(
        "SELECT * FROM user_reminder_state WHERE user_tg_id=? AND campaign_id=?",
        (tg_id, campaign_id))
    if row:
        return dict(row)
    db.execute(
        "INSERT OR IGNORE INTO user_reminder_state (user_tg_id, campaign_id, next_allowed_at) "
        "VALUES (?,?,?)", (tg_id, campaign_id, _now()))
    return dict(db.fetchone(
        "SELECT * FROM user_reminder_state WHERE user_tg_id=? AND campaign_id=?",
        (tg_id, campaign_id)))


def claim(tg_id: int, campaign_id: int) -> bool:
    """Занять право на отправку. True — заняли именно мы.

    Одним UPDATE с условием: если два процесса дойдут сюда одновременно,
    строку получит только один, второй увидит 0 изменённых строк. Так же
    отсекается повторная отправка после рестарта.
    """
    user_state(tg_id, campaign_id)
    now = _now()
    cur = db.execute(
        "UPDATE user_reminder_state SET last_status='sending', last_attempt_at=?, "
        "updated_at=? WHERE user_tg_id=? AND campaign_id=? "
        "AND COALESCE(last_status,'') <> 'sending' "
        "AND (next_allowed_at IS NULL OR next_allowed_at <= ?)",
        (now, now, tg_id, campaign_id, now))
    return bool(getattr(cur, "rowcount", 0))


def mark_sent(tg_id: int, campaign_id: int, cooldown: int) -> None:
    now = datetime.utcnow()
    db.execute(
        "UPDATE user_reminder_state SET last_status='sent', last_sent_at=?, "
        "next_allowed_at=?, send_count=COALESCE(send_count,0)+1, last_skip_reason='', "
        "updated_at=? WHERE user_tg_id=? AND campaign_id=?",
        (now.isoformat(timespec="seconds"),
         (now + timedelta(seconds=max(60, cooldown))).isoformat(timespec="seconds"),
         now.isoformat(timespec="seconds"), tg_id, campaign_id))


def mark_deferred(tg_id: int, campaign_id: int, reason: str, retry_after: int = 900) -> None:
    """Не отправили — перенесли. Отправкой это не считается."""
    user_state(tg_id, campaign_id)   # строки может ещё не быть: откладываем до захвата
    now = datetime.utcnow()
    db.execute(
        "UPDATE user_reminder_state SET last_status='deferred', last_skip_reason=?, "
        "next_allowed_at=?, updated_at=? WHERE user_tg_id=? AND campaign_id=?",
        (reason, (now + timedelta(seconds=retry_after)).isoformat(timespec="seconds"),
         now.isoformat(timespec="seconds"), tg_id, campaign_id))


def mark_blocked(tg_id: int, campaign_id: int, reason: str = "bot_blocked") -> None:
    """Человек закрыл бота — больше не долбимся."""
    user_state(tg_id, campaign_id)
    db.execute("UPDATE users SET bot_blocked=1 WHERE tg_id=?", (tg_id,))
    db.execute(
        "UPDATE user_reminder_state SET last_status='blocked', last_skip_reason=?, "
        "next_allowed_at='2999-01-01T00:00:00', updated_at=? "
        "WHERE user_tg_id=? AND campaign_id=?", (reason, _now(), tg_id, campaign_id))


def mark_failed(tg_id: int, campaign_id: int, reason: str) -> None:
    mark_deferred(tg_id, campaign_id, reason[:200], retry_after=3600)
    db.execute("UPDATE user_reminder_state SET last_status='failed' "
               "WHERE user_tg_id=? AND campaign_id=?", (tg_id, campaign_id))


def reset_user(tg_id: int, campaign_key: str = REMINDER) -> None:
    camp = get_campaign(campaign_key)
    db.execute("DELETE FROM user_reminder_state WHERE user_tg_id=? AND campaign_id=?",
               (tg_id, camp["id"]))
    db.execute("UPDATE users SET bot_blocked=0 WHERE tg_id=?", (tg_id,))


def user_report(tg_id: int, campaign_key: str = REMINDER) -> dict:
    """Что показать админу в карточке пользователя."""
    camp = get_campaign(campaign_key)
    st = db.fetchone(
        "SELECT * FROM user_reminder_state WHERE user_tg_id=? AND campaign_id=?",
        (tg_id, camp["id"]))
    # Набор полей всегда один и тот же — иначе на новой версии кампании
    # (когда отправок ещё не было) вызывающий код спотыкался бы о KeyError.
    out = {"version": camp["version"], "campaign": campaign_label(campaign_key),
           "never": True, "last_sent_at": "", "next_allowed_at": "",
           "send_count": 0, "last_status": "", "last_skip_reason": ""}
    if not st:
        return out
    st = dict(st)
    out.update({
        "never": not st.get("last_sent_at"),
        "last_sent_at": (st.get("last_sent_at") or "")[:16].replace("T", " "),
        "next_allowed_at": (st.get("next_allowed_at") or "")[:16].replace("T", " "),
        "send_count": st.get("send_count") or 0,
        "last_status": st.get("last_status") or "",
        "last_skip_reason": st.get("last_skip_reason") or "",
    })
    return out


# ===================== кандидаты и статистика =====================

def due_users(campaign_id: int, limit: int = 50) -> list:
    """Кому пора — по времени. Занятость проверяется отдельно, перед отправкой."""
    now = _now()
    rows = db.fetchall(
        "SELECT u.tg_id FROM users u "
        "LEFT JOIN user_reminder_state s ON s.user_tg_id=u.tg_id AND s.campaign_id=? "
        "WHERE COALESCE(u.bot_blocked,0)=0 AND COALESCE(u.is_blocked,0)=0 "
        "AND (s.id IS NULL OR (COALESCE(s.last_status,'') <> 'sending' "
        "     AND (s.next_allowed_at IS NULL OR s.next_allowed_at <= ?))) "
        "ORDER BY COALESCE(s.last_sent_at, '') ASC, u.id ASC LIMIT ?",
        (campaign_id, now, int(limit)))
    return [r["tg_id"] for r in rows]


def stats(campaign_key: str = REMINDER) -> dict:
    camp = get_campaign(campaign_key)
    cid = camp["id"]
    today = datetime.utcnow().strftime("%Y-%m-%d")

    def _c(sql, args=()):
        row = db.fetchone(sql, args)
        return (row or {"c": 0})["c"]

    total_users = _c("SELECT COUNT(*) AS c FROM users")
    got = _c("SELECT COUNT(*) AS c FROM user_reminder_state WHERE campaign_id=? "
             "AND COALESCE(send_count,0)>0", (cid,))
    sent_total = _c("SELECT COALESCE(SUM(send_count),0) AS c FROM user_reminder_state "
                    "WHERE campaign_id=?", (cid,))
    sent_today = _c("SELECT COUNT(*) AS c FROM user_reminder_state WHERE campaign_id=? "
                    "AND last_sent_at LIKE ?", (cid, today + "%"))
    deferred = _c("SELECT COUNT(*) AS c FROM user_reminder_state WHERE campaign_id=? "
                  "AND last_status='deferred'", (cid,))
    deferred_test = _c("SELECT COUNT(*) AS c FROM user_reminder_state WHERE campaign_id=? "
                       "AND last_skip_reason IN ('active_test','active_duel','active_live',"
                       "'open_state')", (cid,))
    blocked = _c("SELECT COUNT(*) AS c FROM users WHERE COALESCE(bot_blocked,0)=1")
    last = db.fetchone("SELECT MAX(last_sent_at) AS m FROM user_reminder_state "
                       "WHERE campaign_id=?", (cid,))
    return {
        "campaign": camp, "label": campaign_label(campaign_key),
        "total_users": total_users, "got_current_version": got,
        "sent_total": sent_total, "sent_today": sent_today,
        "deferred": deferred, "deferred_busy": deferred_test, "blocked": blocked,
        "last_sent_at": ((last or {}).get("m") or "—")[:16].replace("T", " "),
    }


def human_cooldown(seconds: int) -> str:
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "без ограничения"
    if seconds % 86400 == 0:
        d = seconds // 86400
        return f"{d} " + ("день" if d == 1 else "дня" if 2 <= d <= 4 else "дней")
    if seconds % 3600 == 0:
        h = seconds // 3600
        return f"{h} ч"
    return f"{seconds // 60} мин"
