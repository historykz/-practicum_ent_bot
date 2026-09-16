"""
Водяной знак с @username и id юзера на картинках вопросов.
Накладывается на лету при отправке платных/приватных тестов в личке.
Оригиналы картинок кэшируются в памяти (по file_id), чтобы не скачивать
каждый раз; наложение знака — быстрое.
"""
import io
import logging

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

# Кэш скачанных оригиналов: file_id -> bytes (до ~60 картинок)
_orig_cache: dict = {}
_CACHE_LIMIT = 60


async def download_photo(bot, file_id: str) -> bytes:
    """Скачать фото по file_id (с кэшем оригиналов)."""
    if file_id in _orig_cache:
        return _orig_cache[file_id]
    tg_file = await bot.get_file(file_id)
    buf = io.BytesIO()
    await bot.download_file(tg_file.file_path, destination=buf)
    data = buf.getvalue()
    if len(_orig_cache) >= _CACHE_LIMIT:
        _orig_cache.pop(next(iter(_orig_cache)))
    _orig_cache[file_id] = data
    return data


def apply_watermark(img_bytes: bytes, username: str, tg_id: int) -> bytes:
    """Наложить бледный диагональный водяной знак (3 повтора)."""
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
        W, H = img.size
        text = (f"@{username} · id:{tg_id}" if username
                else f"id:{tg_id}")
        fsize = max(18, W // 30)
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", fsize)
        except Exception:
            font = ImageFont.load_default()

        bbox_w = int(fsize * 0.62 * len(text)) + 20
        txt_img = Image.new("RGBA", (bbox_w, fsize + 14), (0, 0, 0, 0))
        td = ImageDraw.Draw(txt_img)
        td.text((0, 0), text, fill=(140, 140, 140, 70), font=font)
        rot = txt_img.rotate(22, expand=True)

        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        positions = [
            (int(W * 0.05), int(H * 0.10)),
            (int(W * 0.30), int(H * 0.45)),
            (int(W * 0.10), int(H * 0.75)),
        ]
        for x, y in positions:
            layer.paste(rot, (x, y), rot)
        out_img = Image.alpha_composite(img, layer).convert("RGB")
        out = io.BytesIO()
        out_img.save(out, format="JPEG", quality=88)
        return out.getvalue()
    except Exception as e:
        log.warning("watermark: %s", e)
        return img_bytes


async def send_watermarked_photo(bot, chat_id: int, file_id: str,
                                   username: str, tg_id: int,
                                   protect: bool = True):
    """Скачать, наложить знак, отправить. Возвращает Message или None."""
    from aiogram.types import BufferedInputFile
    try:
        orig = await download_photo(bot, file_id)
        marked = apply_watermark(orig, username or '', tg_id)
        photo = BufferedInputFile(marked, filename="q.jpg")
        return await bot.send_photo(chat_id, photo, protect_content=protect)
    except Exception as e:
        log.warning("send watermarked: %s", e)
        try:
            return await bot.send_photo(chat_id, file_id,
                                          protect_content=protect)
        except Exception:
            return None
