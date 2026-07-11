"""
test_scorecard.py — Smoke test for the one-pass Section 4.3 scorecard.

The deterministic StubLLM configuration must pass every automatable gate
(exit code 0) and write the MDL-11 audit sheets when asked.
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from hazop.l3_reasoner.mdl_scorecard import main


class TestScorecard(unittest.TestCase):
    def test_stub_run_passes_all_automatable_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["--audit-dir", tmp, "--trials", "5"])
            self.assertEqual(code, 0, out.getvalue())

            text = out.getvalue()
            for gate in ("MDL-7", "MDL-9", "MDL-10", "MDL-11", "MDL-12",
                         "MDL-13"):
                self.assertIn(gate, text)
            self.assertIn("HUMAN AUDIT", text)
            self.assertNotIn("FAIL", text)

            self.assertTrue((Path(tmp) / "mdl11_audit_sheet.csv").exists())
            self.assertTrue((Path(tmp) / "mdl11_audit_sheet.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
