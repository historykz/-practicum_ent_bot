"""
Конфигурация бота.
Все настройки бота централизованы здесь.
Чувствительные данные (BOT_TOKEN) загружаются из переменных окружения или .env файла.
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # dotenv опционален - если не установлен, читаем напрямую из окружения
    pass

# === Основные настройки ===
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")

# Список ID администраторов (через запятую в .env)
_admin_ids_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: list[int] = [
    int(x.strip()) for x in _admin_ids_raw.split(",") if x.strip().isdigit()
]

# Username менеджера (без @) для платных тестов и поддержки
MANAGER_USERNAME: str = os.getenv("MANAGER_USERNAME", "historyentk_bot")

# Канал обязательной подписки (с @ или без; пустая строка = выключено).
# По умолчанию — @historykazakhkz. Можно переопределить через Railway Variables.
REQUIRED_CHANNEL: str = os.getenv("REQUIRED_CHANNEL", "@historykazakhkz").strip()

# Подпись «автор» в карточках теста при шеринге.
# Не зависит от того, кто реально создал тест — это просто витрина бренда.
SHARE_AUTHOR_LABEL: str = os.getenv("SHARE_AUTHOR_LABEL", "@historykazakhkz")

# === База данных ===
BASE_DIR = Path(__file__).resolve().parent
DB_PATH: str = os.getenv("DB_PATH", str(BASE_DIR / "ent_bot.db"))

# === Защита от спама ===
ANTISPAM_COOLDOWN_SECONDS: float = 0.5   # минимальный интервал между действиями
TEST_START_COOLDOWN_SECONDS: int = 3      # пауза между стартами тестов

# === Тесты ===
DEFAULT_TIME_PER_QUESTION: int = 30       # сек на вопрос по умолчанию
MAX_PAUSE_MISS_COUNT: int = 2             # после скольких пропусков ставить на паузу
MAX_OPTIONS_PER_QUESTION: int = 10
MIN_OPTIONS_PER_QUESTION: int = 2

# === Дуэли ===
DUEL_QUEUE_TIMEOUT_SECONDS: int = 60      # макс. время ожидания соперника
DUEL_QUESTIONS_COUNT: int = 10
DUEL_TIME_PER_QUESTION: int = 20
DUEL_SCORE_PER_QUESTION: int = 100
DUEL_SPEED_BONUS_MAX: int = 50

# === Daily ENT ===
DAILY_DEFAULT_QUESTIONS: int = 10

# === Группы ===
GROUP_MIN_PLAYERS: int = 2
GROUP_JOIN_TIMEOUT: int = 30              # сек на сбор участников

# === Логирование ===
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE: str = os.getenv("LOG_FILE", str(BASE_DIR / "bot.log"))

# === Telegram ===
# protect_content=True не позволяет пересылать/копировать сообщения средствами Telegram.
# Это НЕ защищает от скриншотов - Telegram Bot API не предоставляет такой возможности.
PROTECT_CONTENT: bool = True

# === Inline mode ===
INLINE_CACHE_TIME: int = 5

# === Имя бота для deep link (без @). Заполнится автоматически при запуске. ===
BOT_USERNAME: str = ""
