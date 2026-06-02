"""
Импорт вопросов из ZIP-архива с картинками.
Структура ZIP:
  questions.txt  — вопросы с метками [img:файл.png]
  images/        — картинки (от нейронки)

Формат questions.txt:
  [img:q1.png]            <- картинка вопроса (опц.)
  Текст вопроса
  A) текст или [img:a.png]
  B) [img:b.png] *        <- звёздочка = правильный
  ...
  (пустая строка между вопросами)

Бот НЕ генерирует картинки — берёт готовые из архива.
Если у вариантов есть картинки — склеивает их в одно фото A/B/C/D.
"""
import os
import io
import re
import zipfile
import logging

import database as db

log = logging.getLogger(__name__)

IMPORT_DIR = "/tmp/zip_import"
os.makedirs(IMPORT_DIR, exist_ok=True)

_IMG_RE = re.compile(r'\[img:([^\]]+)\]', re.IGNORECASE)
_OPT_RE = re.compile(r'^([A-EА-Е])\)\s*(.*)$')


def parse_zip(zip_bytes: bytes) -> tuple:
    """
    Разобрать ZIP. Возвращает (questions, images_dict, errors).
    questions: [{'text','q_image','options':[{'text','image','correct'}]}]
    images_dict: {filename: bytes}
    """
    errors = []
    images = {}
    txt_content = None

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except Exception as e:
        return [], {}, [f"Не открыть ZIP: {e}"]

    for name in zf.namelist():
        low = name.lower()
        if low.endswith('.txt'):
            try:
                txt_content = zf.read(name).decode('utf-8', errors='ignore')
            except Exception as e:
                errors.append(f"Чтение {name}: {e}")
        elif low.endswith(('.png', '.jpg', '.jpeg', '.webp')):
            # Сохраняем по базовому имени файла
            base = os.path.basename(name)
            try:
                images[base] = zf.read(name)
            except Exception as e:
                errors.append(f"Картинка {name}: {e}")
    zf.close()

    if not txt_content:
        return [], images, errors + ["В архиве нет .txt файла с вопросами."]

    questions = _parse_txt(txt_content, errors)
    return questions, images, errors


def _parse_txt(text: str, errors: list) -> list:
    """Разобрать текст на вопросы."""
    questions = []
    blocks = re.split(r'\n\s*\n', text.replace('\r\n', '\n'))
    for bi, block in enumerate(blocks, 1):
        lines = [l for l in block.split('\n') if l.strip()]
        if not lines:
            continue
        q = {'text': '', 'q_image': None, 'options': []}
        q_text_lines = []
        i = 0
        # Текст вопроса (и картинка) — до первого варианта
        while i < len(lines):
            line = lines[i].strip()
            if _OPT_RE.match(line):
                break
            # картинка вопроса?
            m = _IMG_RE.search(line)
            if m:
                q['q_image'] = m.group(1).strip()
                # текст без метки
                rest = _IMG_RE.sub('', line).strip()
                if rest:
                    q_text_lines.append(rest)
            else:
                q_text_lines.append(line)
            i += 1
        q['text'] = ' '.join(q_text_lines).strip()

        # Варианты
        while i < len(lines):
            line = lines[i].strip()
            i += 1
            m = _OPT_RE.match(line)
            if not m:
                continue
            body = m.group(2).strip()
            is_correct = body.endswith('*')
            if is_correct:
                body = body[:-1].strip()
            img = None
            im = _IMG_RE.search(body)
            if im:
                img = im.group(1).strip()
                body = _IMG_RE.sub('', body).strip()
            q['options'].append({
                'text': body, 'image': img, 'correct': is_correct})

        if not q['text'] and not q['q_image']:
            errors.append(f"Блок {bi}: нет текста/картинки вопроса.")
            continue
        if len(q['options']) < 2:
            errors.append(f"Блок {bi}: меньше 2 вариантов.")
            continue
        if not any(o['correct'] for o in q['options']):
            errors.append(f"Блок {bi}: не отмечен правильный ответ (*).")
            continue
        questions.append(q)
    return questions


def merge_option_images(options: list, images: dict) -> bytes:
    """
    Склеить картинки вариантов в одну (A/B/C/D с подписями).
    Возвращает bytes PNG или b'' если не удалось.
    """
    from PIL import Image, ImageDraw, ImageFont
    letters = "ABCDE"
    items = []  # (letter, PIL.Image)
    for idx, o in enumerate(options):
        if o.get('image') and o['image'] in images:
            try:
                img = Image.open(io.BytesIO(images[o['image']])).convert("RGB")
                items.append((letters[idx], img))
            except Exception:
                pass
    if not items:
        return b''

    pad = 20
    label_w = 50
    max_w = max(img.width for _, img in items)
    total_h = sum(img.height for _, img in items) + pad * (len(items) + 1)
    canvas = Image.new("RGB", (max_w + label_w + pad * 2, total_h), "white")
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 28)
    except Exception:
        font = ImageFont.load_default()

    y = pad
    for letter, img in items:
        draw.text((pad, y + img.height // 2 - 14), f"{letter})",
                  fill="black", font=font)
        canvas.paste(img, (label_w + pad, y))
        y += img.height + pad

    out = io.BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()
