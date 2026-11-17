"""Normalize Anthropic system blocks for llama.cpp's strict Qwen template."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import secrets
import threading
import urllib.error
import urllib.request


def normalize_system(data):
    def text(content):
        if isinstance(content, str):
            return content
        if not isinstance(content, list) or any(b.get('type') != 'text' for b in content):
            raise ValueError('Only text system blocks are supported')
        return '\n\n'.join(b.get('text', '') for b in content)

    parts = [text(data['system'])] if data.get('system') is not None else []
    messages = []
    for message in data.get('messages', []):
        if message.get('role') == 'system':
            parts.append(text(message.get('content', '')))
        else:
            messages.append(message)
    result = dict(data, messages=messages)
    if parts:
        result['system'] = '\n\n'.join(parts)
    return result


def start(base_url, upstream_key):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def forward(self):
            auth = self.headers.get('Authorization', '')
            key = self.headers.get('x-api-key', '')
            if not (hmac.compare_digest(auth, 'Bearer ' + token) or hmac.compare_digest(key, token)):
                self.send_error(401)
                return
            if self.path.split('?', 1)[0] not in ('/v1/messages', '/v1/messages/count_tokens', '/v1/models'):
                self.send_error(404)
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size < 0 or size > 64 * 1024 * 1024:
                    self.send_error(413)
                    return
                body = self.rfile.read(size) if size else None
                if body:
                    data = json.loads(body)
                    data = normalize_system(data)
                    body = json.dumps(data).encode()
                headers = {k: v for k, v in self.headers.items()
                           if k.lower() not in ('host', 'connection', 'content-length', 'authorization',
                                                'x-api-key', 'accept-encoding', 'transfer-encoding')}
                headers['Authorization'] = 'Bearer ' + upstream_key
                headers['Accept-Encoding'] = 'identity'
                request = urllib.request.Request(base_url + self.path, body, headers, method=self.command)
                try:
                    response = urllib.request.urlopen(request, timeout=600)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    self.send_response(response.status)
                    for k, v in response.headers.items():
                        if k.lower() not in ('connection', 'transfer-encoding', 'content-length'):
                            self.send_header(k, v)
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    while True:
                        chunk = response.read1(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (ValueError, TypeError, AttributeError):
                self.send_error(400, 'Invalid request')
            except (OSError, urllib.error.URLError):
                self.send_error(502, 'Local model server unavailable')
            finally:
                self.close_connection = True

        do_POST = forward
        do_GET = forward

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, 'http://127.0.0.1:' + str(server.server_port), token
