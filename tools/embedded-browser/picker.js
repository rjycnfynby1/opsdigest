(() => {
  if (window.__opsPicker) return;
  window.__opsPicker = true;
  let marked, previous;
  const eligible = e => e instanceof Element && !e.closest('input[type=password],input[type=hidden]');
  document.addEventListener('mousemove', ev => {
    if (marked) marked.style.outline = previous;
    marked = null;
    if (!ev.altKey || !eligible(ev.target)) return;
    marked = ev.target; previous = marked.style.outline;
    marked.style.outline = '3px solid #2563eb';
  }, true);
  document.addEventListener('click', ev => {
    if (!ev.altKey) return;
    ev.preventDefault(); ev.stopImmediatePropagation();
    const e = ev.target;
    if (!eligible(e)) return;
    const name = (e.getAttribute('aria-label') || e.labels?.[0]?.innerText || e.getAttribute('placeholder') || e.innerText || '').trim().slice(0,160);
    // No form values, query strings, fragments or cookies in saved descriptors.
    const descriptor = {version:1, kind:'element', page:location.origin + location.pathname,
      tag:e.tagName.toLowerCase(), role:e.getAttribute('role') || '', name,
      frame:window === top ? 'main' : 'child',
      text:(e.matches('input,textarea,select') ? '' : e.innerText || '').slice(0,2000)};
    window.chrome.webview.postMessage(descriptor);
  }, true);
})();
