// Тестовый стенд: имитация ExtJS-портала ITSM.
// У КАЖДОГО элемента id и классы случайные и меняются при каждой перерисовке,
// стабильных атрибутов нет — опереться можно только на видимый текст и расположение.
(function () {
  const R = () => Math.random().toString(36).slice(2, 8);
  window.rid = () => 'ext-gen' + Math.floor(Math.random() * 900000 + 100000);

  window.h = function (tag, style, text, attrs) {
    const el = document.createElement(tag);
    el.id = rid();
    el.className = 'x-' + R() + ' x-' + R();
    if (style) el.setAttribute('style', style);
    if (text != null) el.textContent = text;
    if (attrs) for (const k in attrs) {
      if (k.startsWith('on')) el.addEventListener(k.slice(2), attrs[k]);
      else el.setAttribute(k, attrs[k]);
    }
    return el;
  };
  window.ap = (p, ...cs) => { cs.forEach(c => c && p.appendChild(c)); return p; };
  window.db = () => window.top.__db;
  window.log = (s) => window.top.__db.log.push(s);

  // Кнопка «как в ExtJS 3»: table > tbody > tr > td > em > button > span
  window.extBtn = function (text, onclick, opts) {
    const t = h('table', 'display:inline-table;margin:2px;border-collapse:collapse;vertical-align:middle');
    const tb = h('tbody'), tr = h('tr'), td = h('td', 'padding:0'), em = h('em', 'font-style:normal');
    const b = h('button', 'padding:4px 10px;border:1px solid #99bbe8;background:linear-gradient(#fff,#dfe8f6);border-radius:3px;cursor:pointer;font:12px Arial');
    b.type = 'button';
    if (opts && opts.icon) { b.textContent = opts.icon; b.title = text; }   // кнопка-иконка, текст только в подсказке
    else ap(b, h('span', null, text));
    b.addEventListener('click', onclick);
    ap(t, ap(tb, ap(tr, ap(td, ap(em, b)))));
    return t;
  };

  // Выпадающий список «как в ExtJS»: input + кнопка-треугольник, список рисуется в body
  window.combo = function (value, options, onchange, opts) {
    opts = opts || {};
    const wrap = h('div', 'display:inline-flex;align-items:center');
    const inp = h('input', 'width:' + (opts.width || 220) + 'px;padding:3px;border:1px solid #b5b8c8', null, { readonly: 'readonly' });
    inp.value = value || '';
    const trg = h('div', 'width:17px;height:21px;border:1px solid #b5b8c8;border-left:0;background:#eee;cursor:pointer;text-align:center;font-size:10px;line-height:21px', '▼');
    const open = (e) => {
      e.stopPropagation();
      if (window.__openList) window.__openList.remove();
      const r = inp.getBoundingClientRect();
      const list = h('div', `position:absolute;left:${r.left + scrollX}px;top:${r.bottom + scrollY}px;width:${r.width + 17}px;background:#fff;border:1px solid #99bbe8;z-index:9999;box-shadow:0 2px 6px #999`);
      options.forEach(o => {
        const it = h('div', 'padding:4px;cursor:pointer', o);
        it.addEventListener('click', (ev) => {
          ev.stopPropagation(); inp.value = o; list.remove(); window.__openList = null; onchange && onchange(o);
        });
        it.addEventListener('mouseover', () => it.style.background = '#dfe8f6');
        it.addEventListener('mouseout', () => it.style.background = '');
        ap(list, it);
      });
      setTimeout(() => { document.body.appendChild(list); window.__openList = list; }, 150 + Math.random() * 200);
    };
    trg.addEventListener('click', open);
    if (!opts.triggerOnly) inp.addEventListener('click', open);   // у некоторых комбо список открывает только треугольник
    ap(wrap, inp, trg);
    wrap.input = inp;
    return wrap;
  };

  document.addEventListener('click', () => { if (window.__openList) { window.__openList.remove(); window.__openList = null; } });
})();
