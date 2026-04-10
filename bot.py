import os
import re
import logging
from typing import List, Dict, Optional, Tuple

import psycopg2
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode, PollType
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    PollAnswerHandler,
    filters,
)

# =========================
# ЗАГРУЗКА ПЕРЕМЕННЫХ
# =========================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMINS_RAW = os.getenv("ADMINS", "").strip()

if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN")

if not DATABASE_URL:
    raise ValueError("Не найден DATABASE_URL")

if not ADMINS_RAW:
    raise ValueError("Не найден ADMINS")

ADMINS = {
    int(x.strip())
    for x in ADMINS_RAW.split(",")
    if x.strip().isdigit()
}

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# =========================
# БАЗА ДАННЫХ
# =========================
def get_conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id BIGINT PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS tests (
                    id SERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_by BIGINT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS questions (
                    id SERIAL PRIMARY KEY,
                    test_id INTEGER NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
                    question_text TEXT NOT NULL,
                    position INTEGER NOT NULL
                );
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS options (
                    id SERIAL PRIMARY KEY,
                    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
                    option_text TEXT NOT NULL,
                    is_correct BOOLEAN NOT NULL DEFAULT FALSE,
                    position INTEGER NOT NULL
                );
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_test_progress (
                    user_id BIGINT NOT NULL,
                    test_id INTEGER NOT NULL,
                    current_question_pos INTEGER NOT NULL DEFAULT 0,
                    score INTEGER NOT NULL DEFAULT 0,
                    finished BOOLEAN NOT NULL DEFAULT FALSE,
                    updated_at TIMESTAMP DEFAULT NOW(),
                    PRIMARY KEY (user_id, test_id)
                );
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS pending_imports (
                    admin_id BIGINT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)

        conn.commit()


# =========================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =========================
def is_admin(user_id: int) -> bool:
    return user_id in ADMINS


def save_user(user_id: int, username: Optional[str], full_name: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (id, username, full_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (id)
                DO UPDATE SET
                    username = EXCLUDED.username,
                    full_name = EXCLUDED.full_name;
            """, (user_id, username, full_name))
        conn.commit()


def set_pending_import(admin_id: int, title: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pending_imports (admin_id, title)
                VALUES (%s, %s)
                ON CONFLICT (admin_id)
                DO UPDATE SET
                    title = EXCLUDED.title,
                    created_at = NOW();
            """, (admin_id, title))
        conn.commit()


def get_pending_import(admin_id: int) -> Optional[str]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT title
                FROM pending_imports
                WHERE admin_id = %s
            """, (admin_id,))
            row = cur.fetchone()
            return row[0] if row else None


def clear_pending_import(admin_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pending_imports WHERE admin_id = %s", (admin_id,))
        conn.commit()


def create_test(title: str, created_by: int) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO tests (title, created_by)
                VALUES (%s, %s)
                RETURNING id
            """, (title, created_by))
            test_id = cur.fetchone()[0]
        conn.commit()
    return test_id


def add_question_with_options(
    test_id: int,
    question_text: str,
    options: List[Tuple[str, bool]],
    position: int
):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO questions (test_id, question_text, position)
                VALUES (%s, %s, %s)
                RETURNING id
            """, (test_id, question_text, position))
            question_id = cur.fetchone()[0]

            for idx, (option_text, is_correct_value) in enumerate(options):
                cur.execute("""
                    INSERT INTO options (question_id, option_text, is_correct, position)
                    VALUES (%s, %s, %s, %s)
                """, (question_id, option_text, is_correct_value, idx))

        conn.commit()


def get_tests() -> List[Tuple[int, str]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, title
                FROM tests
                ORDER BY id DESC
            """)
            return cur.fetchall()


def get_test_title(test_id: int) -> Optional[str]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT title FROM tests WHERE id = %s", (test_id,))
            row = cur.fetchone()
            return row[0] if row else None


def get_questions_for_test(test_id: int) -> List[Dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, question_text, position
                FROM questions
                WHERE test_id = %s
                ORDER BY position ASC, id ASC
            """, (test_id,))
            questions = cur.fetchall()

            result = []
            for q_id, q_text, pos in questions:
                cur.execute("""
                    SELECT option_text, is_correct, position
                    FROM options
                    WHERE question_id = %s
                    ORDER BY position ASC, id ASC
                """, (q_id,))
                opts = cur.fetchall()

                result.append({
                    "id": q_id,
                    "question_text": q_text,
                    "position": pos,
                    "options": [
                        {
                            "text": opt[0],
                            "is_correct": opt[1],
                            "position": opt[2],
                        }
                        for opt in opts
                    ]
                })

            return result


def reset_user_progress(user_id: int, test_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_test_progress (
                    user_id,
                    test_id,
                    current_question_pos,
                    score,
                    finished
                )
                VALUES (%s, %s, 0, 0, FALSE)
                ON CONFLICT (user_id, test_id)
                DO UPDATE SET
                    current_question_pos = 0,
                    score = 0,
                    finished = FALSE,
                    updated_at = NOW();
            """, (user_id, test_id))
        conn.commit()


def get_user_progress(user_id: int, test_id: int) -> Optional[Tuple[int, int, bool]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT current_question_pos, score, finished
                FROM user_test_progress
                WHERE user_id = %s AND test_id = %s
            """, (user_id, test_id))
            return cur.fetchone()


def update_user_progress(
    user_id: int,
    test_id: int,
    current_question_pos: int,
    score: int,
    finished: bool
):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE user_test_progress
                SET current_question_pos = %s,
                    score = %s,
                    finished = %s,
                    updated_at = NOW()
                WHERE user_id = %s AND test_id = %s
            """, (current_question_pos, score, finished, user_id, test_id))
        conn.commit()


# =========================
# ПАРСИНГ ВОПРОСОВ
# =========================
def normalize_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip())


def is_option_line(line: str) -> bool:
    return bool(re.match(
        r"^\s*([A-Za-zА-Яа-яЁёІіҢңҒғҮүҰұҚқӨөҺһ0-9]+)[\)\.\-]\s+.+",
        line.strip()
    ))


def clean_option_prefix(line: str) -> str:
    return re.sub(
        r"^\s*([A-Za-zА-Яа-яЁёІіҢңҒғҮүҰұҚқӨөҺһ0-9]+)[\)\.\-]\s+",
        "",
        line.strip()
    )


def parse_questions_block(text: str) -> List[Dict]:
    lines = [line.rstrip() for line in text.splitlines()]

    blocks = []
    current_block = []

    for line in lines:
        if line.strip() == "":
            if current_block:
                blocks.append(current_block)
                current_block = []
        else:
            current_block.append(line)

    if current_block:
        blocks.append(current_block)

    parsed = []

    for block in blocks:
        question_lines = []
        option_lines = []

        for line in block:
            if is_option_line(line):
                option_lines.append(line)
            else:
                question_lines.append(line)

        if not question_lines or len(option_lines) < 2:
            raise ValueError(
                "Неверный формат вопроса. Нужен текст вопроса и минимум 2 варианта ответа."
            )

        question_text = " ".join(normalize_line(x) for x in question_lines).strip()

        options = []
        correct_count = 0

        for raw_option in option_lines:
            option_text = clean_option_prefix(raw_option)
            is_correct_value = "*" in option_text
            option_text = option_text.replace("*", "").strip()

            if not option_text:
                raise ValueError("Один из вариантов ответа пустой.")

            if is_correct_value:
                correct_count += 1

            options.append((option_text, is_correct_value))

        if correct_count != 1:
            raise ValueError(
                f'В вопросе "{question_text}" должен быть ровно 1 правильный ответ со звездочкой *'
            )

        parsed.append({
            "question_text": question_text,
            "options": options
        })

    if not parsed:
        raise ValueError("Не найдено ни одного вопроса.")

    return parsed


# =========================
# ОТПРАВКА СЛЕДУЮЩЕГО ВОПРОСА
# =========================
async def send_next_question(context: ContextTypes.DEFAULT_TYPE, user_id: int, test_id: int):
    questions = get_questions_for_test(test_id)
    progress = get_user_progress(user_id, test_id)

    if not progress:
        reset_user_progress(user_id, test_id)
        progress = get_user_progress(user_id, test_id)

    current_question_pos, score, finished = progress

    if finished:
        await context.bot.send_message(
            chat_id=user_id,
            text="Этот тест уже завершён."
        )
        return

    if current_question_pos >= len(questions):
        update_user_progress(user_id, test_id, current_question_pos, score, True)
        await context.bot.send_message(
            chat_id=user_id,
            text=f"✅ Тест завершён!\n\nВаш результат: {score} из {len(questions)}"
        )
        return

    question = questions[current_question_pos]
    options = question["options"]

    option_texts = [opt["text"] for opt in options]
    correct_indexes = [i for i, opt in enumerate(options) if opt["is_correct"]]

    if not correct_indexes:
        await context.bot.send_message(
            chat_id=user_id,
            text="Ошибка: у вопроса не найден правильный ответ."
        )
        return

    correct_index = correct_indexes[0]

    poll_message = await context.bot.send_poll(
        chat_id=user_id,
        question=question["question_text"][:300],
        options=option_texts[:10],
        type=PollType.QUIZ,
        is_anonymous=False,
        correct_option_id=correct_index,
        explanation=f"Вопрос {current_question_pos + 1} из {len(questions)}"
    )

    context.bot_data[poll_message.poll.id] = {
        "user_id": user_id,
        "test_id": test_id,
        "question_pos": current_question_pos,
        "correct_index": correct_index,
    }


# =========================
# ХЕНДЛЕРЫ
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message

    if not user or not message:
        return

    save_user(user.id, user.username, user.full_name)

    tests = get_tests()

    text = (
        f"Привет, {user.first_name or 'друг'}! 👋\n\n"
        "Я бот для прохождения тестов.\n"
        "Выберите тест ниже."
    )

    if not tests:
        await message.reply_text(text + "\n\nПока тестов нет.")
        return

    keyboard = [
        [InlineKeyboardButton(title, callback_data=f"start_test:{test_id}")]
        for test_id, title in tests
    ]

    await message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def tests_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    tests = get_tests()

    if not tests:
        await message.reply_text("Пока тестов нет.")
        return

    keyboard = [
        [InlineKeyboardButton(title, callback_data=f"start_test:{test_id}")]
        for test_id, title in tests
    ]

    await message.reply_text(
        "Выберите тест:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def addtest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message

    if not user or not message:
        return

    if not is_admin(user.id):
        await message.reply_text("Эта команда доступна только админу.")
        return

    if not context.args:
        await message.reply_text("Напишите так:\n/addtest Название теста")
        return

    title = " ".join(context.args).strip()
    if not title:
        await message.reply_text("Укажите название теста.")
        return

    set_pending_import(user.id, title)

    await message.reply_text(
        f"✅ Режим добавления включён.\n\n"
        f"Название теста: <b>{title}</b>\n\n"
        f"Теперь следующим сообщением отправьте вопросы одним текстом.\n\n"
        f"Пример:\n"
        f"Кто такой Абылай хан?\n"
        f"А) хан*\n"
        f"Б) раб\n"
        f"С) батыр\n"
        f"Д) аксакал",
        parse_mode=ParseMode.HTML
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message

    if not user or not message:
        return

    clear_pending_import(user.id)
    await message.reply_text("Отменено.")


async def listtests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message

    if not user or not message:
        return

    if not is_admin(user.id):
        await message.reply_text("Эта команда доступна только админу.")
        return

    tests = get_tests()
    if not tests:
        await message.reply_text("Тестов пока нет.")
        return

    text = "📚 Список тестов:\n\n"
    for test_id, title in tests:
        text += f"{test_id}. {title}\n"

    await message.reply_text(text)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user

    if not message:
        return

    admin_block = ""
    if user and is_admin(user.id):
        admin_block = (
            "\n\n<b>Команды админа:</b>\n"
            "/addtest Название теста — добавить тест\n"
            "/listtests — список тестов\n"
            "/cancel — отменить режим добавления"
        )

    await message.reply_text(
        "Команды:\n"
        "/start — начать\n"
        "/tests — список тестов\n"
        "/help — помощь"
        + admin_block,
        parse_mode=ParseMode.HTML
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message

    if not user or not message or not message.text:
        return

    save_user(user.id, user.username, user.full_name)

    pending_title = get_pending_import(user.id)

    if pending_title and is_admin(user.id):
        try:
            parsed_questions = parse_questions_block(message.text)
            test_id = create_test(pending_title, user.id)

            for idx, q in enumerate(parsed_questions):
                add_question_with_options(
                    test_id=test_id,
                    question_text=q["question_text"],
                    options=q["options"],
                    position=idx
                )

            clear_pending_import(user.id)

            await message.reply_text(
                f"✅ Тест успешно создан!\n\n"
                f"Название: {pending_title}\n"
                f"ID теста: {test_id}\n"
                f"Количество вопросов: {len(parsed_questions)}"
            )
            return

        except Exception as e:
            await message.reply_text(
                f"❌ Ошибка при разборе теста:\n{e}\n\n"
                f"Проверьте формат и отправьте заново.\n"
                f"Или используйте /cancel"
            )
            return

    await message.reply_text("Нажмите /start или /tests, чтобы выбрать тест.")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.from_user:
        return

    await query.answer()

    data = query.data or ""
    user_id = query.from_user.id

    if data.startswith("start_test:"):
        test_id = int(data.split(":")[1])
        title = get_test_title(test_id)

        if not title:
            if query.message:
                await query.message.reply_text("Тест не найден.")
            return

        reset_user_progress(user_id, test_id)

        if query.message:
            await query.message.reply_text(
                f"▶️ Начинаем тест:\n<b>{title}</b>",
                parse_mode=ParseMode.HTML
            )

        await send_next_question(context, user_id, test_id)


async def poll_answer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    if not answer:
        return

    poll_id = answer.poll_id
    user_id = answer.user.id
    chosen_ids = answer.option_ids or []

    poll_data = context.bot_data.get(poll_id)
    if not poll_data:
        return

    if poll_data["user_id"] != user_id:
        return

    test_id = poll_data["test_id"]
    correct_index = poll_data["correct_index"]

    progress = get_user_progress(user_id, test_id)
    if not progress:
        return

    current_question_pos, score, finished = progress
    if finished:
        return

    if chosen_ids and chosen_ids[0] == correct_index:
        score += 1

    next_pos = current_question_pos + 1
    questions = get_questions_for_test(test_id)
    is_finished = next_pos >= len(questions)

    update_user_progress(user_id, test_id, next_pos, score, is_finished)

    if is_finished:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"✅ Тест завершён!\n\nВаш результат: {score} из {len(questions)}"
        )
    else:
        await send_next_question(context, user_id, test_id)


# =========================
# ЗАПУСК
# =========================
def main():
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("tests", tests_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("addtest", addtest))
    app.add_handler(CommandHandler("listtests", listtests))
    app.add_handler(CommandHandler("cancel", cancel))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(PollAnswerHandler(poll_answer_handler))

    logger.info("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
