// AFTER: the component knows the product's words (a prompt, a shape) and one route of its own app.
// Which engine draws the picture, and how its graph is wired, is not its business.
export function GenerateButton({ prompt, portrait }: { prompt: string; portrait: boolean }) {
  async function generate() {
    const job = await fetch('/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ service: 'image', inputs: { prompt, shape: portrait ? 'portrait' : 'square' } }),
    }).then((r) => r.json());

    if (job.status === 'done') window.open(job.outputs[0].url);
    else alert(job.error);
  }

  return <button onClick={generate}>Generate</button>;
}
