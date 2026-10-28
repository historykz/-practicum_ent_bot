"""
Интерактивные задания, написанные кодом (HTML, CSS, JavaScript) прямо на сайте.

Безопасность. Код автора всегда работает в изолированном фрейме:
  • атрибут sandbox="allow-scripts" без allow-same-origin и такой же заголовок
    Content-Security-Policy: у документа «пустое» происхождение, ему не видны
    cookie, вход, localStorage сайта, страница вокруг и админка;
  • connect-src 'none': сам код не может отправить запрос ни на сайт, ни наружу;
  • картинки только с сайта (загруженные к заданию) или data:/blob:,
    скрипты и стили сверх встроенных — только с трёх известных CDN.
Связь с платформой — только через window.SmartENT (postMessage к странице вокруг):
прогресс, ответ, тренировочный результат. Страница проверяет, что сообщение пришло
именно из её фрейма, и сама ходит на сервер от имени вошедшего ученика.

Оценка. Если задание влияет на оценку (grading='server'), ученик присылает только
свой ответ, а баллы считает сервер по ключу ответа автора. Результат, который
посчитал сам JavaScript задания (reportScore), всегда сохраняется как
тренировочный и так же подписывается.
"""
import json
import logging
import os
import re
import secrets
import shutil
from pathlib import Path
from typing import Optional

import config
import database as db
import utils

log = logging.getLogger(__name__)

DRAFT, PUBLISHED = "draft", "published"
PRACTICE, SERVER = "practice", "server"
MAX_CODE = 300_000            # символов в одном поле кода
MAX_STATE = 50_000            # байт сохранённого прогресса
MAX_ANSWER = 20_000           # байт ответа ученика
ASSET_MAX = 5 * 1024 * 1024
ASSET_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
             ".webp": "image/webp", ".svg": "image/svg+xml"}
CDN_SCRIPT = ("https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://unpkg.com "
              "https://cdn.tailwindcss.com https://code.jquery.com")
CDN_STYLE = "https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://unpkg.com https://fonts.googleapis.com"
CDN_FONT = "https://fonts.gstatic.com https://cdn.jsdelivr.net https://cdnjs.cloudflare.com"
KEY_TYPES = ("exact", "set", "sequence", "pairs", "fields", "number")

STARTER_HTML = """<h2>Соотнесите события и годы</h2>
<p class="hint">Перетащите год к событию (или нажмите на год, а потом на событие).</p>
<div class="years" id="years">
  <span class="chip" draggable="true" data-v="1465">1465</span>
  <span class="chip" draggable="true" data-v="1731">1731</span>
  <span class="chip" draggable="true" data-v="1991">1991</span>
</div>
<div class="rows">
  <div class="row"><span>Образование Казахского ханства</span><b class="slot" data-k="khanate"></b></div>
  <div class="row"><span>Независимость Казахстана</span><b class="slot" data-k="independence"></b></div>
  <div class="row"><span>Принятие российского подданства Младшим жузом</span><b class="slot" data-k="junior"></b></div>
</div>
<button id="check">Проверить</button>
<p id="out"></p>"""

STARTER_CSS = """body { font-family: system-ui, sans-serif; margin: 16px; color: #1d2433; }
.years { display: flex; gap: 8px; flex-wrap: wrap; margin: 12px 0; }
.chip { padding: 8px 14px; border-radius: 999px; background: #eef2ff; border: 1px solid #c7d2fe; cursor: grab; user-select: none; }
.chip.picked { outline: 3px solid #6366f1; }
.rows { display: flex; flex-direction: column; gap: 8px; }
.row { display: flex; justify-content: space-between; align-items: center; gap: 10px; padding: 10px; border: 1px solid #e5e7eb; border-radius: 10px; }
.slot { min-width: 70px; min-height: 30px; border: 2px dashed #cbd5e1; border-radius: 8px; text-align: center; padding: 4px; }
button { margin-top: 14px; padding: 10px 18px; border: 0; border-radius: 10px; background: #4f46e5; color: #fff; font-size: 1rem; }
.hint { color: #64748b; }"""

STARTER_JS = """// Ответ хранится в объекте: событие -> год
let answer = {};
let picked = null;

function draw() {
  document.querySelectorAll('.slot').forEach(s => { s.textContent = answer[s.dataset.k] || ''; });
}
document.querySelectorAll('.chip').forEach(c => {
  c.addEventListener('dragstart', e => e.dataTransfer.setData('text', c.dataset.v));
  c.addEventListener('click', () => {
    document.querySelectorAll('.chip').forEach(x => x.classList.remove('picked'));
    picked = c.dataset.v; c.classList.add('picked');
  });
});
document.querySelectorAll('.slot').forEach(s => {
  s.addEventListener('dragover', e => e.preventDefault());
  s.addEventListener('drop', e => { e.preventDefault(); answer[s.dataset.k] = e.dataTransfer.getData('text'); draw(); SmartENT.saveProgress(answer); });
  s.addEventListener('click', () => { if (picked) { answer[s.dataset.k] = picked; draw(); SmartENT.saveProgress(answer); } });
});

// Прогресс сохраняется на сервере: при возврате ученик продолжит с того же места
SmartENT.ready(saved => { if (saved) { answer = saved; draw(); } });

// Проверка — на сервере по ключу ответа (вкладка «Оценка»)
document.getElementById('check').addEventListener('click', () => SmartENT.submit(answer));
SmartENT.onResult(r => {
  document.getElementById('out').textContent =
    r.error ? r.error : `Баллы: ${r.score} из ${r.max}` + (r.correct ? ' — всё верно!' : '');
});"""

STARTER_KEY = json.dumps({"type": "pairs", "answer": {"khanate": "1465", "independence": "1991", "junior": "1731"}},
                         ensure_ascii=False, indent=2)


# ───────────────────────── хранение ─────────────────────────

def assets_dir() -> Path:
    d = Path(config.DB_PATH).resolve().parent / "uploads" / "interactive"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get(task_id: int) -> Optional[dict]:
    row = db.fetchone("SELECT * FROM interactive_tasks WHERE id=?", (int(task_id),))
    return dict(row) if row else None


def list_all() -> list:
    rows = db.fetchall(
        "SELECT t.*, (SELECT COUNT(*) FROM interactive_results r WHERE r.task_id=t.id) AS results_count, "
        "(SELECT COUNT(DISTINCT r.user_id) FROM interactive_results r WHERE r.task_id=t.id) AS users_count, "
        "(SELECT COUNT(DISTINCT b.lesson_id) FROM lesson_blocks b WHERE b.task_id=t.id) AS lessons_count "
        "FROM interactive_tasks t ORDER BY t.id DESC")
    return [dict(r) for r in rows]


def list_published() -> list:
    """Общий список для учеников: опубликованные и не помеченные «только в уроках»."""
    return [dict(r) for r in db.fetchall(
        "SELECT id, title, description, subject, topic, grading, max_score FROM interactive_tasks "
        "WHERE status='published' AND COALESCE(visibility,'all')='all' ORDER BY id DESC")]


def save_meta(task_id: int, data: dict) -> tuple:
    """Описание и настройки задания без кода (для заданий из архива)."""
    task = get(task_id)
    if not task:
        return False, "Задание не найдено."
    title = str(data.get("title") or "").strip()
    if not title:
        return False, "Укажите название задания."
    grading = SERVER if data.get("grading") == SERVER else PRACTICE
    key_raw = str(data.get("answer_key") or "").strip()
    if grading == SERVER:
        _k, err = parse_key(key_raw)
        if err:
            return False, "Ключ ответа: " + err

    def num(name, default, lo, hi):
        try:
            return max(lo, min(hi, int(data.get(name) if data.get(name) not in (None, "") else default)))
        except (TypeError, ValueError):
            return default
    vis = data.get("visibility") if data.get("visibility") in ("all", "lessons") else (task.get("visibility") or "all")
    db.execute(
        "UPDATE interactive_tasks SET title=?, description=?, subject=?, topic=?, grading=?, answer_key=?, max_score=?, "
        "allow_retry=?, max_attempts=?, frame_height=?, visibility=?, updated_at=? WHERE id=?",
        (title[:200], str(data.get("description") or "")[:2000], str(data.get("subject") or "")[:120],
         str(data.get("topic") or "")[:200], grading, key_raw[:20000], num("max_score", 10, 1, 1000),
         0 if data.get("allow_retry") in (False, 0, "0", "off") else 1, num("max_attempts", 0, 0, 100),
         num("frame_height", 0, 0, 4000), vis, utils.now_iso(), int(task_id)))
    return True, ""


def create(created_by: int, title: str = "") -> int:
    cur = db.execute(
        "INSERT INTO interactive_tasks (title, html, css, js, grading, answer_key, max_score, created_by) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ((title or "").strip()[:200] or "Новое интерактивное задание", STARTER_HTML, STARTER_CSS, STARTER_JS,
         SERVER, STARTER_KEY, 10, int(created_by or 0)))
    return cur.lastrowid


def save(task_id: int, data: dict) -> tuple:
    """Сохранить черновик из редактора. Возвращает (ok, ошибка)."""
    task = get(task_id)
    if not task:
        return False, "Задание не найдено."
    title = str(data.get("title") or "").strip()
    if not title:
        return False, "Укажите название задания."
    fields = {}
    for k in ("html", "css", "js", "full_doc"):
        v = str(data.get(k) or "")
        if len(v) > MAX_CODE:
            return False, f"Слишком много кода в поле {k.upper()} (больше {MAX_CODE} символов)."
        fields[k] = v
    grading = SERVER if data.get("grading") == SERVER else PRACTICE
    key_raw = str(data.get("answer_key") or "").strip()
    if grading == SERVER:
        _k, err = parse_key(key_raw)
        if err:
            return False, "Ключ ответа: " + err
    try:
        max_score = max(1, min(1000, int(data.get("max_score") or 10)))
    except (TypeError, ValueError):
        max_score = 10
    db.execute(
        "UPDATE interactive_tasks SET title=?, description=?, subject=?, topic=?, html=?, css=?, js=?, full_doc=?, "
        "use_full_doc=?, grading=?, answer_key=?, max_score=?, allow_retry=?, version=version+1, updated_at=? "
        "WHERE id=?",
        (title[:200], str(data.get("description") or "")[:2000], str(data.get("subject") or "")[:120],
         str(data.get("topic") or "")[:200], fields["html"], fields["css"], fields["js"], fields["full_doc"],
         1 if data.get("use_full_doc") else 0, grading, key_raw[:20000], max_score,
         0 if data.get("allow_retry") in (False, 0, "0", "off") else 1, utils.now_iso(), int(task_id)))
    extra = {}
    if data.get("visibility") in ("all", "lessons"):
        extra["visibility"] = data["visibility"]
    for name, hi in (("max_attempts", 100), ("frame_height", 4000)):
        if data.get(name) not in (None, ""):
            try:
                extra[name] = max(0, min(hi, int(data[name])))
            except (TypeError, ValueError):
                pass
    for k, v in extra.items():
        db.execute(f"UPDATE interactive_tasks SET {k}=? WHERE id=?", (v, int(task_id)))
    return True, ""


def publish_problems(task: dict) -> list:
    problems = []
    if not (task.get("title") or "").strip():
        problems.append("Нет названия.")
    body = task.get("full_doc") if task.get("use_full_doc") else (task.get("html") or "") + (task.get("js") or "")
    if not (body or "").strip():
        problems.append("Задание пустое: нет кода.")
    if task.get("grading") == SERVER:
        _k, err = parse_key(task.get("answer_key") or "")
        if err:
            problems.append("Ключ ответа: " + err)
    return problems


def publish(task_id: int) -> tuple:
    task = get(task_id)
    if not task:
        return False, ["Задание не найдено."]
    problems = publish_problems(task)
    if problems:
        return False, problems
    db.execute("UPDATE interactive_tasks SET status='published', published_at=? WHERE id=?",
               (utils.now_iso(), int(task_id)))
    return True, []


def unpublish(task_id: int) -> None:
    db.execute("UPDATE interactive_tasks SET status='draft' WHERE id=?", (int(task_id),))


def delete(task_id: int) -> bool:
    task = get(task_id)
    if not task:
        return False
    for a in db.fetchall("SELECT stored_name FROM interactive_assets WHERE task_id=?", (int(task_id),)):
        try:
            (assets_dir() / a["stored_name"]).unlink()
        except OSError:
            pass
    from services import interactive_pkg as _pkg
    _pkg.delete_task_files(task)
    db.execute("DELETE FROM interactive_tasks WHERE id=?", (int(task_id),))
    return True


def duplicate(task_id: int, created_by: int) -> Optional[int]:
    """Копия задания: свои копии картинок, ссылки в коде переписываются на них."""
    src = get(task_id)
    if not src:
        return None
    cur = db.execute(
        "INSERT INTO interactive_tasks (title, description, subject, topic, html, css, js, full_doc, use_full_doc, "
        "grading, answer_key, max_score, allow_retry, copied_from_id, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"{src['title']} (копия)"[:200], src["description"], src["subject"], src["topic"], src["html"], src["css"],
         src["js"], src["full_doc"], src["use_full_doc"], src["grading"], src["answer_key"], src["max_score"],
         src["allow_retry"], src["id"], int(created_by or 0)))
    new_id = cur.lastrowid
    fields = {k: src[k] or "" for k in ("html", "css", "js", "full_doc")}
    for a in db.fetchall("SELECT * FROM interactive_assets WHERE task_id=?", (src["id"],)):
        ext = os.path.splitext(a["stored_name"])[1]
        new_name = secrets.token_hex(16) + ext
        try:
            shutil.copyfile(assets_dir() / a["stored_name"], assets_dir() / new_name)
        except OSError:
            continue
        db.execute("INSERT INTO interactive_assets (task_id, stored_name, orig_name, mime, size) VALUES (?,?,?,?,?)",
                   (new_id, new_name, a["orig_name"], a["mime"], a["size"]))
        for k in fields:
            fields[k] = fields[k].replace(a["stored_name"], new_name)
    db.execute("UPDATE interactive_tasks SET html=?, css=?, js=?, full_doc=?, max_attempts=?, frame_height=?, "
               "visibility=? WHERE id=?",
               (fields["html"], fields["css"], fields["js"], fields["full_doc"], src.get("max_attempts") or 0,
                src.get("frame_height") or 0, src.get("visibility") or "all", new_id))
    if src.get("source") == "zip":
        from services import interactive_pkg as _pkg
        _pkg.duplicate_files(src, new_id)
    return new_id


# ───────────────────────── картинки ─────────────────────────

def asset_url(stored_name: str) -> str:
    return f"/interactive/asset/{stored_name}"


def add_asset(task_id: int, filename: str, blob: bytes) -> tuple:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in ASSET_EXT:
        return None, "Можно загрузить только картинку: png, jpg, gif, webp или svg."
    if not blob:
        return None, "Файл пустой."
    if len(blob) > ASSET_MAX:
        return None, "Картинка больше 5 МБ."
    stored = secrets.token_hex(16) + ext
    with open(assets_dir() / stored, "wb") as f:
        f.write(blob)
    cur = db.execute("INSERT INTO interactive_assets (task_id, stored_name, orig_name, mime, size) VALUES (?,?,?,?,?)",
                     (int(task_id), stored, os.path.basename(filename or "")[:200], ASSET_EXT[ext], len(blob)))
    return {"id": cur.lastrowid, "url": asset_url(stored), "name": os.path.basename(filename or ""),
            "size": len(blob)}, ""


def assets_of(task_id: int) -> list:
    return [{"id": r["id"], "url": asset_url(r["stored_name"]), "name": r["orig_name"], "size": r["size"]}
            for r in db.fetchall("SELECT * FROM interactive_assets WHERE task_id=? ORDER BY id", (int(task_id),))]


def delete_asset(asset_id: int) -> Optional[int]:
    row = db.fetchone("SELECT * FROM interactive_assets WHERE id=?", (int(asset_id),))
    if not row:
        return None
    try:
        (assets_dir() / row["stored_name"]).unlink()
    except OSError:
        pass
    db.execute("DELETE FROM interactive_assets WHERE id=?", (int(asset_id),))
    return row["task_id"]


def asset_path(stored_name: str) -> Optional[Path]:
    if not re.fullmatch(r"[0-9a-f]{32}\.(png|jpg|jpeg|gif|webp|svg)", stored_name or ""):
        return None
    if not db.fetchone("SELECT 1 FROM interactive_assets WHERE stored_name=?", (stored_name,)):
        return None
    p = assets_dir() / stored_name
    return p if p.exists() else None


# ───────────────────────── документ задания ─────────────────────────

def csp(site_origin: str = "") -> str:
    """Политика для документа задания. self у изолированного документа не всегда
    совпадает с сайтом, поэтому адрес сайта указываем явно."""
    own = f" {site_origin}" if site_origin else ""
    return ("default-src 'none'; "
            f"script-src 'unsafe-inline' 'unsafe-eval' {CDN_SCRIPT}; "
            f"style-src 'unsafe-inline' {CDN_STYLE}; "
            f"img-src 'self'{own} data: blob: https:; media-src 'self'{own} data: blob: https:; "
            f"font-src data: {CDN_FONT}; connect-src 'none'; form-action 'none'; base-uri 'none'")


BRIDGE = r"""<script>
/* SmartENT: связь задания с платформой. Вход, cookie и сеть заданию недоступны.
   localStorage в изолированном фрейме запрещён браузером, поэтому здесь своя замена:
   то, что задание кладёт в localStorage, сохраняется на сервере как прогресс ученика. */
(function () {
  var INIT = __SMARTENT_INIT__;
  var P = window.parent, ctx = {mode: INIT.mode || '', graded: !!INIT.graded};
  var saved = INIT.progress, env = saved && saved.__se;
  var state = env ? (saved.state === undefined ? null : saved.state) : (saved === undefined ? null : saved);
  var LS = (env && saved.ls && typeof saved.ls === 'object') ? saved.ls : {};
  var readyCbs = [], resultCbs = [], saveTimer = null, waitResult = [], waitPractice = [];
  function post(type, data) { try { P.postMessage({__smartent: 1, type: type, data: data}, '*'); } catch (e) {} }
  function report(err) {
    post('error', {message: String((err && err.message) || err), stack: String((err && err.stack) || '').slice(0, 1500)});
  }
  function persist() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () { post('progress', {__se: 1, state: state, ls: LS}); }, 400);
  }
  function store(data, keep) {
    function has(k) { return Object.prototype.hasOwnProperty.call(data(), k); }
    return {
      getItem: function (k) { k = String(k); return has(k) ? data()[k] : null; },
      setItem: function (k, v) { data()[String(k)] = String(v); if (keep) persist(); },
      removeItem: function (k) { delete data()[String(k)]; if (keep) persist(); },
      clear: function () { var d = data(); Object.keys(d).forEach(function (k) { delete d[k]; }); if (keep) persist(); },
      key: function (i) { return Object.keys(data())[i] || null; },
      get length() { return Object.keys(data()).length; }
    };
  }
  var SS = {};
  var blocked = false;
  try { window.localStorage.getItem('x'); } catch (e) { blocked = true; }
  if (blocked) {
    try { Object.defineProperty(window, 'localStorage', {configurable: true, get: function () { return localShim; }}); } catch (e) {}
    try { Object.defineProperty(window, 'sessionStorage', {configurable: true, get: function () { return sessionShim; }}); } catch (e) {}
  }
  var localShim = store(function () { return LS; }, true);
  var sessionShim = store(function () { return SS; }, false);
  window.addEventListener('message', function (e) {
    if (e.source !== P) return;
    var m = e.data || {};
    if (m.__smartent && m.type === 'result') {
      var w = waitResult.shift();
      if (w) w(m.data || {});
      resultCbs.forEach(function (cb) { try { cb(m.data || {}); } catch (err) { report(err); } });
    } else if (m.__smartent && m.type === 'practice_result') {
      var wp = waitPractice.shift();
      if (wp) wp(m.data || {});
    }
  });
  window.addEventListener('error', function (e) {
    post('error', {message: e.message, line: e.lineno, col: e.colno});
  });
  window.addEventListener('unhandledrejection', function (e) { report(e.reason); });
  var origError = console.error;
  console.error = function () {
    post('error', {message: Array.prototype.map.call(arguments, function (a) { return String(a); }).join(' ')});
    if (origError) origError.apply(console, arguments);
  };
  function voices(lang) {
    try { return (window.speechSynthesis.getVoices() || []).filter(function (v) { return (v.lang || '').toLowerCase().indexOf(lang.slice(0, 2)) === 0; }); }
    catch (e) { return []; }
  }
  window.SmartENT = {
    version: 1,
    formatVersion: 1,
    info: {mode: ctx.mode, graded: ctx.graded, base: INIT.base || ''},
    ready: function (cb) { setTimeout(function () { try { cb(state, ctx); } catch (err) { report(err); } }, 0); },
    getProgress: function () { return state; },
    saveProgress: function (s) { state = s; persist(); },
    /* Ответ на проверку сервером. Возвращает Promise с результатом {score, max, percent, correct, items}. */
    submit: function (answer) {
      return new Promise(function (resolve) { waitResult.push(resolve); post('submit', {answer: answer}); });
    },
    /* Результат, посчитанный самим заданием: сохраняется как тренировочный. */
    reportScore: function (score, max) {
      return new Promise(function (resolve) {
        waitPractice.push(resolve); post('practice', {score: Number(score), max: Number(max)});
      });
    },
    onResult: function (cb) { resultCbs.push(cb); },
    resize: function () { post('resize', {h: document.documentElement.scrollHeight}); },
    /* Полный адрес файла из папки задания (картинка, звук, JSON). */
    asset: function (path) { try { return new URL(path, document.baseURI).href; } catch (e) { return path; } },
    /* Озвучка текста голосом браузера. lang: 'ru-RU' (по умолчанию), 'kk-KZ', 'en-US'. */
    speak: function (text, opts) {
      opts = opts || {};
      return new Promise(function (resolve, reject) {
        if (!window.speechSynthesis || !window.SpeechSynthesisUtterance) { reject(new Error('tts_unavailable')); return; }
        try {
          var u = new SpeechSynthesisUtterance(String(text || ''));
          u.lang = opts.lang || 'ru-RU';
          u.rate = opts.rate || 1; u.pitch = opts.pitch || 1;
          var v = voices(u.lang); if (v.length) u.voice = v[0];
          u.onend = function () { resolve(); }; u.onerror = function (e) { reject(new Error(e.error || 'tts_error')); };
          window.speechSynthesis.cancel(); window.speechSynthesis.speak(u);
        } catch (e) { reject(e); }
      });
    },
    stopSpeaking: function () { try { window.speechSynthesis.cancel(); } catch (e) {} },
    canSpeak: function () { return !!(window.speechSynthesis && window.SpeechSynthesisUtterance); },
    canListen: function () { return !!(window.SpeechRecognition || window.webkitSpeechRecognition); },
    /* Распознать сказанное (если браузер умеет). Возвращает Promise {text, alternatives}. */
    listen: function (opts) {
      opts = opts || {};
      return new Promise(function (resolve, reject) {
        var R = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!R) { reject(new Error('stt_unavailable')); return; }
        try {
          var r = new R(); r.lang = opts.lang || 'ru-RU'; r.interimResults = false; r.maxAlternatives = 5;
          r.onresult = function (e) {
            var res = e.results[0], alts = [];
            for (var i = 0; i < res.length; i++) alts.push(res[i].transcript);
            resolve({text: alts[0] || '', alternatives: alts});
          };
          r.onerror = function (e) { reject(new Error(e.error || 'stt_error')); };
          r.onnomatch = function () { resolve({text: '', alternatives: []}); };
          r.start();
          if (opts.timeout) setTimeout(function () { try { r.stop(); } catch (e) {} }, opts.timeout);
        } catch (e) { reject(e); }
      });
    }
  };
  function size() { post('resize', {h: document.documentElement.scrollHeight}); }
  window.addEventListener('load', size);
  if (window.ResizeObserver) { try { new ResizeObserver(size).observe(document.documentElement); } catch (e) {} }
  post('hello', {});
})();
</script>"""


def _init_json(init: Optional[dict]) -> str:
    """Начальные данные для моста: безопасно для вставки внутрь <script>."""
    raw = json.dumps(init or {}, ensure_ascii=False)
    return raw.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def build_document(task: dict, site_origin: str = "", init: Optional[dict] = None) -> str:
    """Полный HTML-документ задания: политика безопасности, мост SmartENT, код автора.

    init — прогресс ученика и режим. Он встраивается в сам документ, чтобы задание
    получило сохранённое (в том числе свой localStorage) сразу при запуске."""
    head = (f'<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta http-equiv="Content-Security-Policy" content="{csp(site_origin)}">'
            + BRIDGE.replace("__SMARTENT_INIT__", _init_json(init)))
    if task.get("use_full_doc"):
        doc = task.get("full_doc") or ""
        m = re.search(r"<head[^>]*>", doc, re.I)
        if m:
            return doc[:m.end()] + head + doc[m.end():]
        m = re.search(r"<html[^>]*>", doc, re.I)
        if m:
            return doc[:m.end()] + "<head>" + head + "</head>" + doc[m.end():]
        return "<!DOCTYPE html><html><head>" + head + "</head><body>" + doc + "</body></html>"
    css = (task.get("css") or "").replace("</style", "<\\/style")
    js = (task.get("js") or "").replace("</script", "<\\/script")
    return ("<!DOCTYPE html><html lang=\"ru\"><head>" + head + "<style>" + css + "</style></head><body>"
            + (task.get("html") or "") + "<script>" + js + "\n</script></body></html>")


# ───────────────────────── проверка ответа на сервере ─────────────────────────

def parse_key(raw: str) -> tuple:
    """Ключ ответа автора: JSON {"type": ..., "answer": ...}. Возвращает (ключ, ошибка)."""
    try:
        key = json.loads(raw or "")
    except ValueError as e:
        return None, f"это не JSON ({e.msg}, строка {e.lineno})."
    if not isinstance(key, dict) or key.get("type") not in KEY_TYPES or "answer" not in key:
        return None, "нужен объект {\"type\": один из " + ", ".join(KEY_TYPES) + ", \"answer\": …}."
    t, a = key["type"], key["answer"]
    if t in ("set", "sequence") and not isinstance(a, list):
        return None, f"для {t} ответ должен быть списком."
    if t in ("pairs", "fields") and not (isinstance(a, dict) and a):
        return None, f"для {t} ответ должен быть объектом с ключами."
    if t == "number":
        try:
            float(a)
            float(key.get("tolerance", 0) or 0)
        except (TypeError, ValueError):
            return None, "для number ответ и tolerance должны быть числами."
    return key, ""


def _n(v, cs: bool) -> str:
    s = re.sub(r"\s+", " ", str(v if v is not None else "")).strip().replace("ё", "е").replace("Ё", "Е")
    return s if cs else s.casefold()


def _canon(v, cs: bool):
    """Каноничный вид для «точного» сравнения: строки без регистра и лишних пробелов."""
    if isinstance(v, str):
        return _n(v, cs)
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, list):
        return [_canon(x, cs) for x in v]
    if isinstance(v, dict):
        return {str(k): _canon(x, cs) for k, x in v.items()}
    return str(v)


def grade(key: dict, answer, max_score: int) -> dict:
    """Баллы по ключу. Частичный зачёт — у pairs и fields (по каждому ключу)."""
    cs = bool(key.get("case_sensitive"))
    t, right = key["type"], key["answer"]
    part, total = 0, 1
    if t == "exact":
        part = 1 if _canon(answer, cs) == _canon(right, cs) else 0
    elif t == "set":
        ok = isinstance(answer, list) and sorted(map(str, _canon(answer, cs))) == sorted(map(str, _canon(right, cs)))
        part = 1 if ok else 0
    elif t == "sequence":
        part = 1 if isinstance(answer, list) and _canon(answer, cs) == _canon(right, cs) else 0
    elif t == "number":
        try:
            got = float(str(answer).replace(",", ".").strip())
            part = 1 if abs(got - float(right)) <= float(key.get("tolerance", 0) or 0) + 1e-12 else 0
        except (TypeError, ValueError):
            part = 0
    items = None
    if t in ("pairs", "fields"):
        total = len(right)
        got = answer if isinstance(answer, dict) else {}
        items = {}
        for k, v in right.items():
            given = got.get(k)
            accepted = v if (t == "fields" and isinstance(v, list)) else [v]
            ok_item = given is not None and _canon(given, cs) in [_canon(x, cs) for x in accepted]
            items[str(k)] = ok_item
            if ok_item:
                part += 1
    score = round(max_score * part / total) if total else 0
    out = {"score": score, "max": max_score, "percent": round(score / max_score * 100, 1) if max_score else 0,
           "correct": part == total, "parts": part, "parts_total": total}
    if items is not None and key.get("feedback", "items") != "total":
        out["items"] = items          # какие пары/поля верны — чтобы задание показало ошибки
    return out


# ───────────────────────── ученик: прогресс и результаты ─────────────────────────

def load_progress(user_id: int, task_id: int):
    row = db.fetchone("SELECT state_json FROM interactive_progress WHERE user_id=? AND task_id=?",
                      (int(user_id), int(task_id)))
    if not row or not row["state_json"]:
        return None
    try:
        return json.loads(row["state_json"])
    except ValueError:
        return None


def save_progress(user_id: int, task_id: int, state) -> str:
    raw = json.dumps(state, ensure_ascii=False)
    if len(raw.encode("utf-8")) > MAX_STATE:
        return "Прогресс слишком большой (больше 50 КБ)."
    db.execute("INSERT INTO interactive_progress (user_id, task_id, state_json, updated_at) VALUES (?,?,?,?) "
               "ON CONFLICT(user_id, task_id) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at",
               (int(user_id), int(task_id), raw, utils.now_iso()))
    return ""


def submit(task: dict, user_id: int, answer, lesson_id=None) -> dict:
    """Ответ ученика. Для задания с оценкой баллы считает сервер по ключу автора."""
    raw = json.dumps(answer, ensure_ascii=False)
    if len(raw.encode("utf-8")) > MAX_ANSWER:
        return {"error": "Ответ слишком большой."}
    if task.get("grading") != SERVER:
        return {"error": "Это тренировочное задание: результат считает само задание (SmartENT.reportScore).",
                "kind": PRACTICE}
    key, err = parse_key(task.get("answer_key") or "")
    if err:
        return {"error": "Задание настроено с ошибкой, сообщите преподавателю."}
    limit = 1 if not task.get("allow_retry") else int(task.get("max_attempts") or 0)
    if limit:
        used = db.fetchone("SELECT COUNT(*) AS c FROM interactive_results WHERE task_id=? AND user_id=? "
                           "AND kind='graded'", (task["id"], int(user_id)))["c"]
        if used >= limit:
            last = db.fetchone("SELECT * FROM interactive_results WHERE task_id=? AND user_id=? AND kind='graded' "
                               "ORDER BY id DESC LIMIT 1", (task["id"], int(user_id)))
            note = ("Засчитана первая попытка: повторная отправка в этом задании не меняет оценку."
                    if limit == 1 else f"Попытки закончились ({limit} из {limit}). Засчитана последняя.")
            return {"score": last["score"], "max": last["max_score"], "percent": last["percent"],
                    "correct": last["score"] >= last["max_score"], "kind": "graded", "repeat": True,
                    "attempts_left": 0, "note": note}
    res = grade(key, answer, int(task.get("max_score") or 10))
    db.execute("INSERT INTO interactive_results (task_id, user_id, kind, score, max_score, percent, answer_json, "
               "details_json, lesson_id) VALUES (?,?,?,?,?,?,?,?,?)",
               (task["id"], int(user_id), "graded", res["score"], res["max"], res["percent"], raw,
                json.dumps({"parts": res["parts"], "parts_total": res["parts_total"]}), lesson_id))
    res["kind"] = "graded"
    if limit:
        res["attempts_left"] = max(0, limit - used - 1)
    return res


def report_practice(task: dict, user_id: int, score, max_score, lesson_id=None) -> dict:
    """Результат, посчитанный самим заданием. Сохраняется только как тренировочный."""
    try:
        mx = max(1, min(100000, int(float(max_score))))
        sc = max(0, min(mx, int(float(score))))
    except (TypeError, ValueError):
        return {"error": "Неверный результат: нужны числа score и max."}
    pct = round(sc / mx * 100, 1)
    # Защита от засорения: не больше 60 тренировочных результатов в час на задание
    recent = db.fetchone("SELECT COUNT(*) AS c FROM interactive_results WHERE task_id=? AND user_id=? AND kind=? "
                         "AND created_at >= datetime('now', '-1 hour')", (task["id"], int(user_id), PRACTICE))["c"]
    if recent >= 60:
        return {"score": sc, "max": mx, "percent": pct, "kind": PRACTICE, "saved": False}
    db.execute("INSERT INTO interactive_results (task_id, user_id, kind, score, max_score, percent, lesson_id) "
               "VALUES (?,?,?,?,?,?,?)", (task["id"], int(user_id), PRACTICE, sc, mx, pct, lesson_id))
    return {"score": sc, "max": mx, "percent": pct, "kind": PRACTICE}


def user_results(task_id: int, user_id: int, limit: int = 10) -> list:
    return [dict(r) for r in db.fetchall(
        "SELECT kind, score, max_score, percent, created_at FROM interactive_results WHERE task_id=? AND user_id=? "
        "ORDER BY id DESC LIMIT ?", (int(task_id), int(user_id), int(limit)))]


def admin_results(task_id: int, limit: int = 300) -> list:
    return [dict(r) for r in db.fetchall(
        "SELECT r.*, u.username, u.first_name, u.tg_id, l.title AS lesson_title FROM interactive_results r "
        "JOIN users u ON u.id=r.user_id LEFT JOIN lessons l ON l.id=r.lesson_id "
        "WHERE r.task_id=? ORDER BY r.id DESC LIMIT ?", (int(task_id), int(limit)))]
