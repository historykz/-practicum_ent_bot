"""Плеер для видео-уроков кодом (v98): как обычное видео.

Нейросеть делает ролик из сцен: <section data-se-scene data-duration="8" data-say="…">.
Плеер платформы сам показывает сцены по времени и рисует привычную панель:
пуск и пауза, сколько идёт и сколько всего, полоса перемотки, −5 и +5 секунд,
скорость 0.5×–2×, субтитры и озвучка. CSS-анимации внутри сцены идут по времени ролика,
поэтому перемотка и скорость управляют и ими. Ролики без data-se-scene (старые, со своими
кнопками) показываются как раньше.
"""
import re

RUNTIME = r"""<style id="sev-style">
.sev-stage{position:relative;overflow:hidden}
[data-se-scene]{position:absolute;inset:0;opacity:0;visibility:hidden;transition:opacity .35s;pointer-events:none}
[data-se-scene].se-on{opacity:1;visibility:visible;pointer-events:auto}
.sev-sub{position:absolute;left:4%;right:4%;bottom:4%;z-index:20;background:rgba(0,0,0,.66);color:#fff;
  padding:.45em .8em;border-radius:10px;font:600 clamp(13px,2.3vw,20px)/1.35 system-ui,-apple-system,sans-serif;text-align:center}
.sev-sub[hidden]{display:none}
.sev-big{position:absolute;inset:0;margin:auto;width:86px;height:86px;border-radius:50%;border:0;z-index:30;cursor:pointer;
  background:rgba(0,0,0,.6);color:#fff;font-size:36px;line-height:86px;padding:0 0 0 6px;box-shadow:0 4px 18px rgba(0,0,0,.3)}
.sev-big[hidden]{display:none}
.sev-bar{background:#111;color:#fff;padding:4px 10px 8px;border-radius:0 0 12px 12px;
  font:14px/1.2 system-ui,-apple-system,sans-serif;-webkit-user-select:none;user-select:none}
.sev-seek{width:100%;margin:6px 0 4px;accent-color:#e5484d;cursor:pointer}
.sev-row{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.sev-row button,.sev-row select{background:transparent;color:#fff;border:1px solid rgba(255,255,255,.28);border-radius:8px;
  min-height:36px;min-width:40px;padding:2px 10px;font:inherit;font-weight:600;cursor:pointer}
.sev-row select option{color:#111}
.sev-time{font-variant-numeric:tabular-nums;opacity:.92;margin-left:4px;white-space:nowrap}
.sev-gap{flex:1}
</style>
<script id="sev-runtime">
(function () {
  'use strict';
  var RATES = [0.5, 0.75, 1, 1.25, 1.5, 2];
  function fmt(s) { s = Math.max(0, Math.floor(s)); var m = Math.floor(s / 60), x = s % 60; return m + ':' + (x < 10 ? '0' : '') + x; }
  function words(t) { return (t || '').trim().split(/\s+/).filter(Boolean).length; }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e; }
  function boot() {
    var scenes = [].slice.call(document.querySelectorAll('[data-se-scene]'));
    if (!scenes.length) return;                         // старый ролик со своими кнопками
    var stage = scenes[0].parentElement;
    stage.classList.add('sev-stage');
    if (!stage.getBoundingClientRect().height) stage.style.aspectRatio = '16 / 9';
    var lang = stage.getAttribute('data-se-lang') || document.documentElement.lang || 'ru-RU';
    var list = scenes.map(function (s) {
      var say = s.getAttribute('data-say') || '';
      var d = parseFloat(s.getAttribute('data-duration'));
      if (!(d > 0)) d = Math.max(4, words(say) / 2.3 + 1.5);
      return {el: s, say: say, dur: d, audio: s.querySelector('audio[data-se-audio]')};
    });
    var total = 0;
    list.forEach(function (s) { s.start = total; total += s.dur; });
    var hasAudio = list.some(function (s) { return s.audio; });
    var canVoice = !hasAudio && 'speechSynthesis' in window && list.some(function (s) { return s.say; });
    var t = 0, playing = false, rate = 1, cur = -1, last = 0, spoken = -1, voice = canVoice, ended = false;
    var handlers = {frame: [], scene: []};
    function emit(n) { var a = [].slice.call(arguments, 1); handlers[n].forEach(function (f) { try { f.apply(null, a); } catch (e) {} }); }

    var sub = el('div', 'sev-sub'); sub.hidden = true; stage.appendChild(sub);
    var big = el('button', 'sev-big', '▶'); big.type = 'button'; big.setAttribute('aria-label', 'Смотреть'); stage.appendChild(big);
    var bar = el('div', 'sev-bar');
    var seek = el('input', 'sev-seek'); seek.type = 'range'; seek.min = 0; seek.max = Math.round(total * 10); seek.step = 1; seek.value = 0;
    seek.setAttribute('aria-label', 'Перемотка');
    var row = el('div', 'sev-row');
    var bPlay = el('button', '', '▶'), bBack = el('button', '', '⟲ 5'), bFwd = el('button', '', '5 ⟳');
    var time = el('span', 'sev-time'), gap = el('span', 'sev-gap');
    var bVoice = el('button', '', '🔊'), speed = el('select');
    [bPlay, bBack, bFwd, bVoice].forEach(function (b) { b.type = 'button'; });
    bPlay.setAttribute('aria-label', 'Пуск или пауза'); bBack.title = 'Назад на 5 секунд'; bFwd.title = 'Вперёд на 5 секунд';
    RATES.forEach(function (r) { var o = el('option', '', r + '×'); o.value = r; if (r === 1) o.selected = true; speed.appendChild(o); });
    speed.setAttribute('aria-label', 'Скорость');
    row.appendChild(bPlay); row.appendChild(bBack); row.appendChild(bFwd); row.appendChild(time); row.appendChild(gap);
    if (canVoice) row.appendChild(bVoice);
    row.appendChild(speed);
    bar.appendChild(seek); bar.appendChild(row);
    stage.parentNode.insertBefore(bar, stage.nextSibling);

    function sceneAt(x) { for (var i = list.length - 1; i >= 0; i--) if (x >= list[i].start) return i; return 0; }
    function stopVoice() { try { if (canVoice) speechSynthesis.cancel(); } catch (e) {} }
    function speak(s, i, local) {
      if (!voice || !playing || spoken === i || !s.say || local > 1.5) return;
      spoken = i; stopVoice();
      try { var u = new SpeechSynthesisUtterance(s.say); u.lang = lang; u.rate = Math.min(2, Math.max(0.5, rate)); speechSynthesis.speak(u); } catch (e) {}
    }
    function apply() {
      var i = sceneAt(Math.min(t, total - 0.001)), s = list[i], local = t - s.start;
      if (i !== cur) {
        list.forEach(function (o, k) { o.el.classList.toggle('se-on', k === i); if (k !== i && o.audio) o.audio.pause(); });
        cur = i; sub.textContent = s.say; sub.hidden = !s.say; spoken = -1; stopVoice(); emit('scene', i);
      }
      var anims = s.el.getAnimations ? s.el.getAnimations({subtree: true}) : [];
      anims.forEach(function (a) { try { a.pause(); a.currentTime = local * 1000; } catch (e) {} });
      if (s.audio) {
        try { if (Math.abs(s.audio.currentTime - local) > 0.35) s.audio.currentTime = local; s.audio.playbackRate = rate; } catch (e) {}
        if (playing && s.audio.paused) s.audio.play().catch(function () {});
        if (!playing && !s.audio.paused) s.audio.pause();
      }
      speak(s, i, local);
      seek.value = Math.round(t * 10);
      time.textContent = fmt(t) + ' / ' + fmt(total);
      emit('frame', local, i, t);
    }
    function frame(now) {
      if (playing) {
        t += Math.min(0.25, (now - last) / 1000) * rate; last = now;
        if (t >= total) { t = total; pause(); ended = true; big.textContent = '↻'; big.hidden = false; }
        apply();
      }
      requestAnimationFrame(frame);
    }
    function play() {
      if (ended || t >= total) { t = 0; ended = false; cur = -1; }
      playing = true; last = performance.now(); bPlay.textContent = '⏸'; big.hidden = true;
      try { if (canVoice && speechSynthesis.paused) speechSynthesis.resume(); } catch (e) {}
      apply();
    }
    function pause() {
      playing = false; bPlay.textContent = '▶';
      try { if (canVoice) speechSynthesis.pause(); } catch (e) {}
      list.forEach(function (o) { if (o.audio) o.audio.pause(); });
    }
    function seekTo(x) { t = Math.max(0, Math.min(total, x)); ended = false; spoken = -1; stopVoice(); big.textContent = '▶'; apply(); }
    bPlay.onclick = function () { playing ? pause() : play(); };
    big.onclick = play;
    bBack.onclick = function () { seekTo(t - 5); };
    bFwd.onclick = function () { seekTo(t + 5); };
    seek.oninput = function () { seekTo(seek.value / 10); };
    speed.onchange = function () { rate = parseFloat(speed.value) || 1; spoken = -1; };
    bVoice.onclick = function () { voice = !voice; bVoice.textContent = voice ? '🔊' : '🔇'; if (!voice) stopVoice(); };
    stage.addEventListener('click', function (e) { if (e.target === stage || e.target.closest('[data-se-scene]')) { playing ? pause() : play(); } });
    document.addEventListener('keydown', function (e) {
      if (e.target && /^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName) && e.target !== seek) return;
      if (e.key === ' ' || e.key === 'k') { e.preventDefault(); playing ? pause() : play(); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); seekTo(t - 5); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); seekTo(t + 5); }
    });
    document.querySelectorAll('img').forEach(function (i) { i.setAttribute('draggable', 'false'); });
    window.SEVideo = {
      duration: total, get time() { return t; }, get playing() { return playing; },
      play: play, pause: pause, seek: seekTo, setRate: function (r) { rate = r; speed.value = r; },
      on: function (n, f) { if (handlers[n]) handlers[n].push(f); }
    };
    apply();
    requestAnimationFrame(frame);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
</script>"""


def inject(html: str) -> str:
    """Добавить плеер в документ видео-урока (после <head>, до кода нейросети)."""
    if not html or "sev-runtime" in html:
        return html
    i = html.lower().find("</head>")
    if i >= 0:
        return html[:i] + RUNTIME + html[i:]
    m = re.search(r"<body(\s[^>]*)?>", html, re.I)
    if m:
        return html[:m.end()] + RUNTIME + html[m.end():]
    return RUNTIME + html
