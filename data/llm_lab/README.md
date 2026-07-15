# LLM Lab — local-model benchmarking across our devices

Goal: pick the self-hosted model that replaces the cloud generator
(`AnthropicLLM`, claude-opus-4-8) — the on-prem branch of DDR-06. The lab
measures, per **device × model**, whether our hardware can run a model at a
usable speed and whether that model is accurate enough against the plant
graph and the HAZOP gold set.

Everything is driven from the dashboard's **LLM Lab** tab
(`hazop-web` → http://localhost:8780) or headless:

```bash
python -m hazop.s5_sw.llm_lab --device peter-mbp --model llama3.1:8b \
    --benchmarks capability,graph_accuracy
```

## How it works

- **Hub + workers.** The dashboard is the hub. A worker is nothing but an
  Ollama server reachable over the network — no agent, no install beyond
  Ollama itself.
- **[`llm_lab.yaml`](llm_lab.yaml) is the single source of truth** for the
  device/model matrix. Edit it, commit, done — the hub re-reads it on every
  page refresh (no restart). The UI annotates it with live state (device
  reachable? model installed?) and can pull missing models, but never edits
  the file.
- Every run — **including failures; an out-of-memory is a result** — is
  appended to `runs.jsonl` (gitignored, device-local) and shown in the
  comparison table.

## The three benchmarks

| benchmark | question it answers | metrics |
|---|---|---|
| `capability` | can this device run this model at a usable speed, and does the model hold our JSON-schema output contract at all | load time, tokens/s, wall time, `json_schema_ok` |
| `gold_eval` | does a real local generator pass the Fable §4 gates on the pump/vessel gold set (minutes — it runs all 29 deviations) | deviation coverage, cause/consequence/safeguard recall, hallucination rate, grounding precision, MDL-12 latency P50/P95 |
| `graph_accuracy` | can the model translate a plant engineer's plain-English question into the correct graph query | kind / intent / result accuracy over a 30-question suite with known answers on the 2401 plant graph; hallucinated tags **fail closed** and score as failures |

## Findings so far (2026-07-15, MacBook Pro M5 Pro 24 GB)

**Capability** — `llama3.2:3b`: 107 tokens/s, 1.5 s load, JSON-schema
contract held. Plenty of headroom on the M5 Pro.

**Graph accuracy** — the run history is the story (result accuracy over
30 questions):

| iteration | llama3.2:3b | llama3.1:8b | failed-closed |
|---|---|---|---|
| initial harness + prompt | 13% | — | 25/30 |
| + nullish-field normalization | 30% | 27% | 18–20/30 |
| + prompt rewrite (field rules, worked examples) | **73%** | **90%** | **0** |

Two lessons that produced those jumps, worth keeping for any local-LLM
structured-output work:

1. **Small models emit the *string* `"null"`** (or stray lists) for unused
   JSON-schema fields. Those strings reached tag grounding and failed
   closed. Translator payloads now normalize nullish/non-string values to
   real `None` before grounding (`query._clean_field`).
2. **Show the model what a tag looks like.** The original prompt never
   did, so models put category words in tag fields (`tag: "relief_valve"`).
   The rewritten prompt states field rules explicitly and includes four
   worked examples. That alone tripled accuracy.

**Verdict so far:** `llama3.1:8b` at 90% result accuracy is a viable local
translator; `llama3.2:3b` at 73% is not (it stays useful as the smaller
evidence-critic model per DDR-02). A prior full local gold-set eval
(`evaluate.py --llm local`, same gates) had `llama3.1:8b` passing with 100%
deviation coverage, 90% cause recall, 0% hallucination at ~6.6 s/deviation.
Next: the same matrix on the PC's GPU, and the 7B–14B qwen candidates.

## Connecting another device (e.g. the PC)

On the worker machine (Windows/Linux/macOS):

1. Install Ollama (https://ollama.com) and pull at least one candidate:
   `ollama pull llama3.1:8b`
2. Make Ollama listen beyond localhost — **only on a trusted LAN or
   tailnet**:
   - macOS/Linux: `OLLAMA_HOST=0.0.0.0 ollama serve`
   - Windows: set the user environment variable `OLLAMA_HOST=0.0.0.0`
     (Settings → Environment Variables), then restart Ollama from the tray.
   - Allow inbound TCP 11434 in the OS firewall if prompted.
3. Find the machine's address: LAN IP (`ipconfig` / `ifconfig`) or — the
   easy answer across different networks — install Tailscale on both
   machines and use the worker's `100.x.y.z` tailscale IP.

On the hub (wherever the dashboard runs):

4. Put that address in [`llm_lab.yaml`](llm_lab.yaml) under the device's
   `base_url` (the `bro-pc` entry is a placeholder), plus the real
   GPU/RAM in `hardware`. Commit it so both of us have it.
5. Verify from the hub before blaming the code:
   `curl http://<worker-ip>:11434/api/tags` — you should get JSON, not a
   timeout.
6. Refresh the LLM Lab tab: the device card turns green with its installed
   models. To let the worker's owner drive the dashboard from their own
   browser, start the hub with `HAZOP_HOST=0.0.0.0 hazop-web` and have
   them open `http://<hub-ip>:8780`.

Security note: `OLLAMA_HOST=0.0.0.0` exposes an unauthenticated inference
API and `HAZOP_HOST=0.0.0.0` an unauthenticated dashboard. Keep both to a
trusted LAN or a Tailscale network; don't port-forward either to the
internet.
