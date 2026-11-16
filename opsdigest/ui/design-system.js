// Shared semantics for existing and dynamically rendered form controls.
(() => {
  function associate() {
    document.querySelectorAll('label:not([for])').forEach(label => {
      if (label.querySelector('input,select,textarea')) return;
      const next = label.nextElementSibling;
      const fields = next?.matches('input[id],select[id],textarea[id]') ? [next] :
        (next?.classList.contains('row') ? next.querySelectorAll('input[id],select[id],textarea[id]') : []);
      if (fields?.length === 1) label.htmlFor = fields[0].id;
    });
    document.querySelectorAll('.msg').forEach(el => {
      el.setAttribute('role', 'status'); el.setAttribute('aria-live', 'polite');
    });
    document.querySelectorAll('nav button').forEach(el => {
      if (!el.hasAttribute('aria-label')) el.setAttribute('aria-label', el.textContent.trim().replace(/\s+/g,' '));
    });
    document.querySelectorAll('input[placeholder],textarea[placeholder]').forEach(el => {
      if (!el.labels?.length && !el.hasAttribute('aria-label') && !el.hasAttribute('aria-labelledby'))
        el.setAttribute('aria-label', el.placeholder);
    });
  }
  associate();
  new MutationObserver(associate).observe(document.body, {childList:true, subtree:true});
})();
