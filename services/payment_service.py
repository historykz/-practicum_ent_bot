"""
Платежи Telegram Stars: покупка теста, раздела (−20% при 5+ платных),
подарки, повторы ошибок, доступы, статистика продаж, возвраты.
"""
import json
import logging
from typing import Optional

import database as db

log = logging.getLogger(__name__)

SECTION_DISCOUNT = 0.20          # скидка на раздел
SECTION_MIN_PAID_TESTS = 5       # от скольки платных тестов показывать скидку
REDO_PRICE_STARS = 5             # цена повтора ошибок (после 1 бесплатного)


# ===================== ДОСТУП =====================

def has_paid_access(test_id: int, user_tg_id: int) -> bool:
    """Куплен ли тест (напрямую, разделом или подарен)."""
    t = db.fetchone("SELECT category_id FROM tests WHERE id=?", (test_id,))
    cat_id = t.get('category_id') if t else None
    r = db.fetchone(
        """SELECT id FROM purchases
           WHERE test_id=? AND kind IN ('test','gift')
             AND (user_tg_id=? OR gifted_to_tg_id=?) LIMIT 1""",
        (test_id, user_tg_id, user_tg_id))
    if r:
        return True
    if cat_id:
        r = db.fetchone(
            """SELECT id FROM purchases
               WHERE category_id=? AND kind='category'
                 AND (user_tg_id=? OR gifted_to_tg_id=?) LIMIT 1""",
            (cat_id, user_tg_id, user_tg_id))
        if r:
            return True
    return False


def grant_purchase(user_tg_id: int, kind: str, stars: int,
                    charge_id: str = None, test_id: int = None,
                    category_id: int = None,
                    gifted_to: int = None) -> int:
    """Записать покупку. Возвращает id записи."""
    cur = db.execute(
        """INSERT INTO purchases
           (user_tg_id, kind, test_id, category_id, gifted_to_tg_id,
            stars_amount, charge_id)
           VALUES (?,?,?,?,?,?,?)""",
        (user_tg_id, kind, test_id, category_id, gifted_to,
         stars, charge_id))
    return cur.lastrowid


# ===================== ЦЕНЫ РАЗДЕЛА =====================

def get_section_offer(category_id: int, user_tg_id: int) -> Optional[dict]:
    """
    Если в разделе >= SECTION_MIN_PAID_TESTS платных тестов —
    вернуть предложение со скидкой. Иначе None.
    """
    rows = db.fetchall(
        "SELECT id, price_stars FROM tests "
        "WHERE category_id=? AND COALESCE(price_stars,0) > 0 "
        "AND status='active'", (category_id,))
    if len(rows) < SECTION_MIN_PAID_TESTS:
        return None
    total = sum(r['price_stars'] for r in rows)
    discounted = int(total * (1 - SECTION_DISCOUNT))
    if discounted < 1:
        return None
    cat = db.fetchone("SELECT name, emoji FROM test_categories WHERE id=?",
                       (category_id,))
    return {
        'category_id': category_id,
        'name': (cat.get('name') if cat else '') or 'Раздел',
        'emoji': (cat.get('emoji') if cat else '') or '📚',
        'tests_count': len(rows),
        'full_price': total,
        'price': discounted,
    }


# ===================== ПОВТОРЫ =====================

def count_redos_used(user_tg_id: int, test_id: int) -> int:
    """Сколько повторов ошибок юзер уже делал по этому тесту."""
    u = db.fetchone("SELECT id FROM users WHERE tg_id=?", (user_tg_id,))
    if not u:
        return 0
    r = db.fetchone(
        "SELECT COUNT(*) AS c FROM test_attempts "
        "WHERE user_id=? AND test_id=? AND attempt_num=999",
        (u['id'], test_id))
    return (r['c'] if r else 0) or 0


# ===================== СТАТИСТИКА =====================

def sales_stats() -> dict:
    def cnt(sql, params=()):
        r = db.fetchone(sql, params)
        return (r['c'] if r else 0) or 0
    def total(sql, params=()):
        r = db.fetchone(sql, params)
        return (r['s'] if r else 0) or 0
    return {
        'tests': cnt("SELECT COUNT(*) AS c FROM purchases WHERE kind='test'"),
        'categories': cnt("SELECT COUNT(*) AS c FROM purchases WHERE kind='category'"),
        'gifts': cnt("SELECT COUNT(*) AS c FROM purchases WHERE kind='gift'"),
        'redos': cnt("SELECT COUNT(*) AS c FROM purchases WHERE kind='redo'"),
        'redo_stars': total("SELECT SUM(stars_amount) AS s FROM purchases WHERE kind='redo'"),
        'total_stars': total("SELECT SUM(stars_amount) AS s FROM purchases"),
    }


def user_purchases(user_tg_id: int) -> list:
    """Покупки юзера (для «Мои покупки»)."""
    return db.fetchall(
        """SELECT p.*, t.title AS test_title, c.name AS cat_name, c.emoji AS cat_emoji
           FROM purchases p
           LEFT JOIN tests t ON t.id = p.test_id
           LEFT JOIN test_categories c ON c.id = p.category_id
           WHERE p.user_tg_id=? OR p.gifted_to_tg_id=?
           ORDER BY p.id DESC LIMIT 50""",
        (user_tg_id, user_tg_id))


def find_purchase_by_charge(charge_id: str) -> Optional[dict]:
    return db.fetchone("SELECT * FROM purchases WHERE charge_id=?", (charge_id,))
