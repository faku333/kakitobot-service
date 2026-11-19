"""Local Twitch chat relay for the project operator."""
import getpass
import hashlib
import re
import threading
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BOT_LOGIN = 'kakitobot'
BOT_CLIENT = '9oxu6vz4meh44845ex0109y967e1e8'
LOCAL_URL = 'http://127.0.0.1:17435'
STUDIO_CLIENT = 'so8oaf13qa02qr618m26tjngqtpsqf'


def request_json(url, headers=None, body=None):
    with urlopen(Request(url, data=body, headers=headers or {}), timeout=5) as response:
        return json.load(response)


class Relay:
    def __init__(self, secret):
        self.secret = secret
        self.token = ''
        self.expires = 0
        self.sender = ''
        self.last_sent = {}

    def token_headers(self):
        if time.monotonic() >= self.expires:
            result = request_json('https://id.twitch.tv/oauth2/token', body=urlencode({
                'client_id': BOT_CLIENT, 'client_secret': self.secret,
                'grant_type': 'client_credentials',
            }).encode())
            self.token = result['access_token']
            self.expires = time.monotonic() + max(0, result['expires_in'] - 60)
        return {'Authorization': 'Bearer ' + self.token, 'Client-Id': BOT_CLIENT}

    def connect(self):
        self.sender = request_json('https://api.twitch.tv/helix/users?login=' + BOT_LOGIN,
                                   self.token_headers())['data'][0]['id']

    def send(self, token, message):
        identity = request_json('https://id.twitch.tv/oauth2/validate',
                                {'Authorization': 'OAuth ' + token})
        channel = identity.get('user_id')
        if identity.get('client_id') != STUDIO_CLIENT or not channel or channel == self.sender:
            return 403, {'status': 'Canal de Kakito Studio no autorizado'}
        now = time.monotonic()
        if now - self.last_sent.get(channel, -10) < 2:
            return 429, {'status': 'Esperá antes de volver a probar'}
        self.last_sent[channel] = now
        result = request_json('https://api.twitch.tv/helix/chat/messages',
                              dict(self.token_headers(), **{'Content-Type': 'application/json'}),
                              json.dumps({'broadcaster_id': channel, 'sender_id': self.sender,
                                          'message': message}).encode())['data'][0]
        return 200, {'status': 'Conectado' if result['is_sent'] else 'Twitch rechazó el mensaje'}


class BoardLinks:
    """A capability-derived name can only be updated by its secret holder."""
    def __init__(self):self.entries={};self.lock=threading.Lock()
    def register(self,values):
        secret=values.get('secret','')
        if not isinstance(secret,str) or not re.fullmatch(r'[A-Za-z0-9_-]{48,100}',secret):raise ValueError('Identidad inválida')
        ident=hashlib.sha256(secret.encode()).hexdigest();enabled=values.get('enabled')
        if type(enabled) is not bool:raise ValueError('Estado inválido')
        target=values.get('target','')
        if enabled and not re.fullmatch(r'https://[a-z0-9-]+\.trycloudflare\.com/board/[A-Za-z0-9_-]{32}/',target):raise ValueError('Destino de túnel inválido')
        with self.lock:
            now=time.monotonic();self.entries={k:v for k,v in self.entries.items() if v[1]>now}
            if enabled:
                if ident not in self.entries and len(self.entries)>=1000:raise ValueError('Servicio ocupado')
                self.entries[ident]=(target,now+90)
            else:self.entries.pop(ident,None)
        return {'id':ident,'active':enabled}
    def target(self,ident):
        with self.lock:
            entry=self.entries.get(ident)
            return entry[0] if entry and entry[1]>time.monotonic() else ''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        match=re.fullmatch(r'/pizarra/([a-f0-9]{64})',self.path)
        if match:
            target=self.server.links.target(match[1])
            if target:
                self.send_response(302);self.send_header('Location',target);self.send_header('Cache-Control','no-store');self.send_header('Referrer-Policy','no-referrer');self.send_header('Content-Length','0');self.end_headers()
            else:self.reply(503,{'status':'El streamer no tiene el pizarrón conectado en este momento.'})
        elif self.path == '/status':
            self.reply(200, {'status': 'Servicio disponible', 'id': self.server.relay.sender})
        else:
            self.reply(404, {})

    def do_POST(self):
        if self.path=='/board/register':
            try:
                size=int(self.headers.get('Content-Length','0'))
                if self.headers.get('Origin') or not 0<size<=2048:raise ValueError('Solicitud inválida')
                self.reply(200,self.server.links.register(json.loads(self.rfile.read(size))))
            except (ValueError,TypeError,AttributeError):self.reply(400,{'status':'Registro de enlace inválido'})
            return
        size = self.headers.get('Content-Length', '')
        token = self.headers.get('Authorization', '').removeprefix('Bearer ')
        if (self.path != '/chat' or self.headers.get('Origin') or not token.isascii()
                or not token.isalnum() or not size.isdecimal() or not 0 < int(size) <= 4096):
            self.reply(400, {'status': 'Solicitud inválida'})
            return
        try:
            message = json.loads(self.rfile.read(int(size))).get('message')
            if not isinstance(message, str) or not message.strip() or len(message) > 350:
                self.reply(400, {'status': 'Mensaje inválido'})
                return
            with self.server.relay_lock:self.reply(*self.server.relay.send(token, message))
        except HTTPError as error:
            status = 'Revisá la autorización del bot y el permiso /mod kakitobot' if error.code in (401, 403) else 'Twitch no pudo publicar el mensaje'
            self.reply(502, {'status': status})
        except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
            self.reply(502, {'status': 'El servicio no pudo completar la solicitud'})


def main():
    secret = os.environ.get('KAKITO_BOT_CLIENT_SECRET')
    relay = Relay(secret or getpass.getpass('Client Secret de KakitoBot: ').strip())
    try:
        relay.connect()
    except (OSError, ValueError, KeyError, IndexError):
        raise SystemExit('No se pudo conectar a Twitch. Revisá el Client Secret y la conexión.') from None
    with ThreadingHTTPServer((os.environ.get('KAKITO_BIND', '127.0.0.1'), int(os.environ.get('PORT', '17435'))), Handler) as server:
        server.relay = relay
        server.relay_lock=threading.Lock();server.links=BoardLinks()
        print('Servicio KakitoBot disponible.', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    main()
