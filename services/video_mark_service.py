"""
Персональная метка на видео урока.

В Telegram видео уходит с protect_content: переслать и сохранить нельзя, на
Android заблокирован и снимок экрана. Вшить номер ученика прямо в кадры
можно только перекодированием — это делается, если на сервере есть ffmpeg
(на Railway: переменная окружения NIXPACKS_PKGS=ffmpeg). Без ffmpeg номер
идёт подписью под видео, а на сайте — полупрозрачной сеткой поверх плеера.
"""
import asyncio
import io
import logging
import os
import shutil
import subprocess
import tempfile
from typing import Optional

log = logging.getLogger(__name__)

MAX_SOURCE_BYTES = 45 * 1024 * 1024     # Bot API: скачать ≤20 МБ по file_id*, отдать ≤50 МБ
MAX_OUTPUT_BYTES = 49 * 1024 * 1024
ENCODE_TIMEOUT = 600                     # секунд на перекодирование


def available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _font_candidates():
    return (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
    )


def overlay_png(label: str, w: int, h: int) -> bytes:
    """Прозрачный PNG размером с кадр: три диагональные надписи."""
    from PIL import Image, ImageDraw, ImageFont
    size = max(18, min(w, h) // 16)
    font = None
    for path in _font_candidates():
        try:
            font = ImageFont.truetype(path, size)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    box = probe.textbbox((0, 0), label, font=font)
    tw, th = max(1, box[2] - box[0]), max(1, box[3] - box[1])
    txt = Image.new("RGBA", (tw + 20, th + 20), (0, 0, 0, 0))
    ImageDraw.Draw(txt).text((10, 10), label, font=font, fill=(255, 255, 255, 110))
    rot = txt.rotate(24, expand=True)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    for fx, fy in ((0.05, 0.10), (0.35, 0.42), (0.10, 0.74)):
        layer.paste(rot, (int(w * fx), int(h * fy)), rot)
    out = io.BytesIO()
    layer.save(out, format="PNG")
    return out.getvalue()


def _probe_size(path: str) -> Optional[tuple]:
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=60)
        w, h = res.stdout.strip().split(",")[:2]
        return int(w), int(h)
    except Exception as e:
        log.warning("ffprobe: %s", e)
        return None


def burn_in(src_path: str, label: str) -> Optional[str]:
    """Перекодировать видео с меткой. Путь к результату или None."""
    size = _probe_size(src_path)
    if not size:
        return None
    w, h = size
    workdir = tempfile.mkdtemp(prefix="vmark_")
    png = os.path.join(workdir, "mark.png")
    out = os.path.join(workdir, "marked.mp4")
    with open(png, "wb") as f:
        f.write(overlay_png(label, w, h))
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", src_path, "-i", png,
           "-filter_complex", "[0:v][1:v]overlay=0:0:format=auto",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
           "-c:a", "copy", "-movflags", "+faststart", out]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=ENCODE_TIMEOUT)
    except Exception as e:
        log.warning("ffmpeg: %s", e)
        shutil.rmtree(workdir, ignore_errors=True)
        return None
    if not os.path.exists(out) or os.path.getsize(out) > MAX_OUTPUT_BYTES:
        shutil.rmtree(workdir, ignore_errors=True)
        return None
    return out


async def make_marked(bot, file_id: str, label: str) -> Optional[str]:
    """Скачать видео из Telegram, вшить метку. Путь к файлу или None
    (тогда вызывающий код отправляет оригинал с подписью)."""
    if not available():
        return None
    try:
        tg_file = await bot.get_file(file_id)
        if (tg_file.file_size or 0) > MAX_SOURCE_BYTES:
            return None
        workdir = tempfile.mkdtemp(prefix="vsrc_")
        src = os.path.join(workdir, "src" + os.path.splitext(tg_file.file_path or "")[1] or ".mp4")
        await bot.download_file(tg_file.file_path, destination=src)
    except Exception as e:
        log.warning("video download: %s", e)
        return None
    try:
        return await asyncio.to_thread(burn_in, src, label)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def cleanup(path: Optional[str]) -> None:
    if not path:
        return
    try:
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)
    except Exception:
        pass
