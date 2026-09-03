"""
Настройки контроля обучения и шаблоны сообщений.

Всё, что админ может захотеть поменять — сроки, тексты, время рассылки,
контакт менеджера — лежит в базе, а не в коде. Здесь значения по умолчанию
и удобные функции чтения-записи.
"""
import database as db

# Ключ настройки → значение по умолчанию.
# Это именно значения «из коробки»: админ меняет их в панели, и дальше
# используется его вариант.
DEFAULTS = {
    "study_enabled": "1",            # работает ли контроль обучения
    "study_premium_only": "1",       # писать только премиум-ученикам
    "study_warn1_days": "2",         # мягкое напоминание
    "study_warn2_days": "3",         # напоминание построже
    "study_warn3_days": "4",         # строгое
    "study_send_hour": "18",         # час отправки по Алматы
    "study_send_until": "21",        # позже этого часа не пишем
    "study_quiet_from": "22",        # ночная тишина: с
    "study_quiet_to": "9",           # ночная тишина: до
    "study_min_gap_hours": "20",     # минимум между авто-сообщениями
    "study_motivation_enabled": "1",
    "study_motivation_repeat_days": "14",   # не повторять мотивацию X дней
    "study_report_enabled": "1",     # ежедневный отчёт админу
    "study_report_hour": "20",
    "study_count_visit": "0",        # считать ли простой заход учёбой
    "support_username": "",          # контакт менеджера
    "support_text": ("Если возник вопрос по платформе, напиши нашему "
                     "менеджеру 👇"),
    "onboarding_enabled": "1",
}

# Шаблоны писем. Админ переписывает их под себя, переменные подставляются.
TEMPLATE_TITLES = {
    "warn1": "2 дня без активности",
    "warn2": "3 дня без активности",
    "warn3": "4+ дней без активности",
    "reading_no_hw": "Конспект открыт, ДЗ не сделано",
    "hw_unfinished": "ДЗ начато, но не закончено",
    "returned": "Ученик вернулся",
    "many_unfinished": "Много незакрытых заданий",
    "never_started": "Премиум есть, но занятия не начаты",
}

TEMPLATE_DEFAULTS = {
    "warn1": ("{name}, ты уже {days_without_activity}-й день не занимаешься 👀\n\n"
              "Не выпадай из графика. Сегодня достаточно открыть хотя бы одну "
              "тему и выполнить ДЗ."),
    "warn2": ("Так, это уже {days_without_activity}-й день без нормальной учёбы.\n\n"
              "Если постоянно переносить занятия на завтра, темы начнут "
              "накапливаться. Сегодня обязательно закрой хотя бы одну тему и ДЗ."),
    "warn3": ("{days_without_activity} дня уже потеряно.\n\n"
              "Так подготовка работать не будет. Сейчас не нужно пытаться "
              "закрыть всё сразу — открой одну тему, изучи конспект и выполни "
              "одно ДЗ. Главное — снова войти в ритм."),
    "reading_no_hw": ("Конспект ты уже открыл, но ДЗ по теме осталось "
                      "незакрытым 👀\n\nДавай сегодня закончим тему до конца: "
                      "{last_topic}."),
    "hw_unfinished": ("Просто открыть конспект недостаточно 😐\n\n"
                      "У тебя уже {unfinished_homeworks} незакрытых заданий. "
                      "Сегодня нужно не просто посмотреть материал, а выполнить ДЗ."),
    "returned": ("Вот, уже другое дело 🔥\n\nДЗ выполнено. Продолжаем в таком темпе."),
    "many_unfinished": ("{name}, у тебя накопилось {unfinished_homeworks} "
                        "незакрытых тем.\n\nНе пытайся закрыть всё разом — "
                        "начни с одной: {last_topic}."),
    "never_started": ("{name}, Премиум у тебя уже открыт, а занятия ещё не "
                      "начались 🙂\n\nОткрой первую тему, прочитай конспект и "
                      "выполни ДЗ — дальше пойдёт легче."),
}


def get(key: str, default=None):
    row = db.fetchone("SELECT value FROM settings WHERE key=?", (key,))
    if row and row["value"] not in (None, ""):
        return row["value"]
    if default is not None:
        return default
    return DEFAULTS.get(key, "")


def get_int(key: str, default: int = 0) -> int:
    try:
        return int(str(get(key)).strip())
    except (ValueError, TypeError):
        try:
            return int(DEFAULTS.get(key, default))
        except (ValueError, TypeError):
            return default


def get_bool(key: str) -> bool:
    return str(get(key)).strip() in ("1", "true", "True", "on", "да")


def set_value(key: str, value) -> None:
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
               (key, str(value)))


# ---------- Шаблоны ----------

def ensure_templates() -> None:
    """Первый запуск: кладём тексты по умолчанию, дальше их правит админ."""
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS study_templates (
            key TEXT PRIMARY KEY, text TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        for key, text in TEMPLATE_DEFAULTS.items():
            db.execute("INSERT OR IGNORE INTO study_templates (key, text) VALUES (?,?)",
                       (key, text))
    except Exception:
        pass


def template(key: str) -> str:
    ensure_templates()
    row = db.fetchone("SELECT text, enabled FROM study_templates WHERE key=?", (key,))
    if row and row["enabled"] and (row["text"] or "").strip():
        return row["text"]
    if row and not row["enabled"]:
        return ""                     # админ выключил этот шаблон
    return TEMPLATE_DEFAULTS.get(key, "")


def all_templates() -> list:
    ensure_templates()
    rows = db.fetchall("SELECT * FROM study_templates ORDER BY key")
    out = []
    for r in rows:
        d = dict(r)
        d["title"] = TEMPLATE_TITLES.get(d["key"], d["key"])
        out.append(d)
    return out


def set_template(key: str, text: str = None, enabled: bool = None) -> None:
    ensure_templates()
    if text is not None:
        db.execute("INSERT OR REPLACE INTO study_templates (key, text, enabled) "
                   "VALUES (?, ?, COALESCE((SELECT enabled FROM study_templates "
                   "WHERE key=?), 1))", (key, text, key))
    if enabled is not None:
        db.execute("UPDATE study_templates SET enabled=? WHERE key=?",
                   (1 if enabled else 0, key))


# ---------- Подстановка переменных ----------

VARIABLES = ["{name}", "{days_without_activity}", "{unfinished_homeworks}",
             "{last_topic}", "{completed_topics}", "{premium_end_date}"]


def render(text: str, data: dict) -> str:
    """Подставляет данные ученика в шаблон. Неизвестные переменные не трогаем."""
    if not text:
        return ""
    idle = data.get("days_idle")
    last_topic = ""
    if data.get("unfinished"):
        last_topic = data["unfinished"][0]["title"]
    elif data.get("last_note_title"):
        last_topic = data["last_note_title"]

    values = {
        "{name}": (data.get("name") or "").strip() or "Привет",
        "{days_without_activity}": str(int(idle)) if idle is not None else "0",
        "{unfinished_homeworks}": str(data.get("unfinished_count", 0)),
        "{last_topic}": last_topic or "любая тема из списка",
        "{completed_topics}": str(data.get("done_topics", 0)),
        "{premium_end_date}": (data.get("premium_until") or "")[:10] or "—",
    }
    out = text
    for k, v in values.items():
        out = out.replace(k, v)
    return out
