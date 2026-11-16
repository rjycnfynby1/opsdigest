// Ассистент: отдельный полноэкранный чат с ИИ по всем источникам (Telegram — все чаты, почта, ITSM, вики), история
// бесед слева (беседы, вопросы по выделенному из мессенджера, запросы ИИ в чате, сводки).
// Пользуется помощниками окна: api, $, esc, sleep?, mdLite (messenger.js), CFG, PROV_RU, SRC (modules.js).
'use strict';
const AS = {threads: [], cur: null, kind: '', started: false, busy: false, pending: null, files: [], job: null};
const AS_KIND_IC = {free: '✦', selection: '❝', chat: '💬', digest: '★', glossary: '📖', feedback: '📝', maverick: '🛰', command: '⚙'};
const AS_KIND_RU = {free: 'беседа', selection: 'по выделенному', chat: 'ИИ в чате', digest: 'сводка', glossary: 'глоссарий',
  feedback: 'отзыв', maverick: 'Maverick как на портале', command: 'указание'};

async function asStart() {
  if (!AS.started) { AS.started = true; asInit(); }
  asProv(); asSrcDefaults();
  if (typeof srcPills === 'function') srcPills().then(asSrcDefaults);
  await asList();
  if (AS.pending) { const t = AS.pending; AS.pending = null; await asOpen(t); }
  else if (AS.cur) await asOpen(AS.cur.id);
  else asEmpty();
  setTimeout(() => $('as-in').focus(), 60);
}
function asProv() {
  const sel = $('as-prov'), profs = (CFG.media || {}).profiles || [];
  const cur = sel.value || (CFG.llm || {}).provider || profs[0] || '';
  sel.innerHTML = profs.map(p => `<option value="${esc(p)}"${p === cur ? ' selected' : ''}>${esc((typeof PROF_RU !== 'undefined' && PROF_RU[p]) || p)}</option>`).join('');
}
function asSrcDefaults() {
  const d = (typeof SRC !== 'undefined' && SRC.data) || null;
  if (!$('as-src-list').children.length) {
    $('as-src-list').innerHTML = SRC_KEYS.map(k => `<label><input type="checkbox" data-as="${k}"> ${SRC_IC[k]} ${esc(SRC_RU[k])} <span class="pst" data-pst="${k}"></span></label>`).join('');
    document.querySelectorAll('#as-src-list [data-as]').forEach(el => el.addEventListener('change', () => { try { localStorage.setItem('od_as_src', JSON.stringify(asSources())); } catch (e) {} }));
    $('as-mav').addEventListener('change', asMavMode);
  }
  $('as-mav').disabled = !(d && d.available); $('as-mav').closest('label').style.opacity = d && d.available ? '' : '.5';
  let saved = {}; try { saved = JSON.parse(localStorage.getItem('od_as_src') || '{}'); } catch (e) {}
  document.querySelectorAll('[data-as]').forEach(el => {
    const k = el.dataset.as;
    if (k === 'telegram') { el.checked = saved.telegram !== false; return; }
    const ok = d && d.available && d.status[k] && d.status[k].enabled;
    el.disabled = !ok; el.checked = ok && (k in saved ? !!saved[k] : ['mail', 'itsm', 'wiki'].includes(k));
    el.closest('label').style.opacity = ok ? '' : '.5';
    el.closest('label').title = ok ? '' : (d && d.available ? 'Источник выключен в настройках' : 'Maverick не настроен');
  });
}
function asSources() { const o = {}; document.querySelectorAll('[data-as]').forEach(el => { o[el.dataset.as] = el.checked && !el.disabled; }); return o; }
function asMavMode() {  // «как на портале»: свои галочки источников не нужны — Maverick выбирает плагины сам
  const on = $('as-mav').checked;
  document.querySelectorAll('[data-as], #as-period').forEach(el => { el.closest('label') ? el.closest('label').style.display = on ? 'none' : '' : el.style.display = on ? 'none' : ''; });
  $('as-prov').style.display = on ? 'none' : '';
}
async function asList() {
  try { const j = await api('/api/asst/threads?q=' + encodeURIComponent($('as-q').value.trim())); AS.threads = j.threads; }
  catch (e) { $('as-list').innerHTML = `<div class="mx-hint">${esc(e.message)}</div>`; return; }
  asRenderList();
}
function asDay(iso) {
  const d = new Date(iso), now = new Date(), y = new Date(now); y.setDate(now.getDate() - 1);
  if (d.toDateString() === now.toDateString()) return 'Сегодня';
  if (d.toDateString() === y.toDateString()) return 'Вчера';
  if (now - d < 7 * 864e5) return 'На этой неделе';
  return d.toLocaleDateString('ru-RU', {month: 'long', year: 'numeric'});
}
function asRenderList() {
  const list = AS.threads.filter(t => !AS.kind || t.kind === AS.kind);
  let html = '', last = '';
  for (const t of list) {
    const g = t.pinned ? 'Закреплённые' : asDay(t.updated_at);
    if (g !== last) { html += `<div class="as-g">${esc(g)}</div>`; last = g; }
    html += `<div class="as-it${AS.cur && AS.cur.id === t.id ? ' on' : ''}" data-id="${t.id}"><span class="ic" title="${esc(AS_KIND_RU[t.kind] || '')}">${AS_KIND_IC[t.kind] || '✦'}</span>` +
      `<div class="b"><b>${esc(t.title || 'Беседа')}</b><small>${esc(t.last || '')}</small></div></div>`;
  }
  $('as-list').innerHTML = html || '<div class="mx-hint">Здесь будет история: беседы, вопросы по выделенному в мессенджере, запросы ИИ в чатах и сводки.</div>';
}
function asEmpty() {
  AS.cur = null; asRenderList();
  $('as-head').innerHTML = '<b>Новая беседа</b><small>вопрос по всем источникам — без привязки к одному чату</small>';
  $('as-ctx').innerHTML = '';
  $('as-msgs').innerHTML = `<div class="as-hello"><h2>✦ Ассистент</h2><p>Спросите о чём угодно по работе — я поищу во всех чатах Telegram, а если
    включены источники, — в почте, ITSM, вики, X5 Rooms, трекере и расшифровках встреч, и соберу ответ со ссылками. Можно
    приложить файл (📎, перетащить, Ctrl+V), надиктовать (🎤) или дать указание: «всем чатам с „авария“ поставь режим
    инцидент», «запомни, что…» — изменения покажу и применю по кнопке. «🛰 Maverick как на портале» — разговор прямо с Maverick.</p><div class="chips">` +
    ['Что требует моей реакции за сегодня?', 'Что известно по INC…?', 'Что решили на встречах на этой неделе?', 'Запомни: чат с INC в названии — инцидент-чат, а не обращение']
      .map(x => `<button data-ex="${esc(x)}">${esc(x)}</button>`).join('') + '</div></div>';
}
async function asOpen(id) {
  try { const j = await api('/api/asst/thread?id=' + id); AS.cur = j.thread; }
  catch (e) { asEmpty(); return; }
  asRenderList(); asRender();
}
function asRender() {
  const t = AS.cur; if (!t) return asEmpty();
  const m = t.meta || {};
  if ((t.kind === 'maverick' || m.mav_session) && !$('as-mav').disabled && !$('as-mav').checked) { $('as-mav').checked = true; asMavMode(); }
  // сообщения пользователя показывают свои вложения (файлы беседы привязаны к сообщению)
  const byMsg = {}; (t.files || []).forEach(f => { if (f.msg_id) (byMsg[f.msg_id] = byMsg[f.msg_id] || []).push(f); });
  t.messages.forEach(x => { if (x.role === 'user' && byMsg[x.id]) x.data = Object.assign({}, x.data, {files: byMsg[x.id]}); });
  $('as-head').innerHTML = `<b>${esc(t.title || 'Беседа')}</b><small>${esc(AS_KIND_RU[t.kind] || '')}${m.chat_title ? ' · из чата «' + esc(m.chat_title) + '»' : ''}</small>` +
    `<span style="flex:1"></span><button class="ib" onclick="asRename()" title="Переименовать">✎</button>` +
    `<button class="ib" onclick="asPin()" title="${t.pinned ? 'Открепить' : 'Закрепить вверху списка'}">${t.pinned ? '📌' : '📍'}</button>` +
    `<button class="ib" onclick="asDel()" title="Удалить беседу">🗑</button>`;
  $('as-ctx').innerHTML = m.quote ? `<div class="as-quote"><span>❝ ${esc(m.quote.slice(0, 400))}</span>` +
    (m.chat != null && uiMods().mx ? `<button class="lnk" onclick="asGoMsg(${Number(m.chat)}, ${Number((m.ids || [])[0] || 0)})">открыть в чате</button>` : '') + '</div>' : '';
  $('as-msgs').innerHTML = t.messages.map(asMsgHTML).join('') + (AS.busy && AS.busyThread === t.id ? `<div class="as-m bot"><div class="bub"><span class="spin"></span><span id="as-prog">${esc(AS.prog || 'Думаю…')}</span>` +
    ` <button class="lnk" onclick="asStop()" title="Остановить ответ: запросы к модели оборвутся, сессия Maverick закроется">⏹ остановить</button></div></div>` : '');
  const box = $('as-msgs'); box.scrollTop = box.scrollHeight;
}
function asFileHTML(f) {
  if (!f || !f.id) return f && f.name ? `<span class="f hint">✗ ${esc(f.name)}${f.error ? ': ' + esc(f.error) : ''}</span>` : '';
  const u = `/asst-file/${f.id}?t=${encodeURIComponent(T)}`;
  return f.image ? `<a href="${u}" target="_blank" rel="noopener" title="${esc(f.name)}"><img src="${u}" alt="${esc(f.name)}"></a>`
    : `<a class="f" href="${u}&dl=1" download="${esc(f.name)}">📄 ${esc(f.name)} <small>${f.size ? Math.ceil(f.size / 1024) + ' КБ' : ''}</small></a>`;
}
function asMsgHTML(x) {
  if (x.role === 'user') { const fs = (x.data || {}).files || [];
    return `<div class="as-m me"><div class="bub">${esc(x.text).replace(/\n/g, '<br>')}${fs.length ? '<div class="as-att">' + fs.map(asFileHTML).join('') + '</div>' : ''}</div></div>`; }
  if (x.role === 'note') return `<div class="as-m note">${esc(x.text)}</div>`;
  const d = x.data || {};
  const card = {refs: d.refs || [], chat: null};
  let extra = '';
  if (d.report) extra += `<div class="acts"><button class="b" onclick="openReport('${esc(d.report)}')">Открыть отчёт</button></div>`;
  if ((d.ext || []).length) extra += '<div class="as-ext">' + d.ext.map(e => `<div class="ex ${esc(e.source)}"><span class="src ${esc(e.source)}">${esc(SRC_RU[e.source] || e.source)}</span> ` +
    (e.url ? `<a href="${esc(e.url)}" target="_blank" rel="noopener"><b>${esc(e.title || e.ref)}</b></a>` : `<b>${esc(e.source === 'itsm' ? e.ref : e.title || e.ref)}</b>`) +
    (e.source === 'itsm' && e.title ? ' ' + esc(e.title) : '') + (e.status ? ` <span class="pill">${esc(e.status)}</span>` : '') +
    (e.date ? ` <small>${esc(e.date)}</small>` : '') + (e.from ? ` <small>· ${esc(e.from)}</small>` : '') +
    (e.summary ? `<div class="hint">${esc(e.summary)}</div>` : '') + '</div>').join('') + '</div>';
  const att = (d.files || []).concat(d.files_failed || []).map(asFileHTML).join('') +
    (d.media || []).map(m => `<a href="#" class="f" data-gochat="${m.chat}" data-gomsg="${m.msg}" title="${esc(m.summary || '')}">` +
      (['photo', 'image'].includes(m.kind) ? `<img src="/media-file/${m.media}?t=${encodeURIComponent(T)}" alt="">` : `📎 ${esc(m.name || 'файл')}`) + '</a>').join('') +
    (d.ext || []).flatMap(e => (e.images || []).map(i => `<a href="/ext-media/${i.media}?t=${encodeURIComponent(T)}" target="_blank" rel="noopener" title="${esc(i.desc || i.name)}"><img src="/ext-media/${i.media}?t=${encodeURIComponent(T)}" alt=""></a>`)).join('');
  if (att) extra += `<div class="as-att">${att}</div>`;
  extra += askHTML(x) + planHTML(x);
  const off = Object.keys(d.offline || {}).filter(k => k !== 'telegram');
  if (off.length) extra += `<div class="msg warn" style="display:block">Не ответили: ${off.map(k => SRC_RU[k] || k).join(', ')} — переподключите плагин в чате Maverick.</div>`;
  const f = d.found;
  const meta = (d.model ? esc(d.model) : '') + (f ? ' · ' + Object.entries(f).filter(([k, v]) => v).map(([k, v]) => `${SRC_RU[k] || k}: ${v}`).join(', ') : '');
  return `<div class="as-m bot"><div class="bub"><div class="md">${mdLite(x.text, card)}</div>${extra}` +
    `<div class="as-meta">${meta}<span style="flex:1"></span>${fbButtons(d.feedback ? 'other' : 'answer', () => asExcerpt(x), {thread: AS.cur && AS.cur.id, msg: x.id, title: AS.cur && AS.cur.title})}` +
    `<button class="lnk" data-copy="${x.id}">Копировать</button></div></div></div>`;
}
function asExcerpt(x) {
  const msgs = (AS.cur && AS.cur.messages) || [];
  const i = msgs.findIndex(m => m.id === x.id);
  const q = i > 0 && msgs[i - 1].role === 'user' ? 'Вопрос: ' + msgs[i - 1].text + '\n\n' : '';
  return q + 'Ответ: ' + x.text;
}
async function asNew(meta, title) {
  try { const j = await api('/api/asst/new', Object.assign({title: title || 'Новая беседа'}, meta || {})); await asList(); await asOpen(j.id); return j.id; }
  catch (e) { alert(e.message); }
}
async function asAsk(answers) {
  if (answers && (answers instanceof Event || answers.target)) answers = null;  // вызов кнопкой ➤
  const q = $('as-in').value.trim(); if ((!q && !AS.files.length) || AS.busy) return;
  const srcs = asSources();
  const files = AS.files.map(f => f.id), mav = $('as-mav').checked && !$('as-mav').disabled;
  try { localStorage.setItem('od_as_src', JSON.stringify(srcs)); } catch (e) {}
  let tid = AS.cur ? AS.cur.id : null;
  if (!tid) { tid = await asNew(null, q.slice(0, 80)); if (!tid) return; }
  $('as-in').value = '';
  AS.busy = true; AS.busyThread = tid; AS.prog = 'Отправляю…'; asSendBtn();
  AS.cur.messages.push({role: 'user', text: q, id: 'tmp', data: {files: AS.files}}); AS.files = []; asFilesRender(); asRender();
  try {
    const {job} = await api('/api/asst/ask', {thread: tid, question: q, sources: srcs, period: $('as-period').value, provider: $('as-prov').value || null,
      files, maverick: mav, answers: answers || null});
    AS.job = job; asSendBtn();
    let since = 0, fails = 0;
    while (true) {
      let j;
      try { j = await api(`/api/jobs/${job}?since=${since}`); fails = 0; }
      catch (e) { if (/не найдена/.test(e.message) || ++fails > 8) throw e; await new Promise(r => setTimeout(r, 3000)); continue; }
      since = j.log_total;
      if (j.log.length) { AS.prog = j.log[j.log.length - 1]; const p = $('as-prog'); if (p) p.textContent = AS.prog; }
      if (j.status !== 'running') { if (j.status === 'error') toast(j.stopped || /^Остановлено/.test(j.error || '') ? '⏹ Ответ остановлен' : '✗ ' + j.error); break; }
      await new Promise(r => setTimeout(r, 900));
    }
  } catch (e) { toast('✗ ' + e.message); }
  AS.busy = false; AS.job = null; asSendBtn();
  await asList();
  if (AS.cur && AS.cur.id === tid) await asOpen(tid);
  if (typeof srcPills === 'function') srcPills();
}
// пока ассистент отвечает, кнопка ➤ становится ⏹ — остановить ответ (и закрыть сессию Maverick, если она открыта)
function asSendBtn() {
  const b = $('as-send'); if (!b) return;
  if (AS.busy) { b.textContent = '⏹'; b.title = 'Остановить ответ'; b.classList.add('as-stop'); b.onclick = asStop; }
  else { b.textContent = '➤'; b.title = 'Отправить (Enter)'; b.classList.remove('as-stop'); b.onclick = () => asAsk(); }
}
async function asStop() {
  if (!AS.job) return;
  AS.prog = 'Останавливаю…'; const p = $('as-prog'); if (p) p.textContent = AS.prog;
  try { await api(`/api/jobs/${AS.job}/stop`, {}); } catch (e) { toast('✗ ' + e.message); }
}
async function asRename() { const t = AS.cur; if (!t) return; const v = prompt('Название беседы', t.title || ''); if (v == null) return;
  await api('/api/asst/update', {id: t.id, title: v}); await asList(); await asOpen(t.id); }
async function asPin() { const t = AS.cur; if (!t) return; await api('/api/asst/update', {id: t.id, pinned: !t.pinned}); await asList(); await asOpen(t.id); }
async function asDel() { const t = AS.cur; if (!t || !confirm('Удалить беседу «' + (t.title || '') + '»?')) return;
  await api('/api/asst/delete', {id: t.id}); AS.cur = null; await asList(); asEmpty(); }
function asGoMsg(chat, id) { if (!uiMods().mx) return toast('Включите раздел «Мессенджер» в настройках'); setMode('mx'); mxOpen(chat, id ? {around: id} : {}); }
// из мессенджера: «развернуть» вопрос по выделенному в полноэкранную беседу
async function asFromSelection(text, id, question) {
  const meta = {chat: MX.cur, ids: id ? [id] : [], quote: text || '', topic: MX.topic, kind: 'selection'};
  const j = await api('/api/asst/new', Object.assign({title: (question || text || 'По выделенному').slice(0, 80)}, meta));
  AS.pending = j.id; setMode('asst');
  if (question) { $('as-in').value = question; }
}
function asOpenThread(id) { AS.pending = id; setMode('asst'); }

// ---------------------------------------------------------------- вложения
async function asAttach(list) {
  for (const f of [...(list || [])]) {
    if (f.size > 64 * 2**20) { toast('Файл больше 64 МБ: ' + f.name); continue; }
    try {
      const r = await fetch(`/api/asst/upload?name=${encodeURIComponent(f.name || 'вставка.png')}&mime=${encodeURIComponent(f.type || '')}` +
        (AS.cur ? `&thread=${AS.cur.id}` : ''), {method: 'POST', headers: {'X-Token': T}, body: f});
      const j = await r.json(); if (!j.ok) throw new Error(j.error);
      AS.files.push(j.file);
    } catch (e) { toast('✗ ' + (f.name || 'файл') + ': ' + e.message); }
  }
  asFilesRender();
}
function asFilesRender() {
  $('as-files').innerHTML = AS.files.map((f, i) => `<span class="as-fchip">${f.image ? `<img src="/asst-file/${f.id}?t=${encodeURIComponent(T)}" alt="">` : '📄'} ${esc(f.name)}` +
    `<button title="Убрать" data-rmf="${i}">×</button></span>`).join('');
}

function asInit() {
  $('as-files').addEventListener('click', e => { const b = e.target.closest('[data-rmf]'); if (b) { AS.files.splice(Number(b.dataset.rmf), 1); asFilesRender(); } });
  $('as-in').addEventListener('paste', e => { const fs = [...(e.clipboardData || {}).files || []]; if (fs.length) { e.preventDefault(); asAttach(fs); } });
  const main = document.querySelector('.as-main');
  main.addEventListener('dragover', e => { if ([...e.dataTransfer.types].includes('Files')) { e.preventDefault(); main.classList.add('as-drop'); } });
  main.addEventListener('dragleave', e => { if (e.target === main) main.classList.remove('as-drop'); });
  main.addEventListener('drop', e => { main.classList.remove('as-drop'); if (e.dataTransfer.files.length) { e.preventDefault(); asAttach(e.dataTransfer.files); } });
  $('as-in').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (!AS.busy) asAsk(); } });
  $('as-in').addEventListener('input', () => { const t = $('as-in'); t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight, 220) + 'px'; });
  let qt = null; $('as-q').addEventListener('input', () => { clearTimeout(qt); qt = setTimeout(asList, 250); });
  $('as-kinds').addEventListener('click', e => { const b = e.target.closest('[data-k]'); if (!b) return; AS.kind = b.dataset.k;
    document.querySelectorAll('#as-kinds button').forEach(x => x.classList.toggle('on', x === b)); asRenderList(); });
  $('as-list').addEventListener('click', e => { const it = e.target.closest('[data-id]'); if (it) asOpen(Number(it.dataset.id)); });
  $('as-msgs').addEventListener('click', async e => {
    const ex = e.target.closest('[data-ex]'); if (ex) { $('as-in').value = ex.dataset.ex.replace('…', ' '); $('as-in').focus(); return; }
    const cp = e.target.closest('[data-copy]');
    if (cp) { const m = AS.cur.messages.find(x => String(x.id) === cp.dataset.copy); if (m) { await navigator.clipboard.writeText(m.text); toast('Скопировано'); } return; }
    const r = e.target.closest('a.ref'); if (r && r.dataset.chat) { e.preventDefault(); asGoMsg(Number(r.dataset.chat), Number(r.dataset.msg)); return; }
    const g = e.target.closest('[data-gochat]'); if (g) { e.preventDefault(); asGoMsg(Number(g.dataset.gochat), Number(g.dataset.gomsg)); }
  });
  document.querySelectorAll('[data-as]').forEach(el => el.addEventListener('change', () => { try { localStorage.setItem('od_as_src', JSON.stringify(asSources())); } catch (e) {} }));
}
