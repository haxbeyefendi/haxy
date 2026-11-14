import hmac
import json
import os
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from flask import Flask, request, jsonify, Response, send_file

app = Flask(__name__, static_folder=None)
app.config['MAX_CONTENT_LENGTH'] = 100_000
ROOT = Path(__file__).resolve().parent
API_KEY = os.environ.get('OPENROUTER_API_KEY', '').strip()
PASSWORD = os.environ.get('APP_PASSWORD', '')
ORIGIN = (os.environ.get('APP_ORIGIN') or os.environ.get('RENDER_EXTERNAL_URL') or '').rstrip('/')
if not ORIGIN and os.environ.get('RENDER_EXTERNAL_HOSTNAME'):
    ORIGIN = 'https://' + os.environ['RENDER_EXTERNAL_HOSTNAME']
if not API_KEY or len(PASSWORD) < 16 or not ORIGIN.startswith('https://'):
    raise RuntimeError('Set OPENROUTER_API_KEY, APP_PASSWORD (16+ characters), and APP_ORIGIN (your HTTPS website URL).')
lock = threading.Lock()
slots = threading.BoundedSemaphore(1)
state = {'last': 0.0, 'day': -1, 'count': 0, 'auth_times': []}

@app.before_request
def protect():
    if request.path == '/healthz' and request.method == 'GET':
        return None
    auth = request.authorization
    valid = auth and auth.type.lower() == 'basic' and hmac.compare_digest((auth.username or '').encode(), b'haxy') and hmac.compare_digest((auth.password or '').encode(), PASSWORD.encode())
    if not valid:
        now = time.monotonic()
        with lock:
            state['auth_times'] = [t for t in state['auth_times'] if now-t < 60]
            if len(state['auth_times']) >= 30:
                return jsonify(error='Too many login attempts. Wait a minute.'), 429
            state['auth_times'].append(now)
        return Response('Sign in with username haxy and your app password.', 401, {'WWW-Authenticate': 'Basic realm="HAXY", charset="UTF-8"'})
    if request.method not in ('GET', 'HEAD', 'POST'):
        return jsonify(error='Method not allowed.'), 405
    if request.method == 'POST' and (request.headers.get('Origin', '').rstrip('/') != ORIGIN or request.headers.get('Sec-Fetch-Site') == 'cross-site'):
        return jsonify(error='Origin rejected. Check APP_ORIGIN matches this website URL exactly.'), 403

@app.after_request
def headers(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    return response

@app.get('/healthz')
def health():
    return jsonify(status='ok')

@app.get('/')
@app.get('/index.html')
def index():
    return send_file(ROOT / 'index.html')

@app.get('/api/status')
def status():
    return jsonify(configured=True)

@app.post('/api/chat')
def chat():
    if not request.is_json:
        return jsonify(error='Expected JSON.'), 415
    data = request.get_json(silent=True)
    messages = data.get('messages') if isinstance(data, dict) else None
    if not isinstance(messages, list) or not 1 <= len(messages) <= 20:
        return jsonify(error='Send 1â€“20 chat messages.'), 400
    clean = []
    for m in messages:
        if not isinstance(m, dict) or m.get('role') not in ('user', 'assistant') or not isinstance(m.get('content'), str) or not 1 <= len(m['content']) <= 12000:
            return jsonify(error='Invalid message or message too long.'), 400
        clean.append({'role': m['role'], 'content': m['content']})
    if sum(len(m['content']) for m in clean) > 60000:
        return jsonify(error='Conversation too long. Reload to start a new chat.'), 400
    if not slots.acquire(blocking=False):
        return jsonify(error='A reply is already in progress. Try again shortly.'), 429
    try:
        now = time.monotonic()
        day = int(time.time() // 86400)
        with lock:
            if state['day'] != day:
                state.update(day=day, count=0)
            if now-state['last'] < 5 or state['count'] >= 40:
                return jsonify(error='App limit reached. Allow 5 seconds between requests; maximum 40 per UTC day while this process runs.'), 429
            state['last'] = now
            state['count'] += 1
        payload = {'model': 'openrouter/free', 'messages': [{'role': 'system', 'content': 'You are HAXY, a helpful, accurate creative assistant.'}] + clean, 'max_tokens': 1024, 'stream': False}
        upstream = Request('https://openrouter.ai/api/v1/chat/completions', data=json.dumps(payload).encode(), headers={'Authorization': 'Bearer '+API_KEY, 'Content-Type': 'application/json'})
        try:
            with urlopen(upstream, timeout=85) as reply:
                result = json.load(reply)
            content = result.get('choices', [{}])[0].get('message', {}).get('content')
            if not isinstance(content, str) or not content.strip():
                return jsonify(error='No text returned by provider. Try again.'), 502
            return jsonify(content=content, model=result.get('model', 'openrouter/free'))
        except HTTPError as exc:
            text = {401: 'OpenRouter key is invalid. Update it in Render.', 402: 'OpenRouter rejected this request for account or credit reasons. Paid fallback is disabled.', 403: 'Provider denied access. Check OpenRouter permissions and data settings.', 429: 'OpenRouter free quota reached. Try later.'}.get(exc.code, 'Provider unavailable. Try later.')
            return jsonify(error=text), exc.code if exc.code in (401,402,403,429) else 502
        except (URLError, TimeoutError, OSError):
            return jsonify(error='Provider network error or timeout. Try again.'), 502
        except (ValueError, TypeError, IndexError, AttributeError):
            return jsonify(error='Unexpected provider response.'), 502
    finally:
        slots.release()

@app.errorhandler(413)
def too_big(error):
    return jsonify(error='Request too large.'), 413