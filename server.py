import json
import os
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from threading import BoundedSemaphore

ROOT = Path(__file__).resolve().parent
# Read only known local settings; never evaluate shell commands.
env_file = ROOT / '.env'
if env_file.exists():
    for line in env_file.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        if key.strip() in {'RELAY_API_KEY', 'PORT', 'RELAY_MAX_TOKENS'}:
            os.environ.setdefault(key.strip(), value.strip().strip('"\''))

KEY = os.environ.get('RELAY_API_KEY', '').strip()
PORT = int(os.environ.get('PORT', '8000'))
MAX_TOKENS = max(1, min(int(os.environ.get('RELAY_MAX_TOKENS', '1024')), 8192))
BASE = 'https://codex-relay.codes/v1'
SLOTS = BoundedSemaphore(1)
ALLOWED_HOSTS = {f'localhost:{PORT}', f'127.0.0.1:{PORT}'}
ALLOWED_ORIGINS = {f'http://{host}' for host in ALLOWED_HOSTS}


def upstream(path, data=None):
    if not KEY or KEY == 'paste_your_new_key_here':
        raise ValueError('Set RELAY_API_KEY in your local .env file, then restart the server.')
    req = Request(BASE + path,
                  data=json.dumps(data).encode() if data is not None else None,
                  headers={'Authorization': 'Bearer ' + KEY,
                           'Content-Type': 'application/json',
                           'Accept': 'application/json',
                           'User-Agent': 'HaxyLocal/1.0'})
    with urlopen(req, timeout=90) as response:
        raw = response.read(4_000_001)
        if len(raw) > 4_000_000:
            raise ValueError('Provider response exceeded the local size limit.')
        return json.loads(raw)


class Handler(BaseHTTPRequestHandler):
    server_version = 'HaxyLocal'

    def log_message(self, fmt, *args):
        # Do not log request paths, message bodies, keys, or provider responses.
        pass

    def reply(self, code, data):
        raw = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(raw)

    def trusted(self, write=False):
        if self.headers.get('Host') not in ALLOWED_HOSTS:
            self.reply(403, {'error': 'Unrecognized local host.'})
            return False
        origin = self.headers.get('Origin')
        if (origin and origin not in ALLOWED_ORIGINS) or (write and origin not in ALLOWED_ORIGINS):
            self.reply(403, {'error': 'Only the local HAXY page can make this request.'})
            return False
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            self.reply(403, {'error': 'Cross-site requests are disabled.'})
            return False
        return True

    def fail(self, error):
        if isinstance(error, HTTPError):
            code = error.code
            text = {400: 'Relay rejected this request. Check model compatibility and conversation size.',
                    401: 'Relay rejected the API key. Replace it locally and restart.',
                    402: 'Relay reports insufficient credits or a billing restriction.',
                    403: 'Relay denied access. Check your key permissions and account.',
                    404: 'Relay could not find this endpoint or model. Refresh the model list.',
                    429: 'Relay rate limit or quota reached. Wait before retrying.'}.get(code, 'Relay returned an error. Check its dashboard for details.')
            self.reply(code if 400 <= code < 500 else 502, {'error': text})
        elif isinstance(error, ValueError):
            self.reply(400, {'error': str(error) if not isinstance(error, json.JSONDecodeError) else 'Invalid JSON response.'})
        else:
            self.reply(502, {'error': 'Cannot reach Relay or the request timed out. Check the dashboard before retrying; a timed-out request may still be billed.'})

    def do_GET(self):
        if not self.trusted():
            return
        if self.path in ('/', '/index.html'):
            raw = (ROOT / 'public' / 'index.html').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(raw)
        elif self.path == '/api/models':
            try:
                data = upstream('/models')
                models = sorted({m['id'] for m in data.get('data', [])
                                 if isinstance(m, dict) and isinstance(m.get('id'), str)})
                self.reply(200, {'models': models})
            except Exception as error:
                self.fail(error)
        else:
            self.reply(404, {'error': 'Not found.'})

    def do_POST(self):
        if not self.trusted(write=True):
            return
        if self.path != '/api/chat':
            self.reply(404, {'error': 'Not found.'})
            return
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            self.reply(415, {'error': 'Expected application/json.'})
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 250_000:
                self.reply(413, {'error': 'Conversation too large. Reload to start a new chat.'})
                return
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError('Invalid request.')
            model, messages = body.get('model'), body.get('messages')
            if not isinstance(model, str) or not model or len(model) > 250:
                raise ValueError('Select a valid model.')
            if not isinstance(messages, list) or not 1 <= len(messages) <= 100:
                raise ValueError('Conversation must contain 1–100 messages. Reload to start over.')
            clean = []
            for message in messages:
                if not isinstance(message, dict) or message.get('role') not in ('user', 'assistant'):
                    raise ValueError('Invalid message role.')
                text = message.get('content')
                if not isinstance(text, str) or not text.strip() or len(text) > 30000:
                    raise ValueError('Each message must contain 1–30,000 characters.')
                clean.append({'role': message['role'], 'content': text})
        except (ValueError, TypeError) as error:
            self.fail(error)
            return
        if not SLOTS.acquire(blocking=False):
            self.reply(429, {'error': 'A chat request is already running. Please wait.'})
            return
        try:
            data = upstream('/chat/completions', {
                'model': model, 'messages': clean, 'stream': False, 'max_tokens': MAX_TOKENS
            })
            choices = data.get('choices', [])
            msg = choices[0].get('message', {}) if choices else {}
            content = msg.get('content')
            if isinstance(content, list):
                content = '\n'.join(part.get('text', '') for part in content if isinstance(part, dict))
            if not isinstance(content, str) or not content.strip():
                raise ValueError('Relay returned no text response. This model may not support plain text chat or may need a different token limit.')
            self.reply(200, {'content': content})
        except Exception as error:
            self.fail(error)
        finally:
            SLOTS.release()


if __name__ == '__main__':
    if not KEY:
        print('No key configured. Copy .env.example to .env and add your replacement key.')
    print(f'HAXY is running at http://localhost:{PORT}. Press Ctrl+C to stop.')
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')
    finally:
        server.server_close()
