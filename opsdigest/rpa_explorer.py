"""Read-only browser explorer. Playwright objects stay on their owner thread."""
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rpa-explorer")
_portal = None
_workspace = None

SCAN = r"""() => Array.from(document.querySelectorAll('button,a,input,textarea,select,[role="button"],[role="grid"],table,[role="textbox"]'))
.filter(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')
.filter(e => !e.matches('input[type="password"],input[type="hidden"]'))
.slice(0,500).map(e => ({tag:e.tagName.toLowerCase(), role:e.getAttribute('role') || '',
name:(e.getAttribute('aria-label') || (e.labels && e.labels[0]?.innerText) || e.getAttribute('placeholder') || e.innerText || '').trim().slice(0,160),
text:(e.matches('input,textarea,select') ? e.value : e.innerText || '').slice(0,2000),
scrollable:e.scrollHeight > e.clientHeight + 5, disabled:!!e.disabled}))"""


def request(ws, action, url="", channel="msedge"):
    return _worker.submit(_request, ws, action, url, channel).result(timeout=60)


def _request(ws, action, url, channel):
    global _portal, _workspace
    if action == "close":
        if _portal:
            _portal.close()
        _portal = _workspace = None
        return {"closed": True}
    if action == "open":
        if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).hostname:
            raise ValueError("Укажите адрес http:// или https://")
        if channel not in ("msedge", "chrome"):
            raise ValueError("Выберите Edge или Chrome")
        if _portal:
            raise ValueError("Сначала закройте текущую сессию разведки")
        from itsm_rpa import Portal
        _portal = Portal.launch(url, profile_dir=ws / "rpa_explorer_profile", channel=channel)
        _workspace = ws
    if action not in ("open", "scan"):
        raise ValueError("Неизвестное действие разведчика")
    if not _portal or _workspace != ws:
        raise ValueError("Сначала откройте браузер разведки")
    pages = []
    for page in _portal.page.context.pages:
        frames = []
        for frame in page.frames:
            try:
                if frame.parent_frame:
                    host = frame.frame_element()
                    if not host.is_visible():
                        continue
                frames.append({"url": frame.url, "name": frame.name,
                               "parent": frame.parent_frame.url if frame.parent_frame else None,
                               "elements": frame.evaluate(SCAN), "limit": 500})
            except Exception:
                frames.append({"url": frame.url, "error": "Фрейм изменился или недоступен; повторите исследование"})
        pages.append({"url": page.url, "frames": frames})
    return {"pages": pages, "notice": "Снимок видимых элементов; до 500 на фрейм, текст до 2000 символов. Полнота страницы не гарантируется. Данные не сохраняются."}
