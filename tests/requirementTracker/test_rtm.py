"""
test_rtm.py — Tests for the Requirements Traceability Matrix (Fable §9).

Validates the shipped requirements.json (unique ids, legal statuses),
the rollup math, the citation scanner against known docstring citations,
update round-trips on a temp copy, and the web endpoints.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from hazop.requirementTracker.rtm import (RTM_PATH, VALID_STATUSES, load_rtm, rollup,
                       rtm_view, scan_citations, update_requirement)
import hazop.web.app as webapp


class TestShippedFile(unittest.TestCase):
    def test_loads_and_validates(self):
        data = load_rtm()
        ids = [r["id"] for r in data["requirements"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 75)   # full Fable coverage
        for r in data["requirements"]:
            self.assertIn(r["status"], VALID_STATUSES)
            self.assertTrue(r["section"])
            self.assertTrue(r["text"])

    def test_every_fable_family_is_covered(self):
        ids = {r["id"] for r in load_rtm()["requirements"]}
        for family, count in (("FR-DIM", 6), ("FR-PML", 5), ("FR-ARE", 9),
                              ("FR-SW", 6), ("FR-RCM", 5), ("FR-AGM", 3),
                              ("AR", 5), ("MDL", 14), ("DR", 5),
                              ("NFR", 8), ("VV", 5), ("OI", 4)):
            for i in range(1, count + 1):
                self.assertIn(f"{family}-{i}", ids)


class TestRollup(unittest.TestCase):
    def test_weighted_progress(self):
        reqs = [
            {"id": "X-1", "section": "S", "status": "done"},
            {"id": "X-2", "section": "S", "status": "partial"},
            {"id": "X-3", "section": "S", "status": "todo"},
            {"id": "X-4", "section": "S", "status": "out_of_scope"},
        ]
        r = rollup(reqs)
        # out_of_scope excluded: (1 + 0.5 + 0) / 3
        self.assertEqual(r["sections"]["S"]["progress"], 0.5)
        self.assertEqual(r["overall"]["total"], 4)
        self.assertEqual(r["overall"]["progress"], 0.5)


class TestScanner(unittest.TestCase):
    def test_finds_known_docstring_citations(self):
        hits = scan_citations({"FR-ARE-9", "MDL-13", "FR-PML-5"})
        files = {h["file"] for h in hits["FR-ARE-9"]}
        self.assertTrue(any("core.py" in f or "topology.py" in f
                            for f in files), files)
        self.assertTrue(hits["MDL-13"])
        for h in hits["FR-ARE-9"]:
            self.assertGreater(h["line"], 0)

    def test_unknown_ids_cannot_appear(self):
        hits = scan_citations({"FR-ARE-9"})
        self.assertEqual(set(hits), {"FR-ARE-9"})


class TestUpdate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "requirements.json"
        shutil.copy(RTM_PATH, self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_round_trip(self):
        entry = update_requirement("FR-ARE-7", status="partial",
                                   notes="prototype started", path=self.path)
        self.assertEqual(entry["status"], "partial")
        again = load_rtm(self.path)
        row = next(r for r in again["requirements"] if r["id"] == "FR-ARE-7")
        self.assertEqual(row["notes"], "prototype started")

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            update_requirement("FR-ARE-7", status="finished", path=self.path)
        with self.assertRaises(KeyError):
            update_requirement("FR-XX-99", status="done", path=self.path)

    def test_view_merges_citations(self):
        view = rtm_view(self.path)
        are9 = next(r for r in view["requirements"] if r["id"] == "FR-ARE-9")
        self.assertTrue(are9["citations"])
        self.assertIn("overall", view["rollup"])


class TestEndpoints(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "requirements.json"
        shutil.copy(RTM_PATH, self.path)
        self._old = webapp.RTM_PATH
        webapp.RTM_PATH = self.path
        self.client = webapp.app.test_client()

    def tearDown(self):
        webapp.RTM_PATH = self._old
        self._tmp.cleanup()

    def test_get_serves_view(self):
        d = self.client.get("/api/rtm").get_json()
        self.assertGreaterEqual(len(d["requirements"]), 75)
        self.assertIn("overall", d["rollup"])

    def test_post_updates_and_persists(self):
        r = self.client.post("/api/rtm/FR-SW-4",
                             json={"status": "partial", "notes": "wip"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["status"], "partial")
        raw = json.loads(self.path.read_text())
        row = next(x for x in raw["requirements"] if x["id"] == "FR-SW-4")
        self.assertEqual(row["notes"], "wip")

    def test_post_error_codes(self):
        self.assertEqual(self.client.post(
            "/api/rtm/FR-XX-99", json={"status": "done"}).status_code, 404)
        self.assertEqual(self.client.post(
            "/api/rtm/FR-SW-4", json={"status": "finished"}).status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
