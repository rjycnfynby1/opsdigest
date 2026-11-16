"""Desktop UX regressions against synthetic data; never opens real accounts."""
import importlib.util
import os
import re
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def preview_url():
    spec = importlib.util.spec_from_file_location('design_preview', ROOT / 'tools/design_preview.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = module.PreviewServer(('127.0.0.1', 0), module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}/#t=fixture'
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


@pytest.mark.parametrize('width,height', [(1440, 900), (1024, 768)])
def test_desktop_sections(preview_url, width, height):
    from playwright.sync_api import sync_playwright, expect
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport=dict(width=width, height=height))
        errors = []
        page.on('pageerror', lambda error: errors.append(error.stack))
        page.goto(preview_url)
        expect(page.locator('.main')).to_have_class(re.compile('mode-set'))
        for mode in ('mx', 'digest', 'rpa', 'asst', 'gl', 'set'):
            page.locator('#mode-' + mode).click()
            expect(page.locator('.main')).to_have_class(re.compile('mode-' + mode))
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.locator('#mode-asst').click()
        expect(page.locator('#as-list')).to_contain_text('Здесь будет история')
        expect(page.locator('#as-msgs')).to_contain_text('Ассистент')
        page.locator('#as-in').fill('Тестовый вопрос')
        page.once('dialog', lambda dialog: dialog.dismiss())
        page.locator('#as-in').press('Enter')
        expect(page.locator('#as-in')).to_have_value('Тестовый вопрос')
        page.emulate_media(reduced_motion='reduce')
        assert page.locator('#as-send').evaluate('(e)=>getComputedStyle(e).transitionDuration') == '0s'
        # Optional screenshot evidence, always synthetic and outside the repository.
        if os.environ.get('OPSDIGEST_QA_DIR'):
            out = Path(os.environ['OPSDIGEST_QA_DIR'])
            out.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(out / f'assistant-{width}.jpg'))
        assert not errors, errors
        browser.close()


def test_rpa_loading_edit_error_and_keyboard(preview_url):
    from playwright.sync_api import sync_playwright, expect
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport=dict(width=1440, height=900))
        page.goto(preview_url)
        expect(page.locator('.main')).to_have_class(re.compile('mode-set'))
        page.locator('#mode-rpa').click()
        expect(page.locator('#rpa-result')).to_contain_text('Загружаю')
        expect(page.locator('#rpa-fields')).to_have_attribute('aria-busy', 'false')
        expect(page.locator('#rpa-run')).to_be_disabled()
        # Reload while editing the already displayed fields: preserve pending edits.
        page.locator('#mode-rpa').click()
        page.get_by_label('Номер', exact=True).fill('INC-TEST-42')
        expect(page.locator('#rpa-fields')).to_have_attribute('aria-busy', 'false')
        expect(page.get_by_label('Номер', exact=True)).to_have_value('INC-TEST-42')
        page.get_by_role('button', name='Добавить инциденты в очередь', exact=True).click()
        expect(page.locator('#rpa-queue')).to_contain_text('INC-TEST-42')
        expect(page.locator('#rpa-run')).to_be_enabled()
        page.get_by_label('Имя шаблона', exact=True).fill('Проверка')
        page.get_by_label('Имя шаблона', exact=True).press('Tab')
        expect(page.get_by_role('button', name='Сохранить шаблон', exact=True)).to_be_focused()
        assert page.evaluate('parseFloat(getComputedStyle(document.activeElement).outlineWidth)') >= 2
        page.keyboard.press('Enter')
        expect(page.locator('#rpa-result')).to_contain_text('Тестовый сервис недоступен')
        expect(page.get_by_label('Имя шаблона', exact=True)).to_have_value('Проверка')
        expect(page.get_by_label('Номер', exact=True)).to_have_value('INC-TEST-42')
        page.get_by_role('button', name='Удалить', exact=True).click()
        expect(page.locator('#rpa-run')).to_be_disabled()
        browser.close()
