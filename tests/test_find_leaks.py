"""Tests for scripts/find_leaks.py, and that the "after" example really runs."""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

import comfy_pipeline as cp  # noqa: E402
import find_leaks  # noqa: E402


def kinds(line):
    return [kind for _, kind, _ in find_leaks.scan_text(line)]


def cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = find_leaks.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Patterns(unittest.TestCase):
    def test_a_node_id_used_as_an_address(self):
        for line in ('workflow["6"]["inputs"]["text"] = prompt',
                     "graph['3'].inputs.seed = 1",
                     'prompt[ "12" ]?.inputs',
                     "const image = entry.outputs['9'].images[0]",
                     'history[id]["outputs"]["9"]'):
            self.assertEqual(kinds(line), ['node-id-index'], line)

    def test_ordinary_indexing_is_not_one(self):
        for line in ('rows["6"]["name"]', 'items[6].inputs', 'form.inputs["6"]', 'const outputs = []',
                     'job.outputs[0].url'):
            self.assertEqual(kinds(line), [], line)

    def test_the_graph_format_and_node_names(self):
        self.assertEqual(kinds('if (node.class_type === "KSampler") {'), ['class-type', 'node-class-name'])
        self.assertEqual(kinds('import { SaveImageButton } from "./ui"'), [])
        self.assertEqual(kinds('const n = find("CLIPTextEncode")'), ['node-class-name'])

    def test_endpoints(self):
        for line in ("fetch(base + '/prompt', {", 'fetch(`${base}/history/${id}`)', 'post("/upload/image", form)',
                     'new WebSocket(`ws://${host}/ws?clientId=${id}`)', 'const base = "http://127.0.0.1:8188"',
                     'img.src = base + "/view?filename=" + name'):
            self.assertEqual(kinds(line), ['comfy-endpoint'], line)

    def test_the_apps_own_routes_are_not_one(self):
        for line in ("fetch('/api/generate', {", 'router.push("/prompts")', 'const port = 81880', 'href="/history"'):
            self.assertEqual(kinds(line), [], line)


class Scan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(text)

    def test_allowed_paths_skipped_folders_and_extensions(self):
        leak = 'graph["6"]["inputs"]["text"] = prompt\n'
        self.write('src/pages/Generate.tsx', 'const a = 1\n' + leak)
        self.write('src/generation/mapper.ts', leak)
        self.write('src/generation/comfy/executor.ts', leak)
        self.write('node_modules/pkg/index.js', leak)
        self.write('notes/idea.md', leak)
        found = find_leaks.scan(self.root, allow=['src/generation/*'])
        self.assertEqual(found, [('src/pages/Generate.tsx', 2, 'node-id-index', leak.strip())])
        self.assertEqual(len(find_leaks.scan(self.root)), 3)
        self.assertEqual(len(find_leaks.scan(self.root, extensions='.md')), 1)

    def test_the_command_line(self):
        self.write('src/a.ts', 'ok()\n')
        code, out, _ = cli(self.root)
        self.assertEqual((code, out), (0, 'no leaks found (patterns only; see --help)\n'))
        self.write('src/b.ts', 'x.class_type\n')
        code, out, _ = cli(self.root)
        self.assertEqual(code, 1)
        self.assertEqual(out, 'src/b.ts:1: class-type: x.class_type\n1 leak in 1 file\n')
        code, _, err = cli(os.path.join(self.root, 'nope'))
        self.assertEqual(code, 2)
        self.assertIn('is not a folder', err)


class Examples(unittest.TestCase):
    def test_before_leaks_and_after_does_not(self):
        before = find_leaks.scan(os.path.join(ROOT, 'examples', 'before'))
        self.assertEqual(len(before), 9)
        self.assertEqual(sorted({kind for _, _, kind, _ in before}),
                         ['class-type', 'comfy-endpoint', 'node-class-name', 'node-id-index'])
        self.assertEqual(find_leaks.scan(os.path.join(ROOT, 'examples', 'after')), [])

    def test_the_after_route_runs(self):
        path = os.path.join(ROOT, 'examples', 'after', 'server', 'generate.py')
        spec = importlib.util.spec_from_file_location('after_generate', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        ref = {'node': '7', 'filename': 'job_00001_.png', 'subfolder': '', 'type': 'output'}
        module.EXECUTOR = cp.FakeExecutor(files=[(ref, b'png')])
        with tempfile.TemporaryDirectory() as out:
            answer = module.generate({'service': 'image', 'inputs': {'prompt': 'a red bicycle', 'shape': 'portrait'}}, out)
            self.assertEqual(answer['status'], 'done', answer['error'])
            self.assertEqual(len(answer['outputs']), 1)
            self.assertTrue(answer['outputs'][0]['url'].startswith('/files/' + answer['id']))
            self.assertEqual(module.EXECUTOR.workflows[0]['4']['inputs']['height'], 1024)
            answer = module.generate({'service': 'image', 'inputs': {'shape': 'round'}}, out)
            self.assertEqual(answer['status'], 'failed')
            self.assertIn('"prompt" is required', answer['error'])


if __name__ == '__main__':
    unittest.main()
