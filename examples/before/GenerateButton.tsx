// BEFORE: the component is the pipeline. Written for this repo as the thing to get rid of; it is not from any app.
// Re-export the workflow and node "6" becomes node "12": this button then sends the prompt to the wrong node, or
// to none, and nothing says so.
import workflow from './workflow_api.json';

export function GenerateButton({ prompt, portrait }: { prompt: string; portrait: boolean }) {
  async function generate() {
    const graph = structuredClone(workflow) as Record<string, any>;
    graph['6'].inputs.text = prompt;
    graph['5']['inputs']['width'] = portrait ? 576 : 768;
    graph['5']['inputs']['height'] = portrait ? 1024 : 768;
    const sampler = Object.values(graph).find((node) => node.class_type === 'KSampler');
    sampler.inputs.seed = Math.floor(Math.random() * 1e9);

    const queued = await fetch('http://127.0.0.1:8188/prompt', {
      method: 'POST',
      body: JSON.stringify({ prompt: graph }),
    }).then((r) => r.json());

    let entry;
    while (!entry) {
      await new Promise((r) => setTimeout(r, 1000));
      const history = await fetch(`http://127.0.0.1:8188/history/${queued.prompt_id}`).then((r) => r.json());
      entry = history[queued.prompt_id];
    }
    const image = entry.outputs['9'].images[0];
    window.open(`http://127.0.0.1:8188/view?filename=${image.filename}&type=output`);
  }

  return <button onClick={generate}>Generate</button>;
}
