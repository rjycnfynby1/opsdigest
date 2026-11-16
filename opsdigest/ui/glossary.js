// Глоссарий и база знаний: термины с несколькими значениями, вопросы к пользователю, пополнение из чатов,
// вики через Maverick. Пользуется помощниками окна (index.html): api, $, esc, msg, busy, poll, BG, runBg, CFG, PROF_RU.
'use strict';
const GL = {terms: [], kinds: {}, questions: [], counts: {}, cur: null};
const GL_ST = {confirmed: 'подтверждён', draft: 'черновик ИИ', hidden: 'скрыт'};

async function glLoad() {
  try { const j = await api('/api/glossary' + ($('gl-status').value === 'hidden' ? '?hidden=1' : ''));
    Object.assign(GL, {terms: j.terms, kinds: j.kinds, questions: j.questions, counts: j.counts, titles: j.titles || {}});
    if ($('gl-kind').options.length <= 1) $('gl-kind').innerHTML = '<option value="">все виды</option>' + Object.entries(GL.kinds).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join('');
    glBadge(); glQuestions(); glRender();
  } catch (e) { msg('gl-msg', e.message, 'bad'); }
}
function glBadge() {
  const n = (GL.counts || {}).questions || 0;
  const b = document.querySelector('nav button[data-p=gl] .n');
  if (b) { b.textContent = n ? n : '📖'; b.classList.toggle('q', !!n); b.title = n ? `Вопросов к вам: ${n}` : ''; }
  $('gl-stat').textContent = `Терминов: ${GL.counts.terms || 0} (подтверждено ${GL.counts.confirmed || 0}, черновиков ИИ ${GL.counts.draft || 0})`;
}
function glVisible() {
  const q = ($('gl-q').value || '').toLowerCase().replace(/ё/g, 'е'), k = $('gl-kind').value, s = $('gl-status').value;
  return GL.terms.filter(e => {
    if (k && e.kind !== k) return false;
    if (s && s !== 'hidden' && e.status !== s) return false;
    if (s === 'hidden' && e.status !== 'hidden') return false;
    if (q) { const hay = [e.term, ...(e.aliases || []), e.summary || '', ...(e.senses || []).map(x => x.meaning || '')].join(' ').toLowerCase().replace(/ё/g, 'е');
      if (!hay.includes(q)) return false; }
    return true;
  });
}
function glRender() {
  const list = glVisible();
  $('gl-body').innerHTML = list.map(e => `<tr data-id="${e.id}" class="${e.status === 'draft' ? 'draft' : ''}">` +
    `<td class="ttl"><button class="lnk" data-ed>${esc(e.term)}</button>${e.pinned ? ' <span class="pill" title="Подставляется в каждый запрос">всегда</span>' : ''}` +
    (e.aliases.length ? `<div class="hint">${esc(e.aliases.slice(0, 6).join(', '))}</div>` : '') + `</td>` +
    `<td><span class="pill">${esc(GL.kinds[e.kind] || e.kind || '')}</span></td>` +
    `<td class="gl-sum">${esc(e.summary || (e.senses[0] || {}).meaning || '')}${e.senses.length > 1 ? `<div class="hint">значений: ${e.senses.length}</div>` : ''}</td>` +
    `<td>${e.status === 'confirmed' ? '✓ ' : ''}${esc(GL_ST[e.status] || e.status)}</td><td class="num">${e.hits || ''}</td>` +
    `<td><button class="lnk" data-ed>Открыть</button></td></tr>`).join('') ||
    '<tr><td colspan="6" class="hint">Ничего не найдено.</td></tr>';
  $('gl-count').textContent = `Показано ${list.length} из ${GL.terms.length}`;
}
function glQuestions() {
  const qs = GL.questions || [];
  $('gl-q-card').style.display = qs.length ? '' : 'none';
  $('gl-q-n').textContent = qs.length;
  $('gl-qs').innerHTML = qs.map(q => `<div class="glq" data-q="${q.id}"><div><b>${esc(q.term || 'Вопрос')}</b> — ${esc(q.question)}</div>` +
    (q.context ? `<div class="hint">${esc(q.context)}${(q.refs || []).length ? ' · ' + q.refs.map(r => `<a class="lnk" data-open="${r[0]}:${r[1]}">#${r[1]}</a>`).join(' ') : ''}</div>` : '') +
    (q.options.length ? `<div class="chips" style="margin-top:6px">${q.options.map(o => `<button data-opt>${esc(o)}</button>`).join('')}</div>` : '') +
    `<div class="row" style="margin-top:6px"><input type="text" placeholder="Ваш ответ — своими словами"><button class="b p fit" data-ans>Ответить</button>` +
    `<button class="b fit" data-wiki title="Найти в Confluence и ITSM через Maverick">🔎 Вики</button><button class="b fit" data-skip title="Убрать вопрос">Пропустить</button></div>` +
    `<div class="glq-res hint"></div></div>`).join('');
}
async function glAnswer(qid, answer, box) {
  if (!answer.trim()) return;
  box.querySelector('.glq-res').innerHTML = '<span class="spin"></span>Уточняю статью…';
  try { const {job} = await api('/api/glossary/answer', {id: qid, answer});
    await poll(job, null, async j => { if (j.status === 'error') { box.querySelector('.glq-res').textContent = '✗ ' + j.error; return; } await glLoad(); });
  } catch (e) { box.querySelector('.glq-res').textContent = '✗ ' + e.message; }
}
async function glWiki(term, out, onText) {
  out.innerHTML = '<span class="spin"></span>Спрашиваю Maverick (Confluence, ITSM)…';
  try { const {job} = await api('/api/glossary/wiki', {term});
    await poll(job, null, j => { if (j.status === 'error') { out.textContent = '✗ ' + j.error; return; }
      out.innerHTML = `<div class="gl-wiki">${esc(j.result.text)}</div>`; if (onText) onText(j.result.text); });
  } catch (e) { out.textContent = '✗ ' + e.message; }
}
async function glScan() {
  if (!confirm('Прочитать чаты, где есть новое с прошлого прохода (до 40 самых активных, кроме исключённых), и пополнить глоссарий? ' +
    'Новые термины появятся черновиками, неясное — вопросами вам. Это несколько запросов к модели на чат.')) return;
  try { const {job} = await api('/api/glossary/scan', {}); runBg('glossary', job); } catch (e) { alert(e.message); }
}
async function glExport() {
  try { const j = await api('/api/glossary/export');
    const blob = new Blob([JSON.stringify(j.data, null, 1)], {type: 'application/json'});
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'glossary-opsdigest.json'; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  } catch (e) { alert(e.message); }
}
async function glImport(file) {
  if (!file) return;
  try { const data = JSON.parse(await file.text()); const r = await api('/api/glossary/import', {data});
    msg('gl-msg', `Загружено терминов: ${r.terms}, вопросов: ${r.questions}.`, 'ok'); await glLoad(); }
  catch (e) { msg('gl-msg', 'Не загрузилось: ' + e.message, 'bad'); }
}

// ---- статья
function glOpen(e) {
  GL.cur = e || {term: '', kind: 'other', aliases: [], senses: [{}], status: 'confirmed', refs: []};
  const x = GL.cur;
  $('ge-title').textContent = x.id ? x.term : 'Новый термин';
  $('ge-term').value = x.term || ''; $('ge-kind').innerHTML = Object.entries(GL.kinds).map(([k, v]) => `<option value="${k}"${k === x.kind ? ' selected' : ''}>${esc(v)}</option>`).join('');
  $('ge-status').value = x.status || 'confirmed'; $('ge-pin').checked = !!x.pinned;
  $('ge-aliases').value = (x.aliases || []).join(', '); $('ge-summary').value = x.summary || ''; $('ge-owner').value = x.owner || '';
  $('ge-details').value = x.details || ''; $('ge-related').value = (x.related || []).join(', ');
  glSenses(x.senses && x.senses.length ? x.senses : [{}]);
  $('ge-refs').innerHTML = (x.refs || []).slice(-15).reverse().map(r => `<a class="chipb" data-open="${r[0]}:${r[1]}">${esc((GL.titles || {})[r[0]] || 'чат')} · #${r[1]}</a>`).join(' ') || '<span class="hint">—</span>';
  $('ge-examples').value = (x.examples || []).join('\n');
  $('ge-links').value = (x.links || []).map(l => (l.url || '') + (l.title ? ' ' + l.title : '')).join('\n');
  $('ge-visual').value = x.visual || ''; GL.imgs = {attached: [], candidates: []}; $('ge-cand').innerHTML = ''; $('ge-imgs-hint').textContent = '';
  $('ge-imgs').innerHTML = x.id && (x.images || []).length ? '<span class="spin"></span>' : '<span class="hint">Нет — «Подобрать» найдёт описанные картинки, где встречается термин.</span>';
  if (x.id && (x.images || []).length) glImages(false);
  $('ge-del').style.display = x.id ? '' : 'none'; $('ge-wiki-out').innerHTML = ''; $('ge-saved').textContent = '';
  $('gl-modal').classList.add('on'); setTimeout(() => $('ge-term').focus(), 50);
}
function glSenses(list) {
  $('ge-senses').innerHTML = list.map((s, i) => `<div class="gls"><div class="gls-h"><b>Значение ${i + 1}</b><button class="x" data-rm="${i}" title="Убрать значение">×</button></div>` +
    `<textarea data-k="meaning" placeholder="Что это значит">${esc(s.meaning || '')}</textarea>` +
    `<div class="grid2"><div><input type="text" data-k="how_to_tell" placeholder="Как понять, что речь об этом значении" value="${esc(s.how_to_tell || '')}"></div>` +
    `<div class="row"><input type="text" data-k="id_format" placeholder="Номер: регулярка, напр. INC\\d{7}" value="${esc(s.id_format || '')}"><input type="text" data-k="owner" placeholder="Кто отвечает" value="${esc(s.owner || '')}"></div></div></div>`).join('');
}
function glSensesRead(all) {
  return [...document.querySelectorAll('#ge-senses .gls')].map(d => { const o = {}; d.querySelectorAll('[data-k]').forEach(el => { if (el.value.trim()) o[el.dataset.k] = el.value.trim(); }); return o; }).filter(o => all || o.meaning);
}
async function glSave() {
  const x = GL.cur || {};
  const body = {id: x.id, term: $('ge-term').value.trim(), kind: $('ge-kind').value, status: $('ge-status').value, pinned: $('ge-pin').checked,
    aliases: $('ge-aliases').value.split(',').map(s => s.trim()).filter(Boolean), summary: $('ge-summary').value, owner: $('ge-owner').value,
    details: $('ge-details').value, related: $('ge-related').value.split(',').map(s => s.trim()).filter(Boolean), senses: glSensesRead(), refs: x.refs || [],
    visual: $('ge-visual').value, images: x.images || [],
    examples: $('ge-examples').value.split('\n').map(s => s.trim()).filter(Boolean),
    links: $('ge-links').value.split('\n').map(s => s.trim()).filter(Boolean).map(l => { const m = l.match(/^(\S+)\s*(.*)$/); return /^https?:/.test(m[1]) ? {url: m[1], title: m[2]} : {url: '', title: l}; })};
  try { const j = await api('/api/glossary/save', {term: body}); GL.cur = j.term; $('ge-saved').textContent = '✓ Сохранено'; $('ge-title').textContent = j.term.term; $('ge-del').style.display = ''; glLoad();
    if (typeof GQ !== 'undefined' && GQ.filling) { const k = GQ.filling; GQ.filling = null; gqSet('done', [k]); } }
  catch (e) { $('ge-saved').textContent = '✗ ' + e.message; }
}
async function glDel() { const x = GL.cur; if (!x || !x.id || !confirm(`Удалить «${x.term}» из глоссария?`)) return;
  try { await api('/api/glossary/delete', {id: x.id}); $('gl-modal').classList.remove('on'); glLoad(); } catch (e) { alert(e.message); } }
async function glSuggest() {
  const term = $('ge-term').value.trim(); if (!term) return $('ge-term').focus();
  $('ge-wiki-out').innerHTML = '<span class="spin"></span>Ищу термин в переписке и предлагаю статью…';
  try { const {job} = await api('/api/glossary/suggest', {term});
    await poll(job, null, j => {
      if (j.status === 'error') { $('ge-wiki-out').textContent = '✗ ' + j.error; return; }
      const e = j.result.entry || {};
      if (!$('ge-summary').value.trim() && e.summary) $('ge-summary').value = e.summary;
      if (!$('ge-aliases').value.trim() && (e.aliases || []).length) $('ge-aliases').value = e.aliases.join(', ');
      if (!$('ge-owner').value.trim() && e.owner) $('ge-owner').value = e.owner;
      if (e.kind && GL.kinds[e.kind]) $('ge-kind').value = e.kind;
      const cur = glSensesRead(); if (!cur.length && (e.senses || []).length) glSenses(e.senses);
      if (j.result.refs) GL.cur.refs = j.result.refs;
      $('ge-wiki-out').innerHTML = j.result.note ? esc(j.result.note) : `Найдено упоминаний: ${j.result.found}. Пустые поля заполнены предложением ИИ — проверьте и сохраните.` +
        ((e.questions || []).length ? '<br>Неясно: ' + e.questions.map(esc).join('; ') : '');
    });
  } catch (e) { $('ge-wiki-out').textContent = '✗ ' + e.message; }
}
function glWikiEd() {
  const term = $('ge-term').value.trim(); if (!term) return;
  glWiki(term, $('ge-wiki-out'), text => { $('ge-wiki-out').insertAdjacentHTML('beforeend', '<div class="actions"><button class="b" id="ge-wiki-ins">Вставить в «Подробнее»</button></div>');
    $('ge-wiki-ins').onclick = () => { const d = $('ge-details'); d.value = (d.value.trim() ? d.value.trim() + '\n\n' : '') + 'Из вики (Maverick, ' + new Date().toLocaleDateString('ru-RU') + '): ' + text; }; });
}
function glOpenRef(v) { const [c, m] = v.split(':').map(Number); $('gl-modal').classList.remove('on'); setMode('mx'); mxOpen(c, {around: m}); }
function glNew(term) { if (typeof GQ !== 'undefined') GQ.filling = null; go('gl'); glOpen({term: term || '', kind: 'other', aliases: [], senses: [{}], status: 'confirmed', refs: []}); }

function glInit() {
  if (GL.inited) return; GL.inited = true;
  ['gl-q', 'gl-kind'].forEach(id => $(id).addEventListener(id === 'gl-q' ? 'input' : 'change', glRender));
  $('gl-status').addEventListener('change', glLoad);
  $('gl-body').addEventListener('click', e => { const b = e.target.closest('[data-ed]'); if (b) glOpen(GL.terms.find(x => x.id === Number(b.closest('tr').dataset.id))); });
  $('gl-qs').addEventListener('click', e => {
    const box = e.target.closest('.glq'); if (!box) return; const qid = Number(box.dataset.q), q = GL.questions.find(x => x.id === qid);
    const op = e.target.closest('[data-open]'); if (op) return glOpenRef(op.dataset.open);
    if (e.target.closest('[data-opt]')) return glAnswer(qid, e.target.closest('[data-opt]').textContent, box);
    if (e.target.closest('[data-ans]')) return glAnswer(qid, box.querySelector('input').value, box);
    if (e.target.closest('[data-skip]')) return api('/api/glossary/dismiss', {id: qid}).then(glLoad);
    if (e.target.closest('[data-wiki]')) return glWiki(q.term || q.question, box.querySelector('.glq-res'));
  });
  $('gl-qs').addEventListener('keydown', e => { if (e.key === 'Enter' && e.target.matches('input')) { const box = e.target.closest('.glq'); glAnswer(Number(box.dataset.q), e.target.value, box); } });
  $('ge-senses').addEventListener('click', e => { const b = e.target.closest('[data-rm]'); if (!b) return; const s = glSensesRead(); s.splice(Number(b.dataset.rm), 1); glSenses(s.length ? s : [{}]); });
  $('gl-modal').addEventListener('click', e => { const op = e.target.closest('[data-open]'); if (op) glOpenRef(op.dataset.open); });
}

// ---- иллюстрации: описанные изображения из переписки ↔ термин
const glThumb = (c, act) => `<figure><a data-gimg="${c.media}"><img loading="lazy" src="/media-file/${c.media}?t=${encodeURIComponent(T)}" alt=""></a>` +
  `<button class="tb" data-${act}="${c.media}" title="${act === 'add' ? 'Прикрепить к термину' : 'Открепить'}">${act === 'add' ? '＋' : '×'}</button>` +
  `<figcaption>${esc((c.summary || '').slice(0, 90))}${c.chat_title ? ' · ' + esc(c.chat_title) : ''}</figcaption></figure>`;
async function glImages(search) {
  const x = GL.cur; if (!x || !x.id) { $('ge-imgs-hint').textContent = 'Сначала сохраните термин.'; return; }
  if (search) $('ge-cand').innerHTML = '<span class="spin"></span>';
  try { const j = await api('/api/glossary/images?id=' + x.id); GL.imgs = j;
    $('ge-imgs').innerHTML = j.attached.length ? j.attached.map(c => glThumb(c, 'rm')).join('') : '<span class="hint">Не прикреплены.</span>';
    if (search) { $('ge-cand').innerHTML = j.candidates.map(c => glThumb(c, 'add')).join('');
      $('ge-imgs-hint').textContent = j.candidates.length ? `Подходят по описанию: ${j.candidates.length}. «＋» — прикрепить (сохраните статью).` : 'Подходящих описанных картинок не нашлось — опишите изображения чатов («Чаты» → чат → «Изображения»).'; }
  } catch (e) { $('ge-imgs-hint').textContent = e.message; }
}
document.addEventListener('click', e => {
  if (!e.target.closest('#gl-modal')) return;
  const add = e.target.closest('[data-add]'), rm = e.target.closest('[data-rm]'), im = e.target.closest('[data-gimg]');
  const x = GL.cur; if (!x) return;
  if (add) { const id = Number(add.dataset.add); x.images = [...new Set([...(x.images || []), id])]; const c = GL.imgs.candidates.find(c => c.media === id);
    GL.imgs.candidates = GL.imgs.candidates.filter(c => c.media !== id); if (c) GL.imgs.attached.push(c); add.closest('figure').remove();
    $('ge-imgs').innerHTML = GL.imgs.attached.map(c => glThumb(c, 'rm')).join(''); $('ge-saved').textContent = 'не сохранено'; }
  if (rm && rm.closest('#ge-imgs')) { const id = Number(rm.dataset.rm); x.images = (x.images || []).filter(i => i !== id); GL.imgs.attached = GL.imgs.attached.filter(c => c.media !== id);
    rm.closest('figure').remove(); $('ge-saved').textContent = 'не сохранено'; }
  if (im) { e.preventDefault(); window.open(`/media-file/${im.dataset.gimg}?t=${encodeURIComponent(T)}`, '_blank'); }
});
