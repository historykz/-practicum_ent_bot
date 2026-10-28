"""
ZIP-шаблон «для создания интерактивных заданий через ИИ».

Владелец скачивает его в админке, прикладывает к чату с ChatGPT или Claude и пишет
идею. В шаблоне есть всё, что нужно нейросети: инструкция, точная спецификация
формата, JSON-схема manifest.json и рабочие примеры (одно задание и набор).
Архив собирается на лету из текстов ниже, поэтому всегда совпадает с тем, что
понимает сайт (services/interactive_pkg.py). Версия формата — FORMAT_VERSION.
"""
import io
import json
import math
import struct
import wave
import zipfile
from datetime import datetime

from services import interactive_pkg as pkg

TEMPLATE_VERSION = "1.0"


def _ext_list() -> str:
    groups = {
        "Страницы и код": [".html", ".htm", ".css", ".js", ".mjs", ".json", ".wasm", ".map", ".txt", ".md", ".csv", ".xml", ".vtt"],
        "Картинки": [".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".ico", ".bmp"],
        "Звук и видео": [".mp3", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".flac", ".opus", ".mp4", ".webm", ".ogv"],
        "Шрифты": [".woff", ".woff2", ".ttf", ".otf"],
        "3D и анимации": [".glb", ".gltf", ".bin", ".obj", ".mtl", ".fbx", ".stl", ".lottie", ".atlas"],
    }
    return "\n".join(f"- {k}: {' '.join(v)}" for k, v in groups.items())


README_AI = """# Инструкция для нейросети (ChatGPT, Claude и других)

Ты — программист обучающих интерактивных заданий для платформы **SMART ЕНТ**
(подготовка к ЕНТ, Казахстан). Пользователь (преподаватель) не программист: он
описывает идею, а ты делаешь готовый ZIP-архив, который он загрузит на сайт.

В этом шаблоне есть всё, что нужно. Прочитай файлы в таком порядке:
1. этот файл;
2. `SPEC.md` — точный формат архива, ограничения среды и API `SmartENT`;
3. `manifest.schema.json` — схема `manifest.json`;
4. `examples/` — рабочие примеры: `single` (одно задание) и `multi` (несколько заданий в одном архиве).

## Что ты отдаёшь пользователю

Один ZIP-архив. В его корне лежит `manifest.json`, задания — каждое в своей папке:

```
мой-архив.zip
├── manifest.json
└── tasks/
    ├── organelles-drag/
    │   ├── index.html
    │   ├── style.css
    │   ├── game.js
    │   └── img/cell.svg
    └── mitosis-order/
        ├── index.html
        └── ...
```

- Одно задание или двадцать — формат один и тот же: просто разное число записей в `"tasks"`.
- Задание с несколькими этапами или уровнями — это ОДНО задание (одна запись, одна папка).
- Каждое задание самостоятельно: всё, что ему нужно (картинки, звуки, js, css, json),
  лежит в его папке. Не делай общих папок и не ссылайся из одного задания в папку другого.
- Пути внутри задания — только относительные: `img/cell.png`, `audio/ok.mp3`, `data/levels.json`.

Если ты не умеешь создавать файлы-архивы, выдай пользователю содержимое каждого файла
с полным путём и коротко объясни, как сложить их в ZIP (или используй свой инструмент
запуска кода, чтобы собрать архив и дать ссылку на скачивание).

## Обязательные правила

1. `manifest.json` по `SPEC.md`: `"format": "smartent-interactive"`, `"format_version": 1`.
2. У каждого задания уникальный `id` (латиница, цифры, `-`, `_`) и своя папка `folder`.
3. Задание работает без интернета-сервера: никаких запросов к своим или чужим API,
   никаких ключей API в коде. Разрешены: файлы своей папки и библиотеки с CDN
   cdn.jsdelivr.net, cdnjs.cloudflare.com, unpkg.com, cdn.tailwindcss.com, code.jquery.com.
   Лучше класть библиотеку файлом в папку задания — так надёжнее.
4. Связь с платформой — только через объект `window.SmartENT` (см. SPEC.md).
   Платформа добавляет его сама, подключать ничего не нужно.
5. Если задание влияет на оценку (`"grading": "server"`), правильные ответы НЕ кладутся
   в код задания. Они пишутся в `answer_key` в `manifest.json`, задание отправляет
   ответ ученика через `SmartENT.submit(answer)`, а баллы считает сервер.
   Если задание тренировочное (`"grading": "practice"`), оно может проверять само и
   сообщать результат через `SmartENT.reportScore(баллы, максимум)`.
6. Работает на телефоне (ширина от 320 px, касания пальцем) и на компьютере (мышь).
   Перетаскивание делай через Pointer Events или дублируй нажатием «выбрать → поставить».
7. Прогресс сохраняй через `SmartENT.saveProgress(объект)` и восстанавливай в `SmartENT.ready(...)`.
8. Не используй `alert`, `prompt`, `confirm` для основного сценария — показывай сообщения на странице.
9. Не добавляй водяные знаки и имя ученика: платформа рисует их сама поверх задания.
10. Текст для ученика — на языке запроса пользователя (обычно русский или казахский).

## Перед тем как отдать архив, проверь

- [ ] В корне архива `manifest.json`, он проходит `manifest.schema.json`.
- [ ] Число записей в `"tasks"` равно числу заданий, которые просил пользователь.
- [ ] У каждого задания есть папка и в ней файл из `"entry"` (обычно `index.html`).
- [ ] Все пути относительные и указывают на существующие файлы этой же папки.
- [ ] Для `"grading": "server"` есть `answer_key` того же вида, что и ответ, который
      отправляет `SmartENT.submit` (ключи объектов совпадают символ в символ).
- [ ] Нет `fetch` к внешним адресам, нет ключей API, нет `localStorage`-зависимости
      без восстановления (localStorage в задании работает, но хранится на сервере).
- [ ] Вёрстка не вылезает за ширину 320 px; кнопки не меньше 40×40 px.

## Как отвечать пользователю

Коротко: что за задания получились (по одному предложению на каждое), какие из них
с оценкой сервером, и сам архив. Не пересказывай этот шаблон.
"""


SPEC = """# Формат интерактивных заданий SMART ЕНТ — версия 1

Версия формата: **1** (`"format_version": 1`). Версия шаблона: __TPL__.
Если сайт обновит формат, в админке появится новый шаблон с большей версией.
Архив более новой версии, чем понимает сайт, не примется с понятной ошибкой.

## 1. Архив

- ZIP, без пароля. До __ZIPMB__ МБ архив, до __UNZIPMB__ МБ после распаковки, до __FILES__ файлов,
  один файл до __FILEMB__ МБ, одно задание до __TASKMB__ МБ, в архиве до __TASKS__ заданий.
- В корне `manifest.json`. Допускается одна обёрточная папка: `набор/manifest.json`.
- Без `manifest.json` архив примется только если в нём ровно одна HTML-страница —
  как одно задание без оценки. Всегда делай `manifest.json`.
- Файлы вне папок заданий не используются. Служебные `__MACOSX`, `.DS_Store` игнорируются.
- Пути без `..`, без абсолютных путей и ссылок (symlink).

Допустимые типы файлов (остальные будут пропущены):
__EXT__

## 2. manifest.json

```json
{
  "format": "smartent-interactive",
  "format_version": 1,
  "title": "Строение клетки",
  "tasks": [
    {
      "id": "organelles-drag",
      "folder": "tasks/organelles-drag",
      "entry": "index.html",
      "title": "Подпиши органоиды клетки",
      "subject": "Биология",
      "topic": "Строение клетки",
      "description": "Перетащи названия к частям клетки.",
      "grading": "server",
      "max_score": 10,
      "allow_retry": true,
      "max_attempts": 3,
      "height": 640,
      "visibility": "all",
      "answer_key": {"type": "pairs", "answer": {"p1": "ядро", "p2": "митохондрия"}}
    }
  ]
}
```

| Поле | Обязательно | Что значит |
|---|---|---|
| `id` | да | Уникальный в архиве: латиница, цифры, `-`, `_`, до 64 символов. По нему же задание заменяется новой версией. |
| `folder` | нет | Папка задания. По умолчанию `tasks/<id>`. Папки разных заданий не пересекаются. |
| `entry` | нет | Страница входа внутри папки, по умолчанию `index.html`. |
| `title` | да | Название для учителя и ученика. |
| `subject`, `topic`, `description` | нет | Предмет, тема, описание для ученика. |
| `grading` | нет | `practice` (по умолчанию) — тренировка, задание считает само; `server` — оценку ставит сервер по `answer_key`. |
| `max_score` | нет | Максимум баллов (1–1000), по умолчанию 10. |
| `allow_retry` | нет | `false` — засчитывается только первая отправка. |
| `max_attempts` | нет | Сколько раз можно отправить на проверку (0 — без ограничения). |
| `height` | нет | Начальная высота окна задания в px. Дальше окно подстраивается само. |
| `visibility` | нет | `all` — в общем списке заданий; `lessons` — только внутри уроков. |
| `answer_key` | для `server` | Ключ ответа (раздел 4). |

## 3. Среда, в которой работает задание

Задание открывается внутри урока или отдельной страницы в изолированном окне (iframe
с `sandbox` и политикой безопасности). Это значит:

- Работают: HTML, CSS, JavaScript (в том числе `type="module"`, Canvas, SVG, WebGL,
  Web Audio, анимации CSS/JS, WebAssembly), `fetch` к файлам СВОЕЙ папки
  (например `fetch('data/levels.json')`), картинки/звук/видео своей папки и `data:`/`blob:`.
- Библиотеки: файлом в папке задания или с CDN: cdn.jsdelivr.net, cdnjs.cloudflare.com,
  unpkg.com, cdn.tailwindcss.com, code.jquery.com (стили ещё и fonts.googleapis.com).
  Указывай точную версию: `https://cdn.jsdelivr.net/npm/gsap@3.12.5/dist/gsap.min.js`.
- Запрещены: запросы к любым серверам (кроме своих файлов), формы с отправкой, доступ к
  cookie, данным ученика и страницам сайта, вложенные iframe. `alert/confirm` работают,
  но не используй их как основной интерфейс.
- `localStorage` работает и сохраняется на сервере как прогресс ученика (до 50 КБ вместе
  с `saveProgress`). `sessionStorage` живёт до перезагрузки.
- Относительные пути считаются от папки страницы входа. Если библиотеке нужен полный адрес,
  возьми `SmartENT.asset('img/cell.png')`.
- Ошибки JavaScript видны преподавателю в предпросмотре; ученик их не видит.

## 4. Ключ ответа (`answer_key`) для `"grading": "server"`

Задание отправляет ответ: `SmartENT.submit(answer)`. Сервер сравнивает его с ключом.
Регистр букв, лишние пробелы и «ё/е» не важны (`"case_sensitive": true` — важны).

| `type` | Каким должен быть `answer` | Баллы |
|---|---|---|
| `pairs` | объект `{"ключ": "значение"}` — сопоставление, подписи, распределение по категориям | за каждую верную пару |
| `fields` | объект `{"поле": "текст"}`, в ключе допустимы варианты: `{"p1": ["Абай", "Абай Кунанбаев"]}` — пропуски, кроссворд, таблица | за каждое верное поле |
| `sequence` | массив в правильном порядке `["a","b","c"]` | всё или ничего |
| `set` | массив без порядка `["a","c"]` — «выбери все верные» | всё или ничего |
| `number` | число, в ключе можно `"tolerance": 0.5` | всё или ничего |
| `exact` | любое JSON-значение целиком | всё или ничего |

Распределение по категориям — это `pairs`: `{"митохондрия": "органоид", "ДНК": "вещество"}`.
Кроссворд — `fields`: `{"1-across": "клетка", "2-down": "ядро"}`.
Многоуровневое задание с оценкой: собери ответы всех уровней в один объект и отправь
один раз в конце (ключи уровней: `"L1-p1"`, `"L2-p1"` …).

Ответ сервера (его же получает `onResult` и Promise из `submit`):
```json
{"score": 7, "max": 10, "percent": 70, "correct": false,
 "items": {"p1": true, "p2": false}, "attempts_left": 2}
```
`items` есть у `pairs` и `fields`: по нему покажи ученику, где ошибки.
Если попытки кончились, придёт `"repeat": true` и `"note"`.

## 5. API `window.SmartENT`

Платформа сама добавляет его в начало страницы. Подключать файл не нужно.

```js
SmartENT.ready(function (progress, ctx) { ... });  // запуск: сохранённый прогресс (или null), ctx.mode, ctx.graded
SmartENT.saveProgress(obj);                        // сохранить прогресс ученика (до 50 КБ)
SmartENT.getProgress();                            // текущий прогресс
SmartENT.submit(answer).then(function (r) {...});  // ответ на проверку сервером (grading: server)
SmartENT.onResult(function (r) {...});             // то же, подпиской
SmartENT.reportScore(score, max);                  // результат тренировки (grading: practice), сохраняется как тренировочный
SmartENT.resize();                                 // подогнать высоту окна (обычно не нужно, следит само)
SmartENT.asset('audio/ok.mp3');                    // полный адрес файла своей папки
SmartENT.speak('Митохондрия', {lang: 'ru-RU'});    // озвучка голосом браузера, Promise; 'kk-KZ' — казахский
SmartENT.stopSpeaking();
SmartENT.canSpeak(); SmartENT.canListen();         // есть ли озвучка / распознавание речи
SmartENT.listen({lang: 'ru-RU'}).then(function (r) { r.text; r.alternatives; });  // распознать ответ голосом
SmartENT.info                                      // {mode, graded, base}
```

Чтобы задание открывалось и в обычном браузере при разработке, добавь в начало своего
скрипта заглушку (на платформе она не сработает, там SmartENT уже есть):

```js
window.SmartENT = window.SmartENT || {
  ready: function (cb) { cb(null, {mode: 'dev'}); }, saveProgress: function () {}, getProgress: function () { return null; },
  submit: function (a) { console.log('submit', a); return Promise.resolve({score: 0, max: 10, percent: 0, correct: false}); },
  onResult: function () {}, reportScore: function (s, m) { console.log('score', s, m); return Promise.resolve({}); },
  resize: function () {}, asset: function (p) { return p; },
  speak: function (t) { return Promise.resolve(); }, stopSpeaking: function () {},
  canSpeak: function () { return false; }, canListen: function () { return false; },
  listen: function () { return Promise.reject(new Error('stt_unavailable')); }, info: {mode: 'dev'}
};
```

## 6. Голос, звук, анимации, 3D

- Озвучка: `SmartENT.speak(text, {lang})` (голос браузера, бесплатно, без ключей) или
  заранее подготовленные файлы `audio/*.mp3` в папке задания (их можно сделать в любом
  ИИ-синтезаторе речи). Звук включай только после нажатия ученика (правило браузеров).
- Распознавание речи: `SmartENT.listen()` — работает в браузерах с поддержкой (Chrome,
  Edge, Safari). Всегда давай запасной вариант: поле для ввода или кнопки.
- Внешние платные сервисы голоса и ИИ из задания вызывать нельзя: в коде задания не
  должно быть ключей API, а запросы наружу заблокированы.
- Анимации: CSS, Canvas, SVG, GSAP, Lottie (`.json`/`.lottie` в папке), Three.js (`.glb`).
- Учитывай `prefers-reduced-motion`.

## 7. Телефон и компьютер

- `<meta name="viewport">` добавляет платформа; ширина от 320 px до 1200 px.
- Без горизонтальной прокрутки, размеры в `%`, `rem`, `clamp()`, `flex`/`grid`.
- Касания: Pointer Events (`pointerdown/move/up`) и `touch-action: none` на перетаскиваемых.
- Кнопки и цели касания не меньше 40×40 px; шрифт не меньше 16 px.
- Высоту окна платформа подстраивает по содержимому; не делай `height: 100vh` у body.

## 8. Подсказки, попытки, таймер, уровни

Всё это — часть кода задания: делай как требует идея. Платформа хранит прогресс
(`saveProgress`/`localStorage`), считает попытки проверки (`max_attempts`) и баллы
(`answer_key`). Таймер считай в задании; для оценки сервер учитывает только ответ.

## 9. Безопасность (что проверяет сайт)

Структура архива, пути, типы и размеры файлов; задание выполняется изолированно и без
доступа к данным; баллы с оценкой считает только сервер; результат `reportScore`
всегда подписан как тренировочный. Новая версия опубликованного задания сначала
проходит предпросмотр у преподавателя, прежняя версия хранится для отката.
"""


PROMPTS = """# Примеры запросов к нейросети

Приложи к чату этот ZIP-шаблон целиком и напиши свою идею. Например:

1. «Создай мне 10 разных интерактивных игр по теме „Строение клетки“: перетаскивание
   органоидов, соединение терминов, подписи к рисунку, последовательность процессов
   и другие задания. Подготовь готовый ZIP для SMART ЕНТ.»

2. «Придумай 30 разнообразных обучающих игр по теме генетики с перетаскиванием,
   анимацией, озвучиванием и автоматической проверкой ответов. Задания с оценкой
   делай с проверкой сервером. Один ZIP для SMART ЕНТ.»

3. «Создай одно необычное интерактивное задание по строению зуба с анимацией эмалевых
   призм, 3 уровня сложности, подсказки, итоговая проверка сервером. ZIP для SMART ЕНТ.»

4. «Сделай кроссворд на 12 слов по истории Казахского ханства с оценкой сервером
   и озвучиванием вопросов на казахском языке. ZIP для SMART ЕНТ.»

Если нейросеть ответила кодом, а не архивом, напишите: «Собери всё в один ZIP по шаблону
SMART ЕНТ» или вставьте код вручную в редакторе на сайте («Создать кодом»).
"""


SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "SMART ЕНТ: manifest.json архива интерактивных заданий",
    "type": "object",
    "required": ["format", "format_version", "tasks"],
    "properties": {
        "format": {"const": "smartent-interactive"},
        "format_version": {"type": "integer", "minimum": 1, "maximum": 1},
        "title": {"type": "string", "maxLength": 200},
        "tasks": {
            "type": "array", "minItems": 1, "maxItems": 200,
            "items": {
                "type": "object",
                "required": ["id", "title"],
                "properties": {
                    "id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"},
                    "folder": {"type": "string"},
                    "entry": {"type": "string", "pattern": "\\.html?$"},
                    "title": {"type": "string", "maxLength": 200},
                    "subject": {"type": "string", "maxLength": 120},
                    "topic": {"type": "string", "maxLength": 200},
                    "description": {"type": "string", "maxLength": 2000},
                    "grading": {"enum": ["practice", "server"]},
                    "max_score": {"type": "integer", "minimum": 1, "maximum": 1000},
                    "allow_retry": {"type": "boolean"},
                    "max_attempts": {"type": "integer", "minimum": 0, "maximum": 100},
                    "height": {"type": "integer", "minimum": 0, "maximum": 4000},
                    "visibility": {"enum": ["all", "lessons"]},
                    "answer_key": {
                        "type": "object", "required": ["type", "answer"],
                        "properties": {
                            "type": {"enum": ["exact", "set", "sequence", "pairs", "fields", "number"]},
                            "answer": {},
                            "tolerance": {"type": "number", "minimum": 0},
                            "case_sensitive": {"type": "boolean"},
                            "feedback": {"enum": ["items", "total"]},
                        },
                    },
                },
                "allOf": [{"if": {"properties": {"grading": {"const": "server"}}, "required": ["grading"]},
                           "then": {"required": ["answer_key"]}}],
            },
        },
    },
}

# ───────────────────────── пример: одно задание ─────────────────────────

SINGLE_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
  <title>Подпиши органоиды клетки</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <main class="wrap">
    <h1>Подпиши органоиды клетки</h1>
    <p class="hint">Перетащи название к номеру на рисунке. На телефоне: нажми название, потом номер.</p>
    <div class="stage">
      <img src="img/cell.svg" alt="Схема клетки с номерами 1–4" class="cell">
      <button class="slot" data-k="p1" style="left:46%;top:44%">1</button>
      <button class="slot" data-k="p2" style="left:72%;top:28%">2</button>
      <button class="slot" data-k="p3" style="left:30%;top:70%">3</button>
      <button class="slot" data-k="p4" style="left:24%;top:24%">4</button>
    </div>
    <div class="bank" id="bank"></div>
    <div class="row">
      <button id="hint" class="btn ghost">Подсказка</button>
      <button id="reset" class="btn ghost">Сначала</button>
      <button id="check" class="btn">Проверить</button>
    </div>
    <p id="out" class="out" role="status"></p>
  </main>
  <script src="game.js"></script>
</body>
</html>
"""

SINGLE_CSS = """* { box-sizing: border-box; }
body { margin: 0; font: 16px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; color: #1d2433; background: #fff; }
.wrap { max-width: 720px; margin: 0 auto; padding: 14px; }
h1 { font-size: clamp(1.15rem, 4vw, 1.5rem); margin: 0 0 6px; }
.hint { color: #64748b; margin: 0 0 12px; }
.stage { position: relative; width: 100%; max-width: 520px; margin: 0 auto; }
.cell { width: 100%; height: auto; display: block; }
.slot { position: absolute; transform: translate(-50%, -50%); min-width: 44px; min-height: 44px; padding: 4px 10px;
  border-radius: 22px; border: 2px dashed #6366f1; background: rgba(255,255,255,.92); font-weight: 700; cursor: pointer;
  max-width: 44%; font-size: clamp(.72rem, 3.4vw, 1rem); line-height: 1.15; white-space: normal; overflow-wrap: break-word; hyphens: auto; }
.slot.filled { border-style: solid; background: #eef2ff; }
.slot.ok { border-color: #16a34a; background: #dcfce7; }
.slot.bad { border-color: #dc2626; background: #fee2e2; }
.bank { display: flex; flex-wrap: wrap; gap: 8px; margin: 14px 0; min-height: 48px; }
.chip { padding: 10px 14px; border-radius: 999px; background: #eef2ff; border: 1px solid #c7d2fe; cursor: grab;
  user-select: none; touch-action: none; min-height: 44px; }
.chip.picked { outline: 3px solid #6366f1; }
.chip.dragging { opacity: .6; }
.row { display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }
.btn { min-height: 44px; padding: 10px 18px; border: 0; border-radius: 12px; background: #4f46e5; color: #fff; font-size: 1rem; cursor: pointer; }
.btn.ghost { background: #eef2ff; color: #3730a3; }
.out { min-height: 1.5em; font-weight: 600; }
@media (prefers-reduced-motion: no-preference) { .slot, .chip { transition: background .2s, border-color .2s; } }
"""

SINGLE_JS = """// Заглушка для запуска вне платформы (на SMART ЕНТ объект SmartENT уже есть)
window.SmartENT = window.SmartENT || {
  ready: function (cb) { cb(null, {mode: 'dev'}); }, saveProgress: function () {},
  submit: function (a) { console.log('submit', a); return Promise.resolve({score: 0, max: 10, percent: 0, correct: false, items: {}}); },
  onResult: function () {}, speak: function () { return Promise.resolve(); }, canSpeak: function () { return false; }
};

// Правильных ответов здесь НЕТ: они в manifest.json (answer_key), проверяет сервер.
var WORDS = ['ядро', 'митохондрия', 'клеточная мембрана', 'рибосома'];
var HINTS = ['Самый крупный органоид в центре клетки хранит ДНК.',
             'Органоид овальной формы с внутренними складками — «энергостанция».',
             'Граница клетки, которая отделяет её от внешней среды.',
             'Маленькая точка, на ней собираются белки.'];
var answer = {};          // номер на рисунке -> название
var picked = null;
var hintN = 0;

var bank = document.getElementById('bank');
var out = document.getElementById('out');

function draw() {
  bank.innerHTML = '';
  var used = Object.values(answer);
  WORDS.forEach(function (w) {
    if (used.indexOf(w) >= 0) return;
    var c = document.createElement('div');
    c.className = 'chip' + (picked === w ? ' picked' : '');
    c.textContent = w;
    c.dataset.w = w;
    bindChip(c);
    bank.appendChild(c);
  });
  document.querySelectorAll('.slot').forEach(function (s) {
    var v = answer[s.dataset.k];
    s.textContent = v || s.dataset.k.slice(1);
    s.classList.toggle('filled', !!v);
    s.classList.remove('ok', 'bad');
  });
}

function place(key, word) {
  Object.keys(answer).forEach(function (k) { if (answer[k] === word) delete answer[k]; });
  answer[key] = word;
  picked = null;
  draw();
  SmartENT.saveProgress({answer: answer, hintN: hintN});
}

// Перетаскивание через Pointer Events: одинаково мышью и пальцем
function bindChip(c) {
  c.addEventListener('pointerdown', function (e) {
    var ghost = c.cloneNode(true), moved = false, sx = e.clientX, sy = e.clientY;
    c.setPointerCapture(e.pointerId);
    function move(ev) {
      if (!moved && Math.hypot(ev.clientX - sx, ev.clientY - sy) < 6) return;
      if (!moved) { moved = true; ghost.style.cssText = 'position:fixed;pointer-events:none;z-index:9'; document.body.appendChild(ghost); c.classList.add('dragging'); }
      ghost.style.left = (ev.clientX - 30) + 'px'; ghost.style.top = (ev.clientY - 20) + 'px';
    }
    function up(ev) {
      c.removeEventListener('pointermove', move); c.removeEventListener('pointerup', up);
      c.classList.remove('dragging'); if (ghost.parentNode) ghost.remove();
      if (!moved) { picked = picked === c.dataset.w ? null : c.dataset.w; draw(); return; }
      var t = document.elementFromPoint(ev.clientX, ev.clientY);
      var slot = t && t.closest ? t.closest('.slot') : null;
      if (slot) place(slot.dataset.k, c.dataset.w);
    }
    c.addEventListener('pointermove', move); c.addEventListener('pointerup', up);
  });
}

document.querySelectorAll('.slot').forEach(function (s) {
  s.addEventListener('click', function () {
    if (picked) { place(s.dataset.k, picked); return; }
    if (answer[s.dataset.k]) { delete answer[s.dataset.k]; draw(); SmartENT.saveProgress({answer: answer, hintN: hintN}); }
  });
});

document.getElementById('hint').addEventListener('click', function () {
  out.textContent = '💡 ' + HINTS[hintN % HINTS.length];
  if (SmartENT.canSpeak && SmartENT.canSpeak()) SmartENT.speak(HINTS[hintN % HINTS.length]).catch(function () {});
  hintN++;
  SmartENT.saveProgress({answer: answer, hintN: hintN});
});
document.getElementById('reset').addEventListener('click', function () {
  answer = {}; picked = null; out.textContent = ''; draw(); SmartENT.saveProgress({answer: answer, hintN: hintN});
});
document.getElementById('check').addEventListener('click', function () {
  if (Object.keys(answer).length < 4) { out.textContent = 'Подпиши все четыре номера.'; return; }
  out.textContent = 'Проверяем…';
  SmartENT.submit(answer).then(function (r) {
    if (r.error) { out.textContent = r.error; return; }
    Object.keys(r.items || {}).forEach(function (k) {
      var s = document.querySelector('.slot[data-k="' + k + '"]');
      if (s) s.classList.add(r.items[k] ? 'ok' : 'bad');
    });
    out.textContent = 'Баллы: ' + r.score + ' из ' + r.max + (r.correct ? '. Всё верно! 🎉' : '. Красным отмечены ошибки.')
      + (r.attempts_left !== undefined ? ' Осталось попыток: ' + r.attempts_left + '.' : '') + (r.note ? ' ' + r.note : '');
  });
});

SmartENT.ready(function (saved) {
  if (saved && saved.answer) { answer = saved.answer; hintN = saved.hintN || 0; }
  draw();
});
"""

CELL_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 520 360" role="img" aria-label="Клетка">
  <ellipse cx="260" cy="180" rx="240" ry="160" fill="#fef9c3" stroke="#ca8a04" stroke-width="6"/>
  <circle cx="240" cy="160" r="58" fill="#c4b5fd" stroke="#6d28d9" stroke-width="4"/>
  <circle cx="240" cy="160" r="18" fill="#7c3aed"/>
  <ellipse cx="380" cy="95" rx="48" ry="24" fill="#fda4af" stroke="#be123c" stroke-width="4"/>
  <path d="M342 95 q10 -14 20 0 t20 0 t20 0 t20 0" fill="none" stroke="#be123c" stroke-width="3"/>
  <circle cx="120" cy="238" r="7" fill="#0f766e"/><circle cx="150" cy="262" r="7" fill="#0f766e"/>
  <circle cx="98" cy="268" r="7" fill="#0f766e"/>
</svg>
"""

SINGLE_MANIFEST = {
    "format": "smartent-interactive", "format_version": 1, "title": "Пример: одно задание",
    "tasks": [{
        "id": "cell-organelles", "folder": "tasks/cell-organelles", "entry": "index.html",
        "title": "Подпиши органоиды клетки", "subject": "Биология", "topic": "Строение клетки",
        "description": "Перетащи названия органоидов к номерам на рисунке.",
        "grading": "server", "max_score": 8, "allow_retry": True, "max_attempts": 3, "height": 620,
        "answer_key": {"type": "pairs", "answer": {"p1": "ядро", "p2": "митохондрия",
                                                   "p3": "рибосома", "p4": "клеточная мембрана"}},
    }],
}

# ───────────────────────── пример: несколько заданий ─────────────────────────

MATCH_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<title>Соедини органоид и функцию</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; font: 16px/1.4 system-ui, sans-serif; color: #1d2433; }
  .wrap { max-width: 760px; margin: 0 auto; padding: 14px; position: relative; }
  h1 { font-size: clamp(1.1rem, 4vw, 1.4rem); margin: 0 0 10px; }
  .cols { display: grid; grid-template-columns: 1fr 1fr; gap: clamp(24px, 10vw, 90px); position: relative; }
  .col { display: flex; flex-direction: column; gap: 10px; }
  .item { min-height: 48px; padding: 10px 12px; border: 2px solid #cbd5e1; border-radius: 12px; background: #fff;
    cursor: pointer; text-align: left; font-size: .95rem; position: relative; z-index: 1; }
  .item.sel { border-color: #6366f1; background: #eef2ff; }
  .item.ok { border-color: #16a34a; } .item.bad { border-color: #dc2626; }
  svg.lines { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; z-index: 0; }
  .row { display: flex; gap: 8px; justify-content: flex-end; margin-top: 14px; flex-wrap: wrap; }
  button.btn { min-height: 44px; padding: 10px 18px; border: 0; border-radius: 12px; background: #4f46e5; color: #fff; font-size: 1rem; }
  button.ghost { background: #eef2ff; color: #3730a3; }
  #out { font-weight: 600; min-height: 1.4em; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Соедини органоид с его функцией</h1>
  <p>Нажми слева, потом справа — появится линия. Нажми линию слева ещё раз, чтобы убрать.</p>
  <div class="cols" id="cols">
    <svg class="lines" id="lines"></svg>
    <div class="col" id="left"></div>
    <div class="col" id="right"></div>
  </div>
  <div class="row"><button class="btn ghost" id="clear">Стереть линии</button><button class="btn" id="check">Проверить</button></div>
  <p id="out" role="status"></p>
</div>
<script>
window.SmartENT = window.SmartENT || { ready: function (cb) { cb(null, {}); }, saveProgress: function () {},
  submit: function (a) { console.log(a); return Promise.resolve({score: 0, max: 6, correct: false, items: {}}); } };
var LEFT = [['nucleus', 'Ядро'], ['mito', 'Митохондрия'], ['ribo', 'Рибосома']];
var RIGHT = ['Синтез белка', 'Хранение наследственной информации', 'Образование АТФ'];
var links = {}, sel = null;
var L = document.getElementById('left'), R = document.getElementById('right'), svg = document.getElementById('lines');
LEFT.forEach(function (p) { var b = document.createElement('button'); b.className = 'item'; b.textContent = p[1]; b.dataset.k = p[0]; L.appendChild(b); });
RIGHT.forEach(function (t) { var b = document.createElement('button'); b.className = 'item'; b.textContent = t; b.dataset.v = t; R.appendChild(b); });
function draw() {
  svg.innerHTML = '';
  var box = document.getElementById('cols').getBoundingClientRect();
  Object.keys(links).forEach(function (k) {
    var a = L.querySelector('[data-k="' + k + '"]').getBoundingClientRect();
    var b = Array.prototype.find.call(R.children, function (x) { return x.dataset.v === links[k]; }).getBoundingClientRect();
    var line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', a.right - box.left); line.setAttribute('y1', a.top + a.height / 2 - box.top);
    line.setAttribute('x2', b.left - box.left); line.setAttribute('y2', b.top + b.height / 2 - box.top);
    line.setAttribute('stroke', '#6366f1'); line.setAttribute('stroke-width', '3'); line.setAttribute('stroke-linecap', 'round');
    svg.appendChild(line);
  });
  L.querySelectorAll('.item').forEach(function (x) { x.classList.toggle('sel', x.dataset.k === sel); });
}
L.addEventListener('click', function (e) { var b = e.target.closest('.item'); if (!b) return;
  if (links[b.dataset.k] && sel !== b.dataset.k) { delete links[b.dataset.k]; sel = null; save(); draw(); return; }
  sel = sel === b.dataset.k ? null : b.dataset.k; draw(); });
R.addEventListener('click', function (e) { var b = e.target.closest('.item'); if (!b || !sel) return;
  Object.keys(links).forEach(function (k) { if (links[k] === b.dataset.v) delete links[k]; });
  links[sel] = b.dataset.v; sel = null; save(); draw(); });
function save() { SmartENT.saveProgress(links); }
document.getElementById('clear').onclick = function () { links = {}; sel = null; save(); draw(); };
document.getElementById('check').onclick = function () {
  SmartENT.submit(links).then(function (r) {
    L.querySelectorAll('.item').forEach(function (x) { x.classList.remove('ok', 'bad'); if (r.items && x.dataset.k in r.items) x.classList.add(r.items[x.dataset.k] ? 'ok' : 'bad'); });
    document.getElementById('out').textContent = r.error || ('Баллы: ' + r.score + ' из ' + r.max);
  });
};
window.addEventListener('resize', draw);
SmartENT.ready(function (p) { if (p) links = p; draw(); });
</script>
</body>
</html>
"""

ORDER_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<title>Фазы митоза по порядку</title>
<link rel="stylesheet" href="order.css">
</head>
<body>
<div class="wrap">
  <h1>Расставь фазы митоза по порядку</h1>
  <p class="muted">Кнопками ▲ ▼ или перетаскиванием.</p>
  <ol id="list"></ol>
  <button id="check" class="btn">Проверить</button>
  <p id="out" role="status"></p>
</div>
<script src="order.js"></script>
</body>
</html>
"""

ORDER_CSS = """* { box-sizing: border-box; }
body { margin: 0; font: 16px/1.4 system-ui, sans-serif; color: #1d2433; }
.wrap { max-width: 560px; margin: 0 auto; padding: 14px; }
h1 { font-size: clamp(1.1rem, 4vw, 1.4rem); margin: 0 0 6px; } .muted { color: #64748b; }
ol { padding: 0; list-style: none; counter-reset: n; display: flex; flex-direction: column; gap: 8px; }
li { counter-increment: n; display: flex; align-items: center; gap: 8px; padding: 10px 12px; border: 2px solid #cbd5e1;
  border-radius: 12px; background: #fff; touch-action: none; }
li::before { content: counter(n) "."; font-weight: 700; color: #64748b; }
li span { flex: 1; } li button { width: 44px; height: 44px; border-radius: 10px; border: 1px solid #cbd5e1; background: #f8fafc; }
.btn { margin-top: 12px; min-height: 44px; padding: 10px 18px; border: 0; border-radius: 12px; background: #4f46e5; color: #fff; font-size: 1rem; }
#out { font-weight: 600; }
"""

ORDER_JS = """window.SmartENT = window.SmartENT || { ready: function (cb) { cb(null, {}); }, saveProgress: function () {},
  submit: function (a) { console.log(a); return Promise.resolve({score: 0, max: 5, correct: false}); } };
// Порядок перемешан; правильный порядок знает только сервер (answer_key в manifest.json)
var ITEMS = ['Анафаза', 'Профаза', 'Телофаза', 'Метафаза'];
var list = document.getElementById('list');
function render(order) {
  list.innerHTML = '';
  order.forEach(function (t) {
    var li = document.createElement('li');
    li.innerHTML = '<span></span><button aria-label="Выше">▲</button><button aria-label="Ниже">▼</button>';
    li.querySelector('span').textContent = t; li.dataset.t = t; list.appendChild(li);
  });
}
function current() { return Array.prototype.map.call(list.children, function (li) { return li.dataset.t; }); }
list.addEventListener('click', function (e) {
  var b = e.target.closest('button'); if (!b) return; var li = b.closest('li');
  if (b.textContent === '▲' && li.previousElementSibling) list.insertBefore(li, li.previousElementSibling);
  if (b.textContent === '▼' && li.nextElementSibling) list.insertBefore(li.nextElementSibling, li);
  SmartENT.saveProgress(current());
});
document.getElementById('check').onclick = function () {
  SmartENT.submit(current()).then(function (r) {
    document.getElementById('out').textContent = r.error || (r.correct ? 'Верно! ' : 'Пока неверно. ') + 'Баллы: ' + r.score + ' из ' + r.max;
  });
};
SmartENT.ready(function (p) { render(Array.isArray(p) ? p : ITEMS); });
"""

BLANKS_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<title>Заполни пропуски (с озвучкой)</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; font: 17px/1.6 system-ui, sans-serif; color: #1d2433; }
  .wrap { max-width: 680px; margin: 0 auto; padding: 14px; }
  h1 { font-size: clamp(1.1rem, 4vw, 1.4rem); margin: 0 0 8px; }
  .text input { width: 9.5em; max-width: 45vw; font-size: 1rem; padding: 4px 8px; border: 2px solid #cbd5e1; border-radius: 8px; }
  .text input.ok { border-color: #16a34a; background: #dcfce7; } .text input.bad { border-color: #dc2626; background: #fee2e2; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
  button { min-height: 44px; padding: 10px 16px; border: 0; border-radius: 12px; background: #4f46e5; color: #fff; font-size: 1rem; }
  button.ghost { background: #eef2ff; color: #3730a3; }
  #out { font-weight: 600; min-height: 1.4em; } .timer { float: right; color: #64748b; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Заполни пропуски <span class="timer" id="timer"></span></h1>
  <div class="text" id="text">Загружаем…</div>
  <div class="row">
    <button class="ghost" id="say">🔊 Прослушать</button>
    <button class="ghost" id="voice">🎤 Ответить голосом</button>
    <button id="check">Проверить</button>
  </div>
  <p id="out" role="status"></p>
  <audio id="ding" src="audio/correct.wav" preload="auto"></audio>
</div>
<script>
window.SmartENT = window.SmartENT || { ready: function (cb) { cb(null, {}); }, saveProgress: function () {},
  reportScore: function (s, m) { console.log(s, m); return Promise.resolve({}); }, speak: function () { return Promise.resolve(); },
  canSpeak: function () { return false; }, canListen: function () { return false; } };
// Тренировочное задание: данные и ответы лежат в data.json своей папки, проверяет сам код,
// результат уходит через reportScore и сохраняется как тренировочный.
var data = null, left = 60, timerId = null, focusInput = null;
fetch('data.json').then(function (r) { return r.json(); }).then(function (d) {
  data = d;
  var html = d.text.replace(/\\{(\\d+)\\}/g, function (_, i) { return '<input data-i="' + i + '" aria-label="Пропуск ' + i + '">'; });
  document.getElementById('text').innerHTML = html;
  document.querySelectorAll('.text input').forEach(function (inp) {
    inp.addEventListener('focus', function () { focusInput = inp; });
    inp.addEventListener('input', save);
  });
  SmartENT.ready(function (p) {
    if (p && p.values) Object.keys(p.values).forEach(function (i) { var x = document.querySelector('input[data-i="' + i + '"]'); if (x) x.value = p.values[i]; });
    left = p && p.left ? p.left : d.seconds;
    tick(); timerId = setInterval(tick, 1000);
  });
});
function values() { var v = {}; document.querySelectorAll('.text input').forEach(function (x) { v[x.dataset.i] = x.value; }); return v; }
function save() { SmartENT.saveProgress({values: values(), left: left}); }
function tick() { document.getElementById('timer').textContent = '⏱ ' + left + ' с'; if (left <= 0) { clearInterval(timerId); check(); return; } left--; if (left % 5 === 0) save(); }
function norm(s) { return String(s || '').trim().toLowerCase().replace(/ё/g, 'е'); }
function check() {
  clearInterval(timerId);
  var score = 0, v = values();
  Object.keys(data.answers).forEach(function (i) {
    var ok = data.answers[i].map(norm).indexOf(norm(v[i])) >= 0;
    var x = document.querySelector('input[data-i="' + i + '"]'); x.classList.toggle('ok', ok); x.classList.toggle('bad', !ok);
    if (ok) score++;
  });
  var max = Object.keys(data.answers).length;
  document.getElementById('out').textContent = 'Верно ' + score + ' из ' + max + '.';
  if (score === max) { try { document.getElementById('ding').play(); } catch (e) {} }
  SmartENT.reportScore(score, max);
}
document.getElementById('check').onclick = check;
document.getElementById('say').onclick = function () {
  var t = data ? data.text.replace(/\\{\\d+\\}/g, ' … ') : '';
  SmartENT.speak(t, {lang: 'ru-RU'}).catch(function () { document.getElementById('out').textContent = 'Озвучка недоступна в этом браузере.'; });
};
document.getElementById('voice').onclick = function () {
  if (!SmartENT.canListen()) { document.getElementById('out').textContent = 'Голосовой ввод недоступен: впишите ответ вручную.'; return; }
  var target = focusInput || document.querySelector('.text input');
  SmartENT.listen({lang: 'ru-RU'}).then(function (r) { target.value = r.text; save(); })
    .catch(function () { document.getElementById('out').textContent = 'Не расслышали, попробуйте ещё раз или впишите.'; });
};
</script>
</body>
</html>
"""

BLANKS_DATA = {
    "seconds": 90,
    "text": "Митохондрии вырабатывают {1}, а синтез белка идёт на {2}. Наследственная информация хранится в {3}.",
    "answers": {"1": ["АТФ", "атф"], "2": ["рибосомах", "рибосоме"], "3": ["ядре", "ДНК"]},
}

MULTI_MANIFEST = {
    "format": "smartent-interactive", "format_version": 1, "title": "Пример: набор из трёх заданий",
    "tasks": [
        {"id": "organoid-function", "folder": "tasks/organoid-function", "entry": "index.html",
         "title": "Соедини органоид и функцию", "subject": "Биология", "topic": "Строение клетки",
         "grading": "server", "max_score": 6, "allow_retry": True,
         "answer_key": {"type": "pairs", "answer": {"nucleus": "Хранение наследственной информации",
                                                    "mito": "Образование АТФ", "ribo": "Синтез белка"}}},
        {"id": "mitosis-order", "folder": "tasks/mitosis-order", "entry": "index.html",
         "title": "Фазы митоза по порядку", "subject": "Биология", "topic": "Деление клетки",
         "grading": "server", "max_score": 5, "max_attempts": 3,
         "answer_key": {"type": "sequence", "answer": ["Профаза", "Метафаза", "Анафаза", "Телофаза"]}},
        {"id": "cell-blanks-voice", "folder": "tasks/cell-blanks-voice", "entry": "index.html",
         "title": "Заполни пропуски (с озвучкой и таймером)", "subject": "Биология", "topic": "Строение клетки",
         "grading": "practice", "visibility": "lessons", "height": 420},
    ],
}


def _beep_wav() -> bytes:
    """Короткий звук «верно» для примера: WAV 0,25 с."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        frames = bytearray()
        for i in range(4000):
            t = i / 16000
            amp = int(9000 * math.sin(2 * math.pi * (660 if t < 0.12 else 880) * t) * (1 - i / 4000))
            frames += struct.pack("<h", amp)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def _fill(text: str) -> str:
    mb = 1024 * 1024
    return (text.replace("__TPL__", TEMPLATE_VERSION).replace("__ZIPMB__", str(pkg.ZIP_MAX // mb))
            .replace("__UNZIPMB__", str(pkg.UNZIP_MAX // mb)).replace("__FILES__", str(pkg.FILES_MAX))
            .replace("__FILEMB__", str(pkg.FILE_MAX // mb)).replace("__TASKMB__", str(pkg.TASK_MAX // mb))
            .replace("__TASKS__", str(pkg.TASKS_MAX)).replace("__EXT__", _ext_list()))


def example_files() -> dict:
    """Файлы примеров: {путь: байты}. Используются и в шаблоне, и в проверках."""
    j = lambda o: json.dumps(o, ensure_ascii=False, indent=2).encode("utf-8")  # noqa: E731
    return {
        "examples/single/manifest.json": j(SINGLE_MANIFEST),
        "examples/single/tasks/cell-organelles/index.html": SINGLE_HTML.encode(),
        "examples/single/tasks/cell-organelles/style.css": SINGLE_CSS.encode(),
        "examples/single/tasks/cell-organelles/game.js": SINGLE_JS.encode(),
        "examples/single/tasks/cell-organelles/img/cell.svg": CELL_SVG.encode(),
        "examples/multi/manifest.json": j(MULTI_MANIFEST),
        "examples/multi/tasks/organoid-function/index.html": MATCH_HTML.encode(),
        "examples/multi/tasks/mitosis-order/index.html": ORDER_HTML.encode(),
        "examples/multi/tasks/mitosis-order/order.css": ORDER_CSS.encode(),
        "examples/multi/tasks/mitosis-order/order.js": ORDER_JS.encode(),
        "examples/multi/tasks/cell-blanks-voice/index.html": BLANKS_HTML.encode(),
        "examples/multi/tasks/cell-blanks-voice/data.json": j(BLANKS_DATA),
        "examples/multi/tasks/cell-blanks-voice/audio/correct.wav": _beep_wav(),
    }


def build_zip() -> bytes:
    """Шаблон целиком. В корне НЕТ manifest.json: если его по ошибке загрузить на
    сайт как задание, сайт честно скажет, что заданий в нём нет."""
    info = {"format": pkg.FORMAT, "format_version": pkg.FORMAT_VERSION, "template_version": TEMPLATE_VERSION,
            "generated_at": datetime.utcnow().strftime("%Y-%m-%d"),
            "note": "Это шаблон для нейросети, а не задание. Задания собираются по SPEC.md."}
    files = {
        "ПРОЧТИ_МЕНЯ_ИИ.md": README_AI.encode(),
        "SPEC.md": _fill(SPEC).encode(),
        "ПРИМЕРЫ_ЗАПРОСОВ.md": PROMPTS.encode(),
        "manifest.schema.json": json.dumps(SCHEMA, ensure_ascii=False, indent=2).encode(),
        "smartent-template.json": json.dumps(info, ensure_ascii=False, indent=2).encode(),
    }
    files.update(example_files())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zi = zipfile.ZipInfo("smartent-ai-template/" + name, date_time=(2026, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.flag_bits |= 0x800          # имена в UTF-8
            zf.writestr(zi, data)
    return buf.getvalue()


def example_zip(which: str) -> bytes:
    """Готовый к загрузке архив примера (single или multi) — для проверки и показа."""
    prefix = f"examples/{which}/"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in example_files().items():
            if name.startswith(prefix):
                zi = zipfile.ZipInfo(name[len(prefix):], date_time=(2026, 1, 1, 0, 0, 0))
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.flag_bits |= 0x800
                zf.writestr(zi, data)
    return buf.getvalue()
