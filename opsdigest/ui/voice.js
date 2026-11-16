// Голосовой ввод: микрофон окна (Edge) → PCM 16 кГц → /api/voice/recognize (локальная модель GigaAM v3 в отдельном
// процессе OpsDigest) → текст в поле на месте курсора. Фразы отправляются по паузам — текст появляется, пока говорите.
// Кнопка 🎤 у полей с атрибутом data-voice и у основных полей ввода; клавиша (по умолчанию F9) — в любом поле.
'use strict';
const VC = {st: null, rec: null, target: null, hotkey: 'F9', lastField: null, queue: Promise.resolve()};
const VC_FIELDS = ['as-in', 'mx-in', 'ai-q', 'cm-desc', 'pm-area', 'pm-ask', 'pm-comes', 'pm-note', 'ask-q', 'pr-resp'];

// ---------------------------------------------------------------- кнопки 🎤
function vcAttachAll() {
  VC_FIELDS.forEach(id => { const el = $(id); if (el && !el.dataset.voice) el.dataset.voice = ['mx-in', 'ai-q', 'as-in', 'ask-q'].includes(id) ? 'inline' : '1'; });
  document.querySelectorAll('[data-voice]').forEach(vcAttach);
}
function vcAttach(el) {
  if (el._vc) return; el._vc = true;
  const b = document.createElement('button');
  b.type = 'button'; b.className = 'vc-mic'; b.textContent = '🎤';
  b.title = `Голосовой ввод (${VC.hotkey || 'кнопка'}): нажмите и говорите, ещё раз — стоп`;
  b.addEventListener('mousedown', e => e.preventDefault());  // фокус остаётся в поле
  b.addEventListener('click', e => { e.preventDefault(); vcToggle(el, b); });
  if (el.dataset.voice === 'inline' && el.nextElementSibling) el.parentNode.insertBefore(b, el.nextElementSibling);
  else { const w = document.createElement('span'); w.className = 'vc-wrap'; el.parentNode.insertBefore(w, el); w.appendChild(el); w.appendChild(b); }
  el._vcBtn = b;
}
document.addEventListener('focusin', e => { const t = e.target; if (t && (t.tagName === 'TEXTAREA' || (t.tagName === 'INPUT' && /^(text|search|)$/.test(t.type)))) VC.lastField = t; });
document.addEventListener('keydown', e => {
  if (!VC.hotkey) return;
  const hk = VC.hotkey === 'Ctrl+Space' ? (e.ctrlKey && e.code === 'Space') : e.key === VC.hotkey;
  if (!hk) return;
  const t = document.activeElement;
  const field = t && (t.tagName === 'TEXTAREA' || t.tagName === 'INPUT') ? t : VC.lastField;
  if (!field && !VC.rec) return;
  e.preventDefault(); vcToggle(VC.rec ? VC.target : field, (field || {})._vcBtn);
});

// ---------------------------------------------------------------- запись
async function vcToggle(el, btn) {
  if (VC.rec) { const same = VC.target === el; await vcStopRec(); if (same || !el) return; }
  if (!el) return;
  VC.target = el; el.focus();
  try { await vcStartRec(btn); }
  catch (e) {
    const why = e && e.name === 'NotAllowedError' ? 'микрофон запрещён — разрешите его для OpsDigest (значок слева в строке адреса Edge); если не даёт — запрет политикой компании'
      : e && e.name === 'NotFoundError' ? 'микрофон не найден' : (e.message || String(e));
    toast('🎤 ' + why);
  }
}
async function vcStartRec(btn) {
  const stream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true}});
  let ctx;
  try { ctx = new AudioContext({sampleRate: 16000}); } catch (e) { ctx = new AudioContext(); }
  const src = ctx.createMediaStreamSource(stream);
  const proc = ctx.createScriptProcessor(4096, 1, 1);
  const rec = {stream, ctx, src, proc, btn, buf: [], n: 0, speech: false, silent: 0, sinceVoice: 0, rate: ctx.sampleRate, level: 0, started: Date.now()};
  proc.onaudioprocess = ev => {
    const x = ev.inputBuffer.getChannelData(0);
    let s = 0; for (let i = 0; i < x.length; i++) s += x[i] * x[i];
    const rms = Math.sqrt(s / x.length), dur = x.length / rec.rate;
    rec.level = rms;
    const loud = rms > 0.012;
    if (loud) { rec.speech = true; rec.silent = 0; rec.sinceVoice = 0; } else { rec.silent += dur; rec.sinceVoice += dur; }
    if (rec.speech || rec.buf.length < 3) { rec.buf.push(new Float32Array(x)); rec.n += x.length; }
    else { rec.buf = rec.buf.slice(-2).concat([new Float32Array(x)]); rec.n = rec.buf.reduce((a, b) => a + b.length, 0); }  // немного тишины до фразы
    if (rec.speech && rec.silent > 0.8 && rec.n / rec.rate > 0.6) vcFlush(rec);
    else if (rec.n / rec.rate > 24) vcFlush(rec);  // длинная фраза без пауз
    if (rec.sinceVoice > 45) vcStopRec();  // забыли выключить
    if (btn) btn.style.setProperty('--lvl', Math.min(1, rms * 12).toFixed(2));
  };
  src.connect(proc); proc.connect(ctx.destination);
  VC.rec = rec;
  if (btn) btn.classList.add('on');
  toast('🎤 Говорите… (ещё раз — стоп)');
}
function vcFlush(rec) {
  if (!rec.n || !rec.speech) { rec.buf = []; rec.n = 0; return; }
  const all = new Float32Array(rec.n); let o = 0; rec.buf.forEach(b => { all.set(b, o); o += b.length; });
  rec.buf = []; rec.n = 0; rec.speech = false; rec.silent = 0;
  const pcm = vcPcm16(all, rec.rate);
  const target = VC.target;
  VC.queue = VC.queue.then(() => vcSend(pcm, target)).catch(() => {});
}
async function vcStopRec() {
  const rec = VC.rec; if (!rec) return;
  VC.rec = null;
  vcFlush(rec);
  try { rec.proc.disconnect(); rec.src.disconnect(); rec.stream.getTracks().forEach(t => t.stop()); await rec.ctx.close(); } catch (e) {}
  if (rec.btn) { rec.btn.classList.remove('on'); rec.btn.style.removeProperty('--lvl'); }
  document.querySelectorAll('.vc-mic.on').forEach(b => b.classList.remove('on'));
}
function vcPcm16(x, rate) {
  let y = x;
  if (rate !== 16000) {  // окно не дало 16 кГц — пересчитать (линейно)
    const k = rate / 16000, n = Math.floor(x.length / k); y = new Float32Array(n);
    for (let i = 0; i < n; i++) { const p = i * k, a = Math.floor(p), f = p - a; y[i] = x[a] * (1 - f) + (x[a + 1] || 0) * f; }
  }
  const out = new Int16Array(y.length);
  for (let i = 0; i < y.length; i++) { const v = Math.max(-1, Math.min(1, y[i])); out[i] = v < 0 ? v * 0x8000 : v * 0x7fff; }
  return out.buffer;
}
async function vcSend(pcm, target) {
  const btn = target && target._vcBtn; if (btn) btn.classList.add('busy');
  try {
    const r = await fetch('/api/voice/recognize?rate=16000', {method: 'POST', headers: {'X-Token': T, 'Content-Type': 'application/octet-stream'}, body: pcm});
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'не распознано');
    if (j.text) vcInsert(target, j.text);
  } catch (e) {
    toast('🎤 ' + e.message + (/не установлена|пакет|модель/.test(e.message) ? ' — «Настройки → Голосовой ввод»' : ''));
    if (/не установлена|выключен/.test(e.message)) vcStopRec();
  } finally { if (btn) btn.classList.remove('busy'); }
}
function vcInsert(el, text) {
  if (!el) return;
  const v = el.value, a = el.selectionStart ?? v.length, b = el.selectionEnd ?? v.length;
  const before = v.slice(0, a), after = v.slice(b);
  let t = text;
  if (before && !/[\s\n]$/.test(before) && !/^[\n.,!?;:]/.test(t)) t = ' ' + t;
  if (/[.!?]\s*$/.test(before) || !before.trim()) t = t.replace(/^(\s*)(\S)/, (m, s, c) => s + c.toUpperCase());
  el.value = before + t + after;
  const pos = (before + t).length; el.setSelectionRange(pos, pos);
  el.dispatchEvent(new Event('input', {bubbles: true}));
}

// ---------------------------------------------------------------- «Настройки → Голосовой ввод»
async function vcLoad(force) {
  try { VC.st = await api('/api/voice/status' + (force ? '?force=1' : '')); } catch (e) { msg('vc-msg', e.message, 'bad'); return; }
  const st = VC.st, s = st.settings, rt = st.runtime, m = st.model;
  VC.hotkey = s.hotkey || ''; $('vc-hk-lbl').textContent = VC.hotkey || '—';
  const pk = rt.packages || {};
  $('vc-state').innerHTML =
    `<div class="vs ${rt.ok ? 'ok' : 'bad'}"><b>${rt.ok ? '✓' : '✗'} Пакеты</b> <span class="hint">Python ${esc(rt.python)} · ${esc(rt.platform)} · ` +
      (rt.ok ? `onnxruntime ${esc(pk.onnxruntime)}, onnx-asr ${esc(pk.onnx_asr)}` : 'ещё не установлены: numpy, onnxruntime, onnx-asr — это нормально до установки, способы ниже' +
        (Object.keys(pk).filter(k => pk[k]).length ? ' (уже есть: ' + esc(['numpy', 'onnxruntime', 'onnx_asr'].filter(k => pk[k]).join(', ')) + ')' : '')) + '</span></div>' +
    `<div class="vs ${m.ok ? 'ok' : 'bad'}"><b>${m.ok ? '✓' : '✗'} Модель</b> <span class="hint">${esc(m.title)} · ${m.ok ? esc(m.mb + ' МБ · ' + m.dir) : 'не установлена (' + esc(m.dir) + ')'}</span></div>` +
    `<div class="vs ${st.worker.running ? 'ok' : ''}"><b>${st.worker.running ? '● В памяти' : '○ Не загружена'}</b> <span class="hint">${st.worker.running ? 'выгрузится через ' + Math.ceil(st.worker.idle_left / 60) + ' мин простоя' : 'загрузится при первой фразе (несколько секунд)'}${st.worker.error ? ' · ' + esc(st.worker.error) : ''}</span></div>`;
  $('vc-found').textContent = st.archives.length ? 'Найден архив: ' + st.archives[0] : '';
  $('vc-model').innerHTML = Object.entries(st.models).map(([k, v]) => `<option value="${esc(k)}"${k === s.model ? ' selected' : ''}>${esc(v)}</option>`).join('');
  $('vc-thr').value = s.threads; $('vc-idle').value = s.idle_minutes; $('vc-hk').value = s.hotkey || '';
  $('vc-hf').value = s.hf_mirror || ''; $('vc-pypi').value = s.pypi_index || ''; $('vc-q8').checked = s.quantization === 'int8';
  $('vc-cmd').checked = s.commands !== false; $('vc-path').value = s.path || '';
  $('vc-rep').value = (s.replace || []).map(x => x[0] + ' → ' + x[1]).join('\n');
  msg('vc-msg', rt.ok && m.ok ? '' : 'Голосовой ввод ещё не установлен — выберите способ ниже.', rt.ok && m.ok ? '' : 'warn');
}
function vcBody() {
  return {model: $('vc-model').value, threads: $('vc-thr').value, idle_minutes: $('vc-idle').value, hotkey: $('vc-hk').value,
    hf_mirror: $('vc-hf').value.trim(), pypi_index: $('vc-pypi').value.trim(), quantization: $('vc-q8').checked ? 'int8' : 'none',
    commands: $('vc-cmd').checked, replace: $('vc-rep').value, path: $('vc-path').value.trim()};
}
async function vcSave() {
  try { await api('/api/voice/settings', vcBody()); $('vc-saved').textContent = '✓ Сохранено'; setTimeout(() => $('vc-saved').textContent = '', 2500); await vcLoad(); }
  catch (e) { $('vc-saved').textContent = '✗ ' + e.message; }
}
function vcTime(s) { s = Math.max(0, Math.round(s)); return s >= 60 ? `${Math.floor(s / 60)} мин ${String(s % 60).padStart(2, '0')} с` : `${s} с`; }
// полоса хода установки: шаг N из M, проценты шага и общие, скорость, сколько осталось, что сейчас делается
function vcMeter(j) {
  const box = $('vc-meter'), m = j.meter || {};
  box.classList.toggle('on', j.status === 'running' || !!m.step);
  if (!m.step) { $('vc-m-title').textContent = 'Готовлюсь…'; $('vc-m-bar').parentNode.classList.add('indet'); return; }
  const run = j.status === 'running';
  $('vc-m-title').textContent = `Шаг ${m.step}: ${m.title || ''}` + (m.pct != null ? ` — ${Math.round(m.pct)}%` : '');
  const eta = m.eta != null && run ? ` · осталось ~${vcTime(m.eta)}` : '';
  $('vc-m-eta').textContent = run ? `идёт ${vcTime(m.elapsed || 0)}${eta}` : (j.status === 'done' ? `готово за ${vcTime(j.seconds)}` : 'остановлено');
  // толстая полоса — текущий шаг (как в заголовке), тонкая — вся установка
  const ov = j.status === 'done' ? 100 : (m.overall || 0);
  const main = $('vc-m-bar').parentNode; main.classList.toggle('indet', run && m.pct == null);
  $('vc-m-bar').style.width = (j.status === 'done' ? 100 : m.pct != null ? m.pct : 0) + '%';
  $('vc-m-sub').parentNode.classList.remove('indet'); $('vc-m-sub').style.width = ov + '%';
  if (run) $('vc-m-eta').textContent += ` · всего ${Math.round(ov)}%`;
  $('vc-m-detail').textContent = [m.done && m.total ? `${m.done} из ${m.total}` : m.done, m.speed, m.detail].filter(Boolean).join(' · ');
  $('vc-m-stop').style.display = run ? '' : 'none';
}
async function vcCancel() { if (VC.job) try { await api('/api/jobs/' + VC.job + '/cancel', {}); } catch (e) {} }
async function vcJob(path, body, btn) {
  await api('/api/voice/settings', vcBody());
  if (btn) busy(btn, true, 'Идёт…');
  const box = $('vc-log'); box.textContent = ''; box.classList.add('on'); msg('vc-msg', '');
  try {
    const {job} = await api(path, body || {}); VC.job = job;
    let since = 0, j;
    while (true) {
      try { j = await api('/api/jobs/' + job + '?since=' + since); }
      catch (e) { if (/задача не найдена/.test(e.message)) break; throw e; }
      if (j.log.length) { box.textContent += j.log.join('\n') + '\n'; box.scrollTop = box.scrollHeight; }
      since = j.log_total; vcMeter(j);
      if (j.status !== 'running') break;
      await new Promise(r => setTimeout(r, 700));
    }
    if (j && j.status === 'error') msg('vc-msg', j.error, 'bad');
    await vcLoad(true);
  } catch (e) { msg('vc-msg', e.message, 'bad'); }
  VC.job = null;
  if (btn) busy(btn, false);
}
function vcDownload() { vcJob('/api/voice/download', {}, 'vc-dl'); }
function vcArchive(github) { vcJob('/api/voice/archive', {path: github ? '' : $('vc-zip').value.trim(), github: !!github}, 'vc-arc'); }
async function vcUsePath() {
  const p = $('vc-path').value.trim(); if (!p) return;
  await api('/api/voice/settings', vcBody());
  await vcJob('/api/voice/install', {path: p});
}
async function vcStop() { await api('/api/voice/stop', {}); await vcLoad(); }

async function vcBoot() {
  try { const st = await api('/api/voice/status'); VC.hotkey = st.settings.hotkey || ''; VC.st = st; } catch (e) {}
  vcAttachAll();
  new MutationObserver(() => document.querySelectorAll('[data-voice]').forEach(vcAttach)).observe(document.body, {childList: true, subtree: true});
}
document.addEventListener('DOMContentLoaded', () => setTimeout(vcBoot, 1500));
