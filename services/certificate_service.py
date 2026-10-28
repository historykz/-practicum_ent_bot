"""
Грамота и итоговый документ в PDF: страница 1 — грамота с QR-кодом проверки,
страница 2 — итоговый документ о прохождении курса и вознаграждении.

Рисуем через Pillow. Шрифты DejaVu берём из matplotlib (он уже в
requirements.txt): так кириллица гарантированно есть и на сервере, где
системных шрифтов может не быть.
"""
import io
import os
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from services import qr_code

W, H = 1754, 1240                     # A4 альбомная, 150 dpi
GOLD = (190, 150, 40)
GOLD_LIGHT = (232, 205, 120)
INK = (40, 34, 24)
MUTED = (110, 100, 85)
PAPER = (252, 248, 236)


def _font_paths() -> dict:
    out = {}
    try:
        import matplotlib
        d = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
        for key, name in (("serif", "DejaVuSerif.ttf"), ("serif_bold", "DejaVuSerif-Bold.ttf"),
                          ("serif_italic", "DejaVuSerif-Italic.ttf"), ("sans", "DejaVuSans.ttf"),
                          ("sans_bold", "DejaVuSans-Bold.ttf")):
            p = os.path.join(d, name)
            if os.path.exists(p):
                out[key] = p
    except Exception:
        pass
    for key, p in (("serif", "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"),
                   ("serif_bold", "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"),
                   ("sans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                   ("sans_bold", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")):
        if key not in out and os.path.exists(p):
            out[key] = p
    return out


_PATHS = None


def _font(kind: str, size: int):
    global _PATHS
    if _PATHS is None:
        _PATHS = _font_paths()
    path = _PATHS.get(kind) or _PATHS.get(kind.split("_")[0]) or _PATHS.get("sans")
    if path:
        return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def fonts_ok() -> bool:
    global _PATHS
    if _PATHS is None:
        _PATHS = _font_paths()
    return "serif_bold" in _PATHS and "sans" in _PATHS


def _wrap(draw, text: str, font, width: int) -> list:
    words, lines, cur = (text or "").split(), [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if draw.textlength(test, font=font) <= width or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _center(draw, y: int, text: str, font, fill=INK, width: int = W) -> int:
    tw = draw.textlength(text, font=font)
    draw.text(((width - tw) / 2, y), text, font=font, fill=fill)
    bbox = draw.textbbox((0, 0), text, font=font)
    return y + (bbox[3] - bbox[1])


def _fit(draw, text, kind, size, max_w, min_size=24):
    while size > min_size and draw.textlength(text, font=_font(kind, size)) > max_w:
        size -= 2
    return _font(kind, size)


def _frame(draw):
    draw.rectangle([0, 0, W, H], fill=PAPER)
    draw.rectangle([28, 28, W - 28, H - 28], outline=GOLD, width=14)
    draw.rectangle([56, 56, W - 56, H - 56], outline=GOLD_LIGHT, width=3)
    for (cx, cy) in ((56, 56), (W - 56, 56), (56, H - 56), (W - 56, H - 56)):
        draw.ellipse([cx - 22, cy - 22, cx + 22, cy + 22], fill=PAPER, outline=GOLD, width=5)
        draw.ellipse([cx - 8, cy - 8, cx + 8, cy + 8], fill=GOLD)


def _qr_block(img, draw, url: str, x: int, y: int, size: int = 250):
    q = qr_code.image(url, scale=8, border=2).resize((size, size), Image.NEAREST)
    img.paste(q, (x, y))
    f = _font("sans", 20)
    cap = "Проверить подлинность"
    draw.text((x + (size - draw.textlength(cap, font=f)) / 2, y + size + 8), cap, font=f, fill=MUTED)


def _money(tiyn, sign: bool = False) -> str:
    from services.reward_service import fmt
    return fmt(tiyn, sign=sign)


def page_certificate(c: dict, verify_url: str) -> Image.Image:
    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)
    _frame(d)
    y = _center(d, 120, "SMART ENT", _font("sans_bold", 30), fill=GOLD) + 26
    y = _center(d, y, "ГРАМОТА", _font("serif_bold", 118), fill=GOLD) + 40
    y = _center(d, y, "награждается", _font("serif_italic", 38), fill=MUTED) + 28
    name_font = _fit(d, c["full_name"], "serif_bold", 76, W - 360)
    y = _center(d, y, c["full_name"], name_font) + 36
    y = _center(d, y, "за успешное завершение курса", _font("serif", 36), fill=MUTED) + 16
    tf = _font("serif_bold", 44)
    for line in _wrap(d, f"«{c['subject_title']}»", tf, W - 420)[:2]:
        y = _center(d, y, line, tf) + 10
    y += 26
    sf = _font("sans", 30)
    days = c.get("days") or 0
    y = _center(d, y, f"Обучение: {c['started']} — {c['completed']}  ·  {days} дн.  ·  "
                      f"уроков пройдено: {c.get('lessons') or 0}", sf) + 14
    line2 = f"Итоговое вознаграждение: {_money(c.get('payout') or 0)}"
    if c.get("rank"):
        line2 += f"  ·  место в рейтинге: {c['rank']} из {c.get('rank_total') or c['rank']}"
    _center(d, y, line2, _font("sans_bold", 30))
    # низ: реквизиты слева, печать и QR справа
    rf = _font("sans", 24)
    by = H - 250
    for i, t in enumerate((f"Серийный номер: {c['serial']}", f"ID ученика: {c['tg_id']}",
                           f"Дата выдачи: {c['issued']}")):
        d.text((110, by + i * 38), t, font=rf, fill=INK)
    sx, sy = W - 640, H - 300
    d.ellipse([sx, sy, sx + 190, sy + 190], outline=GOLD, width=6)
    d.ellipse([sx + 16, sy + 16, sx + 174, sy + 174], outline=GOLD_LIGHT, width=2)
    sf2 = _font("sans_bold", 24)
    for i, t in enumerate(("SMART", "ENT", "✓")):
        tw = d.textlength(t, font=sf2)
        d.text((sx + 95 - tw / 2, sy + 52 + i * 32), t, font=sf2, fill=GOLD)
    _qr_block(img, d, verify_url, W - 380, H - 330)
    return img


def page_statement(c: dict, verify_url: str) -> Image.Image:
    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)
    d.rectangle([28, 28, W - 28, H - 28], outline=GOLD, width=6)
    y = _center(d, 80, f"ИТОГОВЫЙ ДОКУМЕНТ № {c['serial']}", _font("serif_bold", 50)) + 14
    y = _center(d, y, "о прохождении курса и вознаграждении", _font("serif_italic", 32), fill=MUTED) + 40
    rows = [
        ("ФИО", c["full_name"]),
        ("ID ученика (Telegram)", str(c["tg_id"])),
        ("Предмет", c["subject_title"]),
        ("Дата начала обучения", c["started"]),
        ("Дата завершения", c["completed"]),
        ("Срок прохождения курса", f"{c.get('days') or 0} дн."),
        ("Пройдено уроков", str(c.get("lessons") or 0)),
        ("Начислено за уроки", _money(c.get("earned") or 0)),
        ("Штрафы за пропуски", _money(c.get("penalties") or 0)),
        ("Бонусы и корректировки", _money(c.get("adjustments") or 0, sign=True)),
        ("Итоговый баланс", _money(c.get("balance", c.get("payout") or 0))),
        ("К выплате", _money(c.get("payout") or 0)),
    ]
    if c.get("rank"):
        rows.append(("Место в рейтинге", f"{c['rank']} из {c.get('rank_total') or c['rank']}"))
    lf, vf = _font("sans", 28), _font("sans_bold", 28)
    x0, x1, x2 = 120, 700, W - 440
    yy = y
    for i, (k, v) in enumerate(rows):
        # Длинное значение (название курса, ФИО) переносим, а не обрезаем:
        # в итоговом документе не должно пропадать ни слова.
        lines = _wrap(d, v, vf, x2 - x1 - 20) if v else ["—"]
        if len(lines) > 2:
            small = _font("sans_bold", 23)
            lines = _wrap(d, v, small, x2 - x1 - 20)
            font, step = small, 30
        else:
            font, step = vf, 38
        h = max(54, 16 + step * len(lines))
        if i % 2 == 0:
            d.rectangle([x0 - 16, yy - 8, x2, yy + h - 10], fill=(246, 239, 219))
        d.text((x0, yy), k, font=lf, fill=MUTED)
        for j, line in enumerate(lines):
            d.text((x1, yy + j * step), line, font=font, fill=INK)
        yy += h
    note_y = yy + 26
    if (c.get("balance", 0) or 0) < 0:
        d.text((x0, note_y), "Отрицательный баланс не является долгом ученика: к выплате 0 ₸.",
               font=_font("sans", 24), fill=MUTED)
        note_y += 36
    d.text((x0, note_y), f"Документ сформирован {c['issued']} платформой Smart ENT. "
                         f"Подлинность проверяется по QR-коду или по номеру {c['serial']}.",
           font=_font("sans", 22), fill=MUTED)
    _qr_block(img, d, verify_url, W - 380, 300)
    return img


def render_pdf(cert: dict, verify_url: str) -> bytes:
    from services.reward_service import fmt_date
    c = dict(cert)
    c["started"] = fmt_date(cert.get("started_at"))
    c["completed"] = fmt_date(cert.get("completed_at"))
    c["issued"] = fmt_date(cert.get("issued_at"))
    c["balance"] = (cert.get("earned") or 0) + (cert.get("penalties") or 0) + (cert.get("adjustments") or 0)
    p1, p2 = page_certificate(c, verify_url), page_statement(c, verify_url)
    buf = io.BytesIO()
    p1.save(buf, "PDF", save_all=True, append_images=[p2], resolution=150)
    return buf.getvalue()


def render_png(cert: dict, verify_url: str) -> bytes:
    """Первая страница картинкой — для предпросмотра и проверки QR."""
    from services.reward_service import fmt_date
    c = dict(cert)
    c["started"] = fmt_date(cert.get("started_at"))
    c["completed"] = fmt_date(cert.get("completed_at"))
    c["issued"] = fmt_date(cert.get("issued_at"))
    buf = io.BytesIO()
    page_certificate(c, verify_url).save(buf, "PNG")
    return buf.getvalue()
