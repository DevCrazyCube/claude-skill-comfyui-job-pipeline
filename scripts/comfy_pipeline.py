#!/usr/bin/env python3
"""ComfyUI behind one boundary: JobSpec -> WorkflowTemplate -> mapper -> executor -> OutputAsset.

    python3 comfy_pipeline.py check TEMPLATE.json             does the template still fit its workflow?
    python3 comfy_pipeline.py map   TEMPLATE.json SPEC.json   print the workflow that would be sent
    python3 comfy_pipeline.py run   TEMPLATE.json SPEC.json --server http://127.0.0.1:8188 --out out/

Exit 0 fine, 1 a problem with the template, the spec or the run, 2 cannot start (a file, an option).

The pieces, each usable on its own:

    WorkflowTemplate.load(path)        a template and its API-format workflow
    check_template(template)           every problem with the template, as a list of sentences
    select_template(templates, ...)    the one template for a service
    map_job(template, inputs)          a new workflow with the inputs written in (the only code that knows node ids)
    ComfyExecutor(server)              the only code that talks to ComfyUI: upload, submit, wait, fetch
    FakeExecutor()                     the same interface without a server, for the caller's own tests
    run_job(template, inputs, executor, out_dir)   all of it; returns a GenerationJob, failed or done

The endpoints used are POST /upload/image, POST /prompt, GET /history/{prompt_id} and GET /view.

Standard library only. Python 3.8 or newer.
"""
import argparse
import copy
import dataclasses
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

TYPES = ('text', 'int', 'float', 'choice', 'image')


class PipelineError(Exception):
    """Anything this module refuses or cannot do."""


class TemplateError(PipelineError):
    """The template does not fit its workflow."""


class MappingError(PipelineError):
    """The inputs do not fit the template. .problems holds every one of them."""

    def __init__(self, problems):
        super().__init__('; '.join(problems))
        self.problems = list(problems)


class PromptRejected(PipelineError):
    """ComfyUI answered POST /prompt with 400. .error and .node_errors are its own."""

    def __init__(self, error, node_errors):
        message = error.get('message') if isinstance(error, dict) else str(error)
        nodes = ', '.join(sorted(node_errors)) if isinstance(node_errors, dict) and node_errors else 'none named'
        super().__init__(f'ComfyUI rejected the workflow: {message} (nodes: {nodes})')
        self.error, self.node_errors = error, node_errors


class ExecutionFailed(PipelineError):
    """The workflow was accepted and then failed, or gave nothing where an output was expected."""


class ExecutionTimeout(PipelineError):
    """No result within the time limit."""


class ProviderError(PipelineError):
    """The server could not be reached, or answered with something this code does not understand."""


@dataclasses.dataclass
class WorkflowTemplate:
    id: str
    version: int
    service: str
    provider: str
    inputs: dict
    output: dict
    workflow: dict

    @classmethod
    def load(cls, path):
        """Read TEMPLATE.json and the workflow file it names (relative to the template)."""
        with open(path, encoding='utf-8') as f:
            raw = json.load(f)
        missing = [k for k in ('id', 'version', 'service', 'provider', 'workflow', 'inputs', 'output') if k not in raw]
        if missing:
            raise TemplateError(f'{path}: missing {", ".join(missing)}')
        workflow = raw['workflow']
        if isinstance(workflow, str):
            with open(os.path.join(os.path.dirname(os.path.abspath(path)), workflow), encoding='utf-8') as f:
                workflow = json.load(f)
        return cls(raw['id'], raw['version'], raw['service'], raw['provider'], raw['inputs'], raw['output'], workflow)


@dataclasses.dataclass
class OutputAsset:
    id: str
    kind: str
    path: str
    meta: dict


@dataclasses.dataclass
class GenerationJob:
    id: str
    template: str
    template_version: int
    status: str  # running, done, failed
    created: float
    finished: float = None
    prompt_id: str = None
    outputs: list = dataclasses.field(default_factory=list)
    not_saved: list = dataclasses.field(default_factory=list)  # what the run produced that this job did not keep
    error: str = None


@dataclasses.dataclass
class ExecResult:
    prompt_id: str
    files: list  # [(ref, bytes)], ref = {'node', 'filename', 'subfolder', 'type'}
    unhandled: list  # 'node.key' for every output this code does not know how to fetch


def is_api_format(workflow):
    """True for the graph that ComfyUI's API export writes: {node id: {'class_type': …, 'inputs': {…}}}."""
    return (isinstance(workflow, dict) and bool(workflow) and all(
        isinstance(n, dict) and isinstance(n.get('class_type'), str) and isinstance(n.get('inputs'), dict)
        for n in workflow.values()))


def is_link(value):
    """A wire from another node: [node id, output slot]."""
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], int)


def _check_target(where, target, workflow):
    if not isinstance(target, dict) or not all(isinstance(target.get(k), str) for k in ('node', 'class_type', 'input')):
        return [f'{where}: a target needs "node", "class_type" and "input"']
    node = workflow.get(target['node'])
    if node is None:
        return [f'{where}: node {target["node"]} is not in the workflow']
    if node['class_type'] != target['class_type']:
        return [f'{where}: node {target["node"]} is a {node["class_type"]}, the template expects {target["class_type"]}']
    if target['input'] not in node['inputs']:
        return [f'{where}: node {target["node"]} ({node["class_type"]}) has no input "{target["input"]}"']
    if is_link(node['inputs'][target['input']]):
        return [f'{where}: input "{target["input"]}" of node {target["node"]} is wired to another node; '
                f'writing a value there would cut the wire']
    return []


def check_template(template):
    """Every problem with the template, as sentences. An empty list means it fits its workflow."""
    wf = template.workflow
    if not is_api_format(wf):
        return ['the workflow is not in API format: export it from ComfyUI with the API export, not the normal save']
    out = []
    if not isinstance(template.inputs, dict) or not template.inputs:
        out.append('the template has no inputs')
    for name, field in (template.inputs.items() if isinstance(template.inputs, dict) else []):
        where = f'input "{name}"'
        if not isinstance(field, dict) or field.get('type') not in TYPES:
            out.append(f'{where}: "type" must be one of {", ".join(TYPES)}')
            continue
        if field.get('required') and 'default' in field:
            out.append(f'{where}: is required and has a default; pick one')
        targets = field.get('to')
        if not isinstance(targets, list) or not targets:
            out.append(f'{where}: "to" must list at least one target')
            continue
        for target in targets:
            out += _check_target(where, target, wf)
        if field['type'] == 'choice':
            choices = field.get('choices')
            if not isinstance(choices, dict) or not choices:
                out.append(f'{where}: a choice needs "choices"')
                continue
            if 'default' in field and field['default'] not in choices:
                out.append(f'{where}: the default "{field["default"]}" is not one of its choices')
            for label, value in choices.items():
                for target in targets:
                    if isinstance(value, dict) and isinstance(target, dict) and target.get('key') not in value:
                        out.append(f'{where}: choice "{label}" has no "{target.get("key")}" for node {target.get("node")}')
        elif 'default' in field:
            out += [f'{where}: default: {p}' for p in _type_problems(field['type'], field['default'])]
    output = template.output
    if not isinstance(output, dict) or not isinstance(output.get('node'), str):
        out.append('the template\'s "output" needs a "node"')
    elif output['node'] not in wf:
        out.append(f'output: node {output["node"]} is not in the workflow')
    elif output.get('class_type') and wf[output['node']]['class_type'] != output['class_type']:
        out.append(f'output: node {output["node"]} is a {wf[output["node"]]["class_type"]}, '
                   f'the template expects {output["class_type"]}')
    return out


def select_template(templates, service, template_id=None):
    """The template to use. By id when one is given; otherwise the single template for the service."""
    if template_id is not None:
        found = [t for t in templates if t.id == template_id]
    else:
        found = [t for t in templates if t.service == service]
    if len(found) == 1:
        return found[0]
    what = f'id "{template_id}"' if template_id is not None else f'service "{service}"'
    if not found:
        raise TemplateError(f'no template for {what}')
    raise TemplateError(f'{len(found)} templates for {what} ({", ".join(t.id for t in found)}): name one by id')


def _type_problems(kind, value):
    if kind == 'text' and not isinstance(value, str):
        return ['must be text']
    if kind == 'image' and not (isinstance(value, str) and value):
        return ['must be the path of an image file']
    if kind == 'int' and (isinstance(value, bool) or not isinstance(value, int)):
        return ['must be a whole number']
    if kind == 'float' and (isinstance(value, bool) or not isinstance(value, (int, float))):
        return ['must be a number']
    return []


def resolve(template, inputs):
    """{field: value} after defaults and checks. Raises MappingError with every problem, not the first."""
    problems, values = [], {}
    if not isinstance(inputs, dict):
        raise MappingError(['the inputs must be an object of field names and values'])
    for name in inputs:
        if name not in template.inputs:
            problems.append(f'"{name}" is not an input of template {template.id}')
    for name, field in template.inputs.items():
        if name in inputs:
            value = inputs[name]
        elif 'default' in field:
            value = field['default']
        elif field.get('required'):
            problems.append(f'"{name}" is required')
            continue
        else:
            continue  # optional and not given: the workflow keeps its own value
        if field['type'] == 'choice':
            if not isinstance(value, str) or value not in field['choices']:
                problems.append(f'"{name}" must be one of {", ".join(field["choices"])}')
                continue
        else:
            bad = _type_problems(field['type'], value)
            if bad:
                problems += [f'"{name}" {p}' for p in bad]
                continue
        values[name] = value
    if problems:
        raise MappingError(problems)
    return values


def map_job(template, inputs, uploaded=None):
    """A new workflow with the inputs written in. The template's own workflow is not changed.

    uploaded maps an image field to the name the provider gave the uploaded file.
    """
    values, uploaded = resolve(template, inputs), uploaded or {}
    workflow = copy.deepcopy(template.workflow)
    for name, value in values.items():
        field = template.inputs[name]
        if field['type'] == 'image':
            if name not in uploaded:
                raise MappingError([f'"{name}" is an image and has not been uploaded'])
            value = uploaded[name]
        elif field['type'] == 'choice':
            value = field['choices'][value]
        for target in field['to']:
            workflow[target['node']]['inputs'][target['input']] = value[target['key']] if isinstance(value, dict) else value
    return workflow


class ComfyExecutor:
    """The only code that talks to a ComfyUI server."""

    provider = 'comfyui'

    def __init__(self, server='http://127.0.0.1:8188', timeout=600.0, poll=1.0, headers=None,
                 sleep=time.sleep, clock=time.monotonic):
        self.server, self.timeout, self.poll = server.rstrip('/'), timeout, poll
        self.headers, self.sleep, self.clock = dict(headers or {}), sleep, clock

    def _request(self, method, path, body=None, headers=None):
        request = urllib.request.Request(self.server + path, data=body, method=method,
                                         headers={**self.headers, **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as err:
            with err:
                return err.code, err.read()
        except (urllib.error.URLError, OSError) as err:
            raise ProviderError(f'cannot reach {self.server}{path}: {getattr(err, "reason", err)}') from err

    def _json(self, method, path, body=None, headers=None, also=()):
        status, raw = self._request(method, path, body, headers)
        if status != 200 and status not in also:
            raise ProviderError(f'{method} {path} answered {status}')
        try:
            return status, json.loads(raw)
        except ValueError as err:
            raise ProviderError(f'{method} {path} did not answer with JSON') from err

    def upload(self, path):
        """Send a local image to the server's input folder. Returns the name the server stored it under."""
        with open(path, 'rb') as f:
            data = f.read()
        boundary = uuid.uuid4().hex
        filename = os.path.basename(path).replace('"', '')
        kind = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        body = b''.join([
            f'--{boundary}\r\nContent-Disposition: form-data; name="type"\r\n\r\ninput\r\n'.encode(),
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{filename}"\r\n'
            f'Content-Type: {kind}\r\n\r\n'.encode(), data, f'\r\n--{boundary}--\r\n'.encode()])
        _, answer = self._json('POST', '/upload/image', body, {'Content-Type': f'multipart/form-data; boundary={boundary}'})
        if not isinstance(answer, dict) or not isinstance(answer.get('name'), str):
            raise ProviderError('POST /upload/image did not name the stored file')
        return answer['name']

    def submit(self, workflow):
        """Queue the workflow. Returns the prompt id."""
        status, answer = self._json('POST', '/prompt', json.dumps({'prompt': workflow}).encode(),
                                    {'Content-Type': 'application/json'}, also=(400,))
        if status == 400:
            answer = answer if isinstance(answer, dict) else {}
            raise PromptRejected(answer.get('error', {}), answer.get('node_errors', {}))
        if not isinstance(answer, dict) or not isinstance(answer.get('prompt_id'), str):
            raise ProviderError('POST /prompt did not return a prompt_id')
        return answer['prompt_id']

    def wait(self, prompt_id):
        """Ask GET /history/{prompt_id} until the run is there. Returns its history entry."""
        start = self.clock()
        while True:
            _, answer = self._json('GET', '/history/' + urllib.parse.quote(prompt_id, safe=''))
            if isinstance(answer, dict) and prompt_id in answer:
                entry = answer[prompt_id]
                if not isinstance(entry, dict):
                    raise ProviderError(f'GET /history/{prompt_id} returned something that is not a run')
                status = entry.get('status')
                if isinstance(status, dict) and status.get('status_str') == 'error':
                    raise ExecutionFailed(f'run {prompt_id} failed: {json.dumps(status.get("messages"))[:600]}')
                return entry
            if self.clock() - start >= self.timeout:
                raise ExecutionTimeout(f'run {prompt_id} gave no result within {self.timeout:g} s')
            self.sleep(self.poll)

    def fetch(self, ref):
        """The bytes of one output file."""
        query = urllib.parse.urlencode({'filename': ref['filename'], 'subfolder': ref['subfolder'], 'type': ref['type']})
        status, raw = self._request('GET', '/view?' + query)
        if status != 200:
            raise ProviderError(f'GET /view for {ref["filename"]} answered {status}')
        return raw

    def run(self, workflow):
        prompt_id = self.submit(workflow)
        entry = self.wait(prompt_id)
        refs, unhandled = [], []
        outputs = entry.get('outputs') or {}
        for node, produced in outputs.items():
            for key, items in (produced.items() if isinstance(produced, dict) else []):
                if key != 'images' or not isinstance(items, list):
                    unhandled.append(f'{node}.{key}')
                    continue
                for item in items:
                    if isinstance(item, dict) and isinstance(item.get('filename'), str):
                        refs.append({'node': node, 'filename': item['filename'],
                                     'subfolder': item.get('subfolder', ''), 'type': item.get('type', 'output')})
                    else:
                        unhandled.append(f'{node}.{key}')
        return ExecResult(prompt_id, [(ref, self.fetch(ref)) for ref in refs], unhandled)


class FakeExecutor:
    """The executor's interface without a server. Records what it was given; returns what it was told to."""

    def __init__(self, files=None, unhandled=(), provider='comfyui', error=None):
        self.files, self.unhandled, self.provider, self.error = files, list(unhandled), provider, error
        self.uploads, self.workflows = [], []

    def upload(self, path):
        self.uploads.append(path)
        return os.path.basename(path)

    def run(self, workflow):
        self.workflows.append(workflow)
        if self.error:
            raise self.error
        return ExecResult('fake-' + str(len(self.workflows)), list(self.files or []), list(self.unhandled))


def _extension(filename):
    ext = os.path.splitext(filename)[1].lstrip('.').lower()
    return '.' + ext if ext.isalnum() and len(ext) <= 5 else '.bin'


def run_job(template, inputs, executor, out_dir, now=time.time):
    """Check, upload, map, run and save. Always returns a GenerationJob: status 'done', or 'failed' with .error."""
    job = GenerationJob(str(uuid.uuid4()), template.id, template.version, 'running', now())
    try:
        problems = check_template(template)
        if problems:
            raise TemplateError(f'template {template.id} does not fit its workflow: ' + '; '.join(problems))
        if template.provider != executor.provider:
            raise TemplateError(f'template {template.id} is for {template.provider}, the executor is {executor.provider}')
        values = resolve(template, inputs)
        uploaded = {name: executor.upload(values[name]) for name in values if template.inputs[name]['type'] == 'image'}
        result = executor.run(map_job(template, inputs, uploaded))
        job.prompt_id = result.prompt_id
        node = template.output['node']
        wanted = [(ref, data) for ref, data in result.files if ref['node'] == node]
        job.not_saved = sorted({f'{ref["node"]}.images' for ref, _ in result.files if ref['node'] != node} | set(result.unhandled))
        if not wanted:
            raise ExecutionFailed(f'output node {node} produced no image')
        os.makedirs(out_dir, exist_ok=True)
        for i, (ref, data) in enumerate(wanted):
            path = os.path.join(out_dir, f'{job.id}-{i}{_extension(ref["filename"])}')  # never the server's own name
            with open(path, 'wb') as f:
                f.write(data)
            job.outputs.append(OutputAsset(f'{job.id}-{i}', 'image', path, {
                'bytes': len(data), 'provider': executor.provider, 'prompt_id': result.prompt_id, 'source': ref}))
        job.status = 'done'
    except (PipelineError, OSError) as err:
        job.status, job.error = 'failed', str(err)
    job.finished = now()
    return job


def main(argv):
    ap = argparse.ArgumentParser(description='ComfyUI behind one boundary: check a template, map a job, run it.')
    sub = ap.add_subparsers(dest='command', required=True)
    for name, text in (('check', 'does the template fit its workflow?'), ('map', 'print the workflow that would be sent'),
                       ('run', 'run the job on a ComfyUI server and save its outputs')):
        p = sub.add_parser(name, help=text)
        p.add_argument('template')
        if name != 'check':
            p.add_argument('spec', help='a JSON file: {"service": "...", "inputs": {...}}')
        if name == 'run':
            p.add_argument('--server', default='http://127.0.0.1:8188')
            p.add_argument('--out', default='outputs')
            p.add_argument('--timeout', type=float, default=600.0, help='seconds to wait for the result')
    args = ap.parse_args(argv)
    try:
        template = WorkflowTemplate.load(args.template)
        spec = None
        if args.command != 'check':
            with open(args.spec, encoding='utf-8') as f:
                spec = json.load(f)
    except (OSError, ValueError) as err:
        print(f'comfy_pipeline: {err}', file=sys.stderr)
        return 2
    except TemplateError as err:
        print(f'comfy_pipeline: {err}', file=sys.stderr)
        return 1
    problems = check_template(template)
    for problem in problems:
        print(f'{args.template}: {problem}')
    if args.command == 'check':
        print(f'{args.template}: template {template.id} v{template.version}, {len(template.inputs)} inputs, '
              f'{len(problems)} problem{"" if len(problems) == 1 else "s"}')
    if problems:
        return 1
    if args.command == 'check':
        return 0
    if not isinstance(spec, dict) or not isinstance(spec.get('inputs'), dict):
        print(f'{args.spec}: a spec is {{"service": "...", "inputs": {{...}}}}')
        return 1
    if spec.get('service') != template.service:
        print(f'{args.spec}: the spec is for service "{spec.get("service")}", the template for "{template.service}"')
        return 1
    if args.command == 'map':
        try:
            values = resolve(template, spec['inputs'])
            names = {n: os.path.basename(v) for n, v in values.items() if template.inputs[n]['type'] == 'image'}
            print(json.dumps(map_job(template, spec['inputs'], names), indent=2))
        except MappingError as err:
            for problem in err.problems:
                print(f'{args.spec}: {problem}')
            return 1
        return 0
    job = run_job(template, spec['inputs'], ComfyExecutor(args.server, timeout=args.timeout), args.out)
    for asset in job.outputs:
        print(asset.path)
    for item in job.not_saved:
        print(f'not saved: {item}')
    print(f'job {job.id}: {job.status}' + (f': {job.error}' if job.error else f', {len(job.outputs)} saved'))
    return 0 if job.status == 'done' else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
