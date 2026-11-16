"""Explorer lifecycle validation, independent of browser installation."""
import unittest
from pathlib import Path
from opsdigest import rpa_explorer as explorer


class ExplorerValidation(unittest.TestCase):
    def test_rejects_non_web_urls(self):
        for url in ('file:///C:/secret', 'javascript:alert(1)', '', 'https://'):
            with self.assertRaises(ValueError):
                explorer.request(Path('.'), 'open', url)

    def test_requires_session(self):
        explorer.request(Path('.'), 'close')
        with self.assertRaises(ValueError):
            explorer.request(Path('.'), 'scan')

    def test_rejects_unknown_action(self):
        with self.assertRaises(ValueError):
            explorer.request(Path('.'), 'click')

    def test_rejects_unknown_browser(self):
        with self.assertRaises(ValueError):
            explorer.request(Path('.'), 'open', 'https://example.com', 'unknown')
