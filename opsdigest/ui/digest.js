// OpsDigest — раздел «Сводка»: период до минут (1/2/3 ч, сутки, неделя, месяц, свой), выбор чатов, дополнительные
// указания модели, окно «Проверьте перед запуском» (период, чаты, плагины и их подключение), остановка сводки,
// шаблоны (сохранённые настройки) и сводки по расписанию.
// Пользуется помощниками окна: api, poll, $, esc, busy, msg, toast, ST, SRC_*, dgSources, dgSrcWarn, srcPills.
'use strict';
const DG = {job: null, chats: [], sel: new Set(), presets: [], cf: null, cfBody: null, preset: '', chT: null, shown: false};

function dgLS(k, v) { try { if (v === undefined) return localStorage.getItem(k); if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (e) { return null; } }

// ---------------------------------------------------------------- период
function dgPeriodSet(v, quiet) {
  PERIOD = v;
  document.querySelectorAll('#dg-period button').forEach(x => x.classList.toggle('on', x.dataset.v === v));
  $('dg-custom').style.display = v === 'custom' ? '' : 'none';
  dgPeriodText();
  if (!quiet) { dgLS('od_dg_period', v); dgChLoadSoon(); }
}
function dgPeriodBody() {
  if (PERIOD !== 'custom') return {period: PERIOD};
  const h = Number($('dg-h').value || 0), m = Number($('dg-m').value || 0);
  if (h || m) return {period: 'hours', hours: h, minutes: m};
  if ($('dg-from').value) return {period: 'custom', since: $('dg-from').value, until: $('dg-to').value || null};
  return null;
}
function dgFmt(d) { return d.toLocaleString('ru-RU', {day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit'}); }
function dgPeriodText() {
  const p = dgPeriodBody(), el = $('dg-period-text');
  if (!p) { el.textContent = 'Укажите «последние N ч M мин» или дату и время начала.'; return; }
  const now = new Date(); let a, b = now;
  if (p.period === 'last') { el.textContent = 'С конца прошлой сводки до сейчас — только новое (точное время — в окне проверки).'; return; }
  const mins = {h1: 60, h2: 120, h3: 180, day: 1440, week: 10080, month: 43200}[p.period];
  if (mins) a = new Date(now - mins * 60000);
  else if (p.period === 'hours') a = new Date(now - (p.hours * 60 + p.minutes) * 60000);
  else { a = new Date(p.since); if (p.until) b = new Date(p.until); }
  if (isNaN(a)) { el.textContent = ''; return; }
  el.textContent = `Сообщения с ${dgFmt(a)} по ` + (p.until ? dgFmt(b) : `сейчас (${dgFmt(b)})`) + '.';
}

// ---------------------------------------------------------------- выбор чатов
function dgChLoadSoon() { clearTimeout(DG.chT); DG.chT = setTimeout(dgChLoad, 450); }
async function dgChLoad() {
  const p = dgPeriodBody(); if (!p) return;
  try { const j = await api('/api/digest/chats', p); DG.chats = j.chats || []; }
  catch (e) { $('dg-ch-list').innerHTML = `<div class="hint">${esc(e.message)}</div>`; return; }
  dgChRender();
}
function dgChRender() {
  const q = $('dg-ch-q').value.trim().toLowerCase(), act = $('dg-ch-active').checked;
  const rows = DG.chats.filter(c => (!q || c.title.toLowerCase().includes(q)) && (!act || c.n > 0 || DG.sel.has(c.id)));
  rows.sort((x, y) => (DG.sel.has(y.id) - DG.sel.has(x.id)) || (y.n - x.n) || x.title.localeCompare(y.title, 'ru'));
  $('dg-ch-list').innerHTML = rows.slice(0, 800).map(c => {
    const on = DG.sel.has(c.id);
    return `<label class="${on ? 'on' : ''}${c.excluded ? ' ex' : ''}" title="${esc(c.title)}${c.excluded ? ' — исключён из сводок; если отметить, войдёт в эту сводку' : ''}">` +
      `<input type="checkbox" data-c="${c.id}"${on ? ' checked' : ''}><span class="t">${esc(c.title)}</span>` +
      (c.excluded ? '<small>исключён</small>' : '') + `<small>${c.n ? c.n : '—'}</small></label>`;
  }).join('') || `<div class="hint" style="padding:8px 2px">${DG.chats.length ? 'Ничего не нашлось' + (act ? ' среди чатов с сообщениями за период — снимите «с сообщениями».' : '.') : 'В базе пока нет чатов.'}</div>`;
  dgChSum();
}
function dgChSum() {
  const live = DG.chats.filter(c => c.n && !c.excluded);
  if (!DG.sel.size) { $('dg-ch-sum').textContent = `все чаты · с сообщениями за период: ${live.length}`; return; }
  const sel = DG.chats.filter(c => DG.sel.has(c.id));
  $('dg-ch-sum').textContent = `выбрано: ${DG.sel.size} · сообщений: ${sel.reduce((a, c) => a + (c.n || 0), 0)}`;
}
function dgChSave() { dgLS('od_dg_chats', JSON.stringify([...DG.sel])); }
function dgChAll(on) {
  if (!on) DG.sel.clear(); else document.querySelectorAll('#dg-ch-list [data-c]').forEach(x => DG.sel.add(Number(x.dataset.c)));
  dgChSave(); dgChRender();
}

// ---------------------------------------------------------------- готовность и запрос
function readiness() {
  const s = ST.steps || {}; const miss = []; const hasBase = !!(ST.base && ST.base.messages);
  if (!s.workspace) miss.push('папку'); if (!s.llm) miss.push('модель (кнопка «Сохранить и проверить»)');
  const srcMode = DG.mode === 'source';
  if (!srcMode && !s.login && !hasBase) miss.push('вход в Telegram или загрузку экспорта (шаг Telegram)');
  if (srcMode && typeof dgSmReady === 'function' && !dgSmReady()) miss.push('Maverick и плагин ' + (SM.start === 'itsm' ? 'ITSM' : 'трекера') + ' («Настройки → Источники»)');
  if (!s.login) { $('dg-sync').checked = false; $('dg-sync').disabled = true; $('dg-send').checked = false; $('dg-send').disabled = true; }
  else { $('dg-sync').disabled = false; $('dg-send').disabled = false; }
  $('dg-base').textContent = baseLine();
  $('dg-preempt').checked = dgLS('od_dg_preempt') !== '0';
  if (miss.length) msg('dg-notready', (srcMode ? 'Чтобы разобрать очередь, настройте: ' : 'Чтобы собрать сводку, настройте: ') + miss.join(', ') + '.' +
    (s.workspace && s.llm ? ' Модель уже можно проверить кнопкой «Пробная сводка на примере».' : ''), 'warn'); else msg('dg-notready', '');
  $('dg-btn').disabled = !!miss.length || !!DG.job; $('demo-btn').disabled = !(s.workspace && s.llm) || !!DG.job;
}
function dgBody() {
  if (DG.mode === 'source') return dgSmBody();
  const p = dgPeriodBody(); if (!p) return null;
  const titles = {}; DG.chats.forEach(c => { if (DG.sel.has(c.id)) titles[c.id] = c.title; });
  return Object.assign(p, {send: $('dg-send').checked, sync: $('dg-sync').checked, sources: dgSources(), history: $('dg-hist').checked,
    preempt: $('dg-preempt').checked, chat_ids: DG.sel.size ? [...DG.sel] : null, chat_titles: titles,
    extra_prompt: $('dg-extra').value.trim()});
}

// ---------------------------------------------------------------- «Проверьте перед запуском»
async function dgConfirm() {
  const body = dgBody();
  if (!body) return alert('Укажите период: «последние N ч M мин» или дату и время начала.');
  if (body.mode === 'source') dgSmSave();
  DG.cfBody = body;
  $('dg-cf').classList.add('on'); $('dg-cf-log').classList.remove('on'); $('dg-cf-log').textContent = '';
  await dgCfRender();
}
function dgCfClose() { $('dg-cf').classList.remove('on'); }
function dgAgo(iso) {
  if (!iso) return ''; const m = Math.round((Date.now() - new Date(iso)) / 60000);
  return m < 1 ? 'только что' : m < 60 ? `${m} мин назад` : m < 1440 ? `${Math.round(m / 60)} ч назад` : new Date(iso).toLocaleDateString('ru-RU');
}
async function dgCfRender() {
  $('dg-cf-body').innerHTML = '<div class="hint"><span class="spin"></span> Считаю сообщения и проверяю настройки…</div>';
  $('dg-cf-go').disabled = true;
  let j;
  try { j = await api('/api/digest/preview', DG.cfBody); }
  catch (e) { $('dg-cf-body').innerHTML = `<div class="msg bad" style="display:block">${esc(e.message)}</div>`; return; }
  DG.cf = j;
  if (j.mode === 'source') return dgCfRenderSource(j);
  const P = j.period, C = j.chats, b = DG.cfBody;
  const mins = P.minutes, dur = mins < 1440 ? (mins >= 60 ? Math.floor(mins / 60) + ' ч ' : '') + (mins % 60 ? mins % 60 + ' мин' : '') : Math.round(mins / 1440) + ' дн.';
  const srcRows = Object.entries(j.sources || {}).filter(([k, v]) => v.enabled).map(([k, v]) => {
    const st = v.state || 'unknown';
    return `<label><input type="checkbox" data-cfs="${k}"${v.selected ? ' checked' : ''}> ${SRC_IC[k] || ''} ${esc(v.short)}` +
      ` <span class="pst-i ${esc(st)}" title="${esc(v.detail || '')}">${esc(PST_RU[st] || st)}</span>` +
      (v.at ? ` <small class="hint" style="margin:0">проверено ${esc(dgAgo(v.at))}</small>` : '') + '</label>';
  }).join('');
  const stops = (j.will_stop || []);
  $('dg-cf-body').innerHTML = `<div class="dg-cf">` +
    `<div>Период</div><div><b>${esc(P.text)}</b> <span class="hint" style="margin:0">(${esc(dur.trim())})</span></div>` +
    `<div>Чаты</div><div>` + (C.all ? `<b>все</b>, кроме исключённых · с сообщениями за период: <b>${C.with_msgs}</b>`
      : `<b>выбрано ${C.selected}</b> · с сообщениями за период: <b>${C.with_msgs}</b><div class="names">${C.names.map(esc).join(', ')}${C.selected > C.names.length ? '…' : ''}</div>`) + '</div>' +
    `<div>Сообщений</div><div><b>${C.messages}</b> в базе` + (j.sync ? ' · перед сводкой заберу новые из Telegram' : ' · без синхронизации с Telegram') + '</div>' +
    `<div>Прошлые сводки</div><div>` + (!(j.history || {}).on ? 'не учитываю — модель прочтёт всё заново' :
      (j.history.prev ? `сравню с прошлой (${esc(j.history.prev)})` : 'прошлой сводки за это время нет') +
      (j.history.covered ? ` · уже разобрано <b>${j.history.covered}</b> из ${C.messages} сообщений (${j.history.chats} чат.) — заново не читаю` : '')) + '</div>' +
    `<div>Модель</div><div>${j.model ? esc(j.model.name) + (j.model.ok ? ' ✓' : ' <span style="color:var(--warn)">— не проверена</span>') : '—'}</div>` +
    `<div>Плагины</div><div class="dg-cf-src">` + (j.maverick ? (srcRows || '<span class="hint" style="margin:0">все источники выключены в «Настройки → Источники»</span>') +
      (j.maverick_web ? '' : '') : '<span class="hint" style="margin:0">Maverick не настроен — сводка только по Telegram</span>') + '</div>' +
    `<div>Указания</div><div>${j.extra_prompt ? `<div class="extra">${esc(j.extra_prompt)}</div>` : '<span class="hint" style="margin:0">нет — обычная сводка</span>'}</div>` +
    `<div>Перед запуском</div><div>` + (j.preempt ? (stops.length ? 'остановлю: ' + stops.map(x => esc(x.title) + (x.long ? ' (пауза до конца сводки)' : '')).join(', ')
      : 'других задач нет') : 'другие задачи не трогаю') + (j.send ? ' · пришлю в «Избранное» Telegram' : '') + '</div>' +
    `</div><div class="dg-cf-w">` + (j.warnings || []).map(w => `<div class="msg ${esc(w.level)}">${esc(w.text)}</div>`).join('') + '</div>';
  document.querySelectorAll('#dg-cf-body [data-cfs]').forEach(x => x.addEventListener('change', () => {
    DG.cfBody.sources = Object.assign({}, DG.cfBody.sources, {[x.dataset.cfs]: x.checked});
    const el = $('dg-s-' + x.dataset.cfs); if (el && !el.disabled) { el.checked = x.checked; dgSrcWarn(); }
    clearTimeout(DG.cfT); DG.cfT = setTimeout(dgCfRender, 300);
  }));
  const blocking = !!(b.chat_ids !== null && b.chat_ids !== undefined && !b.chat_ids.length);
  const bad = (j.warnings || []).some(w => w.level === 'bad');
  $('dg-cf-go').disabled = blocking;
  $('dg-cf-go').textContent = bad ? 'Запустить всё равно' : 'Всё верно — запустить';
  $('dg-cf-check').style.display = j.maverick ? '' : 'none';
}
async function dgCfCheck() {
  const kinds = DG.cfBody.mode === 'source' ? (DG.cfKinds || []) : Object.entries(DG.cfBody.sources || {}).filter(([k, v]) => v).map(([k]) => k);
  if (!kinds.length) return alert('Плагины не выбраны — отметьте нужные в списке выше.');
  busy('dg-cf-check', true, 'Спрашиваю Maverick…');
  try { const {job} = await api('/api/plugins/check', {kinds}); await poll(job, 'dg-cf-log', () => {}); }
  catch (e) { alert(e.message); }
  busy('dg-cf-check', false);
  try { await srcPills(); dgSrcFill(); } catch (e) {}
  await dgCfRender();
}
async function dgCfGo() {
  const body = DG.cfBody; if (!body) return;
  dgCfClose();
  if ($('dg-extra-keep').checked) dgLS('od_dg_extra', $('dg-extra').value); else dgLS('od_dg_extra', null);
  await runDigest(false, body);
}

// ---------------------------------------------------------------- запуск, ход, остановка
async function runDigest(demo, body) {
  if (!demo && !body) return dgConfirm();
  const btn = demo ? 'demo-btn' : 'dg-btn';
  busy(btn, true, 'Собираю…');
  let job;
  try { job = (await api(demo ? '/api/demo' : '/api/digest', demo ? {} : body)).job; }
  catch (e) { busy(btn, false); alert(e.message); return; }
  busy(btn, false);
  await dgAttach(job, demo);
}
async function dgAttach(job, demo) {
  if (!job || DG.job === job) return;
  DG.job = job;
  const btn = demo ? 'demo-btn' : 'dg-btn';
  busy(btn, true, 'Собираю…'); $('dg-stop').style.display = ''; $('dg-stop').disabled = false;
  $('dg-result').style.display = 'none';
  $('dg-status').textContent = demo ? 'Сводка по выдуманной переписке — проверка модели и вида отчёта.'
    : (DG.mode === 'source' ? 'Идёт разбор очереди.' : 'Идёт сводка.') + ' Можно переходить в другие разделы — ход останется здесь и в «⏳ Задачи».';
  try { await poll(job, 'dg-log', dgDone); }
  catch (e) { $('dg-status').textContent = ''; msg('dg-notready', 'Ход сводки не виден: ' + e.message + ' — она продолжается, см. «⏳ Задачи».', 'warn'); }
  if (DG.job === job) DG.job = null;
  $('dg-stop').style.display = 'none';
  busy(btn, false); if (!demo) $('dg-btn').textContent = DG.mode === 'source' ? 'Разобрать очередь' : 'Собрать сводку'; readiness();
}
function dgDone(j) {
  $('dg-status').textContent = '';
  const r = $('dg-result'); r.style.display = '';
  if (j.status === 'lost') { r.innerHTML = '<b>Программа перезапустилась во время сводки.</b> <div class="hint">Соберите сводку заново — сделанное до перезапуска не сохранилось.</div>'; return; }
  if (j.status === 'error') {
    r.innerHTML = j.stopped || /^Остановлено/.test(j.error || '') ? `<b>⏹ Сводка остановлена.</b> <span class="hint">${esc(j.error || '')}</span>`
      : `<b style="color:var(--bad)">Не получилось:</b> ${esc(j.error)}<div class="hint">Непонятно, в чём дело? Раздел «Диагностика» → «Создать отчёт об ошибке».</div>`;
    return;
  }
  const x = j.result;
  if (x.mode === 'source') {
    r.innerHTML = dgDoneSource(Object.assign({seconds: j.seconds}, x)) +
      `<div class="actions"><button class="b p" onclick="openReport('${esc(x.report)}')">Открыть отчёт</button>` +
      `<span class="hint">Что в отчёте хорошо или плохо?</span>${fbButtons('report', x.headline || '', {report: x.report, title: x.headline ? x.headline.slice(0, 80) : x.report})}</div>`;
    if (MX.mode === 'digest') openReport(x.report);
    loadReports(); srcPills(); glBadgeRefresh();
    return;
  }
  r.innerHTML = `<h3>Готово за ${Math.round(j.seconds)} с: чатов ${x.chats}, сообщений ${x.messages}, нужна реакция — ${x.top}</h3>` +
    `<div>${esc(x.headline)}</div>` + (x.cross ? `<div class="dg-cross"><b>Общая картина</b> (${x.cross.sources.map(k => SRC_RU[k] || k).join(', ')}): ` +
      (x.cross.threads ? `нитей — ${x.cross.threads}. ${esc(x.cross.headline || '')}` : 'совпадений не найдено.') +
      (x.cross.offline.length ? `<div class="msg warn" style="display:block">Не подключены в Maverick: ${x.cross.offline.map(k => SRC_RU[k] || k).join(', ')} — переподключите плагин в чате Maverick и соберите сводку снова.</div>` : '') + '</div>' : '') +
    (x.sync_error ? `<div class="msg warn" style="display:block">Telegram не ответил — сводка по тому, что уже было в базе.</div>` : '') +
    (x.send_error ? `<div class="msg warn" style="display:block">В «Избранное» не отправлено: ${esc(x.send_error)}</div>` : '') +
    (x.errors.length ? `<div class="hint">Ошибок при обработке: ${x.errors.length} (см. отчёт)</div>` : '') +
    (x.glossary_pending ? `<div class="hint">Глоссарий: терминов в очереди — ${x.glossary_pending}${x.glossary_processing ? ' (обрабатываются фоном)' : ''}.</div>` : '') +
    `<div class="actions"><button class="b p" onclick="openReport('${esc(x.report)}')">Открыть отчёт</button>` +
    `<span class="hint">Что в отчёте хорошо или плохо?</span>${fbButtons('report', x.headline || '', {report: x.report, title: x.headline ? x.headline.slice(0, 80) : x.report})}</div>`;
  if (MX.mode === 'digest') openReport(x.report);
  loadReports(); srcPills(); glBadgeRefresh();
}
async function dgStop() {
  if (!DG.job) return;
  if (!confirm('Остановить сводку? Запросы к модели оборвутся, открытые ею сессии Maverick закроются, отчёт не будет записан.')) return;
  $('dg-stop').disabled = true; $('dg-stop').innerHTML = '<span class="spin"></span>Останавливаю…';
  try { await api(`/api/jobs/${DG.job}/stop`, {}); }
  catch (e) { alert(e.message); }
  $('dg-stop').innerHTML = '⏹ Остановить сводку';
}
// сводка шла, пока окно было в другом разделе или его перезагрузили, — показать её ход снова
async function dgReattach() {
  if (DG.job) return;
  let run = (ST.running || []);
  try { run = (await api('/api/tasks')).running || run; } catch (e) {}
  const r = run.find(x => x.kind === 'digest' || x.kind === 'demo');
  if (r && r.info && r.info.mode === 'source' && DG.mode !== 'source') dgModeSet('source', true);
  if (r) dgAttach(r.id, r.kind === 'demo');
}

// ---------------------------------------------------------------- шаблоны
async function dgPresetsLoad() {
  try { DG.presets = (await api('/api/digest/presets')).presets || []; } catch (e) { DG.presets = []; }
  dgPresetsRender();
}
function dgPresetsRender() {
  const el = $('dg-presets');
  el.innerHTML = DG.presets.length ? '<span class="dg-pre-l">Шаблоны:</span>' + DG.presets.map(p =>
    `<span class="dg-pre${DG.preset === p.id ? ' on' : ''}"><button class="dg-pre-b" data-pre="${esc(p.id)}" title="${esc(p.text || '')}">${esc(p.name)}</button>` +
    `<button class="dg-pre-x" data-prex="${esc(p.id)}" title="Удалить шаблон">×</button></span>`).join('')
    : '<span class="hint" style="margin:0">Шаблонов пока нет: настройте сводку, нажмите «Собрать сводку» и в окне проверки — «💾 Сохранить как шаблон».</span>';
}
function dgPresetApply(id) {
  const p = DG.presets.find(x => x.id === id); if (!p) return;
  const s = p.settings || {};
  if (s.mode === 'source') {
    dgModeSet('source'); dgSmFill(s); dgSmSave();
    $('dg-extra').value = s.extra_prompt || '';
    if (s.sync !== undefined && !$('dg-sync').disabled) $('dg-sync').checked = !!s.sync;
    if (s.send !== undefined && !$('dg-send').disabled) $('dg-send').checked = !!s.send;
    if (s.preempt !== undefined) $('dg-preempt').checked = !!s.preempt;
    DG.preset = id; dgLS('od_dg_preset', id); dgPresetsRender();
    return toast(`Шаблон «${p.name}» применён — проверьте и нажмите «Разобрать очередь»`);
  }
  if (DG.mode === 'source') dgModeSet('tg');
  if (['h1', 'h2', 'h3', 'day', 'week', 'month', 'last'].includes(s.period)) dgPeriodSet(s.period, true);
  else {
    dgPeriodSet('custom', true);
    if (s.period === 'hours') { $('dg-h').value = s.hours || ''; $('dg-m').value = s.minutes || ''; $('dg-from').value = $('dg-to').value = ''; }
    else { $('dg-h').value = $('dg-m').value = ''; $('dg-from').value = (s.since || '').slice(0, 16); $('dg-to').value = (s.until || '').slice(0, 16); }
    dgPeriodText();
  }
  $('dg-extra').value = s.extra_prompt || '';
  if (s.sync !== undefined && !$('dg-sync').disabled) $('dg-sync').checked = !!s.sync;
  if (s.send !== undefined && !$('dg-send').disabled) $('dg-send').checked = !!s.send;
  if (s.preempt !== undefined) $('dg-preempt').checked = !!s.preempt;
  if (s.history !== undefined) $('dg-hist').checked = !!s.history;
  SRC_KEYS.forEach(k => { const el = $('dg-s-' + k); if (el && !el.disabled) el.checked = !!(s.sources || {})[k]; });
  try { dgSrcWarn(); } catch (e) {}
  DG.sel = new Set((s.chat_ids || []).map(Number)); dgChSave();
  DG.preset = id; dgLS('od_dg_preset', id); dgPresetsRender(); dgChLoadSoon();
  toast(`Шаблон «${p.name}» применён — проверьте и нажмите «Собрать сводку»`);
}
async function dgPresetSave() {
  const b = DG.cfBody || dgBody(); if (!b) return alert('Сначала укажите период.');
  const cur = DG.presets.find(x => x.id === DG.preset);
  if (b.mode === 'source') {
    const nm = prompt('Название шаблона (например, «Очередь L2-Биллинг: что ждёт релиза»).\nТо же название — заменит прежний шаблон.', cur ? cur.name : '');
    if (!nm || !nm.trim()) return;
    const st = {mode: 'source', start: b.start, query: b.query, look: b.look, extra_prompt: b.extra_prompt, sync: b.sync, send: b.send, preempt: b.preempt};
    try { const j = await api('/api/digest/presets/save', {name: nm.trim(), settings: st}); DG.presets = j.presets; DG.preset = j.id; dgLS('od_dg_preset', j.id);
      dgPresetsRender(); toast('Шаблон «' + nm.trim() + '» сохранён'); if ($('sc-card')) scLoad(); }
    catch (e) { alert(e.message); }
    return;
  }
  const name = prompt('Название шаблона (например, «Утро: аварии за 3 часа»).\nТо же название — заменит прежний шаблон.', cur ? cur.name : '');
  if (!name || !name.trim()) return;
  const settings = {period: b.period, hours: b.hours, minutes: b.minutes, since: b.since, until: b.until, chat_ids: b.chat_ids,
    chat_titles: b.chat_titles, extra_prompt: b.extra_prompt, sources: b.sources, sync: b.sync, send: b.send, preempt: b.preempt,
    history: b.history};
  if (b.period === 'custom' && !confirm('В шаблоне будут точные даты (' + (b.since || '').replace('T', ' ') + (b.until ? ' — ' + b.until.replace('T', ' ') : '') +
    ').\nДля повторяющихся сводок удобнее «последние N ч» или «сутки». Сохранить с датами?')) return;
  try { const j = await api('/api/digest/presets/save', {name: name.trim(), settings}); DG.presets = j.presets; DG.preset = j.id; dgLS('od_dg_preset', j.id);
    dgPresetsRender(); toast('Шаблон «' + name.trim() + '» сохранён'); if ($('sc-card')) scLoad(); }
  catch (e) { alert(e.message); }
}
async function dgPresetDel(id) {
  const p = DG.presets.find(x => x.id === id); if (!p || !confirm(`Удалить шаблон «${p.name}»?`)) return;
  try { DG.presets = (await api('/api/digest/presets/delete', {id})).presets; if (DG.preset === id) DG.preset = ''; dgPresetsRender(); scLoad(); }
  catch (e) { alert(e.message); }
}

// ---------------------------------------------------------------- расписание
const SC = {list: [], presets: [], edit: null};
const SC_DAYS = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
async function scLoad() {
  let j; try { j = await api('/api/schedules'); } catch (e) { $('sc-list').innerHTML = `<div class="hint">${esc(e.message)}</div>`; return; }
  SC.list = j.schedules; SC.presets = j.presets;
  msg('sc-legacy', j.legacy ? `Включено старое расписание через Планировщик Windows (в ${j.legacy_at || '08:40'}). В защищённой папке оно не срабатывает — нужен пароль.` : '', 'warn');
  if (j.legacy) $('sc-legacy').innerHTML += ' <button class="lnk" onclick="scMigrate()">Перенести в новое расписание</button>';
  const a = j.autostart || {};
  $('sc-auto').innerHTML = a.supported ? (a.enabled ? 'Программа запускается сама при входе в Windows.' :
    'Чтобы расписание работало и после перезагрузки, включите <button class="lnk" onclick="tasksOpen()">«⏳ Задачи» → «Запускать OpsDigest при входе в Windows»</button>.') : '';
  $('sc-list').innerHTML = SC.list.length ? SC.list.map(x => {
    const st = x.state || {};
    const last = st.status === 'running' ? '<span class="pill tk-info">идёт</span>' :
      st.status === 'done' ? `✓ ${esc(scWhen(st.finished))}` + (st.report ? ` · <button class="lnk" onclick="openReport('${esc(st.report)}')">отчёт</button>` : '') +
        (st.send_error ? ` · <span style="color:var(--warn)">в «Избранное» не ушло</span>` : '') :
      st.status === 'error' ? `<span style="color:var(--bad)">✗ ${esc(scWhen(st.finished))}: ${esc((st.error || '').slice(0, 120))}</span>` :
      st.status === 'stopped' ? `⏹ остановлена ${esc(scWhen(st.finished))}` : 'ещё не запускалась';
    return `<div class="sc-row${x.enabled ? '' : ' off'}"><label class="sc-on" title="Включено"><input type="checkbox" ${x.enabled ? 'checked' : ''} onchange="scToggle('${esc(x.id)}', this.checked)"></label>` +
      `<div class="sc-b"><b>${esc(x.name)}</b><small>${esc(x.text)} · шаблон: ${esc(x.preset_name || 'сутки, все чаты')}${x.preset_name ? ' (' + esc(x.preset_text) + ')' : ''}${x.send ? ' · в «Избранное»' : ''}</small>` +
      `<small>${x.enabled && x.next ? 'следующая: ' + esc(scWhen(x.next)) : x.enabled ? '' : 'выключено'}${x.pending ? ' · <b>ждёт конца другой сводки</b>' : ''} · последняя: ${last}</small></div>` +
      `<div class="sc-a"><button class="b" onclick="scRun('${esc(x.id)}')" title="Собрать по этому расписанию сейчас">▶</button>` +
      `<button class="b" onclick="scEdit('${esc(x.id)}')" title="Изменить">✎</button><button class="b" onclick="scDel('${esc(x.id)}')" title="Удалить">✕</button></div></div>`;
  }).join('') : '<div class="hint">Расписаний пока нет.</div>';
  if (SC.edit !== null) scEditRender();
}
function scWhen(iso) {
  if (!iso) return ''; const d = new Date(iso), now = new Date(), t = d.toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit'});
  if (d.toDateString() === now.toDateString()) return 'сегодня ' + t;
  const tm = new Date(now); tm.setDate(now.getDate() + 1); if (d.toDateString() === tm.toDateString()) return 'завтра ' + t;
  const y = new Date(now); y.setDate(now.getDate() - 1); if (d.toDateString() === y.toDateString()) return 'вчера ' + t;
  return d.toLocaleDateString('ru-RU', {day: '2-digit', month: '2-digit', weekday: 'short'}) + ' ' + t;
}
function scEdit(id) {
  const x = id ? SC.list.find(s => s.id === id) : null;
  SC.edit = x ? JSON.parse(JSON.stringify(x)) : {id: '', name: '', enabled: true, preset: DG.preset || '', mode: 'days', days: [1, 2, 3, 4, 5], times: ['08:40'], every_min: 180, from: '09:00', to: '21:00', send: true};
  scEditRender(); $('sc-edit').scrollIntoView({block: 'nearest'});
}
function scEditRender() {
  const e = SC.edit, el = $('sc-edit'); if (!e) { el.style.display = 'none'; return; }
  el.style.display = '';
  el.innerHTML = `<div class="row"><div><label>Название</label><input id="se-name" value="${esc(e.name || '')}" placeholder="Например: Утро — что за ночь"></div>` +
    `<div><label>Шаблон сводки</label><select id="se-preset"><option value="">Сутки · все чаты · источники из настроек</option>` +
    SC.presets.map(p => `<option value="${esc(p.id)}"${p.id === e.preset ? ' selected' : ''}>${esc(p.name)} — ${esc(p.text)}</option>`).join('') + '</select></div></div>' +
    `<label>Как часто</label><div class="seg" id="se-mode"><button data-v="days" class="${e.mode !== 'every' ? 'on' : ''}">В определённое время</button><button data-v="every" class="${e.mode === 'every' ? 'on' : ''}">Каждые N часов</button></div>` +
    `<div class="se-days">${SC_DAYS.map((d, i) => `<label class="check fit"><input type="checkbox" data-sd="${i + 1}"${(e.days || []).includes(i + 1) ? ' checked' : ''}> ${d}</label>`).join('')}` +
    `<button class="lnk" data-sdp="12345">будни</button><button class="lnk" data-sdp="1234567">все дни</button></div>` +
    (e.mode === 'every' ? `<div class="dg-last">Каждые <input type="number" id="se-every-h" min="0" max="24" value="${Math.floor((e.every_min || 180) / 60)}"> ч <input type="number" id="se-every-m" min="0" max="55" step="5" value="${(e.every_min || 180) % 60}"> мин,
      с <input type="time" id="se-from" value="${esc(e.from || '09:00')}" style="width:auto"> до <input type="time" id="se-to" value="${esc(e.to || '21:00')}" style="width:auto"></div>
      <div class="hint">Подойдёт шаблон с периодом «С прошлой сводки» — каждая сводка покажет только новое и что изменилось.</div>`
      : `<div class="dg-last">Во сколько: <input id="se-times" value="${esc((e.times || []).join(', '))}" style="width:220px" placeholder="08:40, 13:00"> <span class="hint">— можно несколько через запятую</span></div>`) +
    `<label class="check"><input type="checkbox" id="se-send"${e.send ? ' checked' : ''}> Прислать короткую сводку и отчёт в «Избранное» Telegram</label>` +
    `<div class="actions"><button class="b p" onclick="scSave()">Сохранить расписание</button><button class="b" onclick="SC.edit=null;scEditRender()">Отмена</button></div>`;
  el.querySelectorAll('#se-mode button').forEach(b => b.onclick = () => { scGrab(); SC.edit.mode = b.dataset.v; scEditRender(); });
  el.querySelectorAll('[data-sdp]').forEach(b => b.onclick = () => { scGrab(); SC.edit.days = b.dataset.sdp.split('').map(Number); scEditRender(); });
}
function scGrab() {
  const e = SC.edit; if (!e) return e;
  e.name = $('se-name').value.trim(); e.preset = $('se-preset').value; e.send = $('se-send').checked;
  e.days = [...document.querySelectorAll('#sc-edit [data-sd]')].filter(x => x.checked).map(x => Number(x.dataset.sd));
  if (e.mode === 'every') { e.every_min = Number($('se-every-h').value || 0) * 60 + Number($('se-every-m').value || 0); e.from = $('se-from').value; e.to = $('se-to').value; }
  else e.times = $('se-times').value;
  return e;
}
async function scSave() {
  const e = scGrab();
  try { await api('/api/schedules/save', {schedule: e}); SC.edit = null; scEditRender(); msg('sc-msg', '✓ Расписание сохранено.', 'ok'); await scLoad(); }
  catch (err) { msg('sc-msg', err.message, 'bad'); }
}
async function scToggle(id, on) { try { await api('/api/schedules/toggle', {id, enabled: on}); } catch (e) { alert(e.message); } scLoad(); }
async function scDel(id) { const x = SC.list.find(s => s.id === id); if (!x || !confirm(`Удалить расписание «${x.name}»?`)) return;
  try { await api('/api/schedules/delete', {id}); } catch (e) { alert(e.message); } scLoad(); }
async function scRun(id) {
  const x = SC.list.find(s => s.id === id); if (!x || !confirm(`Собрать сводку «${x.name}» сейчас?` + (x.send ? ' Она придёт и в «Избранное».' : ''))) return;
  try { const {job} = await api('/api/schedules/run', {id}); setTimeout(scLoad, 1500); dgAttach(job, false); window.scrollTo({top: 0, behavior: 'smooth'}); }
  catch (e) { alert(e.message); }
}
async function scMigrate() { try { const j = await api('/api/schedules/migrate', {}); msg('sc-msg', j.message, 'ok'); scLoad(); } catch (e) { msg('sc-msg', e.message, 'bad'); } }

// ---------------------------------------------------------------- показ раздела
function dgShow() {
  if (!DG.shown) {
    DG.shown = true;
    document.querySelectorAll('#dg-period button').forEach(b => b.onclick = () => { DG.preset = ''; dgPresetsRender(); dgPeriodSet(b.dataset.v); });
    ['dg-h', 'dg-m'].forEach(id => $(id).addEventListener('input', () => { if ($('dg-h').value || $('dg-m').value) { $('dg-from').value = ''; $('dg-to').value = ''; } dgPeriodText(); dgChLoadSoon(); }));
    ['dg-from', 'dg-to'].forEach(id => $(id).addEventListener('change', () => { if ($('dg-from').value) { $('dg-h').value = ''; $('dg-m').value = ''; } dgPeriodText(); dgChLoadSoon(); }));
    $('dg-ch-q').addEventListener('input', dgChRender); $('dg-ch-active').addEventListener('change', dgChRender);
    $('dg-ch-list').addEventListener('change', e => {
      const x = e.target.closest('[data-c]'); if (!x) return; const id = Number(x.dataset.c);
      if (x.checked) DG.sel.add(id); else DG.sel.delete(id);
      x.closest('label').classList.toggle('on', x.checked); dgChSave(); dgChSum();
    });
    $('dg-presets').addEventListener('click', e => {
      const b = e.target.closest('[data-pre]'); if (b) return dgPresetApply(b.dataset.pre);
      const x = e.target.closest('[data-prex]'); if (x) dgPresetDel(x.dataset.prex);
    });
    try { DG.sel = new Set(JSON.parse(dgLS('od_dg_chats') || '[]').map(Number)); } catch (e) { DG.sel = new Set(); }
    const ex = dgLS('od_dg_extra'); if (ex) { $('dg-extra').value = ex; $('dg-extra-keep').checked = true; }
    DG.preset = dgLS('od_dg_preset') || '';
    const per = dgLS('od_dg_period'); dgPeriodSet(per && per !== 'custom' && per !== 'since' ? per : 'day', true);
    $('dg-hist').checked = dgLS('od_dg_hist') !== '0';
    dgSmInit();
  }
  dgPeriodText(); dgChLoad(); dgPresetsLoad(); scLoad(); dgReattach();
}
setInterval(() => { if (typeof MX !== 'undefined' && MX.mode === 'digest' && !document.hidden) { dgPeriodText(); if ($('sc-card')) scLoad(); } }, 60000);
