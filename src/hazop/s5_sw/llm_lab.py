"""
llm_lab.py — multi-device local-LLM benchmarking (the dashboard's LLM Lab).

Purpose: decide which self-hosted model replaces the cloud generator
(DDR-06 on-prem branch) by measuring, per device x model:

  capability       can this device run this model at a usable speed, and
                   does the model hold our JSON-schema output contract
  gold_eval        the Fable §4 gates against the pump/vessel gold set —
                   deviation coverage, cause recall, hallucination rate,
                   grounding precision, per-deviation latency (MDL-12)
  graph_accuracy   NL -> typed-Intent translation scored against the plant
                   graph: a fixed question suite with known answers,
                   executed through s2_pml.query.GraphQuery

Architecture: hub + workers. This module runs inside the dashboard (the
hub); a worker is nothing but an Ollama server reachable over the LAN /
tailnet (OLLAMA_HOST=0.0.0.0). The device/model matrix is declared in
data/llm_lab/llm_lab.yaml — the single source of truth, versioned in git;
the UI renders it and annotates live state but never edits it.

Every run (including failures — an OOM answers "can this device run this
model") is appended to data/llm_lab/runs.jsonl with the resolved config,
so results stay comparable across devices and days.

All HTTP goes through injectable transports; tests never need a server.
CLI: python -m hazop.s5_sw.llm_lab --device peter-mbp --model llama3.1:8b
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

BENCHMARKS = ("capability", "gold_eval", "graph_accuracy")


class LabError(RuntimeError):
    """User-facing lab failure (bad config, unknown device/model)."""


# --------------------------------------------------------------------------
# YAML config (data/llm_lab/llm_lab.yaml)
# --------------------------------------------------------------------------

@dataclass
class Device:
    name: str
    base_url: str
    hardware: str = ""


@dataclass
class LabModel:
    name: str
    role: str = "candidate"        # "candidate" | "critic"


@dataclass
class LabConfig:
    devices: list[Device]
    models: list[LabModel]
    defaults: dict = field(default_factory=dict)
    overrides: dict = field(default_factory=dict)

    def device(self, name: str) -> Device:
        for d in self.devices:
            if d.name == name:
                return d
        raise LabError(f"unknown device '{name}' — declared: "
                       f"{', '.join(d.name for d in self.devices)}")

    def candidates(self, device_name: str) -> list[LabModel]:
        excluded = set(self.overrides.get(device_name, {})
                       .get("exclude", []))
        return [m for m in self.models if m.name not in excluded]

    def critic_model(self) -> str | None:
        for m in self.models:
            if m.role == "critic":
                return m.name
        return None


_config_cache: dict[str, tuple[float, LabConfig]] = {}


def load_config(path: str | Path) -> LabConfig:
    """Parse the lab YAML; cached on mtime so the hub picks up edits
    without a restart."""
    import yaml
    path = Path(path)
    if not path.exists():
        raise LabError(f"lab config not found: {path}")
    mtime = path.stat().st_mtime
    cached = _config_cache.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    devices = [Device(**d) for d in raw.get("devices", [])]
    models = [LabModel(name=m) if isinstance(m, str) else LabModel(**m)
              for m in raw.get("models", [])]
    if not devices:
        raise LabError(f"{path}: no devices declared")
    if not models:
        raise LabError(f"{path}: no models declared")
    config = LabConfig(devices=devices, models=models,
                       defaults=raw.get("defaults", {}) or {},
                       overrides=raw.get("overrides", {}) or {})
    _config_cache[str(path)] = (mtime, config)
    return config


# --------------------------------------------------------------------------
# Ollama HTTP (stdlib; `transport` injectable for tests)
# --------------------------------------------------------------------------

def _http_json(url: str, payload: dict | None = None,
               timeout: float = 5.0) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None \
        else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_installed(base_url: str, transport=None) -> list[dict]:
    """Models installed on a worker (Ollama GET /api/tags), trimmed to what
    the UI shows. Short timeout: a dead worker must not stall the page."""
    transport = transport or (lambda url: _http_json(url, timeout=2.0))
    data = transport(f"{base_url.rstrip('/')}/api/tags")
    out = []
    for m in data.get("models", []):
        details = m.get("details", {})
        out.append({"name": m.get("name", "?"),
                    "size_gb": round(m.get("size", 0) / 1e9, 2),
                    "parameter_size": details.get("parameter_size", ""),
                    "quantization": details.get("quantization_level", "")})
    return out


def device_status(device: Device, transport=None) -> dict:
    """Live view of one worker; unreachable is a state, not an exception."""
    try:
        installed = list_installed(device.base_url, transport=transport)
        return {"reachable": True, "installed": installed, "error": None}
    except Exception as err:
        return {"reachable": False, "installed": [], "error": str(err)}


def pull_model(base_url: str, model: str, progress=None,
               transport=None) -> dict:
    """Pull a model onto a worker (Ollama POST /api/pull, streamed NDJSON).
    `progress(text)` gets human-readable updates; returns the final line."""
    if transport is not None:                      # test seam: whole result
        return transport(f"{base_url.rstrip('/')}/api/pull",
                         {"model": model})
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/pull",
        data=json.dumps({"model": model}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    last: dict = {}
    with urllib.request.urlopen(req, timeout=3600) as resp:
        for line in resp:
            last = json.loads(line.decode("utf-8"))
            if progress:
                status = last.get("status", "")
                total, done = last.get("total"), last.get("completed")
                if total and done:
                    status += f" {100 * done / total:.0f}%"
                progress(status)
            if last.get("error"):
                raise LabError(f"pull failed: {last['error']}")
    return last


# --------------------------------------------------------------------------
# benchmark 1 — capability probe
# --------------------------------------------------------------------------

_PROBE_PROMPT = ("List three generic causes of high pressure in a process "
                 "vessel. Answer in two short sentences.")
_PROBE_SCHEMA = {
    "type": "object",
    "properties": {"causes": {"type": "array",
                              "items": {"type": "string"}}},
    "required": ["causes"], "additionalProperties": False,
}


def run_capability(device: Device, model: str, transport=None,
                   progress=None) -> dict:
    """Load + generation speed via Ollama's native /api/generate timings,
    then a structured-output probe through the OpenAI-compatible path —
    the reasoner is unusable on models that can't hold a JSON schema."""
    transport = transport or (lambda url, payload:
                              _http_json(url, payload, timeout=600))
    if progress:
        progress("generation probe (includes model load) …")
    start = time.perf_counter()
    gen = transport(f"{device.base_url.rstrip('/')}/api/generate",
                    {"model": model, "prompt": _PROBE_PROMPT,
                     "stream": False})
    wall_s = time.perf_counter() - start
    if gen.get("error"):
        raise LabError(gen["error"])
    metrics = {
        "wall_s": round(wall_s, 2),
        "load_s": round(gen.get("load_duration", 0) / 1e9, 2),
        "prompt_tokens": gen.get("prompt_eval_count", 0),
        "output_tokens": gen.get("eval_count", 0),
        "tokens_per_s": round(
            gen["eval_count"] / (gen["eval_duration"] / 1e9), 1)
        if gen.get("eval_duration") and gen.get("eval_count") else None,
    }
    if progress:
        progress("structured-output probe …")
    from hazop.s3_are.reasoner.llm import OpenAICompatClient
    client = OpenAICompatClient(base_url=f"{device.base_url}/v1",
                                model=model)
    try:
        data = client.chat_json(
            "You return only JSON matching the schema.", _PROBE_PROMPT,
            _PROBE_SCHEMA, "probe", max_tokens=400)
        metrics["json_schema_ok"] = isinstance(data.get("causes"), list)
    except Exception as err:
        metrics["json_schema_ok"] = False
        metrics["json_schema_error"] = str(err)[:200]
    return metrics


# --------------------------------------------------------------------------
# benchmark 2 — HAZOP gold-set eval (Fable §4 gates, real model)
# --------------------------------------------------------------------------

def run_gold_eval(device: Device, model: str, critic_model: str | None = None,
                  llm=None, critic=None, progress=None) -> dict:
    """The same gates as /api/scorecard but with a real local generator:
    per-deviation latency (MDL-12) wraps the run, then gold-set metrics
    (MDL-7/9), grounding audit (MDL-10). `llm`/`critic` injectable."""
    from hazop.mdl import PUMP_VESSEL_GOLD, evaluate_node, load_gold
    from hazop.mdl.grounding import audit_grounding
    from hazop.mdl.latency import measure_latency
    from hazop.s3_are.mock_data.pump_vessel import (build_study_node,
                                                    build_topology)
    from hazop.s3_are.reasoner.core import AIReasoner
    from hazop.s3_are.reasoner.evidence_critic import LexicalEvidenceCritic
    from hazop.s3_are.reasoner.llm import LocalLLM
    from hazop.s3_are.reasoner.mock_retriever import MockRetriever

    if llm is None:
        llm = LocalLLM(base_url=f"{device.base_url}/v1", model=model)
    if critic is None and critic_model:
        from hazop.s3_are.reasoner.evidence_critic import LocalEvidenceCritic
        critic = LocalEvidenceCritic(base_url=f"{device.base_url}/v1",
                                     model=critic_model)
    topology, node = build_topology(), build_study_node()
    reasoner = AIReasoner(topology=topology, retriever=MockRetriever(),
                          llm=llm, grounding_required=True,
                          evidence_critic=critic or LexicalEvidenceCritic())
    if progress:
        progress("running the full deviation matrix (29 deviations, "
                 "serial for MDL-12 timing) …")
    rows, latency = measure_latency(reasoner, node)
    gold = evaluate_node(rows, load_gold(PUMP_VESSEL_GOLD))
    grounding = audit_grounding(rows, topology)
    return {
        "deviation_coverage": round(gold.deviation_coverage, 3),
        "cause_recall": round(gold.causes.recall, 3),
        "consequence_recall": round(gold.consequences.recall, 3),
        "safeguard_recall": round(gold.safeguards.recall, 3),
        "hallucination_rate": round(gold.hallucination_rate, 3),
        "grounding_precision": round(grounding.precision, 3),
        "latency_p50_s": round(latency.p50, 2),
        "latency_p95_s": round(latency.p95, 2),
        "latency_worst_s": round(latency.worst[1], 2),
        "gold_passed": gold.passed,
        "retriever": "mock (isolates model quality from KB coverage)",
        "critic": critic_model or "lexical",
    }


# --------------------------------------------------------------------------
# benchmark 3 — graph-retrieval accuracy (NL -> Intent vs the plant graph)
# --------------------------------------------------------------------------

def question_suite(graph: dict) -> list[dict]:
    """~30 questions with known answers, built from tags that exist in this
    graph. Half parse under the rule grammar (ground truth by construction);
    half are paraphrases the grammar can't read — exactly the cases an LLM
    translator must earn its keep on."""
    from hazop.s2_pml.query import Intent, parse_question

    def first_tags(etype: str, n: int) -> list[str]:
        return [x["tag"] for x in graph["nodes"]
                if x["equipment_type"] == etype][:n]

    compressors = first_tags("compressor", 2) or first_tags("pump", 2)
    vessels = first_tags("vessel", 3) or first_tags("tank", 3)
    if not compressors or not vessels:
        raise LabError("graph has no compressor/pump or vessel to build "
                       "the question suite from")
    k1 = compressors[0]
    v1, v2 = vessels[0], vessels[-1]

    suite: list[dict] = []

    def grammar(question: str):
        intent = parse_question(question, graph)
        if intent is None:
            raise LabError(f"suite bug: grammar can't parse '{question}'")
        suite.append({"question": question, "expected": intent,
                      "grammar_parseable": True})

    def paraphrase(question: str, expected):
        suite.append({"question": question, "expected": expected,
                      "grammar_parseable": False})

    # -- grammar-parseable half --------------------------------------
    grammar(f"What is downstream of {k1}?")
    grammar(f"Which vessels are downstream of {k1}?")
    grammar(f"What is upstream of {v1}?")
    grammar(f"What feeds {v1}?")
    grammar(f"What is connected to {v1}?")
    grammar(f"Show the path from {k1} to {v1}")
    grammar("List all relief valves")
    grammar("List all compressors")
    grammar("Show every check valve")
    grammar("How many of each equipment type?")
    grammar("How many vessels are there?")
    grammar(f"Does {v1} have a relief path?")
    grammar(f"Is {v2} protected against overpressure?")
    grammar(f"Tell me about {k1}")
    grammar(f"What is downstream of {v2}?")

    # -- paraphrases (hand-authored expected intents) ------------------
    paraphrase(f"Trace the flow leaving {k1}",
               Intent("downstream", tag=k1))
    paraphrase(f"Where could material from {k1} eventually end up?",
               Intent("downstream", tag=k1))
    paraphrase(f"Everything that can flow into {v1}, please",
               Intent("upstream", tag=v1))
    paraphrase(f"Which equipment sits immediately around {v1}?",
               Intent("neighbours", tag=v1))
    paraphrase(f"How does material get from {k1} to {v1} through the "
               f"process?", Intent("path", source=k1, target=v1))
    paraphrase("Show me every PSV on the unit",
               Intent("list", equipment_type="relief_valve"))
    paraphrase("Which pressure safety valves does this plant have?",
               Intent("list", equipment_type="relief_valve"))
    paraphrase("Give me a tally of the equipment by kind",
               Intent("count"))
    paraphrase(f"If {v1} is blocked in, can it still relieve pressure?",
               Intent("relief", tag=v1))
    paraphrase(f"Is there any overpressure protection downstream of {v2}?",
               Intent("relief", tag=v2))
    paraphrase(f"What kind of item is {k1}?", Intent("info", tag=k1))
    paraphrase(f"Describe the equipment {v2} for me",
               Intent("info", tag=v2))
    paraphrase(f"Which vessels would see flow originating at {k1}?",
               Intent("downstream", tag=k1, equipment_type="vessel"))
    paraphrase("What non-return valves are installed?",
               Intent("list", equipment_type="check_valve"))
    paraphrase(f"Immediate neighbours of {v2}?",
               Intent("neighbours", tag=v2))
    return suite


def run_graph_accuracy(graph_query, translator, suite=None,
                       progress=None) -> dict:
    """Score a translator against the suite. Three tiers per question:
    kind match, exact intent match (after re-grounding), and result match
    (both intents executed; same node set = same answer). A hallucinated
    tag raises QueryError inside reground -> counted as fail-closed, which
    is the designed behavior, not a crash."""
    from hazop.s2_pml.query import QueryError

    suite = suite or question_suite(graph_query.graph)
    kind_ok = intent_ok = result_ok = failed_closed = 0
    failures = []
    for i, item in enumerate(suite):
        if progress:
            progress(f"question {i + 1}/{len(suite)}")
        expected = item["expected"]
        outcome = "ok"
        got = None
        try:
            got = translator.translate(item["question"])
            if got is None:
                outcome = "no translation"
            else:
                got = graph_query.reground(got)
        except QueryError as err:
            outcome = f"failed closed: {err.hint[:80]}"
            failed_closed += 1
            got = None
        if got is not None:
            if got.kind == expected.kind:
                kind_ok += 1
            if got == expected:
                intent_ok += 1
            try:
                expected_nodes = {n["tag"] for n in
                                  graph_query.run(expected).nodes}
                got_nodes = {n["tag"] for n in graph_query.run(got).nodes}
                if got.kind == expected.kind and \
                        got_nodes == expected_nodes:
                    result_ok += 1
                    if got != expected:
                        outcome = "result matched, intent differed"
                else:
                    outcome = "wrong result"
            except QueryError as err:
                outcome = f"execution failed: {err.hint[:80]}"
        if got != expected:
            failures.append({
                "question": item["question"],
                "expected": asdict(expected),
                "got": asdict(got) if got is not None else None,
                "outcome": outcome,
                "grammar_parseable": item["grammar_parseable"],
            })
    n = len(suite)
    return {
        "questions": n,
        "kind_accuracy": round(kind_ok / n, 3),
        "intent_accuracy": round(intent_ok / n, 3),
        "result_accuracy": round(result_ok / n, 3),
        "failed_closed": failed_closed,
        "failures": failures,
    }


# --------------------------------------------------------------------------
# run manager — background execution + runs.jsonl persistence
# --------------------------------------------------------------------------

class RunManager:
    """One benchmark/pull run at a time per device, on a background thread
    so Flask stays responsive. Finished runs (success OR failure — an OOM
    is a result) are appended to runs.jsonl."""

    def __init__(self, runs_path: str | Path):
        self.runs_path = Path(runs_path)
        self._active: dict[str, dict] = {}     # run_id -> status
        self._busy: set[str] = set()           # device names
        self._lock = threading.Lock()

    def start(self, kind: str, device: str, model: str, fn,
              meta: dict | None = None) -> str:
        """fn(progress_cb) -> metrics dict; raises on failure."""
        with self._lock:
            if device in self._busy:
                raise LabError(f"device '{device}' already has a run in "
                               f"progress")
            run_id = uuid.uuid4().hex[:12]
            self._busy.add(device)
            self._active[run_id] = {
                "run_id": run_id, "kind": kind, "device": device,
                "model": model, "state": "running", "progress": "starting…",
                "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "meta": meta or {},
            }
        thread = threading.Thread(target=self._run, args=(run_id, fn),
                                  daemon=True)
        thread.start()
        return run_id

    def _run(self, run_id: str, fn):
        status = self._active[run_id]

        def progress(text: str):
            status["progress"] = text

        try:
            status["result"] = fn(progress)
            final_state = "done"
        except Exception as err:
            status["error"] = str(err)[:500]
            final_state = "error"
        status["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:
            self._busy.discard(status["device"])
        # persist BEFORE flipping the visible state: a poller that sees
        # done/error must find the record in runs.jsonl
        self._append({**status, "state": final_state})
        status["state"] = final_state

    def _append(self, status: dict):
        record = {k: v for k, v in status.items() if k != "progress"}
        self.runs_path.parent.mkdir(parents=True, exist_ok=True)
        with self.runs_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def status(self, run_id: str) -> dict | None:
        return self._active.get(run_id)

    def busy_devices(self) -> list[str]:
        with self._lock:
            return sorted(self._busy)


def read_runs(runs_path: str | Path, limit: int = 200) -> list[dict]:
    path = Path(runs_path)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(out))                  # newest first


# --------------------------------------------------------------------------
# benchmark dispatch (shared by the web endpoints and the CLI)
# --------------------------------------------------------------------------

def run_benchmark(benchmark: str, config: LabConfig, device_name: str,
                  model: str, graph_query=None, progress=None) -> dict:
    device = config.device(device_name)
    if benchmark == "capability":
        return run_capability(device, model, progress=progress)
    if benchmark == "gold_eval":
        return run_gold_eval(device, model,
                             critic_model=config.critic_model(),
                             progress=progress)
    if benchmark == "graph_accuracy":
        if graph_query is None:
            raise LabError("graph_accuracy needs the plant graph "
                           "(run via the dashboard or CLI)")
        from hazop.s2_pml.query import LocalTranslator
        types = sorted({n["equipment_type"]
                        for n in graph_query.graph["nodes"]})
        translator = LocalTranslator(types,
                                     base_url=f"{device.base_url}/v1",
                                     model=model)
        return run_graph_accuracy(graph_query, translator,
                                  progress=progress)
    raise LabError(f"unknown benchmark '{benchmark}' — one of "
                   f"{', '.join(BENCHMARKS)}")


def main() -> int:
    """Headless runner — same YAML, same benchmarks, no browser."""
    import os

    parser = argparse.ArgumentParser(description=__doc__)
    here = Path(__file__).resolve()
    data_dir = Path(os.environ.get("HAZOP_DATA",
                                   here.parents[3] / "data"))
    parser.add_argument("--config", default=data_dir / "llm_lab/llm_lab.yaml")
    parser.add_argument("--device", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--benchmarks", default="capability",
                        help=f"comma-separated: {','.join(BENCHMARKS)}")
    parser.add_argument("--runs-path",
                        default=data_dir / "llm_lab/runs.jsonl")
    args = parser.parse_args()

    config = load_config(args.config)
    graph_query = None
    wanted = [b.strip() for b in args.benchmarks.split(",") if b.strip()]
    if "graph_accuracy" in wanted:
        from hazop.s2_pml import (GraphQuery, build_equipment_graph,
                                  load_plant_model)
        graph_query = GraphQuery(build_equipment_graph(load_plant_model(
            data_dir / "l1_output" / "plant_model_dexpi.json")))

    manager = RunManager(args.runs_path)
    for benchmark in wanted:
        print(f"== {benchmark} · {args.device} · {args.model} ==")
        try:
            result = run_benchmark(benchmark, config, args.device,
                                   args.model, graph_query=graph_query,
                                   progress=lambda t: print(f"   {t}"))
        except Exception as err:
            print(f"   FAILED: {err}")
            manager._append({"run_id": uuid.uuid4().hex[:12],
                             "kind": benchmark, "device": args.device,
                             "model": args.model, "state": "error",
                             "error": str(err)[:500],
                             "started": time.strftime("%Y-%m-%dT%H:%M:%S")})
            continue
        for key, value in result.items():
            if key != "failures":
                print(f"   {key}: {value}")
        manager._append({"run_id": uuid.uuid4().hex[:12],
                         "kind": benchmark, "device": args.device,
                         "model": args.model, "state": "done",
                         "result": result,
                         "started": time.strftime("%Y-%m-%dT%H:%M:%S")})
    print(f"appended to {args.runs_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
