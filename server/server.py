#!/usr/bin/env python3
"""Сервис Мастер — маленький сервер для сохранения прайса.

POST /api/login   {"password": "..."}      -> {"token": "..."}
PUT  /api/prices  (Authorization: Bearer T) -> сохраняет прайс в prices.json
POST /api/logout  (Authorization: Bearer T)

Только стандартная библиотека Python 3. Статику отдаёт nginx.
Смена пароля:  python3 server.py --set-password
"""
import argparse, getpass, hashlib, hmac, json, os, secrets, shutil, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA = os.environ.get('SERVIS_DATA', '/var/lib/servis-master')
AUTH_FILE = os.environ.get('SERVIS_AUTH', '/etc/servis-master/auth.json')
TOKEN_DAYS = 30
MAX_BODY = 5 * 1024 * 1024
lock = threading.Lock()
fails = {}  # ip -> [count, until]


def load_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def write_atomic(path, text, mode=0o644):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), bytes.fromhex(salt), 200_000).hex()


def set_password():
    pw = getpass.getpass('Новый пароль для редактирования сайта: ')
    if len(pw) < 4:
        sys.exit('Пароль слишком короткий (минимум 4 символа).')
    if pw != getpass.getpass('Повторите пароль: '):
        sys.exit('Пароли не совпадают.')
    salt = secrets.token_hex(16)
    os.makedirs(os.path.dirname(AUTH_FILE), exist_ok=True)
    write_atomic(AUTH_FILE, json.dumps({'salt': salt, 'hash': hash_pw(pw, salt)}), 0o600)
    tokens = os.path.join(DATA, 'tokens.json')
    if os.path.exists(tokens):
        os.remove(tokens)  # выйти со всех устройств
    print('Пароль сохранён.')


def check_pw(pw):
    a = load_json(AUTH_FILE, None)
    if not a:
        return False
    return hmac.compare_digest(hash_pw(pw, a['salt']), a['hash'])


def tokens_path():
    return os.path.join(DATA, 'tokens.json')


def valid_token(tok):
    if not tok:
        return False
    now = time.time()
    with lock:
        toks = {k: v for k, v in load_json(tokens_path(), {}).items() if v > now}
    return any(hmac.compare_digest(tok, k) for k in toks)


def valid_prices(d):
    if not isinstance(d, dict) or not isinstance(d.get('brands'), list) or not isinstance(d.get('models'), list):
        return False
    for b in d['brands']:
        if not isinstance(b, dict) or not isinstance(b.get('id'), str) or not isinstance(b.get('name'), str):
            return False
    for m in d['models']:
        if not isinstance(m, dict) or not isinstance(m.get('name'), str) or not isinstance(m.get('services', []), list):
            return False
        for s in m.get('services', []):
            if not isinstance(s, dict) or not isinstance(s.get('name', ''), str):
                return False
            try:
                float(s.get('price', 0))
            except (TypeError, ValueError):
                return False
    return True


class H(BaseHTTPRequestHandler):
    server_version = 'ServisMaster/1.0'

    def log_message(self, fmt, *args):
        sys.stderr.write('%s %s\n' % (self.ip(), fmt % args))

    def ip(self):
        return self.headers.get('X-Real-IP') or self.client_address[0]

    def send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n <= 0 or n > MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(n).decode('utf-8'))
        except Exception:
            return None

    def token(self):
        h = self.headers.get('Authorization', '')
        return h[7:].strip() if h.startswith('Bearer ') else ''

    def do_POST(self):
        if self.path == '/api/login':
            ip = self.ip()
            c, until = fails.get(ip, [0, 0])
            if until > time.time():
                return self.send(429, {'error': 'wait'})
            d = self.body() or {}
            if check_pw(str(d.get('password', ''))):
                fails.pop(ip, None)
                tok = secrets.token_urlsafe(32)
                with lock:
                    now = time.time()
                    toks = {k: v for k, v in load_json(tokens_path(), {}).items() if v > now}
                    toks[tok] = now + TOKEN_DAYS * 86400
                    write_atomic(tokens_path(), json.dumps(toks), 0o600)
                return self.send(200, {'token': tok})
            time.sleep(1)
            c += 1
            fails[ip] = [c, time.time() + 300 if c >= 5 else 0]
            if c >= 5:
                fails[ip][0] = 0
            return self.send(401, {'error': 'bad password'})
        if self.path == '/api/logout':
            tok = self.token()
            with lock:
                toks = load_json(tokens_path(), {})
                toks.pop(tok, None)
                write_atomic(tokens_path(), json.dumps(toks), 0o600)
            return self.send(200, {'ok': True})
        if self.path == '/api/check':
            return self.send(200 if valid_token(self.token()) else 401, {})
        self.send(404, {'error': 'not found'})

    def do_PUT(self):
        if self.path != '/api/prices':
            return self.send(404, {'error': 'not found'})
        if not valid_token(self.token()):
            return self.send(401, {'error': 'login required'})
        d = self.body()
        if not valid_prices(d):
            return self.send(400, {'error': 'bad data'})
        path = os.path.join(DATA, 'prices.json')
        with lock:
            if os.path.exists(path):  # резервная копия, храним последние 100
                bdir = os.path.join(DATA, 'backups')
                os.makedirs(bdir, exist_ok=True)
                shutil.copy2(path, os.path.join(bdir, time.strftime('prices-%Y%m%d-%H%M%S.json')))
                old = sorted(os.listdir(bdir))[:-100]
                for f in old:
                    os.remove(os.path.join(bdir, f))
            write_atomic(path, json.dumps(d, ensure_ascii=False))
        self.send(200, {'ok': True})

    # Только для локальной проверки: --static DIR отдаёт файлы сайта
    def do_GET(self):
        st = getattr(self.server, 'static', None)
        if not st:
            return self.send(404, {'error': 'not found'})
        p = self.path.split('?')[0]
        f = os.path.join(DATA, 'prices.json') if p == '/prices.json' else os.path.join(st, (p.strip('/') or 'index.html'))
        if not os.path.isfile(f):
            return self.send(404, {'error': 'not found'})
        data = open(f, 'rb').read()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json' if f.endswith('.json') else 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--set-password', action='store_true')
    ap.add_argument('--port', type=int, default=8787)
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--static')
    a = ap.parse_args()
    if a.set_password:
        set_password()
        sys.exit(0)
    os.makedirs(DATA, exist_ok=True)
    srv = ThreadingHTTPServer((a.host, a.port), H)
    srv.static = a.static
    print('Сервис Мастер API на %s:%d' % (a.host, a.port), flush=True)
    srv.serve_forever()
