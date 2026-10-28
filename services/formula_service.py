"""
Генератор картинок формул из LaTeX.
Из txt с формулами (по одной на строку) → PNG на белом фоне → ZIP.
Использует matplotlib mathtext (LaTeX-подмножество), без установки полного LaTeX.
"""
import os
import io
import re
import zipfile
import logging

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

log = logging.getLogger(__name__)

OUT_DIR = "/tmp/formula_img"
os.makedirs(OUT_DIR, exist_ok=True)


def _convert_cases(latex: str) -> str:
    """
    matplotlib mathtext не знает \\begin{cases}.
    Превращаем систему в стопку строк через \\genfrac или просто переносы.
    \\begin{cases} A \\\\ B \\end{cases} → используем \\left\\{ ... \\right.
    с массивом — но mathtext и array не знает. Поэтому возвращаем спец-маркер.
    """
    return latex


def render_formula(latex: str, out_path: str) -> bool:
    """Отрендерить одну LaTeX-формулу в PNG на белом фоне."""
    latex = latex.strip().strip('$').strip()
    if not latex:
        return False

    # Системы уравнений \begin{cases}...\end{cases} — рисуем вручную
    cases_match = re.search(r'\\begin\{cases\}(.+?)\\end\{cases\}', latex, re.DOTALL)
    if cases_match:
        return _render_cases(latex, cases_match, out_path)

    try:
        width = max(3, min(14, len(latex) * 0.25))
        fig = plt.figure(figsize=(width, 1.8))
        fig.patch.set_facecolor('white')
        fig.text(0.5, 0.5, f'${latex}$', fontsize=30,
                 ha='center', va='center', color='black')
        plt.savefig(out_path, dpi=150, bbox_inches='tight',
                    facecolor='white', pad_inches=0.3)
        plt.close(fig)
        return True
    except Exception as e:
        log.warning("render formula '%s': %s", latex[:50], e)
        try:
            plt.close('all')
        except Exception:
            pass
        return False


def _render_cases(latex, cases_match, out_path: str) -> bool:
    """Отрисовать систему уравнений с фигурной скобкой { вручную."""
    try:
        inner = cases_match.group(1)
        before = latex[:cases_match.start()].strip()
        after = latex[cases_match.end():].strip()
        # Уравнения разделены \\ — но в тексте может быть 1 или 2 бэкслеша
        eqs = [e.strip() for e in re.split(r'\\{1,}', inner) if e.strip()]

        n = len(eqs)
        fig = plt.figure(figsize=(7, 0.8 * n + 1.2))
        fig.patch.set_facecolor('white')

        # Большая фигурная скобка слева
        y_top = 0.75
        y_bot = 0.25
        y_mid = (y_top + y_bot) / 2
        fig.text(0.18, y_mid, '{', fontsize=40 + n * 12,
                 ha='center', va='center', color='black')

        # Уравнения справа от скобки, стопкой
        for i, eq in enumerate(eqs):
            y = y_top - (i / max(1, n - 1)) * (y_top - y_bot) if n > 1 else y_mid
            fig.text(0.28, y, f'${eq}$', fontsize=24,
                     ha='left', va='center', color='black')
        # Подпись сверху если был текст
        if before:
            fig.text(0.5, 0.95, before, fontsize=20, ha='center', va='top')
        plt.savefig(out_path, dpi=150, bbox_inches='tight',
                    facecolor='white', pad_inches=0.3)
        plt.close(fig)
        return True
    except Exception as e:
        log.warning("render cases: %s", e)
        try:
            plt.close('all')
        except Exception:
            pass
        return False


def parse_formulas_txt(text: str) -> list:
    """
    Разобрать txt. Каждая непустая строка = одна формула.
    Строки начинающиеся с # — комментарии (игнор).
    Поддержка подписи: 'формула | подпись' — подпись пойдёт в имя файла.
    """
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        label = None
        if '|' in line:
            parts = line.split('|', 1)
            line = parts[0].strip()
            label = parts[1].strip()
        out.append({'latex': line, 'label': label})
    return out


def generate_zip(formulas: list) -> tuple:
    """
    Сгенерировать ZIP с картинками.
    Возвращает (zip_path, ok_count, failed_list).
    """
    import time
    ts = int(time.time())
    zip_path = os.path.join(OUT_DIR, f"formulas_{ts}.zip")
    ok = 0
    failed = []
    paths = []
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for i, f in enumerate(formulas, 1):
            img_path = os.path.join(OUT_DIR, f"f_{ts}_{i}.png")
            if render_formula(f['latex'], img_path):
                arcname = f"{i}.png"
                zf.write(img_path, arcname)
                paths.append(img_path)
                ok += 1
            else:
                failed.append((i, f['latex']))
    return zip_path, ok, failed, paths


def render_single_to_bytes(latex: str) -> bytes:
    """Отрендерить одну формулу и вернуть bytes PNG (для предпросмотра)."""
    tmp = os.path.join(OUT_DIR, "preview.png")
    if render_formula(latex, tmp):
        with open(tmp, 'rb') as f:
            return f.read()
    return b''


# ===================== АВТО-ОПРЕДЕЛЕНИЕ ФОРМУЛ В ТЕКСТЕ =====================

# Признаки что в тексте есть математика, которую стоит отрисовать
_MATH_MARKERS = [
    r'\$',           # $...$
    r'\\frac', r'\\sqrt', r'\\sum', r'\\int', r'\\begin\{cases\}',
    r'\^\{', r'_\{',  # степени/индексы в фигурных
    r'\\times', r'\\div', r'\\pm', r'\\leq', r'\\geq', r'\\neq',
    r'\\alpha', r'\\beta', r'\\pi', r'\\theta', r'\\sin', r'\\cos',
]
import re as _re2
_MATH_RE = _re2.compile('|'.join(_MATH_MARKERS))


def has_math(text: str) -> bool:
    """Есть ли в тексте формула которую стоит отрисовать картинкой."""
    if not text:
        return False
    return bool(_MATH_RE.search(text))


def _extract_latex(text: str) -> str:
    """
    Достать LaTeX из текста.
    Если есть $...$ — берём содержимое (может быть несколько).
    Иначе — весь текст как формулу (если в нём LaTeX-команды).
    """
    # Все куски в $...$
    parts = _re2.findall(r'\$(.+?)\$', text, _re2.DOTALL)
    if parts:
        # Объединяем найденные формулы, текст между ними как \text
        return r'\quad '.join(p.strip() for p in parts)
    # Нет долларов — но есть LaTeX команды → весь текст формула
    return text.strip()


def render_question_image(text: str, out_path: str) -> bool:
    """
    Отрисовать текст вопроса с формулой в картинку.
    Кириллица — обычным шрифтом, формулы ($...$) — математическим.
    Каждый сегмент на своей строке для читаемости.
    """
    text = text.strip()
    if not text:
        return False
    # Если в тексте есть система cases — рендерим всю формулу через render_formula
    if '\\begin{cases}' in text:
        latex = _extract_latex(text)
        return render_formula(latex, out_path)
    try:
        # Разбиваем на сегменты: обычный текст и $формулы$
        segments = _re2.split(r'(\$.+?\$)', text)
        lines_to_draw = []  # (content, is_formula)
        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue
            if seg.startswith('$') and seg.endswith('$'):
                lines_to_draw.append((seg.strip('$').strip(), True))
            else:
                lines_to_draw.append((seg, False))
        if not lines_to_draw:
            return False

        n = len(lines_to_draw)
        fig_h = 0.9 * n + 0.6
        fig_w = 8
        fig = plt.figure(figsize=(fig_w, fig_h))
        fig.patch.set_facecolor('white')

        # Рисуем каждый сегмент своей строкой сверху вниз
        y_step = 1.0 / (n + 1)
        for i, (content, is_formula) in enumerate(lines_to_draw):
            y = 1.0 - (i + 1) * y_step
            if is_formula:
                txt = f'${content}$'
            else:
                txt = content  # обычный текст — кириллица ОК
            fig.text(0.5, y, txt, fontsize=22, ha='center', va='center',
                     color='black', wrap=True)
        plt.savefig(out_path, dpi=150, bbox_inches='tight',
                    facecolor='white', pad_inches=0.3)
        plt.close(fig)
        return True
    except Exception as e:
        log.warning("render_question_image '%s': %s", text[:40], e)
        try:
            plt.close('all')
        except Exception:
            pass
        latex = _extract_latex(text)
        return render_formula(latex, out_path)


# Кэш отрисованных формул (text hash -> file_id), чтобы не рендерить заново
_render_cache = {}


def get_cached_file_id(text: str):
    import hashlib
    key = hashlib.md5(text.encode()).hexdigest()
    return _render_cache.get(key)


def set_cached_file_id(text: str, file_id: str):
    import hashlib
    key = hashlib.md5(text.encode()).hexdigest()
    _render_cache[key] = file_id
