"""Tests for scripts/comfy_pipeline.py. The server is tests/mock_comfy.py; no ComfyUI is needed or used."""
import contextlib
import copy
import io
import json
import os
import socket
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
sys.path.insert(0, HERE)

import comfy_pipeline as cp  # noqa: E402
from mock_comfy import PNG, MockComfy  # noqa: E402

TEMPLATE = os.path.join(ROOT, 'examples', 'txt2img-basic.template.json')
SPEC = os.path.join(ROOT, 'examples', 'spec.json')


def template(**changes):
    t = cp.WorkflowTemplate.load(TEMPLATE)
    t.inputs, t.workflow, t.output = copy.deepcopy(t.inputs), copy.deepcopy(t.workflow), dict(t.output)
    for key, value in changes.items():
        setattr(t, key, value)
    return t


def with_image_input():
    t = template()
    t.workflow['8'] = {'class_type': 'LoadImage', 'inputs': {'image': 'placeholder.png'}}
    t.inputs['photo'] = {'type': 'image', 'required': True,
                         'to': [{'node': '8', 'class_type': 'LoadImage', 'input': 'image'}]}
    return t


def cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cp.main(list(argv))
        except SystemExit as exit_:
            code = exit_.code
    return code, out.getvalue(), err.getvalue()


class Template(unittest.TestCase):
    def test_the_shipped_example_fits_its_workflow(self):
        self.assertEqual(cp.check_template(template()), [])

    def test_a_normal_save_is_not_the_api_format(self):
        t = template(workflow={'nodes': [{'id': 1, 'type': 'KSampler'}], 'links': []})
        self.assertIn('not in API format', cp.check_template(t)[0])

    def test_a_node_that_is_gone(self):
        t = template()
        del t.workflow['2']
        self.assertEqual(cp.check_template(t), ['input "prompt": node 2 is not in the workflow'])

    def test_a_node_that_became_another_class(self):
        t = template()
        t.workflow['5']['class_type'] = 'KSamplerAdvanced'
        problems = cp.check_template(t)
        self.assertEqual(len(problems), 2)  # quality and seed both point at node 5
        self.assertIn('node 5 is a KSamplerAdvanced, the template expects KSampler', problems[0])

    def test_an_input_that_is_gone(self):
        t = template()
        del t.workflow['5']['inputs']['seed']
        self.assertEqual(cp.check_template(t), ['input "seed": node 5 (KSampler) has no input "seed"'])

    def test_a_target_that_is_a_wire(self):
        t = template()
        t.inputs['seed']['to'][0]['input'] = 'model'
        self.assertIn('would cut the wire', cp.check_template(t)[0])

    def test_the_output_node(self):
        t = template(output={'node': '9', 'class_type': 'SaveImage'})
        self.assertEqual(cp.check_template(t), ['output: node 9 is not in the workflow'])
        t = template(output={'node': '6', 'class_type': 'SaveImage'})
        self.assertIn('node 6 is a VAEDecode', cp.check_template(t)[0])

    def test_choices(self):
        t = template()
        del t.inputs['shape']['choices']['portrait']['height']
        t.inputs['quality']['default'] = 'ultra'
        problems = cp.check_template(t)
        self.assertIn('input "shape": choice "portrait" has no "height" for node 4', problems)
        self.assertIn('input "quality": the default "ultra" is not one of its choices', problems)

    def test_required_with_a_default_unknown_type_and_bad_default(self):
        t = template()
        t.inputs['prompt']['default'] = 'x'
        t.inputs['avoid']['type'] = 'string'
        t.inputs['seed']['default'] = 'seven'
        problems = cp.check_template(t)
        self.assertIn('input "prompt": is required and has a default; pick one', problems)
        self.assertIn('input "avoid": "type" must be one of text, int, float, choice, image', problems)
        self.assertIn('input "seed": default: must be a whole number', problems)

    def test_load_reports_missing_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 't.json')
            with open(path, 'w') as f:
                json.dump({'id': 'x', 'version': 1}, f)
            with self.assertRaises(cp.TemplateError) as caught:
                cp.WorkflowTemplate.load(path)
            self.assertIn('missing service, provider, workflow, inputs, output', str(caught.exception))

    def test_select(self):
        a, b = template(id='a'), template(id='b')
        video = template(id='v', service='video')
        self.assertIs(cp.select_template([a, video], 'image'), a)
        self.assertIs(cp.select_template([a, b, video], 'image', 'b'), b)
        with self.assertRaises(cp.TemplateError) as caught:
            cp.select_template([a, b], 'image')
        self.assertIn('2 templates for service "image" (a, b): name one by id', str(caught.exception))
        with self.assertRaises(cp.TemplateError):
            cp.select_template([a], 'audio')


class Mapping(unittest.TestCase):
    def test_values_land_on_their_nodes(self):
        t = template()
        wf = cp.map_job(t, {'prompt': 'a red bicycle', 'shape': 'portrait', 'quality': 'high', 'seed': 7})
        self.assertEqual(wf['2']['inputs']['text'], 'a red bicycle')
        self.assertEqual((wf['4']['inputs']['width'], wf['4']['inputs']['height']), (576, 1024))
        self.assertEqual(wf['5']['inputs']['steps'], 32)
        self.assertEqual(wf['5']['inputs']['seed'], 7)

    def test_defaults_and_untouched_optionals(self):
        wf = cp.map_job(template(), {'prompt': 'x'})
        self.assertEqual((wf['4']['inputs']['width'], wf['5']['inputs']['steps'], wf['5']['inputs']['seed']), (768, 20, 1))
        self.assertEqual(wf['3']['inputs']['text'], 'blurry, text, watermark')  # "avoid" not given: the workflow's own

    def test_the_template_is_not_changed_and_wires_stay(self):
        t = template()
        before = copy.deepcopy(t.workflow)
        wf = cp.map_job(t, {'prompt': 'changed'})
        self.assertEqual(t.workflow, before)
        self.assertEqual(wf['5']['inputs']['model'], ['1', 0])

    def test_every_problem_is_reported_at_once(self):
        with self.assertRaises(cp.MappingError) as caught:
            cp.map_job(template(), {'node_6_text': 'x', 'shape': 'round', 'seed': '7', 'quality': 3})
        self.assertEqual(caught.exception.problems, [
            '"node_6_text" is not an input of template txt2img-basic',
            '"prompt" is required',
            '"shape" must be one of square, portrait, landscape',
            '"quality" must be one of draft, normal, high',
            '"seed" must be a whole number'])

    def test_true_is_not_a_number_and_text_is_text(self):
        for inputs in ({'prompt': 'x', 'seed': True}, {'prompt': 5}, {'prompt': 'x', 'seed': 1.5}):
            with self.assertRaises(cp.MappingError):
                cp.map_job(template(), inputs)

    def test_inputs_must_be_an_object(self):
        with self.assertRaises(cp.MappingError):
            cp.map_job(template(), ['prompt'])

    def test_an_image_needs_its_upload(self):
        t = with_image_input()
        self.assertEqual(cp.check_template(t), [])
        with self.assertRaises(cp.MappingError) as caught:
            cp.map_job(t, {'prompt': 'x', 'photo': '/tmp/in.png'})
        self.assertIn('has not been uploaded', str(caught.exception))
        wf = cp.map_job(t, {'prompt': 'x', 'photo': '/tmp/in.png'}, {'photo': 'in (1).png'})
        self.assertEqual(wf['8']['inputs']['image'], 'in (1).png')


class Executor(unittest.TestCase):
    def serve(self, **options):
        mock = MockComfy(**options)
        url = mock.start()
        self.addCleanup(mock.stop)
        return mock, cp.ComfyExecutor(url, timeout=5, poll=0)

    def test_submit_wait_fetch(self):
        mock, executor = self.serve(polls_before_done=3)
        workflow = cp.map_job(template(), {'prompt': 'a red bicycle'})
        result = executor.run(workflow)
        self.assertEqual(mock.prompts[result.prompt_id], workflow)
        self.assertEqual(mock.polls[result.prompt_id], 4)  # three empty answers, then the run
        self.assertEqual(len(result.files), 1)
        ref, data = result.files[0]
        self.assertEqual(ref, {'node': '7', 'filename': 'job_00001_.png', 'subfolder': '', 'type': 'output'})
        self.assertEqual(data, PNG)
        self.assertEqual(result.unhandled, [])
        self.assertEqual([p for m, p in mock.requests if m == 'POST'], ['/prompt'])

    def test_a_refused_workflow(self):
        _, executor = self.serve(reject=True)
        with self.assertRaises(cp.PromptRejected) as caught:
            executor.submit({'5': {'class_type': 'KSampler', 'inputs': {}}})
        self.assertEqual(sorted(caught.exception.node_errors), ['5'])
        self.assertIn('the mock refuses this (nodes: 5)', str(caught.exception))

    def test_a_run_that_fails(self):
        _, executor = self.serve(fail=True, polls_before_done=0)
        with self.assertRaises(cp.ExecutionFailed) as caught:
            executor.run({'1': {'class_type': 'X', 'inputs': {}}})
        self.assertIn('boom', str(caught.exception))

    def test_a_run_that_never_ends(self):
        mock = MockComfy(polls_before_done=10 ** 9)
        url = mock.start()
        self.addCleanup(mock.stop)
        ticks = iter(range(0, 1000, 10))
        slept = []
        executor = cp.ComfyExecutor(url, timeout=30, poll=2, sleep=slept.append, clock=lambda: next(ticks))
        with self.assertRaises(cp.ExecutionTimeout) as caught:
            executor.wait(executor.submit({'1': {'class_type': 'X', 'inputs': {}}}))
        self.assertIn('within 30 s', str(caught.exception))
        self.assertEqual(slept, [2, 2])  # the clock reads 0, 10, 20, 30: two waits, then the limit

    def test_upload_sends_the_bytes_unchanged(self):
        mock, executor = self.serve()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'in put.png')
            with open(path, 'wb') as f:
                f.write(PNG)
            self.assertEqual(executor.upload(path), 'in put.png')
        self.assertEqual(mock.uploads, {'in put.png': PNG})

    def test_outputs_it_does_not_know_are_named_not_dropped(self):
        outputs = {'7': {'images': [{'filename': 'job_00001_.png', 'subfolder': '', 'type': 'output'}]},
                   '9': {'gifs': [{'filename': 'a.gif'}], 'images': ['not-a-ref']}}
        _, executor = self.serve(outputs=outputs, polls_before_done=0)
        result = executor.run({'1': {'class_type': 'X', 'inputs': {}}})
        self.assertEqual(len(result.files), 1)
        self.assertEqual(result.unhandled, ['9.gifs', '9.images'])

    def test_a_file_the_server_does_not_have(self):
        outputs = {'7': {'images': [{'filename': 'gone.png', 'subfolder': '', 'type': 'output'}]}}
        _, executor = self.serve(outputs=outputs, polls_before_done=0)
        with self.assertRaises(cp.ProviderError) as caught:
            executor.run({'1': {'class_type': 'X', 'inputs': {}}})
        self.assertIn('GET /view for gone.png answered 404', str(caught.exception))

    def test_an_answer_that_is_not_json(self):
        _, executor = self.serve(history_garbage=True)
        with self.assertRaises(cp.ProviderError) as caught:
            executor.wait('abc')
        self.assertIn('did not answer with JSON', str(caught.exception))

    def test_no_server(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        with self.assertRaises(cp.ProviderError) as caught:
            cp.ComfyExecutor(f'http://127.0.0.1:{port}').submit({})
        self.assertIn('cannot reach', str(caught.exception))


class Job(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, 'out')

    def ref(self, node='7', filename='job_00001_.png'):
        return {'node': node, 'filename': filename, 'subfolder': '', 'type': 'output'}

    def test_done(self):
        fake = cp.FakeExecutor(files=[(self.ref(), b'one'), (self.ref(), b'two')])
        job = cp.run_job(template(), {'prompt': 'a red bicycle'}, fake, self.out)
        self.assertEqual((job.status, job.error, job.template, job.template_version), ('done', None, 'txt2img-basic', 1))
        self.assertEqual(fake.workflows[0]['2']['inputs']['text'], 'a red bicycle')
        self.assertEqual([a.kind for a in job.outputs], ['image', 'image'])
        with open(job.outputs[1].path, 'rb') as f:
            self.assertEqual(f.read(), b'two')
        self.assertEqual(job.outputs[0].meta['source'], self.ref())
        self.assertGreaterEqual(job.finished, job.created)

    def test_bad_inputs_fail_the_job_and_nothing_is_sent(self):
        fake = cp.FakeExecutor(files=[(self.ref(), b'x')])
        job = cp.run_job(template(), {'shape': 'round'}, fake, self.out)
        self.assertEqual(job.status, 'failed')
        self.assertIn('"prompt" is required', job.error)
        self.assertEqual(fake.workflows, [])
        self.assertFalse(os.path.exists(self.out))

    def test_a_template_that_no_longer_fits_fails_before_anything_is_sent(self):
        t = template()
        t.workflow['5']['class_type'] = 'KSamplerAdvanced'
        fake = cp.FakeExecutor(files=[(self.ref(), b'x')])
        job = cp.run_job(t, {'prompt': 'x'}, fake, self.out)
        self.assertEqual(job.status, 'failed')
        self.assertIn('does not fit its workflow', job.error)
        self.assertEqual(fake.workflows, [])

    def test_the_wrong_provider(self):
        job = cp.run_job(template(), {'prompt': 'x'}, cp.FakeExecutor(provider='other'), self.out)
        self.assertIn('is for comfyui, the executor is other', job.error)

    def test_an_executor_error_becomes_a_failed_job(self):
        fake = cp.FakeExecutor(error=cp.PromptRejected({'message': 'no'}, {'5': {}}))
        job = cp.run_job(template(), {'prompt': 'x'}, fake, self.out)
        self.assertEqual(job.status, 'failed')
        self.assertIn('ComfyUI rejected the workflow: no (nodes: 5)', job.error)

    def test_nothing_from_the_output_node(self):
        fake = cp.FakeExecutor(files=[(self.ref(node='6'), b'preview')], unhandled=['9.gifs'])
        job = cp.run_job(template(), {'prompt': 'x'}, fake, self.out)
        self.assertEqual(job.status, 'failed')
        self.assertEqual(job.error, 'output node 7 produced no image')
        self.assertEqual(job.not_saved, ['6.images', '9.gifs'])

    def test_the_servers_file_name_never_becomes_a_path(self):
        fake = cp.FakeExecutor(files=[(self.ref(filename='../../evil.sh;rm'), b'x'), (self.ref(filename='a.PNG'), b'y')])
        job = cp.run_job(template(), {'prompt': 'x'}, fake, self.out)
        names = [os.path.basename(a.path) for a in job.outputs]
        self.assertEqual(names, [f'{job.id}-0.bin', f'{job.id}-1.png'])
        self.assertEqual(sorted(os.listdir(self.out)), sorted(names))

    def test_images_are_uploaded_first(self):
        fake = cp.FakeExecutor(files=[(self.ref(), b'x')])
        job = cp.run_job(with_image_input(), {'prompt': 'x', 'photo': '/somewhere/in.png'}, fake, self.out)
        self.assertEqual(job.status, 'done')
        self.assertEqual(fake.uploads, ['/somewhere/in.png'])
        self.assertEqual(fake.workflows[0]['8']['inputs']['image'], 'in.png')

    def test_end_to_end_against_the_mock_server(self):
        mock = MockComfy()
        url = mock.start()
        self.addCleanup(mock.stop)
        job = cp.run_job(template(), {'prompt': 'a red bicycle'}, cp.ComfyExecutor(url, poll=0), self.out)
        self.assertEqual(job.status, 'done', job.error)
        self.assertEqual(mock.prompts[job.prompt_id]['2']['inputs']['text'], 'a red bicycle')
        with open(job.outputs[0].path, 'rb') as f:
            self.assertEqual(f.read(), PNG)


class CommandLine(unittest.TestCase):
    def test_check(self):
        code, out, _ = cli('check', TEMPLATE)
        self.assertEqual(code, 0)
        self.assertIn('template txt2img-basic v1, 5 inputs, 0 problems', out)

    def test_check_a_template_that_drifted(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = template()
            del t.workflow['2']
            raw = {'id': t.id, 'version': 2, 'service': t.service, 'provider': t.provider, 'workflow': t.workflow,
                   'inputs': t.inputs, 'output': t.output}
            path = os.path.join(tmp, 't.json')
            with open(path, 'w') as f:
                json.dump(raw, f)
            code, out, _ = cli('check', path)
        self.assertEqual(code, 1)
        self.assertIn('input "prompt": node 2 is not in the workflow', out)
        self.assertIn('1 problem\n', out)

    def test_map_prints_the_workflow(self):
        code, out, _ = cli('map', TEMPLATE, SPEC)
        self.assertEqual(code, 0)
        wf = json.loads(out)
        self.assertEqual(wf['2']['inputs']['text'], 'a red bicycle against a brick wall, overcast')
        self.assertEqual((wf['4']['inputs']['width'], wf['5']['inputs']['steps'], wf['5']['inputs']['seed']), (576, 10, 42))

    def test_map_with_bad_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = os.path.join(tmp, 's.json')
            with open(spec, 'w') as f:
                json.dump({'service': 'image', 'inputs': {'shape': 'round'}}, f)
            code, out, _ = cli('map', TEMPLATE, spec)
            self.assertEqual(code, 1)
            self.assertIn('"prompt" is required', out)
            with open(spec, 'w') as f:
                json.dump({'service': 'video', 'inputs': {}}, f)
            code, out, _ = cli('map', TEMPLATE, spec)
            self.assertEqual(code, 1)
            self.assertIn('the spec is for service "video", the template for "image"', out)

    def test_run_against_the_mock_server(self):
        mock = MockComfy(polls_before_done=1)
        url = mock.start()
        self.addCleanup(mock.stop)
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = cli('run', TEMPLATE, SPEC, '--server', url, '--out', tmp, '--timeout', '5')
            self.assertEqual(code, 0, out)
            saved = os.listdir(tmp)
            self.assertEqual(len(saved), 1)
            self.assertIn(os.path.join(tmp, saved[0]), out)
            self.assertIn('done, 1 saved', out)

    def test_a_missing_file_is_exit_2(self):
        code, _, err = cli('check', '/no/such/template.json')
        self.assertEqual(code, 2)
        self.assertIn('comfy_pipeline:', err)


if __name__ == '__main__':
    unittest.main()
