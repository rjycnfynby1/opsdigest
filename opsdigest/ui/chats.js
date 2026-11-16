// Раздел «Чаты» (настройка): папки как в Telegram, фильтры по типу, режиму, активности и названию, массовые изменения,
// правила по названию для будущих чатов, окно чата (описание, темы форума, участники, изображения), люди,
// шаблоны описаний, ИИ-описания фоном. Пользуется общими помощниками окна (index.html): api, $, esc, msg, busy, poll,
// CHATS, BG, runBg, ST, CFG; и папками мессенджера (messenger.js): inFolder.
'use strict';
const PRIO = [['high', 'высокая'], ['normal', 'обычная'], ['low', 'низкая']];
const MODE = [['chat', 'рабочий'], ['incident', 'авария'], ['alerts', 'алерты'], ['news', 'новости']];
const KIND = {private: 'личный', group: 'группа', channel: 'канал', bot: 'бот'};
const PPRIO = [['high', 'важный'], ['normal', 'обычный'], ['low', 'неважный']];
const CH = {folders: [], rules: [], folder: 'all', sel: new Set(), sort: 'last', dir: -1, cur: null, tab: 'desc', topics: [],
  members: [], people: [], psel: new Set(), person: null, tpl: null, tplFor: null, view: 'chats'};
const opts = (list, cur) => list.map(([v, t]) => `<option value="${v}"${cur === v ? ' selected' : ''}>${t}</option>`).join('');
const daysAgo = iso => iso ? Math.floor((Date.now() - new Date(iso)) / 864e5) : null;
function ago(iso) {
  const d = daysAgo(iso); if (d === null) return '—';
  if (d < 1) return 'сегодня'; if (d < 2) return 'вчера'; if (d < 31) return d + ' дн.';
  if (d < 365) return Math.round(d / 30) + ' мес.'; return (d / 365).toFixed(1).replace('.0', '') + ' г.';
}

// ================================================================ список чатов
async function listChats(quiet) {
  try {
    const j = await api('/api/chats/list');
    CHATS = j.chats; CH.folders = j.folders || []; CH.rules = j.rules || [];
    CH.byId = {}; CHATS.forEach(c => CH.byId[c.id] = c);
    renderMediaStats(j.stats); chFolders(); chRender(); chRules();
    $('ch-people-n').textContent = j.people ? ` · описано людей: ${j.people}` : '';
    if (!quiet) msg('chats-msg', CHATS.length ? '' : 'В базе пока нет чатов — нажмите «Обновить список из Telegram» или загрузите экспорт (шаг Telegram).', 'info');
  } catch (e) { if (!quiet) msg('chats-msg', e.message, 'bad'); }
}
async function loadChats() {
  busy('chats-btn', true, 'Загружаю…'); msg('chats-msg', 'Читаю список диалогов из Telegram…', 'info');
  try { const {job} = await api('/api/chats/load', {});
    await poll(job, null, async j => { if (j.status === 'error') return msg('chats-msg', j.error, 'bad'); msg('chats-msg', ''); await listChats(); });
  } catch (e) { msg('chats-msg', e.message, 'bad'); }
  busy('chats-btn', false);
}
function chFolderList() {
  const fs = [{id: 'all', title: 'Все чаты'}].concat(CH.folders || []);
  if (CHATS.some(c => c.archived)) fs.push({id: 'archive', title: 'Архив'});
  return fs;
}
function chInFolder(c, f) {
  if (f.id === 'all') return true;  // в настройке «Все» — вместе с архивом: исключать нужно и там
  return inFolder(Object.assign({unread: c.unread, contact: c.contact, muted: c.muted, archived: c.archived}, c), f);
}
function chFolders() {
  const fs = chFolderList();
  if (!fs.some(f => String(f.id) === String(CH.folder))) CH.folder = 'all';
  $('ch-folders').innerHTML = fs.map(f => { const n = CHATS.filter(c => chInFolder(c, f)).length;
    return `<button data-f="${esc(f.id)}" class="${String(f.id) === String(CH.folder) ? 'on' : ''}"><span>${f.emo ? esc(f.emo) + ' ' : ''}${esc(f.title)}</span><small>${n}</small></button>`; }).join('') +
    (CH.folders.length ? '' : '<div class="hint" style="padding:6px 8px">Папки Telegram появятся после подключения мессенджера.</div>');
}
function chWords(s) { return s.toLowerCase().split(',').map(x => x.trim()).filter(Boolean); }
function chVisible() {
  const f = chFolderList().find(x => String(x.id) === String(CH.folder)) || {id: 'all'};
  const words = chWords($('ch-q').value), qmode = $('ch-qmode').value;
  const ty = $('ch-type').value, mo = $('ch-mode').value, act = $('ch-act').value, de = $('ch-desc').value, ex = $('ch-excl').value;
  const out = CHATS.filter(c => {
    if (!chInFolder(c, f)) return false;
    if (words.length) { const t = (c.title || '').toLowerCase() + ' @' + (c.username || '').toLowerCase();
      const hit = words.some(w => t.includes(w)); if (qmode === 'in' ? !hit : hit) return false; }
    if (ty === 'forum' ? !c.forum : ty && c.kind !== ty) return false;
    if (mo && c.mode !== mo) return false;
    const d = daysAgo(c.last);
    if (act === 'none' ? c.last : act.startsWith('-') ? (d === null || d < -Number(act)) : act && (d === null || d > Number(act))) return false;
    if (de === 'yes' && !c.description) return false; if (de === 'no' && c.description) return false;
    if (de === 'ai' && !c.ai) return false; if (de === 'noai' && c.ai) return false;
    if (de === 'none' && (c.description || c.ai)) return false;
    if (ex === 'in' && c.excluded) return false; if (ex === 'out' && !c.excluded) return false;
    return true;
  });
  const k = CH.sort, dir = CH.dir;
  out.sort((a, b) => dir * (k === 'title' ? (a.title || '').localeCompare(b.title || '', 'ru') : k === 'messages' ? (a.messages || 0) - (b.messages || 0)
    : (a.last || '').localeCompare(b.last || '')));
  return out;
}
function chRender() {
  const list = chVisible();
  CH.shown = list;
  $('chats-wrap').style.display = CHATS.length ? '' : 'none';
  $('chats-body').innerHTML = list.map(c => {
    const marks = (c.description ? '<span class="mk" title="' + esc(c.description) + '">✎</span>' : '') +
      (c.ai ? '<span class="mk ai" title="Анализ ИИ: ' + esc(c.ai) + '">✦</span>' : '') +
      (c.muted ? ' <span class="pill">без звука</span>' : '') + (c.archived ? ' <span class="pill">архив</span>' : '') +
      (c.forum ? ` <span class="pill" title="Форум: темы">тем: ${c.topics || '?'}</span>` : '') +
      (c.by && c.by !== 'id' ? ` <span class="pill" title="Настройки — по правилу по названию">правило «${esc(c.by)}»</span>` : '');
    const sug = c.ai && !c.explicit_mode ? '' : '';
    return `<tr data-id="${c.id}" class="${c.excluded ? 'excl' : ''}${CH.sel.has(c.id) ? ' sel' : ''}">` +
      `<td class="cb"><input type="checkbox" data-sel${CH.sel.has(c.id) ? ' checked' : ''}></td>` +
      `<td class="ttl"><span class="ch-t">${avatar({id: c.id, title: c.title, photo: c.photo}, 'xs')}<span><button class="lnk" data-open>${esc(c.title || String(c.id))}</button>${marks}${sug}</span></span></td>` +
      `<td><span class="pill">${KIND[c.kind] || esc(c.kind)}</span></td>` +
      `<td class="num" title="${esc(c.last ? new Date(c.last).toLocaleString('ru-RU') : 'сообщений нет')}">${ago(c.last)}</td>` +
      `<td class="num" title="${c.history_done ? 'история выгружена полностью' : ''}">${c.messages || ''}${c.history_done ? ' ✓' : ''}</td>` +
      `<td><select data-f="priority">${opts(PRIO, c.priority)}</select></td>` +
      `<td><select data-f="mode">${opts(MODE, c.mode)}</select></td>` +
      `<td style="text-align:center"><input type="checkbox" data-f="exclude"${c.excluded ? ' checked' : ''}></td>` +
      `<td><button class="lnk" data-open>Настроить</button></td></tr>`;
  }).join('');
  const nsel = [...CH.sel].filter(id => CH.byId[id]).length;
  $('ch-count').textContent = `Показано ${list.length} из ${CHATS.length}` + (nsel ? ` · выбрано ${nsel}` : '');
  $('ch-bulk-who').textContent = nsel ? `Выбрано: ${nsel}` : `Все показанные (${list.length})`;
  $('ch-all').checked = list.length > 0 && list.every(c => CH.sel.has(c.id));
  $('ch-rule-q').style.display = chWords($('ch-q').value).length === 1 ? '' : 'none';
}
function chTargets() { const sel = [...CH.sel].filter(id => CH.byId[id]); return sel.length ? sel : (CH.shown || []).map(c => c.id); }
async function chSet(ids, set, quiet) {
  try { await api('/api/chats/bulk', {ids, set});
    ids.forEach(id => { const c = CH.byId[id]; if (!c) return;
      if ('priority' in set) c.priority = set.priority; if ('mode' in set) c.mode = set.mode; if ('exclude' in set) c.excluded = !!set.exclude; });
    if (!quiet) toastCh(`Сохранено: ${ids.length}`); chRender(); if (MX.started) mxDialogs();
  } catch (e) { msg('chats-msg', e.message, 'bad'); }
}
async function chBulk(set, label) {
  const ids = chTargets(); if (!ids.length) return;
  if (!CH.sel.size && ids.length > 1 && !confirm(`${label} — для всех показанных чатов (${ids.length})?`)) return;
  await chSet(ids, set);
}
let _chToastT = null;
function toastCh(t) { const el = $('ch-saved'); el.textContent = '✓ ' + t; el.style.opacity = 1; clearTimeout(_chToastT); _chToastT = setTimeout(() => el.style.opacity = 0, 1800); }
function chResetFilters() { ['ch-q', 'ch-type', 'ch-mode', 'ch-act', 'ch-desc', 'ch-excl'].forEach(id => $(id).value = ''); $('ch-qmode').value = 'in'; chRender(); }

// ---- правила по названию
function chRules() {
  $('ch-rules-list').innerHTML = CH.rules.length ? CH.rules.map(r => `<tr><td><b>${esc(r.match)}</b></td><td>${esc((MODE.find(m => m[0] === r.kind) || ['', '—'])[1])}</td>` +
    `<td>${esc((PRIO.find(p => p[0] === r.priority) || ['', '—'])[1])}</td><td>${r.exclude ? 'исключить' : ''}</td>` +
    `<td class="hint">${esc(r.description || '')}</td><td><button class="x" onclick="chRuleDel(${r.i})" title="Удалить правило">×</button></td></tr>`).join('')
    : '<tr><td colspan="6" class="hint">Правил нет.</td></tr>';
}
async function chRuleAdd(match) {
  match = (match || $('nr-match').value).trim();
  const body = {match, mode: $('nr-mode').value || undefined, priority: $('nr-prio').value || undefined, exclude: $('nr-excl').checked};
  if ($('nr-desc').value.trim()) body.description = $('nr-desc').value.trim();
  try { await api('/api/chats/rule', body); $('nr-match').value = ''; $('nr-desc').value = ''; msg('nr-msg', 'Правило добавлено — действует и на новые чаты.', 'ok'); await listChats(true); }
  catch (e) { msg('nr-msg', e.message, 'bad'); }
}
async function chRuleDel(i) { if (!confirm('Удалить правило?')) return; try { await api('/api/chats/rule', {delete: i}); await listChats(true); } catch (e) { alert(e.message); } }
function chRuleFromFilter() {
  const w = chWords($('ch-q').value)[0]; if (!w) return;
  chView('rules'); $('nr-match').value = w; $('nr-mode').value = 'incident'; $('nr-excl').checked = $('ch-qmode').value === 'in';
  $('nr-match').focus();
}

// ================================================================ ИИ-описания (фоновая задача)
function chAiAsk(kind, ids, extra) {
  CH.aiReq = {kind, ids, extra: extra || {}};
  const n = ids.length;
  const what = {chats: `чатов: ${n}`, people: `людей: ${n}`, topics: `тем: ${n}`, roles: `ролей: ${n}`}[kind];
  $('ai-what').textContent = what;
  $('ai-w-topics').style.display = $('ai-w-people').style.display = kind === 'chats' ? '' : 'none';
  $('ai-w-mode').style.display = kind === 'chats' ? '' : 'none';
  const sel = $('ai-prov2'), profs = (CFG.media || {}).profiles || [];
  sel.innerHTML = profs.map(p => `<option value="${esc(p)}"${p === (CFG.llm || {}).provider ? ' selected' : ''}>${esc(PROF_RU[p] || p)}</option>`).join('');
  const d = (typeof SRC !== 'undefined' && SRC.data) || null, def = (d && d.describe_sources) || [];
  $('ai-o-src').innerHTML = (typeof SRC_KEYS !== 'undefined' ? SRC_KEYS : []).map(k => {
    const ok = d && d.available && d.status[k] && d.status[k].enabled;
    return `<label class="check fit" style="margin:2px 0;${ok ? '' : 'opacity:.45'}"><input type="checkbox" data-dsrc="${k}"${ok && def.includes(k) ? ' checked' : ''}${ok ? '' : ' disabled'}> ${SRC_IC[k]} ${esc(SRC_RU[k])}</label>`;
  }).join('');
  $('ai-modal').classList.add('on');
  if (!d && typeof srcStatus === 'function') srcStatus().then(() => { if ($('ai-modal').classList.contains('on')) chAiAsk(kind, ids, extra); });
}
async function chAiGo() {
  const r = CH.aiReq; if (!r) return;
  $('ai-modal').classList.remove('on');
  const targets = {}; targets[r.kind] = r.ids;
  if (r.extra.roles) targets.roles = r.extra.roles;
  const w = [];
  if (r.kind === 'chats') { if ($('ai-o-topics').checked) w.push('topics'); if ($('ai-o-people').checked) w.push('people'); if ($('ai-o-roles').checked) w.push('roles'); }
  try {
    const sources = [...document.querySelectorAll('[data-dsrc]')].filter(x => x.checked && !x.disabled).map(x => x.dataset.dsrc);
    const {job} = await api('/api/describe', {targets, with: w, overwrite: $('ai-o-over').checked, apply_mode: $('ai-o-mode').checked,
      provider: $('ai-prov2').value || null, sources});
    runBg('describe', job, r.extra.log);
  } catch (e) { alert(e.message); }
}

// ================================================================ окно чата
function openChat(i) { const c = CHATS[i]; if (c) chOpen(c.id); }
async function chOpen(id, tab) {
  const c = CH.byId && CH.byId[id]; if (!c) { await listChats(true); if (!CH.byId[id]) return toast ? toast('Чат ещё не в базе') : null; }
  CH.cur = CH.byId[id]; CH.topics = []; CH.members = [];
  const x = CH.cur;
  $('cm-title').textContent = x.title || ('чат ' + x.id);
  $('cm-info').textContent = `${KIND[x.kind] || x.kind} · сообщений в базе: ${x.messages || 0}` + (x.from ? ` (с ${x.from.slice(0, 10)})` : '') +
    ` · последняя активность: ${ago(x.last)}` + (x.history_done ? ' · история выгружена полностью' : '');
  $('cm-prio').innerHTML = opts(PRIO, x.priority); $('cm-mode').innerHTML = opts(MODE, x.mode);
  $('cm-excl').checked = !!x.excluded; $('cm-desc').value = x.description || ''; $('cm-ai').value = x.ai || '';
  $('cm-ai-at').textContent = x.ai_at ? 'обновлено ' + new Date(x.ai_at).toLocaleString('ru-RU') : '';
  $('cm-fb').innerHTML = typeof fbButtons === 'function' ? fbButtons('describe_chat', () => 'Чат: ' + (x.title || '') + '\nОписание ИИ: ' + $('cm-ai').value, {chat: x.id, title: x.title}) : '';
  $('cm-saved').textContent = '';
  $('cm-tab-topics').style.display = x.forum || x.topics ? '' : 'none';
  $('cm-img').textContent = x.images ? `Изображений: ${x.images}, скачано ${x.images_ok}, описано ${x.images_described}.` + (x.refs ? ` Тяжёлых файлов-ссылок: ${x.refs}.` : '') : 'Изображений в базе пока нет — выгрузите историю чата.';
  if (!((BG.images && IMG_CHAT === x.id) || BG.export)) $('cm-log').classList.remove('on');
  $('chat-modal').classList.add('on');
  cmTab(tab || 'desc');
}
function closeChat() { $('chat-modal').classList.remove('on'); }
function cmTab(t) {
  CH.tab = t;
  document.querySelectorAll('#cm-tabs button').forEach(b => b.classList.toggle('on', b.dataset.t === t));
  document.querySelectorAll('#chat-modal .cm-pane').forEach(p => p.classList.toggle('on', p.id === 'cm-p-' + t));
  if (t === 'topics') cmTopics(false); if (t === 'people') cmMembers(false);
}
async function saveChat() {
  const c = CH.cur; if (!c) return;
  const body = {id: c.id, title: c.title, type: c.kind, priority: $('cm-prio').value, kind: $('cm-mode').value, exclude: $('cm-excl').checked,
    description: $('cm-desc').value.trim(), ai_description: $('cm-ai').value.trim()};
  try { await api('/api/chat/save', {chat: body});
    Object.assign(c, {priority: body.priority, mode: body.kind, excluded: body.exclude, description: body.description, ai: body.ai_description});
    $('cm-saved').textContent = '✓ Сохранено'; chRender(); if (MX.started) mxDialogs();
  } catch (e) { $('cm-saved').textContent = '✗ ' + e.message; }
}
function cmAiNow() { const c = CH.cur; if (!c) return; $('ai-o-over').checked = true; chAiAsk('chats', [c.id], {log: 'cm-ai-log'}); }

// ---- темы форума
async function cmTopics(live) {
  const c = CH.cur; if (!c) return;
  $('cm-topics').innerHTML = '<tr><td colspan="6"><span class="spin"></span>Загружаю темы…</td></tr>';
  try { const j = await api(`/api/chat/topics?chat=${c.id}${live ? '&live=1' : ''}`); CH.topics = j.topics;
    if (j.note) msg('cm-t-msg', j.note, 'warn'); else msg('cm-t-msg', '');
    cmTopicsRender();
  } catch (e) { msg('cm-t-msg', e.message, 'bad'); }
}
function cmTopicsRender() {
  $('cm-topics').innerHTML = CH.topics.length ? CH.topics.map(t => `<tr data-t="${t.topic_id}" class="${t.excluded ? 'excl' : ''}">` +
    `<td class="ttl">${t.pinned ? '📌 ' : ''}${t.closed ? '🔒 ' : ''}<button class="lnk" data-edit>${esc(t.title)}</button>` +
    (t.description ? '<span class="mk" title="' + esc(t.description) + '">✎</span>' : '') + (t.ai ? '<span class="mk ai" title="' + esc(t.ai) + '">✦</span>' : '') + `</td>` +
    `<td class="num">${t.messages || 0}</td><td class="num">${ago(t.top_date || t.last)}</td>` +
    `<td><select data-f="priority">${opts(PRIO, t.priority)}</select></td>` +
    `<td style="text-align:center"><input type="checkbox" data-f="exclude"${t.excluded ? ' checked' : ''}></td>` +
    `<td><button class="lnk" data-edit>Описание</button></td></tr>` +
    `<tr class="t-ed" data-te="${t.topic_id}" style="display:none"><td colspan="6"><div class="ed2">` +
    `<div><label>Ваше описание темы ${tplBtn('topic', 'te-d-' + t.topic_id)}</label><textarea id="te-d-${t.topic_id}" class="desc">${esc(t.description || '')}</textarea></div>` +
    `<div><label>Анализ ИИ <span class="hint">${t.ai_at ? new Date(t.ai_at).toLocaleDateString('ru-RU') : ''}</span></label><textarea id="te-a-${t.topic_id}" class="desc">${esc(t.ai || '')}</textarea></div></div>` +
    `<div class="actions" style="margin-top:6px"><button class="b p" data-tsave>Сохранить</button><button class="b" data-tai>✦ Описать ИИ</button>` +
    (t.ai && typeof fbButtons === 'function' ? fbButtons('describe_topic', 'Тема «' + t.title + '» чата «' + ((CH.cur || {}).title || '') + '»\nОписание ИИ: ' + t.ai,
      {chat: (CH.cur || {}).id, topic: t.topic_id, title: t.title}) : '') + `</div></td></tr>`).join('')
    : '<tr><td colspan="6" class="hint">Тем нет (или список ещё не загружен из Telegram).</td></tr>';
}
async function cmTopicSet(ids, set) {
  const c = CH.cur;
  try { await api('/api/chat/topics', {chat: c.id, topics: ids, set, titles: Object.fromEntries(ids.map(i => [i, (CH.topics.find(t => t.topic_id === i) || {}).title || '']))});
    ids.forEach(i => { const t = CH.topics.find(x => x.topic_id === i); if (!t) return;
      if ('priority' in set) t.priority = set.priority; if ('exclude' in set) t.excluded = !!set.exclude;
      if ('description' in set) t.description = set.description; if ('ai_description' in set) t.ai = set.ai_description; });
    cmTopicsRender(); msg('cm-t-msg', 'Сохранено.', 'ok');
  } catch (e) { msg('cm-t-msg', e.message, 'bad'); }
}
function cmTopicsAi(all) {
  const ids = CH.topics.filter(t => all || !(t.description || t.ai)).map(t => [CH.cur.id, t.topic_id, t.title]);
  if (!ids.length) return msg('cm-t-msg', 'Все темы уже описаны — отметьте «описать заново» в окне запуска.', 'info');
  chAiAsk('topics', ids, {log: 'cm-t-log'});
}

// ---- участники
async function cmMembers(live) {
  const c = CH.cur; if (!c) return;
  $('cm-members').innerHTML = '<tr><td colspan="6"><span class="spin"></span>Загружаю участников…</td></tr>';
  try { const j = await api(`/api/chat/members?chat=${c.id}${live ? '&live=1' : ''}`); CH.members = j.members;
    if (j.note) msg('cm-m-msg', j.note, 'warn'); else msg('cm-m-msg', '');
    cmMembersRender();
  } catch (e) { msg('cm-m-msg', e.message, 'bad'); }
}
function cmMembersRender() {
  const q = ($('cm-m-q').value || '').toLowerCase();
  const list = CH.members.filter(m => !q || (m.name || '').toLowerCase().includes(q) || (m.username || '').toLowerCase().includes(q));
  $('cm-members').innerHTML = list.length ? list.map(m => `<tr data-u="${m.id || ''}">` +
    `<td class="ttl"><button class="lnk" data-person ${m.id ? '' : 'disabled'}>${esc(m.name || '?')}</button>${m.username ? ' <span class="hint">@' + esc(m.username) + '</span>' : ''}` +
    (m.tg_role === 'admin' || m.tg_role === 'creator' ? ' <span class="pill">' + (m.tg_role === 'creator' ? 'владелец' : 'админ') + '</span>' : '') +
    (m.described ? '<span class="mk" title="Есть ваше описание">✎</span>' : '') + (m.ai ? '<span class="mk ai" title="Есть анализ ИИ">✦</span>' : '') + `</td>` +
    `<td class="num">${m.msgs || ''}</td><td class="num">${ago(m.last)}</td>` +
    `<td>${m.id ? `<select data-pp>${opts(PPRIO, m.priority)}</select>` : ''}</td>` +
    `<td class="hint role">${esc(m.role_text || m.role_ai || '')}</td>` +
    `<td>${m.id ? '<button class="lnk" data-person>Описать</button>' : ''}</td></tr>`).join('')
    : '<tr><td colspan="6" class="hint">Участников не найдено. В группе — «Загрузить список из Telegram»; в базе — те, кто писал.</td></tr>';
}
function cmMembersAi(roles) {
  const c = CH.cur, ids = CH.members.filter(m => m.id && m.msgs).map(m => m.id);
  if (!ids.length) return msg('cm-m-msg', 'В этом чате никто не писал (в базе) — описывать не по чему.', 'info');
  if (roles) chAiAsk('roles', ids.map(id => [id, c.id]), {log: 'cm-m-log'}); else chAiAsk('people', ids, {log: 'cm-m-log'});
}

// ================================================================ человек
async function personOpen(id, chat) {
  try { const p = await api(`/api/person?id=${id}${chat != null ? '&chat=' + chat : ''}`); CH.person = p;
    $('pm-title').textContent = p.name + (p.username ? ' · @' + p.username : '');
    $('pm-info').textContent = `Сообщений в базе: ${p.msgs || 0} · чатов: ${(p.chats || []).length}`;
    $('pm-name').value = p.name || ''; $('pm-prio').innerHTML = opts(PPRIO, p.priority);
    ['post', 'dept', 'contacts', 'area', 'ask', 'comes', 'note'].forEach(f => $('pm-' + f).value = p[f] || '');
    $('pm-card').innerHTML = p.card ? '📚 Из вики / адресной книги: ' + esc(p.card) + (p.card_url ? ` <a href="${esc(p.card_url)}" target="_blank" rel="noopener">карточка ↗</a>` : '') : '';
    $('pm-fb').innerHTML = typeof fbButtons === 'function' ? fbButtons('describe_person', () => 'Человек: ' + p.name + '\nОписание ИИ: ' + $('pm-ai').value, {person: p.id, title: p.name}) : '';
    $('pm-aliases').value = p.aliases || ''; $('pm-ai').value = p.ai || '';
    $('pm-ai-at').textContent = p.ai_at ? 'обновлено ' + new Date(p.ai_at).toLocaleString('ru-RU') : '';
    const chat0 = p.chat != null ? (CH.byId[p.chat] || {title: String(p.chat)}) : null;
    $('pm-role-box').style.display = chat0 ? '' : 'none';
    if (chat0) { $('pm-role-t').textContent = `Роль в чате «${chat0.title}»`; $('pm-role').value = p.role || ''; $('pm-role-ai').value = p.role_ai || ''; }
    $('pm-chats').innerHTML = (p.chats || []).map(c => `<button class="chipb" data-c="${c.id}" title="${c.msgs} сообщ.">${esc(c.title)}${c.msgs ? ' · ' + c.msgs : ''}</button>`).join('') || '<span class="hint">—</span>';
    $('pm-saved').textContent = ''; $('person-modal').classList.add('on');
  } catch (e) { alert(e.message); }
}
async function personSave() {
  const p = CH.person; if (!p) return;
  const body = {id: p.id, name: $('pm-name').value, priority: $('pm-prio').value, aliases: $('pm-aliases').value, ai: $('pm-ai').value};
  ['post', 'dept', 'contacts', 'area', 'ask', 'comes', 'note'].forEach(f => body[f] = $('pm-' + f).value);
  if (p.chat != null) Object.assign(body, {chat: p.chat, role: $('pm-role').value, role_ai: $('pm-role-ai').value});
  try { await api('/api/person', body); $('pm-saved').textContent = '✓ Сохранено';
    if (CH.cur && $('chat-modal').classList.contains('on') && CH.tab === 'people') cmMembers(false);
    if (CH.view === 'people') chPeople();
  } catch (e) { $('pm-saved').textContent = '✗ ' + e.message; }
}
function personAi() {
  const p = CH.person; if (!p) return; $('ai-o-over').checked = true;
  chAiAsk('people', [p.id], {log: 'pm-log', roles: p.chat != null ? [[p.id, p.chat]] : null});
}

// ================================================================ люди (все)
async function chPeople() {
  try { const j = await api('/api/people'); CH.people = j.people; chPeopleRender(); }
  catch (e) { msg('pp-msg', e.message, 'bad'); }
}
function chPeopleVisible() {
  const q = ($('pp-q').value || '').toLowerCase(), de = $('pp-desc').value, act = $('pp-act').value, pr = $('pp-prio').value;
  return CH.people.filter(p => {
    if (q && !(p.name || '').toLowerCase().includes(q) && !(p.username || '').toLowerCase().includes(q)) return false;
    if (de === 'yes' && !p.described) return false; if (de === 'no' && p.described) return false;
    if (de === 'ai' && !p.ai) return false; if (de === 'none' && (p.described || p.ai)) return false;
    if (pr && p.priority !== pr) return false;
    const d = daysAgo(p.last); if (act && (d === null || d > Number(act))) return false;
    return true;
  });
}
function chPeopleRender() {
  const list = chPeopleVisible(); CH.pshown = list;
  $('pp-body').innerHTML = list.slice(0, 1500).map(p => `<tr data-u="${p.id}" class="${CH.psel.has(p.id) ? 'sel' : ''}">` +
    `<td class="cb"><input type="checkbox" data-psel${CH.psel.has(p.id) ? ' checked' : ''}></td>` +
    `<td class="ttl"><span class="ch-t">${p.id ? avatar({id: p.id, title: p.name}, 'xs', true) : ''}<span><button class="lnk" data-person>${esc(p.name)}</button>${p.username ? ' <span class="hint">@' + esc(p.username) + '</span>' : ''}</span></span>` +
    (p.described ? '<span class="mk">✎</span>' : '') + (p.ai ? '<span class="mk ai">✦</span>' : '') + `</td>` +
    `<td class="num">${p.msgs || ''}</td><td class="num">${p.chats || ''}</td><td class="num">${ago(p.last)}</td>` +
    `<td><select data-pp>${opts(PPRIO, p.priority)}</select></td><td><button class="lnk" data-person>Описать</button></td></tr>`).join('');
  const n = [...CH.psel].length;
  $('pp-count').textContent = `Показано ${list.length} из ${CH.people.length}` + (n ? ` · выбрано ${n}` : '');
  $('pp-who').textContent = n ? `Выбрано: ${n}` : `Все показанные (${list.length})`;
}
function ppTargets() { return CH.psel.size ? [...CH.psel] : (CH.pshown || []).map(p => p.id); }
async function ppPrio(ids, pr) {
  const names = Object.fromEntries(ids.map(id => [id, ((CH.people.find(p => p.id === id) || CH.members.find(m => m.id === id)) || {}).name || '']));
  try { await api('/api/people/bulk', {ids, set: {priority: pr}, names});
    ids.forEach(id => { const p = CH.people.find(x => x.id === id); if (p) p.priority = pr; const m = CH.members.find(x => x.id === id); if (m) m.priority = pr; });
    if (CH.view === 'people') chPeopleRender(); toastCh('Сохранено: ' + ids.length);
  } catch (e) { alert(e.message); }
}

// ================================================================ шаблоны описаний
function tplBtn(kind, target) { return `<button class="tplb" type="button" data-tpl="${kind}" data-target="${target}" title="Шаблоны и уже написанные описания">📋 Шаблоны</button>`; }
async function tplOpen(btn) {
  const kind = btn.dataset.tpl, target = btn.dataset.target, box = $('tpl-menu');
  CH.tplFor = {kind, target};
  box.innerHTML = '<div class="hint" style="padding:8px"><span class="spin"></span></div>'; box.classList.add('on');
  const r = btn.getBoundingClientRect();
  box.style.left = Math.min(r.left, innerWidth - 380) + 'px'; box.style.top = Math.min(r.bottom + 4, innerHeight - 300) + 'px';
  try { const j = await api('/api/templates?kind=' + encodeURIComponent(kind)); CH.tpl = j; tplRender(); }
  catch (e) { box.innerHTML = `<div class="hint" style="padding:8px">${esc(e.message)}</div>`; }
}
function tplRender() {
  const j = CH.tpl || {templates: [], written: []}, box = $('tpl-menu');
  const item = (x, i, own) => `<div class="tpl-i" data-i="${i}" data-own="${own ? 1 : 0}"><b>${esc(x.name)}</b><span>${esc(x.text)}</span>` +
    (own ? `<button class="x" data-del="${esc(x.name)}" title="Удалить шаблон">×</button>` : '') + '</div>';
  box.innerHTML = `<div class="tpl-h">Шаблоны</div>` + (j.templates.length ? j.templates.map((x, i) => item(x, i, true)).join('') : '<div class="hint" style="padding:4px 10px">Пока нет — сохраните текст ниже как шаблон.</div>') +
    (j.written.length ? `<div class="tpl-h">Уже написано (другие чаты и люди)</div>` + j.written.map((x, i) => item(x, i, false)).join('') : '') +
    `<div class="tpl-f"><button class="b" data-save>＋ Сохранить текущий текст как шаблон</button></div>`;
}
function tplInsert(text) {
  const ta = $(CH.tplFor.target); if (!ta) return;
  const v = ta.value;
  if (!v.trim()) ta.value = text;
  else { const p = ta.selectionStart ?? v.length; ta.value = v.slice(0, p) + (p && v[p - 1] !== '\n' ? '\n' : '') + text + v.slice(p); }
  ta.focus(); $('tpl-menu').classList.remove('on');
}
async function tplSave() {
  const ta = $(CH.tplFor.target), text = ta ? ta.value.trim() : '';
  if (!text) return alert('Сначала напишите текст — его и сохраню как шаблон.');
  const name = prompt('Название шаблона:', text.split(/[.\n]/)[0].slice(0, 40)); if (!name) return;
  try { await api('/api/templates', {kind: CH.tplFor.kind, name, text}); const j = await api('/api/templates?kind=' + encodeURIComponent(CH.tplFor.kind)); CH.tpl = j; tplRender(); }
  catch (e) { alert(e.message); }
}

// ================================================================ вид раздела и обработчики
function chView(v) {
  CH.view = v;
  document.querySelectorAll('#ch-views button').forEach(b => b.classList.toggle('on', b.dataset.v === v));
  document.querySelectorAll('#p-chats .ch-v').forEach(x => x.style.display = x.id === 'ch-v-' + v ? '' : 'none');
  if (v === 'people') chPeople();
}
function chInit() {
  if (CH.inited) return; CH.inited = true;
  $('ch-views').addEventListener('click', e => { const b = e.target.closest('button[data-v]'); if (b) chView(b.dataset.v); });
  $('ch-folders').addEventListener('click', e => { const b = e.target.closest('button[data-f]'); if (!b) return; CH.folder = b.dataset.f; CH.sel.clear(); chFolders(); chRender(); });
  ['ch-q', 'ch-qmode', 'ch-type', 'ch-mode', 'ch-act', 'ch-desc', 'ch-excl'].forEach(id => $(id).addEventListener(id === 'ch-q' ? 'input' : 'change', chRender));
  document.querySelectorAll('#ch-head th[data-sort]').forEach(th => th.onclick = () => { const k = th.dataset.sort; CH.dir = CH.sort === k ? -CH.dir : (k === 'title' ? 1 : -1); CH.sort = k; chRender(); });
  $('ch-all').addEventListener('change', e => { (CH.shown || []).forEach(c => e.target.checked ? CH.sel.add(c.id) : CH.sel.delete(c.id)); chRender(); });
  $('chats-body').addEventListener('change', e => {
    const tr = e.target.closest('tr'); if (!tr) return; const id = Number(tr.dataset.id);
    if (e.target.matches('[data-sel]')) { e.target.checked ? CH.sel.add(id) : CH.sel.delete(id); tr.classList.toggle('sel', e.target.checked); chRender(); return; }
    const f = e.target.dataset.f; if (!f) return;
    chSet([id], {[f]: e.target.type === 'checkbox' ? e.target.checked : e.target.value}, false);
  });
  $('chats-body').addEventListener('click', e => { const b = e.target.closest('[data-open]'); if (b) chOpen(Number(b.closest('tr').dataset.id)); });
  $('ch-bulk').addEventListener('change', e => {
    const s = e.target; if (!s.value) return;
    if (s.id === 'chb-prio') chBulk({priority: s.value}, 'Важность «' + s.selectedOptions[0].text + '»');
    if (s.id === 'chb-mode') chBulk({mode: s.value}, 'Режим «' + s.selectedOptions[0].text + '»');
    s.value = '';
  });
  // окно чата
  $('cm-tabs').addEventListener('click', e => { const b = e.target.closest('button[data-t]'); if (b) cmTab(b.dataset.t); });
  $('cm-topics').addEventListener('change', e => {
    const tr = e.target.closest('tr[data-t]'); if (!tr) return; const id = Number(tr.dataset.t), f = e.target.dataset.f;
    if (f) cmTopicSet([id], {[f]: e.target.type === 'checkbox' ? e.target.checked : e.target.value});
  });
  $('cm-topics').addEventListener('click', e => {
    const ed = e.target.closest('[data-edit]');
    if (ed) { const id = ed.closest('tr').dataset.t, row = document.querySelector(`#cm-topics tr[data-te="${id}"]`); row.style.display = row.style.display === 'none' ? '' : 'none'; return; }
    const sv = e.target.closest('[data-tsave]');
    if (sv) { const id = Number(sv.closest('tr').dataset.te); return cmTopicSet([id], {description: $('te-d-' + id).value.trim(), ai_description: $('te-a-' + id).value.trim()}); }
    const ai = e.target.closest('[data-tai]');
    if (ai) { const id = Number(ai.closest('tr').dataset.te), t = CH.topics.find(x => x.topic_id === id); $('ai-o-over').checked = true; chAiAsk('topics', [[CH.cur.id, id, t ? t.title : '']], {log: 'cm-t-log'}); }
  });
  $('cm-members').addEventListener('click', e => { const b = e.target.closest('[data-person]'); if (b && !b.disabled) personOpen(Number(b.closest('tr').dataset.u), CH.cur ? CH.cur.id : null); });
  $('cm-members').addEventListener('change', e => { if (e.target.matches('[data-pp]')) ppPrio([Number(e.target.closest('tr').dataset.u)], e.target.value); });
  $('cm-m-q').addEventListener('input', cmMembersRender);
  // люди
  ['pp-q', 'pp-desc', 'pp-act', 'pp-prio'].forEach(id => $(id).addEventListener(id === 'pp-q' ? 'input' : 'change', chPeopleRender));
  $('pp-body').addEventListener('click', e => { const b = e.target.closest('[data-person]'); if (b) personOpen(Number(b.closest('tr').dataset.u), null); });
  $('pp-body').addEventListener('change', e => {
    const tr = e.target.closest('tr'); const id = Number(tr.dataset.u);
    if (e.target.matches('[data-psel]')) { e.target.checked ? CH.psel.add(id) : CH.psel.delete(id); tr.classList.toggle('sel', e.target.checked); chPeopleRender(); }
    if (e.target.matches('[data-pp]')) ppPrio([id], e.target.value);
  });
  $('pp-all').addEventListener('change', e => { (CH.pshown || []).forEach(p => e.target.checked ? CH.psel.add(p.id) : CH.psel.delete(p.id)); chPeopleRender(); });
  $('ppb-prio').addEventListener('change', e => { if (e.target.value) { const ids = ppTargets(); if (CH.psel.size || ids.length < 2 || confirm(`Приоритет для всех показанных (${ids.length})?`)) ppPrio(ids, e.target.value); e.target.value = ''; } });
  $('pm-chats').addEventListener('click', e => { const b = e.target.closest('[data-c]'); if (b) { $('person-modal').classList.remove('on'); setMode('mx'); mxOpen(Number(b.dataset.c)); } });
  // шаблоны
  document.addEventListener('click', e => {
    const b = e.target.closest('.tplb'); if (b) { e.preventDefault(); return tplOpen(b); }
    const m = $('tpl-menu'); if (!m.classList.contains('on')) return;
    if (!e.target.closest('#tpl-menu')) return m.classList.remove('on');
    const del = e.target.closest('[data-del]');
    if (del) { api('/api/templates', {kind: CH.tplFor.kind, name: del.dataset.del, delete: true}).then(() => api('/api/templates?kind=' + encodeURIComponent(CH.tplFor.kind))).then(j => { CH.tpl = j; tplRender(); }); return; }
    if (e.target.closest('[data-save]')) return tplSave();
    const it = e.target.closest('.tpl-i'); if (it) { const list = it.dataset.own === '1' ? CH.tpl.templates : CH.tpl.written; tplInsert(list[Number(it.dataset.i)].text); }
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') { $('tpl-menu').classList.remove('on'); } });
}

async function chManage(action) {
  const ids = [...CH.sel].filter(id => CH.byId[id]), box = $('ch-manage-report');
  if (!ids.length) { box.textContent = 'Выберите чаты галочками. Можно применить фильтры и выбрать все показанные.'; return; }
  const username = $('ch-invite-user').value.trim();
  if (action === 'invite' && !username) { box.textContent = 'Введите ник Telegram.'; $('ch-invite-user').focus(); return; }
  const hidden = ids.filter(id => !(CH.shown || []).some(c => c.id === id)).length;
  if (!confirm(`${action === 'leave' ? 'Выйти из чатов' : 'Добавить ' + username + ' в чаты'}: ${ids.length} (скрыто фильтрами: ${hidden})?\n\n${ids.map(id => CH.byId[id].title || id).join('\n')}`)) return;
  $('ch-invite').disabled = $('ch-leave').disabled = true;
  box.setAttribute('aria-busy', 'true'); box.textContent = 'Выполняю действия в Telegram…';
  try {
    const {job} = await api('/api/chats/manage', {ids, action, username});
    await poll(job, null, async j => {
      if (!j.result || !j.result.rows) { box.textContent = j.error || 'Проверьте результат в разделе «Задачи» перед повтором.'; return; }
      CH.manageReport = j.result;
      const rows = j.result.rows, labels = {success: 'Выполнено', already: 'Уже в чате', failed: 'Не выполнено', skipped: 'Пропущено'};
      box.innerHTML = '<p>Отчёт: ' + rows.length + ' чатов. Выполнено: ' + rows.filter(r => r.status === 'success').length + '.</p><div class="tablewrap"><table><thead><tr><th>Чат</th><th>Результат</th><th>Подробности</th></tr></thead><tbody>' + rows.map(r => `<tr><td>${esc(r.title)}</td><td>${esc(labels[r.status])}</td><td>${esc(r.detail)}${r.link ? ' <a target="_blank" rel="noopener" href="' + esc(r.link) + '">Открыть чат</a>' : ''}</td></tr>`).join('') + '</tbody></table></div><button class="b" onclick="chDownloadReport()">Скачать отчёт JSON</button>';
      if (action === 'leave') rows.filter(r => r.status === 'success').forEach(r => CH.sel.delete(r.id));
      chRender();
    });
  } catch (e) { box.textContent = e.message; }
  finally { $('ch-invite').disabled = $('ch-leave').disabled = false; box.removeAttribute('aria-busy'); }
}

function chDownloadReport() {
  if (!CH.manageReport) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(CH.manageReport, null, 2)], {type: 'application/json'}));
  const link = document.createElement('a'); link.href = url; link.download = 'telegram-chat-report.json'; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
