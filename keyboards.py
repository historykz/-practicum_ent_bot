"""
InlineKeyboard - все клавиатуры бота.
"""
from typing import Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from locales import t


# ---------- Базовые ----------

def language_kb() -> InlineKeyboardMarkup:
    """Выбор языка при первом старте/смене."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🇰🇿 Қазақша", callback_data="setlang:kz")
    kb.button(text="🇷🇺 Русский", callback_data="setlang:ru")
    kb.adjust(2)
    return kb.as_markup()


def main_menu_kb(lang: str, is_admin: bool = False) -> InlineKeyboardMarkup:
    """Главное меню пользователя."""
    kb = InlineKeyboardBuilder()
    kb.button(text=t("btn_tests", lang), callback_data="m:tests")
    kb.button(text=t("btn_duel", lang), callback_data="m:duel")
    kb.button(text=t("btn_rating", lang), callback_data="m:rating")
    kb.button(text=t("btn_my_results", lang), callback_data="m:results")
    kb.button(text=t("btn_profile", lang), callback_data="m:profile")
    kb.button(text=t("btn_invite", lang), callback_data="m:invite")
    kb.button(text=t("btn_support", lang), callback_data="m:support")
    kb.button(text=t("btn_help", lang), callback_data="m:help")
    if is_admin:
        kb.button(text="🛠 Admin", callback_data="m:admin")
    kb.adjust(2)
    return kb.as_markup()


def back_kb(lang: str, callback: str = "m:menu") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("btn_back", lang), callback_data=callback)
    return kb.as_markup()


def yes_no_kb(prefix: str, lang: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("yes", lang), callback_data=f"{prefix}:1")
    kb.button(text=t("no", lang), callback_data=f"{prefix}:0")
    kb.adjust(2)
    return kb.as_markup()


def cancel_kb(lang: str, callback: str = "cancel") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("btn_cancel", lang), callback_data=callback)
    return kb.as_markup()


# ---------- Профиль ----------

def profile_kb(lang: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("btn_change_lang", lang), callback_data="profile:lang")
    kb.button(text=t("btn_back", lang), callback_data="m:menu")
    kb.adjust(1)
    return kb.as_markup()


# ---------- Каталог тестов / заметок ----------

def tests_list_kb(tests: list[dict], lang: str, page: int = 0,
                  per_page: int = 8) -> InlineKeyboardMarkup:
    """Список тестов с пагинацией."""
    kb = InlineKeyboardBuilder()
    start = page * per_page
    chunk = tests[start:start + per_page]
    for tst in chunk:
        label = f"{tst['title']} ({tst['language'].upper()})"
        kb.button(text=label, callback_data=f"test:{tst['id']}")
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"tests_page:{page-1}"))
    if start + per_page < len(tests):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"tests_page:{page+1}"))
    kb.adjust(1)
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="m:menu"))
    return kb.as_markup()


def test_card_kb(test_id: int, lang: str, allow_group: bool = True,
                 has_access: bool = True) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if has_access:
        kb.button(text=t("btn_start_test", lang), callback_data=f"run:{test_id}")
    if allow_group:
        kb.button(text=t("btn_start_in_group", lang),
                  callback_data=f"share_group:{test_id}")
    kb.button(text=t("btn_share_test", lang), callback_data=f"share:{test_id}")
    kb.button(text=t("btn_back", lang), callback_data="m:tests")
    kb.adjust(1)
    return kb.as_markup()


def paid_test_kb(test_id: int, lang: str, manager: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("btn_write_manager", lang), url=f"https://t.me/{manager}")
    kb.button(text=t("btn_check_access", lang), callback_data=f"checkacc:{test_id}")
    kb.button(text=t("btn_back", lang), callback_data="m:tests")
    kb.adjust(1)
    return kb.as_markup()


def paid_note_kb(note_id: int, lang: str, manager: str, is_premium: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if is_premium:
        kb.button(text=t("btn_buy_premium", lang), url=f"https://t.me/{manager}")
    kb.button(text=t("btn_write_manager", lang), url=f"https://t.me/{manager}")
    kb.button(text=t("btn_check_access", lang), callback_data=f"checknote:{note_id}")
    kb.button(text=t("btn_back", lang), callback_data="m:notes")
    kb.adjust(1)
    return kb.as_markup()


# ---------- Подписка на канал ----------

def subscription_kb(channel: str, lang: str, action_callback: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    # Канал может быть с @ или без
    ch = channel.lstrip("@")
    kb.button(text=t("btn_subscribe", lang), url=f"https://t.me/{ch}")
    kb.button(text=t("btn_check_sub", lang), callback_data=action_callback)
    kb.adjust(1)
    return kb.as_markup()


# ---------- Вопрос с вариантами ----------

def options_kb(attempt_id: int, question_id: int, options: list[dict]) -> InlineKeyboardMarkup:
    """Кнопки вариантов ответа для одного вопроса теста."""
    kb = InlineKeyboardBuilder()
    for i, opt in enumerate(options):
        # callback хранит attempt_id и option_id (НЕ is_correct, чтобы не дать подсмотреть)
        kb.button(
            text=f"{chr(ord('A') + i)}) {opt['text'][:60]}",
            callback_data=f"ans:{attempt_id}:{question_id}:{opt['id']}",
        )
    # Кнопка СТОП — отдельной строкой
    kb.button(text="🛑 СТОП", callback_data=f"abort:{attempt_id}")
    kb.adjust(1)
    return kb.as_markup()


def group_options_kb(group_quiz_id: int, question_id: int,
                     options: list[dict]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for i, opt in enumerate(options):
        kb.button(
            text=f"{chr(ord('A') + i)}) {opt['text'][:60]}",
            callback_data=f"gans:{group_quiz_id}:{question_id}:{opt['id']}",
        )
    kb.adjust(1)
    return kb.as_markup()


def duel_options_kb(duel_id: int, question_id: int, options: list[dict]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for i, opt in enumerate(options):
        kb.button(
            text=f"{chr(ord('A') + i)}) {opt['text'][:60]}",
            callback_data=f"duelans:{duel_id}:{question_id}:{opt['id']}",
        )
    kb.adjust(1)
    return kb.as_markup()


# ---------- Пауза ----------

def pause_personal_kb(attempt_id: int, lang: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("btn_continue_test", lang), callback_data=f"resume:{attempt_id}")
    kb.button(text=t("btn_finish_test", lang), callback_data=f"abort:{attempt_id}")
    kb.adjust(1)
    return kb.as_markup()


def pause_group_kb(group_quiz_id: int, lang: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("btn_continue_quiz", lang), callback_data=f"gresume:{group_quiz_id}")
    kb.button(text=t("btn_finish_quiz", lang), callback_data=f"gabort:{group_quiz_id}")
    kb.adjust(1)
    return kb.as_markup()


# ---------- Группа: набор участников ----------

def group_join_kb(group_quiz_id: int, lang: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("btn_join_quiz", lang), callback_data=f"gjoin:{group_quiz_id}")
    kb.button(text=t("btn_start_quiz_now", lang), callback_data=f"gstart:{group_quiz_id}")
    kb.adjust(1)
    return kb.as_markup()


# ---------- Daily ----------

def daily_kb(lang: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("btn_start_daily", lang), callback_data="daily:start")
    kb.button(text=t("btn_daily_streak", lang), callback_data="daily:streak")
    kb.button(text=t("btn_daily_rating", lang), callback_data="daily:rating")
    kb.button(text=t("btn_back", lang), callback_data="m:menu")
    kb.adjust(1)
    return kb.as_markup()


# ---------- Дуэль ----------

def duel_menu_kb(lang: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("btn_duel_fast", lang), callback_data="duel:fast")
    kb.button(text=t("btn_duel_subject", lang), callback_data="duel:subject")
    kb.button(text=t("btn_duel_history", lang), callback_data="duel:history")
    kb.button(text=t("btn_back", lang), callback_data="m:menu")
    kb.adjust(1)
    return kb.as_markup()


def duel_cancel_kb(lang: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("btn_cancel", lang), callback_data="duel:cancel")
    return kb.as_markup()


# ---------- Рейтинг ----------

def rating_menu_kb(lang: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("btn_rating_overall", lang), callback_data="rating:overall")
    kb.button(text=t("btn_rating_week", lang), callback_data="rating:week")
    kb.button(text=t("btn_back", lang), callback_data="m:menu")
    kb.adjust(1)
    return kb.as_markup()


# ---------- Заметки ----------

def notes_list_kb(notes: list[dict], lang: str, page: int = 0,
                  per_page: int = 8) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    start = page * per_page
    chunk = notes[start:start + per_page]
    for n in chunk:
        kb.button(text=n["title"], callback_data=f"note:{n['id']}")
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"notes_page:{page-1}"))
    if start + per_page < len(notes):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"notes_page:{page+1}"))
    kb.adjust(1)
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="m:menu"))
    return kb.as_markup()


def note_card_kb(note_id: int, lang: str, has_homework: bool,
                 has_access: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if has_access:
        kb.button(text=t("btn_read_note", lang), callback_data=f"noteread:{note_id}:0")
    if has_homework:
        kb.button(text=t("btn_start_hw", lang), callback_data=f"hw:{note_id}")
    kb.button(text=t("btn_back", lang), callback_data="m:notes")
    kb.adjust(1)
    return kb.as_markup()


def note_page_kb(note_id: int, page_idx: int, total_pages: int,
                 lang: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    nav: list[InlineKeyboardButton] = []
    if page_idx > 0:
        nav.append(InlineKeyboardButton(
            text=t("btn_prev_page", lang),
            callback_data=f"noteread:{note_id}:{page_idx-1}",
        ))
    if page_idx + 1 < total_pages:
        nav.append(InlineKeyboardButton(
            text=t("btn_next_page", lang),
            callback_data=f"noteread:{note_id}:{page_idx+1}",
        ))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text=t("btn_back", lang),
                                callback_data=f"note:{note_id}"))
    return kb.as_markup()


# ---------- Языки для админа ----------

def admin_lang_kb(prefix: str = "newtest_lang") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🇰🇿 Қазақша", callback_data=f"{prefix}:kz")
    kb.button(text="🇷🇺 Русский", callback_data=f"{prefix}:ru")
    kb.adjust(2)
    return kb.as_markup()


def test_type_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for code, name in [
        ("regular", "Обычный тест"),
        ("mock", "Пробник"),
        ("quiz", "Викторина"),
        ("daily", "Daily"),
        ("duel", "Дуэльный"),
        ("tournament", "Турнирный"),
        ("adaptive", "Адаптивный"),
    ]:
        kb.button(text=name, callback_data=f"newtest_type:{code}")
    kb.adjust(1)
    return kb.as_markup()


# ---------- Админ-меню ----------

def admin_menu_kb(lang: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("btn_admin_create_test", lang), callback_data="adm:create_test")
    kb.button(text=t("btn_admin_my_tests", lang), callback_data="adm:my_tests")
    kb.button(text="📂 Разделы", callback_data="adm:categories")
    kb.button(text=t("btn_admin_import_text", lang), callback_data="adm:import_text")
    kb.button(text=t("btn_admin_premium", lang), callback_data="adm:premium")
    kb.button(text=t("btn_admin_block", lang), callback_data="adm:block")
    kb.button(text=t("btn_admin_channels", lang), callback_data="adm:channels")
    kb.button(text=t("btn_admin_stats", lang), callback_data="adm:stats")
    kb.button(text=t("btn_admin_export", lang), callback_data="adm:export")
    kb.button(text="🛠 Управление админами", callback_data="adm:admins")
    kb.button(text=t("btn_back", lang), callback_data="m:menu")
    kb.adjust(2)
    return kb.as_markup()


def admin_tests_list_kb(tests: list[dict], lang: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for tst in tests[:30]:
        kb.button(text=f"{tst['id']}. {tst['title']}",
                  callback_data=f"admtest:{tst['id']}")
    kb.button(text=t("btn_back", lang), callback_data="m:admin")
    kb.adjust(1)
    return kb.as_markup()


def admin_test_actions_kb(test_id: int, lang: str, is_private: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Импорт текстом", callback_data=f"admimport_text:{test_id}")
    kb.button(text="📥 Импорт Quiz Poll", callback_data=f"admimport_poll:{test_id}")
    kb.button(text="📋 Черновики", callback_data=f"admdrafts:{test_id}")
    kb.button(text="❓ Вопросы", callback_data=f"admquestions:{test_id}")
    if is_private:
        kb.button(text="🔓 Снять приватный режим", callback_data=f"admpriv:{test_id}:0")
    else:
        kb.button(text="🔐 Сделать приватным", callback_data=f"admpriv:{test_id}:1")
    kb.button(text="🗑 Удалить тест", callback_data=f"admdel:{test_id}")
    kb.button(text=t("btn_back", lang), callback_data="adm:my_tests")
    kb.adjust(1)
    return kb.as_markup()


def import_done_kb(lang: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("btn_import_done", lang), callback_data="import:done")
    kb.button(text=t("btn_cancel", lang), callback_data="cancel")
    kb.adjust(1)
    return kb.as_markup()


def draft_fix_kb(draft_id: int, option_count: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for i in range(option_count):
        kb.button(text=f"{chr(ord('A') + i)}", callback_data=f"draftfix:{draft_id}:{i}")
    kb.button(text="🗑 Удалить черновик", callback_data=f"draftdel:{draft_id}")
    kb.adjust(min(option_count, 4))
    return kb.as_markup()


def note_access_kb(lang: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🆓 Бесплатно", callback_data="newnote_access:free")
    kb.button(text="💰 Платно", callback_data="newnote_access:paid")
    kb.button(text="👑 Premium", callback_data="newnote_access:premium")
    kb.adjust(1)
    return kb.as_markup()


def note_pages_done_kb(lang: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("btn_import_done", lang), callback_data="newnote_done")
    kb.button(text=t("btn_cancel", lang), callback_data="cancel")
    kb.adjust(1)
    return kb.as_markup()
