"""Real installed Edge; no corporate endpoints or credentials."""
from pathlib import Path
from types import SimpleNamespace

from opsdigest import rpa_explorer as explorer


def test_real_browser_snapshot():
    def run():
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True)
            try:
                context = browser.new_context()
                page = context.new_page()
                page.set_content('''<label>Name<input value="Alice"></label>
                    <input type="password" value="secret-password">
                    <button style="display:none">Hidden</button>
                    <iframe srcdoc="&lt;button&gt;Nested&lt;/button&gt;"></iframe>''')
                page.frames[1].wait_for_selector('button')
                explorer._portal = SimpleNamespace(page=page)
                explorer._workspace = Path('.')
                result = explorer._request(Path('.'), 'scan', '', '')
                assert len(result['pages'][0]['frames']) == 2
                assert 'secret-password' not in str(result)
                assert 'Hidden' not in str(result)
                assert 'Nested' in str(result)
                page.locator('input').first.fill('Bob')
                assert 'Bob' in str(explorer._request(Path('.'), 'scan', '', ''))
                second = page.context.new_page()
                second.set_content('<a href="#">Second tab</a>')
                assert len(explorer._request(Path('.'), 'scan', '', '')['pages']) == 2
            finally:
                explorer._portal = explorer._workspace = None
                browser.close()
    explorer._worker.submit(run).result(timeout=45)
