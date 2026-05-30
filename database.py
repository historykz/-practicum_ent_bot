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


class _RowDict(dict):
    """Словарь с защитой от KeyError — для совместимости с .get() и [key]."""
    pass


def _row_to_dict(row) -> Optional[_RowDict]:
    """Конвертирует sqlite3.Row в dict-подобный объект."""
    if row is None:
        return None
    if isinstance(row, dict):
        return _RowDict(row)
    try:
        return _RowDict({k: row[k] for k in row.keys()})
    except Exception:
        return _RowDict(dict(row))


def fetchone(sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
    """Вернуть одну строку как dict (с поддержкой .get()) или None."""
    with db_lock() as conn:
        cur = conn.execute(sql, params)
        row = cur.fetchone()
        return _row_to_dict(row)


def fetchall(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    """Вернуть все строки как dict-объекты."""
    with db_lock() as conn:
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        return [_row_to_dict(r) for r in rows if r is not None]


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
            granted_by_admin INTEGER,
            notified_expired INTEGER DEFAULT 0
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

        # --- (group_quizzes определена ниже в актуальной версии) ---

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
            verified INTEGER DEFAULT 0,
            verified_at TEXT,
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

        # --- ГРУППЫ, ГДЕ БОТ ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS known_groups (
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            type TEXT,                 -- group/supergroup/channel
            added_by INTEGER,          -- tg_id того, кто добавил
            is_bot_admin INTEGER DEFAULT 0,
            seen_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # --- ГРУППОВЫЕ ТЕСТЫ (live-сессии) ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS group_quizzes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            test_id INTEGER NOT NULL,
            started_by INTEGER NOT NULL,         -- tg_id админа, запустившего
            status TEXT DEFAULT 'lobby',         -- lobby, running, finished, cancelled
            lobby_message_id INTEGER,
            current_question_index INTEGER DEFAULT 0,
            current_poll_id TEXT,
            current_poll_message_id INTEGER,
            current_poll_correct_index INTEGER,
            current_poll_options TEXT,           -- json
            current_question_started_at TEXT,
            language TEXT DEFAULT 'ru',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            started_at TEXT,
            finished_at TEXT
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_gq_chat ON group_quizzes(chat_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_gq_status ON group_quizzes(status);")

        # === МИГРАЦИИ для старых БД (раньше была другая схема group_quizzes) ===
        for alter in [
            "ALTER TABLE group_quizzes ADD COLUMN started_by INTEGER",
            "ALTER TABLE group_quizzes ADD COLUMN lobby_message_id INTEGER",
            "ALTER TABLE group_quizzes ADD COLUMN current_poll_id TEXT",
            "ALTER TABLE group_quizzes ADD COLUMN current_poll_message_id INTEGER",
            "ALTER TABLE group_quizzes ADD COLUMN current_poll_correct_index INTEGER",
            "ALTER TABLE group_quizzes ADD COLUMN current_poll_options TEXT",
            "ALTER TABLE group_quizzes ADD COLUMN current_question_started_at TEXT",
            "ALTER TABLE group_quizzes ADD COLUMN started_at TEXT",
        ]:
            try:
                cur.execute(alter)
            except Exception:
                pass

        # Если БД старая — там было started_by_user_id; копируем в started_by
        try:
            cur.execute(
                "UPDATE group_quizzes SET started_by = started_by_user_id "
                "WHERE started_by IS NULL AND started_by_user_id IS NOT NULL")
        except Exception:
            pass

        # Проверяем есть ли старое поле started_by_user_id с NOT NULL — пересоздаём таблицу
        try:
            cols = cur.execute("PRAGMA table_info(group_quizzes)").fetchall()
            col_names = [c[1] for c in cols]
            has_legacy = 'started_by_user_id' in col_names
            if has_legacy:
                logger.info("Обнаружена старая схема group_quizzes, пересоздаём таблицу...")
                # Сохраняем данные
                cur.execute("ALTER TABLE group_quizzes RENAME TO group_quizzes_old")
                # Создаём новую таблицу с правильной схемой
                cur.execute("""
                    CREATE TABLE group_quizzes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id INTEGER NOT NULL,
                        test_id INTEGER NOT NULL,
                        started_by INTEGER NOT NULL,
                        status TEXT DEFAULT 'lobby',
                        lobby_message_id INTEGER,
                        current_question_index INTEGER DEFAULT 0,
                        current_poll_id TEXT,
                        current_poll_message_id INTEGER,
                        current_poll_correct_index INTEGER,
                        current_poll_options TEXT,
                        current_question_started_at TEXT,
                        language TEXT DEFAULT 'ru',
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                        started_at TEXT,
                        finished_at TEXT
                    )""")
                # Переносим данные
                cur.execute("""
                    INSERT INTO group_quizzes
                        (id, chat_id, test_id, started_by, status,
                         current_question_index, language, finished_at, created_at)
                    SELECT id, chat_id, test_id,
                           COALESCE(started_by, started_by_user_id) AS started_by,
                           status, current_question_index, language, finished_at, created_at
                    FROM group_quizzes_old
                """)
                cur.execute("DROP TABLE group_quizzes_old")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_gq_chat ON group_quizzes(chat_id);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_gq_status ON group_quizzes(status);")
                logger.info("group_quizzes успешно пересоздана с актуальной схемой")
        except Exception as e:
            logger.warning("Миграция group_quizzes провалилась: %s", e)

        # --- УЧАСТНИКИ ГРУППОВОГО ТЕСТА ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS group_quiz_players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_quiz_id INTEGER NOT NULL,
            tg_id INTEGER NOT NULL,
            username TEXT,
            full_name TEXT,
            correct_answers INTEGER DEFAULT 0,
            wrong_answers INTEGER DEFAULT 0,
            skipped_answers INTEGER DEFAULT 0,
            total_time_seconds INTEGER DEFAULT 0,
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(group_quiz_id, tg_id)
        );
        """)

        # --- СТАТИСТИКА ПРОХОЖДЕНИЙ ТЕСТА (для лидерборда) ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS test_statistics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            tg_id INTEGER,
            username TEXT,
            full_name TEXT,
            score INTEGER DEFAULT 0,
            total_questions INTEGER DEFAULT 0,
            correct_answers INTEGER DEFAULT 0,
            wrong_answers INTEGER DEFAULT 0,
            skipped_answers INTEGER DEFAULT 0,
            percentage REAL DEFAULT 0,
            total_time_seconds INTEGER DEFAULT 0,
            average_answer_time REAL DEFAULT 0,
            source_type TEXT DEFAULT 'private', -- private / group
            group_chat_id INTEGER,
            group_quiz_id INTEGER,
            started_at TEXT,
            finished_at TEXT,
            is_first_attempt INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ts_test ON test_statistics(test_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ts_user ON test_statistics(user_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ts_first ON test_statistics(test_id, is_first_attempt);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ts_score ON test_statistics(test_id, score DESC, total_time_seconds ASC);")

        # --- МИГРАЦИИ для существующих БД ---
        try:
            cur.execute("ALTER TABLE premium_users ADD COLUMN notified_expired INTEGER DEFAULT 0")
        except Exception:
            pass

        try:
            cur.execute("ALTER TABLE tests ADD COLUMN is_private INTEGER DEFAULT 0")
        except Exception:
            pass

        # Серийный номер вопроса Q-NNNN
        try:
            cur.execute("ALTER TABLE questions ADD COLUMN serial_no TEXT")
        except Exception:
            pass
        try:
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_q_serial ON questions(serial_no)")
        except Exception:
            pass

        # Предупреждения юзера + бан
        try:
            cur.execute("ALTER TABLE users ADD COLUMN appeal_warnings INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE users ADD COLUMN banned_until TEXT")
        except Exception:
            pass

        try:
            cur.execute("ALTER TABLE test_attempts ADD COLUMN paused_at TEXT")
        except Exception:
            pass

        # Таблица апелляций
        cur.execute("""
        CREATE TABLE IF NOT EXISTS appeals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id INTEGER NOT NULL,
            user_tg_id INTEGER NOT NULL,
            user_text TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            resolved_by INTEGER,
            resolved_at TEXT
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_appeals_status ON appeals(status);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_appeals_q ON appeals(question_id);")

        # Бэкфилл серийных номеров для существующих вопросов
        try:
            rows = cur.execute("SELECT id FROM questions WHERE serial_no IS NULL ORDER BY id").fetchall()
            for r in rows:
                qid = r[0] if isinstance(r, tuple) else r['id']
                serial = f"Q-{qid:04d}"
                cur.execute("UPDATE questions SET serial_no=? WHERE id=?", (serial, qid))
        except Exception:
            pass

        # Триггер: автоназначение serial_no новым вопросам
        try:
            cur.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_q_serial
                AFTER INSERT ON questions
                WHEN NEW.serial_no IS NULL
                BEGIN
                    UPDATE questions
                    SET serial_no = printf('Q-%04d', NEW.id)
                    WHERE id = NEW.id;
                END;
            """)
        except Exception:
            pass

        # --- ПРИВАТНЫЙ ДОСТУП К ТЕСТАМ ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS private_test_access (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_id INTEGER NOT NULL,
            user_tg_id INTEGER NOT NULL,
            granted_by INTEGER,
            granted_at TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT,
            notified_expired INTEGER DEFAULT 0,
            UNIQUE(test_id, user_tg_id)
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pta_user ON private_test_access(user_tg_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pta_test ON private_test_access(test_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pta_expires ON private_test_access(expires_at);")

        # Миграции для существующих БД
        try:
            cur.execute("ALTER TABLE private_test_access ADD COLUMN expires_at TEXT")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE private_test_access ADD COLUMN notified_expired INTEGER DEFAULT 0")
        except Exception:
            pass

        # --- КАТЕГОРИИ ТЕСТОВ (разделы каталога) ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS test_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            emoji TEXT DEFAULT '📚',
            sort_order INTEGER DEFAULT 0,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        try:
            cur.execute("ALTER TABLE tests ADD COLUMN category_id INTEGER")
        except Exception:
            pass

        # --- Обязательный предмет (виден всем) ---
        try:
            cur.execute("ALTER TABLE test_categories ADD COLUMN is_required INTEGER DEFAULT 0")
        except Exception:
            pass

        # --- Профильные предметы юзера (CSV из category_id) ---
        try:
            cur.execute("ALTER TABLE users ADD COLUMN profile_subjects TEXT")
        except Exception:
            pass

        # --- Флаг прохождения онбординга ---
        try:
            cur.execute("ALTER TABLE users ADD COLUMN onboarded_at TEXT")
        except Exception:
            pass

        logger.info("База данных инициализирована")
