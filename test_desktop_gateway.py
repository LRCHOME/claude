import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import desktop_gateway


class UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    calls = []
    respond_error = False

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(length)) if length else {}
        UpstreamHandler.calls.append({
            'path': self.path,
            'authorization': self.headers.get('Authorization'),
            'anthropic_beta': self.headers.get('anthropic-beta'),
            'body': body,
        })
        if UpstreamHandler.respond_error:
            data = b'{"type":"error","error":{"type":"invalid_request_error","message":"bad model"}}'
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if body.get('stream'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(b'event: message_start\ndata: {"type":"message_start"}\n\n')
            self.wfile.flush()
            self.wfile.write(b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
            self.wfile.flush()
            self.close_connection = True
            return
        data = json.dumps({'id': 'msg_fixture', 'type': 'message',
                           'content': [{'type': 'text', 'text': 'ok'}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        UpstreamHandler.calls = []
        UpstreamHandler.respond_error = False
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), UpstreamHandler)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        config = desktop_gateway.GatewayConfig(
            upstream='http://127.0.0.1:%d' % self.upstream.server_address[1],
            key='fixture-key', token='fixture-token')
        self.gateway = desktop_gateway.create_server(config, '127.0.0.1', 0)
        threading.Thread(target=self.gateway.serve_forever, daemon=True).start()
        self.addCleanup(self.gateway.server_close)
        self.addCleanup(self.gateway.shutdown)
        self.base = 'http://127.0.0.1:%d' % self.gateway.server_address[1]

    def request(self, path, payload, token='fixture-token'):
        request = urllib.request.Request(self.base + path,
                                         data=json.dumps(payload).encode(),
                                         method='POST')
        request.add_header('Content-Type', 'application/json')
        if token is not None:
            request.add_header('Authorization', 'Bearer ' + token)
        return urllib.request.urlopen(request, timeout=10)

    def message(self, model, **extra):
        payload = {'model': model, 'max_tokens': 16,
                   'messages': [{'role': 'user', 'content': 'hi'}]}
        payload.update(extra)
        return payload

    def test_opus_maps_to_deepseek_and_unsupported_fields_are_stripped(self):
        with self.request('/v1/messages',
                          self.message('claude-opus-5-5', context_management={'x': 1})) as response:
            self.assertEqual(json.load(response)['id'], 'msg_fixture')
        call = UpstreamHandler.calls[-1]
        self.assertEqual(call['body']['model'], 'deepseek-v4-pro')
        self.assertNotIn('context_management', call['body'])
        self.assertEqual(call['authorization'], 'Bearer fixture-key')

    def test_haiku_maps_to_the_fast_deepseek_model(self):
        with self.request('/v1/messages', self.message('claude-haiku-5-5')):
            pass
        self.assertEqual(UpstreamHandler.calls[-1]['body']['model'], 'deepseek-flash')

    def test_non_claude_model_id_passes_through(self):
        with self.request('/v1/messages', self.message('deepseek-v4-pro')):
            pass
        self.assertEqual(UpstreamHandler.calls[-1]['body']['model'], 'deepseek-v4-pro')

    def test_streaming_response_is_forwarded_chunk_by_chunk(self):
        with self.request('/v1/messages', self.message('claude-sonnet-5-5', stream=True)) as response:
            self.assertIn('text/event-stream', response.headers.get('Content-Type'))
            data = response.read()
        self.assertIn(b'event: message_start', data)
        self.assertIn(b'event: message_stop', data)

    def test_invalid_gateway_token_is_rejected_without_contacting_the_upstream(self):
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.request('/v1/messages', self.message('claude-opus-5-5'), token='wrong')
        self.assertEqual(failure.exception.code, 401)
        self.assertEqual(UpstreamHandler.calls, [])

    def test_models_endpoint_lists_claude_ids_for_discovery(self):
        with urllib.request.urlopen(self.base + '/v1/models', timeout=10) as response:
            data = json.load(response)
        ids = [model['id'] for model in data['data']]
        self.assertEqual(ids, ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5'])

    def test_count_tokens_is_estimated_without_calling_the_upstream(self):
        with self.request('/v1/messages/count_tokens', self.message('claude-opus-5-5')) as response:
            data = json.load(response)
        self.assertGreater(data['input_tokens'], 0)
        self.assertEqual(UpstreamHandler.calls, [])

    def test_upstream_error_status_and_body_are_forwarded(self):
        UpstreamHandler.respond_error = True
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.request('/v1/messages', self.message('claude-opus-5-5'))
        self.assertEqual(failure.exception.code, 400)
        self.assertIn('bad model', failure.exception.read().decode('utf-8'))


if __name__ == '__main__':
    unittest.main()
