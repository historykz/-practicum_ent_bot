"""
Модуль базы данных.
Использует sqlite3 без ORM.
Содержит инициализацию схемы и универсальные хелперы.

ВАЖНО:
- Соединение хранится одно на процесс (SQLite не очень любит много открытых соединений).
- check_same_thread=False, потому что aiogram использует asyncio (но мы оборачиваем вызовы в asyncio.to_thread).
- Включён WAL для лучшей производительности при параллельных чтениях.
"""
import sqlite3
import threading
import logging
from contextlib import contextmanager
from typing import Any, Iterable, Optional

from config import DB_PATH

logger = logging.getLogger(__name__)

# Глобальное соединение с локом для безопасной работы из разных потоков/корутин
_conn: Optional[sqlite3.Connection] = None
_lock = threading.RLock()


def get_conn() -> sqlite3.Connection:
    """Получить (или создать) глобальное соединение с БД."""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL;")
        _conn.execute("PRAGMA foreign_keys=ON;")
        _conn.execute("PRAGMA synchronous=NORMAL;")
    return _conn


@contextmanager
def db_lock():
    """Контекстный менеджер для блокировки соединения."""
    with _lock:
        yield get_conn()


def execute(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    """Выполнить SQL и вернуть курсор."""
    with db_lock() as conn:
        return conn.execute(sql, params)


def executemany(sql: str, seq: Iterable[Iterable[Any]]) -> sqlite3.Cursor:
    """Выполнить SQL для последовательности параметров."""
    with db_lock() as conn:
        return conn.executemany(sql, seq)


def fetchone(sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
    """Вернуть одну строку или None."""
    with db_lock() as conn:
        cur = conn.execute(sql, params)
        return cur.fetchone()


def fetchall(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    """Вернуть все строки."""
    with db_lock() as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall()


def init_db() -> None:
    """
    Инициализация всех таблиц.
    Безопасна для повторного запуска (CREATE IF NOT EXISTS).
    """
    logger.info("Инициализация базы данных %s", DB_PATH)
    with db_lock() as conn:
        cur = conn.cursor()

        # --- USERS ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            tg_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            language TEXT DEFAULT 'ru',
            school TEXT,
            city TEXT,
            invited_by INTEGER,
            current_streak INTEGER DEFAULT 0,
            best_streak INTEGER DEFAULT 0,
            last_daily_date TEXT,
            is_blocked INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # --- ADMINS (для тех, кто не в ADMIN_IDS, но получил права через бота) ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER UNIQUE NOT NULL,
            granted_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # --- TESTS ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            subject TEXT DEFAULT '',
            grade INTEGER DEFAULT 0,
            category TEXT DEFAULT '',
            language TEXT NOT NULL DEFAULT 'ru',
            test_type TEXT DEFAULT 'regular',  -- regular, mock, quiz, daily, duel, tournament, adaptive
            status TEXT DEFAULT 'active',       -- active, hidden, finished
            is_paid INTEGER DEFAULT 0,
            price INTEGER DEFAULT 0,
            attempts_limit INTEGER DEFAULT 0,    -- 0 = без лимита
            first_attempt_only INTEGER DEFAULT 1,
            deadline TEXT,
            shuffle_questions INTEGER DEFAULT 1,
            shuffle_options INTEGER DEFAULT 1,
            show_correct INTEGER DEFAULT 1,
            show_explanation INTEGER DEFAULT 1,
            time_per_question INTEGER DEFAULT 30,
            required_subscription INTEGER DEFAULT 0,
            required_channel TEXT,
            allow_in_group INTEGER DEFAULT 1,
            allow_duel INTEGER DEFAULT 0,
            allow_daily INTEGER DEFAULT 0,
            allow_tournament INTEGER DEFAULT 0,
            display_mode TEXT DEFAULT 'inline',  -- inline или poll
            created_by INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tests_lang ON tests(language);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tests_type ON tests(test_type);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tests_status ON tests(status);")

        # --- QUESTIONS ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            explanation TEXT DEFAULT '',
            score INTEGER DEFAULT 1,
            image_file_id TEXT,
            topic TEXT DEFAULT '',
            difficulty INTEGER DEFAULT 2,
            poll_id TEXT,
            source_type TEXT DEFAULT 'manual', -- manual, text_import, poll_import, poll_forwarded
            order_num INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (test_id) REFERENCES tests(id) ON DELETE CASCADE
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_questions_test ON questions(test_id);")

        # --- QUESTION OPTIONS ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS question_options (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            is_correct INTEGER DEFAULT 0,
            order_num INTEGER DEFAULT 0,
            FOREIGN KEY (question_id) REFERENCES questions(id) ON DELETE CASCADE
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_options_q ON question_options(question_id);")

        # --- TEST ATTEMPTS ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS test_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            test_id INTEGER NOT NULL,
            current_question_index INTEGER DEFAULT 0,
            question_order TEXT DEFAULT '',  -- JSON список id вопросов
            options_order TEXT DEFAULT '{}', -- JSON {qid: [option_ids]}
            correct_answers INTEGER DEFAULT 0,
            wrong_answers INTEGER DEFAULT 0,
            skipped_answers INTEGER DEFAULT 0,
            start_time TEXT,
            end_time TEXT,
            status TEXT DEFAULT 'in_progress',  -- in_progress, paused, finished, aborted
            missed_questions_counter INTEGER DEFAULT 0,
            pause_time TEXT,
            is_counted INTEGER DEFAULT 1,
            is_first_attempt INTEGER DEFAULT 1,
            attempt_num INTEGER DEFAULT 1,
            language TEXT DEFAULT 'ru',
            group_id INTEGER,
            started_by_user_id INTEGER,
            score INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (test_id) REFERENCES tests(id) ON DELETE CASCADE
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_attempts_user ON test_attempts(user_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_attempts_test ON test_attempts(test_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_attempts_status ON test_attempts(status);")

        # --- ATTEMPT ANSWERS ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS attempt_answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            selected_option_id INTEGER,
            is_correct INTEGER DEFAULT 0,
            response_time_ms INTEGER DEFAULT 0,
            skipped INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (attempt_id) REFERENCES test_attempts(id) ON DELETE CASCADE
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_answers_attempt ON attempt_answers(attempt_id);")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_answer_q ON attempt_answers(attempt_id, question_id);")

        # --- PAID ACCESS ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS paid_access (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            test_id INTEGER,
            note_id INTEGER,
            granted_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, test_id, note_id)
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_paid_user ON paid_access(user_id);")

        # --- PREMIUM ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS premium_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE NOT NULL,
            granted_at TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT,  -- NULL = бессрочно
            granted_by_admin INTEGER
        );
        """)

        # --- REQUIRED CHANNELS ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS required_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_username TEXT NOT NULL,
            title TEXT DEFAULT '',
            is_global INTEGER DEFAULT 0,
            test_id INTEGER,
            note_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # --- IMPORTED POLLS (для трекинга) ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS imported_polls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_id INTEGER NOT NULL,
            poll_id TEXT,
            question_text TEXT,
            raw_data TEXT,
            correct_option_id INTEGER,
            needs_manual_correct_answer INTEGER DEFAULT 0,
            imported_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # --- QUESTION DRAFTS (для poll без correct_option_id) ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS question_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_id INTEGER NOT NULL,
            source_type TEXT DEFAULT 'poll_forwarded',
            question_text TEXT NOT NULL,
            raw_options TEXT NOT NULL,  -- JSON список текстов вариантов
            status TEXT DEFAULT 'pending',  -- pending, completed
            draft_correct_option INTEGER,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # --- GROUP QUIZZES ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS group_quizzes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            test_id INTEGER NOT NULL,
            started_by_user_id INTEGER NOT NULL,
            status TEXT DEFAULT 'collecting',  -- collecting, running, paused, finished
            current_question_index INTEGER DEFAULT 0,
            question_order TEXT DEFAULT '',
            missed_questions_counter INTEGER DEFAULT 0,
            announce_message_id INTEGER,
            language TEXT DEFAULT 'ru',
            finished_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_groupq_chat ON group_quizzes(chat_id);")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS group_quiz_participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_quiz_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            score INTEGER DEFAULT 0,
            correct_count INTEGER DEFAULT 0,
            wrong_count INTEGER DEFAULT 0,
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(group_quiz_id, user_id),
            FOREIGN KEY (group_quiz_id) REFERENCES group_quizzes(id) ON DELETE CASCADE
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS group_quiz_answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_quiz_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            option_id INTEGER,
            is_correct INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(group_quiz_id, user_id, question_id)
        );
        """)

        # --- DAILY ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_date TEXT NOT NULL,
            language TEXT NOT NULL,
            subject TEXT DEFAULT '',
            category TEXT DEFAULT '',
            question_ids TEXT NOT NULL,  -- JSON
            mode TEXT DEFAULT 'random',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(task_date, language)
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            task_date TEXT NOT NULL,
            correct_answers INTEGER DEFAULT 0,
            wrong_answers INTEGER DEFAULT 0,
            skipped_answers INTEGER DEFAULT 0,
            percentage REAL DEFAULT 0,
            streak INTEGER DEFAULT 0,
            best_streak INTEGER DEFAULT 0,
            completed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, task_date)
        );
        """)

        # --- REFERRALS ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inviter_id INTEGER NOT NULL,
            invited_id INTEGER UNIQUE NOT NULL,
            bonus_granted TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # --- ACHIEVEMENTS ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_achievements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, code)
        );
        """)

        # --- TOURNAMENTS ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS tournaments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            test_id INTEGER NOT NULL,
            language TEXT DEFAULT 'ru',
            start_at TEXT,
            end_at TEXT,
            prize TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS tournament_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            score INTEGER DEFAULT 0,
            attempt_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(tournament_id, user_id)
        );
        """)

        # --- DUELS ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS duels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player1_id INTEGER NOT NULL,
            player2_id INTEGER NOT NULL,
            subject TEXT DEFAULT '',
            question_ids TEXT NOT NULL,
            status TEXT DEFAULT 'active',   -- active, finished, aborted
            winner_id INTEGER,
            score1 INTEGER DEFAULT 0,
            score2 INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            started_at TEXT,
            finished_at TEXT
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS duel_answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            duel_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            selected_option_id INTEGER,
            is_correct INTEGER DEFAULT 0,
            response_time_ms INTEGER DEFAULT 0,
            score INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # --- NOTES (конспекты) ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            subject TEXT DEFAULT '',
            category TEXT DEFAULT '',
            language TEXT NOT NULL DEFAULT 'ru',
            topic TEXT DEFAULT '',
            difficulty INTEGER DEFAULT 2,
            access_type TEXT DEFAULT 'free',  -- free, paid, premium
            price INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',     -- active, hidden
            created_by INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS note_pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id INTEGER NOT NULL,
            page_number INTEGER NOT NULL,
            content TEXT NOT NULL,
            image_file_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pages_note ON note_pages(note_id);")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS note_homeworks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id INTEGER UNIQUE NOT NULL,
            homework_type TEXT DEFAULT 'test',  -- test, open
            test_id INTEGER,
            open_task_prompt TEXT DEFAULT '',
            open_task_keywords TEXT DEFAULT '',  -- ключевые слова через запятую
            auto_check_enabled INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_notes_progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            note_id INTEGER NOT NULL,
            last_page INTEGER DEFAULT 0,
            completed INTEGER DEFAULT 0,
            homework_completed INTEGER DEFAULT 0,
            homework_score INTEGER,
            homework_answer TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, note_id)
        );
        """)

        # --- SETTINGS ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)

        logger.info("База данных инициализирована")
