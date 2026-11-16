// OpsDigest — «Сводка» → «От источника»: разбор начинается с ITSM (очередь группы) или с трекера, а Telegram и почта
// проверяются только по номерам из очереди. Пользуется помощниками окна и digest.js: api, $, esc, msg, DG, dgLS, pstHTML,
// srcStatus, SRC.
'use strict';
const SM = {start: 'tracker', inited: false};

function dgSmInit() {
  if (SM.inited) return; SM.inited = true;
  document.querySelectorAll('#dg-mode button').forEach(b => b.onclick = () => { DG.preset = ''; dgPresetsRender(); dgModeSet(b.dataset.v); });
  document.querySelectorAll('#dg-sm-start button').forEach(b => b.onclick = () => { dgSmStart(b.dataset.v); dgSmSave(); });
  ['dg-sm-group', 'dg-sm-text', 'dg-sm-limit', 'dg-sm-days', 'dg-sm-state'].forEach(id => $(id).addEventListener('change', () => { dgSmSave(); dgSmSum(); }));
  ['dg-sm-from', 'dg-sm-to'].forEach(id => $(id).addEventListener('change', () => { dgSmSave(); dgSmSum(); }));
  ['dg-sm-group', 'dg-sm-text'].forEach(id => $(id).addEventListener('input', dgSmSum));
  document.querySelectorAll('#dg-smright input[type=checkbox], #dg-sm-types input').forEach(x => x.addEventListener('change', () => { dgSmSave(); dgSmSum(); }));
  try { dgSmFill(JSON.parse(dgLS('od_dg_sm') || '{}')); } catch (e) { dgSmFill({}); }
  dgModeSet(dgLS('od_dg_mode') === 'source' ? 'source' : 'tg', true);
}

function dgModeSet(v, quiet) {
  DG.mode = v === 'source' ? 'source' : 'tg';
  document.querySelectorAll('#dg-mode button').forEach(x => x.classList.toggle('on', x.dataset.v === DG.mode));
  const on = DG.mode === 'source';
  document.body.classList.toggle('dg-src-on', on);
  $('dg-sm').style.display = on ? '' : 'none'; $('dg-smright').style.display = on ? '' : 'none';
  $('dg-lead').textContent = on
    ? 'Разбор начинается с ITSM или трекера: список записей, карточки, связанные задачи и их статусы; Telegram и почта — только по номерам. Откроется отчёт: цифры, группы, что требует внимания, что можно закрыть.'
    : 'Соберите сводку по чатам за период — откроется отчёт: главное, где нужна ваша реакция (с черновиками ответов), аварии, номера инцидентов.';
  $('dg-extra-l').innerHTML = on ? 'Вопрос и указания к разбору <span class="hint">— что посчитать, на что обратить внимание; ответ будет в начале отчёта</span>'
    : 'Дополнительно для этой сводки <span class="hint">— на что обратить внимание, что опустить</span>';
  $('dg-extra').placeholder = on ? 'Например: сколько инцидентов ждут релиза 5.3? какие висят дольше двух недель? по каждому, где ждут от нас, — что написать пользователю.'
    : 'Например: особенно — всё про релиз 5.2 и биллинг; опусти поздравления и обеды; черновики ответов — короче. Пусто — обычная сводка.';
  if (!DG.job) $('dg-btn').textContent = on ? 'Разобрать очередь' : 'Собрать сводку';
  if (!quiet) dgLS('od_dg_mode', DG.mode);
  if (on) { dgSmPst(); dgSmSum(); }
  readiness();
}

function dgSmStart(v) {
  SM.start = 'tracker';
  document.querySelectorAll('#dg-sm-start button').forEach(x => x.classList.toggle('on', x.dataset.v === SM.start));
  const it = SM.start === 'itsm';
  $('dg-sm-group-l').textContent = it ? 'Группа исполнителей' : 'Проект, доска, фильтр или исполнитель';
  $('dg-sm-group').placeholder = it ? 'Например: L2-Биллинг' : 'Например: проект BILL, доска «Платежи», мои задачи';
  $('dg-sm-types').style.display = it ? '' : 'none';
  $('dg-sm-second-t').textContent = it ? '🗂 Задачи трекера из инцидентов — статус, релиз, кто сейчас действует'
    : '🎫 Инциденты ITSM, связанные с задачами — статус, ожидание';
  const p = document.querySelector('#dg-smright [data-pst-sm]'); if (p) p.dataset.pstSm = it ? 'tracker' : 'itsm';
  document.querySelectorAll('.dg-sm-s1').forEach(x => x.textContent = it ? 'ITSM' : 'трекера');
  document.querySelectorAll('.dg-sm-s2').forEach(x => x.textContent = it ? 'задачи трекера из них' : 'инциденты ITSM, связанные с задачами');
  dgSmPst(); dgSmSum();
}

function dgSmFill(s) {
  s = s || {}; const q = s.query || {}, l = s.look || {};
  $('dg-sm-group').value = q.group || ''; $('dg-sm-text').value = q.text || ''; $('dg-sm-state').value = q.state || 'open';
  $('dg-sm-limit').value = q.limit || 60; $('dg-sm-days').value = l.days || 30;
  $('dg-sm-from').value = q.from || ''; $('dg-sm-to').value = q.to || '';
  const types = q.types && q.types.length ? q.types : ['incident'];
  document.querySelectorAll('#dg-sm-types [data-t]').forEach(x => { x.checked = types.includes(x.dataset.t); });
  $('dg-sm-second').checked = l.second !== false; $('dg-sm-tg').checked = l.telegram !== false; $('dg-sm-mail').checked = l.mail !== false;
  $('dg-sm-rooms').checked = !!l.rooms; $('dg-sm-meet').checked = !!l.meet;
  dgSmStart(s.start || 'itsm');
}
function dgSmValues() {
  const types = [...document.querySelectorAll('#dg-sm-types [data-t]')].filter(x => x.checked).map(x => x.dataset.t);
  return {start: SM.start,
    query: {group: $('dg-sm-group').value.trim(), state: $('dg-sm-state').value, text: $('dg-sm-text').value.trim(),
      types: SM.start === 'itsm' ? (types.length ? types : ['incident']) : undefined, limit: Number($('dg-sm-limit').value) || 60,
      from: $('dg-sm-from').value || undefined, to: $('dg-sm-to').value || undefined},
    look: {second: $('dg-sm-second').checked, telegram: $('dg-sm-tg').checked, mail: $('dg-sm-mail').checked,
      rooms: $('dg-sm-rooms').checked, meet: $('dg-sm-meet').checked, days: Number($('dg-sm-days').value) || 30}};
}
function dgSmSave() { dgLS('od_dg_sm', JSON.stringify(dgSmValues())); }
function dgSmBody() {
  return Object.assign({mode: 'source'}, dgSmValues(), {send: $('dg-send').checked, sync: $('dg-sync').checked && $('dg-sm-tg').checked,
    preempt: $('dg-preempt').checked, extra_prompt: $('dg-extra').value.trim()});
}
function dgSmSum() {
  const v = dgSmValues(), q = v.query, it = v.start === 'itsm';
  const T = {incident: 'инциденты', request: 'запросы', problem: 'проблемы', change: 'изменения'};
  const S = {open: 'все незакрытые', waiting: 'в ожидании', work: 'в работе', new: 'новые'};
  const what = it ? (q.types || []).map(t => T[t]).join(', ') : 'задачи трекера';
  $('dg-sm-sum').textContent = `Будет разобрано: ${what} · ${S[q.state]}` + (q.group ? (it ? ` · группа «${q.group}»` : ` · ${q.group}`) : '') +
    (q.from ? ` · с ${q.from.replace('T', ' ')}${q.to ? ' до ' + q.to.replace('T', ' ') : ''}` : '') +
    (q.text ? ` · ${q.text}` : '') + ` · до ${q.limit} записей.` + (!q.group && !q.text && !q.from ? ' Условия не заданы — Maverick вернёт все подходящие записи, которые вам видны.' : '');
}
async function dgSmPst() {
  try { await srcStatus(); } catch (e) {}
  document.querySelectorAll('#dg-smright [data-pst-sm]').forEach(x => { x.innerHTML = pstHTML(x.dataset.pstSm); });
  const d = SRC.data, it = SM.start;
  let w = '';
  if (!d || !d.available) w = 'Для разбора от источника нужен Maverick с плагинами: «Настройки → Модель» → Maverick, затем «Настройки → Источники».';
  else if (!d.status[it] || !d.status[it].enabled) w = `${SRC_RU[it]} выключен в «Настройки → Источники» — включите его.`;
  else if (['offline', 'error'].includes(d.status[it].state)) w = `${SRC_RU[it]}: при последней проверке плагин не отвечал — переподключите его в чате Maverick. Перед разбором подключение проверится ещё раз.`;
  msg('dg-sm-warn', w, 'warn');
  return !w || (d && d.available && d.status[it] && d.status[it].enabled);
}
function dgSmReady() {
  const d = (typeof SRC !== 'undefined' && SRC.data) || null;
  return !d || (d.available && d.status[SM.start] && d.status[SM.start].enabled);
}

// окно «Проверьте перед запуском» для разбора от источника
function dgCfRenderSource(j) {
  const L = j.look || {};
  const rows = Object.entries(j.sources || {}).map(([k, v]) =>
    `<div>${SRC_IC[k] || (k === 'telegram' ? '💬' : '')} ${esc(v.short)}${v.role ? ` <small class="hint" style="margin:0">— ${esc(v.role)}</small>` : ''}` +
    (k === 'telegram' ? '' :
      ` <span class="pst-i ${esc(v.state)}" title="${esc(v.detail || '')}">${esc(PST_RU[v.state] || v.state)}</span>` +
      (v.at ? ` <small class="hint" style="margin:0">проверено ${esc(dgAgo(v.at))}</small>` : '')) + '</div>').join('');
  const stops = (j.will_stop || []);
  $('dg-cf-body').innerHTML = `<div class="dg-cf">` +
    `<div>Начать с</div><div><b>${esc(j.start_title)}</b></div>` +
    `<div>Что разбираем</div><div><b>${esc(j.query_text)}</b>` + (j.query.limit ? ` <span class="hint" style="margin:0">· до ${j.query.limit} записей</span>` : '') + '</div>' +
    `<div>Источники</div><div class="dg-cf-src">${rows}</div>` +
    `<div>Упоминания</div><div>за последние <b>${L.days}</b> дн.` + (L.telegram ? ` · в базе Telegram за это время <b>${j.tg_messages}</b> сообщений` +
      (j.sync ? ' · сначала заберу новые' : '') : ' · Telegram не проверяю') + '</div>' +
    `<div>Модель</div><div>${j.model ? esc(j.model.name) + (j.model.ok ? ' ✓' : ' <span style="color:var(--warn)">— не проверена</span>') : '—'}</div>` +
    `<div>Указания</div><div>${j.extra_prompt ? `<div class="extra">${esc(j.extra_prompt)}</div>` : '<span class="hint" style="margin:0">нет</span>'}</div>` +
    `<div>Перед запуском</div><div>` + (j.preempt ? (stops.length ? 'остановлю: ' + stops.map(x => esc(x.title) + (x.long ? ' (пауза до конца разбора)' : '')).join(', ')
      : 'других задач нет') : 'другие задачи не трогаю') + (j.send ? ' · пришлю в «Избранное» Telegram' : '') + '</div>' +
    `</div><div class="dg-cf-w">` + (j.warnings || []).map(w => `<div class="msg ${esc(w.level)}">${esc(w.text)}</div>`).join('') + '</div>';
  const blocking = (j.warnings || []).some(w => w.level === 'bad' && w.blocking);
  const bad = (j.warnings || []).some(w => w.level === 'bad');
  $('dg-cf-go').disabled = blocking;
  $('dg-cf-go').textContent = bad ? 'Запустить всё равно' : 'Всё верно — запустить';
  $('dg-cf-check').style.display = j.maverick ? '' : 'none';
  DG.cfKinds = Object.keys(j.sources || {}).filter(k => k !== 'telegram');
}

// итог разбора в разделе «Сводка»
function dgDoneSource(x) {
  const s = x.source || {};
  const OFF = (s.offline || []).map(k => SRC_RU[k] || k);
  return `<h3>Готово за ${Math.round(x.seconds || 0)} с: записей ${s.items}` + (s.total && s.total !== s.items ? ` из ${s.total}` : '') +
    `, связанных ${s.secondary}, групп ${s.groups}, требуют внимания — ${s.attention}</h3>` +
    `<div>${esc(x.headline)}</div>` +
    `<div class="hint">Обсуждались в Telegram: ${s.in_telegram} · в почте: ${s.in_mail}</div>` +
    (s.truncated ? `<div class="msg warn" style="display:block">Разобраны не все записи — увеличьте «не больше N записей» или сузьте условия.</div>` : '') +
    (OFF.length ? `<div class="msg warn" style="display:block">Не подключены или выключены: ${esc(OFF.join(', '))} — без них разбор неполный.</div>` : '');
}

function dgPresetSourceText(s) {
  const T = {itsm: 'ITSM', tracker: 'Трекер'};
  const q = s.query || {};
  return `${T[s.start] || s.start}: ${q.group || 'все'}`;
}
