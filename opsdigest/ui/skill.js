// Навык: правила и поправки, опорные ссылки на вики, отзывы (👍 / 👎 / «Поправить» у любого результата), журнал
// изменений с отменой; карточки «план действий» и «уточняющий вопрос» для ассистента.
// Пользуется помощниками окна: api, $, esc, msg, poll?, toast (messenger.js), AS (assistant.js), setMode.
'use strict';
const SK = {data: null, started: false, tab: 'terms'};
const SK_SCOPE = {digest: 'сводки', assistant: 'ассистент и ИИ в чате', search: 'поиск в источниках', describe: 'описания',
  glossary: 'глоссарий', vision: 'изображения', all: 'везде'};
const SK_TARGET = {report: 'отчёт', answer: 'ответ ассистента', search: 'поиск везде', chat_ai: 'ИИ в чате',
  describe_chat: 'описание чата', describe_topic: 'описание темы', describe_person: 'описание человека',
  glossary: 'статья глоссария', image: 'описание изображения', other: 'результат'};
const SK_ST = {new: 'новый', done: 'учтён', question: 'ждёт ответа', failed: 'ошибка', pending: 'ждёт «Применить»'};

function skInit() {
  if (!SK.started) {
    SK.started = true;
    $('gl-tabs').addEventListener('click', e => { const b = e.target.closest('[data-t]'); if (b) skTab(b.dataset.t); });
    $('sk-r-scope').innerHTML = Object.entries(SK_SCOPE).map(([k, v]) =>
      `<label class="check fit" style="margin:0 14px 0 0;display:inline-flex;gap:4px"><input type="checkbox" data-sks="${k}"${k === 'all' ? ' checked' : ''}> ${esc(v)}</label>`).join('');
    let t = null; $('sk-r-q').addEventListener('input', () => { clearTimeout(t); t = setTimeout(skRender, 200); });
    $('sk-r-off').addEventListener('change', skRender);
  }
  skTab(SK.tab);
  skLoad();
}
function skTab(t) {
  SK.tab = t;
  document.querySelectorAll('#gl-tabs button').forEach(b => b.classList.toggle('on', b.dataset.t === t));
  $('gl-terms-wrap').style.display = t === 'terms' ? '' : 'none';
  $('sk-wrap').style.display = t === 'terms' ? 'none' : '';
  document.querySelectorAll('#sk-wrap [data-skt]').forEach(c => { c.style.display = c.dataset.skt === t ? '' : 'none'; });
}
async function skLoad() {
  try { SK.data = await api('/api/skill'); } catch (e) { msg('sk-msg', e.message, 'bad'); return; }
  $('sk-auto').value = SK.data.auto_apply || 'knowledge';
  const c = SK.data.counts || {};
  const n = (c.questions || 0) + (c.pending || 0);
  $('sk-tab-cnt').textContent = n ? String(n) : ''; $('sk-tab-cnt').style.display = n ? '' : 'none';
  skRender();
}
function skScopes(sc) { return (sc || []).map(s => `<span class="pill">${esc(SK_SCOPE[s] || s)}</span>`).join(' '); }
function skRender() {
  const d = SK.data; if (!d) return;
  const q = $('sk-r-q').value.trim().toLowerCase(), off = $('sk-r-off').checked;
  const rs = d.rules.filter(r => (off || r.active) && (!q || (r.text + ' ' + r.keys.join(' ')).toLowerCase().includes(q)));
  $('sk-r-n').textContent = String(d.rules.filter(r => r.active).length);
  $('sk-rules').innerHTML = rs.map(r => `<div class="sk-rule${r.active ? '' : ' off'}" data-id="${r.id}">` +
    `<div class="t">${esc(r.text)}</div><div class="m">${skScopes(r.scope)}` +
    (r.keys.length ? ` <span class="hint">признаки: ${esc(r.keys.join(', '))}</span>` : ' <span class="hint">всегда</span>') +
    (r.pinned ? ' <span class="pill">📌 всегда</span>' : '') +
    ` <span class="hint">· ${esc({feedback: 'из отзыва', assistant: 'из ассистента', user: 'вручную', import: 'загружено'}[r.source] || r.source || '')}` +
    (r.hits ? `, использовано ${r.hits}` : '') + '</span>' +
    `<span style="flex:1"></span><button class="lnk" onclick="skRuleEdit(${r.id})">изменить</button>` +
    `<button class="lnk" onclick="skRuleToggle(${r.id})">${r.active ? 'выключить' : 'включить'}</button>` +
    `<button class="lnk" onclick="skRuleDel(${r.id})">удалить</button></div></div>`).join('') ||
    '<div class="hint">Пока нет правил. Оцените любой ответ или отчёт (👍 / 👎) или скажите ассистенту «запомни, что…».</div>';
  $('sk-l-n').textContent = String(d.links.length);
  $('sk-links').innerHTML = d.links.map(l => `<div class="sk-rule" data-id="${l.id}"><div class="t">` +
    (l.url ? `<a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.title)}</a>` : esc(l.title)) +
    (l.note ? ` — <span class="hint">${esc(l.note)}</span>` : '') + `</div><div class="m">` +
    (l.keys.length ? `<span class="hint">признаки: ${esc(l.keys.join(', '))}</span>` : '<span class="hint">для всех запросов к вики</span>') +
    `<span style="flex:1"></span><button class="lnk" onclick="skLinkEdit(${l.id})">изменить</button><button class="lnk" onclick="skLinkDel(${l.id})">удалить</button></div></div>`).join('') ||
    '<div class="hint">Добавьте страницы вики, с которых начинать поиск.</div>';
  $('sk-fb').innerHTML = d.feedback.map(f => `<div class="sk-rule"><div class="t">${{1: '👍 ', '-1': '👎 '}[f.rating] || '✎ '}${esc(f.text || '(без текста)')}</div>` +
    `<div class="m"><span class="pill">${esc(SK_TARGET[f.target] || f.target)}</span> <span class="pill ${f.status === 'question' || f.status === 'pending' ? 'warn' : ''}">${esc(SK_ST[f.status] || f.status)}</span>` +
    ` <span class="hint">${esc(new Date(f.created_at).toLocaleString('ru-RU'))}${(f.result || {}).reply ? ' · ' + esc(f.result.reply.slice(0, 160)) : ''}</span><span style="flex:1"></span>` +
    (f.thread ? `<button class="lnk" onclick="asOpenThread(${f.thread})">открыть в ассистенте</button>` : '') + '</div></div>').join('') ||
    '<div class="hint">Отзывов пока нет.</div>';
  $('sk-journal').innerHTML = d.journal.map(j => `<div class="sk-rule${j.undone ? ' off' : ''}"><div class="t">${esc(j.summary)}</div>` +
    `<div class="m"><span class="hint">${esc(new Date(j.created_at).toLocaleString('ru-RU'))} · ${esc({feedback: 'из отзыва', assistant: 'из ассистента'}[j.source] || j.source || '')}` +
    `${j.undone ? ' · отменено' : ''}</span><span style="flex:1"></span>${j.undone ? '' : `<button class="lnk" onclick="skUndo(${j.id})">отменить</button>`}</div></div>`).join('') ||
    '<div class="hint">Изменений пока нет.</div>';
}
async function skRuleSave() {
  const text = $('sk-r-text').value.trim(); if (!text) return;
  const scope = [...document.querySelectorAll('[data-sks]')].filter(x => x.checked).map(x => x.dataset.sks);
  try { await api('/api/skill/rule', {id: Number($('sk-r-id').value) || null, text, scope, keys: $('sk-r-keys').value, pinned: $('sk-r-pin').checked});
    $('sk-r-text').value = ''; $('sk-r-keys').value = ''; $('sk-r-id').value = ''; $('sk-r-pin').checked = false; await skLoad(); }
  catch (e) { msg('sk-msg', e.message, 'bad'); }
}
function skRuleEdit(id) {
  const r = SK.data.rules.find(x => x.id === id); if (!r) return;
  $('sk-r-text').value = r.text; $('sk-r-keys').value = r.keys.join(', '); $('sk-r-id').value = r.id; $('sk-r-pin').checked = r.pinned;
  document.querySelectorAll('[data-sks]').forEach(x => { x.checked = r.scope.includes(x.dataset.sks); });
  $('sk-r-text').focus();
}
async function skRuleToggle(id) { const r = SK.data.rules.find(x => x.id === id); if (!r) return;
  await api('/api/skill/rule', Object.assign({}, r, {active: !r.active})); await skLoad(); }
async function skRuleDel(id) { if (!confirm('Удалить правило?')) return; await api('/api/skill/rule/delete', {id}); await skLoad(); }
async function skLinkSave() {
  try { await api('/api/skill/link', {id: Number($('sk-l-id').value) || null, title: $('sk-l-title').value, url: $('sk-l-url').value.trim(),
    note: $('sk-l-note').value, keys: $('sk-l-keys').value}); skLinkClear(); await skLoad(); }
  catch (e) { alert(e.message); }
}
function skLinkClear() { ['sk-l-id', 'sk-l-title', 'sk-l-url', 'sk-l-note', 'sk-l-keys'].forEach(i => { $(i).value = ''; }); }
function skLinkEdit(id) { const l = SK.data.links.find(x => x.id === id); if (!l) return;
  $('sk-l-id').value = l.id; $('sk-l-title').value = l.title; $('sk-l-url').value = l.url; $('sk-l-note').value = l.note || ''; $('sk-l-keys').value = l.keys.join(', '); }
async function skLinkDel(id) { if (!confirm('Удалить ссылку?')) return; await api('/api/skill/link/delete', {id}); await skLoad(); }
async function skUndo(id) { try { await api('/api/skill/undo', {journal: id}); toast('Отменено'); await skLoad(); } catch (e) { alert(e.message); } }
async function skAuto() { await api('/api/skill/settings', {auto_apply: $('sk-auto').value}); }
async function skExport(fmt) {
  const j = await api('/api/skill/export' + (fmt === 'md' ? '?format=md' : ''));
  const blob = new Blob([fmt === 'md' ? j.text : JSON.stringify(j.data, null, 1)], {type: fmt === 'md' ? 'text/markdown' : 'application/json'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = j.name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}
async function skImport(f) {
  if (!f) return;
  try { const r = await api('/api/skill/import', {data: await f.text()}); toast(`Загружено: правил ${r.rules}, ссылок ${r.links}, терминов ${r.terms}`); await skLoad(); }
  catch (e) { alert(e.message); }
}

// ---------------------------------------------------------------- отзыв на любой результат
const FB = {target: '', excerpt: '', ref: {}, rating: 0};
function fbOpen(target, excerpt, ref, rating) {
  FB.target = target; FB.excerpt = String(excerpt || ''); FB.ref = ref || {}; FB.rating = rating || 0;
  $('fb-title').textContent = 'Отзыв: ' + (SK_TARGET[target] || 'результат') + (FB.ref.title ? ' — ' + FB.ref.title : '');
  $('fb-excerpt').textContent = FB.excerpt.slice(0, 4000) || '—';
  $('fb-text').value = ''; msg('fb-msg', ''); $('fb-log').textContent = '';
  document.querySelectorAll('#fb-rate button').forEach(b => b.classList.toggle('on', Number(b.dataset.r) === FB.rating));
  $('fb-modal').classList.add('on'); setTimeout(() => $('fb-text').focus(), 50);
}
function fbButtons(target, getExcerpt, ref) {
  // кнопки 👍 👎 ✎ — getExcerpt: функция или строка (что оцениваем)
  const id = 'fb' + Math.random().toString(36).slice(2, 8);
  (window._FBX = window._FBX || {})[id] = {target, getExcerpt, ref};
  return `<span class="fb-btns" data-fbx="${id}"><button class="lnk" data-fbr="1" title="Хорошо — запомнить, что так и надо">👍</button>` +
    `<button class="lnk" data-fbr="-1" title="Плохо — рассказать, что не так">👎</button><button class="lnk" data-fbr="0" title="Поправить: что изменить в следующий раз">✎ Поправить</button></span>`;
}
document.addEventListener('click', async e => {
  const b = e.target.closest('[data-fbr]'); if (!b) return;
  const box = b.closest('[data-fbx]'); const x = box && (window._FBX || {})[box.dataset.fbx]; if (!x) return;
  e.preventDefault();
  const ex = typeof x.getExcerpt === 'function' ? x.getExcerpt() : x.getExcerpt;
  const r = Number(b.dataset.fbr);
  if (r === 1) {  // 👍 — сразу, без окна
    try { const j = await api('/api/feedback', {target: x.target, rating: 1, text: 'Хорошо, так и надо', excerpt: ex, ref: x.ref});
      toast('Спасибо — запомню, что так хорошо'); fbWatch(j.job, null); } catch (err) { toast('✗ ' + err.message); }
    return;
  }
  fbOpen(x.target, ex, x.ref, r);
});
document.addEventListener('click', e => { const b = e.target.closest('#fb-rate [data-r]'); if (!b) return;
  FB.rating = Number(b.dataset.r); document.querySelectorAll('#fb-rate button').forEach(x => x.classList.toggle('on', x === b)); });
async function fbSend() {
  const text = $('fb-text').value.trim();
  if (!text && !FB.rating) return msg('fb-msg', 'Напишите, что поправить', 'warn');
  busy('fb-send', true, 'Разбираю…');
  try {
    const j = await api('/api/feedback', {target: FB.target, rating: FB.rating, text, excerpt: FB.excerpt, ref: FB.ref});
    await fbWatch(j.job, 'fb-log');
  } catch (e) { msg('fb-msg', e.message, 'bad'); }
  busy('fb-send', false);
}
async function fbWatch(job, logId) {
  let since = 0, j;
  while (true) {
    j = await api(`/api/jobs/${job}?since=${since}`); since = j.log_total;
    if (logId && j.log.length) $(logId).textContent += j.log.join('\n') + '\n';
    if (j.status !== 'running') break;
    await new Promise(r => setTimeout(r, 800));
  }
  if (j.status === 'error') { if (logId) msg('fb-msg', j.error, 'bad'); else toast('✗ ' + j.error); return; }
  const r = j.result || {};
  if (logId) $('fb-modal').classList.remove('on');
  if (r.status === 'question') {
    toast('Нужно уточнение — вопрос в ассистенте');
    if (typeof uiMods === 'function' && uiMods().asst) asOpenThread(r.thread);
  } else if (logId) {
    const done = (r.plan || []).filter(p => p.state === 'done').length, wait = (r.plan || []).filter(p => p.state === 'pending').length;
    toast((r.reply || 'Учтено') + (wait ? ` Ждут «Применить»: ${wait}.` : ''));
    if (wait && typeof uiMods === 'function' && uiMods().asst) asOpenThread(r.thread);
  }
  if (SK.started) skLoad();
}

// ---------------------------------------------------------------- карточки ассистента: план и вопрос
const PLAN_ST = {done: '✓ сделано', pending: 'ждёт', error: '✗', cancelled: 'отменено'};
function planHTML(x) {
  const plan = (x.data || {}).plan || []; if (!plan.length) return '';
  const pend = plan.some(p => p.state === 'pending');
  return `<div class="as-plan" data-plan="${x.id}"><div class="pl-h">Изменения навыка и данных</div>` + plan.map((p, i) => {
    const tg = (p.targets || []);
    return `<div class="pl ${esc(p.state)}"><span class="st">${esc(PLAN_ST[p.state] || p.state)}</span><div style="flex:1"><div>${esc(p.summary || p.op)}</div>` +
      (p.error ? `<div class="hint">${esc(p.error)}</div>` : '') +
      (tg.length ? `<details><summary class="hint">${tg.length}: ${esc(tg.slice(0, 4).map(t => t.title).join(', '))}${tg.length > 4 ? '…' : ''}</summary>` +
        `<div class="hint">${tg.map(t => esc(t.title)).join('<br>')}</div></details>` : '') + '</div>' +
      (p.state === 'done' && p.journal ? `<button class="lnk" data-undo="${p.journal}">отменить</button>` : '') + '</div>';
  }).join('') + (pend ? `<div class="acts"><button class="b p" data-apply="${x.id}">Применить</button><button class="b" data-apply-cancel="${x.id}">Не нужно</button></div>` : '') + '</div>';
}
function askHTML(x) {
  const a = (x.data || {}).ask; if (!a) return '';
  const msgs = (AS.cur && AS.cur.messages) || [];
  const last = msgs.length && msgs[msgs.length - 1].id === x.id;  // отвечать можно на последний вопрос
  let h = `<div class="as-ask" data-askmsg="${x.id}"><div class="q">❓ ${esc(a.question).replace(/\n/g, '<br>')}</div>`;
  if ((a.form || []).length) {
    h += '<div class="as-form">' + a.form.map((f, i) => `<div class="ff"><label>${esc(f.label)}${f.required ? ' *' : ''}</label>` +
      (f.type === 'choice' ? `<select data-ff="${esc(f.name)}"><option value="">—</option>${f.options.map(o => `<option>${esc(o)}</option>`).join('')}</select>`
        : f.type === 'bool' ? `<select data-ff="${esc(f.name)}"><option value="">—</option><option>Да</option><option>Нет</option></select>`
          : `<input type="text" data-ff="${esc(f.name)}" data-voice>`) + '</div>').join('') + '</div>' +
      (last ? `<div class="acts"><button class="b p" data-askform="${x.id}">Ответить</button></div>` : '');
  }
  h += (last ? (a.options || []).map(o => `<button class="chipb" data-askopt="${esc(o)}">${esc(o)}</button>`).join('') : '') + '</div>';
  return h;
}
function askFormSend(mid) {
  const box = document.querySelector(`[data-askmsg="${mid}"]`); if (!box) return;
  const m = (AS.cur.messages || []).find(x => x.id === mid), form = (((m || {}).data || {}).ask || {}).form || [];
  const answers = {}, lines = [];
  box.querySelectorAll('[data-ff]').forEach(el => { const v = el.value.trim(); if (!v) return; answers[el.dataset.ff] = v;
    const f = form.find(x => x.name === el.dataset.ff); lines.push(`${f ? f.label : el.dataset.ff}: ${v}`); });
  const miss = form.filter(f => f.required && !answers[f.name]);
  if (miss.length) return toast('Заполните: ' + miss.map(f => f.label).join(', '));
  $('as-in').value = lines.join('\n') || 'Готово';
  asAsk(answers);
}
document.addEventListener('click', async e => {
  const ap = e.target.closest('[data-apply]'), cn = e.target.closest('[data-apply-cancel]'), un = e.target.closest('[data-undo]');
  const opt = e.target.closest('[data-askopt]');
  try {
    if (ap) { ap.disabled = true; await api('/api/skill/apply', {msg: Number(ap.dataset.apply)}); toast('Применено'); if (AS.cur) await asOpen(AS.cur.id); }
    else if (cn) { await api('/api/skill/apply', {msg: Number(cn.dataset.applyCancel), cancel: true}); if (AS.cur) await asOpen(AS.cur.id); }
    else if (un) { await api('/api/skill/undo', {journal: Number(un.dataset.undo)}); toast('Отменено'); if (AS.cur) await asOpen(AS.cur.id); }
    else if (opt && opt.closest('#as-msgs')) { $('as-in').value = opt.dataset.askopt; asAsk(); }
    else if (e.target.closest('[data-askform]')) askFormSend(Number(e.target.closest('[data-askform]').dataset.askform));
  } catch (err) { toast('✗ ' + err.message); }
});
