---
name: comfyui-job-pipeline
description: Put ComfyUI behind one boundary in an app, so the frontend sends product words (a JobSpec) and never a node id. Use when an app calls ComfyUI's HTTP API (POST /prompt, /history, /view, /upload/image), when a component edits a workflow JSON by node id, when a re-exported workflow silently broke generation, when adding a second workflow or a second provider, or when asked to audit, refactor or build a ComfyUI integration.
---

# ComfyUI job pipeline

ComfyUI is an execution backend. An app that treats its workflow JSON as its own data model breaks every time
the workflow is re-exported, because node ids are the graph editor's, not the app's. This skill keeps ComfyUI
behind one boundary:

```
frontend  ->  JobSpec  ->  WorkflowTemplate  ->  mapper  ->  executor  ->  OutputAsset
(product words)            (the only file that      (pure)     (the only code
                            names nodes)                        that talks HTTP)
```

`scripts/comfy_pipeline.py` is that pipeline in one standard-library file. Use it as it is, or as the reference
when the app is in another language: the shapes matter, not the Python.

## The five pieces

| Piece | What it is | What it may know |
|---|---|---|
| **JobSpec** | `{"service": "image", "inputs": {"prompt": "...", "shape": "portrait"}}` | product words only |
| **WorkflowTemplate** | a JSON file: the exported workflow, the inputs it accepts, where each one lands, the output node | node ids and class names |
| **Mapper** (`map_job`) | pure function: template + inputs → the graph to send | the template format |
| **Executor** (`ComfyExecutor`) | upload, submit, wait, fetch | ComfyUI's HTTP API |
| **OutputAsset / GenerationJob** | what the app stores and shows | nothing about ComfyUI |

## Rules

1. **Nothing outside the template, the mapper and the executor knows ComfyUI.** No node id, `class_type`, node
   class name, endpoint or port in a component, a page, a route handler or a store. Check with
   `python3 scripts/find_leaks.py SRC --allow 'path/to/generation/*'`.
2. **The frontend sends a JobSpec to the app's own route.** It gets a job back: `status`, `outputs`, `error`.
3. **A template names every node twice:** by id and by class (`{"node": "5", "class_type": "KSampler", "input": "seed"}`).
   The class is what catches a re-export that moved the ids.
4. **Run `check` whenever the workflow file changes,** and in CI:
   `python3 scripts/comfy_pipeline.py check TEMPLATE.json` (exit 0 fits, 1 does not).
5. **When the workflow changes, change the template.** Never the UI, never the mapper's code.
6. **Inputs are validated before anything is sent,** and every problem is reported at once. An unknown field is
   an error, not ignored.
7. **The executor is the only code that submits,** and it is replaceable: anything with `provider`, `upload(path)`
   and `run(workflow)` works (`FakeExecutor` in tests). Tests never need a GPU.
8. **A failed job is a result, not an exception.** `run_job` returns `status: "failed"` with the reason.
9. **Files are saved under the job's own name,** never the name the server sent. Outputs the pipeline does not
   handle are listed in `not_saved`, not dropped silently.
10. **One vertical slice first.** One service, one template, end to end, before a second template or provider.
    Two templates for one service must be chosen by id; there is no guessing.

## The workflow file

Export it from ComfyUI in **API format** (the export the ComfyUI example script calls "File -> Export (API)"):
an object of node id → `{"class_type", "inputs"}`. A normal save (`{"nodes": [...], "links": [...]}`) is refused
by `check`. Look the menu up in the user's ComfyUI version rather than assuming where it is.

## The template

```json
{
  "id": "txt2img-basic", "version": 1, "service": "image", "provider": "comfyui",
  "workflow": "txt2img-basic.workflow.json",
  "inputs": {
    "prompt": {"type": "text", "required": true,
               "to": [{"node": "2", "class_type": "CLIPTextEncode", "input": "text"}]},
    "shape":  {"type": "choice", "default": "square",
               "choices": {"square": {"width": 768, "height": 768}, "portrait": {"width": 576, "height": 1024}},
               "to": [{"node": "4", "class_type": "EmptyLatentImage", "input": "width", "key": "width"},
                      {"node": "4", "class_type": "EmptyLatentImage", "input": "height", "key": "height"}]}
  },
  "output": {"node": "7", "class_type": "SaveImage"}
}
```

- Types: `text`, `int`, `float`, `choice`, `image` (a local path; uploaded first, the stored name is written in).
- `required: true` or a `default`, not both. With neither, a field that is not given keeps the workflow's own value.
- A `choice` maps a product word to a value, or to several values picked by each target's `key`.
- A target may not be an input that is wired to another node; `check` refuses it.
- Bump `version` when the template's behaviour changes; the job records it.

## Commands

```bash
python3 scripts/comfy_pipeline.py check TEMPLATE.json              # does the template fit its workflow?
python3 scripts/comfy_pipeline.py map   TEMPLATE.json SPEC.json    # print the graph that would be sent
python3 scripts/comfy_pipeline.py run   TEMPLATE.json SPEC.json --server http://127.0.0.1:8188 --out outputs
python3 scripts/find_leaks.py SRC --allow 'src/generation/*'       # ComfyUI knowledge outside the boundary
```

Exit codes: 0 fine, 1 a problem with the template, the inputs or the run, 2 a file that cannot be read.

## Auditing an existing integration

1. **Read the current state.** Where is `/prompt` called? Where is the workflow JSON loaded and edited? Run
   `find_leaks.py` on the source with nothing allowed; the list is the audit.
2. **Name the product inputs.** For each edited node input, what does the user actually choose? That list is
   the template's `inputs`. Anything nobody chooses stays in the workflow.
3. **Write the template** for the one flow that matters most and make `check` pass.
4. **Move submission behind one route** that takes a JobSpec and calls `run_job` (see `examples/after/`).
5. **Delete the old path.** Rerun `find_leaks.py` with the boundary allowed until it is clean, then keep it in CI.

Do not keep both paths "for now". Existing integration code is not sacred; a second way to submit is how the
coupling comes back.

## What not to claim

- The endpoints and answer shapes here were read from ComfyUI's `server.py` and `execution.py` on the master
  branch. ComfyUI changes; when an answer does not match, read the user's version before guessing.
- `ComfyExecutor` polls `/history`. It does not use the WebSocket, shows no progress, and handles `images`
  outputs only; anything else is named in `not_saved`.
- `find_leaks.py` searches for patterns. A clean run is not proof of no coupling.
- Say what was run. "The tests pass against the mock server" is not "it works against ComfyUI".
