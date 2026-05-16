"""Сервис конспектов."""
from typing import Optional

import database as db
import utils


def create_note(title: str, description: str, subject: str, category: str,
                language: str, access_type: str, price: int, created_by: int) -> int:
    db.execute("""INSERT INTO notes (title, description, subject, category, language,
                                     access_type, price, status, created_by, created_at)
                  VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
               (title, description, subject, category, language,
                access_type, price, created_by, utils.now_iso()))
    return db.fetchone("SELECT last_insert_rowid() AS id")['id']


def add_page(note_id: int, content: str, order_num: int, image_file_id: str = None) -> int:
    db.execute("""INSERT INTO note_pages (note_id, content, image_file_id, page_number)
                  VALUES (?, ?, ?, ?)""",
               (note_id, content, image_file_id, order_num))
    return db.fetchone("SELECT last_insert_rowid() AS id")['id']


def list_active_notes(language: str = None) -> list[dict]:
    if language:
        rows = db.fetchall(
            "SELECT * FROM notes WHERE status='active' AND language=? ORDER BY id DESC",
            (language,))
    else:
        rows = db.fetchall("SELECT * FROM notes WHERE status='active' ORDER BY id DESC")
    return [dict(r) for r in rows]


def get_note(note_id: int) -> Optional[dict]:
    r = db.fetchone("SELECT * FROM notes WHERE id=?", (note_id,))
    return dict(r) if r else None


def get_pages(note_id: int) -> list[dict]:
    rows = db.fetchall(
        "SELECT * FROM note_pages WHERE note_id=? ORDER BY page_number", (note_id,))
    return [dict(r) for r in rows]


def get_page(note_id: int, order_num: int) -> Optional[dict]:
    r = db.fetchone(
        "SELECT * FROM note_pages WHERE note_id=? AND page_number=?",
        (note_id, order_num))
    return dict(r) if r else None


def user_has_note_access(user_id: int, note: dict) -> bool:
    if note['access_type'] == 'free':
        return True
    if utils.is_premium(user_id):
        return True
    if note['access_type'] == 'premium':
        return False
    # paid
    r = db.fetchone("SELECT 1 FROM paid_access WHERE user_id=? AND note_id=?",
                    (user_id, note['id']))
    return bool(r)


def update_progress(user_id: int, note_id: int, last_page: int):
    existing = db.fetchone(
        "SELECT id FROM user_notes_progress WHERE user_id=? AND note_id=?",
        (user_id, note_id))
    if existing:
        db.execute(
            "UPDATE user_notes_progress SET last_page=?, updated_at=? WHERE id=?",
            (last_page, utils.now_iso(), existing['id']))
    else:
        db.execute(
            """INSERT INTO user_notes_progress (user_id, note_id, last_page,
                                                homework_score, updated_at)
               VALUES (?, ?, ?, NULL, ?)""",
            (user_id, note_id, last_page, utils.now_iso()))


def get_progress(user_id: int, note_id: int) -> Optional[dict]:
    r = db.fetchone(
        "SELECT * FROM user_notes_progress WHERE user_id=? AND note_id=?",
        (user_id, note_id))
    return dict(r) if r else None


def grant_paid_note(user_id: int, note_id: int):
    db.execute("""INSERT OR IGNORE INTO paid_access (user_id, note_id, granted_at)
                  VALUES (?, ?, ?)""",
               (user_id, note_id, utils.now_iso()))


def delete_note(note_id: int):
    db.execute("DELETE FROM note_pages WHERE note_id=?", (note_id,))
    db.execute("DELETE FROM note_homeworks WHERE note_id=?", (note_id,))
    db.execute("DELETE FROM user_notes_progress WHERE note_id=?", (note_id,))
    db.execute("DELETE FROM paid_access WHERE note_id=?", (note_id,))
    db.execute("DELETE FROM notes WHERE id=?", (note_id,))


def link_homework(note_id: int, homework_type: str, linked_test_id: int = None,
                  open_task_text: str = None, open_task_keywords: str = None):
    db.execute("""INSERT OR REPLACE INTO note_homeworks
                    (note_id, homework_type, test_id, open_task_prompt,
                     open_task_keywords, auto_check_enabled, created_at)
                  VALUES (?, ?, ?, ?, ?, 1, ?)""",
               (note_id, homework_type, linked_test_id, open_task_text or '',
                open_task_keywords or '', utils.now_iso()))


def get_homework(note_id: int) -> Optional[dict]:
    r = db.fetchone("SELECT * FROM note_homeworks WHERE note_id=? ORDER BY id DESC LIMIT 1",
                    (note_id,))
    return dict(r) if r else None
