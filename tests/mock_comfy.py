"""A small stand-in for a ComfyUI server, for the tests.

It answers the four endpoints the executor uses, in the shapes ComfyUI's own server.py and execution.py give
them (read on 2026-11-11, master branch):

    POST /upload/image         multipart field "image" -> {"name", "subfolder", "type"}; 400 without a file
    POST /prompt               {"prompt": graph} -> {"prompt_id", "number", "node_errors"};
                               400 {"error": {...}, "node_errors": {...}} when refused
    GET  /history/{prompt_id}  {} until the run is done, then {prompt_id: {"prompt", "outputs", "status"}}
    GET  /view                 ?filename=&subfolder=&type= -> the file's bytes; 400 for a path, 404 when unknown

It runs nothing. It is not ComfyUI, and a test passing here says only that the client follows those shapes.
"""
import json
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PNG = b'\x89PNG\r\n\x1a\n' + bytes(range(256)) + b'\r\n--not-a-boundary\r\n\x00\xff'


def parse_multipart(content_type, body):
    """{field name: (filename or None, bytes)} of a multipart/form-data body."""
    boundary = content_type.split('boundary=', 1)[1].strip().strip('"').encode()
    fields = {}
    for part in body.split(b'--' + boundary)[1:]:
        if part.startswith(b'--'):
            break
        head, _, data = part.lstrip(b'\r\n').partition(b'\r\n\r\n')
        if data.endswith(b'\r\n'):
            data = data[:-2]
        disposition = next(line for line in head.decode().split('\r\n') if line.lower().startswith('content-disposition'))
        params = dict(p.strip().split('=', 1) for p in disposition.split(';')[1:])
        fields[params['name'].strip('"')] = (params['filename'].strip('"') if 'filename' in params else None, data)
    return fields


class MockComfy:
    def __init__(self, polls_before_done=2, reject=False, fail=False, outputs=None, history_garbage=False):
        self.polls_before_done, self.reject, self.fail = polls_before_done, reject, fail
        self.outputs = outputs if outputs is not None else {
            '7': {'images': [{'filename': 'job_00001_.png', 'subfolder': '', 'type': 'output'}]}}
        self.history_garbage = history_garbage
        self.files = {'job_00001_.png': PNG}
        self.prompts, self.polls, self.uploads, self.requests = {}, {}, {}, []
        self.server = self.thread = None

    def start(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, status, body, kind='application/json'):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                mock.requests.append(('POST', self.path))
                if self.path == '/upload/image':
                    fields = parse_multipart(self.headers.get('Content-Type', ''), body)
                    filename, data = fields.get('image', (None, b''))
                    if not filename:
                        return self.answer(400, b'', 'text/plain')
                    mock.uploads[filename] = data
                    kind = fields.get('type', (None, b'input'))[1].decode()
                    return self.answer(200, {'name': filename, 'subfolder': '', 'type': kind})
                if self.path == '/prompt':
                    data = json.loads(body)
                    if 'prompt' not in data:
                        return self.answer(400, {'error': {'type': 'no_prompt', 'message': 'No prompt provided',
                                                           'details': 'No prompt provided', 'extra_info': {}},
                                                 'node_errors': {}})
                    if mock.reject:
                        return self.answer(400, {'error': {'type': 'mock_rejected', 'message': 'the mock refuses this',
                                                           'details': '', 'extra_info': {}},
                                                 'node_errors': {'5': {'errors': [{'message': 'mock'}]}}})
                    prompt_id = data.get('prompt_id') or str(uuid.uuid4())
                    mock.prompts[prompt_id], mock.polls[prompt_id] = data['prompt'], 0
                    return self.answer(200, {'prompt_id': prompt_id, 'number': len(mock.prompts), 'node_errors': {}})
                self.answer(404, b'', 'text/plain')

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                mock.requests.append(('GET', self.path))
                if url.path.startswith('/history/'):
                    prompt_id = urllib.parse.unquote(url.path[len('/history/'):])
                    if mock.history_garbage:
                        return self.answer(200, b'<html>not json</html>', 'text/html')
                    if prompt_id not in mock.prompts:
                        return self.answer(200, {})
                    mock.polls[prompt_id] += 1
                    if mock.polls[prompt_id] <= mock.polls_before_done:
                        return self.answer(200, {})
                    if mock.fail:
                        status = {'status_str': 'error', 'completed': False, 'messages': [['mock_error', {'why': 'boom'}]]}
                        return self.answer(200, {prompt_id: {'prompt': [], 'outputs': {}, 'status': status}})
                    status = {'status_str': 'success', 'completed': True, 'messages': []}
                    return self.answer(200, {prompt_id: {'prompt': [], 'outputs': mock.outputs, 'status': status}})
                if url.path == '/view':
                    query = urllib.parse.parse_qs(url.query)
                    filename = query.get('filename', [''])[0]
                    if not filename or filename[0] == '/' or '..' in filename:
                        return self.answer(400, b'', 'text/plain')
                    if filename not in mock.files:
                        return self.answer(404, b'', 'text/plain')
                    return self.answer(200, mock.files[filename], 'image/png')
                self.answer(404, b'', 'text/plain')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f'http://127.0.0.1:{self.server.server_address[1]}'

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
