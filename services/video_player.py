"""Плеер для видео-уроков кодом: как обычное видео (v98, переделан в v99).

Понимает два вида роликов:
  • сцены платформы: <section data-se-scene data-duration="8" data-say="…">;
  • ролики «по кадрам» от нейросети владельца: window.LESSON = {frames: [...]} и
    window.__show(n) в index.html (кадры с озвучкой audio/NN.mp3, отметки at и end).
Свои кнопки ролика («← Шаг», «Шаг →», «Сначала», полоска кадров) прячутся.
Вместо них одна панель, как у обычного видео: пуск и пауза, полоса перемотки
с минутами, −5 и +5 секунд, скорость 0.5×–2×, субтитры и весь экран.

Весь экран просит страница урока (она прячет меню и растягивает окно ролика),
а если ролик открыт без неё — сам документ. Выйти можно кнопкой, клавишей Esc
и повторным нажатием ⛶.
"""
import re

RUNTIME = r"""<style id="sev-style">
.sev-player{position:relative;max-width:100%}
.sev-box{position:relative;overflow:hidden;border-radius:12px 12px 0 0}
.sev-player .sev-stage{border-radius:12px 12px 0 0}
.sev-stage{position:relative;overflow:hidden}
[data-se-scene]{position:absolute;inset:0;opacity:0;visibility:hidden;transition:opacity .35s;pointer-events:none}
[data-se-scene].se-on{opacity:1;visibility:visible;pointer-events:auto}
.sev-sub{display:flex;align-items:center;justify-content:center;text-align:center;min-height:3.1em;padding:.4em 1em;
  background:#0B3D2E;color:#fff;border-left:6px solid #C9A227;font:600 clamp(14px,1.9vw,19px)/1.4 system-ui,-apple-system,"Segoe UI",sans-serif}
.sev-sub[hidden]{display:none}
.sev-big{position:absolute;inset:0;margin:auto;width:84px;height:84px;border-radius:50%;border:0;z-index:30;cursor:pointer;
  background:rgba(0,0,0,.62);color:#fff;font-size:34px;line-height:84px;padding:0 0 0 6px;box-shadow:0 4px 18px rgba(0,0,0,.35)}
.sev-big.sev-again{padding:0}
.sev-big[hidden]{display:none}
.sev-wait{position:absolute;inset:0;margin:auto;width:46px;height:46px;border-radius:50%;z-index:25;pointer-events:none;
  border:4px solid rgba(255,255,255,.35);border-top-color:#fff;animation:sevspin .8s linear infinite}
.sev-wait[hidden]{display:none}
@keyframes sevspin{to{transform:rotate(360deg)}}
.sev-bar{background:#111;color:#fff;padding:2px 10px 8px;border-radius:0 0 12px 12px;
  font:14px/1.2 system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-user-select:none;user-select:none;transition:opacity .25s}
.sev-seek{display:block;width:100%;margin:8px 0 6px;accent-color:#e5484d;cursor:pointer;height:18px}
.sev-row{display:flex;align-items:center;gap:6px}
.sev-row button,.sev-row select{background:transparent;color:#fff;border:1px solid rgba(255,255,255,.28);border-radius:8px;
  min-height:36px;min-width:40px;padding:2px 9px;font:inherit;font-weight:600;cursor:pointer;flex:none}
.sev-row button.sev-off{opacity:.45}
.sev-row select option{color:#111}
.sev-time{font-variant-numeric:tabular-nums;opacity:.95;margin-left:2px;white-space:nowrap;flex:none}
.sev-chap{opacity:.7;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;flex:1 1 auto;margin-left:6px}
@media (max-width:560px){.sev-row{gap:4px}.sev-row button,.sev-row select{min-width:34px;padding:2px 6px;min-height:34px}
  .sev-chap{display:none}.sev-time{flex:1 1 auto;font-size:13px;min-width:0;overflow:hidden;text-overflow:ellipsis}
  .sev-row .sev-back,.sev-row .sev-fwd{display:none}}
html.sev-lesson .tap{display:none!important}
html.sev-fs,html.sev-fs body{overflow:hidden!important;background:#000!important}
html.sev-fs{--sev-caph:clamp(52px,12vh,120px)}
html.sev-fs .sev-player{position:fixed;inset:0;z-index:2147483000;background:#000;display:flex;flex-direction:column;align-items:center;
  justify-content:center;max-width:none}
html.sev-fs .sev-box{width:min(100vw,calc((100vh - var(--sev-caph)) * 16 / 9));border-radius:0;background:#000;flex:none}
html.sev-fs.sev-nocc .sev-box{width:min(100vw,calc(100vh * 16 / 9))}
html.sev-fs .sev-sub{width:min(100vw,calc((100vh - var(--sev-caph)) * 16 / 9));height:var(--sev-caph);min-height:0;flex:none;
  border-left:0;background:#000;font-size:clamp(14px,2.8vh,28px);padding:.2em 1em}
html.sev-fs .sev-stage{width:100%!important;max-width:none!important;margin:0!important;border-radius:0!important;box-shadow:none!important}
html.sev-fs .sev-bar{position:absolute;left:0;right:0;bottom:0;border-radius:0;background:linear-gradient(rgba(0,0,0,0),rgba(0,0,0,.88) 45%);
  padding:26px 14px 10px}
html.sev-fs.sev-idle .sev-bar{opacity:0;pointer-events:none}
html.sev-fs.sev-idle .sev-player{cursor:none}
</style>
<script id="sev-runtime">
(function () {
  'use strict';
  var RATES = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2];
  var P = (window.parent && window.parent !== window) ? window.parent : null;
  function fmt(s) { s = Math.max(0, Math.floor(s || 0)); var h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
    return (h ? h + ':' + (m < 10 ? '0' : '') : '') + m + ':' + (x < 10 ? '0' : '') + x; }
  function words(t) { return (t || '').trim().split(/\s+/).filter(Boolean).length; }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e; }
  function post(type, data) { try { if (P) P.postMessage({__smartent: 1, type: type, data: data}, '*'); } catch (e) {} }
  function hide(e) { if (e) e.style.setProperty('display', 'none', 'important'); }
  function frameAt(list, x) { var lo = 0, hi = list.length - 1;
    while (lo < hi) { var mid = (lo + hi + 1) >> 1; if (list[mid].start <= x) lo = mid; else hi = mid - 1; } return lo; }

  /* ── ролик из сцен платформы ── */
  function scenesEngine() {
    var scenes = [].slice.call(document.querySelectorAll('[data-se-scene]'));
    if (!scenes.length) return null;
    var stage = scenes[0].parentElement;
    if (!stage.getBoundingClientRect().height) stage.style.aspectRatio = '16 / 9';
    var lang = stage.getAttribute('data-se-lang') || document.documentElement.lang || 'ru-RU';
    var list = scenes.map(function (s) {
      var say = s.getAttribute('data-say') || '', d = parseFloat(s.getAttribute('data-duration'));
      if (!(d > 0)) d = Math.max(4, words(say) / 2.3 + 1.5);
      return {el: s, say: say, dur: d, audio: s.querySelector('audio[data-se-audio]')};
    });
    var total = 0;
    list.forEach(function (s) { s.start = total; total += s.dur; });
    var hasAudio = list.some(function (s) { return s.audio; });
    var canVoice = !hasAudio && 'speechSynthesis' in window && list.some(function (s) { return s.say; });
    var cur = -1, spoken = -1, voice = canVoice;
    function stopVoice() { try { if (canVoice) speechSynthesis.cancel(); } catch (e) {} }
    return {
      kind: 'scenes', stage: stage, total: total, canVoice: canVoice, hasSubs: list.some(function (s) { return s.say; }),
      setVoice: function (on) { voice = on; if (!on) stopVoice(); },
      at: function (t) { var i = frameAt(list, Math.min(t, total - 0.001)); return {i: i, sub: list[i].say, chap: '', start: list[i].start}; },
      apply: function (t, playing, rate, jumped) {
        var i = frameAt(list, Math.min(t, total - 0.001)), s = list[i], local = t - s.start;
        if (i !== cur || jumped) {
          list.forEach(function (o, k) { o.el.classList.toggle('se-on', k === i); if (k !== i && o.audio) o.audio.pause(); });
          if (i !== cur) { spoken = -1; stopVoice(); }
          cur = i;
        }
        var anims = s.el.getAnimations ? s.el.getAnimations({subtree: true}) : [];
        anims.forEach(function (a) { try { a.pause(); a.currentTime = local * 1000; } catch (e) {} });
        if (s.audio) {
          try { if (Math.abs(s.audio.currentTime - local) > 0.35) s.audio.currentTime = local; s.audio.playbackRate = rate; } catch (e) {}
          if (playing && s.audio.paused) s.audio.play().catch(function () {});
          if (!playing && !s.audio.paused) s.audio.pause();
        }
        if (voice && playing && spoken !== i && s.say && local <= 1.5) {
          spoken = i; stopVoice();
          try { var u = new SpeechSynthesisUtterance(s.say); u.lang = lang; u.rate = Math.min(2, Math.max(0.5, rate)); speechSynthesis.speak(u); } catch (e) {}
        }
      },
      clock: function () { return null; },
      waiting: function () { return false; },
      pause: function () { try { if (canVoice) speechSynthesis.cancel(); } catch (e) {} spoken = -1;
        list.forEach(function (o) { if (o.audio) o.audio.pause(); }); },
      seeked: function () { spoken = -1; stopVoice(); }
    };
  }

  /* ── ролик «по кадрам» (window.LESSON + window.__show) ── */
  function lessonEngine() {
    var D = window.LESSON;
    if (!D || !D.frames || !D.frames.length || typeof window.__show !== 'function') return null;
    var F = D.frames, stage = document.getElementById('stage') || document.querySelector('.stage');
    if (!stage) return null;
    try { if (typeof window.__stop === 'function') window.__stop(); } catch (e) {}
    document.documentElement.classList.add('sev-lesson');
    ['prog', 'cap'].forEach(function (id) { hide(document.getElementById(id)); });
    ['play', 'where'].forEach(function (id) { var b = document.getElementById(id); if (b) hide(b.parentElement); });
    ['prev', 'next', 'again'].forEach(function (id) { hide(document.getElementById(id)); });
    var list = [], total = 0;
    F.forEach(function (f, k) {
      var d, nx = F[k + 1];
      if (f.au && f.at != null) {
        if (nx && nx.au === f.au && nx.at != null) d = Math.max(0.05, nx.at - f.at);
        else d = Math.max(0.3, (f.end != null ? f.end : f.at + 3) - f.at + 0.45);
      } else d = f.kind === 'timer' ? 1 : Math.max(2.6, (f.cap || '').length * 0.062 + 1);
      list.push({f: f, start: total, dur: d}); total += d;
    });
    var g = null;
    list.forEach(function (o, k) {
      if (o.f.au && o.f.at != null && k > 0 && g && list[k - 1].f.au === o.f.au) { o.g = g; }
      else if (o.f.au && o.f.at != null) { g = o; o.g = o; }
      else g = null;
    });
    var A = new Audio(); A.preload = 'auto';
    var src = '', cur = -1, pend = null, bad = {}, holdSince = 0, wantSeek = true, curG = null;
    A.addEventListener('loadedmetadata', function () { if (pend != null) { try { A.currentTime = pend; } catch (e) {} pend = null; } });
    A.addEventListener('error', function () { if (src) bad[src] = 1; });
    function use(u) { if (src === u) return; src = u; pend = null; try { A.pause(); } catch (e) {} A.src = u; try { A.load(); } catch (e) {} }
    function chap(f) { return f.ch >= 0 && D.chapters ? (D.chapters[f.ch] || '') : 'Введение'; }
    return {
      kind: 'lesson', stage: stage, total: total, canVoice: false, hasSubs: F.some(function (f) { return f.cap; }),
      at: function (t) { var i = frameAt(list, Math.min(t, total - 0.001)); return {i: i, sub: list[i].f.cap || '', chap: chap(list[i].f), start: list[i].start}; },
      apply: function (t, playing, rate, jumped) {
        var i = frameAt(list, Math.min(t, total - 0.001)), o = list[i], f = o.f, local = t - o.start;
        if (i !== cur) { try { window.__show(i); } catch (e) {} cur = i; }
        if (jumped) wantSeek = true;
        if (o.g !== curG) { if (o.g) wantSeek = true; curG = o.g; }
        if (f.au && f.at != null && !bad[f.au]) {
          var pos = f.at + local, tail = f.end != null && pos > f.end + 0.05;
          if (src !== f.au) { use(f.au); pend = pos; wantSeek = false; }
          else if (wantSeek && A.readyState >= 1) { try { A.currentTime = pos; } catch (e) {} wantSeek = false; }
          else if (wantSeek) { pend = pos; wantSeek = false; }
          try { A.defaultPlaybackRate = rate; A.playbackRate = rate; } catch (e) {}
          if (playing && !tail && A.paused && !A.ended) { var pr = A.play(); if (pr && pr.catch) pr.catch(function () {}); }
          if ((!playing || tail) && !A.paused) A.pause();
        } else if (!A.paused) A.pause();
      },
      /* Пока звучит озвучка, время ролика идёт по звуку: картинка и голос не расходятся. */
      clock: function (t, playing) {
        if (!playing) return null;
        var o = list[frameAt(list, Math.min(t, total - 0.001))], f = o.f;
        if (!f.au || f.at == null || bad[f.au] || src !== f.au) { holdSince = 0; return null; }
        if (f.end != null && f.at + (t - o.start) > f.end + 0.05) { holdSince = 0; return null; }
        if (A.paused || A.ended) { holdSince = 0; return null; }
        if (A.seeking || A.readyState < 3) {
          if (!holdSince) holdSince = performance.now();
          if (performance.now() - holdSince > 6000) return null;
          return {hold: true};
        }
        holdSince = 0;
        var ta = o.g.start + (A.currentTime - o.g.f.at);
        if (Math.abs(ta - t) > 2) return null;
        return {t: Math.max(ta, o.start)};
      },
      waiting: function () { return holdSince && performance.now() - holdSince > 350; },
      pause: function () { try { A.pause(); } catch (e) {} },
      seeked: function () { wantSeek = true; }
    };
  }

  function boot() {
    var E = lessonEngine() || scenesEngine();
    if (!E) return;                                    // обычная страница без ролика
    var stage = E.stage;
    stage.classList.add('sev-stage');
    var player = el('div', 'sev-player'), box = el('div', 'sev-box');
    stage.parentNode.insertBefore(player, stage);
    player.appendChild(box); box.appendChild(stage);
    var sub = el('div', 'sev-sub'), subTx = el('span'); sub.appendChild(subTx); sub.hidden = true;
    var big = el('button', 'sev-big', '▶'), spin = el('div', 'sev-wait'); spin.hidden = true;
    big.type = 'button'; big.setAttribute('aria-label', 'Смотреть');
    box.appendChild(spin); box.appendChild(big); player.appendChild(sub);
    var bar = el('div', 'sev-bar');
    var seek = el('input', 'sev-seek'); seek.type = 'range'; seek.min = 0; seek.max = Math.max(1, Math.round(E.total * 10)); seek.step = 1; seek.value = 0;
    seek.setAttribute('aria-label', 'Перемотка');
    var row = el('div', 'sev-row');
    var bPlay = el('button', 'sev-play', '▶'), bBack = el('button', 'sev-back', '⟲ 5'), bFwd = el('button', 'sev-fwd', '5 ⟳');
    var time = el('span', 'sev-time'), chapEl = el('span', 'sev-chap');
    var bVoice = el('button', 'sev-voice', '🔊'), bCC = el('button', 'sev-cc', 'CC'), speed = el('select', 'sev-speed'), bFull = el('button', 'sev-full', '⛶');
    [bPlay, bBack, bFwd, bVoice, bCC, bFull].forEach(function (b) { b.type = 'button'; });
    bPlay.setAttribute('aria-label', 'Пуск или пауза'); bBack.title = 'Назад на 5 секунд'; bFwd.title = 'Вперёд на 5 секунд';
    bCC.title = 'Субтитры'; bFull.title = 'На весь экран'; bFull.setAttribute('aria-label', 'На весь экран');
    RATES.forEach(function (r) { var o = el('option', '', r + '×'); o.value = r; if (r === 1) o.selected = true; speed.appendChild(o); });
    speed.setAttribute('aria-label', 'Скорость'); speed.title = 'Скорость';
    row.appendChild(bPlay); row.appendChild(bBack); row.appendChild(bFwd); row.appendChild(time); row.appendChild(chapEl);
    if (!E.kind || E.kind !== 'lesson') { var gap = el('span', 'sev-chap'); if (E.kind === 'scenes') chapEl.remove(); row.appendChild(gap); }
    if (E.canVoice) row.appendChild(bVoice);
    if (E.hasSubs) row.appendChild(bCC);
    row.appendChild(speed); row.appendChild(bFull);
    bar.appendChild(seek); bar.appendChild(row); player.appendChild(bar);

    var t = 0, playing = false, rate = 1, ended = false, last = 0, jumped = true, cc = true, voice = E.canVoice, fs = false, drag = false;
    var lastI = -1, idleT = null, handlers = {frame: [], scene: []};
    function emit(n) { var args = [].slice.call(arguments, 1); handlers[n].forEach(function (f) { try { f.apply(null, args); } catch (e) {} }); }
    try { if (localStorage.getItem('se_video_cc') === '0') cc = false; } catch (e) {}
    function paintCC() { bCC.classList.toggle('sev-off', !cc); bCC.setAttribute('aria-pressed', cc ? 'true' : 'false');
      document.documentElement.classList.toggle('sev-nocc', !cc || !E.hasSubs); }
    paintCC();
    function render() {
      E.apply(t, playing, rate, jumped); jumped = false;
      var a = E.at(t);
      if (a.i !== lastI) emit('scene', a.i);
      if (a.i !== lastI || sub.__cc !== cc) { subTx.textContent = a.sub || ''; sub.hidden = !cc || !E.hasSubs; chapEl.textContent = a.chap || ''; lastI = a.i; sub.__cc = cc; }
      if (!drag) seek.value = Math.round(t * 10);
      time.textContent = fmt(t) + ' / ' + fmt(E.total);
      spin.hidden = !(playing && E.waiting());
      emit('frame', t - (a.start || 0), a.i, t);
    }
    function frame(now) {
      if (playing) {
        var dt = Math.min(0.25, Math.max(0, (now - last) / 1000)); last = now;
        var c = E.clock(t, playing);
        if (c && c.t != null) t = c.t; else if (!(c && c.hold)) t += dt * rate;
        if (t >= E.total) { t = E.total; pause(); ended = true; big.textContent = '↻'; big.classList.add('sev-again'); big.hidden = false; showBar(); }
        render();
      } else last = now;
      requestAnimationFrame(frame);
    }
    function play() {
      if (ended || t >= E.total - 0.05) { t = 0; ended = false; jumped = true; lastI = -1; }
      playing = true; last = performance.now(); bPlay.textContent = '⏸'; big.hidden = true; big.classList.remove('sev-again'); big.textContent = '▶';
      render(); idle();
    }
    function pause() { playing = false; bPlay.textContent = '▶'; E.pause(); big.hidden = false; showBar(); render(); }
    function toggle() { playing ? pause() : play(); }
    function seekTo(x) { t = Math.max(0, Math.min(E.total, x)); ended = false; jumped = true; E.seeked(); big.textContent = '▶'; big.classList.remove('sev-again'); render(); }

    /* ── весь экран ── */
    var fsWait = null;
    function setFs(on) {
      fs = !!on; clearTimeout(fsWait);
      document.documentElement.classList.toggle('sev-fs', fs);
      // В полном экране панель лежит на нижнем крае картинки и не закрывает субтитры
      if (fs && bar.parentNode !== box) box.appendChild(bar);
      if (!fs && bar.parentNode !== player) player.appendChild(bar);
      bFull.textContent = fs ? '⤡' : '⛶'; bFull.title = fs ? 'Выйти из полного экрана' : 'На весь экран';
      bFull.setAttribute('aria-label', bFull.title);
      [0, 120, 400].forEach(function (ms) { setTimeout(function () { try { window.dispatchEvent(new Event('resize')); } catch (e) {} }, ms); });
      showBar(); idle();
    }
    function selfFs(on) {
      var d = document.documentElement;
      if (on) {
        var req = d.requestFullscreen || d.webkitRequestFullscreen;
        if (req) { try { var p = req.call(d); if (p && p.then) p.then(function () { setFs(true); }, function () { setFs(true); }); else setFs(true); return; } catch (e) {} }
        setFs(true);                                    // браузер не умеет: растягиваем на всё окно
      } else {
        var ex = document.exitFullscreen || document.webkitExitFullscreen;
        if ((document.fullscreenElement || document.webkitFullscreenElement) && ex) { try { ex.call(document); } catch (e) {} }
        setFs(false);
      }
    }
    function wantFs(on) {
      if (!P) { selfFs(on); return; }
      post('fullscreen', {on: !!on});
      fsWait = setTimeout(function () { selfFs(on); }, 500);   // страница не ответила — сами
    }
    window.addEventListener('message', function (e) {
      if (!P || e.source !== P) return;
      var m = e.data || {};
      if (m.__smartent && m.type === 'fullscreen_state') { clearTimeout(fsWait); setFs(!!(m.data && m.data.on)); }
    });
    function onFsChange() { if (!P && !(document.fullscreenElement || document.webkitFullscreenElement) && fs) setFs(false); }
    document.addEventListener('fullscreenchange', onFsChange);
    document.addEventListener('webkitfullscreenchange', onFsChange);

    /* ── панель прячется в полном экране, пока идёт ролик ── */
    function showBar() { document.documentElement.classList.remove('sev-idle'); }
    function idle() {
      clearTimeout(idleT);
      if (fs && playing) idleT = setTimeout(function () { if (fs && playing && !drag) document.documentElement.classList.add('sev-idle'); }, 2600);
    }
    function wake() { showBar(); idle(); }
    ['pointermove', 'pointerdown', 'touchstart'].forEach(function (ev) { player.addEventListener(ev, wake, {passive: true}); });

    bPlay.onclick = toggle;
    big.onclick = function (e) { e.stopPropagation(); play(); };
    bBack.onclick = function () { seekTo(t - 5); };
    bFwd.onclick = function () { seekTo(t + 5); };
    seek.addEventListener('input', function () { drag = true; seekTo(seek.value / 10); });
    seek.addEventListener('change', function () { drag = false; idle(); });
    seek.addEventListener('pointerup', function () { drag = false; idle(); });
    speed.onchange = function () { rate = parseFloat(speed.value) || 1; jumped = true; render(); };
    bVoice.onclick = function () { voice = !voice; bVoice.textContent = voice ? '🔊' : '🔇'; if (E.setVoice) E.setVoice(voice); };
    bCC.onclick = function () { cc = !cc; paintCC(); try { localStorage.setItem('se_video_cc', cc ? '1' : '0'); } catch (e) {} render(); };
    bFull.onclick = function () { wantFs(!fs); };
    var tapT = null;
    box.addEventListener('click', function (e) {
      if (e.target.closest && e.target.closest('.sev-big, .sev-bar')) return;
      var touch = e.pointerType === 'touch' || (e.sourceCapabilities && e.sourceCapabilities.firesTouchEvents);
      if (fs && touch && document.documentElement.classList.contains('sev-idle')) { wake(); return; }
      clearTimeout(tapT); tapT = setTimeout(toggle, 220);
    });
    // Двойное нажатие: по левому краю −5 с, по правому +5 с, посередине — весь экран
    box.addEventListener('dblclick', function (e) {
      if (e.target.closest && e.target.closest('.sev-bar, .sev-big')) return;
      clearTimeout(tapT);
      var r = box.getBoundingClientRect(), x = r.width ? (e.clientX - r.left) / r.width : 0.5;
      if (x < 0.3) seekTo(t - 5); else if (x > 0.7) seekTo(t + 5); else wantFs(!fs);
      wake();
    });
    window.addEventListener('keydown', function (e) {
      var tg = e.target && e.target.tagName;
      if (tg && /^(INPUT|SELECT|TEXTAREA)$/.test(tg) && e.target !== seek) return;
      var k = e.key, done = true;
      if (k === ' ' || k === 'k' || k === 'K') toggle();
      else if (k === 'ArrowLeft') seekTo(t - 5);
      else if (k === 'ArrowRight') seekTo(t + 5);
      else if (k === 'f' || k === 'F' || k === 'а' || k === 'А') wantFs(!fs);
      else if (k === 'c' || k === 'C' || k === 'с' || k === 'С') bCC.onclick();
      else if (k === 'Escape' && fs) wantFs(false);
      else done = false;
      if (done) { e.preventDefault(); e.stopPropagation(); wake(); }
    }, true);
    document.querySelectorAll('img').forEach(function (i) { i.setAttribute('draggable', 'false'); });
    window.SEVideo = {
      duration: E.total, kind: E.kind, get time() { return t; }, get playing() { return playing; }, get fullscreen() { return fs; },
      get rate() { return rate; },
      play: play, pause: pause, seek: seekTo, fullscreen_toggle: function (on) { wantFs(on === undefined ? !fs : on); },
      setRate: function (r) { rate = r; speed.value = r; jumped = true; render(); },
      on: function (n, f) { if (handlers[n] && typeof f === 'function') handlers[n].push(f); }
    };
    render();
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
