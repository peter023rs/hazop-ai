"""
test_telemetry.py — Tests for the MDL-14 / FR-SW-2 telemetry module.

Schema validation, provenance transitions, JSONL persistence across log
instances (the append-only audit property), and summary math on
AI-generated synthetic event streams.
"""

import json
import tempfile
import unittest
from pathlib import Path

from hazop.telemetry import (
    SCHEMA_VERSION, SuggestionAction, SuggestionEvent, TelemetryLog,
)
from hazop.l3_reasoner.reasoner.worksheet import Provenance


def _event(action="accepted", **kw) -> SuggestionEvent:
    defaults = dict(
        study_id="STUDY-1", node_id="NODE-1",
        deviation_label="More Pressure", kind="cause",
        suggestion_text="Blocked outlet at V-201.",
        action=action, user="scribe1",
        confidence=0.85, model_version="claude-opus-4-8",
        prompt_template_version="pt-3", kb_snapshot_version="kb-2026-07",
    )
    defaults.update(kw)
    return SuggestionEvent(**defaults)


class TestEventSchema(unittest.TestCase):
    def test_defaults_are_populated(self):
        e = _event()
        self.assertTrue(e.event_id)
        self.assertIn("T", e.timestamp)          # ISO 8601
        self.assertIn("+00:00", e.timestamp)     # UTC, explicit offset

    def test_round_trip_preserves_all_fields(self):
        e = _event(action="edited", edited_text="Blocked outlet at V-201 "
                                                "due to inadvertent closure.")
        d = e.to_dict()
        self.assertEqual(d["schema_version"], SCHEMA_VERSION)
        self.assertEqual(SuggestionEvent.from_dict(d), e)

    def test_edited_requires_edited_text(self):
        with self.assertRaises(ValueError):
            _event(action="edited")

    def test_user_identity_is_mandatory(self):
        with self.assertRaises(ValueError):
            _event(user="")

    def test_unknown_action_rejected(self):
        with self.assertRaises(ValueError):
            _event(action="maybe")

    def test_provenance_transitions(self):
        self.assertEqual(_event(action="accepted").resulting_provenance,
                         Provenance.AI_GENERATED_HUMAN_APPROVED)
        self.assertEqual(_event(action="edited", edited_text="x")
                         .resulting_provenance,
                         Provenance.AI_GENERATED_HUMAN_MODIFIED)
        self.assertIsNone(_event(action="rejected").resulting_provenance)


class TestLog(unittest.TestCase):
    def test_append_only_jsonl_persists_across_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs" / "telemetry.jsonl"
            TelemetryLog(path).record(_event())        # creates parents
            TelemetryLog(path).record(_event(action="rejected"))

            with open(path) as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 2)
            for line in lines:
                json.loads(line)                       # each line valid JSON

            events = TelemetryLog(path).events()
            self.assertEqual([e.action for e in events],
                             [SuggestionAction.ACCEPTED,
                              SuggestionAction.REJECTED])

    def test_missing_file_is_empty_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = TelemetryLog(Path(tmp) / "nope.jsonl")
            self.assertEqual(log.events(), [])
            self.assertEqual(log.summarize().total, 0)


class TestSummary(unittest.TestCase):
    def test_counts_and_rates(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = TelemetryLog(Path(tmp) / "t.jsonl")
            for _ in range(6):
                log.record(_event(action="accepted"))
            for _ in range(3):
                log.record(_event(action="edited", edited_text="x",
                                  kind="safeguard"))
            log.record(_event(action="rejected"))

            s = log.summarize()
            self.assertEqual(s.total, 10)
            self.assertEqual(s.by_action, {"accepted": 6, "edited": 3,
                                           "rejected": 1})
            self.assertAlmostEqual(s.rate(SuggestionAction.ACCEPTED), 0.6)
            self.assertEqual(s.by_kind["safeguard"], {"edited": 3})
            self.assertIn("accepted: 6 (60.0%)", s.summary())


if __name__ == "__main__":
    unittest.main(verbosity=2)
