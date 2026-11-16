let RPA = {items: [], templates: {}, fields: {}, statuses: []};
async function rpaExplore(action) {
  const target = $('rpa-explorer');
  target.textContent = 'Подождите…';
  try {
    const r = await api('/api/rpa/explorer', {action, url:$('rpa-url').value, channel:$('rpa-channel').value});
    if (r.closed) { target.textContent = 'Браузер закрыт'; return; }
    target.innerHTML = `<p>${esc(r.notice)}</p>` + r.pages.map(p =>
      `<h4>${esc(p.url)}</h4>` + p.frames.map(f => `<details open><summary>Фрейм: ${esc(f.name || 'основной/без имени')} · ${esc(f.url)}</summary>` +
        (f.error ? esc(f.error) : f.elements.map(e => `<details><summary>${esc(e.tag)} · ${esc(e.name || '(без подписи)')}${e.scrollable ? ' · прокрутка' : ''}</summary><pre style="white-space:pre-wrap">${esc(e.text)}</pre></details>`).join('')) + '</details>').join('')).join('');
  } catch(e) { target.textContent = e.message; }
}
async function rpaLoad() {
  $('rpa-result').textContent = 'Загружаю поля и шаблоны…';
  $('rpa-fields').setAttribute('aria-busy', 'true');
  try {
    Object.assign(RPA, await api('/api/rpa/templates'));
    // A reload must not discard edits made while the request was pending.
    const values = Object.fromEntries(Array.from(document.querySelectorAll('[data-rpa-field]'), e => [e.dataset.rpaField, e.value]));
    const statuses = new Set(Array.from(document.querySelectorAll('#rpa-statuses input:checked'), e => e.value));
    $('rpa-fields').innerHTML = Object.entries(RPA.fields).map(([k,v]) =>
      `<label>${esc(v)}<input data-rpa-field="${k}" type="${/_(after|before)$/.test(k) ? 'datetime-local' : 'text'}"></label>`).join('');
    $('rpa-statuses').innerHTML = RPA.statuses.map(s => `<label class="check"><input type="checkbox" value="${esc(s)}">${esc(s)}</label>`).join('');
    document.querySelectorAll('[data-rpa-field]').forEach(e => { e.value = values[e.dataset.rpaField] || ''; });
    document.querySelectorAll('#rpa-statuses input').forEach(e => { e.checked = statuses.has(e.value); });
    $('rpa-templates').innerHTML = '<option value="">Выберите шаблон</option>' + Object.keys(RPA.templates).map(n => `<option>${esc(n)}</option>`).join('');
    rpaRender();
    $('rpa-result').textContent = '';
  } catch(e) { $('rpa-result').textContent = e.message; }
  finally { $('rpa-fields').setAttribute('aria-busy', 'false'); }
}
function rpaRender() {
  $('rpa-run').disabled = RPA.items.length === 0;
  $('rpa-queue').innerHTML = RPA.items.map((i,n) => `<li><b>${i.kind === 'incidents' ? 'Инциденты' : 'Проблемы'}</b>: ${esc(Object.entries(i.filters).map(([k,v]) => (RPA.fields[k] || k) + ': ' + v).join('; '))} ${esc(i.statuses.join(', '))} <button onclick="RPA.items.splice(${n},1);rpaRender()">Удалить</button> <button onclick="if(${n}>0){[RPA.items[${n-1}],RPA.items[${n}]]=[RPA.items[${n}],RPA.items[${n-1}]];rpaRender()}">↑</button></li>`).join('');
}
function rpaAdd() {
  const kind = 'incidents';
  const filters = {};
  if (kind === 'incidents') document.querySelectorAll('[data-rpa-field]').forEach(e => {if(e.value) filters[e.dataset.rpaField] = e.value;});
  RPA.items.push({kind, filters, statuses: Array.from(document.querySelectorAll('#rpa-statuses input:checked'), e=>e.value), confirm_all: false});
  rpaRender();
}
function rpaAddProblems() {
  RPA.items.push({kind: 'problems', filters: {}, statuses: [], confirm_all: true});
  rpaRender();
}
async function rpaSave() {
  try { await api('/api/rpa/templates', {name: $('rpa-name').value, items: RPA.items}); await rpaLoad(); }
  catch(e) { $('rpa-result').textContent = e.message; }
}
async function rpaRun() {
  $('rpa-run').disabled = true;
  $('rpa-analyze').disabled = true;
  RPA.lastRun = null;
  $('rpa-result').textContent = 'Запускаю очередь…';
  try {
    const r = await api('/api/rpa/run', {items:RPA.items, url:$('rpa-url').value, channel:$('rpa-channel').value});
    while (true) {
      const j = await api('/api/jobs/' + r.job);
      rpaShowProgress(j);
      if (j.status !== 'running') {
        if (j.status === 'done' && j.result?.run) {
          RPA.lastRun = j.result.run;
          $('rpa-analyze').disabled = false;
        }
        break;
      }
      await new Promise(resolve => setTimeout(resolve, 1500));
    }
  } catch(e) { $('rpa-result').textContent = e.message; }
  finally { $('rpa-run').disabled = false; }
}
async function rpaAnalyze() {
  try {
    const r = await api('/api/rpa/context', {run:RPA.lastRun});
    let j;
    do {
      await new Promise(resolve => setTimeout(resolve, 1500));
      j = await api('/api/jobs/' + r.job);
      rpaShowProgress(j);
    } while (j.status === 'running');
    if (j.status !== 'done') throw new Error(j.error || 'Анализ не завершён');
    $('dg-extra').value = j.result.text;
    go('digest');
  } catch(e) { $('rpa-result').textContent = e.message; }
}

function rpaShowProgress(j) {
  const state = j.cancelled ? 'Остановлено' : ({running: 'Выполняется', done: 'Завершено', error: 'Ошибка'}[j.status] || j.status);
  const lines = [state + ' · ' + Math.round(j.seconds || 0) + ' сек.', ''];
  const log = j.log || [];
  const trace = log.findIndex(line => line.startsWith('Traceback (most recent call last):'));
  lines.push(...(trace < 0 ? log : log.slice(0, trace)));
  if (j.error) lines.push('', 'Ошибка: ' + j.error);
  if (j.status === 'done' && j.result?.directory) {
    lines.push('', 'Файлов сохранено: ' + (j.result.files || []).length, 'Папка: ' + j.result.directory);
  }
  const el = $('rpa-result');
  const follow = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  el.textContent = lines.join('\n');
  if (follow) el.scrollTop = el.scrollHeight;
}
