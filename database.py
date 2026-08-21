"""
Модуль базы данных.
Использует sqlite3 без ORM.
Содержит инициализацию схемы и универсальные хелперы.

ВАЖНО:
- Соединение хранится одно на процесс (SQLite не очень любит много открытых соединений).
- check_same_thread=False, потому что aiogram использует asyncio (но мы оборачиваем вызовы в asyncio.to_thread).
- Включён WAL для лучшей производительности при параллельных чтениях.
"""
import os
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
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None,
                                 timeout=30)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL;")
        _conn.execute("PRAGMA foreign_keys=ON;")
        _conn.execute("PRAGMA synchronous=NORMAL;")
        _conn.execute("PRAGMA busy_timeout=30000;")
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


def snapshot_to(dest_path: str) -> None:
    """Быстрый консистентный снимок БД в файл (WAL-checkpoint + копия файла).
    Держит лок доли секунды — не вешает другие запросы (в отличие от
    поблочного sqlite backup, из-за которого возникал 502)."""
    import shutil
    with _lock:
        conn = get_conn()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        shutil.copy2(DB_PATH, dest_path)


def replace_database(new_db_path: str) -> None:
    """Атомарно заменить рабочую БД содержимым new_db_path. Быстро (копия
    файла под локом), без поблочного копирования — поэтому без зависаний/502."""
    global _conn
    import shutil
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
        for suffix in ("-wal", "-shm"):
            try:
                os.remove(DB_PATH + suffix)
            except OSError:
                pass
        shutil.copy2(new_db_path, DB_PATH)
        get_conn()  # переоткрыть соединение на новой БД


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
        # Обязательная подписка на канал по разделу (category_id) + флаг активности
        try:
            cur.execute("ALTER TABLE required_channels ADD COLUMN category_id INTEGER")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE required_channels ADD COLUMN is_active INTEGER DEFAULT 1")
        except Exception:
            pass
        # --- Планировщик автозапуска тестов по расписанию ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS auto_schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,           -- куда публиковать
                category_id INTEGER,             -- раздел (NULL = все)
                start_date TEXT NOT NULL,        -- дата начала YYYY-MM-DD
                end_date TEXT NOT NULL,          -- дата окончания
                daily_time TEXT NOT NULL,        -- время запуска HH:MM (Астана)
                tests_per_day INTEGER DEFAULT 1, -- сколько тестов в день
                allow_paid INTEGER DEFAULT 0,    -- разрешить платные
                allow_private INTEGER DEFAULT 0, -- разрешить приватные
                status TEXT DEFAULT 'active',    -- active | stopped | finished
                last_run_date TEXT,              -- когда последний раз запускал
                bot_username TEXT,
                created_by INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Лог запусков тестов планировщиком (для умного выбора + статистики)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS auto_schedule_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_id INTEGER NOT NULL,
                test_id INTEGER NOT NULL,
                run_date TEXT,                   -- когда запущен
                participants INTEGER DEFAULT 0,  -- сколько участвовало
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sched_runs "
                    "ON auto_schedule_runs(schedule_id, test_id)")
        # Участники чата за период (для статистики активности)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                user_tg_id INTEGER NOT NULL,
                joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, user_tg_id)
            )
        """)
        # Канал для анонса (отдельно от чата) + задержка перед стартом
        try:
            cur.execute("ALTER TABLE auto_schedule ADD COLUMN channel_id TEXT")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE auto_schedule ADD COLUMN announce_delay INTEGER DEFAULT 60")
        except Exception:
            pass

        # --- Реферальная программа: кого пригласил (сохраняем ВСЕХ, даже отписавшихся) ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS referral_invites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inviter_tg_id INTEGER NOT NULL,
                invited_tg_id INTEGER NOT NULL,
                subscribed INTEGER DEFAULT 0,     -- подписался ли на канал
                counted INTEGER DEFAULT 0,        -- засчитан ли (подписан + уникален)
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(inviter_tg_id, invited_tg_id)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_refinv_inviter "
                    "ON referral_invites(inviter_tg_id)")
        # Награды за рефералов (сколько раз уже выдавали премиум за 10 друзей)
        try:
            cur.execute("ALTER TABLE users ADD COLUMN referral_rewards_given INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE users ADD COLUMN profile_subjects TEXT")
        except Exception:
            pass

        # --- Флаг прохождения онбординга ---
        try:
            cur.execute("ALTER TABLE users ADD COLUMN onboarded_at TEXT")
        except Exception:
            pass

        # --- Фото в вопросах ---
        try:
            cur.execute("ALTER TABLE questions ADD COLUMN photo_file_id TEXT")
        except Exception:
            pass

        # --- Модерация чата (баны/муты) ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_moderation (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_tg_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                action TEXT NOT NULL,
                until_ts TEXT,
                created_by INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, user_tg_id, action)
            )
        """)

        # --- Цена в звёздах для тестов ---
        try:
            cur.execute("ALTER TABLE tests ADD COLUMN price_stars INTEGER DEFAULT 0")
        except Exception:
            pass

        # --- Настройки режимов Карточки/Заучивание для теста ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS test_modes (
                test_id INTEGER PRIMARY KEY,
                flashcards_enabled INTEGER DEFAULT 1,
                learning_enabled INTEGER DEFAULT 1,
                fc_price_1 INTEGER DEFAULT 5,
                fc_price_10 INTEGER DEFAULT 10,
                fc_price_redo INTEGER DEFAULT 2,
                ln_price_1 INTEGER DEFAULT 5,
                ln_price_10 INTEGER DEFAULT 10,
                ln_price_redo INTEGER DEFAULT 2,
                is_free INTEGER DEFAULT 0
            )
        """)

        # --- Купленные прохождения режимов ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mode_passes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_tg_id INTEGER NOT NULL,
                test_id INTEGER NOT NULL,
                mode TEXT NOT NULL,          -- flashcards | learning
                purchased INTEGER DEFAULT 0, -- сколько куплено
                used INTEGER DEFAULT 0,      -- сколько использовано
                charge_id TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_tg_id, test_id, mode)
            )
        """)

        # --- Активные/незавершённые сессии режимов (переживают рестарт) ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mode_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_tg_id INTEGER NOT NULL,
                test_id INTEGER NOT NULL,
                mode TEXT NOT NULL,          -- flashcards | learning
                question_ids TEXT,           -- JSON список id вопросов
                current_index INTEGER DEFAULT 0,
                statuses TEXT,               -- JSON {qid: status}
                answers TEXT,                -- JSON {qid: [попытки]} (learning)
                know_count INTEGER DEFAULT 0,
                dontknow_count INTEGER DEFAULT 0,
                correct_first INTEGER DEFAULT 0,
                correct_retry INTEGER DEFAULT 0,
                wrong_count INTEGER DEFAULT 0,
                skipped_count INTEGER DEFAULT 0,
                main_message_id INTEGER,
                photo_message_id INTEGER,
                side TEXT DEFAULT 'question', -- question | answer (flashcards)
                is_redo INTEGER DEFAULT 0,    -- сессия повтора ошибок (за 2⭐️)
                pass_charged INTEGER DEFAULT 0, -- списано ли прохождение
                status TEXT DEFAULT 'active', -- active | finished
                started_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_action_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_modesess_user "
                    "ON mode_sessions(user_tg_id, status)")

        # --- История завершённых прохождений режимов ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mode_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_tg_id INTEGER NOT NULL,
                test_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                total INTEGER DEFAULT 0,
                know_count INTEGER DEFAULT 0,
                dontknow_count INTEGER DEFAULT 0,
                correct_first INTEGER DEFAULT 0,
                correct_retry INTEGER DEFAULT 0,
                wrong_count INTEGER DEFAULT 0,
                skipped_count INTEGER DEFAULT 0,
                details TEXT,                -- JSON подробности
                duration_sec INTEGER DEFAULT 0,
                is_redo INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_moderes_user "
                    "ON mode_results(user_tg_id, mode)")

        # --- Доп. допустимые ответы для вопроса (для Заучивания) ---
        try:
            cur.execute("ALTER TABLE questions ADD COLUMN accepted_answers TEXT")
        except Exception:
            pass

        # --- Покупки (звёзды) ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_tg_id INTEGER NOT NULL,
                kind TEXT NOT NULL,          -- test | category | gift | redo
                test_id INTEGER,
                category_id INTEGER,
                gifted_to_tg_id INTEGER,     -- кому подарен (для gift)
                stars_amount INTEGER DEFAULT 0,
                charge_id TEXT,              -- telegram_payment_charge_id (для refund)
                gift_code TEXT,              -- код для подарка по ссылке
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_purchases_user "
                    "ON purchases(user_tg_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_purchases_test "
                    "ON purchases(test_id)")

        # --- Приглашения на дуэль (в БД, чтобы ссылка пережила рестарт) ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS duel_invites (
                code TEXT PRIMARY KEY,
                host_tg_id INTEGER NOT NULL,
                host_chat_id INTEGER,
                host_lang TEXT DEFAULT 'ru',
                category_id INTEGER,         -- раздел дуэли (NULL = все)
                guest_tg_id INTEGER,
                duel_id INTEGER,
                status TEXT DEFAULT 'waiting', -- waiting | started | expired
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Раздел в дуэли
        try:
            cur.execute("ALTER TABLE duels ADD COLUMN category_id INTEGER")
        except Exception:
            pass

        # --- Предупреждения за ссылки ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS link_warnings (
                chat_id INTEGER NOT NULL,
                user_tg_id INTEGER NOT NULL,
                warns INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, user_tg_id)
            )
        """)

        # --- Очередь автопубликации (дублируем тут для надёжности) ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS autopub_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_id INTEGER NOT NULL,
                run_at TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                error TEXT DEFAULT '',
                created_by INTEGER,
                series_id TEXT DEFAULT '',
                series_pos INTEGER DEFAULT 0,
                series_total INTEGER DEFAULT 1,
                series_test_ids TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_autopub_status_time "
                    "ON autopub_queue(status, run_at)")

        # === Раздел "Начать обучение" (сайт) ===
        cur.execute("""
            CREATE TABLE IF NOT EXISTS subjects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                cover_url TEXT,
                sort_order INTEGER DEFAULT 0,
                is_open INTEGER DEFAULT 0,     -- 0=нужен доступ, 1=открыт всем
                status TEXT DEFAULT 'active',  -- active | hidden
                created_by INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Приватный предмет: не показывается в общем каталоге вообще,
        # виден и открывается только тем, кому выдан доступ (subject_access).
        try:
            cur.execute("ALTER TABLE subjects ADD COLUMN is_private INTEGER DEFAULT 0")
        except Exception:
            pass
        # Стоп-уроки: настройки прогрессии обучения (каждый предмет отдельно)
        for _col, _sql in (
            ("min_read_min", "ALTER TABLE subjects ADD COLUMN min_read_min INTEGER DEFAULT 0"),      # мин. минут чтения до теста (0=выкл)
            ("require_sequential", "ALTER TABLE subjects ADD COLUMN require_sequential INTEGER DEFAULT 0"),  # следующий урок после сдачи ДЗ
            ("pass_percent", "ALTER TABLE subjects ADD COLUMN pass_percent INTEGER DEFAULT 0"),       # порог сдачи % (0=любой результат)
            ("live_code_enabled", "ALTER TABLE subjects ADD COLUMN live_code_enabled INTEGER DEFAULT 0"),  # инлайн-ввод кода Live внутри предмета
            ("is_pinned", "ALTER TABLE subjects ADD COLUMN is_pinned INTEGER DEFAULT 0"),             # закреплён первым в каталоге у всех
        ):
            try:
                cur.execute(_sql)
            except Exception:
                pass
        # Разрешить ученику прогнать заново только те вопросы, где он ошибся.
        # Включается отдельно у каждого теста: не всякий тест этого хочет.
        for _col, _sql in (
            ("allow_retry_wrong", "ALTER TABLE tests ADD COLUMN allow_retry_wrong INTEGER DEFAULT 0"),
        ):
            try:
                cur.execute(_sql)
            except Exception:
                pass
        # Обязательная подписка на канал для предмета сайта (required_channels.subject_id)
        try:
            cur.execute("ALTER TABLE required_channels ADD COLUMN subject_id INTEGER")
        except Exception:
            pass
        # --- Уровень бот-админа (1 базовый / 2 расширенный). Владелец из ADMIN_IDS = выше всех ---
        try:
            cur.execute("ALTER TABLE admins ADD COLUMN level INTEGER DEFAULT 1")
        except Exception:
            pass
        # --- Сайт-админы: доступ к админке САЙТА отдельно от бота ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS site_admins (
                tg_id INTEGER PRIMARY KEY,
                username TEXT,
                granted_by INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # --- Фото в конспекте урока (несколько на урок) ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lesson_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lesson_id INTEGER NOT NULL,
                image_path TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (lesson_id) REFERENCES lessons(id) ON DELETE CASCADE
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lesson_images_lesson "
                    "ON lesson_images(lesson_id)")
        # Хранилище в Telegram: страница конспекта может лежать не файлом на
        # диске, а file_id в Telegram — место на сервере не тратится.
        # image_path тогда пустой, а источник указан в storage.
        for _sql in (
            "ALTER TABLE lesson_images ADD COLUMN file_id TEXT",
            "ALTER TABLE lesson_images ADD COLUMN file_unique_id TEXT",
            "ALTER TABLE lesson_images ADD COLUMN storage TEXT DEFAULT 'disk'",
            "ALTER TABLE lesson_images ADD COLUMN as_document INTEGER DEFAULT 0",
            "ALTER TABLE lesson_images ADD COLUMN file_name TEXT",
            "ALTER TABLE lesson_images ADD COLUMN added_by INTEGER",
        ):
            try:
                cur.execute(_sql)
            except Exception:
                pass
        try:
            cur.execute("UPDATE lesson_images SET storage='disk' "
                        "WHERE storage IS NULL OR storage=''")
        except Exception:
            pass
        # Журнал выдачи Премиума: кто, каким способом, когда и за сколько.
        # Нужен, чтобы было видно, кто купил за звёзды, кому выдали руками,
        # а кто получил за приглашённых друзей.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS premium_grants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                tg_id INTEGER,
                source TEXT NOT NULL,        -- manual | stars | money | referral
                days INTEGER DEFAULT 0,      -- 0 = бессрочно
                amount INTEGER DEFAULT 0,    -- сколько заплатил (звёзды/деньги)
                currency TEXT DEFAULT '',
                granted_by INTEGER,          -- кто выдал (админ), если вручную
                comment TEXT DEFAULT '',
                expires_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_prem_grants "
                    "ON premium_grants(created_at DESC)")

        # Отдельно продаваемый предмет: его платные уроки НЕ открываются общим
        # Премиумом — только доступом, выданным именно на этот предмет.
        try:
            cur.execute("ALTER TABLE subjects ADD COLUMN premium_ignored INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE subjects ADD COLUMN own_price TEXT DEFAULT ''")
        except Exception:
            pass

        # --- Единый аккаунт: пароль для входа с сайта + привязка Telegram ---
        # tg_id остаётся ключом (его NOT NULL/UNIQUE не трогаем, чтобы не
        # перестраивать таблицу на живых данных). У аккаунта, созданного на
        # сайте без Telegram, там временный отрицательный номер, а tg_linked=0.
        for _col, _sql in (
            ("password_hash", "ALTER TABLE users ADD COLUMN password_hash TEXT"),
            ("password_set_at", "ALTER TABLE users ADD COLUMN password_set_at TEXT"),
            ("tg_linked", "ALTER TABLE users ADD COLUMN tg_linked INTEGER DEFAULT 0"),
            ("tg_linked_at", "ALTER TABLE users ADD COLUMN tg_linked_at TEXT"),
            ("login_fail_count", "ALTER TABLE users ADD COLUMN login_fail_count INTEGER DEFAULT 0"),
            ("lockout_until", "ALTER TABLE users ADD COLUMN lockout_until TEXT"),
            ("session_epoch", "ALTER TABLE users ADD COLUMN session_epoch INTEGER DEFAULT 0"),
        ):
            try:
                cur.execute(_sql)
            except Exception:
                pass
        # У всех, кто пришёл из Telegram, привязка уже есть по факту
        try:
            cur.execute("UPDATE users SET tg_linked=1 WHERE tg_id > 0 "
                        "AND COALESCE(tg_linked,0)=0")
        except Exception:
            pass
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_uname "
                    "ON users(LOWER(username))")

        # Одноразовые коды: вход по паролю с сайта и сброс пароля
        cur.execute("""
            CREATE TABLE IF NOT EXISTS login_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                purpose TEXT DEFAULT 'login',   -- login | reset
                code_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                attempts INTEGER DEFAULT 0,
                used_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_login_codes "
                    "ON login_codes(user_id, purpose, used_at)")

        # Журнал важных событий входа. Пароли и коды сюда не попадают.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS auth_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                tg_id INTEGER,
                event TEXT NOT NULL,
                details TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_auth_events "
                    "ON auth_events(created_at DESC)")

        # --- Кампании сообщений (ручное «Конспекты ЕНТ» и автонапоминания) ---
        # Одна таблица на оба случая: у них одинаковый набор полей (текст,
        # подпись кнопки, ссылка). Отличаются только ключом кампании.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reminder_campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_key TEXT NOT NULL,        -- notes_manual | notes_reminder
                version INTEGER DEFAULT 1,
                enabled INTEGER DEFAULT 1,
                message_text TEXT NOT NULL,
                button_text TEXT NOT NULL,
                button_url TEXT NOT NULL,
                cooldown_seconds INTEGER DEFAULT 259200,   -- 3 суток
                safe_delay_seconds INTEGER DEFAULT 600,    -- 10 минут после активности
                status TEXT DEFAULT 'active',      -- active | archived
                created_by INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Второй кнопке место нашлось не сразу: сообщение после покупки
        # Премиума ведёт и в конспекты, и в платные тесты.
        for _c, _sql in (
            ("button2_text", "ALTER TABLE reminder_campaigns ADD COLUMN button2_text TEXT"),
            ("button2_url", "ALTER TABLE reminder_campaigns ADD COLUMN button2_url TEXT"),
            ("sort_order", "ALTER TABLE reminder_campaigns ADD COLUMN sort_order INTEGER DEFAULT 0"),
        ):
            try:
                cur.execute(_sql)
            except Exception:
                pass
        cur.execute("CREATE INDEX IF NOT EXISTS idx_camp_key "
                    "ON reminder_campaigns(campaign_key, status)")

        # Кому и когда уже отправляли — переживает рестарт и восстановление
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_reminder_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_tg_id INTEGER NOT NULL,
                campaign_id INTEGER NOT NULL,
                last_sent_at TEXT,
                next_allowed_at TEXT,
                send_count INTEGER DEFAULT 0,
                last_status TEXT DEFAULT '',    -- sent | deferred | sending | failed | blocked
                last_skip_reason TEXT DEFAULT '',
                last_attempt_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_tg_id, campaign_id)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_urs_due "
                    "ON user_reminder_state(campaign_id, next_allowed_at)")

        # Активность внутри SmartENT — Bot API не отдаёт online/offline,
        # поэтому «занят ли человек» считаем по своим отметкам.
        for _col, _sql in (
            ("last_activity_at", "ALTER TABLE users ADD COLUMN last_activity_at TEXT"),
            ("last_test_activity_at", "ALTER TABLE users ADD COLUMN last_test_activity_at TEXT"),
            ("bot_blocked", "ALTER TABLE users ADD COLUMN bot_blocked INTEGER DEFAULT 0"),
        ):
            try:
                cur.execute(_sql)
            except Exception:
                pass

        # Сессии приёма частей и журнал восстановлений живут НЕ здесь, а в
        # отдельном файле restore_state.db рядом с базой (services/
        # restore_service.py). Иначе восстановление, которое подменяет этот
        # файл целиком, стирало бы собственный журнал в момент работы.

        # Предмет сайта ↔ раздел бота: создаём предмет на сайте — в боте
        # автоматически появляется одноимённый раздел для тестов.
        try:
            cur.execute("ALTER TABLE subjects ADD COLUMN bot_category_id INTEGER")
        except Exception:
            pass

        # --- Отложенная выдача доступа (тем, кто ещё не нажал /start) ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pending_access (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,          -- lower, без @
                kind TEXT DEFAULT 'private',     -- private | premium
                test_id INTEGER,
                days INTEGER DEFAULT 0,
                granted_by INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                fulfilled INTEGER DEFAULT 0,
                fulfilled_at TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pending_uname "
                    "ON pending_access(username, fulfilled)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS site_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS subject_access (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_id INTEGER NOT NULL,
                user_tg_id INTEGER NOT NULL,
                granted_at TEXT DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT,
                granted_by_admin INTEGER,
                UNIQUE(subject_id, user_tg_id),
                FOREIGN KEY (subject_id) REFERENCES subjects(id) ON DELETE CASCADE
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_subj_access_user "
                    "ON subject_access(user_tg_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (subject_id) REFERENCES subjects(id) ON DELETE CASCADE
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sections_subject "
                    "ON sections(subject_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                section_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                content_html TEXT DEFAULT '',
                test_id INTEGER,
                sort_order INTEGER DEFAULT 0,
                status TEXT DEFAULT 'open',    -- open | closed
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (section_id) REFERENCES sections(id) ON DELETE CASCADE
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lessons_section "
                    "ON lessons(section_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lesson_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_tg_id INTEGER NOT NULL,
                lesson_id INTEGER NOT NULL,
                viewed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_tg_id, lesson_id),
                FOREIGN KEY (lesson_id) REFERENCES lessons(id) ON DELETE CASCADE
            )
        """)
        # Когда пользователь впервые открыл урок — для отсчёта времени чтения (стоп-уроки)
        try:
            cur.execute("ALTER TABLE lesson_progress ADD COLUMN read_started_at TEXT")
        except Exception:
            pass

        # === Симулятор зачётов: урок может быть зачётом (банк вопросов по темам) ===
        for _sql in (
            "ALTER TABLE lessons ADD COLUMN is_zachet INTEGER DEFAULT 0",
            "ALTER TABLE lessons ADD COLUMN zachet_per_attempt INTEGER DEFAULT 20",   # вопросов на попытку
            "ALTER TABLE lessons ADD COLUMN zachet_topic_threshold INTEGER DEFAULT 65",  # % «нужно повторить» по теме
            "ALTER TABLE lessons ADD COLUMN zachet_pass_percent INTEGER DEFAULT 70",  # общий проходной %
        ):
            try:
                cur.execute(_sql)
            except Exception:
                pass
        cur.execute("""
            CREATE TABLE IF NOT EXISTS zachet_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lesson_id INTEGER NOT NULL,
                topic TEXT NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                order_num INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (lesson_id) REFERENCES lessons(id) ON DELETE CASCADE
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_zq_lesson ON zachet_questions(lesson_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS zachet_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lesson_id INTEGER NOT NULL,
                user_tg_id INTEGER NOT NULL,
                question_ids TEXT,          -- JSON: id вопросов этой попытки
                answers_json TEXT,          -- JSON {qid: {"user":..,"ok":0/1}}
                per_topic_json TEXT,        -- JSON {тема: {total,correct,percent}}
                total INTEGER DEFAULT 0,
                correct INTEGER DEFAULT 0,
                percent REAL DEFAULT 0,
                passed INTEGER DEFAULT 0,
                status TEXT DEFAULT 'in_progress',  -- in_progress | finished
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_za_lesson_user "
                    "ON zachet_attempts(lesson_id, user_tg_id)")

        # === Live-режим (Kahoot-style онлайн-викторина в реальном времени) ===
        cur.execute("""
            CREATE TABLE IF NOT EXISTS live_rooms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                test_id INTEGER NOT NULL,
                host_tg_id INTEGER NOT NULL,
                status TEXT DEFAULT 'lobby',      -- lobby | question | stats | finished
                current_index INTEGER DEFAULT -1,
                question_started_at TEXT,          -- серверное время старта вопроса (UTC iso)
                time_per_question INTEGER DEFAULT 20,
                mode TEXT DEFAULT 'competitive',   -- competitive | study
                locked INTEGER DEFAULT 0,          -- запрет входа новых
                rating_visibility TEXT DEFAULT 'full',  -- full | top5 | self | hidden
                question_order TEXT,               -- JSON список question_id
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_live_code ON live_rooms(code)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS live_players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id INTEGER NOT NULL,
                tg_id INTEGER NOT NULL,
                name TEXT,
                score INTEGER DEFAULT 0,
                correct INTEGER DEFAULT 0,
                streak INTEGER DEFAULT 0,
                best_streak INTEGER DEFAULT 0,
                total_time_ms INTEGER DEFAULT 0,
                kicked INTEGER DEFAULT 0,
                last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
                joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(room_id, tg_id)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_live_players_room ON live_players(room_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS live_answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id INTEGER NOT NULL,
                question_index INTEGER NOT NULL,
                question_id INTEGER,
                tg_id INTEGER NOT NULL,
                option_id INTEGER,
                is_correct INTEGER DEFAULT 0,
                answer_ms INTEGER DEFAULT 0,
                points INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(room_id, question_index, tg_id)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_live_ans_room ON live_answers(room_id, question_index)")

        # Журнал выдачи конспектов в Telegram
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lesson_note_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER NOT NULL,
                lesson_id INTEGER NOT NULL,
                test_id INTEGER,
                images_sent INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_note_log ON lesson_note_log(tg_id, lesson_id)")

        # Журнал просмотра конспектов: кто, когда и на каком основании открыл.
        # Данные пишем СНИМКОМ — чтобы история осталась верной, даже если
        # пользователь потом сменит ник, а урок переименуют.
        for _sql in (
            "ALTER TABLE users ADD COLUMN phone TEXT",
            "ALTER TABLE lesson_note_log ADD COLUMN subject_id INTEGER",
            "ALTER TABLE lesson_note_log ADD COLUMN subject_title TEXT",
            "ALTER TABLE lesson_note_log ADD COLUMN section_title TEXT",
            "ALTER TABLE lesson_note_log ADD COLUMN lesson_title TEXT",
            "ALTER TABLE lesson_note_log ADD COLUMN username TEXT",
            "ALTER TABLE lesson_note_log ADD COLUMN first_name TEXT",
            "ALTER TABLE lesson_note_log ADD COLUMN last_name TEXT",
            "ALTER TABLE lesson_note_log ADD COLUMN phone TEXT",
            "ALTER TABLE lesson_note_log ADD COLUMN access_type TEXT",
            "ALTER TABLE lesson_note_log ADD COLUMN access_since TEXT",
            "ALTER TABLE lesson_note_log ADD COLUMN opened_at_local TEXT",
            "ALTER TABLE lesson_note_log ADD COLUMN source TEXT",
        ):
            try:
                cur.execute(_sql)
            except Exception:
                pass
        cur.execute("CREATE INDEX IF NOT EXISTS idx_note_log_time "
                    "ON lesson_note_log(created_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_note_log_subj "
                    "ON lesson_note_log(subject_id)")

        # Сообщения с конспектами — для тихого автоудаления через сутки
        cur.execute("""
            CREATE TABLE IF NOT EXISTS note_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                lesson_id INTEGER,
                sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
                deleted INTEGER DEFAULT 0
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_note_msgs ON note_messages(deleted, sent_at)")

        # Ссылка на карточки Quizlet: у урока своя, у предмета — общая (fallback)
        for _sql in ("ALTER TABLE lessons ADD COLUMN quizlet_url TEXT",
                     "ALTER TABLE subjects ADD COLUMN quizlet_url TEXT"):
            try:
                cur.execute(_sql)
            except Exception:
                pass

        # Веб-путь к картинке вопроса (для сайта, отдельно от Telegram file_id)
        try:
            cur.execute("ALTER TABLE questions ADD COLUMN web_image_path TEXT")
        except Exception:
            pass

        # --- Копии-ярлыки и режим доступа предмета ---
        # original_id: запись-ссылка. Контента не хранит — только обложку
        # (название, описание, свои настройки платности). Всё содержимое,
        # прогресс и доступ берутся у оригинала.
        # access_mode: open | closed | premium | private
        #   open     — открыт всем без выдачи доступа
        #   closed   — виден в каталоге, но весь контент только по доступу
        #   premium  — виден в каталоге, бесплатные уроки открыты,
        #              платные — по Премиуму или выданному доступу
        #   private  — не виден в каталоге, только по выданному доступу
        # free_override: админ вручную сделал урок бесплатным в платном
        # (премиум) предмете — при смене режима такой урок не запирается снова
        try:
            cur.execute("ALTER TABLE lessons ADD COLUMN free_override INTEGER DEFAULT 0")
        except Exception:
            pass
        for _sql in ("ALTER TABLE subjects ADD COLUMN original_id INTEGER",
                     "ALTER TABLE sections ADD COLUMN original_id INTEGER",
                     "ALTER TABLE lessons ADD COLUMN original_id INTEGER",
                     "ALTER TABLE subjects ADD COLUMN access_mode TEXT"):
            try:
                cur.execute(_sql)
            except Exception:
                pass
        for _sql in ("CREATE INDEX IF NOT EXISTS idx_subj_orig ON subjects(original_id)",
                     "CREATE INDEX IF NOT EXISTS idx_sec_orig ON sections(original_id)",
                     "CREATE INDEX IF NOT EXISTS idx_les_orig ON lessons(original_id)"):
            try:
                cur.execute(_sql)
            except Exception:
                pass
        # Проставляем режим уже существующим предметам
        try:
            cur.execute(
                "UPDATE subjects SET access_mode = CASE "
                "  WHEN COALESCE(is_private,0)=1 THEN 'private' "
                "  WHEN COALESCE(is_open,0)=1 THEN 'open' "
                "  ELSE 'closed' END "
                "WHERE access_mode IS NULL OR access_mode=''")
        except Exception:
            pass

        # Показывать ли результаты (счёт/процент) ученику после теста
        try:
            cur.execute("ALTER TABLE tests ADD COLUMN show_results INTEGER DEFAULT 1")
        except Exception:
            pass

        # Черновики импорта теста для урока — превью перед подтверждением
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lesson_test_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_tg_id INTEGER NOT NULL,
                section_id INTEGER NOT NULL,
                lesson_id INTEGER,              -- NULL = новый урок, иначе замена теста существующего
                lesson_title TEXT NOT NULL,
                lesson_description TEXT DEFAULT '',
                lesson_content TEXT DEFAULT '',
                questions_json TEXT NOT NULL,   -- распарсенные вопросы (текст+варианты+картинка)
                errors_json TEXT DEFAULT '[]',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Видео урока: своё загруженное (video_url, отдаётся с диска рядом с БД)
        # или встроенное с YouTube (youtube_id)
        try:
            cur.execute("ALTER TABLE lessons ADD COLUMN video_url TEXT")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE lessons ADD COLUMN youtube_id TEXT")
        except Exception:
            pass
        # Видео, загруженное через бота: файл живёт в Telegram по file_id,
        # на сервере места не занимает — как и страницы конспектов.
        try:
            cur.execute("ALTER TABLE lessons ADD COLUMN video_file_id TEXT")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE lessons ADD COLUMN video_file_unique TEXT")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE lesson_test_drafts ADD COLUMN video_url TEXT")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE lesson_test_drafts ADD COLUMN youtube_id TEXT")
        except Exception:
            pass

        # --- Платные конспекты (отдельно от доступа к предмету) ---
        try:
            cur.execute("ALTER TABLE lessons ADD COLUMN is_paid INTEGER DEFAULT 0")
        except Exception:
            pass
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lesson_access (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lesson_id INTEGER NOT NULL,
                user_tg_id INTEGER NOT NULL,
                granted_at TEXT DEFAULT CURRENT_TIMESTAMP,
                granted_by_admin INTEGER,
                UNIQUE(lesson_id, user_tg_id),
                FOREIGN KEY (lesson_id) REFERENCES lessons(id) ON DELETE CASCADE
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lesson_access_user "
                    "ON lesson_access(user_tg_id)")
        try:
            cur.execute("ALTER TABLE lesson_test_drafts ADD COLUMN is_paid INTEGER DEFAULT 0")
        except Exception:
            pass

        logger.info("База данных инициализирована")
