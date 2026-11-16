// Разделы окна (вкладки вверху) с рубильниками в настройках, тема, источники Maverick, очередь глоссария.
// Пользуется помощниками окна (index.html): api, $, esc, msg, busy, poll, ST, CFG, go, showPage, firstStep, LOCKED.
'use strict';
const MODS = [
  {k: 'mx', t: '💬 Мессенджер', d: 'Чаты Telegram прямо в программе: читать, отвечать, ИИ в контексте чата (сводка чата, вопрос по выделенному, черновик).'},
  {k: 'digest', t: '★ Сводка', d: 'Сводки за сутки, неделю, месяц: что требует вашей реакции, аварии, номера; сверка с почтой, ITSM и вики.'},
  {k: 'rpa', t: '⇩ RPA', d: 'Очереди выгрузок инцидентов и проблем из корпоративного портала.'},
  {k: 'asst', t: '✦ Ассистент', d: 'Отдельный полноэкранный чат с ИИ по всем источникам — без привязки к одному чату. История: беседы, вопросы по выделенному, сводки.'},
  {k: 'gl', t: '📖 Глоссарий', d: 'Термины и их значения, очередь терминов, которые программа собирает сама, вопросы к вам.'},
];
const SRC_RU = {telegram: 'Telegram', mail: 'Почта', itsm: 'ITSM', wiki: 'Вики', rooms: 'Rooms', tracker: 'Трекер', meet: 'Встречи'};
const SRC_IC = {mail: '✉', itsm: '🎫', wiki: '📚', rooms: '💭', tracker: '🗂', meet: '🎙'};
const SRC_KEYS = ['mail', 'wiki', 'rooms', 'tracker', 'meet'];
const SRC_TITLE = {mail: 'Почта', itsm: 'ITSM: инциденты и проблемы', wiki: 'Вики (Confluence)', rooms: 'X5 Rooms (мессенджер)',
  tracker: 'xTracker (задачи и разработка)', meet: 'Scriber AI (расшифровки встреч)'};
const SRC_SHORT = {mail: 'Почта', itsm: 'ITSM', wiki: 'Вики', rooms: 'X5 Rooms', tracker: 'xTracker', meet: 'Scriber AI'};
// галочки источников: где — контейнер, pre — префикс id, extra — подпись справа
function srcChecks(where, pre, attr) {
  const el = $(where); if (!el) return;
  el.innerHTML = SRC_KEYS.map(k => `<label class="check fit" style="margin:2px 16px 2px 0;display:inline-flex;gap:4px"><input type="checkbox" id="${pre}${k}" ${attr ? attr + '="' + k + '"' : ''}> ${SRC_IC[k]} ${esc(SRC_TITLE[k])} <span class="pst" data-pst="${k}"></span></label>`).join('');
}
const PST_RU = {ok: 'подключён', offline: 'не подключён', error: 'не отвечает', unknown: 'не проверялся', disabled: 'выключен'};

function uiMods() { return Object.assign({mx: false, digest: true, rpa: true, asst: true, gl: true}, ((ST || {}).ui || {}).modules || {}); }

// ---------------------------------------------------------------- разделы
function setMode(m, page) {
  const mods = uiMods();
  if (m !== 'set' && !mods[m]) m = 'set';
  if (LOCKED === 'telegram' && m !== 'set') m = 'set';
  MX.mode = m;
  window.chrome?.webview?.postMessage({kind:'rpa-mode', active:m === 'rpa'});
  const main = document.querySelector('.main');
  ['mx', 'digest', 'rpa', 'asst', 'gl', 'set'].forEach(x => main.classList.toggle('mode-' + x, x === m));
  main.classList.toggle('mx-on', m === 'mx');
  document.querySelectorAll('#modes button').forEach(b => b.classList.toggle('on', b.dataset.m === m));
  if (m === 'mx') mxStart();
  else if (m === 'digest' || m === 'rpa' || m === 'gl') showPage(m);
  else if (m === 'asst') asStart();
  else {
    const cur = document.querySelector('.page.on');
    const want = page || (cur && !['p-digest', 'p-rpa', 'p-gl'].includes(cur.id) ? cur.id.slice(2) : '') || LAST_SET || setupStep();
    showPage(want);
  }
  if (ST && ST.workspace && !LOCKED && (ST.ui || {}).tab !== m) { (ST.ui = ST.ui || {}).tab = m; api('/api/ui', {tab: m}).catch(() => {}); }
}
function setupStep() {
  const s = ST.steps || {};
  if (!s.workspace) return 'ws'; if (!s.llm) return 'llm'; if (!s.telegram) return 'tg';
  if (!s.login && !(ST.base && ST.base.messages)) return 'login';
  return 'ws';
}
function modsTabs() {
  const mods = uiMods();
  document.querySelectorAll('#modes button').forEach(b => { if (b.dataset.m !== 'set') b.style.display = mods[b.dataset.m] ? '' : 'none'; });
}
function mxAuto() {
  modsTabs();
  const mods = uiMods(), s = ST.steps || {};
  const ready = ST.workspace && s.llm && (s.login || (ST.base && ST.base.messages)) && !LOCKED;
  const saved = (ST.ui || {}).tab;
  let m = 'set';
  if (ready) {
    if (saved && (saved === 'set' || mods[saved])) m = saved;
    else m = ['mx', 'digest', 'rpa', 'asst', 'gl'].find(k => mods[k] && (k !== 'mx' || s.login)) || 'set';
  }
  setMode(m);
  if (ST.workspace && !LOCKED) srcPills();
  updCheck(true);
  if (!_updT) _updT = setInterval(() => updCheck(true), 4 * 3600e3);  // программа бывает открыта сутками
}
function modsRender() {
  const mods = uiMods();
  $('mods-list').innerHTML = MODS.map(x => `<label class="mod"><span class="sw"><input type="checkbox" data-mod="${x.k}"${mods[x.k] ? ' checked' : ''}><i></i></span>` +
    `<span><b>${esc(x.t)}</b><small>${esc(x.d)}</small></span></label>`).join('') +
    '<label class="mod off"><span class="sw"><input type="checkbox" checked disabled><i></i></span><span><b>⚙ Настройки</b><small>Подключение, источники, разделы, защита, диагностика — всегда.</small></span></label>';
  document.querySelectorAll('#theme-seg button').forEach(b => b.classList.toggle('on', b.dataset.t === themeCur()));
}
async function modSet(k, on) {
  const mods = uiMods(); mods[k] = on;
  try { const u = await api('/api/ui', {modules: mods}); ST.ui = Object.assign(ST.ui || {}, u); modsTabs(); modsRender(); }
  catch (e) { alert(e.message); }
}

// ---------------------------------------------------------------- тема
function themeCur() { try { return localStorage.getItem('od_theme') || (ST.ui || {}).theme || 'auto'; } catch (e) { return (ST.ui || {}).theme || 'auto'; } }
function themeApply(t) {
  t = t || themeCur();
  const dark = t === 'dark' || (t === 'auto' && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  const b = $('theme-btn'); if (b) { b.textContent = t === 'auto' ? '🌓' : dark ? '🌙' : '☀️'; b.title = 'Тема: ' + ({auto: 'как в системе', light: 'светлая', dark: 'тёмная'}[t]) + ' — нажмите, чтобы сменить'; }
}
async function themeSet(t) {
  try { localStorage.setItem('od_theme', t); } catch (e) {}
  themeApply(t);
  if (ST) (ST.ui = ST.ui || {}).theme = t;
  try { await api('/api/ui', {theme: t}); } catch (e) {}
  if (document.querySelector('#p-mods.on')) modsRender();
}
function themeCycle() { const order = ['auto', 'light', 'dark']; themeSet(order[(order.indexOf(themeCur()) + 1) % 3]); }
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => themeApply());

// ---------------------------------------------------------------- источники Maverick
const SRC = {data: null, open: new Set(), dirty: new Set(), timers: {}};
function srcCardValues(c) {
  const o = {}; c.querySelectorAll('[data-f]').forEach(el => { o[el.dataset.f] = el.type === 'checkbox' ? el.checked : el.value; });
  return o;
}
// Карточка источника сохраняется сама: через секунду после ввода, при выходе из поля, переключателем и перед «Проверить».
async function srcSaveCard(k, quiet) {
  clearTimeout(SRC.timers[k]); delete SRC.timers[k];
  const c = document.querySelector(`.src-card[data-k="${k}"]`); if (!c) return true;
  const o = srcCardValues(c);
  if (o.url && !(o.hint || '').includes(o.url)) o.hint = (o.hint ? o.hint + '. ' : '') + 'Confluence: ' + o.url;
  const st = c.querySelector('[data-saved]');
  try {
    await api('/api/config/sources', {settings: {[k]: o}});
    SRC.dirty.delete(k);
    if (SRC.data && SRC.data.settings && SRC.data.settings[k]) Object.assign(SRC.data.settings[k], o);
    if (st) { st.textContent = '✓ сохранено'; st.style.color = ''; setTimeout(() => { if (st.textContent === '✓ сохранено') st.textContent = ''; }, 2500); }
    return true;
  } catch (e) {
    if (st) { st.textContent = '✗ ' + e.message; st.style.color = 'var(--bad)'; }
    if (!quiet) msg('src-nomav', 'Не сохранено: ' + e.message, 'bad');
    return false;
  }
}
function srcCardInput(e) {
  const c = e.target.closest('.src-card'); if (!c || !e.target.dataset.f) return;
  const k = c.dataset.k; SRC.dirty.add(k);
  const st = c.querySelector('[data-saved]'); if (st) { st.textContent = 'изменено…'; st.style.color = ''; }
  clearTimeout(SRC.timers[k]);
  SRC.timers[k] = setTimeout(() => srcSaveCard(k, true), e.type === 'change' ? 50 : 1200);
}
document.addEventListener('DOMContentLoaded', () => {
  const box = $('src-cards'); if (!box) return;
  box.addEventListener('input', srcCardInput); box.addEventListener('change', srcCardInput);
});
window.addEventListener('beforeunload', () => { for (const k of SRC.dirty) srcSaveCard(k, true); });
async function srcStatus() {
  try { SRC.data = await api('/api/plugins'); } catch (e) { SRC.data = null; }
  return SRC.data;
}
function pstHTML(k) {
  const st = ((SRC.data || {}).status || {})[k];
  if (!SRC.data || !SRC.data.available) return '<span class="pst-i none" title="Maverick не настроен">нет Maverick</span>';
  if (!st || !st.enabled) return '<span class="pst-i none">выключен</span>';
  return `<span class="pst-i ${esc(st.state)}" title="${esc(st.detail || '')}">${esc(PST_RU[st.state] || st.state)}</span>`;
}
async function srcPills() {
  await srcStatus();
  const d = SRC.data;
  const el = $('src-pills');
  if (!d || !d.available) { el.innerHTML = ''; return; }
  el.innerHTML = SRC_KEYS.filter(k => d.status[k] && d.status[k].enabled).map(k => {
    const st = d.status[k].state;
    return `<button class="sp ${esc(st)}" onclick="go('src')" title="${esc(SRC_RU[k])}: ${esc(PST_RU[st] || st)}${d.status[k].detail ? ' — ' + esc(d.status[k].detail) : ''}">${SRC_IC[k]}<i></i></button>`;
  }).join('');
  document.querySelectorAll('[data-pst]').forEach(x => { x.innerHTML = pstHTML(x.dataset.pst); });
}
async function srcLoad() {
  await srcPills();
  const d = SRC.data; if (!d) return;
  // перерисовка карточек не должна терять открытые блоки, фокус и ещё не сохранённый ввод
  document.querySelectorAll('.src-card details[data-how]').forEach(x => {
    const k = x.closest('.src-card').dataset.k; if (x.open) SRC.open.add(k); else SRC.open.delete(k); });
  const dirty = {};
  for (const k of SRC.dirty) { const c = document.querySelector(`.src-card[data-k="${k}"]`); if (c) dirty[k] = srcCardValues(c); }
  const a = document.activeElement, fc = a && a.closest && a.closest('.src-card');
  const focus = fc && a.dataset.f ? {k: fc.dataset.k, f: a.dataset.f, pos: a.selectionStart || 0} : null;
  msg('src-nomav', d.available ? '' : 'Maverick не настроен: «Настройки» → «Модель» → Maverick (адрес и API-ключ). Пока его нет, источники недоступны.', 'warn');
  const about = Object.fromEntries((d.kinds || []).map(x => [x.k, x.about]));
  $('src-cards').innerHTML = SRC_KEYS.filter(k => d.settings[k]).map(k => {
    const s = d.settings[k], st = d.status[k];
    const links = (st.links || []).map(u => `<a href="${esc(u)}" target="_blank" rel="noopener">открыть ссылку для подключения ↗</a>`).join(' ');
    return `<div class="card src-card" data-k="${k}"><div class="src-h"><b style="white-space:nowrap">${SRC_IC[k]} ${esc(SRC_SHORT[k])}</b>` +
      `<label class="sw sm"><input type="checkbox" data-f="enabled"${s.enabled ? ' checked' : ''}><i></i></label>${pstHTML(k)}` +
      `<span style="flex:1"></span><span class="hint" data-saved></span><button class="b" onclick="srcCheck('${k}')" title="Сохранить настройки этого источника и проверить, отвечает ли плагин">Проверить</button></div>` +
      `<div class="hint" style="margin-top:-4px">${esc(about[k] || '')}</div>` +
      `<div class="hint">${st.at ? 'Проверено: ' + new Date(st.at).toLocaleString('ru-RU') + '. ' : ''}${esc(st.detail || '')} ${links}` +
      (d.saved && d.saved[k] ? ` · сохранено находок: ${d.saved[k]}` : '') + '</div>' +
      (st.state === 'offline' || st.state === 'error' ? `<div class="msg warn" style="display:block">Плагин не отвечает. Откройте чат Maverick, подключите плагин заново (вход, согласие) и нажмите «Проверить».</div>` + crHelpHTML(k)
        : `<details class="cr-d"><summary class="hint" style="cursor:pointer">Если Maverick «забудет» плагин</summary>${crHelpHTML(k)}</details>`) +
      `<details data-how${SRC.open.has(k) ? ' open' : ''}><summary class="hint" style="cursor:pointer">Как обращаться к плагину</summary><div class="grid2">` +
      `<div><label>Название плагина в Maverick (как в его чате)</label><input type="text" data-f="name" value="${esc(s.name)}" placeholder="например, Outlook"></div>` +
      (k === 'wiki' ? `<div><label>Адрес Confluence (для подсказки Maverick)</label><input type="text" data-f="url" value="${esc(s.url)}" placeholder="https://wiki.company.ru"></div>` : '<div></div>') +
      `<div><label>Подсказка Maverick (добавляется к каждому запросу)</label><input type="text" data-f="hint" value="${esc(s.hint)}" placeholder="например: ищи только в папке «Входящие»"></div>` +
      `<div><label>Поля запроса API, если плагин включается явно (JSON)</label><input type="text" data-f="body" value="${esc(s.body)}" placeholder='{"plugins": ["outlook"]}'></div>` +
      `</div><div class="actions"><button class="b p" onclick="srcSaveCard('${k}')">Сохранить</button>` +
      `<span class="hint">Сохраняется и само — через секунду после ввода и при нажатии «Проверить».</span></div></details></div>`;
  }).join('');
  for (const [k, vals] of Object.entries(dirty)) {  // то, что не успело сохраниться, пока перерисовывали
    const c = document.querySelector(`.src-card[data-k="${k}"]`); if (!c) continue;
    for (const [f, v] of Object.entries(vals)) { const el = c.querySelector(`[data-f="${f}"]`); if (el) { if (el.type === 'checkbox') el.checked = v; else el.value = v; } }
  }
  if (focus) { const el = document.querySelector(`.src-card[data-k="${focus.k}"] [data-f="${focus.f}"]`);
    if (el) { el.focus(); try { el.setSelectionRange(focus.pos, focus.pos); } catch (e) {} } }
  $('src-min').value = d.check_minutes || 30; $('src-web').value = d.web_url || ''; $('src-web').placeholder = d.maverick_web || 'https://maverick.company.ru';
  srcChecks('src-dg-list', 'src-dg-'); srcChecks('src-gl-src', 'src-gls-'); srcChecks('src-ds-src', 'src-dss-');
  document.querySelectorAll('#src-gl-src [data-pst], #src-ds-src [data-pst]').forEach(x => x.remove());
  SRC_KEYS.forEach(k => { $('src-dg-' + k).checked = !!d.digest_sources[k];
    $('src-gls-' + k).checked = (d.glossary.sources || []).includes(k); $('src-dss-' + k).checked = (d.describe_sources || []).includes(k); });
  document.querySelectorAll('#src-dg-list [data-pst]').forEach(x => { x.innerHTML = pstHTML(x.dataset.pst); });
  $('src-private').checked = !!d.cross_private;
  ['incident', 'problem', 'change', 'task'].forEach(k => { $('src-p-' + k).value = (d.patterns || {})[k] || ''; });
  $('src-gl-collect').checked = d.glossary.auto_collect; $('src-gl-process').checked = d.glossary.auto_process; $('src-gl-wiki').checked = d.glossary.use_wiki;
  $('src-lookup-people').checked = d.lookup_people !== false;
  $('src-img-on').checked = (d.images || {}).enabled !== false; $('src-img-desc').checked = (d.images || {}).describe !== false;
}
async function srcSave() {
  const settings = {};
  document.querySelectorAll('.src-card').forEach(c => {
    const o = {}; c.querySelectorAll('[data-f]').forEach(el => { o[el.dataset.f] = el.type === 'checkbox' ? el.checked : el.value; });
    if (o.url && !o.hint.includes(o.url)) o.hint = (o.hint ? o.hint + '. ' : '') + 'Confluence: ' + o.url;
    settings[c.dataset.k] = o;
  });
  const body = {settings, check_minutes: Number($('src-min').value) || 30, web_url: $('src-web').value.trim(),
    digest_sources: Object.fromEntries(SRC_KEYS.map(k => [k, $('src-dg-' + k).checked])),
    cross_private: $('src-private').checked,
    patterns: {incident: $('src-p-incident').value, problem: $('src-p-problem').value, change: $('src-p-change').value, task: $('src-p-task').value},
    glossary: {auto_collect: $('src-gl-collect').checked, auto_process: $('src-gl-process').checked, use_wiki: $('src-gl-wiki').checked,
      sources: SRC_KEYS.filter(k => $('src-gls-' + k).checked)},
    describe_sources: SRC_KEYS.filter(k => $('src-dss-' + k).checked), lookup_people: $('src-lookup-people').checked,
    images: {enabled: $('src-img-on').checked, describe: $('src-img-desc').checked}};
  try { await api('/api/config/sources', body); $('src-saved').textContent = '✓ Сохранено'; setTimeout(() => $('src-saved').textContent = '', 2500);
    CFG = await api('/api/config'); await srcLoad(); }
  catch (e) { $('src-saved').textContent = '✗ ' + e.message; }
}
async function srcCheck(kind) {
  const btn = kind ? null : 'src-check-all';
  if (btn) busy(btn, true, 'Проверяю…');
  // сначала сохранить, что введено: проверка идёт с настройками из файла
  for (const k of (kind ? [kind] : SRC_KEYS)) if (!(await srcSaveCard(k, !kind)) && kind) { return; }
  try { const {job} = await api('/api/plugins/check', kind ? {kind} : {});
    await poll(job, 'src-log', async j => { if (j.status === 'error') msg('src-nomav', j.error, 'bad'); await srcLoad(); });
  } catch (e) { alert(e.message); }
  if (btn) busy(btn, false);
}
// сводка: какие источники сверять (по умолчанию — из настроек)
async function dgSrcFill() {
  await srcPills();
  const d = SRC.data;
  $('dg-src').style.display = d && d.available ? '' : 'none';
  if (!d || !d.available) return;
  if (!$('dg-s-mail')) { srcChecks('dg-s-list', 'dg-s-', 'data-src'); document.querySelectorAll('#dg-s-list input').forEach(x => x.addEventListener('change', dgSrcWarn)); }
  document.querySelectorAll('#dg-s-list [data-pst]').forEach(x => { x.innerHTML = pstHTML(x.dataset.pst); });
  SRC_KEYS.forEach(k => { const el = $('dg-s-' + k); if (!el || !d.status[k]) return; el.checked = !!d.digest_sources[k] && d.status[k].enabled; el.disabled = !d.status[k].enabled; });
  dgSrcWarn();
}
function dgSources() { const o = {}; SRC_KEYS.forEach(k => { const el = $('dg-s-' + k); if (el) o[k] = el.checked && !el.disabled; }); return o; }
function dgSrcWarn() {
  const d = SRC.data; if (!d) return;
  const bad = SRC_KEYS.filter(k => $('dg-s-' + k) && $('dg-s-' + k).checked && d.status[k] && ['offline', 'error'].includes(d.status[k].state));
  msg('dg-src-warn', bad.length ? 'При последней проверке не отвечали: ' + bad.map(k => SRC_RU[k]).join(', ') +
    '. Переподключите плагин в чате Maverick («Настройки → Источники» подскажут учётную запись) — сводка проверит подключение ещё раз перед сверкой.' : '', 'warn');
}

// ---------------------------------------------------------------- глоссарий: очередь терминов, счётчик на вкладке
const GQ = {status: 'pending', items: [], counts: {}, sel: new Set()};
function glBadge() {
  const n = (GL.counts || {}).questions || 0, p = GQ.counts.pending || 0;
  const c = $('gl-tab-cnt');
  if (c) { c.textContent = n ? String(n) : ''; c.title = n ? `Вопросов к вам: ${n}` : ''; c.style.display = n ? '' : 'none'; }
  if ($('gl-stat')) $('gl-stat').textContent = `Терминов: ${GL.counts.terms || 0} (подтверждено ${GL.counts.confirmed || 0}, черновиков ИИ ${GL.counts.draft || 0})` + (p ? ` · в очереди: ${p}` : '');
}
async function gqLoad() {
  try { const j = await api('/api/glossary/queue?status=' + GQ.status); GQ.items = j.items; GQ.counts = j.counts || {}; }
  catch (e) { msg('gq-msg', e.message, 'bad'); return; }
  GQ.sel.clear(); gqRender(); glBadge();
  const d = SRC.data || await srcStatus(); $('gq-auto').checked = d ? d.glossary.auto_process : true;
  if (BG.glossary_auto) { $('gq-run').style.display = 'none'; $('gq-stop').style.display = ''; }
}
function gqRender() {
  const SRCR = {telegram: 'Telegram', digest: 'сводка', mail: 'почта', itsm: 'ITSM', wiki: 'вики', assistant: 'ассистент', chat_ai: 'ИИ в чате',
    rooms: 'X5 Rooms', tracker: 'трекер', meet: 'встречи', feedback: 'отзыв'};
  const c = GQ.counts;
  $('gq-n').textContent = (c.pending || 0) + ' ждут' + (c.question ? ` · ${c.question} с вопросом` : '');
  document.querySelectorAll('#gq-tabs button').forEach(b => { b.classList.toggle('on', b.dataset.s === GQ.status);
    b.textContent = b.textContent.replace(/ \(\d+\)$/, '') + (c[b.dataset.s] ? ` (${c[b.dataset.s]})` : ''); });
  $('gq-body').innerHTML = GQ.items.map(x => `<tr data-k="${esc(x.key)}"><td class="cb"><input type="checkbox" data-sel></td>` +
    `<td class="ttl"><b>${esc(x.term)}</b>${x.note ? `<div class="hint">${esc(x.note)}</div>` : ''}</td>` +
    `<td>${Object.entries(x.sources || {}).map(([k, v]) => `<span class="pill">${esc(SRCR[k] || k)} ${v}</span>`).join(' ')}</td>` +
    `<td class="gl-sum hint">${esc(((x.contexts || [])[0] || {}).text || '')}</td><td class="num">${x.count}</td>` +
    `<td style="white-space:nowrap"><button class="lnk" data-gq="fill">Заполнить</button> · <button class="lnk" data-gq="ai">✦ ИИ</button>` +
    (GQ.status === 'ignored' ? ` · <button class="lnk" data-gq="back">вернуть</button>` : ` · <button class="lnk" data-gq="skip">не термин</button>`) + '</td></tr>').join('') ||
    `<tr><td colspan="6" class="hint">${GQ.status === 'pending' ? 'Очередь пуста — термины появятся после сводки, вопросов ассистенту или ИИ в чате.' : 'Пусто.'}</td></tr>`;
  $('gq-bulk').style.display = GQ.sel.size ? '' : 'none'; $('gq-sel-n').textContent = `Выбрано: ${GQ.sel.size}`;
}
async function gqRun(selected) {
  const keys = selected ? [...GQ.sel] : [];
  busy('gq-run', true, 'Запускаю…');
  try { const {job} = await api('/api/glossary/process', keys.length ? {keys} : {limit: 40});
    BG.glossary_auto = job; $('gq-run').style.display = 'none'; $('gq-stop').style.display = '';
    await poll(job, 'gq-log', async j => { if (j.status === 'error') msg('gq-msg', j.error, 'bad'); else msg('gq-msg', ''); await gqLoad(); await glLoad(); });
  } catch (e) { msg('gq-msg', e.message, 'bad'); }
  delete BG.glossary_auto; $('gq-run').style.display = ''; $('gq-stop').style.display = 'none'; busy('gq-run', false);
}
async function gqSet(status, keys) {
  try { const j = await api('/api/glossary/queue/set', {keys: keys || [...GQ.sel], status}); GQ.counts = j.counts; await gqLoad(); } catch (e) { alert(e.message); }
}
async function gqAuto() {
  try { await api('/api/config/sources', {glossary: {auto_process: $('gq-auto').checked}}); CFG = await api('/api/config'); } catch (e) { alert(e.message); }
}
function gqFill(x) {
  const ex = (x.contexts || []).map(c => c.text).filter(Boolean);
  const refs = (x.contexts || []).map(c => String(c.ref || '')).filter(r => /^-?\d+:\d+$/.test(r)).map(r => r.split(':').map(Number));
  glOpen({term: x.term, kind: 'other', aliases: [], senses: [{}], status: 'confirmed', refs, examples: ex});
  GQ.filling = x.key;
}

document.addEventListener('DOMContentLoaded', () => {
  themeApply();
  $('mods-list').addEventListener('change', e => { const c = e.target.closest('[data-mod]'); if (c) modSet(c.dataset.mod, c.checked); });
  $('theme-seg').addEventListener('click', e => { const b = e.target.closest('[data-t]'); if (b) themeSet(b.dataset.t); });
  document.querySelectorAll('#dg-src input[data-src]').forEach(el => el.addEventListener('change', dgSrcWarn));
  $('gq-tabs').addEventListener('click', e => { const b = e.target.closest('[data-s]'); if (b) { GQ.status = b.dataset.s; gqLoad(); } });
  $('gq-all').addEventListener('change', e => { GQ.sel = e.target.checked ? new Set(GQ.items.map(x => x.key)) : new Set();
    document.querySelectorAll('#gq-body [data-sel]').forEach(c => c.checked = e.target.checked); gqRender(); });
  $('gq-body').addEventListener('change', e => { const tr = e.target.closest('tr[data-k]'); if (!tr || !e.target.matches('[data-sel]')) return;
    if (e.target.checked) GQ.sel.add(tr.dataset.k); else GQ.sel.delete(tr.dataset.k);
    $('gq-bulk').style.display = GQ.sel.size ? '' : 'none'; $('gq-sel-n').textContent = `Выбрано: ${GQ.sel.size}`; });
  $('gq-body').addEventListener('click', e => {
    const b = e.target.closest('[data-gq]'), tr = e.target.closest('tr[data-k]'); if (!b || !tr) return;
    const x = GQ.items.find(i => i.key === tr.dataset.k); if (!x) return;
    if (b.dataset.gq === 'fill') gqFill(x);
    else if (b.dataset.gq === 'ai') { GQ.sel = new Set([x.key]); gqRun(true); }
    else if (b.dataset.gq === 'skip') gqSet('ignored', [x.key]);
    else if (b.dataset.gq === 'back') gqSet('pending', [x.key]);
  });
});
