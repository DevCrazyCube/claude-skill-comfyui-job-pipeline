"""AFTER: the one place in the app that starts a generation. A sketch of a route handler, framework left out.

It hands a JobSpec to the pipeline and returns the job. No node id, class name or ComfyUI address appears here;
those live in the template file and in comfy_pipeline.py, which --allow names when find_leaks.py runs.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..', 'scripts'))
import comfy_pipeline as cp  # noqa: E402

TEMPLATES = [cp.WorkflowTemplate.load(os.path.join(os.path.dirname(__file__), '..', '..', 'txt2img-basic.template.json'))]
# The address is configuration; without it the executor uses its own default.
EXECUTOR = cp.ComfyExecutor(os.environ['GENERATION_SERVER']) if os.environ.get('GENERATION_SERVER') else cp.ComfyExecutor()


def generate(spec, out_dir='outputs'):
    """spec: {"service": "image", "inputs": {...}} as the frontend posted it. Returns what the frontend gets back."""
    template = cp.select_template(TEMPLATES, spec.get('service'), spec.get('template'))
    job = cp.run_job(template, spec.get('inputs', {}), EXECUTOR, out_dir)
    return {'id': job.id, 'status': job.status, 'error': job.error,
            'outputs': [{'kind': a.kind, 'url': '/files/' + os.path.basename(a.path)} for a in job.outputs]}
