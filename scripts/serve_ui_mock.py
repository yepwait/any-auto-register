"""Local-only static preview. This never imports the backend or connects remotely.

All /api calls are intercepted by the Playwright test; unmocked API requests
return 501 rather than forwarding to a real service.
"""
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit
import argparse

root = Path(__file__).resolve().parents[1] / 'static'

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(root), **kwargs)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith('/api/'):
            self.send_error(501, 'Unmocked API is intentionally disabled')
            return
        if not path.startswith('/assets/') and not (root / path.lstrip('/')).is_file():
            self.path = '/index.html'
        super().do_GET()

    def do_POST(self):
        self.send_error(501, 'Real actions are intentionally disabled')
    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18765)
    args = parser.parse_args()
    print(f'MOCK_STATIC_PREVIEW http://127.0.0.1:{args.port}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
