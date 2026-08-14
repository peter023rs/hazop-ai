"""DiagEx LLM-vision fallback: graph conversion + pipeline routing.

No diagex install and no API key needed: convert_graph() operates on plain
dicts (the shape of ReconciledGraph.model_dump()), and the routing test
monkeypatches diagex_fallback._extract/availability.
"""
import json
import os
import tempfile
import unittest

import fitz

from hazop.s1_dim import diagex_fallback as DF
from hazop.s1_dim import pipeline


def synthetic_graph():
    """One page, 1000x800 px frame (page is 500x400 pt -> scale 0.5):

      opc "CWS outlet" (direction=out) <- check valve V-1 <- tank T-101
      plus an instrument FI-101 hung off the valve with a signal line,
      and a stray text node with a line into it (drop + dead-end case).

    Process polylines run tank -> valve -> opc; the OPC's direction=out
    ("至" seed) must orient BOTH process runs via orient_runs propagation
    through the 2-degree valve node.
    """
    bbox = lambda x, y, w, h: {"x": x, "y": y, "w": w, "h": h}
    node = lambda id, kind, label, bb, attrs=None, quote=None: {
        "id": id, "kind": kind, "label": label, "bbox_global": bb,
        "page_index": 0, "attributes": attrs or {}, "confidence": "high",
        "source_quote": quote, "alternate_readings": [],
        "source_annotation_ids": []}
    edge = lambda id, a, b, pts, lt="process", attrs=None: {
        "id": id, "from_node": a, "to_node": b, "line_type": lt,
        "polyline_global": pts, "cross_sheet": False, "confidence": "high",
        "source_annotation_ids": [], "attributes": attrs or {}}
    return {
        "schema_version": "0.1.0",
        "source_path": "test",
        "nodes": [
            node("n-tank", "equipment", "T-101", bbox(100, 300, 200, 200),
                 {"equipment_class": "tank"}),
            node("n-valve", "equipment", "V-1", bbox(480, 380, 40, 40),
                 {"valve_type": "check"}),
            node("n-psv", "equipment", "PSV-1", bbox(480, 100, 40, 40),
                 {"valve_type": "safety_relief"}),
            node("n-opc", "opc", "CWS outlet", bbox(800, 380, 60, 40),
                 {"direction": "out", "service": "cws"},
                 quote="至冷却水系统"),
            node("n-fi", "instrument", "FI-101", bbox(460, 500, 60, 60)),
            node("n-txt", "text", "NOTES", bbox(0, 0, 50, 20)),
        ],
        "edges": [
            edge("e1", "n-tank", "n-valve",
                 [[300, 400], [480, 400]],
                 attrs={"line_id": "50-CW-0001-A1B"}),
            edge("e2", "n-valve", "n-opc", [[520, 400], [800, 400]]),
            edge("e3", "n-valve", "n-fi", [[500, 420], [500, 500]],
                 lt="signal_electric"),
            edge("e4", "n-txt", "n-valve", [[40, 20], [480, 390]],
                 lt="other"),
        ],
        "dangling_opcs": [], "conflicts": [], "per_page_status": {"0": "ok"},
    }


PAGES_META = [{"page_index": 0, "width": 1000, "height": 800, "dpi": 150,
               "effective_dpi": 150, "is_scanned": True, "rotation_deg": 0,
               "source_ref": "p0"}]


class ConvertGraphTest(unittest.TestCase):
    def setUp(self):
        self.topos, self.report = DF.convert_graph(
            synthetic_graph(), PAGES_META,
            page_map={0: 3}, page_sizes_pt={3: (500.0, 400.0)})
        self.topo = self.topos[3]
        self.nodes = {n["id"]: n for n in self.topo["nodes"]}

    def kind(self, k):
        return [n for n in self.topo["nodes"] if n["kind"] == k]

    def test_page_mapping_and_engine(self):
        self.assertEqual(list(self.topos), [3])
        self.assertEqual(self.topo["page"], 3)
        self.assertEqual(self.topo["engine"], "diagex")

    def test_node_kinds(self):
        self.assertEqual(len(self.kind("equipment")), 1)
        self.assertEqual(len(self.kind("valve")), 2)
        self.assertEqual(len(self.kind("instrument")), 1)
        self.assertEqual(len(self.kind("off-page")), 1)
        self.assertEqual(len(self.kind("nozzle")), 1)   # synthesized on tank
        self.assertEqual(self.report["nodes_dropped"], 1)   # the text node

    def test_coordinates_scaled_to_points(self):
        tank = self.kind("equipment")[0]
        self.assertEqual(tank["xy"], [100.0, 200.0])    # (200,400)px * 0.5
        self.assertEqual(tank["bbox"], [50.0, 150.0, 150.0, 250.0])

    def test_valve_subclasses(self):
        by_tag = {v.get("tag"): v for v in self.kind("valve")}
        self.assertEqual(by_tag["V-1"]["subclass"], "check")
        self.assertEqual(by_tag["V-1"]["valve_type"], "check")
        self.assertEqual(by_tag["PSV-1"]["subclass"], "safety")

    def test_nozzle_and_internal_edge(self):
        nz = self.kind("nozzle")[0]
        tank = self.kind("equipment")[0]
        self.assertEqual(nz["equipment"], tank["id"])
        self.assertEqual(nz["xy"], [150.0, 200.0])      # polyline end * 0.5
        internal = [e for e in self.topo["edges"] if e["style"] == "internal"]
        self.assertEqual(len(internal), 1)
        self.assertEqual({internal[0]["a"], internal[0]["b"]},
                         {nz["id"], tank["id"]})

    def test_line_number_attached(self):
        runs = [e for e in self.topo["edges"] if e.get("line_numbers")]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["line_numbers"], ["50-CW-0001-A1B"])

    def test_signal_line_is_dashed(self):
        fi = self.kind("instrument")[0]
        sig = [e for e in self.topo["edges"]
               if fi["id"] in (e["a"], e["b"]) and e["style"] == "dashed"]
        self.assertEqual(len(sig), 1)

    def test_dropped_text_node_becomes_dead_end(self):
        dead = self.kind("dead-end")
        self.assertEqual(len(dead), 1)
        self.assertEqual(self.report["edges_dangling"], 1)

    def test_flow_direction_from_opc_seed(self):
        """direction=out -> 至 seed at the OPC, propagated through the
        2-degree check valve: both process runs directed, zero conflicts."""
        stats = self.topo["direction_stats"]
        self.assertEqual(stats["conflicts"], 0)
        directed = [e for e in self.topo["edges"] if e.get("direction")]
        self.assertEqual(len(directed), 2)
        opc = self.kind("off-page")[0]
        valve = next(v for v in self.kind("valve") if v.get("tag") == "V-1")
        for e in directed:
            # flow must run toward the OPC end of each run
            downstream = e["b"] if e["direction"] == "ab" else e["a"]
            if opc["id"] in (e["a"], e["b"]):
                self.assertEqual(downstream, opc["id"])
                self.assertEqual(e["direction_source"], "connector")
            else:
                self.assertEqual(downstream, valve["id"])
                self.assertEqual(e["direction_source"], "propagated")

    def test_confidence_preserved(self):
        self.assertTrue(all(n.get("confidence") == "high"
                            for n in self.topo["nodes"]
                            if n["kind"] not in ("nozzle", "dead-end")))


class PipelineRoutingTest(unittest.TestCase):
    """A raster-only PDF must route to the fallback, produce topology +
    viewer artefacts, and report engine=diagex."""

    def _blank_scan_pdf(self, path):
        doc = fitz.open()
        page = doc.new_page(width=500, height=400)     # no vectors, no text
        pm = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 50, 40))
        pm.clear_with(255)
        page.insert_image(page.rect, pixmap=pm)
        doc.save(path)
        doc.close()

    def test_unsupported_routes_to_diagex(self):
        with tempfile.TemporaryDirectory() as td:
            pdf = os.path.join(td, "scan.pdf")
            self._blank_scan_pdf(pdf)
            run_dir = os.path.join(td, "run")

            orig_avail, orig_extract = DF.availability, DF._extract
            DF.availability = lambda: (True, "")
            DF._extract = lambda diagram, effort: (
                synthetic_graph(), PAGES_META, {"model": "stub",
                                                "effort": effort,
                                                "run_dir": None,
                                                "dexpi_json": None,
                                                "stats": {}, "cost": {}})
            try:
                res = pipeline.run(pdf, run_dir, "all")
            finally:
                DF.availability, DF._extract = orig_avail, orig_extract

            self.assertEqual(res["engine"], "diagex")
            self.assertEqual(res["compatibility"], "unsupported")
            self.assertEqual(res["valves"], 2)
            self.assertEqual(res["instruments"], 1)
            out = os.path.join(run_dir, "output")
            # page_map {0: 1} on a 1-page doc -> topology_page1.json
            topo = json.load(open(os.path.join(out, "topology_page1.json")))
            self.assertEqual(topo["engine"], "diagex")
            self.assertTrue(topo["nodes"])
            self.assertTrue(os.path.exists(
                os.path.join(out, "diagex_report.json")))
            self.assertTrue(os.path.exists(
                os.path.join(out, "viewer.html")))

    def test_unavailable_fallback_still_refuses(self):
        with tempfile.TemporaryDirectory() as td:
            pdf = os.path.join(td, "scan.pdf")
            self._blank_scan_pdf(pdf)
            orig_avail = DF.availability
            DF.availability = lambda: (False, "diagex not installed")
            try:
                with self.assertRaises(ValueError) as ctx:
                    pipeline.run(pdf, os.path.join(td, "run"), "all")
            finally:
                DF.availability = orig_avail
            self.assertIn("fallback unavailable", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
