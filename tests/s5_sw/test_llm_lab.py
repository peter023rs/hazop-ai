"""
test_llm_lab.py — LLM Lab core: YAML matrix, Ollama discovery, the three
benchmarks with fake transports/models (offline-first — no server, ever),
and the run manager lifecycle.
"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from hazop.s2_pml.query import GraphQuery, Intent, LocalTranslator
from hazop.s5_sw import llm_lab
from hazop.s5_sw.llm_lab import (LabError, RunManager, device_status,
                                 list_installed, load_config, question_suite,
                                 read_runs, run_capability, run_gold_eval,
                                 run_graph_accuracy)

YAML = """\
defaults:
  temperature: 0.2
  benchmarks: [capability]
devices:
  - name: mac
    base_url: http://mac:11434
    hardware: "M5 Pro 24GB"
  - name: pc
    base_url: http://pc:11434
models:
  - llama3.1:8b
  - name: llama3.2:3b
    role: critic
  - big-model:70b
overrides:
  mac:
    exclude: [big-model:70b]
"""


class TestConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "llm_lab.yaml"
        self.path.write_text(YAML)

    def tearDown(self):
        self._tmp.cleanup()

    def test_parses_devices_models_defaults(self):
        c = load_config(self.path)
        self.assertEqual([d.name for d in c.devices], ["mac", "pc"])
        self.assertEqual(c.device("mac").hardware, "M5 Pro 24GB")
        self.assertEqual(c.defaults["temperature"], 0.2)
        self.assertEqual(c.critic_model(), "llama3.2:3b")

    def test_overrides_prune_candidates_per_device(self):
        c = load_config(self.path)
        self.assertEqual([m.name for m in c.candidates("mac")],
                         ["llama3.1:8b", "llama3.2:3b"])
        self.assertEqual([m.name for m in c.candidates("pc")],
                         ["llama3.1:8b", "llama3.2:3b", "big-model:70b"])

    def test_unknown_device_fails_with_names(self):
        with self.assertRaises(LabError) as ctx:
            load_config(self.path).device("toaster")
        self.assertIn("mac", str(ctx.exception))

    def test_mtime_reload_without_restart(self):
        load_config(self.path)
        self.path.write_text(YAML.replace("M5 Pro 24GB", "M5 Pro 24GB RAM"))
        # ensure mtime actually differs on coarse filesystems
        import os
        os.utime(self.path, (time.time() + 2, time.time() + 2))
        self.assertEqual(load_config(self.path).device("mac").hardware,
                         "M5 Pro 24GB RAM")

    def test_empty_config_rejected(self):
        self.path.write_text("devices: []\nmodels: [x]\n")
        with self.assertRaises(LabError):
            load_config(self.path)


class TestDiscovery(unittest.TestCase):
    TAGS = {"models": [{"name": "llama3.1:8b", "size": 4_700_000_000,
                        "details": {"parameter_size": "8.0B",
                                    "quantization_level": "Q4_K_M"}}]}

    def test_list_installed_trims_fields(self):
        out = list_installed("http://x:11434", transport=lambda url: self.TAGS)
        self.assertEqual(out, [{"name": "llama3.1:8b", "size_gb": 4.7,
                                "parameter_size": "8.0B",
                                "quantization": "Q4_K_M"}])

    def test_device_status_unreachable_is_state_not_crash(self):
        def transport(url):
            raise OSError("connection refused")
        status = device_status(llm_lab.Device("pc", "http://pc:11434"),
                               transport=transport)
        self.assertFalse(status["reachable"])
        self.assertIn("refused", status["error"])


class TestCapability(unittest.TestCase):
    def test_probe_reports_speed_and_json_contract(self):
        def transport(url, payload):
            self.assertIn("/api/generate", url)
            return {"eval_count": 60, "eval_duration": 3_000_000_000,
                    "load_duration": 1_500_000_000,
                    "prompt_eval_count": 25, "response": "…"}
        metrics = run_capability(
            llm_lab.Device("mac", "http://mac:11434"), "llama3.1:8b",
            transport=transport)
        self.assertEqual(metrics["tokens_per_s"], 20.0)
        self.assertEqual(metrics["load_s"], 1.5)
        # OpenAICompatClient hits a real socket here -> recorded as a
        # failed JSON contract, never an exception
        self.assertFalse(metrics["json_schema_ok"])
        self.assertIn("json_schema_error", metrics)

    def test_model_error_raises(self):
        with self.assertRaises(LabError):
            run_capability(llm_lab.Device("mac", "http://mac:11434"), "nope",
                           transport=lambda u, p: {"error": "model missing"})


class _ScriptedLLM:
    """LLMInterface stand-in delegating to StubLLM (valid, grounded
    output) — run_gold_eval only cares that it walks the pipeline."""

    def __init__(self):
        from hazop.s3_are.reasoner.llm import StubLLM
        self._stub = StubLLM()

    def generate_findings(self, deviation, node_context, evidence):
        return self._stub.generate_findings(deviation, node_context,
                                            evidence)


class TestGoldEval(unittest.TestCase):
    def test_metrics_shape_with_injected_model(self):
        metrics = run_gold_eval(llm_lab.Device("mac", "http://mac:11434"),
                                "fake", llm=_ScriptedLLM())
        for key in ("deviation_coverage", "cause_recall",
                    "hallucination_rate", "grounding_precision",
                    "latency_p50_s", "latency_p95_s", "gold_passed"):
            self.assertIn(key, metrics)
        self.assertEqual(metrics["deviation_coverage"], 1.0)
        self.assertEqual(metrics["critic"], "lexical")


def _node(tag, etype):
    return {"tag": tag, "equipment_type": etype, "name": tag,
            "attributes": {"sheets": [4]}, "detection_confidence": 1.0}


def _edge(source, target, direction="known"):
    return {"source": source, "target": target, "line_tag": "",
            "attributes": {"line_numbers": [], "direction": direction,
                           **({"direction_sources": ["arrow"]}
                              if direction == "known" else {})}}


def _graph():
    return {"nodes": [_node("2401-K-001", "compressor"),
                      _node("2401-V-001", "vessel"),
                      _node("2401-V-002", "vessel"),
                      _node("2401-PSV-001", "relief_valve"),
                      _node("2401-XV-001", "check_valve")],
            "edges": [_edge("2401-K-001", "2401-V-001"),
                      _edge("2401-V-001", "2401-PSV-001"),
                      _edge("2401-V-001", "2401-V-002", "unknown"),
                      _edge("2401-V-002", "2401-XV-001")],
            "stats": {}}


class TestQuestionSuite(unittest.TestCase):
    def test_suite_builds_from_real_tags_and_ground_truth_executes(self):
        gq = GraphQuery(_graph())
        suite = question_suite(gq.graph)
        self.assertGreaterEqual(len(suite), 25)
        kinds = {item["expected"].kind for item in suite}
        self.assertTrue({"downstream", "upstream", "path", "list",
                         "count", "relief", "info",
                         "neighbours"} <= kinds)
        for item in suite:              # every expected intent must run
            gq.run(item["expected"])

    def test_suite_needs_rotating_equipment(self):
        g = {"nodes": [_node("2401-V-001", "vessel")], "edges": [],
             "stats": {}}
        with self.assertRaises(LabError):
            question_suite(g)


class _PerfectTranslator:
    """Answers from the suite's own key — the accuracy ceiling."""

    def __init__(self, suite):
        self._by_q = {i["question"]: i["expected"] for i in suite}

    def translate(self, question):
        return self._by_q[question]


class _BrokenTranslator:
    """Wrong kinds, hallucinated tags, abstentions — the floor."""

    def __init__(self):
        self.calls = 0

    def translate(self, question):
        self.calls += 1
        if self.calls % 3 == 0:
            return None                                   # abstains
        if self.calls % 3 == 1:
            return Intent("downstream", tag="2401-GHOST-99")  # hallucinated
        return Intent("count")                            # wrong kind


class TestGraphAccuracy(unittest.TestCase):
    def test_perfect_translator_scores_one(self):
        gq = GraphQuery(_graph())
        suite = question_suite(gq.graph)
        report = run_graph_accuracy(gq, _PerfectTranslator(suite),
                                    suite=suite)
        self.assertEqual(report["kind_accuracy"], 1.0)
        self.assertEqual(report["intent_accuracy"], 1.0)
        self.assertEqual(report["result_accuracy"], 1.0)
        self.assertEqual(report["failures"], [])

    def test_hallucinated_tags_fail_closed_and_score_zero(self):
        gq = GraphQuery(_graph())
        suite = question_suite(gq.graph)
        report = run_graph_accuracy(gq, _BrokenTranslator(), suite=suite)
        # near-floor: the wrong-kind branch can coincide with the one
        # bare-count question in the suite, nothing more
        self.assertLess(report["intent_accuracy"], 0.1)
        self.assertGreater(report["failed_closed"], 0)
        self.assertGreaterEqual(len(report["failures"]),
                                report["questions"] - 1)
        outcomes = {f["outcome"] for f in report["failures"]}
        self.assertTrue(any(o.startswith("failed closed") for o in outcomes))


class TestLocalTranslator(unittest.TestCase):
    def test_valid_payload_becomes_intent(self):
        class FakeClient:
            def chat_json(self, system, user, schema, name, max_tokens):
                return {"kind": "downstream", "tag": "2401-K-001",
                        "source": None, "target": None,
                        "equipment_type": None}
        t = LocalTranslator(["vessel"], client=FakeClient())
        intent = t.translate("trace flow from K-001")
        self.assertEqual((intent.kind, intent.tag),
                         ("downstream", "2401-K-001"))

    def test_invalid_json_abstains(self):
        class FakeClient:
            def chat_json(self, *a, **k):
                raise RuntimeError("invalid JSON twice")
        self.assertIsNone(LocalTranslator(["vessel"],
                                          client=FakeClient())
                          .translate("hello"))


class TestRunManager(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.runs = Path(self._tmp.name) / "runs.jsonl"
        self.manager = RunManager(self.runs)

    def tearDown(self):
        self._tmp.cleanup()

    def _wait(self, run_id, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.manager.status(run_id)
            if status["state"] != "running":
                return status
            time.sleep(0.01)
        self.fail("run never finished")

    def test_success_persists_result(self):
        run_id = self.manager.start(
            "benchmark", "mac", "m", lambda progress: {"ok": 1})
        status = self._wait(run_id)
        self.assertEqual(status["state"], "done")
        persisted = read_runs(self.runs)
        self.assertEqual(persisted[0]["result"], {"ok": 1})

    def test_failure_is_a_result_not_a_crash(self):
        def job(progress):
            raise MemoryError("model does not fit in 24GB")
        status = self._wait(self.manager.start("benchmark", "mac", "70b",
                                               job))
        self.assertEqual(status["state"], "error")
        self.assertIn("24GB", status["error"])
        self.assertEqual(read_runs(self.runs)[0]["state"], "error")

    def test_one_run_per_device_and_release_after_finish(self):
        import threading
        gate = threading.Event()
        run_id = self.manager.start("benchmark", "mac", "m",
                                    lambda p: gate.wait(3) or {})
        with self.assertRaises(LabError):
            self.manager.start("benchmark", "mac", "m2", lambda p: {})
        other = self.manager.start("benchmark", "pc", "m",
                                   lambda p: {})    # other device is fine
        gate.set()
        self._wait(run_id)
        third = self.manager.start("benchmark", "mac", "m3",
                                   lambda p: {})    # released after finish
        self._wait(other)          # let every thread finish before the
        self._wait(third)          # tempdir disappears

    def test_read_runs_newest_first_and_skips_junk(self):
        self.runs.write_text('{"run_id":"a"}\nnot json\n{"run_id":"b"}\n')
        self.assertEqual([r["run_id"] for r in read_runs(self.runs)],
                         ["b", "a"])


class TestLabAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import hazop.s5_sw.app as webapp
        cls.webapp = webapp
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        (tmp / "llm_lab.yaml").write_text(YAML)
        cls._old = (webapp.LAB_CONFIG_PATH, webapp.LAB_RUNS_PATH,
                    webapp.lab_runs)
        webapp.LAB_CONFIG_PATH = tmp / "llm_lab.yaml"
        webapp.LAB_RUNS_PATH = tmp / "runs.jsonl"
        webapp.lab_runs = RunManager(webapp.LAB_RUNS_PATH)
        cls.client = webapp.app.test_client()

    @classmethod
    def tearDownClass(cls):
        (cls.webapp.LAB_CONFIG_PATH, cls.webapp.LAB_RUNS_PATH,
         cls.webapp.lab_runs) = cls._old
        cls._tmp.cleanup()

    def test_config_endpoint_reports_matrix_with_live_state(self):
        r = self.client.get("/api/lab/config")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual([x["name"] for x in d["devices"]], ["mac", "pc"])
        mac = d["devices"][0]
        self.assertFalse(mac["reachable"])       # fake host, short timeout
        self.assertEqual([c["name"] for c in mac["candidates"]],
                         ["llama3.1:8b", "llama3.2:3b"])
        self.assertEqual(d["critic_model"], "llama3.2:3b")

    def test_run_validates_device_and_benchmarks(self):
        r = self.client.post("/api/lab/run", json={
            "device": "toaster", "model": "m", "benchmarks": ["capability"]})
        self.assertEqual(r.status_code, 404)
        r = self.client.post("/api/lab/run", json={
            "device": "mac", "model": "m", "benchmarks": ["bogus"]})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/lab/run", json={})
        self.assertEqual(r.status_code, 400)

    def test_pull_and_status_lifecycle(self):
        r = self.client.post("/api/lab/pull",
                             json={"device": "pc", "model": "tiny"})
        self.assertEqual(r.status_code, 202)
        run_id = r.get_json()["run_id"]
        deadline = time.time() + 5
        while time.time() < deadline:
            s = self.client.get(f"/api/lab/run/{run_id}").get_json()
            if s["state"] != "running":
                break
            time.sleep(0.02)
        self.assertEqual(s["state"], "error")    # fake host -> recorded
        runs = self.client.get("/api/lab/runs").get_json()
        self.assertEqual(runs[0]["kind"], "pull")

    def test_unknown_run_id_404(self):
        self.assertEqual(
            self.client.get("/api/lab/run/nope").status_code, 404)


if __name__ == "__main__":
    unittest.main()
