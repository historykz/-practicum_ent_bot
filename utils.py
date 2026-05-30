"""
Утилиты: работа с пользователями, проверки прав, форматирование.
"""
import logging
import re
from datetime import datetime, timedelta, date
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
    """Получить пользователя по tg_id, создать если не существует."""
    row = db.fetchone("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
    if row:
        # Обновляем username/name если изменились
        if (username and row["username"] != username) or \
           (first_name and row["first_name"] != first_name):
            db.execute(
                "UPDATE users SET username=?, first_name=?, last_name=?, updated_at=? WHERE tg_id=?",
                (username, first_name, last_name, now_iso(), tg_id),
            )
        return dict(row)
    db.execute(
        "INSERT INTO users (tg_id, username, first_name, last_name) VALUES (?,?,?,?)",
        (tg_id, username, first_name, last_name),
    )
    row = db.fetchone("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
    return dict(row)


def get_user_by_tg(tg_id: int) -> Optional[dict]:
    row = db.fetchone("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[dict]:
    row = db.fetchone("SELECT * FROM users WHERE id = ?", (user_id,))
    return dict(row) if row else None


def find_user_by_arg(arg: str) -> Optional[dict]:
    """Найти пользователя по '@username' или числовому tg_id."""
    arg = arg.strip()
    if not arg:
        return None
    if arg.startswith("@"):
        uname = arg[1:]
        row = db.fetchone("SELECT * FROM users WHERE username = ?", (uname,))
        return dict(row) if row else None
    if arg.isdigit():
        return get_user_by_tg(int(arg))
    # Возможно, username без @
    row = db.fetchone("SELECT * FROM users WHERE username = ?", (arg,))
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


def is_blocked(tg_id: int) -> bool:
    row = db.fetchone("SELECT is_blocked FROM users WHERE tg_id=?", (tg_id,))
    return bool(row and row["is_blocked"])


def get_profile_subjects(tg_id: int) -> list[int]:
    """Вернуть список category_id профильных предметов юзера."""
    row = db.fetchone("SELECT profile_subjects FROM users WHERE tg_id=?", (tg_id,))
    if not row or not row.get('profile_subjects'):
        return []
    try:
        return [int(x) for x in str(row['profile_subjects']).split(',')
                if x.strip().isdigit()]
    except Exception:
        return []


def set_profile_subjects(tg_id: int, category_ids: list[int]) -> None:
    """Сохранить профильные предметы юзера (список category_id)."""
    csv = ",".join(str(int(x)) for x in category_ids)
    db.execute("UPDATE users SET profile_subjects=? WHERE tg_id=?",
                (csv, tg_id))


def has_profile_subjects(tg_id: int) -> bool:
    return len(get_profile_subjects(tg_id)) > 0


def set_blocked(user_id: int, blocked: bool) -> None:
    db.execute("UPDATE users SET is_blocked=? WHERE id=?", (1 if blocked else 0, user_id))


# === Premium ===
def is_premium(user_id: int) -> bool:
    row = db.fetchone("SELECT expires_at FROM premium_users WHERE user_id=?", (user_id,))
    if not row:
        return False
    exp = row["expires_at"]
    if not exp:
        return True
    try:
        return datetime.fromisoformat(exp) > datetime.utcnow()
    except ValueError:
        return False


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
      questions: список {'text': str, 'options': [str,...], 'correct_index': int}
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
        if correct_count > 1:
            errors.append(f"Блок {bi}: указано несколько правильных ответов.")
            continue

        question_text = "\n".join(question_lines).strip()
        # Удаляем двоеточие/знак вопроса в конце если есть - оставляем как есть
        if not question_text:
            errors.append(f"Блок {bi}: пустой текст вопроса.")
            continue

        correct_index = next(i for i, (_, _, c) in enumerate(option_lines) if c)
        questions.append({
            "text": question_text,
            "options": [opt[1] for opt in option_lines],
            "correct_index": correct_index,
        })

    return questions, errors


# === Форматирование вопроса для отправки ===
def build_question_text(qnum: int, total: int, question_text: str,
                        time_sec: int, lang: str) -> str:
    """Формирует текст сообщения с вопросом."""
    from locales import t
    progress = t("question_progress", lang, n=qnum, total=total)
    time_label = t("time_left", lang, sec=time_sec)
    return f"<b>{progress}</b>\n⏱ {time_sec} сек\n\n{escape_html(question_text)}"


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
