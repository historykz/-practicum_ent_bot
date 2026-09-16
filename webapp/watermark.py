"""
Персональный водяной знак поверх закрытых материалов (конспекты, видео).

Полностью защититься от скриншотов средствами браузера невозможно —
это осознанно НЕ обещается. Знак — сдерживающий фактор и способ
установить, чей это был скриншот, если материал утечёт.
"""
import base64
from datetime import datetime, timedelta, timezone

ALMATY = timezone(timedelta(hours=5))


def build_watermark_data_uri(tg_id: int, username: str = None) -> str:
    label_parts = []
    if username:
        label_parts.append(f"@{username}")
    label_parts.append(f"ID:{tg_id}")
    stamp = datetime.now(ALMATY).strftime("%d.%m.%Y %H:%M")
    label_parts.append(stamp)
    text = "  ·  ".join(label_parts)

    # Серый (не белый и не чёрный), чтобы знак был виден и на тёмном, и на светлом
    # фоне сайта без привязки к теме — сервер не знает, какая тема у клиента.
    # Плотность и размер подняты: на снимке экрана прежний знак почти не читался,
    # а смысл метки в том, чтобы по утёкшему скриншоту было видно, чья это копия.
    # Дальше поднимать нельзя — начнёт мешать чтению самого конспекта.
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200">
<text x="10" y="56" font-family="sans-serif" font-size="17" fill="#7d8598" fill-opacity="0.46"
      transform="rotate(-28 10 56)">{text}</text>
<text x="10" y="156" font-family="sans-serif" font-size="17" fill="#7d8598" fill-opacity="0.46"
      transform="rotate(-28 10 156)">{text}</text>
</svg>'''
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"
