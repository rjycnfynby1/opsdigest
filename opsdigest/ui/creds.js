// «Учётные данные»: логины, пароли, токены. Секрет — только после повторного ввода пароля OpsDigest (на 2 минуты);
// буфер обмена очищается через 30 секунд. Программа сама никуда их не отправляет.
// Пользуется помощниками окна: api, $, esc, msg, toast (messenger.js), SRC (modules.js).
'use strict';
const CR = {items: [], plugins: {}, kinds: {}, open: false, left: 0, cur: null, pw: null, timer: null};
const CR_PL = {mail: 'Почта', itsm: 'ITSM', wiki: 'Вики', rooms: 'X5 Rooms', tracker: 'xTracker', meet: 'Scriber AI'};

async function crLoad() {
  try { const j = await api('/api/creds'); crSet(j); }
  catch (e) { msg('cr-warn', e.message, 'warn'); return; }
  msg('cr-warn', CR.protected === false ? 'Учётные данные хранятся только в защищённой папке — включите защиту (раздел «Защита»).' : '', 'warn');
  crRender();
}
function crSet(j) {
  Object.assign(CR, {items: j.items || [], plugins: j.plugins || {}, assigned: j.assigned || {}, kinds: j.kinds || CR.kinds, protected: j.protected});
  if (j.open !== undefined) crOpenState(j.open ? j.open_left : 0);
}
function crOpenState(left) {
  CR.left = left || 0; CR.open = CR.left > 0;
  clearInterval(CR.timer);
  if (CR.open) CR.timer = setInterval(() => { CR.left--; if (CR.left <= 0) { CR.open = false; clearInterval(CR.timer); } crOpenLine(); }, 1000);
  crOpenLine();
}
function crOpenLine() {
  const el = $('cr-open'); if (!el) return;
  el.textContent = CR.open ? `Доступ открыт ещё ${CR.left} с` : 'Пароли скрыты — показ и копирование после ввода пароля OpsDigest';
  $('cr-lock').style.display = CR.open ? '' : 'none';
}
function crRender() {
  const pl = k => `<span class="pill">${esc(CR_PL[k])}</span>`;
  $('cr-body').innerHTML = CR.items.map(x => `<tr data-id="${esc(x.id)}"><td class="ttl"><b>${esc(x.title)}</b>${x.main ? ' <span class="pill tk-ok">основная</span>' : ''}` +
    (x.note ? `<div class="hint">${esc(x.note)}</div>` : '') + `</td><td>${esc(CR.kinds[x.kind] || x.kind)}${x.auth ? `<div class="hint">${esc(x.auth)}</div>` : ''}</td>` +
    `<td>${esc(x.login)}${x.login ? ` <button class="lnk" data-cr="login" title="Скопировать логин">⧉</button>` : ''}${x.url ? `<div class="hint">${esc(x.url)}</div>` : ''}</td>` +
    `<td><span class="cr-sec" data-sec>${x.has_secret ? '•'.repeat(Math.min(12, x.secret_len || 8)) : '—'}</span>` +
    (x.has_secret ? ` <button class="lnk" data-cr="show" title="Показать">👁</button> <button class="lnk" data-cr="copy" title="Скопировать">⧉</button>` : '') +
    (x.copied_at ? `<div class="hint">копировали: ${new Date(x.copied_at).toLocaleString('ru-RU')}</div>` : '') + '</td>' +
    `<td>${(x.plugins || []).map(pl).join(' ')}</td><td><button class="lnk" data-cr="edit">Изменить</button></td></tr>`).join('') ||
    '<tr><td colspan="6" class="hint">Пока пусто. Добавьте основную учётную запись (логин и пароль для плагинов Maverick) и, если нужно, токен Confluence (PAT).</td></tr>';
  $('cr-map').innerHTML = Object.keys(CR_PL).map(k => {
    const cur = (CR.plugins || {})[k];
    const auto = (CR.assigned || {})[k] ? '' : (cur ? ' — ' + esc(cur.title) : '');
    return `<div><label>${esc(CR_PL[k])}</label><select data-crmap="${k}"><option value="">по умолчанию${auto}</option>` +
      CR.items.map(x => `<option value="${esc(x.id)}"${(CR.assigned || {})[k] === x.id ? ' selected' : ''}>${esc(x.title)}</option>`).join('') + '</select></div>';
  }).join('');
}
// ---- пароль программы
function crPassword() {
  return new Promise(res => { CR.pw = res; $('cr-pw-in').value = ''; msg('cr-pw-msg', ''); $('cr-pw').classList.add('on'); setTimeout(() => $('cr-pw-in').focus(), 50); });
}
function crPwDone(v) { $('cr-pw').classList.remove('on'); const r = CR.pw; CR.pw = null; if (r) r(v); }
async function crPwOk() {
  const pw = $('cr-pw-in').value; if (!pw) return;
  try { const j = await api('/api/creds/open', {password: pw}); crOpenState(j.open_left); crPwDone(true); }
  catch (e) { msg('cr-pw-msg', e.message, 'bad'); $('cr-pw-in').select(); }
}
async function crClose() { try { await api('/api/creds/close', {}); } catch (e) {} crOpenState(0); crLoad(); }
// ---- показать и скопировать
async function crReveal(id, field) {
  if (field !== 'login' && !CR.open) { if (!(await crPassword())) return null; }
  try { const j = await api('/api/creds/reveal', {id, field}); if (j.open_left !== undefined && field !== 'login') crOpenState(j.open_left); return j.value; }
  catch (e) {
    if (/пароль OpsDigest/.test(e.message)) { crOpenState(0); if (await crPassword()) return crReveal(id, field); return null; }
    toast(e.message); return null;
  }
}
async function crCopy(id, field) {
  const v = await crReveal(id, field); if (v == null) return;
  try { await navigator.clipboard.writeText(v); }
  catch (e) { const a = document.createElement('textarea'); a.value = v; document.body.appendChild(a); a.select(); try { document.execCommand('copy'); } catch (x) {} a.remove(); }
  toast(field === 'login' ? 'Логин скопирован' : 'Скопировано — буфер очистится через 30 с');
  if (field !== 'login') setTimeout(async () => {
    try { const now = await navigator.clipboard.readText(); if (now !== v) return; } catch (e) {}
    try { await navigator.clipboard.writeText(''); } catch (e) {}
  }, 30000);
  if (document.querySelector('#p-creds.on')) crLoad();
}
async function crShow(id, row) {
  const v = await crReveal(id, 'secret'); if (v == null) return;
  const el = row.querySelector('[data-sec]'); el.textContent = v; el.classList.add('shown');
  setTimeout(() => { el.textContent = '•'.repeat(Math.min(12, v.length || 8)); el.classList.remove('shown'); }, 15000);
}
// ---- редактирование
function crEdit(x) {
  CR.cur = x || null; x = x || {kind: 'account', plugins: []};
  $('cr-m-title').textContent = CR.cur ? 'Изменить: ' + x.title : 'Новая запись';
  $('cr-f-kind').innerHTML = Object.entries(CR.kinds).map(([k, v]) => `<option value="${k}"${k === x.kind ? ' selected' : ''}>${esc(v)}</option>`).join('');
  ['title', 'login', 'url', 'note', 'auth'].forEach(k => { $('cr-f-' + k).value = x[k] || ''; });
  $('cr-f-secret').value = ''; $('cr-f-secret').type = 'password';
  $('cr-f-keep').textContent = CR.cur && x.has_secret ? '(оставьте пустым — останется прежний)' : '';
  $('cr-f-main').checked = !!x.main || (!CR.cur && !CR.items.some(i => i.main));
  document.querySelectorAll('[data-crp]').forEach(c => { c.checked = (x.plugins || []).includes(c.dataset.crp); });
  $('cr-m-del').style.display = CR.cur ? '' : 'none'; msg('cr-m-msg', '');
  $('cr-modal').classList.add('on'); setTimeout(() => $('cr-f-title').focus(), 50);
}
async function crSave() {
  const item = {id: CR.cur ? CR.cur.id : null, kind: $('cr-f-kind').value, secret: $('cr-f-secret').value, main: $('cr-f-main').checked,
    plugins: [...document.querySelectorAll('[data-crp]')].filter(c => c.checked).map(c => c.dataset.crp)};
  ['title', 'login', 'url', 'note', 'auth'].forEach(k => { item[k] = $('cr-f-' + k).value; });
  try { const j = await api('/api/creds/save', {item}); $('cr-f-secret').value = ''; crSet(j); crRender(); $('cr-modal').classList.remove('on'); toast('Сохранено'); }
  catch (e) { msg('cr-m-msg', e.message, 'bad'); }
}
async function crDel() {
  if (!CR.cur || !confirm(`Удалить «${CR.cur.title}»?`)) return;
  try { const j = await api('/api/creds/delete', {id: CR.cur.id}); crSet(j); crRender(); $('cr-modal').classList.remove('on'); } catch (e) { msg('cr-m-msg', e.message, 'bad'); }
}
// подсказка в карточке плагина («Источники»): какая запись нужна, скопировать, открыть чат Maverick
function crHelpHTML(kind) {
  const d = (typeof SRC !== 'undefined' && SRC.data) || {};
  const c = (d.creds || {})[kind];
  const web = d.maverick_web ? `<a class="b" href="${esc(d.maverick_web)}" target="_blank" rel="noopener">Открыть чат Maverick ↗</a>` : '';
  if (!c) return `<div class="cr-help">Для переподключения: ${web || 'чат Maverick'}. <button class="lnk" onclick="go('creds')">Добавить учётные данные</button>, чтобы копировать логин и пароль отсюда.</div>`;
  return `<div class="cr-help"><span>Нужна запись: <b>${esc(c.title)}</b>${c.login ? ' · ' + esc(c.login) : ''}${c.url ? ' · ' + esc(c.url) : ''}${c.auth ? ' · ' + esc(c.auth) : ''}</span>` +
    (c.login ? `<button class="b" onclick="crCopy('${esc(c.id)}','login')">⧉ Логин</button>` : '') +
    `<button class="b" onclick="crCopy('${esc(c.id)}','secret')">⧉ ${c.kind === 'account' ? 'Пароль' : 'Токен'}</button>` +
    (c.url ? `<button class="b" onclick="crCopy('${esc(c.id)}','url')">⧉ Адрес</button>` : '') + web + '</div>';
}

document.addEventListener('DOMContentLoaded', () => {
  $('cr-body').addEventListener('click', e => {
    const b = e.target.closest('[data-cr]'), tr = e.target.closest('tr[data-id]'); if (!b || !tr) return;
    const x = CR.items.find(i => i.id === tr.dataset.id); if (!x) return;
    if (b.dataset.cr === 'edit') crEdit(x);
    else if (b.dataset.cr === 'show') crShow(x.id, tr);
    else if (b.dataset.cr === 'copy') crCopy(x.id, 'secret');
    else if (b.dataset.cr === 'login') crCopy(x.id, 'login');
  });
  $('cr-map').addEventListener('change', async e => {
    const s = e.target.closest('[data-crmap]'); if (!s) return;
    try { const j = await api('/api/creds/assign', {plugin: s.dataset.crmap, id: s.value || null}); crSet(j); crRender(); toast('Сохранено'); } catch (x) { toast(x.message); }
  });
});
