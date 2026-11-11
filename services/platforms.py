"""Где можно открыть материал: сайт, мини-приложение Telegram или оба (v97).

Платформу запроса знает webapp/auth (вход из мини-приложения помечен в сессии).
Проверка — только на сервере: страница, документ задания, ответы теста и видео
вызывают ok() сами, поэтому адрес API с «чужой» платформы ничего не даёт."""

CHOICES = {"all": "Сайт и мини-приложение", "site": "Только сайт", "miniapp": "Только мини-приложение"}
NAMES = {"site": "на сайте", "miniapp": "в мини-приложении Telegram"}


def current() -> str:
    try:
        from webapp import auth
        return auth.current_source()
    except Exception:
        return "site"


def norm(value) -> str:
    return value if value in CHOICES else "all"


def ok(value) -> bool:
    v = norm(value)
    return v == "all" or v == current()


def why(value) -> str:
    """Текст для ученика, если открыл не там."""
    v = norm(value)
    return "" if v == "all" else f"Этот материал открывается только {NAMES[v]}."
