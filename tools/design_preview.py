"""Isolated UI fixture server. No user workspace, accounts, or external calls."""
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1] / 'opsdigest' / 'ui'


class PreviewServer(ThreadingHTTPServer):
    request_queue_size = 128


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith('/api/'):
            if path == '/api/state':
                data = dict(workspace='', default_workspace='Тестовая папка', steps={},
                            version='UX fixture', ui={'modules': dict(mx=True, digest=True, rpa=True, asst=True, gl=True)})
            elif path == '/api/rpa/templates':
                time.sleep(.8)
                data = dict(templates={}, fields={'number': 'Номер', 'created_after': 'Создан после'}, statuses=['В работе'])
            elif path == '/api/ping':
                data = dict(busy=False, boot='fixture')
            elif path == '/api/glossary':
                data = dict(terms=[], kinds={}, questions=[], counts={})
            elif path == '/api/glossary/queue':
                data = dict(items=[], counts={})
            elif path == '/api/asst/threads':
                data = dict(threads=[])
            elif path == '/api/plugins':
                data = dict(available=False, status={}, settings={}, glossary={})
            elif path == '/api/chats/list':
                data = dict(chats=[], rules=[])
            elif path == '/api/schedules':
                data = dict(schedules=[], presets=[])
            elif path == '/api/skill':
                data = dict(rules=[], links=[], journal=[], feedback=[], questions=[], counts={})
            elif path == '/api/tg/state':
                data = dict(state='offline')
            elif path == '/api/tg/dialogs':
                data = dict(chats=[], folders=[], status=dict(state='offline'))
            elif path == '/api/tg/events':
                time.sleep(2)
                data = dict(seq=0, state='offline', events=[])
            elif path == '/api/telegram/chats':
                data = dict(chats=[])
            elif path == '/api/reports':
                data = dict(reports=[])
            else:
                data = {}
            body = json.dumps(dict(ok=True, **data), ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
        else:
            file = ROOT / (path.removeprefix('/static/') if path.startswith('/static/') else 'index.html')
            if file.parent != ROOT or not file.is_file():
                self.send_error(404)
                return
            body = file.read_bytes()
            if file.name == 'index.html':
                # Keep real markup/functions; expose navigation on this synthetic preview.
                body = body.replace(b'</body>', b"<script>setTimeout(()=>{document.getElementById('modes').style.display='flex'},500)</script></body>")
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8' if file.suffix == '.html' else
                             'text/css' if file.suffix == '.css' else 'application/javascript')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionError):
            pass  # A preview tab may close while the synthetic event poll is pending.

    def do_POST(self):
        # Deliberate recoverable error; never write configuration or call portals.
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        body = json.dumps(dict(ok=False, error='Тестовый сервис недоступен. Повторите действие.'), ensure_ascii=False).encode()
        self.send_response(503)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    PreviewServer(('127.0.0.1', 8879), Handler).serve_forever()
