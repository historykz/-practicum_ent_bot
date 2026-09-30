"""
Утилиты: работа с пользователями, проверки прав, форматирование.
"""
import logging
import re
from datetime import datetime, timedelta, timezone, date
from typing import Optional

import database as db
from config import ADMIN_IDS

logger = logging.getLogger(__name__)


def now_iso() -> str:
    """Текущее время в ISO формате."""
    return datetime.utcnow().isoformat(timespec="seconds")


def today_str() -> str:
    """Сегодняшняя дата как 'YYYY-MM-DD'."""
    return date.today().isoformat()


def yesterday_str() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


# === Пользователи ===
def get_or_create_user(tg_id: int, username: Optional[str] = None,
                       first_name: Optional[str] = None,
                       last_name: Optional[str] = None) -> dict:
    """Получить пользователя по tg_id, создать если не существует.

    Создание атомарное: INSERT OR IGNORE, а не «проверил — вставил». Раньше
    два одновременных запроса от нового человека (открыл мини-приложение и
    нажал /start в одну секунду, или webview прислал /auth/webapp дважды)
    оба не находили строку, и второй INSERT падал на UNIQUE(tg_id) —
    сайт отвечал 500, а бот подставлял пустого пользователя с id=0.
    """
    db.execute(
        "INSERT OR IGNORE INTO users (tg_id, username, first_name, last_name) "
        "VALUES (?,?,?,?)",
        (tg_id, username, first_name, last_name),
    )
    row = db.fetchone("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
    if row is None:
        # Практически невозможно (строка только что гарантирована), но лучше
        # честная ошибка, чем KeyError у вызывающего кода
        raise RuntimeError(f"user {tg_id} not found after insert")
    # Обновляем username/name, если они изменились в Telegram
    if (username and row["username"] != username) or \
       (first_name and row["first_name"] != first_name):
        db.execute(
            "UPDATE users SET username=?, first_name=?, last_name=?, updated_at=? WHERE tg_id=?",
            (username, first_name, last_name, now_iso(), tg_id),
        )
        row = db.fetchone("SELECT * FROM users WHERE tg_id = ?", (tg_id,)) or row
    return dict(row)


def get_user_by_tg(tg_id: int) -> Optional[dict]:
    row = db.fetchone("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[dict]:
    row = db.fetchone("SELECT * FROM users WHERE id = ?", (user_id,))
    return dict(row) if row else None


def find_user_by_arg(arg: str) -> Optional[dict]:
    """Найти пользователя по '@username' (без учёта регистра, как в Telegram) или числовому tg_id."""
    arg = arg.strip()
    if not arg:
        return None
    if arg.startswith("@"):
        uname = arg[1:]
        row = db.fetchone("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (uname,))
        return dict(row) if row else None
    if arg.isdigit():
        return get_user_by_tg(int(arg))
    # Возможно, username без @
    row = db.fetchone("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (arg,))
    return dict(row) if row else None


def get_user_lang(tg_id: int) -> str:
    u = get_user_by_tg(tg_id)
    if not u:
        return "ru"
    return u.get("language") or "ru"


def set_user_lang(tg_id: int, lang: str) -> None:
    db.execute(
        "UPDATE users SET language=?, updated_at=? WHERE tg_id=?",
        (lang, now_iso(), tg_id),
    )


# === Админ / блок ===
def is_admin(tg_id: int) -> bool:
    """Проверка админ-прав. С retry на случай гонки БД."""
    if not tg_id:
        return False
    if tg_id in ADMIN_IDS:
        return True
    # Retry до 3 раз
    for attempt in range(3):
        try:
            row = db.fetchone("SELECT 1 FROM admins WHERE tg_id=?", (tg_id,))
            return bool(row)
        except Exception as e:
            if attempt == 2:
                import logging
                logging.getLogger(__name__).warning(
                    "is_admin DB check failed after 3 retries: %s", e)
                return False
            import time as _t
            _t.sleep(0.05)
    return False


def is_owner(tg_id: int) -> bool:
    """Главный админ (из ADMIN_IDS) — полный доступ везде, всегда."""
    return bool(tg_id) and tg_id in ADMIN_IDS


def admin_level(tg_id: int) -> int:
    """Уровень бот-админа: 3 = владелец (ADMIN_IDS), 2/1 = добавленные в боте, 0 = не админ."""
    if not tg_id:
        return 0
    if tg_id in ADMIN_IDS:
        return 3
    row = db.fetchone("SELECT level FROM admins WHERE tg_id=?", (tg_id,))
    if not row:
        return 0
    lvl = row.get("level")
    return int(lvl) if lvl else 1


def is_site_admin(tg_id: int) -> bool:
    """Доступ к админке САЙТА: только владелец или явно добавленный сайт-админ.
    Бот-админы (таблица admins) сюда НЕ входят — сайт даётся отдельно."""
    if not tg_id:
        return False
    if tg_id in ADMIN_IDS:
        return True
    for attempt in range(3):
        try:
            return bool(db.fetchone("SELECT 1 FROM site_admins WHERE tg_id=?", (tg_id,)))
        except Exception:
            import time as _t
            _t.sleep(0.05)
    return False


def is_blocked(tg_id: int) -> bool:
    row = db.fetchone("SELECT is_blocked FROM users WHERE tg_id=?", (tg_id,))
    return bool(row and row["is_blocked"])


def get_profile_subjects(tg_id: int) -> list:
    """Список профильных: category_id (int) и/или 'other' (str)."""
    row = db.fetchone("SELECT profile_subjects FROM users WHERE tg_id=?", (tg_id,))
    if not row or not row.get('profile_subjects'):
        return []
    out = []
    for x in str(row['profile_subjects']).split(','):
        x = x.strip()
        if x == 'other':
            out.append('other')
        elif x.isdigit():
            out.append(int(x))
    return out


def has_other_subject(tg_id: int) -> bool:
    """Выбрал ли юзер 'Другое' (показывать все тесты)."""
    return 'other' in get_profile_subjects(tg_id)


def set_profile_subjects(tg_id: int, items: list) -> None:
    """Сохранить профильные (category_id или 'other')."""
    parts = []
    for x in items:
        if x == 'other':
            parts.append('other')
        else:
            try:
                parts.append(str(int(x)))
            except (ValueError, TypeError):
                pass
    csv = ",".join(parts)
    db.execute("UPDATE users SET profile_subjects=? WHERE tg_id=?",
                (csv, tg_id))


def manager_username() -> str:
    """Ник менеджера поддержки — без «собачки».

    Сначала смотрим настройку из админки, и только если её не задали —
    значение из .env. Так админ меняет контакт в панели, а бот подхватывает
    его сразу, без правки кода и перезапуска.
    """
    try:
        row = db.fetchone("SELECT value FROM settings WHERE key='support_username'")
        if row and (row.get("value") or "").strip():
            return str(row["value"]).strip().lstrip("@")
    except Exception:
        pass
    try:
        import config
        return (getattr(config, "MANAGER_USERNAME", "") or "").strip().lstrip("@")
    except Exception:
        return ""


def has_profile_subjects(tg_id: int) -> bool:
    return len(get_profile_subjects(tg_id)) > 0


def is_anonymous_chat_admin(message) -> bool:
    """
    Сообщение отправлено от имени чата (анонимный админ)?
    Telegram ставит sender_chat == chat для анонимных админов.
    """
    try:
        sc = getattr(message, 'sender_chat', None)
        if sc and message.chat and sc.id == message.chat.id:
            return True
    except Exception:
        pass
    return False


def set_blocked(user_id: int, blocked: bool) -> None:
    db.execute("UPDATE users SET is_blocked=? WHERE id=?", (1 if blocked else 0, user_id))


# === Premium ===
# ---------- Сроки доступа: один разбор на весь проект ----------
# Премиум, приватные тесты и доступ к предмету проверяются в десятке мест —
# в боте, на сайте, в контроле обучения. Раньше каждое место разбирало срок
# по-своему: одно считало нечитаемую дату истёкшей, другое — вечной, третье
# падало на дате с часовым поясом. Отсюда «?» в списке Премиума у админа и
# приватные тесты, которые не закрывались после дедлайна. Теперь все места
# спрашивают здесь.

_DT_RE = re.compile(
    r"^(\d{4})-(\d{1,2})-(\d{1,2})"
    r"(?:[T ](\d{1,2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?)?"
    r"\s*(Z|[+-]\d{2}(?::?\d{2})?)?$", re.IGNORECASE)
_DMY_RE = re.compile(
    r"^(\d{1,2})\.(\d{1,2})\.(\d{4})(?:[ T,]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$")
_ALMATY_TZ = timezone(timedelta(hours=5))
_unreadable_logged: set = set()


def parse_utc_naive(value) -> Optional[datetime]:
    """Срок из базы → naive UTC. None — если даты нет или она не читается.

    Разбор одинаковый на любой версии Python: раньше сервер (3.12) и
    локальная проверка (3.9) понимали одну и ту же строку по-разному.
    Понимает всё, что у нас когда-либо записывалось: naive UTC (ручная
    выдача), с часовым поясом и микросекундами (звёзды, награда за друзей,
    приватные тесты), с пробелом вместо «T», с «Z», только дату,
    Unix-время и «дд.мм.гггг» (по Астане).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        try:
            return datetime.utcfromtimestamp(float(value))
        except (OverflowError, OSError, ValueError):
            return None
    else:
        s = str(value).strip()
        if not s:
            return None
        if s.isdigit() and 9 <= len(s) <= 11:
            try:
                return datetime.utcfromtimestamp(int(s))
            except (OverflowError, OSError, ValueError):
                return None
        m = _DT_RE.match(s)
        if m:
            y, mo, d, hh, mi, ss, frac, tz = m.groups()
            try:
                dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0),
                              int(ss or 0), int((frac or "0")[:6].ljust(6, "0")))
                if tz:
                    if tz.upper() == "Z":
                        off = timedelta(0)
                    else:
                        digits = tz[1:].replace(":", "")
                        off = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
                        if tz[0] == "-":
                            off = -off
                    dt = dt.replace(tzinfo=timezone(off))
            except ValueError:
                return None
        else:
            m = _DMY_RE.match(s)
            if not m:
                return None
            d, mo, y, hh, mi, ss = m.groups()
            try:
                dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0),
                              int(ss or 0), tzinfo=_ALMATY_TZ)
            except ValueError:
                return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def to_utc_iso(value) -> Optional[str]:
    """Единый формат хранения срока: naive UTC «ГГГГ-ММ-ДДTЧЧ:ММ:СС».

    Тот же, что пишет ручная выдача и now_iso(). Когда все сроки в базе
    одного вида, сравнение прямо в SQL (списки, уведомления об окончании)
    тоже работает правильно.
    """
    dt = parse_utc_naive(value)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") if dt else None


def deadline_active(value) -> bool:
    """Действует ли ещё доступ с таким сроком.

    Пусто — бессрочно. Дата прошла — нет. Нечитаемая дата — тоже нет:
    лучше закрыть и увидеть это в списке админа, чем открыть навсегда.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return True
    dt = parse_utc_naive(value)
    if dt is None:
        key = str(value)[:80]
        if key not in _unreadable_logged and len(_unreadable_logged) < 1000:
            _unreadable_logged.add(key)
            logging.getLogger(__name__).warning(
                "Нечитаемый срок доступа %r — считаем истёкшим", value)
        return False
    return dt > datetime.utcnow()


def describe_deadline(value, now: Optional[datetime] = None) -> dict:
    """Срок для показа людям: активен ли, до какого числа по Астане, сколько осталось."""
    now = now or datetime.utcnow()
    base = {"forever": False, "active": False, "unreadable": False, "raw": value,
            "until": "", "until_date": "", "days_left": None, "hours_left": None}
    if value is None or (isinstance(value, str) and not value.strip()):
        base.update(forever=True, active=True)
        return base
    dt = parse_utc_naive(value)
    if dt is None:
        base["unreadable"] = True
        return base
    local = dt.replace(tzinfo=timezone.utc).astimezone(_ALMATY_TZ)
    active = dt > now
    left = dt - now
    base.update(active=active, dt=dt,
                until=local.strftime("%d.%m.%Y %H:%M"),
                until_date=local.strftime("%d.%m.%Y"),
                days_left=left.days if active else 0,
                hours_left=(left.seconds // 3600) if active else 0)
    return base


def premium_status(user_id: int) -> dict:
    """Премиум человека для показа: то же решение, что у is_premium()."""
    row = db.fetchone("SELECT expires_at, granted_at FROM premium_users WHERE user_id=?",
                      (user_id,)) if user_id else None
    if not row:
        d = describe_deadline("__none__")
        d.update(unreadable=False, has_row=False, granted_at=None, raw=None)
        return d
    d = describe_deadline(row["expires_at"])
    d["has_row"] = True
    d["granted_at"] = row["granted_at"]
    return d


def is_premium(user_id: int) -> bool:
    row = db.fetchone("SELECT expires_at FROM premium_users WHERE user_id=?", (user_id,))
    if not row:
        return False
    return deadline_active(row["expires_at"])


def grant_premium(user_id: int, days: int, admin_tg_id: int) -> None:
    """days=0 -> бессрочно."""
    expires = None
    if days and days > 0:
        expires = (datetime.utcnow() + timedelta(days=days)).isoformat(timespec="seconds")
    existing = db.fetchone("SELECT id FROM premium_users WHERE user_id=?", (user_id,))
    if existing:
        db.execute(
            "UPDATE premium_users SET expires_at=?, granted_at=?, granted_by_admin=?, "
            "notified_expired=0 WHERE user_id=?",
            (expires, now_iso(), admin_tg_id, user_id),
        )
    else:
        db.execute(
            "INSERT INTO premium_users (user_id, expires_at, granted_by_admin, notified_expired) "
            "VALUES (?,?,?,0)",
            (user_id, expires, admin_tg_id),
        )


def revoke_premium(user_id: int) -> None:
    db.execute("DELETE FROM premium_users WHERE user_id=?", (user_id,))


def get_premium_info(user_id: int) -> Optional[dict]:
    row = db.fetchone("SELECT * FROM premium_users WHERE user_id=?", (user_id,))
    if not row:
        return None
    return dict(row)


# === Платный доступ ===
def has_paid_access(user_id: int, test_id: Optional[int] = None,
                    note_id: Optional[int] = None) -> bool:
    if is_premium(user_id):
        return True
    if test_id:
        row = db.fetchone(
            "SELECT 1 FROM paid_access WHERE user_id=? AND test_id=?",
            (user_id, test_id),
        )
        if row:
            return True
        # Покупка за звёзды (по tg_id)
        try:
            u = db.fetchone("SELECT tg_id FROM users WHERE id=?", (user_id,))
            if u and u.get('tg_id'):
                from services import payment_service as _ps
                if _ps.has_paid_access(test_id, u['tg_id']):
                    return True
        except Exception:
            pass
        return False
    elif note_id:
        row = db.fetchone(
            "SELECT 1 FROM paid_access WHERE user_id=? AND note_id=?",
            (user_id, note_id),
        )
    else:
        return False
    return bool(row)


def grant_paid_access(user_id: int, granted_by: int,
                      test_id: Optional[int] = None,
                      note_id: Optional[int] = None) -> None:
    try:
        db.execute(
            "INSERT INTO paid_access (user_id, test_id, note_id, granted_by) VALUES (?,?,?,?)",
            (user_id, test_id, note_id, granted_by),
        )
    except Exception as e:
        # Уже есть запись - игнорируем
        logger.debug("paid_access insert skipped: %s", e)


# === Форматирование ===
def percent_to_level(percent: float, lang: str) -> str:
    """Текстовая оценка уровня по проценту."""
    from locales import t
    if percent < 40:
        return t("level_low", lang)
    if percent < 65:
        return t("level_mid", lang)
    if percent < 85:
        return t("level_high", lang)
    return t("level_top", lang)


def escape_html(text: str) -> str:
    """Безопасный текст для parse_mode=HTML."""
    if not text:
        return ""
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))


# === Парсер текстовых вопросов ===
_OPTION_RE = re.compile(
    r"^\s*([A-Za-zА-Яа-я])\s*[\)\.\-:]\s*(.+?)\s*$"
)
_LETTERS_LATIN = {"A", "B", "C", "D", "E", "F", "G", "H", "I", "J"}
_LETTERS_CYR = {"А", "Б", "В", "Г", "Д", "Е", "Ж", "З", "И", "К"}


def parse_questions_text(raw: str) -> tuple[list[dict], list[str]]:
    """
    Парсит блок текста с одним или несколькими вопросами.

    Формат каждого вопроса:
        Текст вопроса
        A) вариант
        B) вариант
        C) вариант
        D) вариант *   <- правильный

    Возвращает (questions, errors), где
      questions: список {'text': str, 'options': [str,...],
                         'correct_index': int,          # первый правильный
                         'correct_indexes': [int, ...]} # все правильные

    Сколько вариантов помечено звёздочкой, столько правильных и сохраняем.
    Одна звёздочка — обычный вопрос, несколько — вопрос с несколькими
    правильными ответами. Отдельно указывать тип не нужно.
      errors: список текстов ошибок.

    Поддерживает латиницу A-J и кириллицу А-К.
    Метка правильного ответа - '*' в конце строки варианта.
    """
    questions: list[dict] = []
    errors: list[str] = []

    # Делим на блоки по пустой строке
    lines = raw.replace("\r\n", "\n").split("\n")
    blocks: list[list[str]] = []
    current: list[str] = []
    for ln in lines:
        if ln.strip() == "":
            if current:
                blocks.append(current)
                current = []
        else:
            current.append(ln)
    if current:
        blocks.append(current)

    for bi, block in enumerate(blocks, start=1):
        if len(block) < 3:
            # Меньше 3 строк - нет минимум 2 вариантов
            errors.append(f"Блок {bi}: слишком мало строк (нужен текст + минимум 2 варианта).")
            continue

        # Разделяем: первые строки до первого варианта - это текст вопроса
        question_lines: list[str] = []
        option_lines: list[tuple[str, str, bool]] = []  # (letter, text, is_correct)

        idx = 0
        # Текст вопроса - всё до первого распознанного варианта
        while idx < len(block):
            line = block[idx]
            m = _OPTION_RE.match(line)
            if m:
                letter = m.group(1).upper()
                if letter in _LETTERS_LATIN or letter in _LETTERS_CYR:
                    break
            question_lines.append(line)
            idx += 1

        if not question_lines:
            errors.append(f"Блок {bi}: не найден текст вопроса.")
            continue

        # Остальное - варианты
        correct_count = 0
        while idx < len(block):
            line = block[idx]
            idx += 1
            m = _OPTION_RE.match(line)
            if not m:
                # Прилепляем к предыдущему варианту как продолжение
                if option_lines:
                    letter, txt, is_c = option_lines[-1]
                    option_lines[-1] = (letter, txt + " " + line.strip(), is_c)
                else:
                    question_lines.append(line)
                continue
            letter = m.group(1).upper()
            text = m.group(2).strip()
            # Проверяем метку правильного
            is_correct = False
            if text.endswith("*"):
                is_correct = True
                text = text[:-1].rstrip()
                correct_count += 1
            option_lines.append((letter, text, is_correct))

        if len(option_lines) < 2:
            errors.append(f"Блок {bi}: меньше 2 вариантов ответа.")
            continue
        if len(option_lines) > 10:
            errors.append(f"Блок {bi}: больше 10 вариантов ответа.")
            continue
        if correct_count == 0:
            errors.append(f"Блок {bi}: не указан правильный ответ (символ *).")
            continue
        # Несколько звёздочек — это нормально: вопрос просто с несколькими
        # правильными ответами. Тип определяем сами, админу выбирать не нужно.

        question_text = "\n".join(question_lines).strip()
        # Удаляем двоеточие/знак вопроса в конце если есть - оставляем как есть
        if not question_text:
            errors.append(f"Блок {bi}: пустой текст вопроса.")
            continue

        correct_indexes = [i for i, (_, _, c) in enumerate(option_lines) if c]
        correct_index = correct_indexes[0]     # для старого кода, ждущего одно число
        questions.append({
            "text": question_text,
            "options": [opt[1] for opt in option_lines],
            "correct_index": correct_index,
            "correct_indexes": correct_indexes,
        })

    return questions, errors


# === Форматирование вопроса для отправки ===
def build_question_text(qnum: int, total: int, question_text: str,
                        time_sec: int, lang: str) -> str:
    """Формирует текст сообщения с вопросом."""
    from locales import t
    progress = t("question_progress", lang, n=qnum, total=total, sec=time_sec)
    return f"{progress}\n\n{escape_html(question_text)}"


# === Безопасное обновление сообщений ===
async def safe_edit_or_send(call, text, reply_markup=None, parse_mode="HTML"):
    """
    Безопасное обновление callback-сообщения.
    Если edit_text падает (старое сообщение, удалено, нет изменений) —
    отправляет новое и снимает «загрузку».
    """
    try:
        await call.message.edit_text(
            text, reply_markup=reply_markup,
            parse_mode=parse_mode, disable_web_page_preview=True)
    except Exception:
        try:
            await call.message.answer(
                text, reply_markup=reply_markup,
                parse_mode=parse_mode, disable_web_page_preview=True)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("safe_edit_or_send failed: %s", e)
    # Всегда закрываем «загрузку»
    try:
        await call.answer()
    except Exception:
        pass
