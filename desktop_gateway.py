#!/usr/bin/env python3
"""Local Anthropic Messages gateway for Claude Desktop third-party inference.

Claude Desktop's third-party mode expects a gateway that serves Claude model
IDs. This process accepts those IDs on 127.0.0.1 and forwards the requests to
a DeepSeek Anthropic-compatible endpoint, replacing the model name.

This is not an Anthropic-supported configuration: Anthropic states that it
does not support routing Claude Code to non-Claude models through a gateway.
"""
import argparse
from dataclasses import dataclass
import hmac
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.request

import launch

DISCOVERY_MODELS = (
    ('claude-opus-5-5', 'Opus'),
    ('claude-sonnet-5-5', 'Sonnet'),
    ('claude-haiku-5-5', 'Haiku'),
)


@dataclass
class GatewayConfig:
    upstream: str
    key: str
    token: str = 'local'
    opus_model: str = 'deepseek-v4-pro'
    sonnet_model: str = 'deepseek-v4-pro'
    haiku_model: str = 'deepseek-flash'
    default_model: str = 'deepseek-v4-pro'
    timeout: int = 900
    strip_fields: tuple = ('context_management', 'mcp_servers', 'container',
                           'fallbacks', 'task_budget', 'betas')

    def map_model(self, name):
        if not isinstance(name, str) or not name:
            return self.default_model
        lowered = name.lower()
        if 'claude' not in lowered:
            # A DeepSeek (or other) model ID configured directly passes through.
            return name
        if 'haiku' in lowered:
            return self.haiku_model
        if 'sonnet' in lowered:
            return self.sonnet_model
        if 'opus' in lowered or 'fable' in lowered or 'mythos' in lowered:
            return self.opus_model
        return self.default_model

    def display_model(self, name):
        lowered = name.lower()
        if 'haiku' in lowered:
            return self.haiku_model
        if 'sonnet' in lowered:
            return self.sonnet_model
        if 'opus' in lowered:
            return self.opus_model
        return self.default_model


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'claude-desktop-gateway/0.1'

    def log_message(self, format, *args):
        sys.stderr.write('%s - %s\n' % (self.address_string(), format % args))

    @property
    def config(self):
        return self.server.config

    def do_GET(self):
        path = self.path.split('?', 1)[0].rstrip('/') or '/'
        if path == '/health':
            self.send_json(200, {'status': 'ok'})
            return
        if path == '/v1/models':
            data = []
            for model_id, family in DISCOVERY_MODELS:
                data.append({'type': 'model', 'id': model_id,
                             'display_name': family + ' (DeepSeek ' +
                                             self.config.display_model(model_id) + ')',
                             'created_at': '2026-01-01T00:00:00Z'})
            self.send_json(200, {'data': data, 'has_more': False,
                                 'first_id': data[0]['id'], 'last_id': data[-1]['id']})
            return
        if path.startswith('/v1/models/'):
            model_id = path[len('/v1/models/'):]
            for known, family in DISCOVERY_MODELS:
                if known == model_id:
                    self.send_json(200, {'type': 'model', 'id': known,
                                         'display_name': family + ' (DeepSeek ' +
                                                         self.config.display_model(known) + ')',
                                         'created_at': '2026-01-01T00:00:00Z'})
                    return
            self.send_json_error(404, 'model not found: ' + model_id)
            return
        self.send_json_error(404, 'not found')

    def do_POST(self):
        path = self.path.split('?', 1)[0].rstrip('/')
        if path not in ('/v1/messages', '/v1/messages/count_tokens'):
            self.send_json_error(404, 'not found')
            return
        if not self.authorized():
            self.send_json_error(401, 'invalid gateway token')
            return
        try:
            payload = json.loads(self.read_body().decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            self.send_json_error(400, 'request body is not JSON')
            return
        if not isinstance(payload, dict):
            self.send_json_error(400, 'request body must be a JSON object')
            return
        if path == '/v1/messages/count_tokens':
            self.count_tokens(payload)
            return
        self.proxy_messages(payload)

    def authorized(self):
        expected = self.config.token
        if not expected:
            return True
        header = self.headers.get('Authorization', '')
        if header.lower().startswith('bearer '):
            supplied = header[7:].strip()
        else:
            supplied = self.headers.get('x-api-key', '').strip()
        return hmac.compare_digest(supplied, expected)

    def read_body(self):
        if 'chunked' in self.headers.get('Transfer-Encoding', '').lower():
            chunks = []
            while True:
                line = self.rfile.readline().strip()
                size = int(line.split(b';', 1)[0], 16)
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)
            return b''.join(chunks)
        length = int(self.headers.get('Content-Length') or 0)
        return self.rfile.read(length) if length else b''

    def send_json(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json_error(self, status, message):
        self.send_json(status, {'type': 'error',
                                'error': {'type': 'api_error', 'message': message}})

    def count_tokens(self, payload):
        # DeepSeek has no Anthropic count_tokens endpoint; a local estimate is
        # enough for the client's context tracking.
        text = json.dumps([payload.get('system'), payload.get('messages'),
                           payload.get('tools')], ensure_ascii=False)
        self.send_json(200, {'input_tokens': max(1, len(text) // 4)})

    def proxy_messages(self, payload):
        config = self.config
        original = payload.get('model')
        target = config.map_model(original)
        payload['model'] = target
        for name in config.strip_fields:
            payload.pop(name, None)
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(config.upstream.rstrip('/') + '/v1/messages',
                                         data=data, method='POST')
        request.add_header('Content-Type', 'application/json')
        request.add_header('Authorization', 'Bearer ' + config.key)
        request.add_header('anthropic-version',
                           self.headers.get('anthropic-version') or '2023-06-01')
        try:
            response = urllib.request.urlopen(request, timeout=config.timeout)
        except urllib.error.HTTPError as error:
            body = error.read()
            content_type = error.headers.get('Content-Type', 'application/json')
            self.send_response(error.code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.log_message('%s -> %s: upstream HTTP %s', original, target, error.code)
            return
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            self.send_json_error(502, 'upstream unreachable: ' + type(error).__name__)
            self.log_message('%s -> %s: %s', original, target, type(error).__name__)
            return
        with response:
            content_type = response.headers.get('Content-Type', 'application/json')
            if 'text/event-stream' in content_type:
                self.send_response(response.status)
                self.send_header('Content-Type', content_type)
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True
                try:
                    while True:
                        chunk = response.read(8192)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                body = response.read()
                self.send_response(response.status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            self.log_message('%s -> %s: HTTP %s', original, target, response.status)


def create_server(config, host='127.0.0.1', port=8787):
    server = ThreadingHTTPServer((host, port), GatewayHandler)
    server.daemon_threads = True
    server.config = config
    return server


def resolve_key():
    try:
        return launch.api_key('ds')
    except launch.ConfigurationError as error:
        raise SystemExit(str(error))


def check_upstream(config):
    payload = {'model': config.default_model, 'max_tokens': 1,
               'messages': [{'role': 'user', 'content': '.'}]}
    data = json.dumps(payload).encode('utf-8')
    request = urllib.request.Request(config.upstream.rstrip('/') + '/v1/messages',
                                     data=data, method='POST')
    request.add_header('Content-Type', 'application/json')
    request.add_header('Authorization', 'Bearer ' + config.key)
    request.add_header('anthropic-version', '2023-06-01')
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read(400)
        return True, 'upstream accepted the request (HTTP %s): %s' % (
            response.status, body.decode('utf-8', 'replace')[:120])
    except urllib.error.HTTPError as error:
        detail = error.read(400).decode('utf-8', 'replace')
        return False, 'upstream answered HTTP %s: %s' % (error.code, detail[:200])
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return False, 'cannot reach the upstream (' + type(error).__name__ + ')'


def display_host(host):
    return '127.0.0.1' if host in ('0.0.0.0', '::') else host


def print_config(config, host, port):
    print('Gateway base URL: http://%s:%d' % (display_host(host), port))
    print('Gateway token:    ' + (config.token or '(not required)'))
    print('Auth scheme:      bearer (x-api-key also accepted)')
    print('Discovery models: ' + ', '.join(model for model, _ in DISCOVERY_MODELS))
    print('Model mapping:')
    print('  claude-opus-*   -> ' + config.opus_model)
    print('  claude-sonnet-* -> ' + config.sonnet_model)
    print('  claude-haiku-*  -> ' + config.haiku_model)
    print('  other Claude    -> ' + config.default_model)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--upstream', default='https://api.deepseek.com/anthropic')
    parser.add_argument('--token', default='local',
                        help='credential Claude Desktop sends to this gateway')
    parser.add_argument('--opus-model', default='deepseek-v4-pro')
    parser.add_argument('--sonnet-model', default='deepseek-v4-pro')
    parser.add_argument('--haiku-model', default='deepseek-flash')
    parser.add_argument('--default-model', default='deepseek-v4-pro')
    parser.add_argument('--strip', default='context_management,mcp_servers,container,'
                                           'fallbacks,task_budget,betas',
                        help='comma-separated request fields removed before forwarding')
    parser.add_argument('--print-config', action='store_true',
                        help='print the values to enter in Claude Desktop and exit')
    parser.add_argument('--check', action='store_true',
                        help='send one minimal request to the upstream and exit')
    args = parser.parse_args()
    strip = tuple(name.strip() for name in args.strip.split(',') if name.strip())
    config = GatewayConfig(upstream=args.upstream, key=resolve_key(), token=args.token,
                           opus_model=args.opus_model, sonnet_model=args.sonnet_model,
                           haiku_model=args.haiku_model, default_model=args.default_model,
                           strip_fields=strip)
    if args.print_config:
        print_config(config, args.host, args.port)
        return 0
    if args.check:
        ok, message = check_upstream(config)
        print(message)
        return 0 if ok else 1
    server = create_server(config, args.host, args.port)
    print('Claude Desktop gateway on http://%s:%d -> %s' %
          (display_host(args.host), server.server_address[1], config.upstream),
          file=sys.stderr)
    print('In Claude Desktop: Gateway base URL http://%s:%d, static key "%s", bearer.' %
          (display_host(args.host), server.server_address[1], config.token), file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
