"""
FSM-состояния для пошагового взаимодействия пользователя/админа с ботом.
"""
from aiogram.fsm.state import State, StatesGroup


class CommonStates(StatesGroup):
    """Общие состояния."""
    choosing_language = State()
    set_school = State()


class TestCreateStates(StatesGroup):
    """Мастер создания теста админом."""
    title = State()
    description = State()
    subject = State()
    grade = State()
    category = State()
    language = State()
    test_type = State()
    time_per_question = State()
    attempts_limit = State()
    first_attempt_only = State()
    is_paid = State()
    price = State()
    shuffle_questions = State()
    shuffle_options = State()
    show_correct = State()
    show_explanation = State()
    required_channel = State()


class TextImportStates(StatesGroup):
    """Импорт вопросов текстом."""
    waiting_questions = State()


class PollImportStates(StatesGroup):
    """Импорт через Quiz Poll."""
    waiting_polls = State()


class DraftFixStates(StatesGroup):
    """Доуказание правильного ответа для черновиков."""
    waiting_choice = State()


class GrantAccessStates(StatesGroup):
    waiting_user = State()
    waiting_test = State()


class PremiumStates(StatesGroup):
    waiting_user = State()
    waiting_days = State()


class BlockStates(StatesGroup):
    waiting_user = State()


class ChannelStates(StatesGroup):
    waiting_username = State()


class NoteCreateStates(StatesGroup):
    title = State()
    description = State()
    subject = State()
    category = State()
    language = State()
    access_type = State()
    price = State()
    pages = State()


class HomeworkStates(StatesGroup):
    """Текстовое ДЗ - пользователь отправляет открытый ответ."""
    waiting_open_answer = State()


class TestRunStates(StatesGroup):
    """Прохождение теста в личке."""
    running = State()
    paused = State()


class DuelStates(StatesGroup):
    """Дуэли."""
    searching = State()
    in_duel = State()
