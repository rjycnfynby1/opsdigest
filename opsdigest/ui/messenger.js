// Мессенджер OpsDigest: чаты Telegram как в обычном клиенте + ИИ в контексте чата. Обновление программы из GitHub.
// Пользуется общими помощниками окна (index.html): api, $, esc, T, ST, CFG, LOCKED, listChats, openChat, CHATS.
'use strict';
const MX = {mode: 'set', started: false, loop: false, seq: 0, chats: [], byId: {}, folders: [], folder: 'all', cur: null, chat: null,
  msgs: [], more: true, loadingOld: false, hasNewer: false, reply: null, edit: null, files: [], status: {}, typing: {}, newCount: 0,
  unreadFrom: null, q: '', searchRes: null, openSeq: 0, ai: [], aiN: 0, aiQuote: null, menu: null, sel: null,
  topic: null, topics: [], topicView: false, pins: [], pinIdx: 0, marks: {mentions: [], reactions: []}, drafts: {}, inf: 'people'};
const lsGet = (k, d) => { try { const v = localStorage.getItem('od_' + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } };
const lsSet = (k, v) => { try { localStorage.setItem('od_' + k, JSON.stringify(v)); } catch (e) {} };
const KINDRU_MX = {photo: 'Фото', image: 'Изображение', document: 'Файл', video: 'Видео', gif: 'GIF', voice: 'Голосовое сообщение',
  audio: 'Аудио', video_note: 'Видеосообщение', sticker: 'Стикер'};
const MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];
const WD = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];
const EMO = ['👍', '❤', '🔥', '👏', '😁', '🤔', '👎', '🙏'];
const sleep = ms => new Promise(r => setTimeout(r, ms));
const online = () => MX.status.state === 'online';

// ================================================================ режим окна: мессенджер / настройка
// setMode() и mxAuto() — в modules.js: разделы окна (мессенджер, сводка, ассистент, глоссарий, настройки)
let _updT = null;
async function mxStart() {
  if (!MX.started) { MX.started = true; mxInit(); }
  try { MX.status = await api('/api/tg/state'); } catch (e) { MX.status = {state: 'error', error: e.message}; }
  renderConn(); await mxDialogs();
  if (!MX.loop) mxLoop();
}
async function mxDialogs() {
  try { const j = await api('/api/tg/dialogs'); MX.chats = j.chats; MX.folders = j.folders || []; MX.status = j.status || MX.status;
    MX.byId = {}; MX.chats.forEach(c => MX.byId[c.id] = c);
    if (MX.cur && MX.byId[MX.cur]) { MX.chat = MX.byId[MX.cur]; renderHead(); }
    renderFolders(); renderList(); renderConn();
  } catch (e) { $('mx-list').innerHTML = `<div class="mx-hint">${esc(e.message)}</div>`; }
}
async function mxLoop() {
  MX.loop = true;
  while (true) {
    if (LOCKED === 'full' || LOCKED === 'pin' || LOCKED === 'setup') { await sleep(1500); continue; }
    try {
      const j = await api(`/api/tg/events?since=${MX.seq}&wait=25`);
      if (j.seq < MX.seq) MX.seq = 0;  // программа перезапущена
      else MX.seq = j.seq;
      if (MX.status.state !== j.state) { MX.status.state = j.state; renderConn(); renderCompose(); }
      j.events.forEach(e => { try { mxEvent(e); } catch (err) { console.error(err); } });
    } catch (e) { await sleep(3000); }
  }
}
let _dlgT = null, _listT = null;
const _laterT = {};
const later = (fn, ms, key) => { key = key || 'l'; clearTimeout(_laterT[key]); _laterT[key] = setTimeout(fn, ms); };
function mxEvent(e) {
  switch (e.type) {
    case 'state': Object.assign(MX.status, {state: e.state, error: e.error, via: e.via}); renderConn(); renderCompose();
      if (e.state === 'online') later(mxDialogs, 1500, 'd'); break;
    case 'dialogs': later(mxDialogs, 400, 'd'); break;
    case 'folders': MX.folders = e.folders || []; renderFolders(); renderList(); break;
    case 'chat': { const c = e.chat; MX.byId[c.id] = Object.assign(MX.byId[c.id] || {}, c);
      if (!MX.chats.some(x => x.id === c.id)) MX.chats.push(MX.byId[c.id]);
      if (MX.cur === c.id) { MX.chat = MX.byId[c.id]; renderHead(); }
      later(renderList, 150); break; }
    case 'msg': case 'edit': for (const v of e.msgs || []) onMsg(v, e.type, e.quiet); break;
    case 'media': if (e.chat === MX.cur) { const v = MX.msgs.find(x => x.id === e.msg); if (v) { v.media = e.media; rerender(v.id); } } break;
    case 'del': onDel(e); break;
    case 'topics': case 'topic': if (e.chat === MX.cur && MX.chat && MX.chat.forum) later(() => mxTopicsLoad(false), 600, 't'); break;
    case 'read_out': { const c = MX.byId[e.chat]; if (c) c.read_out = Math.max(c.read_out || 0, e.max);
      if (e.chat === MX.cur) document.querySelectorAll('#mx-msgs .mr.out').forEach(n => { const ck = n.querySelector('.ck');
        if (ck && Number(n.dataset.id) <= e.max) ck.textContent = '✓✓'; }); break; }
    case 'typing': MX.typing[e.chat] = {who: e.who, until: Date.now() + 6000}; later(renderList, 100);
      if (e.chat === MX.cur) renderHead(); setTimeout(() => { renderList(); if (e.chat === MX.cur) renderHead(); }, 6100); break;
  }
}
// уведомление Windows без текста и имён: центр уведомлений хранит историю на диске, а переписка — только в программе
let _ntfN = 0, _ntf = null;
function notifyNew(v) {
  const c = MX.byId[v.chat];
  if (!c || c.muted || c.archived || v.out || !('Notification' in window) || Notification.permission !== 'granted') return;
  if (document.hasFocus() && v.chat === MX.cur && MX.mode === 'mx') return;
  _ntfN++;
  try { if (_ntf) _ntf.close();
    _ntf = new Notification('OpsDigest', {body: _ntfN === 1 ? 'Новое сообщение в Telegram' : 'Новых сообщений в Telegram: ' + _ntfN, tag: 'opsdigest'});
    _ntf.onclick = () => { window.focus(); setMode('mx'); mxOpen(v.chat); _ntf.close(); };
  } catch (e) {}
}
window.addEventListener('focus', () => { _ntfN = 0; if (_ntf) { _ntf.close(); _ntf = null; } });
function onMsg(v, typ, quiet) {
  if (typ === 'msg' && !quiet) notifyNew(v);
  const c = MX.byId[v.chat];
  if (c && (!c.last || v.id >= c.last.id) && typ === 'msg') {
    c.last = {id: v.id, text: snippetMx(v), from: v.from, out: v.out, date: v.date};
    if (!c.date || v.date > c.date) c.date = v.date;
    delete MX.typing[v.chat];
    later(renderList, 150);
  }
  if (v.chat !== MX.cur) return;
  if (MX.topicView) return;
  if (MX.topic != null && (v.topic || 1) !== MX.topic) return;
  const i = MX.msgs.findIndex(x => x.id === v.id);
  if (i >= 0) { MX.msgs[i] = v; rerender(v.id); return; }
  if (typ !== 'msg' || MX.hasNewer) { if (typ === 'msg' && !v.out) { MX.newCount++; renderDown(); } return; }
  const box = $('mx-msgs'), atBottom = nearBottom();
  MX.msgs.push(v); MX.msgs.sort((a, b) => a.id - b.id);
  if (MX.msgs[MX.msgs.length - 1].id !== v.id) { renderAll('keep'); return; }
  appendLast();
  if (atBottom || v.out) box.scrollTop = box.scrollHeight; else if (!v.out) { MX.newCount++; renderDown(); }
}
function onDel(e) {
  const gone = new Set((e.items || []).map(x => x[0] + ':' + x[1]));
  if (e.chat != null) (e.ids || []).forEach(i => gone.add(e.chat + ':' + i));
  else if (MX.cur != null && MX.cur > -1000000000000) (e.ids || []).forEach(i => gone.add(MX.cur + ':' + i));
  const before = MX.msgs.length;
  MX.msgs = MX.msgs.filter(v => !gone.has(v.chat + ':' + v.id));
  if (MX.msgs.length !== before) renderAll('keep');
}

// ================================================================ левая колонка
function renderConn() {
  const s = MX.status || {}, el = $('mx-conn');
  const map = {online: ['online', 'В сети' + (s.via ? ' · ' + s.via : '')], connecting: ['wait', 'Подключаюсь к Telegram…'],
    reconnecting: ['wait', 'Связь оборвалась — переподключаюсь…'], error: ['bad', 'Нет связи: ' + (s.error || '')],
    auth: ['bad', 'Нужен вход в Telegram'], off: ['bad', s.error || 'Не подключено']};
  const [cls, text] = map[s.state] || map.off;
  el.className = 'mx-conn ' + cls;
  el.innerHTML = `<i></i><span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(text)}">${esc(text)}</span>` +
    (s.state === 'auth' ? `<button onclick="setMode('set');go('login')">Войти</button>` : s.state !== 'online' ? `<button onclick="mxReconnect()">Переподключить</button>` : '');
}
async function mxReconnect() { try { MX.status = await api('/api/tg/connect', {}); renderConn(); } catch (e) { toast(e.message); } }
function folderList() {
  const fs = [{id: 'all', title: 'Все чаты'}].concat(MX.folders || []);
  if (MX.chats.some(c => c.archived)) fs.push({id: 'archive', title: 'Архив'});
  return fs;
}
function inFolder(c, f) {
  if (f.id === 'all') return !c.archived;
  if (f.id === 'archive') return c.archived;
  if ((f.exc || []).includes(c.id)) return false;
  if ((f.inc || []).includes(c.id) || (f.pin || []).includes(c.id)) return true;
  const fl = f.flags || {};
  if (fl.exclude_muted && c.muted) return false;
  if (fl.exclude_read && !c.unread) return false;
  if (fl.exclude_archived && c.archived) return false;
  if (c.kind === 'private') return c.contact ? !!fl.contacts : !!fl.non_contacts;
  if (c.kind === 'bot') return !!fl.bots;
  if (c.kind === 'group') return !!fl.groups;
  if (c.kind === 'channel') return !!fl.broadcasts;
  return false;
}
const FOLD_ICON = {all: '💬', archive: '🗄'};
function renderFolders() {
  const fs = folderList();
  if (!fs.some(f => String(f.id) === String(MX.folder))) MX.folder = 'all';
  $('mx-fold').style.display = fs.length > 1 ? '' : 'none';
  $('mx-fold').innerHTML = fs.map(f => { const n = MX.chats.filter(c => inFolder(c, f) && c.unread && !c.muted).length;
    const ic = f.emo || FOLD_ICON[f.id] || '📁';
    return `<button data-f="${esc(f.id)}" class="${String(f.id) === String(MX.folder) ? 'on' : ''}${n ? ' has-unread' : ''}" title="${esc(f.title)}"><i>${esc(ic)}</i><span>${esc(f.title)}</span>${n ? `<b>${n > 99 ? '99+' : n}</b>` : ''}</button>`; }).join('');
}
function avColor(id) { return 'c' + (Math.abs(Number(id) || 0) % 7); }
function initials(t) {
  const w = String(t || '?').replace(/[^\p{L}\p{N}\s]/gu, ' ').trim().split(/\s+/).filter(Boolean);
  return ((w[0] || '?')[0] + (w[1] ? w[1][0] : '')).toUpperCase();
}
// ---- аватарки: фото профиля Telegram (окно просит по одной — не больше 2 сразу, в общей очереди запросов окна — и держит в памяти как blob:)
const AV = {url: {}, none: new Set(), pending: new Set(), queue: [], active: 0};
function avKey(id, pid) { return id + ':' + (pid || 'u'); }
function avWant(id, pid) {
  const k = avKey(id, pid);
  if (AV.url[k] || AV.none.has(k) || AV.pending.has(k)) return;
  AV.pending.add(k); AV.queue.push([id, pid, k]); avPump();
}
function avPump() {
  while (AV.active < 2 && AV.queue.length) {
    const [id, pid, k] = AV.queue.shift(); AV.active++;
    netFetch(`/avatar/${id}?v=${encodeURIComponent(pid || 'u')}`, {headers: {'X-Token': T}})
      .then(r => r.ok ? r.blob() : null)
      .then(b => {
        if (!b || !b.size) { AV.none.add(k); return; }
        AV.url[k] = URL.createObjectURL(b);
        document.querySelectorAll(`[data-av="${k}"]`).forEach(el => { el.innerHTML = `<img src="${AV.url[k]}" alt="">`; el.classList.add('ph'); });
      })
      .catch(() => AV.none.add(k))
      .finally(() => { AV.pending.delete(k); AV.active--; avPump(); });
  }
}
// просим только видимые: элементы с data-av замечаются раз в 0,7 с и загружаются, когда попадают на экран
const AV_IO = 'IntersectionObserver' in window ? new IntersectionObserver(es => es.forEach(e => {
  if (!e.isIntersecting) return; AV_IO.unobserve(e.target);
  const k = e.target.dataset.av, i = k.lastIndexOf(':'); avWant(Number(k.slice(0, i)), k.slice(i + 1));
}), {rootMargin: '200px'}) : null;
setInterval(() => document.querySelectorAll('[data-av]:not([data-avo])').forEach(el => {
  el.dataset.avo = '1'; const k = el.dataset.av;
  if (AV.url[k]) { el.innerHTML = `<img src="${AV.url[k]}" alt="">`; el.classList.add('ph'); return; }
  if (AV_IO) AV_IO.observe(el); else { const i = k.lastIndexOf(':'); avWant(Number(k.slice(0, i)), k.slice(i + 1)); }
}), 700);
function avatar(c, sm, person) {
  if (c.title === 'Избранное' && c.excluded && c.kind === 'private') return `<div class="av saved${sm ? ' sm' : ''}">🔖</div>`;
  // чат — если в списке чатов известно, что фото есть; человек — пробуем (нет фото — останутся буквы)
  const want = person ? !!c.id : !!c.photo;
  const k = avKey(c.id, person ? 'u' : c.photo);
  const inner = AV.url[k] ? `<img src="${AV.url[k]}" alt="">` : esc(initials(c.title));
  return `<div class="av ${avColor(c.id)}${sm ? ' ' + (sm === true ? 'sm' : sm) : ''}${AV.url[k] ? ' ph' : ''}"${want ? ` data-av="${k}"` : ''}>${inner}</div>`;
}
function listTime(iso) {
  if (!iso) return '';
  const d = new Date(iso), now = new Date();
  if (d.toDateString() === now.toDateString()) return d.toTimeString().slice(0, 5);
  if (now - d < 6 * 864e5) return WD[d.getDay()];
  return d.toLocaleDateString('ru-RU', {day: '2-digit', month: '2-digit', year: '2-digit'});
}
function snippetMx(v) {
  let t = (v.text || '').replace(/^\[[^\]]*\]\s*/, m => (v.media || (v.m && v.m.fw)) ? '' : m);
  if (!t && v.media) t = KINDRU_MX[v.media.kind] || 'Вложение';
  return t.replace(/\s+/g, ' ').slice(0, 120);
}
function chatItem(c) {
  const ty = MX.typing[c.id];
  const typing = ty && ty.until > Date.now();
  let sub = '';
  if (typing) sub = `<span class="typing">${c.kind === 'private' ? 'печатает…' : esc((ty.who || '').split(' ')[0]) + ' печатает…'}</span>`;
  else if (c.id !== MX.cur && Object.keys(MX.drafts).some(k => k.startsWith(c.id + ':'))) {
    const k = Object.keys(MX.drafts).find(k => k.startsWith(c.id + ':'));
    sub = `<span class="draft">Черновик: </span>${esc(MX.drafts[k].slice(0, 80))}`; }
  else if (c.last) {
    const who = c.last.out ? 'Вы: ' : (c.kind === 'group' && c.last.from ? (c.last.from.split(' ')[0] + ': ') : '');
    sub = (who ? `<span class="ci-who">${esc(who)}</span>` : '') + esc(c.last.text || '');
  }
  const badges = (c.mentions ? `<span class="badge at">@</span>` : '') +
    (c.unread ? `<span class="badge${c.muted ? ' muted' : ''}">${c.unread > 999 ? '999+' : c.unread}</span>` : c.pinned ? '<span class="pin">📌</span>' : '');
  return `<div class="ci${c.id === MX.cur ? ' on' : ''}" data-chat="${c.id}">${avatar(c)}<div class="ci-body">` +
    `<div class="ci-row"><span class="ci-title">${c.forum ? '<span title="Форум с темами">🗂</span> ' : ''}${esc(c.title || String(c.id))}${c.muted ? ' 🔕' : ''}</span><span class="ci-time">${listTime(c.date)}</span></div>` +
    `<div class="ci-row"><span class="ci-sub">${sub || '&nbsp;'}</span>${badges}</div></div></div>`;
}
function renderList() {
  const q = MX.q.trim().toLowerCase();
  const f = folderList().find(x => String(x.id) === String(MX.folder)) || {id: 'all'};
  let list = MX.chats.filter(c => q ? (c.title || '').toLowerCase().includes(q) || (c.username || '').toLowerCase().includes(q) : inFolder(c, f));
  if (!q) { const pin = new Set(f.pin || []);
    list.sort((a, b) => (b.pinned && f.id === 'all') - (a.pinned && f.id === 'all') || pin.has(b.id) - pin.has(a.id) || (b.date || '').localeCompare(a.date || '')); }
  let html = list.length ? list.map(chatItem).join('') : `<div class="mx-hint">${q ? 'Чатов не найдено' : MX.chats.length ? 'В этой папке пусто' : online() ? 'Загружаю чаты…' : 'Чатов в базе пока нет — подключитесь к Telegram или загрузите экспорт (Настройка → Telegram)'}</div>`;
  if (q && MX.searchRes) {
    html += `<div class="mx-sect">Сообщения</div>` + (MX.searchRes.length ? MX.searchRes.map(v => `<div class="sr" data-chat="${v.chat}" data-msg="${v.id}">` +
      `<div class="ci-row"><span class="ci-title">${esc(v.chat_title)}</span><span class="ci-time">${listTime(v.date)}</span></div>` +
      `<div class="ci-sub">${v.out ? '<span class="ci-who">Вы: </span>' : v.from ? `<span class="ci-who">${esc(v.from.split(' ')[0])}: </span>` : ''}${hl(snippetMx(v), MX.q)}</div></div>`).join('')
      : '<div class="mx-hint">Ничего не найдено</div>');
  } else if (q && q.length >= 3) html += '<div class="mx-hint"><span class="spin"></span>Ищу в сообщениях…</div>';
  $('mx-list').innerHTML = html;
  let total = 0; MX.chats.forEach(c => { if (!c.muted && !c.archived) total += c.unread || 0; });
  document.title = (total ? `(${total}) ` : '') + 'OpsDigest';
}
function hl(text, q) {
  const words = q.toLowerCase().split(/\s+/).filter(w => w.length > 1);
  let h = esc(text);
  for (const w of words) h = h.replace(new RegExp(w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'), m => `<b>${m}</b>`);
  return h;
}
let _searchT = null;
function mxSearchInput(v) {
  MX.q = v; MX.searchRes = null; renderList(); clearTimeout(_searchT);
  if (v.trim().length >= 3) _searchT = setTimeout(() => mxSearch(v), 700);
}
async function mxSearch(v) {
  try { const j = await api('/api/tg/search?q=' + encodeURIComponent(v)); if (MX.q === v) { MX.searchRes = j.results; renderList(); } }
  catch (e) { if (MX.q === v) { MX.searchRes = []; renderList(); } }
}

// ================================================================ чат: открытие, лента
function nearBottom() { const b = $('mx-msgs'); return b.scrollHeight - b.scrollTop - b.clientHeight < 140; }
function draftKey() { return MX.cur + ':' + (MX.topic != null ? MX.topic : ''); }
function draftSave() {
  if (MX.cur == null || !$('mx-in')) return;
  const v = $('mx-in').value; if (MX.edit) return;
  if (v.trim()) MX.drafts[draftKey()] = v; else delete MX.drafts[draftKey()];
  lsSet('drafts', MX.drafts);
}
async function mxOpen(id, opts) {
  opts = opts || {};
  id = Number(id);
  const topic = opts.topic != null ? Number(opts.topic) : null;
  if (MX.cur === id && opts.around && (topic == null || topic === MX.topic) && !MX.topicView) { if (jumpTo(opts.around)) return; }
  draftSave();
  const chat = MX.byId[id] || {id, title: String(id), kind: ''};
  if (chat.forum && topic == null && !opts.around && !opts.all) return mxTopicsOpen(id);
  const seq = ++MX.openSeq;
  MX.cur = id; MX.chat = chat; MX.reply = null; MX.edit = null; MX.files = []; MX.topic = topic; MX.topicView = false;
  MX.newCount = 0; MX.hasNewer = !!opts.around; MX.more = true; MX.aiQuote = null; MX.pins = []; MX.marks = {mentions: [], reactions: []};
  $('mx-empty').style.display = 'none'; $('mx-chatbox').style.display = 'flex';
  tcolShow(!!chat.forum);
  $('mx-sbar').classList.remove('on'); $('mx-sres').classList.remove('on');
  $('mx-in').value = MX.drafts[draftKey()] || ''; autosize();
  renderList(); renderHead(); renderBars(); renderAtt(); renderCompose(); renderDown(); renderAi(); renderPin(); renderJumps();
  if (MX.infOn) infLoad();
  if (chat.forum && MX.topicsChat !== id) { tcolHead(chat); mxTopicsLoad(online()); }  // колонка тем — рядом с перепиской
  else if (chat.forum) { tcolHead(chat); renderTopics(); }
  $('mx-msgs').innerHTML = '<div class="mx-more"><span class="spin"></span></div>';
  const q = (opts.around ? `&around=${opts.around}` : '') + (topic != null ? `&topic=${topic}` : '');
  try {
    let j = await api(`/api/tg/history?chat=${id}&limit=50${q}`);
    if (seq !== MX.openSeq) return;
    if (j.chat) { MX.byId[id] = Object.assign(MX.byId[id] || {}, j.chat); MX.chat = MX.byId[id]; renderHead(); }
    MX.msgs = j.messages; MX.more = j.more;
    MX.unreadFrom = calcUnread();
    renderAll(opts.around ? {id: opts.around} : MX.unreadFrom ? 'unread' : 'bottom');
    if (online()) {
      j = await api(`/api/tg/history?chat=${id}&limit=50&live=1${q}`);
      if (seq !== MX.openSeq) return;
      const wasBottom = nearBottom();
      const by = new Map(MX.msgs.map(v => [v.id, v])); j.messages.forEach(v => by.set(v.id, v));
      MX.msgs = [...by.values()].sort((a, b) => a.id - b.id); MX.more = j.more;
      MX.unreadFrom = calcUnread();
      renderAll(opts.around ? {id: opts.around} : wasBottom && !MX.unreadFrom ? 'bottom' : MX.unreadFrom ? 'unread' : 'keep');
    }
    if (seq === MX.openSeq) { loadPins(seq); loadMarks(seq); }
  } catch (e) { if (seq === MX.openSeq) $('mx-msgs').innerHTML = `<div class="mx-more">${esc(e.message)}</div>`; }
}
function calcUnread() {
  // первое непрочитанное: по отметке прочтения из Telegram, а если её нет — по числу непрочитанных
  const c = MX.chat || {};
  if (MX.topic != null) { const t = MX.topics.find(x => x.topic_id === MX.topic); if (!t || !t.unread) return null;
    const inc = MX.msgs.filter(v => !v.out); const f = t.read_in ? inc.find(v => v.id > t.read_in) : inc[Math.max(0, inc.length - t.unread)]; return f ? f.id : null; }
  if (!c.unread) return null;
  const inc = MX.msgs.filter(v => !v.out);
  const f = c.read_in ? inc.find(v => v.id > c.read_in) : inc[Math.max(0, inc.length - c.unread)];
  return f ? f.id : null;
}
function jumpTo(id) {
  const n = document.querySelector(`#mx-msgs .mr[data-ids~="${id}"]`);
  if (!n) return false;
  n.scrollIntoView({block: 'center'}); n.classList.remove('hl'); void n.offsetWidth; n.classList.add('hl');
  return true;
}
function renderHead() {
  const c = MX.chat; if (!c) return;
  const ty = MX.typing[c.id];
  let sub = {private: 'личный чат', group: 'группа', channel: 'канал', bot: 'бот'}[c.kind] || '';
  if (c.username) sub += ' · @' + c.username;
  const typing = ty && ty.until > Date.now();
  if (typing) sub = c.kind === 'private' ? 'печатает…' : `${(ty.who || '').split(' ')[0]} печатает…`;
  const chips = (c.excluded ? '<span class="chip">не в сводке</span>' : '') + (c.mode === 'incident' ? '<span class="chip">авария</span>' : '') +
    (c.mode === 'alerts' ? '<span class="chip">алерты</span>' : '') + (c.prio === 'high' && !c.excluded ? '<span class="chip">важный</span>' : '');
  const t = MX.topic != null ? MX.topics.find(x => x.topic_id === MX.topic) : null;
  const tUnread = t ? t.unread : c.unread;
  const back = '';  // темы форума видны в соседней колонке
  const ttl = t ? `${esc(c.title || '')} <span class="crumb">›</span> ${esc(t.title)}` : (c.forum && MX.topic == null ? esc(c.title) + ' <span class="crumb">› все сообщения</span>' : esc(c.title || String(c.id)));
  const muted = t ? t.muted : c.muted;
  $('mx-head').innerHTML = back + avatar(c, true) +
    `<div class="t" onclick="infToggle(true)" title="Профиль чата: участники, медиа, файлы, ссылки"><b>${ttl}</b><small class="${typing ? 'typing' : ''}">${esc(sub)}${chips}</small></div>` +
    (tUnread ? `<button class="ib" onclick="mxRead()" title="Отметить прочитанным в Telegram">✓ Прочитано (${tUnread})</button>` : '') +
    `<button class="ib" onclick="mxMute(${muted ? 'false' : 'true'})" title="${muted ? 'Включить уведомления' : 'Без звука'}${t ? ' (тема)' : ''}">${muted ? '🔕' : '🔔'}</button>` +
    `<button class="ib" onclick="mxSearchChat()" title="Поиск в чате">🔍</button>` +
    `<button class="ib" onclick="infToggle()" title="Участники, медиа, файлы, ссылки, голосовые">ℹ</button>` +
    `<button class="ib ai${MX.aiOn ? ' on' : ''}" onclick="aiToggle()" title="ИИ: сводка чата, вопросы, черновики, объяснить">✦ ИИ</button>` +
    `<button class="ib" onclick="mxChatSettings()" title="Важность, режим и описание чата (и тем) для сводок">⚙</button>`;
}
function dayLabel(iso) {
  const d = new Date(iso), now = new Date(), y = new Date(now - 864e5);
  if (d.toDateString() === now.toDateString()) return 'Сегодня';
  if (d.toDateString() === y.toDateString()) return 'Вчера';
  return d.getDate() + ' ' + MONTHS[d.getMonth()] + (d.getFullYear() !== now.getFullYear() ? ' ' + d.getFullYear() : '');
}
function items(list) {
  const out = [];
  for (const v of list) {
    const g = v.m && v.m.g, last = out[out.length - 1];
    if (g && last && last.g === g && last.msgs.length < 10) last.msgs.push(v); else out.push({g, msgs: [v]});
  }
  return out;
}
function itemHTML(it, prev) {
  const v = it.msgs[0], c = MX.chat || {};
  const day = new Date(v.date).toDateString(), pv = prev && prev.msgs[prev.msgs.length - 1];
  let h = '';
  if (!pv || new Date(pv.date).toDateString() !== day) h += `<div class="dsep"><span>${dayLabel(v.date)}</span></div>`;
  if (MX.unreadFrom && it.msgs.some(x => x.id === MX.unreadFrom)) h += '<div class="unr" id="mx-unr">Непрочитанные сообщения</div>';
  const sameAuthor = pv && pv.out === v.out && pv.sid === v.sid && new Date(pv.date).toDateString() === day && (new Date(v.date) - new Date(pv.date)) < 600000;
  const showWho = !v.out && c.kind !== 'private' && !sameAuthor && v.from && !(c.kind === 'channel' && v.from === c.title);
  const meta = v.m || {};
  const fwd = it.msgs.map(x => x.m && x.m.fw).find(Boolean);
  const imgs = it.msgs.filter(x => x.media && (x.media.kind === 'photo' || x.media.kind === 'image'));
  const album = it.msgs.length > 1 && imgs.length === it.msgs.length;
  const body = it.msgs.map(x => x.m && x.m.poll ? '' : textOf(x)).filter(Boolean);
  const hasMedia = it.msgs.some(x => x.media);
  const onlyMedia = hasMedia && !body.length && !fwd && !v.reply && imgs.length === it.msgs.length;
  let inner = '';
  if (showWho) inner += `<div class="who k${Math.abs(Number(v.sid) || 0) % 7}">${esc(v.from)}</div>`;
  if (fwd) inner += `<div class="fw">Переслано от <b>${esc(fwd)}</b></div>`;
  const rx_ = meta.rx_;
  if (rx_) inner += `<div class="rq ext" ${rx_.c ? `data-ext="${rx_.c}:${rx_.id || 0}"` : ''} title="Ответ на сообщение из другого чата"><b>↩ ${esc(rx_.from || 'Сообщение')}${rx_.title ? ' · «' + esc(rx_.title) + '»' : ' · из другого чата'}</b><span>${esc(rx_.q || rx_.media || (rx_.c ? 'откройте, чтобы посмотреть' : 'исходное сообщение недоступно'))}</span></div>`;
  else if (v.reply) inner += `<div class="rq" data-jump="${v.reply}"><b>${esc(v.rp ? v.rp.from : 'Сообщение #' + v.reply)}</b><span>${esc(meta.rq || (v.rp ? v.rp.text : 'нажмите, чтобы перейти к нему'))}</span></div>`;
  if (meta.poll) inner += pollHTML(v);
  if (album) inner += `<div class="alb n${Math.min(imgs.length, 3) === 1 ? 1 : imgs.length % 3 === 0 ? 3 : 2}">${imgs.map(x => mediaHTML(x)).join('')}</div>`;
  else it.msgs.forEach(x => { if (x.media) inner += mediaHTML(x); });
  const ticks = v.out && c.kind !== 'channel' ? `<span class="ck">${v.id <= (c.read_out || 0) ? '✓✓' : '✓'}</span>` : '';
  const tm = `<span class="tm">${meta.v ? '👁 ' + meta.v + ' · ' : ''}${meta.ed ? 'изм. ' : ''}${new Date(v.date).toTimeString().slice(0, 5)}${ticks}</span>`;
  if (body.length) inner += `<div class="tx">${body.join('\n')}${tm}</div>`; else inner += onlyMedia ? tm : `<div class="tx">${tm}</div>`;
  const rx = it.msgs.flatMap(x => (x.m && x.m.rx || []).map(r => [x.id, r]));
  if (rx.length) inner += `<div class="rx">${rx.map(([id, r]) => `<button class="${r[2] ? 'my' : ''}" data-react="${id}" data-emo="${esc(r[0])}">${esc(r[0])} ${r[1]}</button>`).join('')}</div>`;
  const bt = meta.bt || [];
  if (bt.length) inner += `<div class="bt">${bt.map(b => b[1] && TG_RX.test(b[1]) ? `<a class="tg" data-tg="${esc(b[1])}">${esc(b[0])}</a>` : b[1] && /^https?:/i.test(b[1]) ? `<a href="${esc(b[1])}" target="_blank" rel="noopener noreferrer">${esc(b[0])}</a>` : `<span title="Кнопка бота — нажмите в Telegram">${esc(b[0])}</span>`).join('')}</div>`;
  const ids = it.msgs.map(x => x.id).join(' ');
  const withAv = !v.out && (c.kind === 'group');
  const av = withAv ? `<div class="mav">${showWho ? avatar({id: v.sid, title: v.from}, 'xs', true) : ''}</div>` : '';
  return h + `<div class="mr${v.out ? ' out' : ''}${sameAuthor ? '' : ' gap'}${withAv ? ' wav' : ''}" data-id="${v.id}" data-ids="${ids}">` + av +
    `<div class="bb${hasMedia ? ' media' : ''}${onlyMedia ? ' only' : ''}">${inner}</div>` +
    `<div class="mact"><button data-act="reply" title="Ответить">↩</button><button data-act="menu" title="Действия и ИИ">⋯</button></div></div>`;
}
function textOf(v) {
  const meta = v.m || {};
  let skip = 0;
  if (v.media || meta.fw) { if (meta.p) skip = meta.p; else { const m = (v.text || '').match(/^\[[^\]]*\]\s*/); if (m) skip = m[0].length; } }
  if (v.media && v.media.kind === 'sticker') return '';
  const s = (v.text || '').slice(skip);
  if (!s.trim()) return '';
  return rich(v.text || '', meta.e, skip);
}
function renderAll(mode) {
  const box = $('mx-msgs'), fromBottom = box.scrollHeight - box.scrollTop;
  const its = items(MX.msgs);
  let html = MX.more ? '<div class="mx-more" id="mx-moreline">' + (MX.loadingOld ? '<span class="spin"></span>' : '') + '</div>'
    : '<div class="mx-more">Начало переписки</div>';
  its.forEach((it, i) => { html += itemHTML(it, its[i - 1]); });
  if (!MX.msgs.length) html += `<div class="mx-more">${online() ? 'Сообщений нет' : 'В базе нет сообщений этого чата — нужна связь с Telegram'}</div>`;
  box.innerHTML = `<div class="col">${html}</div>`;
  if (mode === 'bottom') box.scrollTop = box.scrollHeight;
  else if (mode === 'unread') { const u = $('mx-unr'); if (u) box.scrollTop = Math.max(0, u.offsetTop - 60); else box.scrollTop = box.scrollHeight; }
  else if (mode && mode.id) { if (!jumpTo(mode.id)) box.scrollTop = box.scrollHeight; }
  else box.scrollTop = box.scrollHeight - fromBottom;
  renderDown();
}
function appendLast() {
  const its = items(MX.msgs), col = $('mx-msgs').querySelector('.col');
  if (!col) return renderAll('bottom');
  const last = its[its.length - 1];
  if (last.msgs.length > 1) return rerender(last.msgs[0].id);
  col.insertAdjacentHTML('beforeend', itemHTML(last, its[its.length - 2]));
}
function rerender(id) {
  const its = items(MX.msgs), k = its.findIndex(it => it.msgs.some(x => x.id === id));
  const node = document.querySelector(`#mx-msgs .mr[data-ids~="${id}"]`);
  if (k < 0 || !node) return;
  const tmp = document.createElement('div');
  tmp.innerHTML = itemHTML(its[k], its[k - 1]);
  const fresh = tmp.querySelector('.mr');
  if (fresh) node.replaceWith(fresh);
}
async function loadOlder() {
  if (MX.loadingOld || !MX.more || !MX.msgs.length || MX.cur == null) return;
  MX.loadingOld = true; const seq = MX.openSeq, line = $('mx-moreline'); if (line) line.innerHTML = '<span class="spin"></span>';
  try {
    const j = await api(`/api/tg/history?chat=${MX.cur}&limit=50&before=${MX.msgs[0].id}${online() ? '&live=1' : ''}${MX.topic != null ? '&topic=' + MX.topic : ''}`);
    if (seq !== MX.openSeq) return;
    const have = new Set(MX.msgs.map(v => v.id));
    const add = j.messages.filter(v => !have.has(v.id));
    MX.msgs = add.concat(MX.msgs).sort((a, b) => a.id - b.id); MX.more = j.more && add.length > 0;
    MX.loadingOld = false; renderAll('keep');
  } catch (e) { toast(e.message); } finally { MX.loadingOld = false; }
}
// Плавающая дата над перепиской — одна на окно: день самого верхнего видимого сообщения. Следующий
// разделитель дня подталкивает её вверх. (Раньше «липкими» были сами разделители — и наползали друг на друга.)
let FD_RAF = 0;
function fdate() { if (!FD_RAF) FD_RAF = requestAnimationFrame(() => { FD_RAF = 0; fdateNow(); }); }
function fdateNow() {
  const box = $('mx-msgs'), fd = $('mx-fdate'); if (!box || !fd) return;
  if (MX.cur == null || !box.offsetParent) { fd.classList.remove('on'); return; }
  const top = box.getBoundingClientRect().top;
  let cur = null, next = null;
  for (const s of box.querySelectorAll('.dsep')) {
    const y = s.getBoundingClientRect().top - top;
    if (y <= 2) cur = s; else { next = s; break; }
  }
  if (!cur) { fd.classList.remove('on'); return; }
  const span = fd.firstElementChild, label = cur.textContent;
  if (span.textContent !== label) span.textContent = label;
  fd.style.top = box.offsetTop + 'px';
  fd.classList.add('on');
  const h = fd.offsetHeight || 34, ny = next ? next.getBoundingClientRect().top - top : 1e9;
  fd.style.transform = ny < h ? `translateY(${Math.round(ny - h)}px)` : '';
}
window.addEventListener('resize', () => fdate());
function renderDown() {
  fdate();
  const b = $('mx-down'); if (!b) return;
  b.classList.toggle('on', MX.cur != null && (MX.hasNewer || MX.newCount > 0 || !nearBottom()));
  b.innerHTML = '↓' + (MX.newCount ? `<b>${MX.newCount}</b>` : '');
}
function mxDown() {
  if (MX.hasNewer) return mxOpen(MX.cur, {topic: MX.topic, all: MX.topic == null});
  MX.newCount = 0; const b = $('mx-msgs'); b.scrollTop = b.scrollHeight; renderDown();
}

// ---- текст сообщения: форматирование Telegram (смещения в UTF-16 — как строки JavaScript), ссылки, номера
const NUM_RX = /\b(?:INC|INS|IM|SD|REQ|CHG|RFC|SR|PRB|PM|TASK|CRQ|WO)[-_ ]?\d{3,}\b|\b[A-Z][A-Z0-9]{1,9}-\d{1,7}\b/;
const URL_RX = /\bhttps?:\/\/[^\s<>"']*[^\s<>"'.,;:!?)\]}»]/;
const LINK_RX = new RegExp(URL_RX.source + '|' + NUM_RX.source, 'g');
const safeUrl = u => /^(https?:|tg:|mailto:)/i.test(String(u || ''));
const TG_RX = /^(?:https?:\/\/)?(?:t|telegram)\.me\/|^tg:/i;
function linkify(s) {
  let out = '', last = 0;
  for (const m of s.matchAll(LINK_RX)) {
    out += esc(s.slice(last, m.index)); const v = m[0];
    out += TG_RX.test(v) ? `<a class="tg" data-tg="${esc(v)}" title="Открыть в OpsDigest">${esc(v)}</a>`
      : /^https?:/i.test(v) ? `<a href="${esc(v)}" target="_blank" rel="noopener noreferrer">${esc(v)}</a>`
      : `<a class="num" data-q="${esc(v)}" title="Найти «${esc(v)}» во всех чатах">${esc(v)}</a>`;
    last = m.index + v.length;
  }
  return out + esc(s.slice(last));
}
function wrapEnt(e, h) {
  switch (e.t) {
    case 'b': return `<b>${h}</b>`; case 'i': return `<i>${h}</i>`; case 'u': return `<u>${h}</u>`; case 's': return `<s>${h}</s>`;
    case 'code': return `<code>${h}</code>`; case 'pre': return `<code class="pre">${h}</code>`;
    case 'sp': return `<span class="sp" title="Скрытый текст — нажмите">${h}</span>`; case 'q': return `<i class="q">${h}</i>`;
    case 'a': return TG_RX.test(e.x || '') ? `<a class="tg" data-tg="${esc(e.x)}" title="${esc(e.x)}">${h}</a>`
      : safeUrl(e.x) ? `<a href="${esc(e.x)}" target="_blank" rel="noopener noreferrer" title="${esc(e.x)}">${h}</a>` : h;
    case 'mn': return `<span class="mn">${h}</span>`;
    default: return h;
  }
}
function rich(text, ents, skip) {
  const s = skip ? text.slice(skip) : text, off = skip || 0;
  const marks = (ents || []).map(e => ({t: e[0], a: Math.max(0, e[1] - off), b: Math.min(s.length, e[1] - off + e[2]), x: e[3]})).filter(e => e.b > e.a);
  if (!marks.length) return linkify(s);
  const pts = [...new Set([0, s.length, ...marks.flatMap(e => [e.a, e.b])])].sort((a, b) => a - b);
  let html = '';
  for (let i = 0; i < pts.length - 1; i++) {
    const a = pts[i], b = pts[i + 1], act = marks.filter(e => e.a <= a && e.b >= b), raw = s.slice(a, b);
    let h = act.some(e => e.t === 'code' || e.t === 'pre' || e.t === 'a') ? esc(raw) : linkify(raw);
    for (const e of act) h = wrapEnt(e, h);
    html += h;
  }
  return html;
}
function fsize(n) { if (!n) return ''; const u = ['Б', 'КБ', 'МБ', 'ГБ']; let i = 0; while (n >= 1024 && i < 3) { n /= 1024; i++; } return (i ? n.toFixed(1) : n) + ' ' + u[i]; }
function fdur(s) { s = Math.round(s || 0); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); }
const AUTO_DL = {sticker: 600e3, voice: 3e6, video_note: 9e6, gif: 6e6, photo: 10e6, image: 10e6};
const _dlq = [], _dlSeen = new Set(); let _dlRun = 0;
function autoDl(x) {  // мелкие вложения (стикеры, голосовые, кружки, GIF) — подгружаем сами, когда видны
  if (!online() || !x || x.st === 'ok' || x.st === 'gone' || _dlSeen.has(x.id)) return;
  const lim = AUTO_DL[x.kind]; if (!lim || (x.size || 0) > lim) return;
  _dlSeen.add(x.id); _dlq.push(x.id); pumpDl();
}
async function pumpDl() {
  while (_dlRun < 2 && _dlq.length) { const id = _dlq.shift(); _dlRun++;
    api('/api/tg/download', {media: id}).then(j => { for (const v of MX.msgs) if (v.media && v.media.id === j.media.id) { v.media = j.media; rerender(v.id); } })
      .catch(() => {}).finally(() => { _dlRun--; pumpDl(); }); }
}
function mediaHTML(v) {
  const x = v.media; if (!x) return '';
  const src = `/media-file/${x.id}?t=${encodeURIComponent(T)}`;
  const ar = x.w && x.h ? `aspect-ratio:${x.w}/${x.h};` : '';
  const ok = x.st === 'ok';
  if (!ok) setTimeout(() => autoDl(x), 0);
  const info = [fsize(x.size), x.dur ? fdur(x.dur) : ''].filter(Boolean).join(' · ');
  const wait = online() ? '<span class="spin"></span>' : 'нет связи с Telegram';
  if (x.kind === 'photo' || x.kind === 'image') {
    const w = x.w && x.h ? `width:${Math.min(460, Math.round(x.w * Math.min(1, 460 / Math.max(x.h, 1))))}px;` : '';
    if (ok) return `<a class="ph" data-img="${x.id}" data-cap="${esc(x.desc || '')}" style="${w}${ar}"><img src="${src}" loading="lazy" alt=""></a>`;
    if (x.st === 'pending') return `<div class="ph wait" style="${w}${ar}">${online() ? '<span class="spin"></span>' : 'не скачано'}</div>`;
    return `<div class="ph wait" style="${w}${ar}"><button class="b" data-dl="${x.id}">⬇ Загрузить изображение</button></div>`;
  }
  if (x.kind === 'sticker') {
    const emo = (v.m && v.m.emo) || '🖼';
    if (ok && /webm/.test(x.mime || '')) return `<video class="stk-img" src="${src}" autoplay loop muted playsinline title="${esc(emo)}"></video>`;
    if (ok && !/tgsticker/.test(x.mime || '')) return `<img class="stk-img" src="${src}" alt="${esc(emo)}" title="${esc(emo)}">`;
    return `<div class="stk" title="Стикер">${esc(emo)}</div>`;
  }
  if (x.kind === 'gif') {
    if (ok) return `<video class="vid" src="${src}" autoplay loop muted playsinline style="${ar}"></video>`;
    return `<div class="vbox" data-dl="${x.id}" data-play="1"><div><big>GIF</big>${x.st === 'pending' ? wait : 'нажмите, чтобы загрузить'}<br><small>${esc(info)}</small></div></div>`;
  }
  if (x.kind === 'video_note') {
    if (ok) return `<video class="vn" src="${src}" preload="metadata" playsinline data-vn title="Нажмите, чтобы воспроизвести"></video>`;
    return `<div class="vn wait" data-dl="${x.id}" data-play="1" title="Видеосообщение ${esc(info)}">▶</div>`;
  }
  if (x.kind === 'video') {
    if (ok) return `<video class="vid" src="${src}" controls preload="metadata" style="${ar}"></video>`;
    return `<div class="vbox" data-dl="${x.id}" data-play="1"><div><big>▶</big>Видео${x.name ? ' «' + esc(x.name) + '»' : ''}<br><small>${esc(info)}${x.st === 'gone' ? ' · удалено' : ''}</small></div></div>`;
  }
  if (x.kind === 'voice' || x.kind === 'audio') {
    const title = x.kind === 'audio' ? `<div class="nm" title="${esc(x.name || '')}">${esc(x.name || 'Аудио')}</div>` : '';
    return `<div class="vp" data-vp="${x.id}" data-ok="${ok ? 1 : 0}">${ok ? `<audio src="${src}" preload="metadata"></audio>` : ''}` +
      `<button class="play" title="${ok ? 'Слушать' : 'Загрузить и слушать'}">▶</button><div style="flex:1;min-width:0">${title}` +
      `<div class="bar"><i></i></div><small><span class="cur">0:00</span> / ${x.dur ? fdur(x.dur) : '—'} <button class="sp" title="Скорость">1×</button>${x.size ? ' · ' + fsize(x.size) : ''}</small></div></div>`;
  }
  const icon = {document: '📄', image: '🖼'}[x.kind] || '📎';
  const st = x.st === 'gone' ? 'удалён' : ok ? '' : 'не скачан';
  const btn = ok ? `<span class="ic" data-fopen="${x.id}" title="Открыть" style="cursor:pointer">${icon}</span>`
    : x.st === 'gone' ? `<span class="ic" style="opacity:.5">${icon}</span>` : `<button class="ic" data-dl="${x.id}" title="Скачать">⬇</button>`;
  const acts = ok ? `<div class="acts"><a data-fopen="${x.id}">Открыть</a><a data-fsave="${x.id}">Сохранить в Загрузки</a><a href="${src}" target="_blank" rel="noopener">В браузере</a></div>` : '';
  return `<div class="doc">${btn}<div style="min-width:0"><div class="nm" title="${esc(x.name || '')}">${esc(x.name || KINDRU_MX[x.kind] || 'Файл')}</div><small>${esc([info, st].filter(Boolean).join(' · '))}</small>${acts}</div></div>`;
}
function pollHTML(v) {
  const p = v.m.poll, voted = p.opts.some(o => o.my), show = voted || p.closed || v.out;
  const tot = p.total || 0;
  let h = `<div class="poll" data-poll="${v.id}"><div class="pq">📊 ${esc(p.q)}</div><div class="pk">${p.quiz ? 'Викторина' : 'Опрос'}${p.multi ? ' · несколько ответов' : ''}${p.closed ? ' · завершён' : ''}${p.public ? '' : ' · анонимный'} · голосов: ${tot}</div>`;
  if (show) h += p.opts.map(o => { const pc = tot ? Math.round(o.n * 100 / tot) : 0;
    return `<div class="po${o.my ? ' my' : ''}"><i style="width:${pc}%"></i><span>${o.my ? '✓ ' : ''}${p.quiz && o.ok ? '✅ ' : ''}${esc(o.t)}<em>${pc}% · ${o.n}</em></span></div>`; }).join('');
  else if (p.multi) h += p.opts.map(o => `<label><input type="checkbox" value="${o.o}"> ${esc(o.t)}</label>`).join('') + `<button class="b" data-vote="${v.id}">Голосовать</button>`;
  else h += p.opts.map(o => `<button class="pv" data-vote1="${o.o}">${esc(o.t)}</button>`).join('');
  return h + '</div>';
}
async function vote(id, options) {
  if (!online()) return toast('Голосовать можно, когда есть связь с Telegram');
  try { await api('/api/tg/vote', {chat: MX.cur, id, options}); } catch (e) { toast(e.message); }
}
// голосовые и аудио: свой проигрыватель (скорость, перемотка по полосе)
let _vpCur = null;
async function vpPlay(box) {
  const id = Number(box.dataset.vp);
  if (box.dataset.ok !== '1') { box.querySelector('.play').innerHTML = '<span class="spin" style="margin:0"></span>';
    try { const j = await api('/api/tg/download', {media: id}); for (const v of MX.msgs) if (v.media && v.media.id === id) { v.media = j.media; rerender(v.id); }
      const nb = document.querySelector(`#mx-msgs [data-vp="${id}"]`); if (nb) vpPlay(nb); } catch (e) { toast(e.message); box.querySelector('.play').textContent = '▶'; }
    return; }
  const a = box.querySelector('audio'); if (!a) return;
  if (_vpCur && _vpCur !== a) { _vpCur.pause(); }
  _vpCur = a;
  if (!a._wired) { a._wired = true;
    a.addEventListener('timeupdate', () => { box.querySelector('.bar i').style.width = (a.duration ? a.currentTime * 100 / a.duration : 0) + '%'; box.querySelector('.cur').textContent = fdur(a.currentTime); });
    a.addEventListener('play', () => box.querySelector('.play').textContent = '❚❚'); a.addEventListener('pause', () => box.querySelector('.play').textContent = '▶');
    a.addEventListener('ended', () => { box.querySelector('.play').textContent = '▶'; vpNext(box); }); }
  if (a.paused) a.play().catch(e => toast('Не воспроизводится: ' + e.message)); else a.pause();
}
function vpNext(box) {  // следующее голосовое — как в Telegram
  const all = [...document.querySelectorAll('#mx-msgs [data-vp]')]; const i = all.indexOf(box);
  const n = all[i + 1]; if (n && !n.querySelector('.nm')) vpPlay(n);
}

// ================================================================ поле ввода
function renderCompose() {
  const on = online(), box = $('mx-in');
  if (!box) return;
  $('mx-off').classList.toggle('on', MX.cur != null && !on);
  $('mx-off').textContent = MX.status.state === 'auth' ? 'Нужен вход в Telegram — отправка недоступна.' : 'Нет связи с Telegram — переписка из базы, отправка недоступна.';
  $('mx-send').disabled = !on;
  box.placeholder = on ? 'Сообщение' + (MX.chat ? '' : '') : 'Нет связи с Telegram';
}
function renderBars() {
  const b = $('mx-bar'), x = MX.edit || MX.reply;
  b.classList.toggle('on', !!x);
  if (!x) return;
  b.innerHTML = `<span style="color:var(--tg);font-size:18px">${MX.edit ? '✎' : '↩'}</span><div class="q"><b>${MX.edit ? 'Изменение сообщения' : 'Ответ ' + esc(x.out ? 'себе' : x.from || '')}</b><span>${esc(snippetMx(x))}</span></div>` +
    `<button class="x" onclick="mxCancel()" title="Отменить (Esc)">×</button>`;
}
function mxCancel() { if (MX.edit) $('mx-in').value = ''; MX.reply = null; MX.edit = null; renderBars(); autosize(); }
function mxReply(id) { const v = MX.msgs.find(x => x.id === id); if (!v) return; MX.edit = null; MX.reply = v; renderBars(); $('mx-in').focus(); }
function mxEdit(id) {
  const v = MX.msgs.find(x => x.id === id); if (!v) return;
  MX.reply = null; MX.edit = v; const meta = v.m || {};
  $('mx-in').value = (v.text || '').slice(v.media || meta.fw ? (meta.p || 0) : 0); renderBars(); autosize(); $('mx-in').focus();
}
function autosize() { const t = $('mx-in'); t.style.height = 'auto'; t.style.height = Math.min(220, t.scrollHeight) + 'px'; }
function renderAtt() {
  const a = $('mx-att'); a.classList.toggle('on', MX.files.length > 0);
  a.innerHTML = MX.files.map((f, i) => `<div class="f">${f.url ? `<img src="${f.url}" alt="">` : '📄'}<span>${esc(f.file.name)} · ${fsize(f.file.size)}</span><button onclick="mxUnattach(${i})">×</button></div>`).join('');
}
function mxAttach(list) {
  for (const file of list) {
    if (!file) continue;
    const isImg = /^image\/(png|jpe?g|webp|gif|bmp)$/.test(file.type);
    MX.files.push({file, url: isImg ? URL.createObjectURL(file) : null, photo: isImg && file.type !== 'image/gif'});
  }
  renderAtt(); $('mx-in').focus();
}
function mxUnattach(i) { const f = MX.files.splice(i, 1)[0]; if (f && f.url) URL.revokeObjectURL(f.url); renderAtt(); }
async function upload(chat, f, caption, reply, opt) {
  opt = opt || {};
  const name = f.file.name || 'file';
  const qs = `chat=${chat}&name=${encodeURIComponent(name)}&caption=${encodeURIComponent(caption || '')}` + (reply ? `&reply=${reply}` : '') + (f.photo ? '' : '&doc=1') +
    (opt.topic != null ? `&topic=${opt.topic}` : '') + (opt.silent ? '&silent=1' : '');
  const r = await fetch('/api/tg/upload?' + qs, {method: 'POST', headers: {'X-Token': T, 'Content-Type': 'application/octet-stream'}, body: f.file});
  const j = await r.json().catch(() => ({ok: false, error: 'ошибка ' + r.status}));
  if (!j.ok) throw new Error(j.error || 'файл не отправлен');
}
async function mxSend(opt) {
  opt = opt || {};
  const ta = $('mx-in'), text = ta.value, chat = MX.cur, topic = MX.topic;
  if (chat == null || (!text.trim() && !MX.files.length) || !online()) return;
  const btn = $('mx-send'); btn.disabled = true;
  try {
    if (MX.edit) { await api('/api/tg/edit', {chat, id: MX.edit.id, text}); ta.value = ''; MX.edit = null; renderBars(); autosize(); return; }
    const reply = MX.reply ? MX.reply.id : null, files = MX.files.slice();
    if (opt.schedule && files.length) return toast('Отложенная отправка — только для текста');
    ta.value = ''; MX.reply = null; MX.files = []; renderBars(); renderAtt(); autosize(); delete MX.drafts[draftKey()]; lsSet('drafts', MX.drafts);
    try {
      if (files.length) { for (let i = 0; i < files.length; i++) await upload(chat, files[i], i === 0 ? text : '', i === 0 ? reply : null, {topic, silent: opt.silent}); }
      else { const r = await api('/api/tg/send', {chat, text, reply_to: reply, topic, silent: !!opt.silent, schedule: opt.schedule || null});
        if (r.scheduled) toast('Запланировано: ' + new Date(opt.schedule).toLocaleString('ru-RU')); }
      files.forEach(f => f.url && URL.revokeObjectURL(f.url));
    } catch (e) { if (!ta.value) ta.value = text; MX.files = files; renderAtt(); autosize(); throw e; }
    const b = $('mx-msgs'); b.scrollTop = b.scrollHeight;
  } catch (e) { toast('Не отправлено: ' + e.message); } finally { btn.disabled = !online(); }
}
async function mxRead(maxId) {
  try {
    if (MX.topic != null) { await api('/api/tg/read_topic', {chat: MX.cur, topic: MX.topic, max_id: maxId || null});
      const t = MX.topics.find(x => x.topic_id === MX.topic); if (t) t.unread = 0; renderHead(); }
    else await api('/api/tg/read', {chat: MX.cur, max_id: maxId || null});
    MX.unreadFrom = null; const u = $('mx-unr'); if (u) u.remove();
  } catch (e) { toast(e.message); }
}
async function mxMute(on) {
  try { await api('/api/tg/mute', {chat: MX.cur, on, topic: MX.topic != null ? MX.topic : null});
    if (MX.topic != null) { const t = MX.topics.find(x => x.topic_id === MX.topic); if (t) t.muted = on; } else if (MX.chat) MX.chat.muted = on;
    renderHead(); renderList(); toast(on ? 'Без звука' : 'Уведомления включены');
  } catch (e) { toast(e.message); }
}
function mxInsert(text, replyId) {
  const ta = $('mx-in'); ta.value = text; if (replyId) { const v = MX.msgs.find(x => x.id === replyId); if (v) { MX.reply = v; MX.edit = null; renderBars(); } }
  autosize(); ta.focus();
}

// ================================================================ меню сообщения, выделение текста
function msgById(id) { return MX.msgs.find(x => x.id === Number(id)); }
function hasMaverick() { return ((CFG.media || {}).profiles || []).includes('maverick'); }
function mxMenu(x, y, id) {
  const v = msgById(id); if (!v) return;
  MX.menu = v; const own = v.out, txt = textOf(v) !== '';
  const b = (act, label, cls) => `<button data-m="${act}" class="${cls || ''}">${label}</button>`;
  let h = online() ? `<div class="emo">${EMO.map(e => `<button data-emo="${e}">${e}</button>`).join('')}</div><hr>` : '';
  h += b('reply', '↩ Ответить') + (txt ? b('copy', '⧉ Копировать текст') : '') + b('fwd', '➦ Переслать') +
    (own && txt ? b('edit', '✎ Изменить') : '') + b('readto', '✓ Прочитано до этого сообщения') +
    b('del', '🗑 Удалить', 'bad') + '<hr>' +
    b('ai-ask', '✦ Спросить ИИ об этом сообщении', 'ai') + b('ai-draft', '✦ Черновик ответа', 'ai') +
    b('ai-explain', '✦ Объяснить: что происходит, термины, к кому', 'ai') +
    b('ai-all', '🔎 Найти везде: все чаты и источники', 'ai') + b('ai-pick', '🔎 Найти в выбранных источниках…', 'ai') + (uiMods().asst ? b('ai-asst', '⤢ Спросить в ассистенте (полный экран)', 'ai') : '');
  const m = $('mx-menu'); m.innerHTML = h; m.classList.add('on');
  const w = m.offsetWidth, hh = m.offsetHeight;
  m.style.left = Math.min(x, innerWidth - w - 8) + 'px'; m.style.top = Math.max(8, Math.min(y, innerHeight - hh - 8)) + 'px';
}
function closeMenus() { $('mx-menu').classList.remove('on'); $('mx-selbar').classList.remove('on'); }
async function menuAct(act, emo) {
  const v = MX.menu; closeMenus(); if (!v) return;
  const plain = (v.text || '').slice((v.media || (v.m && v.m.fw)) ? ((v.m && v.m.p) || 0) : 0);
  try {
    if (emo) { const mine = ((v.m && v.m.rx) || []).find(r => r[0] === emo && r[2]); await api('/api/tg/react', {chat: v.chat, id: v.id, emoji: mine ? null : emo}); return; }
    switch (act) {
      case 'reply': return mxReply(v.id);
      case 'copy': await navigator.clipboard.writeText(plain); return toast('Скопировано');
      case 'fwd': return fwdOpen([v.id]);
      case 'edit': return mxEdit(v.id);
      case 'readto': return mxRead(v.id);
      case 'del': {
        const all = v.out || MX.chat.kind === 'private' ? confirm(v.out ? 'Удалить сообщение у всех?' : 'Удалить сообщение у вас и у собеседника?\n«Отмена» — не удалять.') : confirm('Удалить сообщение? (в группе — нужны права администратора)');
        if (!all) return;
        await api('/api/tg/delete', {chat: v.chat, ids: [v.id], revoke: true}); return;
      }
      case 'ai-ask': MX.aiQuote = {text: plain, id: v.id}; aiOpen(); renderAi(); $('ai-q').focus(); return;
      case 'ai-draft': return aiDraft(v.id);
      case 'ai-rel': return aiRelated(plain || snippetMx(v), v.id);
      case 'ai-all': return aiEverywhere(plain || snippetMx(v), v.id, srcAll());
      case 'ai-pick': { const r = (document.querySelector(`#mx-msgs .mr[data-id="${v.id}"]`) || document.body).getBoundingClientRect();
        return srcPick({left: r.left + 40, bottom: Math.min(r.bottom, innerHeight - 320)}, 'Найти сообщение', src => aiEverywhere(plain || snippetMx(v), v.id, src)); }
      case 'ai-asst': return asFromSelection(plain || snippetMx(v), v.id);
      case 'ai-mav': return aiMaverick(plain || snippetMx(v), v.id);
      case 'ai-explain': return aiExplain(plain, v.id);
    }
  } catch (e) { toast(e.message); }
}
function selInfo() {
  const s = getSelection(); const t = s ? s.toString().trim() : '';
  if (!t || t.length < 2 || !s.rangeCount) return null;
  const n = s.anchorNode && (s.anchorNode.nodeType === 1 ? s.anchorNode : s.anchorNode.parentElement);
  const mr = n && n.closest && n.closest('#mx-msgs .mr');
  if (!mr) return null;
  return {text: t, id: Number(mr.dataset.id), rect: s.getRangeAt(0).getBoundingClientRect()};
}
function showSelBar() {
  const si = selInfo(), bar = $('mx-selbar');
  if (!si) { bar.classList.remove('on'); return; }
  MX.sel = si;
  bar.innerHTML = `<button data-s="quote">↩ Ответить с цитатой</button><button data-s="copy">⧉</button>` +
    `<button data-s="explain" class="ai">✦ Объяснить</button><button data-s="gl" title="Добавить в глоссарий">📖</button>` +
    `<button data-s="all" class="ai" title="Все чаты (включая этот) и все подключённые источники">🔎 Найти везде</button><button data-s="pick" class="ai" data-srcpick="1" title="Выбрать, где искать: этот чат, все чаты, почта, ITSM, вики, Rooms, трекер, встречи">▾</button><button data-s="ask" class="ai" title="Ответ по выбранным источникам">✦ Спросить ИИ</button>` +
    (uiMods().asst ? `<button data-s="asst" class="ai" title="Открыть вопрос по выделенному в ассистенте — на весь экран">⤢</button>` : '');
  bar.classList.add('on');
  const w = bar.offsetWidth;
  bar.style.left = Math.max(8, Math.min(si.rect.left + si.rect.width / 2 - w / 2, innerWidth - w - 8)) + 'px';
  bar.style.top = Math.max(8, si.rect.top - 44) + 'px';
}
async function selAct(a) {
  const s = MX.sel; closeMenus(); if (!s) return;
  if (a === 'quote') { mxReply(s.id); const ta = $('mx-in'); ta.value = `«${s.text}»\n` + ta.value; autosize(); }
  else if (a === 'copy') { await navigator.clipboard.writeText(s.text); toast('Скопировано'); }
  else if (a === 'rel') aiRelated(s.text, s.id);
  else if (a === 'all') aiEverywhere(s.text, s.id, srcAll());
  else if (a === 'pick') { srcPick(s.rect, 'Найти выделенное', src => aiEverywhere(s.text, s.id, src)); return; }
  else if (a === 'asst') asFromSelection(s.text, s.id);
  else if (a === 'ask') { MX.aiQuote = {text: s.text, id: s.id}; aiOpen(); renderAi(); $('ai-q').focus(); }
  else if (a === 'mav') aiMaverick(s.text, s.id);
  else if (a === 'explain') aiExplain(s.text, s.id);
  else if (a === 'gl') glQuick(s.text.slice(0, 80), false, [MX.cur, s.id]);
  getSelection().removeAllRanges();
}

// ================================================================ пересылка, поиск в чате, настройки чата, просмотр фото
let FWD = [];
function fwdOpen(ids) { FWD = ids; $('fwd-q').value = ''; fwdList(); $('mx-fwd').classList.add('on'); setTimeout(() => $('fwd-q').focus(), 50); }
function fwdList() {
  const q = $('fwd-q').value.toLowerCase();
  const list = MX.chats.filter(c => !c.archived && (!q || (c.title || '').toLowerCase().includes(q))).slice(0, 60);
  $('fwd-list').innerHTML = list.map(c => `<div class="ci" data-to="${c.id}">${avatar(c, true)}<div class="ci-body"><div class="ci-title">${esc(c.title)}</div></div></div>`).join('') || '<div class="mx-hint">Не найдено</div>';
}
async function fwdTo(to) {
  $('mx-fwd').classList.remove('on');
  try { await api('/api/tg/forward', {chat: MX.cur, ids: FWD, to: Number(to)}); toast('Переслано: ' + ((MX.byId[to] || {}).title || '')); }
  catch (e) { toast(e.message); }
}
function mxSearchChat() { const b = $('mx-sbar'); b.classList.toggle('on'); $('mx-sres').classList.remove('on');
  if (b.classList.contains('on')) { $('mx-sq').focus(); if ($('mx-sfrom').options.length <= 1) api(`/api/chat/members?chat=${MX.cur}`).then(j => {
    $('mx-sfrom').innerHTML = '<option value="">все авторы</option>' + j.members.filter(m => m.id).map(m => `<option value="${m.id}">${esc(m.name)}</option>`).join(''); }).catch(() => {}); } }
async function mxSearchIn() {
  const q = $('mx-sq').value.trim(), frm = $('mx-sfrom').value; if (!q && !frm) return;
  const box = $('mx-sres'); box.classList.add('on'); box.innerHTML = '<div class="mx-hint"><span class="spin"></span>Ищу…</div>';
  try { const j = await api(`/api/tg/search?chat=${MX.cur}&q=` + encodeURIComponent(q) + (frm ? '&from=' + frm : ''));
    box.innerHTML = j.results.length ? j.results.map(v => `<div class="sr" data-chat="${v.chat}" data-msg="${v.id}"><div class="ci-row"><span class="ci-title">${esc(v.out ? 'Вы' : v.from)}</span><span class="ci-time">${listTime(v.date)}</span></div><div class="ci-sub">${hl(snippetMx(v), q)}</div></div>`).join('')
      : '<div class="mx-hint">Ничего не найдено</div>';
  } catch (e) { box.innerHTML = `<div class="mx-hint">${esc(e.message)}</div>`; }
}
async function mxChatSettings(tab) {
  if (MX.cur == null) return;
  try { chInit(); await listChats(true); if (CH.byId[MX.cur]) chOpen(MX.cur, tab || (MX.topic != null ? 'topics' : 'desc')); else toast('Чат ещё не в базе'); }
  catch (e) { toast(e.message); }
}
async function mxDownload(mid, btn) {
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin"></span>'; }
  try { const j = await api('/api/tg/download', {media: mid}); for (const v of MX.msgs) if (v.media && v.media.id === j.media.id) { v.media = j.media; rerender(v.id); } }
  catch (e) { toast(e.message); if (btn) { btn.disabled = false; btn.textContent = '⬇'; } }
}
function viewImg(id, cap) {
  const ids = MX.msgs.filter(v => v.media && (v.media.kind === 'photo' || v.media.kind === 'image') && v.media.st === 'ok').map(v => v.media.id);
  MX.gal = {ids: ids.includes(Number(id)) ? ids : [Number(id)], i: Math.max(0, ids.indexOf(Number(id)))}; galShow();
}
function galShow() {
  const g = MX.gal; const id = g.ids[g.i]; const v = MX.msgs.find(x => x.media && x.media.id === id);
  const cap = v ? (v.media.desc || '') : '';
  $('mx-view').innerHTML = `<div class="vbar"><button data-g="open">Открыть</button><button data-g="save">Сохранить в Загрузки</button>${v ? '<button data-g="goto">К сообщению</button>' : ''}<button data-g="close">✕</button></div>` +
    (g.ids.length > 1 ? `<button class="nav l" data-g="prev">‹</button><button class="nav r" data-g="next">›</button>` : '') +
    `<div><img src="/media-file/${id}?t=${encodeURIComponent(T)}" alt="" data-g="zoom"><div>${esc(cap)}${g.ids.length > 1 ? ` · ${g.i + 1} из ${g.ids.length}` : ''}</div><div id="mx-view-gl" class="hint"></div></div>`;
  $('mx-view').classList.add('on');
  api('/api/glossary/for_media?id=' + id).then(j => { const el = $('mx-view-gl'); if (el && j.terms && j.terms.length) el.innerHTML = '📖 ' + j.terms.map(t => `<a class="lnk" style="color:#9cd2ff;cursor:pointer" data-g="gl" data-term="${esc(t.term)}" title="${esc(t.summary || '')}">${esc(t.term)}</a>`).join(', '); }).catch(() => {});
}
function galAct(act, el) {
  const g = MX.gal; if (!g) return; const id = g.ids[g.i];
  if (act === 'prev') { g.i = (g.i - 1 + g.ids.length) % g.ids.length; return galShow(); }
  if (act === 'next') { g.i = (g.i + 1) % g.ids.length; return galShow(); }
  if (act === 'zoom') return el.classList.toggle('full');
  if (act === 'open' || act === 'save') return fileAct(id, act);
  if (act === 'goto') { const v = MX.msgs.find(x => x.media && x.media.id === id); $('mx-view').classList.remove('on'); if (v) jumpTo(v.id); return; }
  if (act === 'gl') { $('mx-view').classList.remove('on'); return glQuick(el.dataset.term, true); }
  $('mx-view').classList.remove('on');
}
async function fileAct(id, action) {
  try { const j = await api('/api/tg/file', {media: id, action}); toast(action === 'save' ? 'Сохранено: ' + j.path : 'Открываю: ' + j.path.split(/[\\/]/).pop()); }
  catch (e) { toast(e.message); }
}
let _toastT = null;
function toast(t) { const el = $('mx-toast'); el.textContent = t; el.classList.add('on'); clearTimeout(_toastT); _toastT = setTimeout(() => el.classList.remove('on'), 4200); }

// ================================================================ ИИ в чате
const PROV_RU = {local: 'Локальная', maverick: 'Maverick', claude: 'Claude', claude_code: 'Claude Code'};
function aiOpen() { MX.aiOn = true; MX.infOn = false; $('mx-info').classList.remove('on'); $('mx-ai').classList.add('on'); $('mx-split2').classList.add('on'); renderHead(); }
function aiToggle() { MX.aiOn = !MX.aiOn; if (MX.aiOn) { MX.infOn = false; $('mx-info').classList.remove('on'); }
  $('mx-ai').classList.toggle('on', MX.aiOn); $('mx-split2').classList.toggle('on', MX.aiOn || MX.infOn); renderHead(); if (MX.aiOn) renderAi(); }
function renderAi() {
  const sel = $('ai-prov'), profs = (CFG.media || {}).profiles || [];
  const cur = MX.provider || (CFG.llm || {}).provider || profs[0] || '';
  sel.innerHTML = profs.map(p => `<option value="${esc(p)}"${p === cur ? ' selected' : ''}>${esc(PROV_RU[p] || p)}</option>`).join('');
  $('ai-quote').style.display = MX.aiQuote ? '' : 'none';
  if (MX.aiQuote) $('ai-quote').innerHTML = `<div class="q" style="flex:1">${esc(MX.aiQuote.text)}</div><button class="x" onclick="MX.aiQuote=null;renderAi()">×</button>`;
  $('ai-chat').textContent = MX.chat ? '«' + (MX.chat.title || '') + '»' : '';
  if ($('ai-src-lbl')) $('ai-src-lbl').textContent = srcLabel(aiSources());
  document.querySelectorAll('.mav-only').forEach(x => { x.style.display = hasMaverick() ? '' : 'none'; });
  const cards = MX.ai.filter(c => c.chat === MX.cur || c.chat == null);
  $('ai-cards').innerHTML = cards.length ? cards.map(aiCardHTML).join('') : '<div class="mx-hint">Выберите период для сводки, задайте вопрос или выделите текст в переписке — появится меню ✦.</div>';
}
function aiCardHTML(c) {
  let body = '';
  if (c.status === 'run') body = `<div class="prog"><span class="spin"></span>${esc(c.prog || 'Думаю…')}</div>`;
  else if (c.status === 'err') body = `<div class="md">✗ ${esc(c.error)}</div>`;
  else if (c.kind === 'related') body = relatedHTML(c);
  else if (c.kind === 'everywhere') body = everywhereHTML(c);
  else body = `<div class="md">${mdLite(c.text, c)}</div>`;
  let acts = '';
  if (c.status === 'done') {
    if (c.kind === 'draft' && c.text) acts += `<button data-ai="insert" data-n="${c.n}">Вставить в поле ввода</button>`;
    if (c.text) acts += `<button data-ai="copy" data-n="${c.n}">Копировать</button>`;
    if (c.kind === 'related' && c.found && c.found.total) acts += `<button data-ai="analyze" data-n="${c.n}">✦ Разобрать моделью</button>`;
    if (c.kind === 'related' && hasMaverick()) acts += `<button data-ai="mav" data-n="${c.n}">✦ Спросить Maverick</button>`;
    if (c.kind === 'explain') acts += `<button data-ai="gl" data-n="${c.n}" title="Записать термин в глоссарий">📖 В глоссарий</button>`;
    if (c.thread && uiMods().asst) acts += `<button data-ai="asst" data-n="${c.n}" title="Продолжить на весь экран — в разделе «Ассистент»">⤢ В ассистенте</button>`;
    else if (c.kind === 'everywhere' && uiMods().asst) acts += `<button data-ai="asst-new" data-n="${c.n}">⤢ Спросить в ассистенте</button>`;
  }
  if (c.status === 'done' && (c.ext || []).length) body += extHTML(c.ext);
  if (c.status === 'done' && c.offline && Object.keys(c.offline).filter(k => k !== 'telegram').length)
    body += `<div class="prog" style="color:var(--warn)">Не ответили: ${Object.keys(c.offline).filter(k => k !== 'telegram').map(k => SRC_RU[k] || k).join(', ')} — переподключите плагин в чате Maverick.</div>`;
  if (c.status === 'done' && typeof fbButtons === 'function')
    acts += fbButtons(c.kind === 'everywhere' ? 'search' : 'chat_ai', () => (c.quote ? 'Запрос: ' + c.quote + '\n\n' : '') +
      (c.title ? c.title + '\n' : '') + (c.text || JSON.stringify(c.found || {}).slice(0, 3000)), {chat: c.chat, title: (MX.chat || {}).title});
  acts += `<button data-ai="close" data-n="${c.n}" style="margin-left:auto;color:var(--mx-muted)">×</button>`;
  return `<div class="card ai${c.status === 'err' ? ' err' : ''}"><div class="h"><b>${esc(c.title)}</b><span>${esc(c.model || '')}</span></div>` +
    (c.quote ? `<div class="q">${esc(c.quote)}</div>` : '') + body + `<div class="acts">${acts}</div></div>`;
}
function relatedHTML(c) {
  const f = c.found || {};
  if (!f.groups || !f.groups.length) return `<div class="md">В других чатах (в базе) ничего похожего не нашлось.${f.terms && f.terms.length ? ' Искал: ' + esc(f.terms.join(', ')) : ''}</div>`;
  return `<div class="prog">Искал: ${esc((f.terms || []).join(', '))}</div>` + f.groups.map(g => `<div class="rel-g"><b>${esc(g.title)}</b>` +
    g.items.map(i => `<div class="rel-i" data-chat="${g.chat}" data-msg="${i.id}"><small>${listTime(i.date)} · ${esc(i.from || '')}</small><br>${esc(i.text)}</div>`).join('') + '</div>').join('');
}
function extHTML(list) {
  return '<div class="as-ext">' + list.map(e => `<div class="ex"><span class="src ${esc(e.source)}">${esc(SRC_RU[e.source] || e.source)}</span> ` +
    (e.url ? `<a href="${esc(e.url)}" target="_blank" rel="noopener"><b>${esc(e.title || e.ref)}</b></a>` : `<b>${esc(e.source === 'itsm' ? e.ref : (e.title || e.ref))}</b>`) +
    (e.source === 'itsm' && e.title ? ' ' + esc(e.title) : '') + (e.status ? ` <span class="pill">${esc(e.status)}</span>` : '') +
    (e.date ? ` <small>${esc(e.date)}</small>` : '') + (e.summary ? `<div class="hint">${esc(e.summary)}</div>` : '') + '</div>').join('') + '</div>';
}
function everywhereHTML(c) {
  const f = c.found || {};
  const tg = f.telegram || {};
  let h = relatedHTML({found: tg}).replace('В других чатах (в базе) ничего похожего не нашлось.', 'В Telegram (в базе) ничего похожего не нашлось.');
  if (f.sources && !f.sources.includes('telegram')) h = '';
  const ext = SRC_KEYS.flatMap(k => f[k] || []);
  if (ext.length) h += `<div class="prog" style="margin-top:6px">${esc(SRC_KEYS.filter(k => (f[k] || []).length).map(k => SRC_RU[k]).join(', '))}:</div>` + extHTML(ext);
  else if (f.sources && f.sources.some(k => k !== 'telegram')) h += '<div class="prog" style="margin-top:6px">В выбранных источниках ничего не нашлось.</div>';
  if ((f.saved || []).length) h += '<div class="prog" style="margin-top:6px">Найденное раньше:</div>' + extHTML(f.saved);
  const off = Object.keys(f.offline || {});
  if (off.length) h += `<div class="prog" style="color:var(--warn)">Не ответили: ${off.map(k => SRC_RU[k] || k).join(', ')} — переподключите плагин в чате Maverick.</div>`;
  return h;
}
// номер инцидента/проблемы/изменения → карточка из ITSM (через плагин Maverick; из кеша, если запрашивали недавно)
const ITSM_RX = /^(?:INC|INS|IM|SD|REQ|RITM|CHG|RFC|CRQ|PRB|PM)[-_ ]?\d{4,}$/i;
async function aiItsm(number) {
  const card = {n: ++MX.aiN, chat: MX.cur, kind: 'itsm', status: 'run', prog: 'Спрашиваю ITSM…', title: '🎫 ITSM: ' + number};
  MX.ai.unshift(card); aiOpen(); renderAi();
  try {
    const {job} = await api('/api/plugins/itsm', {numbers: [number]});
    while (true) {
      const j = await api('/api/jobs/' + job);
      if (j.log.length) { card.prog = j.log[j.log.length - 1]; renderAi(); }
      if (j.status !== 'running') {
        if (j.status === 'error') Object.assign(card, {status: 'err', error: j.error});
        else { const it = (j.result.items || [])[0];
          Object.assign(card, {status: 'done', text: it && it.found ? itsmText(it.data) : `В ITSM запись ${number} не найдена.`}); }
        break;
      }
      await sleep(900);
    }
  } catch (e) { Object.assign(card, {status: 'err', error: e.message}); }
  renderAi();
}
function itsmText(d) {
  const f = [['Статус', d.status], ['Приоритет', d.priority], ['Сервис', d.service], ['Группа', d.group], ['Исполнитель', d.assignee],
    ['Открыт', d.opened], ['Обновлён', d.updated], ['Решение', d.resolution], ['Связано', (d.related || []).join(', ')], ['Последняя запись', d.last_note]];
  return `### ${d.number || ''} ${d.title || ''}\n` + (d.description ? d.description + '\n' : '') + f.filter(x => x[1]).map(x => `- **${x[0]}:** ${x[1]}`).join('\n');
}
// источники поиска по выделенному: «везде» или выбранные (запоминаются); Telegram — все чаты, только этот или нет
function srcAvail() { const d = (typeof SRC !== 'undefined' && SRC.data) || null;
  return SRC_KEYS.filter(k => d && d.available && d.status[k] && d.status[k].enabled); }
function srcAll() { const o = {telegram: true}; srcAvail().forEach(k => { o[k] = true; }); return o; }
function aiSources() { let s = null; try { s = JSON.parse(localStorage.getItem('od_mx_src') || 'null'); } catch (e) {} return s || srcAll(); }
function srcLabel(s) { const tg = s.telegram === 'this' ? 'этот чат' : s.telegram ? 'все чаты' : '';
  const ext = SRC_KEYS.filter(k => s[k]).map(k => SRC_RU[k]);
  return [tg, ...ext].filter(Boolean).join(', ') || 'нигде'; }
function srcPick(rect, title, onGo) {
  const el = $('mx-srcpick'); const cur = aiSources(), av = srcAvail();
  el.innerHTML = `<b>${esc(title)}</b><div class="hint" style="margin:2px 0 6px">Где искать</div>` +
    `<label><input type="radio" name="sp-tg" value="all"${cur.telegram === true ? ' checked' : ''}> Telegram — все чаты</label>` +
    `<label><input type="radio" name="sp-tg" value="this"${cur.telegram === 'this' ? ' checked' : ''}> Telegram — только этот чат</label>` +
    `<label><input type="radio" name="sp-tg" value="no"${!cur.telegram ? ' checked' : ''}> не искать в Telegram</label><hr>` +
    SRC_KEYS.map(k => `<label style="${av.includes(k) ? '' : 'opacity:.45'}"><input type="checkbox" data-sp="${k}"${cur[k] && av.includes(k) ? ' checked' : ''}${av.includes(k) ? '' : ' disabled'}> ${SRC_IC[k]} ${esc(SRC_RU[k])}</label>`).join('') +
    `<div class="acts"><button class="b p" data-go="sel">Искать</button><button class="b" data-go="all">Везде</button><button class="b" data-go="tg">Только Telegram</button></div>`;
  el.classList.add('on');
  const w = el.offsetWidth, h = el.offsetHeight;
  el.style.left = Math.max(8, Math.min(rect.left, innerWidth - w - 8)) + 'px';
  el.style.top = Math.max(8, Math.min(rect.bottom + 6, innerHeight - h - 8)) + 'px';
  el.onclick = e => {
    const b = e.target.closest('[data-go]'); if (!b) return;
    let s;
    if (b.dataset.go === 'all') s = srcAll();
    else if (b.dataset.go === 'tg') s = {telegram: true};
    else { const tg = (el.querySelector('[name=sp-tg]:checked') || {}).value || 'all';
      s = {telegram: tg === 'all' ? true : tg === 'this' ? 'this' : false};
      el.querySelectorAll('[data-sp]').forEach(x => { s[x.dataset.sp] = x.checked && !x.disabled; }); }
    try { localStorage.setItem('od_mx_src', JSON.stringify(s)); } catch (e2) {}
    el.classList.remove('on'); onGo(s);
  };
}
document.addEventListener('mousedown', e => { if (!e.target.closest('#mx-srcpick') && !e.target.closest('[data-srcpick]')) { const el = $('mx-srcpick'); if (el) el.classList.remove('on'); } });
function aiEverywhere(text, id, sources) {
  const s = sources || srcAll();
  aiRun('everywhere', {text, ids: id ? [id] : [], sources: s}, {title: '🔎 Поиск: ' + srcLabel(s), quote: text, src: {text, id}});
}
function mdLite(t, c) {
  const refs = {}; (c.refs || []).forEach(r => { if (typeof r === 'object') refs[r.id] = r.chat; });
  const inline = s => esc(s).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/#(\d{1,9})\b/g, (m, id) => `<a class="ref" data-chat="${refs[id] != null ? refs[id] : (c.chat != null ? c.chat : '')}" data-msg="${id}">#${id}</a>`);
  let out = '', inList = false;
  for (const line of String(t || '').split('\n')) {
    const l = line.trim();
    if (/^[-*•]\s+/.test(l)) { if (!inList) { out += '<ul>'; inList = true; } out += '<li>' + inline(l.replace(/^[-*•]\s+/, '')) + '</li>'; continue; }
    if (inList) { out += '</ul>'; inList = false; }
    if (/^#{1,4}\s+/.test(l)) out += '<h4>' + inline(l.replace(/^#{1,4}\s+/, '')) + '</h4>';
    else if (l) out += '<p>' + inline(l) + '</p>';
  }
  return out + (inList ? '</ul>' : '');
}
async function aiRun(kind, body, card) {
  card = Object.assign({n: ++MX.aiN, chat: MX.cur, kind, status: 'run', prog: 'Отправляю…'}, card);
  MX.ai.unshift(card); aiOpen(); renderAi();
  try {
    const {job} = await api('/api/ai/' + kind, Object.assign({chat: MX.cur, topic: MX.topic, provider: $('ai-prov').value || null}, body));
    let since = 0;
    while (true) {
      const j = await api(`/api/jobs/${job}?since=${since}`); since = j.log_total;
      if (j.log.length) { card.prog = j.log[j.log.length - 1]; renderAi(); }
      if (j.status !== 'running') {
        if (j.status === 'error') Object.assign(card, {status: 'err', error: j.error});
        else Object.assign(card, {status: 'done', text: j.result.text, refs: j.result.refs, model: j.result.model || card.model,
          found: kind === 'everywhere' ? j.result : j.result.found, thread: j.result.thread, ext: j.result.ext, offline: j.result.offline});
        break;
      }
      await sleep(900);
    }
  } catch (e) { Object.assign(card, {status: 'err', error: e.message}); }
  renderAi();
  return card;
}
const PERIOD_RU = {unread: 'непрочитанное', today: 'сегодня', '3h': '3 часа', day: '24 часа', '3d': '3 дня', week: 'неделя', month: 'месяц'};
function aiSummary(period) {
  const body = {period};
  if (period === 'custom') { body.since = $('ai-since').value; if (!body.since) return toast('Выберите дату'); }
  aiRun('summary', body, {title: 'Сводка чата · ' + (period === 'custom' ? 'с ' + body.since.split('-').reverse().join('.') : PERIOD_RU[period])});
}
function aiAsk() {
  const q = $('ai-q').value.trim(); if (!q) return $('ai-q').focus();
  const quote = MX.aiQuote; $('ai-q').value = ''; MX.aiQuote = null;
  const src = aiSources();
  aiRun('ask', {question: q, text: quote ? quote.text : '', ids: quote ? [quote.id] : [], sources: src},
    {title: q + (quote ? ' · ' + srcLabel(src) : ''), quote: quote ? quote.text : ''});
}
function aiDraft(id) {
  const v = id ? msgById(id) : null;
  aiRun('draft', {ids: id ? [id] : [], hint: $('ai-hint') ? $('ai-hint').value : ''}, {title: 'Черновик ответа', quote: v ? snippetMx(v) : '', reply: id || null});
}
async function aiRelated(text, id) {
  const card = {n: ++MX.aiN, chat: MX.cur, kind: 'related', status: 'run', prog: 'Ищу в базе…', title: 'Связанное в других чатах', quote: text, src: {text, id}};
  MX.ai.unshift(card); aiOpen(); renderAi();
  try { const j = await api('/api/ai/related', {chat: MX.cur, text, id}); Object.assign(card, {status: 'done', found: j, text: ''}); }
  catch (e) { Object.assign(card, {status: 'err', error: e.message}); }
  renderAi();
}
function aiExplain(text, id) { aiRun('explain', {text: text || '', ids: id ? [id] : []}, {title: '✦ Объяснить', quote: text || (id ? snippetMx(msgById(id) || {}) : '')}); }
function aiMaverick(text, id) { aiRun('maverick', {text, ids: id ? [id] : []}, {title: 'Maverick: почта, ITSM, Confluence', quote: text}); }
async function aiCardAct(act, n) {
  const c = MX.ai.find(x => x.n === Number(n)); if (!c) return;
  if (act === 'close') { MX.ai = MX.ai.filter(x => x !== c); renderAi(); }
  else if (act === 'gl') glQuick(c.quote ? c.quote.slice(0, 80) : '', false);
  else if (act === 'copy') { await navigator.clipboard.writeText(c.text || ''); toast('Скопировано'); }
  else if (act === 'insert') mxInsert(c.text, c.reply);
  else if (act === 'analyze') aiRun('analyze', {text: c.src.text, ids: c.src.id ? [c.src.id] : []}, {title: 'Разбор связанного', quote: c.src.text});
  else if (act === 'mav') aiMaverick(c.src.text, c.src.id);
  else if (act === 'asst') asOpenThread(c.thread);
  else if (act === 'asst-new') asFromSelection(c.src.text, c.src.id);
}

// ================================================================ темы форума
async function mxTopicsOpen(id) {
  draftSave();
  id = Number(id); MX.cur = id; MX.chat = MX.byId[id] || {id, title: String(id), kind: '', forum: true}; MX.topic = null; MX.topicView = true;
  MX.openSeq++; MX.msgs = [];
  $('mx-chatbox').style.display = 'none';
  $('mx-empty').style.display = ''; $('mx-empty').innerHTML = '<span>Выберите тему в колонке слева</span>';
  tcolShow(true);
  renderList(); renderAi(); if (MX.infOn) infLoad();
  tcolHead(MX.chat);
  if (MX.topicsChat !== id) $('mx-tlist').innerHTML = '<div class="mx-hint"><span class="spin"></span>Загружаю темы…</div>';
  else renderTopics();
  await mxTopicsLoad(online());
}
// колонка тем форума: видна вместе со списком чатов и перепиской выбранной темы
function tcolShow(on) {
  $('mx-topics').classList.toggle('on', on);
  if (!on) $('mx-empty').innerHTML = '<span>Выберите чат слева</span>';
}
function tcolHead(c) {
  $('mx-thead').innerHTML = avatar(c, true) + `<div class="t" onclick="infToggle(true)"><b>${esc(c.title || '')}</b><small>форум · темы</small></div>` +
    `<button class="ib" onclick="mxChatSettings('topics')" title="Важность и описания тем для сводок">⚙</button>`;
}
async function mxTopicsLoad(live) {
  const id = MX.cur;
  try { let j = await api(`/api/tg/topics?chat=${id}`); if (MX.cur !== id) return; MX.topics = j.topics; MX.topicsChat = id; renderTopics();
    if (!MX.topicView) renderHead();
    if (live) { j = await api(`/api/tg/topics?chat=${id}&live=1`); if (MX.cur !== id) return; MX.topics = j.topics; renderTopics(); if (!MX.topicView) renderHead(); }
  } catch (e) { $('mx-tlist').innerHTML = `<div class="mx-hint">${esc(e.message)}</div>`; }
}
const TCOL = ['#6fb9f0', '#ffd67e', '#cb86db', '#8eee98', '#ff93b2', '#fb6f5f'];
function renderTopics() {
  if (!MX.chat || !MX.chat.forum || MX.topicsChat !== MX.cur) return;
  const list = MX.topics.filter(t => !t.hidden || t.messages);
  const all = `<div class="ti${MX.topic == null && !MX.topicView ? ' on' : ''}" data-topic="all"><div class="dot" style="background:var(--mx-muted)">≡</div><div class="ci-body">` +
    `<div class="ci-row"><span class="ci-title">Все сообщения</span></div><div class="ci-row"><span class="ci-sub">подряд, без деления на темы</span></div></div></div>`;
  $('mx-tlist').innerHTML = all + (list.length ? list.map(t => {
    const col = t.color ? '#' + Number(t.color).toString(16).padStart(6, '0') : TCOL[t.topic_id % TCOL.length];
    return `<div class="ti${t.excluded ? ' excl' : ''}${MX.topic === t.topic_id ? ' on' : ''}" data-topic="${t.topic_id}"><div class="dot" style="background:${col}">${t.topic_id === 1 ? '#' : esc((t.title || '?')[0].toUpperCase())}</div><div class="ci-body">` +
      `<div class="ci-row"><span class="ci-title">${t.pinned ? '📌 ' : ''}${t.closed ? '🔒 ' : ''}${esc(t.title)}${t.muted ? ' 🔕' : ''}` +
      `${t.priority === 'high' ? ' <span class="chip">важная</span>' : ''}${t.excluded ? ' <span class="chip">не в сводке</span>' : ''}</span><span class="ci-time">${listTime(t.top_date || t.last)}</span></div>` +
      `<div class="ci-row"><span class="ci-sub">${esc(t.description || t.ai || (t.messages ? 'сообщений в базе: ' + t.messages : ''))}</span>${t.mentions ? '<span class="badge at">@</span>' : ''}${t.unread ? `<span class="badge${t.muted ? ' muted' : ''}">${t.unread}</span>` : ''}</div></div></div>`;
  }).join('') : `<div class="mx-hint">${online() ? 'Тем нет' : 'Список тем появится, когда будет связь с Telegram'}</div>`);
}

// ================================================================ закреплённое и непрочитанные упоминания / реакции
async function loadPins(seq) {
  try { const j = await api(`/api/tg/pinned?chat=${MX.cur}${MX.topic != null ? '&topic=' + MX.topic : ''}`); if (seq !== MX.openSeq) return; MX.pins = j.pinned || []; MX.pinIdx = 0; renderPin(); } catch (e) {}
}
function renderPin() {
  const el = $('mx-pin'), p = MX.pins[MX.pinIdx];
  el.classList.toggle('on', !!p);
  if (!p) return;
  el.innerHTML = `<span style="color:var(--tg)">📌</span><div class="q"><b>Закреплённое${MX.pins.length > 1 ? ` ${MX.pinIdx + 1} из ${MX.pins.length}` : ''}</b><span>${esc(snippetMx(p) || 'сообщение')}</span></div>`;
}
function pinClick() {
  const p = MX.pins[MX.pinIdx]; if (!p) return;
  if (!jumpTo(p.id)) mxOpen(MX.cur, {around: p.id, topic: MX.topic});
  MX.pinIdx = (MX.pinIdx + 1) % MX.pins.length; renderPin();
}
async function loadMarks(seq) {
  if (!online()) return;
  try { const j = await api(`/api/tg/marks?chat=${MX.cur}${MX.topic != null ? '&topic=' + MX.topic : ''}`); if (seq !== MX.openSeq) return;
    MX.marks = {mentions: j.mentions || [], reactions: j.reactions || []}; renderJumps(); } catch (e) {}
}
function renderJumps() {
  const m = MX.marks || {};
  $('mx-jumps').innerHTML = (m.mentions && m.mentions.length ? `<button data-jm="mentions" title="К упоминанию вас">@<b>${m.mentions.length}</b></button>` : '') +
    (m.reactions && m.reactions.length ? `<button data-jm="reactions" title="К новой реакции на ваше сообщение">❤<b>${m.reactions.length}</b></button>` : '');
}
function jumpMark(k) {
  const list = MX.marks[k] || []; const id = list.shift(); renderJumps(); if (!id) return;
  if (!jumpTo(id)) mxOpen(MX.cur, {around: id, topic: MX.topic});
}

// ================================================================ профиль чата: участники, медиа, файлы, ссылки, голосовые
function infToggle(on) {
  MX.infOn = on === undefined ? !MX.infOn : on;
  $('mx-info').classList.toggle('on', MX.infOn); if (MX.infOn && MX.aiOn) { MX.aiOn = false; $('mx-ai').classList.remove('on'); }
  $('mx-split2').classList.toggle('on', MX.infOn || MX.aiOn);
  if (MX.infOn) infLoad();
}
async function infLoad() {
  const c = MX.chat; if (!c) return;
  $('inf-title').textContent = c.title || ''; $('inf-sub').textContent = {private: 'личный чат', group: 'группа', channel: 'канал', bot: 'бот'}[c.kind] || '';
  document.querySelectorAll('#inf-tabs button').forEach(b => b.classList.toggle('on', b.dataset.k === MX.inf));
  const box = $('inf-body'); box.innerHTML = '<div class="mx-hint"><span class="spin"></span></div>';
  const id = c.id, k = MX.inf;
  try {
    if (k === 'people') {
      const j = await api(`/api/chat/members?chat=${id}${online() && c.kind === 'group' ? '&live=1' : ''}`); if (MX.cur !== id) return;
      MX.members = j.members;
      $('mx-sfrom').innerHTML = '<option value="">все авторы</option>' + j.members.filter(m => m.id).map(m => `<option value="${m.id}">${esc(m.name)}</option>`).join('');
      box.innerHTML = (j.note ? `<div class="mx-hint">${esc(j.note)}</div>` : '') + j.members.map(m => `<div class="inf-row inf-p" data-person="${m.id || ''}">${m.id ? avatar({id: m.id, title: m.name}, 'xs', true) : ''}<div>${esc(m.name)}` +
        (m.priority === 'high' ? ' <span class="chip">важный</span>' : '') + (m.tg_role === 'admin' || m.tg_role === 'creator' ? ' <span class="chip">' + (m.tg_role === 'creator' ? 'владелец' : 'админ') + '</span>' : '') +
        `<small>${m.msgs ? m.msgs + ' сообщ. · ' : ''}${esc(m.role_text || m.role_ai || (m.person && m.person.area) || (m.described || m.ai ? 'есть описание' : 'нет описания — нажмите, чтобы описать'))}</small></div></div>`).join('') || '<div class="mx-hint">Участников нет</div>';
      return;
    }
    const j = await api(`/api/tg/media?chat=${id}&kind=${k}`); if (MX.cur !== id || MX.inf !== k) return;
    const items = j.items || [];
    if (!items.length) { box.innerHTML = '<div class="mx-hint">Пусто</div>'; return; }
    if (k === 'media') box.innerHTML = '<div class="inf-grid">' + items.map(v => { const x = v.media || {}; const pic = x.st === 'ok' && (x.kind === 'photo' || x.kind === 'image');
      return `<div class="cell" data-goto="${v.id}" ${pic ? `data-img="${x.id}"` : ''}>${pic ? `<img src="/media-file/${x.id}?t=${encodeURIComponent(T)}" loading="lazy">` : '▶'}</div>`; }).join('') + '</div>';
    else if (k === 'links') box.innerHTML = items.map(v => { const urls = [...new Set(((v.text || '').match(/https?:\/\/[^\s<>"']+/g) || []).concat(((v.m || {}).e || []).filter(e => e[0] === 'a').map(e => e[3])))].filter(Boolean);
      return urls.map(u => `<div class="inf-row" data-goto="${v.id}"><a ${TG_RX.test(u) ? `class="tg" data-tg="${esc(u)}"` : `href="${esc(u)}" target="_blank" rel="noopener"`} style="color:var(--tg)">${esc(u.slice(0, 80))}</a><small>${esc(v.from || '')} · ${listTime(v.date)}</small></div>`).join(''); }).join('');
    else box.innerHTML = items.map(v => `<div class="inf-row" data-goto="${v.id}">${mediaHTML(v)}<small>${esc(v.out ? 'Вы' : v.from || '')} · ${listTime(v.date)}</small></div>`).join('');
  } catch (e) { box.innerHTML = `<div class="mx-hint">${esc(e.message)}</div>`; }
}

// ================================================================ ссылки Telegram — открыть здесь
async function mxOpenLink(url) {
  try {
    const j = await api('/api/tg/resolve', {link: url});
    if (j.external || j.chat == null) { window.open(url, '_blank', 'noopener'); return; }
    setMode('mx'); window.focus();
    await mxOpen(j.chat, j.msg ? {around: j.msg, topic: j.topic} : (j.topic ? {topic: j.topic} : {}));
  } catch (e) { toast(e.message); }
}
window.mxOpenLink = mxOpenLink;

// ================================================================ глоссарий из мессенджера
async function glQuick(term, open, ref) {
  try { if (!GL.kinds || !Object.keys(GL.kinds).length) { const j = await api('/api/glossary'); Object.assign(GL, {terms: j.terms, kinds: j.kinds, questions: j.questions, counts: j.counts, titles: j.titles || {}}); }
    glInit();
    const low = (term || '').toLowerCase();
    const e = term ? GL.terms.find(x => x.term.toLowerCase() === low || (x.aliases || []).some(a => a.toLowerCase() === low)) : null;
    glOpen(e || {term: term || '', kind: 'other', aliases: [], senses: [{}], status: 'confirmed', refs: ref ? [ref] : []});
    if (!e && term && !open) setTimeout(() => glSuggest(), 100);
  } catch (err) { toast(err.message); }
}

// ================================================================ ширина колонок (перетаскиванием, запоминается; двойной щелчок — как было)
function wireSplit(el, varName, getW, sign) {
  el.addEventListener('mousedown', e0 => {
    e0.preventDefault(); const x0 = e0.clientX, w0 = getW(); el.classList.add('drag'); document.body.style.cursor = 'col-resize';
    const mv = e => { const w = Math.max(220, Math.min(720, w0 + sign * (e.clientX - x0))); document.documentElement.style.setProperty(varName, w + 'px'); };
    const up = () => { document.removeEventListener('mousemove', mv); document.removeEventListener('mouseup', up); el.classList.remove('drag'); document.body.style.cursor = '';
      lsSet(varName, getComputedStyle(document.documentElement).getPropertyValue(varName).trim()); };
    document.addEventListener('mousemove', mv); document.addEventListener('mouseup', up);
  });
  el.addEventListener('dblclick', () => { document.documentElement.style.removeProperty(varName); lsSet(varName, ''); });
  const saved = lsGet(varName, ''); if (saved) document.documentElement.style.setProperty(varName, saved);
}

// ================================================================ обновление программы
let UPD = {}, UPDS = {};
async function updCheck(quiet) {
  try { const j = await api('/api/update/check', {}, 25000); UPD = j.check;
    $('upd-btn').classList.toggle('has', !!UPD.behind);
    $('upd-btn').title = UPD.behind ? 'Есть новая версия — нажмите, чтобы посмотреть, что нового, и обновиться' : 'Обновить программу до последней версии из GitHub';
    if (!quiet) renderUpd();
  } catch (e) { if (!quiet) { $('upd-info').innerHTML = `<div class="msg bad" style="display:block">${esc(e.message)}</div>`; } }
}
async function updOpen() {
  $('upd-modal').classList.add('on'); $('upd-info').innerHTML = '<span class="spin"></span>Проверяю, есть ли новая версия…';
  $('upd-log').classList.remove('on'); msg('upd-msg', '');
  try { const s = await api('/api/update/status'); UPDS = s;
    $('upd-tok-box').style.display = s.can_save_token ? '' : 'none';
    $('upd-tok').placeholder = s.token_set ? '•••••• сохранён (оставьте пустым)' : 'github_pat_…';
    if (s.last && s.last.rolled_back && s.last.error) msg('upd-msg', '⚠ ' + s.last.error, 'warn');
  } catch (e) {}
  await updCheck(false); updZips();
}
function sha7(s) { return s ? String(s).slice(0, 7) : '—'; }
function renderUpd() {
  const c = UPD, l = c.local || {}, r = c.remote;
  let h = `<div>Установлено: <b>${l.sha ? sha7(l.sha) : 'версия неизвестна'}</b>${l.title ? ' — ' + esc(l.title) : ''}${l.method === 'git' ? ' <span class="pill">git</span>' : ''}</div>`;
  if (r) h += `<div>В GitHub: <b>${sha7(r.sha)}</b> — ${esc(r.title || '')}</div>`;
  if (c.error) h += `<div class="msg ${r ? 'warn' : 'bad'}" style="display:block">${esc(c.error)}</div>`;
  if (r && !c.behind) h += '<div class="msg ok" style="display:block">✓ Стоит последняя версия.</div>';
  if (c.behind && c.changes && c.changes.length) h += `<div style="margin-top:8px">Что нового${c.unknown_base ? ' (последние изменения)' : ''}:</div><ul class="upd-ch">` +
    c.changes.map(x => `<li>${esc(x.title)} <span class="hint">${x.date ? new Date(x.date).toLocaleDateString('ru-RU') : ''}</span></li>`).join('') + '</ul>';
  $('upd-info').innerHTML = h;
  $('upd-go').disabled = !c.behind;
  $('upd-tok-help').style.display = c.need_token ? '' : 'none';
  const rp = String((UPDS && UPDS.repo) || c.repo || '').split('/');
  if (rp.length === 2) { $('upd-owner').textContent = rp[0]; $('upd-repo').textContent = rp[1]; }
  // папка программы — клон GitHub и git работает: токен не нужен, лишнее не показываем
  $('upd-tok-box').style.display = UPDS.can_save_token && !(l.method === 'git' && !c.error) ? '' : 'none';
}
async function updZips() {
  try { const j = await api('/api/update/zips');
    $('upd-zips').innerHTML = j.zips.length ? 'Найдено в «Загрузках»: ' + j.zips.map((z, i) => `<button class="lnk" style="border:0;background:none;color:var(--acc);cursor:pointer;font:inherit" onclick="$('upd-zip').value=${esc(JSON.stringify(z.path))}">${esc(z.name)} (${z.mtime})</button>`).join(', ') : '';
  } catch (e) {}
}
async function updToken() {
  try { await api('/api/update/token', {token: $('upd-tok').value.trim()}); $('upd-tok').value = ''; msg('upd-msg', 'Токен сохранён.', 'ok'); await updCheck(false); }
  catch (e) { msg('upd-msg', e.message, 'bad'); }
}
async function updInstall(fromZip) {
  const zip = fromZip ? $('upd-zip').value.trim() : '';
  if (fromZip && !zip) return msg('upd-msg', 'Укажите путь к архиву или выберите найденный.', 'warn');
  if (BG && (BG.export || BG.images) && !confirm('Идёт выгрузка — после перезапуска её нужно будет запустить снова (продолжится с того же места). Обновить?')) return;
  busy(fromZip ? 'upd-zip-go' : 'upd-go', true, 'Устанавливаю…'); msg('upd-msg', '');
  try { const {job} = await api('/api/update/install', {zip});
    await poll(job, 'upd-log', async j => {
      if (j.status === 'error') return msg('upd-msg', j.error, 'bad');
      if (!j.result.updated) { msg('upd-msg', j.result.message || 'Уже последняя версия.', 'ok'); return; }
      msg('upd-msg', 'Новая версия установлена. Перезапускаю программу…', 'ok');
      await sleep(800); await updRestart();
    });
  } catch (e) { msg('upd-msg', e.message, 'bad'); }
  busy(fromZip ? 'upd-zip-go' : 'upd-go', false);
}
async function updRestart() {
  try { await api('/api/update/restart', {}); } catch (e) { msg('upd-msg', e.message, 'bad'); return; }
  $('upd-modal').classList.remove('on'); $('restart').classList.add('on');
  await sleep(2500);
  for (let i = 0; i < 150; i++) {
    try { const r = await fetch('/api/ping', {headers: {'X-Token': T}}); if (r.ok) { location.reload(); return; } } catch (e) {}
    await sleep(1500);
  }
  $('restart-t').textContent = 'Программа не ответила. Запустите OpsDigest.bat заново — если новая версия не запустилась, вернётся прежняя.';
}

// ================================================================ обработчики
function mxInit() {
  // разрешение на уведомления спрашиваем по первому действию в мессенджере (браузер не даёт спросить без него)
  document.querySelector('.mx').addEventListener('click', () => { if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission(); }, {once: true});
  $('mx-q').addEventListener('input', e => mxSearchInput(e.target.value));
  $('mx-q').addEventListener('keydown', e => { if (e.key === 'Enter' && e.target.value.trim()) mxSearch(e.target.value); if (e.key === 'Escape') { e.target.value = ''; mxSearchInput(''); } });
  $('mx-fold').addEventListener('click', e => { const b = e.target.closest('button[data-f]'); if (!b) return; MX.folder = b.dataset.f; renderFolders(); renderList(); $('mx-list').scrollTop = 0; });
  MX.drafts = lsGet('drafts', {}) || {};
  wireSplit($('mx-split'), '--mx-side-w', () => $('mx-side').getBoundingClientRect().width, 1);
  wireSplit($('mx-split2'), '--mx-ai-w', () => (MX.infOn ? $('mx-info') : $('mx-ai')).getBoundingClientRect().width, -1);
  $('mx-tlist').addEventListener('click', e => { const t = e.target.closest('[data-topic]'); if (!t) return;
    if (t.dataset.topic === 'all') mxOpen(MX.cur, {all: true}); else mxOpen(MX.cur, {topic: Number(t.dataset.topic)}); });
  $('mx-pin').addEventListener('click', pinClick);
  $('mx-jumps').addEventListener('click', e => { const b = e.target.closest('[data-jm]'); if (b) jumpMark(b.dataset.jm); });
  $('inf-tabs').addEventListener('click', e => { const b = e.target.closest('button[data-k]'); if (!b) return; MX.inf = b.dataset.k; infLoad(); });
  $('inf-body').addEventListener('click', e => {
    const tg = e.target.closest('a.tg'); if (tg) { e.preventDefault(); return mxOpenLink(tg.dataset.tg); }
    const img = e.target.closest('[data-img]'); if (img) return viewImg(Number(img.dataset.img));
    const vp = e.target.closest('[data-vp]'); if (vp && e.target.closest('.play')) return vpPlay(vp);
    const p = e.target.closest('[data-person]'); if (p && p.dataset.person) return personOpen(Number(p.dataset.person), MX.cur);
    const g = e.target.closest('[data-goto]'); if (g && !e.target.closest('a,button,audio,video')) { if (!jumpTo(Number(g.dataset.goto))) mxOpen(MX.cur, {around: Number(g.dataset.goto), topic: MX.topic}); }
  });
  $('mx-view').addEventListener('click', e => { const b = e.target.closest('[data-g]'); if (b) { e.stopPropagation(); return galAct(b.dataset.g, b); } }, true);
  document.addEventListener('keydown', e => { if (!$('mx-view').classList.contains('on') || !MX.gal) return; if (e.key === 'ArrowLeft') galAct('prev'); if (e.key === 'ArrowRight') galAct('next'); });
  $('mx-send').addEventListener('contextmenu', e => { e.preventDefault(); const m = $('mx-sendmenu'); $('mx-later').style.display = 'none'; m.classList.add('on');
    m.style.left = Math.max(8, Math.min(e.clientX - 200, innerWidth - 320)) + 'px'; m.style.top = Math.max(8, e.clientY - m.offsetHeight - 6) + 'px'; });
  $('mx-sendmenu').addEventListener('click', e => { const b = e.target.closest('[data-s]'); if (!b) return; const m = $('mx-sendmenu');
    if (b.dataset.s === 'silent') { m.classList.remove('on'); return mxSend({silent: true}); }
    if (b.dataset.s === 'later') { const d = new Date(Date.now() + 3600e3); d.setMinutes(0, 0, 0); $('mx-later-at').value = new Date(d - d.getTimezoneOffset() * 60e3).toISOString().slice(0, 16);
      $('mx-later').style.display = ''; m.style.top = Math.max(8, parseFloat(m.style.top) - 44) + 'px'; return; }
    if (b.dataset.s === 'later-go') { const v = $('mx-later-at').value; if (!v) return; m.classList.remove('on'); return mxSend({schedule: v}); }
  });
  document.addEventListener('mousedown', e => { if (!e.target.closest('#mx-sendmenu') && !e.target.closest('#mx-send')) $('mx-sendmenu').classList.remove('on'); });
  $('mx-list').addEventListener('click', e => {
    const s = e.target.closest('.sr'); if (s) return mxOpen(s.dataset.chat, {around: Number(s.dataset.msg)});
    const c = e.target.closest('.ci'); if (c) mxOpen(c.dataset.chat);
  });
  const box = $('mx-msgs');
  box.addEventListener('scroll', () => { if (box.scrollTop < 250) loadOlder(); if (nearBottom() && !MX.hasNewer) MX.newCount = 0; renderDown(); $('mx-selbar').classList.remove('on'); });
  box.addEventListener('click', e => {
    const t = e.target;
    const tg = t.closest('a.tg'); if (tg) { e.preventDefault(); return mxOpenLink(tg.dataset.tg); }
    const img = t.closest('[data-img]'); if (img) { e.preventDefault(); return viewImg(img.dataset.img, img.dataset.cap); }
    const vp = t.closest('[data-vp]'); if (vp) { if (t.closest('.play')) return vpPlay(vp);
      if (t.closest('.sp')) { const a = vp.querySelector('audio'); if (a) { a.playbackRate = a.playbackRate >= 2 ? 1 : a.playbackRate + 0.5; t.closest('.sp').textContent = a.playbackRate + '×'; } return; }
      const bar = t.closest('.bar'); if (bar) { const a = vp.querySelector('audio'); if (a && a.duration) { const r = bar.getBoundingClientRect(); a.currentTime = a.duration * (e.clientX - r.left) / r.width; } return; } }
    const vn = t.closest('video[data-vn]'); if (vn) { vn.muted = false; if (vn.paused) vn.play(); else vn.pause(); return; }
    const fo = t.closest('[data-fopen]'); if (fo) { e.preventDefault(); return fileAct(Number(fo.dataset.fopen), 'open'); }
    const fs = t.closest('[data-fsave]'); if (fs) { e.preventDefault(); return fileAct(Number(fs.dataset.fsave), 'save'); }
    const v1 = t.closest('[data-vote1]'); if (v1) return vote(Number(v1.closest('[data-poll]').dataset.poll), [v1.dataset.vote1]);
    const vm = t.closest('[data-vote]'); if (vm) { const opts = [...vm.closest('[data-poll]').querySelectorAll('input:checked')].map(i => i.value); if (opts.length) return vote(Number(vm.dataset.vote), opts); return; }
    const ex = t.closest('[data-ext]'); if (ex) { const [c, m] = ex.dataset.ext.split(':').map(Number); if (MX.byId[c]) return mxOpen(c, m ? {around: m} : {}); return toast('Этот чат не среди ваших диалогов'); }
    const dl = t.closest('[data-dl]'); if (dl) { if (dl.dataset.play) { const id = Number(dl.dataset.dl); dl.innerHTML = '<span class="spin"></span>';
        return api('/api/tg/download', {media: id}).then(j => { for (const v of MX.msgs) if (v.media && v.media.id === j.media.id) { v.media = j.media; rerender(v.id); }
          const n = document.querySelector(`#mx-msgs video[src*="/media-file/${id}?"]`); if (n) { n.muted = false; n.play().catch(() => {}); } }).catch(err => { toast(err.message); const mr = dl.closest('.mr'); if (mr) rerender(Number(mr.dataset.id)); }); }
      return mxDownload(dl.dataset.dl, dl); }
    const j = t.closest('[data-jump]'); if (j) { if (!jumpTo(Number(j.dataset.jump))) mxOpen(MX.cur, {around: Number(j.dataset.jump)}); return; }
    const num = t.closest('a.num'); if (num) { e.preventDefault(); $('mx-q').value = num.dataset.q; mxSearchInput(num.dataset.q); mxSearch(num.dataset.q);
      if (ITSM_RX.test(num.dataset.q) && hasMaverick()) aiItsm(num.dataset.q); return; }
    const sp = t.closest('.sp'); if (sp) return sp.classList.add('show');
    const rx = t.closest('[data-react]'); if (rx) { MX.menu = msgById(rx.dataset.react); return menuAct(null, rx.dataset.emo); }
    const act = t.closest('[data-act]'); if (act) { const id = Number(act.closest('.mr').dataset.id);
      if (act.dataset.act === 'reply') return mxReply(id); const r = act.getBoundingClientRect(); return mxMenu(r.left, r.bottom + 4, id); }
  });
  box.addEventListener('dblclick', e => { const mr = e.target.closest('.mr'); if (mr && !getSelection().toString().trim()) mxReply(Number(mr.dataset.id)); });
  box.addEventListener('contextmenu', e => { const mr = e.target.closest('.mr'); if (!mr || getSelection().toString().trim()) return; e.preventDefault(); mxMenu(e.clientX, e.clientY, Number(mr.dataset.id)); });
  box.addEventListener('mouseup', () => setTimeout(showSelBar, 10));
  $('mx-menu').addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return; menuAct(b.dataset.m, b.dataset.emo); });
  $('mx-selbar').addEventListener('mousedown', e => e.preventDefault());
  $('mx-selbar').addEventListener('click', e => { const b = e.target.closest('button[data-s]'); if (b) selAct(b.dataset.s); });
  document.addEventListener('mousedown', e => { if (!e.target.closest('#mx-menu')) $('mx-menu').classList.remove('on'); if (!e.target.closest('#mx-selbar') && !e.target.closest('#mx-msgs')) $('mx-selbar').classList.remove('on'); });
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    if ($('mx-view').classList.contains('on')) return $('mx-view').classList.remove('on');
    if ($('mx-menu').classList.contains('on') || $('mx-selbar').classList.contains('on')) return closeMenus();
    if (MX.mode === 'mx' && (MX.reply || MX.edit)) mxCancel();
  });
  $('mx-view').addEventListener('click', () => $('mx-view').classList.remove('on'));
  const ta = $('mx-in');
  ta.addEventListener('input', autosize);
  ta.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); mxSend(); }
    if (e.key === 'ArrowUp' && !ta.value) { const v = [...MX.msgs].reverse().find(x => x.out && textOf(x)); if (v) { e.preventDefault(); mxEdit(v.id); } }
  });
  ta.addEventListener('paste', e => { const fs = [...(e.clipboardData || {}).files || []]; if (fs.length) { e.preventDefault(); mxAttach(fs.map((f, i) => f.name && f.name !== 'image.png' ? f : new File([f], `скриншот-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}${i ? '-' + i : ''}.png`, {type: f.type}))); } });
  $('mx-file').addEventListener('change', e => { mxAttach([...e.target.files]); e.target.value = ''; });
  const chatEl = $('mx-chatbox');
  chatEl.addEventListener('dragover', e => { if ([...e.dataTransfer.types].includes('Files')) { e.preventDefault(); $('mx-drop').classList.add('on'); } });
  chatEl.addEventListener('dragleave', e => { if (!chatEl.contains(e.relatedTarget)) $('mx-drop').classList.remove('on'); });
  chatEl.addEventListener('drop', e => { e.preventDefault(); $('mx-drop').classList.remove('on'); mxAttach([...e.dataTransfer.files]); });
  $('mx-sq').addEventListener('keydown', e => { if (e.key === 'Enter') mxSearchIn(); if (e.key === 'Escape') mxSearchChat(); });
  $('mx-sfrom').addEventListener('change', () => mxSearchIn());
  $('mx-in').addEventListener('input', () => later(draftSave, 800, 'draft'));
  $('mx-sres').addEventListener('click', e => { const s = e.target.closest('.sr'); if (s) mxOpen(MX.cur, {around: Number(s.dataset.msg)}); });
  $('ai-cards').addEventListener('click', e => {
    const b = e.target.closest('button[data-ai]'); if (b) return aiCardAct(b.dataset.ai, b.dataset.n);
    const r = e.target.closest('.ref, .rel-i'); if (r) { const chat = r.dataset.chat !== '' && r.dataset.chat != null ? r.dataset.chat : MX.cur; mxOpen(chat, {around: Number(r.dataset.msg)}); }
  });
  $('ai-chips').addEventListener('click', e => { const b = e.target.closest('button[data-p]'); if (b) aiSummary(b.dataset.p); });
  $('ai-prov').addEventListener('change', e => { MX.provider = e.target.value; });
  $('ai-q').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); aiAsk(); } });
  $('fwd-q').addEventListener('input', fwdList);
  $('fwd-list').addEventListener('click', e => { const c = e.target.closest('[data-to]'); if (c) fwdTo(c.dataset.to); });
  setInterval(() => { if (MX.mode === 'mx' && Object.keys(MX.typing).length) { const now = Date.now(); for (const k in MX.typing) if (MX.typing[k].until < now) delete MX.typing[k]; } }, 3000);
}
