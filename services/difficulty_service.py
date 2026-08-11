"""
Анализ сложности тестов и вопросов на основе реальных ответов учеников.
Определяет тесты/вопросы где чаще всего ошибаются.
Никакого ИИ — чистая статистика по attempt_answers.
"""
import logging

import database as db

log = logging.getLogger(__name__)

MIN_ANSWERS_FOR_ANALYSIS = 5   # минимум ответов чтобы вопрос считался


def get_hardest_questions(limit: int = 10, language: str = None,
                           category_id: int = None) -> list:
    """
    Вопросы с самым высоким процентом ошибок.
    Считаются только вопросы где было >= MIN_ANSWERS_FOR_ANALYSIS ответов.
    """
    cat_filter = ""
    params = [MIN_ANSWERS_FOR_ANALYSIS]
    if language:
        cat_filter += " AND t.language=?"
        params.append(language)
    if category_id:
        cat_filter += " AND t.category_id=?"
        params.append(category_id)

    rows = db.fetchall(f"""
        SELECT q.id AS qid, q.text AS qtext, q.test_id,
               t.title AS test_title,
               COUNT(a.id) AS total_answers,
               SUM(CASE WHEN a.is_correct=0 THEN 1 ELSE 0 END) AS wrong_answers,
               ROUND(100.0 * SUM(CASE WHEN a.is_correct=0 THEN 1 ELSE 0 END) / COUNT(a.id), 1) AS error_pct
        FROM attempt_answers a
        JOIN questions q ON q.id = a.question_id
        JOIN tests t ON t.id = q.test_id
        WHERE a.skipped=0 {cat_filter}
        GROUP BY q.id
        HAVING total_answers >= ?
        ORDER BY error_pct DESC, total_answers DESC
        LIMIT {int(limit)}
    """, tuple(params[1:]) + (params[0],) if False else tuple(params))
    return [dict(r) for r in rows]


def get_hardest_tests(limit: int = 10, language: str = None) -> list:
    """
    Тесты с самым высоким средним процентом ошибок.
    """
    lang_filter = ""
    params = []
    if language:
        lang_filter = " AND t.language=?"
        params.append(language)

    rows = db.fetchall(f"""
        SELECT t.id AS test_id, t.title, t.category_id,
               COUNT(a.id) AS total_answers,
               SUM(CASE WHEN a.is_correct=0 THEN 1 ELSE 0 END) AS wrong_answers,
               ROUND(100.0 * SUM(CASE WHEN a.is_correct=0 THEN 1 ELSE 0 END) / COUNT(a.id), 1) AS error_pct,
               (SELECT COUNT(*) FROM questions WHERE test_id=t.id) AS q_count
        FROM attempt_answers a
        JOIN questions q ON q.id = a.question_id
        JOIN tests t ON t.id = q.test_id
        WHERE a.skipped=0 AND t.status='active'
          AND COALESCE(t.is_private,0)=0 {lang_filter}
        GROUP BY t.id
        HAVING total_answers >= {MIN_ANSWERS_FOR_ANALYSIS}
        ORDER BY error_pct DESC, total_answers DESC
        LIMIT {int(limit)}
    """, tuple(params))
    return [dict(r) for r in rows]


def build_hardest_report(language: str = 'ru') -> str:
    """Текстовый отчёт по сложным тестам и вопросам (для админа)."""
    tests = get_hardest_tests(limit=10, language=language)
    questions = get_hardest_questions(limit=10, language=language)

    lines = ["📊 <b>Анализ сложности (по ответам учеников)</b>\n"]
    if tests:
        lines.append("🔴 <b>Самые сложные тесты:</b>")
        for i, t in enumerate(tests, 1):
            lines.append(
                f"{i}. «{t['title']}» — ошибок {t['error_pct']}% "
                f"({t['wrong_answers']}/{t['total_answers']})")
    else:
        lines.append("<i>Пока мало данных для анализа тестов.</i>")

    lines.append("")
    if questions:
        lines.append("🔴 <b>Самые сложные вопросы:</b>")
        for i, q in enumerate(questions[:10], 1):
            qtext = (q['qtext'] or '')[:60]
            lines.append(
                f"{i}. {qtext}… — ошибок {q['error_pct']}% "
                f"(тест «{q['test_title']}»)")
    else:
        lines.append("<i>Пока мало данных для анализа вопросов.</i>")

    return "\n".join(lines)


def get_hardest_test_ids(limit: int = 10, language: str = None) -> list:
    """ID самых сложных тестов — для автопубликации трудных заданий."""
    tests = get_hardest_tests(limit=limit, language=language)
    return [t['test_id'] for t in tests]
