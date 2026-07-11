"""
test_web_telemetry.py — Tests for the /api/telemetry endpoints (MDL-14).

Uses the Flask test client; the telemetry path is pointed at a temp file so
tests never touch the repo data dir. The endpoints must not require the
heavy dashboard state (plant graph / KB), so recording works even where the
L1 output is absent.
"""

import tempfile
import unittest
from pathlib import Path

import hazop.web.app as webapp


def _payload(**kw):
    p = dict(
        study_id="STUDY-1", node_id="NODE-1",
        deviation_label="More Pressure", kind="cause",
        suggestion_text="Blocked outlet at V-201.",
        action="accepted", user="scribe1",
    )
    p.update(kw)
    return p


class TestTelemetryEndpoints(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_path = webapp.TELEMETRY_PATH
        webapp.TELEMETRY_PATH = Path(self._tmp.name) / "events.jsonl"
        self.client = webapp.app.test_client()

    def tearDown(self):
        webapp.TELEMETRY_PATH = self._old_path
        self._tmp.cleanup()

    def test_post_records_and_get_summarizes(self):
        r = self.client.post("/api/telemetry", json=_payload())
        self.assertEqual(r.status_code, 201)
        self.assertTrue(r.get_json()["event_id"])

        self.client.post("/api/telemetry",
                         json=_payload(action="edited",
                                       edited_text="Blocked outlet at "
                                                   "V-201 (LIC-201 failure)."))
        summary = self.client.get("/api/telemetry").get_json()
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["by_action"],
                         {"accepted": 1, "edited": 1})

    def test_schema_violation_is_400_and_not_recorded(self):
        r = self.client.post("/api/telemetry",
                             json=_payload(action="edited"))  # no edited_text
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/telemetry",
                             json=_payload(bogus_field="x"))
        self.assertEqual(r.status_code, 400)
        summary = self.client.get("/api/telemetry").get_json()
        self.assertEqual(summary["total"], 0)

    def test_empty_body_is_400(self):
        r = self.client.post("/api/telemetry", data="not json",
                             content_type="text/plain")
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
