"""ZIP-шаблон для нейросети: конспекты и видео-анимации кодом (v97).

Владелец скачивает его в редакторе урока, прикладывает к чату с Claude или ChatGPT
вместе со страницами учебника и получает ZIP, который загружает в урок. Формат тот
же, что у интерактивных заданий (services/interactive_pkg.py): manifest.json и папка
на каждую тему, плюс поле "material": "conspect" или "video". Стиль конспекта взят
из промпта владельца «Конспекты по истории Казахстана»: палитра, плашки разделов,
блоки дат, «★ ВАЖНО», итоговые выводы, карточки терминов и схемы-цепочки.
"""
import io
import json
import zipfile
from datetime import datetime

from services import interactive_pkg as pkg

KIT_CSS = r"""/* SMART ЕНТ — оформление конспектов. Подключайте в каждой теме: <link rel="stylesheet" href="smartent-conspect.css"> */
:root { --se-dark: #0B3D2E; --se-green: #1F6E4A; --se-gold: #C9A227; --se-mint: #E4F3EA; --se-cream: #FBF3DD;
  --se-text: #262626; --se-grey: #6B6B6B; --se-grey2: #8C8C8C; }
* { box-sizing: border-box; }
html, body { margin: 0; background: #fff; color: var(--se-text); }
body { font-family: "DejaVu Sans", "Segoe UI", Roboto, Arial, sans-serif; font-size: 15px; line-height: 1.55; padding: 14px; }
.se-page { max-width: 820px; margin: 0 auto; }
.se-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-bottom: 10px; }
.se-head .se-section-name { font-size: 11px; color: var(--se-grey); }
.se-badge { background: var(--se-dark); color: var(--se-gold); font-weight: 700; font-size: 11px; padding: 3px 10px; border-radius: 8px; white-space: nowrap; }
.se-topic { margin: 6px 0 2px; font-size: 22px; line-height: 1.25; font-weight: 800; color: var(--se-dark); text-transform: uppercase; }
.se-subtitle { margin: 0 0 8px; color: var(--se-grey); font-style: italic; font-size: 13px; }
.se-divider { height: 1px; background: var(--se-gold); border: 0; margin: 8px 0 14px; }
.se-plaque { background: var(--se-green); color: #fff; font-weight: 700; font-size: 14px; padding: 8px 12px; border-radius: 10px; margin: 18px 0 10px; }
.se-intro { margin: 0 0 8px; }
.se-list { list-style: none; margin: 6px 0 10px; padding: 0; }
.se-list li { position: relative; padding-left: 18px; margin: 4px 0; }
.se-list li::before { content: "\25B8"; position: absolute; left: 0; color: var(--se-gold); font-weight: 700; }
.se-date { background: var(--se-cream); border: 1.4px solid var(--se-gold); border-radius: 10px; padding: 7px 10px; margin: 8px 0; }
.se-date b { color: var(--se-dark); }
.se-key { background: var(--se-cream); border: 1.8px solid var(--se-gold); border-radius: 12px; padding: 10px 12px; margin: 12px 0; }
.se-key .se-key-title { color: var(--se-gold); font-weight: 800; text-transform: uppercase; margin-bottom: 3px; }
.se-summary { background: var(--se-mint); border: 1.2px solid var(--se-green); border-radius: 12px; padding: 10px 12px; margin: 14px 0; }
.se-cards { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 10px 0; }
.se-card { background: #fff; border: 1.2px dashed var(--se-gold); border-radius: 10px; padding: 9px 11px; font-size: 14px; }
.se-card .se-card-title { color: var(--se-dark); font-weight: 800; margin-bottom: 3px; }
.se-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 10px 0; }
.se-grid > div { border: 1.2px dashed var(--se-gold); border-radius: 10px; padding: 7px 10px; font-size: 14px; }
.se-chain { background: var(--se-cream); border: 1.2px solid var(--se-gold); border-radius: 12px; padding: 10px; margin: 12px 0;
  color: var(--se-dark); font-weight: 700; text-align: center; display: flex; flex-wrap: wrap; align-items: center; justify-content: center; gap: 6px; }
.se-chain .se-arrow { color: var(--se-gold); font-weight: 800; }
.se-chain.se-vertical { flex-direction: column; }
.se-fig { margin: 12px auto; text-align: center; }
.se-fig img { max-width: 100%; height: auto; border-radius: 8px; }
.se-fig figcaption { color: var(--se-grey); font-style: italic; font-size: 13px; margin-top: 4px; }
.se-variants { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.se-foot { border-top: 1px solid #ddd; margin-top: 22px; padding-top: 6px; text-align: right; color: var(--se-gold); font-size: 12px; }
details.se-more { border: 1.2px dashed var(--se-gold); border-radius: 10px; padding: 6px 10px; margin: 8px 0; }
details.se-more summary { cursor: pointer; color: var(--se-dark); font-weight: 700; }
@media (max-width: 560px) {
  body { font-size: 14px; padding: 10px; }
  .se-topic { font-size: 19px; }
  .se-cards, .se-grid, .se-variants { grid-template-columns: 1fr; }
}
"""

EXAMPLE_CONSPECT = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Реформа 1867–1868 гг.</title><link rel="stylesheet" href="smartent-conspect.css"></head>
<body><main class="se-page">
<div class="se-head"><span class="se-section-name">История Казахстана · Административные реформы</span><span class="se-badge">Smart ENT</span></div>
<h1 class="se-topic">Реформа 1867–1868 гг.</h1>
<p class="se-subtitle">Цели реформы, новое административное деление, управление и суд</p>
<hr class="se-divider">
<p class="se-intro">Пример оформления. Текст здесь условный: в настоящем конспекте все факты берутся только из присланных учебников.</p>
<ul class="se-list"><li>Коротко: что происходило и почему.</li><li>Чего добивалась власть.</li><li>Что изменилось для населения.</li></ul>
<div class="se-plaque">1. Причины реформы</div>
<div class="se-chain"><span>Причина</span><span class="se-arrow">→</span><span>Событие</span><span class="se-arrow">→</span><span>Результат</span></div>
<p>Пояснение к схеме: 2–4 предложения, чтобы ученик понял смысл, а не только стрелки.</p>
<div class="se-date"><b>11 июля 1867 г.</b> → пример блока «дата → событие».</div>
<div class="se-key"><div class="se-key-title">★ Главная цель реформы</div>Самое важное, что нужно запомнить. Не больше 1–2 таких блоков на тему.</div>
<div class="se-plaque">2. Административное деление</div>
<div class="se-chain se-vertical"><span>Генерал-губернаторство</span><span class="se-arrow">↓</span><span>Область</span><span class="se-arrow">↓</span><span>Уезд</span><span class="se-arrow">↓</span><span>Волость</span><span class="se-arrow">↓</span><span>Аул</span></div>
<div class="se-cards"><div class="se-card"><div class="se-card-title">Термин</div>Определение термина строго по учебнику.</div>
<div class="se-card"><div class="se-card-title">Сравнение</div>Вторая карточка в ряд.</div></div>
<div class="se-grid"><div>Короткий факт 1</div><div>Короткий факт 2</div><div>Короткий факт 3</div><div>Короткий факт 4</div></div>
<div class="se-variants"><div class="se-card"><div class="se-card-title">Вариант 1</div>Цифра из одного учебника</div><div class="se-card"><div class="se-card-title">Вариант 2</div>Цифра из другого учебника</div></div>
<figure class="se-fig"><img src="images/example.svg" alt=""><figcaption>Подпись к картинке курсивом</figcaption></figure>
<div class="se-summary"><b>Итог.</b> Вывод темы в 2–3 предложениях.</div>
<div class="se-foot">Smart ENT</div>
</main></body></html>
"""

EXAMPLE_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="480" height="200" viewBox="0 0 480 200"><rect width="480" height="200" rx="14" fill="#FBF3DD" stroke="#C9A227" stroke-width="3"/><text x="240" y="108" text-anchor="middle" font-family="sans-serif" font-size="22" fill="#0B3D2E">Иллюстрация из учебника</text></svg>"""

EXAMPLE_VIDEO = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Фотосинтез</title>
<style>
  body { margin: 0; font-family: "DejaVu Sans", Arial, sans-serif; background: #fff; color: #262626; }
  /* Сцена ролика. Кнопки, время, перемотку, скорость и субтитры рисует платформа — свои не нужны. */
  .se-stage { position: relative; aspect-ratio: 16 / 9; max-width: 960px; margin: 0 auto; background: #E4F3EA; border-radius: 14px 14px 0 0; overflow: hidden; }
  .scene { display: flex; align-items: center; justify-content: center; }
  .title { font-size: clamp(22px, 6vw, 54px); font-weight: 800; color: #0B3D2E; animation: rise 1.2s ease-out both; }
  .sun { width: 18%; aspect-ratio: 1; border-radius: 50%; background: #C9A227; position: absolute; left: 6%; top: 8%; animation: pulse 2s infinite; }
  .leaf { width: 40%; aspect-ratio: 2 / 1; background: #1F6E4A; border-radius: 0 100% 0 100%; animation: rise 1s ease-out both; }
  .mol { position: absolute; width: 4%; aspect-ratio: 1; border-radius: 50%; background: #0B3D2E; animation: fly 3s linear infinite; }
  @keyframes pulse { 50% { transform: scale(1.08); } }
  @keyframes fly { from { transform: translate(-200%, 0); } to { transform: translate(900%, -300%); } }
  @keyframes rise { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: none; } }
</style></head>
<body>
<div class="se-stage">
  <section class="scene" data-se-scene data-duration="5" data-say="Фотосинтез: как растение делает питание из света.">
    <div class="title">ФОТОСИНТЕЗ</div>
  </section>
  <section class="scene" data-se-scene data-duration="7" data-say="Пример: свет попадает на лист.">
    <div class="sun"></div><div class="leaf"></div>
  </section>
  <section class="scene" data-se-scene data-duration="8" data-say="Пример: молекулы движутся внутрь листа. Тексты берите только из учебника.">
    <div class="leaf"></div><span class="mol" style="left:20%;top:70%"></span><span class="mol" style="left:30%;top:80%;animation-delay:1s"></span>
  </section>
</div>
</body></html>
"""

README = """# Инструкция для нейросети: конспекты и видео-анимации SMART ЕНТ

Ты делаешь учебные материалы для платформы **SMART ЕНТ** (подготовка к ЕНТ, Казахстан).
Пользователь не программист. Он присылает страницы учебника, список тем и, возможно, свой
промпт оформления. Ты отвечаешь **одним ZIP-архивом**, который он загрузит в урок на сайте.
Материал откроется прямо на странице урока, на телефоне и на компьютере, в изолированном окне.

## Что получится

* **Конспект** (`"material": "conspect"`) — HTML-страница темы в стиле SMART ЕНТ.
* **Видео-анимация** (`"material": "video"`) — анимированный урок на HTML/CSS/SVG, который на сайте
  выглядит как обычное видео: платформа сама рисует кнопки «пуск/пауза», время «0:42 / 6:30»,
  перемотку, −5 и +5 секунд, скорость 0.5×–2×, субтитры и озвучку. Скачать его как ролик нельзя.

Одна тема = одна папка в `tasks/`. В архиве может быть сколько угодно тем, они встанут в урок
по порядку.

## Структура архива

```
konspekt.zip
├─ manifest.json
└─ tasks/
   ├─ reforma-1867/
   │  ├─ index.html
   │  ├─ smartent-conspect.css      ← скопируй из шаблона в КАЖДУЮ папку темы
   │  └─ images/портрет.jpg
   └─ reforma-1886/
      ├─ index.html
      └─ smartent-conspect.css
```

`manifest.json`:

```json
{
  "format": "smartent-interactive",
  "format_version": 1,
  "material": "conspect",
  "title": "История Казахстана: реформы XIX века",
  "tasks": [
    {"id": "reforma-1867", "folder": "tasks/reforma-1867", "entry": "index.html",
     "title": "Реформа 1867–1868 гг.", "subject": "История Казахстана", "topic": "Реформы",
     "material": "conspect", "grading": "practice"}
  ]
}
```

Для видео-анимации всё так же, только `"material": "video"`.

## Правила кода

* Только файлы внутри архива. Никаких внешних ссылок, CDN, шрифтов и картинок из интернета:
  сеть в окне материала закрыта, внешнее просто не загрузится.
* Без `fetch`, форм, `localStorage` для данных ученика, всплывающих окон.
* Картинки: JPEG качества около 85, ширина не больше 1400 px; схемы — SVG. Так архив маленький.
* Вёрстка резиновая: экран телефона 360 px и компьютер. Ничего не должно вылезать по ширине.
* Не используй эмодзи в конспекте: только простые символы ▸ ★ → ↓.

## Оформление конспекта (стиль SMART ЕНТ)

Подключи `smartent-conspect.css` и используй готовые классы (пример в `examples/conspect`):

| Что | Класс |
|---|---|
| Шапка: раздел слева, «Smart ENT» справа | `.se-head`, `.se-section-name`, `.se-badge` |
| Название темы ЗАГЛАВНЫМИ | `h1.se-topic` |
| Подзаголовок курсивом | `.se-subtitle` |
| Золотая линия | `hr.se-divider` |
| Плашка раздела «1. Причины реформы» | `.se-plaque` |
| Пункты со стрелкой ▸ | `ul.se-list` |
| Дата → событие | `.se-date` |
| ★ ВАЖНО (1–2 на тему) | `.se-key` + `.se-key-title` |
| Итоговый вывод | `.se-summary` |
| Карточки по две в ряд | `.se-cards` + `.se-card` + `.se-card-title` |
| Сетка коротких фактов | `.se-grid` |
| Схема-цепочка → и ↓ | `.se-chain`, `.se-chain.se-vertical`, `.se-arrow` |
| Картинка с подписью | `figure.se-fig` |
| «Вариант 1 / Вариант 2» при разных цифрах | `.se-variants` |
| Подробнее по нажатию | `details.se-more` |

Палитра: тёмно-зелёный #0B3D2E, зелёный #1F6E4A, золотой #C9A227, мятный #E4F3EA,
светло-золотой #FBF3DD, текст #262626, серый #6B6B6B / #8C8C8C.

## Правила содержания

Если пользователь приложил свой промпт оформления (например «Конспекты по истории Казахстана»),
он главнее этого раздела. Его правила про источники, глубину, проверку полноты, стиль, схемы, даты
и иллюстрации применяй полностью, а вместо PDF собирай HTML на классах выше:

* Только информация из присланных учебников. Ничего не добавлять от себя.
* В тексте нет ссылок на параграфы, страницы и учебники.
* Сначала коротко суть, потом факты и схемы. 1 факт = 1 короткий пункт. Без «стен текста».
* Разные цифры из разных учебников — оба варианта рядом.
* Каждая иллюстрация — рядом со своим фактом, подпись под ней, без дубля подписи со скана.
* Перед отправкой второй проход «учебник → каждый факт → есть ли он в конспекте».

Подходит для любого предмета: история Казахстана, математическая грамотность (формулы, графики,
пошаговые решения), биология (схемы, процессы), английский (словари, карточки) и другие.

## Видео-анимация

Ролик — это сцены друг за другом. **Свои кнопки, таймеры и полосы прогресса не делай**: панель
плеера (пуск, пауза, время, перемотка, −5/+5 секунд, скорость, субтитры, озвучка) рисует платформа.

* Обёртка `<div class="se-stage">` с соотношением сторон 16:9, внутри — сцены.
* Каждая сцена: `<section data-se-scene data-duration="8" data-say="Текст субтитров">…</section>`.
  `data-duration` — длительность сцены в секундах. `data-say` — что говорит диктор, строго по учебнику:
  это и субтитры, и озвучка. Длительность бери с запасом: примерно 1 секунда на 2 слова плюс 2 секунды.
* Анимацию делай на CSS: `@keyframes` и `animation`. Платформа ведёт её по времени ролика, поэтому
  перемотка, пауза и скорость работают сами. Задержки внутри сцены — через `animation-delay`.
* Не используй `setTimeout`, `setInterval` и `requestAnimationFrame` для смены сцен: так перемотка
  сломается. Если без JS никак (график, Canvas), рисуй кадр по времени:
  `SEVideo.on('frame', function (t) { … })`, где `t` — секунды от начала сцены.
* Свой звук — файлом из архива: `<audio data-se-audio src="sound/scene1.mp3"></audio>` внутри сцены.
  Если звука нет, платформа озвучит `data-say` голосом браузера (кнопка 🔊).
* Всё помещается на экран телефона. Пример: `examples/video`.

## Ответ пользователю

Отдай ZIP-архив целиком и коротко перечисли, какие темы в нём. Не проси пользователя ничего
переделывать руками.
"""


def build_zip() -> bytes:
    info = {"format": pkg.FORMAT, "format_version": pkg.FORMAT_VERSION, "kind": "conspect-template",
            "generated_at": datetime.utcnow().strftime("%Y-%m-%d"),
            "note": "Это шаблон для нейросети, а не конспект. Конспекты собираются по ПРОЧТИ_МЕНЯ_ИИ.md"}
    example_manifest = {"format": pkg.FORMAT, "format_version": pkg.FORMAT_VERSION, "material": "conspect",
                        "title": "Пример конспекта", "tasks": [{"id": "example", "folder": "tasks/example",
                                                                "entry": "index.html", "title": "Реформа 1867–1868 гг.",
                                                                "subject": "История Казахстана", "material": "conspect",
                                                                "grading": "practice"}]}
    video_manifest = {"format": pkg.FORMAT, "format_version": pkg.FORMAT_VERSION, "material": "video",
                      "title": "Пример видео-анимации", "tasks": [{"id": "photosynthesis", "folder": "tasks/photosynthesis",
                                                                  "entry": "index.html", "title": "Фотосинтез",
                                                                  "subject": "Биология", "material": "video",
                                                                  "grading": "practice"}]}
    j = lambda o: json.dumps(o, ensure_ascii=False, indent=2).encode()  # noqa: E731
    files = {
        "ПРОЧТИ_МЕНЯ_ИИ.md": README.encode(),
        "smartent-conspect.css": KIT_CSS.encode(),
        "smartent-template.json": j(info),
        "examples/conspect/manifest.json": j(example_manifest),
        "examples/conspect/tasks/example/index.html": EXAMPLE_CONSPECT.encode(),
        "examples/conspect/tasks/example/smartent-conspect.css": KIT_CSS.encode(),
        "examples/conspect/tasks/example/images/example.svg": EXAMPLE_SVG.encode(),
        "examples/video/manifest.json": j(video_manifest),
        "examples/video/tasks/photosynthesis/index.html": EXAMPLE_VIDEO.encode(),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zi = zipfile.ZipInfo("smartent-konspekt-template/" + name, date_time=(2026, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.flag_bits |= 0x800
            zf.writestr(zi, data)
    return buf.getvalue()


def example_zip(kind: str = "conspect") -> bytes:
    """Готовый к загрузке архив из примера (для проверок)."""
    src = zipfile.ZipFile(io.BytesIO(build_zip()))
    prefix = f"smartent-konspekt-template/examples/{kind}/"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n in src.namelist():
            if n.startswith(prefix) and not n.endswith("/"):
                zf.writestr(n[len(prefix):], src.read(n))
    return buf.getvalue()
