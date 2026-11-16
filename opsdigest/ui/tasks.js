// OpsDigest — связь окна с программой и окно «Задачи».
// 1) Программа перезапустилась (сбой, обновление) или компьютер проснулся — окно не говорит «запустите заново»,
//    а ждёт, пока сервер снова ответит (сторож поднимает его сам), и перечитывает состояние.
// 2) «Задачи»: долгие задачи (выгрузка истории, изображения, ИИ-описания, глоссарий), которые программа доводит
//    до конца — после перезапуска и после ошибок; пауза, продолжить, повторить сейчас, убрать; сон компьютера.

let LINK = {down: false, since: 0, boot: null, waiter: null};
const RECONNECT_MAX = 5 * 60000;   // столько ждём, пока программа поднимется (после сна сеть и диск оживают не сразу)

function linkBanner(text, kind) {
  const b = $('link-banner'); if (!b) return;
  b.className = 'link-banner' + (text ? ' on ' + (kind || '') : '');
  b.innerHTML = text || '';
}
async function rawPing() {
  const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), 6000);
  try {
    const r = await fetch('/api/ping', {headers: {'X-Token': T}, signal: ctl.signal});
    const j = await r.json(); return j && j.ok ? j : null;
  } catch (e) { return null; } finally { clearTimeout(t); }
}
// Ждать, пока программа ответит. Одна попытка на все запросы окна, которые сейчас не прошли.
function reconnect() {
  if (LINK.waiter) return LINK.waiter;
  LINK.down = true; LINK.since = Date.now();
  linkBanner('<span class="spin"></span>Связь с программой прервалась (перезапуск или сон компьютера) — переподключаюсь…', 'warn');
  LINK.waiter = (async () => {
    while (Date.now() - LINK.since < RECONNECT_MAX) {
      const j = await rawPing();
      if (j) {
        const restarted = LINK.boot && j.boot && j.boot !== LINK.boot;
        LINK.boot = j.boot || LINK.boot; LINK.down = false; LINK.waiter = null;
        linkBanner(restarted ? '✓ Программа перезапустилась — задачи продолжаются сами.' : '', 'ok');
        if (restarted) { setTimeout(() => linkBanner(''), 6000); afterRestart(j); } else linkBanner('');
        return true;
      }
      await new Promise(r => setTimeout(r, 2000));
    }
    LINK.waiter = null;
    linkBanner('OpsDigest не отвечает. Запустите OpsDigest.bat — окно подключится само. <button class="lnk" onclick="reconnect()">Подождать ещё</button>', 'bad');
    return false;
  })();
  return LINK.waiter;
}
async function afterRestart(j) {
  try {
    if (j.locked) { lockState(j.locked); return; }
    await refresh();
    if (typeof mxAuto === 'function' && document.querySelector('.main.mx-on')) mxAuto();
  } catch (e) { /* следующий запрос окна попробует снова */ }
}
// Окно снова видно (разблокировали компьютер, развернули) — сразу проверить связь, не дожидаясь таймера.
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') linkCheck(); });
window.addEventListener('online', () => linkCheck());
async function linkCheck() {
  let j = await rawPing();
  if (!j && !LINK.down) { await new Promise(r => setTimeout(r, 1500)); j = await rawPing(); }  // одна задержка — ещё не обрыв
  if (!j) return reconnect();
  if (LINK.boot && j.boot && j.boot !== LINK.boot) { LINK.boot = j.boot; afterRestart(j); }
  LINK.boot = j.boot || LINK.boot;
  if (j.locked && j.locked !== LOCKED) lockState(j.locked);
  if (j.tasks !== undefined) tasksBadge((j.tasks || 0) + (j.jobs || 0));
}

// ---------------------------------------------------------------- окно «Задачи»
const TK_KIND = {export: '⬇', images: '🖼', describe: '✦', glossary: '📖'};
const TK_ST = {running: ['идёт', 'ok'], queued: ['ждёт запуска', 'info'], retry: ['ошибка — повторю', 'warn'],
  paused: ['на паузе', ''], done: ['готово', 'ok']};
function tasksBadge(n) {
  const b = $('tk-btn'); if (!b) return;
  b.querySelector('.cnt').textContent = n ? String(n) : '';
  b.classList.toggle('has', !!n);
}
function tkAgo(iso) {
  if (!iso) return ''; const d = new Date(iso);
  return d.toLocaleString('ru-RU', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'});
}
function tkMin(s) { return s >= 90 ? Math.round(s / 60) + ' мин' : Math.max(1, s) + ' с'; }
async function tasksOpen() { $('tk-modal').classList.add('on'); await tasksLoad(); }
function tasksClose() { $('tk-modal').classList.remove('on'); }
const TK_JOB_IC = {digest: '📋', demo: '📋', asst: '✦', ai: '💬', ask: '❓', plugins: '🔌', feedback: '📝', gl: '📖', vision: '🖼'};
function tkJobRow(x) {
  const open = x.kind === 'digest' || x.kind === 'demo' ? `<button class="b" onclick="tasksClose();go('digest')" title="Показать ход сводки">Показать</button>` :
    x.kind === 'asst' && x.info && x.info.thread ? `<button class="b" onclick="tasksClose();asOpenThread(${Number(x.info.thread)})" title="Открыть беседу">Открыть</button>` : '';
  const stop = x.keep ? '' : `<button class="b" onclick="jobStop('${x.id}', this)" title="Остановить: запросы к модели оборвутся, открытые ею сессии Maverick закроются">⏹ Остановить</button>`;
  return `<div class="tk-row tk-short"><div class="tk-ic">${TK_JOB_IC[x.kind] || '▶'}</div><div class="tk-b"><b>${esc(x.title)}</b>` +
    `<span class="pill tk-ok">${x.cancelled ? 'останавливается' : 'идёт'} · ${tkMin(x.seconds)}</span>` +
    `<small>${esc((x.info && x.info.period) || '')}${x.info && x.info.period && x.last ? ' · ' : ''}${esc(x.last || '')}</small></div><div class="tk-a">${open}${stop}</div></div>`;
}
async function jobStop(id, btn) {
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin"></span>Останавливаю…'; }
  try { await api(`/api/jobs/${id}/stop`, {}); } catch (e) { alert(e.message); }
  setTimeout(() => tasksLoad(), 400);
}
async function tasksLoad() {
  let j; try { j = await api('/api/tasks'); } catch (e) { $('tk-list').innerHTML = `<div class="msg bad" style="display:block">${esc(e.message)}</div>`; return; }
  const rows = j.tasks || [];
  const short = (j.running || []).filter(x => !x.long);
  tasksBadge(rows.filter(t => ['running', 'queued', 'retry'].includes(t.status)).length + short.length);
  $('tk-list').innerHTML = short.map(tkJobRow).join('') + (rows.length ? rows.map(t => {
    const [st, cls] = TK_ST[t.status] || [t.status, ''];
    let sub = '';
    if (t.status === 'retry') sub = `через ${tkMin(t.retry_in || 0)} (попытка ${(t.attempts || 0) + 1}) · ${esc(t.error || '')}`;
    else if (t.status === 'running') sub = (t.attempts > 1 ? `запуск ${t.attempts} — продолжает с места остановки` : 'с ' + tkAgo(t.started));
    else if (t.status === 'queued') sub = 'запустится, как только данные открыты';
    else if (t.status === 'paused') sub = 'остановлена вами ' + tkAgo(t.paused_at) + (t.last ? ' · ' + esc(t.last) : '');
    else if (t.status === 'done') sub = tkAgo(t.done_at) + (t.result && t.result.message ? ' · ' + esc(t.result.message) : '') +
      (t.attempts > 1 ? ` · запусков: ${t.attempts}` : '');
    const btn = (act, label, title) => `<button class="b" onclick="taskAct('${t.id}','${act}')" title="${title}">${label}</button>`;
    const acts = (['running', 'queued', 'retry'].includes(t.status) ? btn('pause', '⏸ Остановить', 'Пауза: сама не продолжится, пока не нажмёте «Продолжить»') : '') +
      (t.status === 'retry' ? btn('now', '↻ Сейчас', 'Повторить сейчас, не дожидаясь паузы') : '') +
      (t.status === 'paused' ? btn('resume', '▶ Продолжить', 'Продолжить с места остановки') : '') +
      (['paused', 'done', 'retry', 'queued'].includes(t.status) ? btn('remove', '✕', 'Убрать из списка (сделанное сохранится)') : '');
    return `<div class="tk-row"><div class="tk-ic">${TK_KIND[t.kind] || '•'}</div><div class="tk-b"><b>${esc(t.title || t.kind)}</b>` +
      `<span class="pill tk-${cls}">${st}</span><small>${sub}</small></div><div class="tk-a">${acts}</div></div>`;
  }).join('') : short.length ? '' : '<div class="hint">Пока нет задач. Выгрузка истории, «Прочитать изображения», ИИ-описания и пополнение глоссария появятся здесь и будут доведены до конца, даже если программа перезапустится. Сводка и ответы ассистента, пока идут, тоже видны здесь — их можно остановить.</div>');
  $('tk-awake').checked = !!j.keep_awake; $('tk-awake-row').style.display = j.power_supported ? '' : 'none';
  $('tk-awake-now').textContent = j.awake ? ('сейчас: сон запрещён' + (j.display ? ', экран не гаснет' : '')) : '';
  if ($('tk-display')) { $('tk-display').value = j.display_mode || 'auto'; $('tk-display').disabled = !j.keep_awake; }
  const pw = j.power || {};
  $('tk-power').textContent = Object.keys(pw).length ? 'Этот компьютер: ' + [pw.modern_standby === true ? 'Modern Standby (сон вместе с экраном)' :
    pw.modern_standby === false ? 'обычный сон (S3)' : '', pw.ac === true ? 'от сети' : pw.ac === false ? 'от батареи' : '',
    pw.battery !== undefined ? `заряд ${pw.battery}%` : ''].filter(Boolean).join(', ') + '.' : '';
  const sl = j.sleeps || [];
  $('tk-sleeps').innerHTML = sl.length ? 'Компьютер спал (программа не работала): ' + sl.slice(-3).reverse().map(x =>
    `${esc(tkAgo(x.from))}–${esc(new Date(x.to).toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit'}))} (${x.minutes} мин` +
    (x.jobs && x.jobs.length ? `, шли: ${esc(x.jobs.join(', '))}` : '') + (x.awake ? (x.display ? ', хотя экран держали' : ', хотя сон был запрещён') : '') + ')').join('; ') +
    (j.slept_while_busy && j.display_mode === 'auto' ? ' — теперь экран не гаснет, пока идут задачи.' : '') : '';
  $('tk-now').innerHTML = '';
  const a = j.autostart || {}; $('tk-auto').checked = !!a.enabled; $('tk-auto-row').style.display = a.supported ? '' : 'none';
  $('tk-sup').style.display = j.supervised ? 'none' : '';
}
async function taskAct(id, act) {
  try { await api(`/api/tasks/${id}/${act}`, {}); } catch (e) { alert(e.message); }
  await tasksLoad(); if (act === 'resume' || act === 'now') setTimeout(() => { refresh().catch(() => {}); }, 600);
}
async function tasksStopAll() {
  if (!confirm('Остановить все задачи? Долгие (выгрузка, ИИ-описания, глоссарий) встанут на паузу — продолжить можно здесь же. ' +
    'Идущие запросы к моделям будут оборваны, открытые сессии Maverick — закрыты.')) return;
  busy('tk-stop-all', true, 'Останавливаю…');
  try { const r = await api('/api/jobs/stop_all', {});
    const box = $('tk-stop-log'); box.classList.add('on');
    box.textContent = (r.log || []).join('\n') || 'Нечего останавливать.';
  } catch (e) { alert(e.message); }
  busy('tk-stop-all', false); await tasksLoad();
}
async function tasksSet(k, v) {
  try { await api('/api/tasks/settings', {[k]: v}); } catch (e) { alert(e.message); }
  await tasksLoad();
}
setInterval(() => { if ($('tk-modal') && $('tk-modal').classList.contains('on') && !LINK.down) tasksLoad(); }, 4000);
