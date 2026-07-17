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

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="420" height="220">
<text x="10" y="60" font-family="sans-serif" font-size="15" fill="#ffffff" fill-opacity="0.16"
      transform="rotate(-28 10 60)">{text}</text>
<text x="10" y="170" font-family="sans-serif" font-size="15" fill="#ffffff" fill-opacity="0.16"
      transform="rotate(-28 10 170)">{text}</text>
</svg>'''
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"
