"""XLSX historical-HAZOP ingestion: one worksheet row = one chunk (DDR-04),
pending-by-default curation (FR-AGM-2), stdlib xlsx reading."""

import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from hazop.l2_knowledge.kb import (CurationStatus, HybridIndex,
                                   read_xlsx_rows, worksheet_to_document)

_SHEET_XMLNS = ('xmlns="http://schemas.openxmlformats.org/'
                'spreadsheetml/2006/main"')


def _make_xlsx(path: Path, rows: list[list[str]], shared: bool = False):
    """Write a minimal real .xlsx: one worksheet, optional sharedStrings."""
    def ref(r, c):
        return f"{chr(ord('A') + c)}{r + 1}"          # columns A..Z suffice

    strings: list[str] = []
    cells_xml = []
    for r, row in enumerate(rows):
        cs = []
        for c, value in enumerate(row):
            if value == "":
                continue
            if shared:
                strings.append(value)
                cs.append(f'<c r="{ref(r, c)}" t="s">'
                          f'<v>{len(strings) - 1}</v></c>')
            else:
                cs.append(f'<c r="{ref(r, c)}" t="inlineStr">'
                          f'<is><t>{escape(value)}</t></is></c>')
        cells_xml.append(f'<row r="{r + 1}">{"".join(cs)}</row>')
    sheet = (f'<?xml version="1.0"?><worksheet {_SHEET_XMLNS}>'
             f'<sheetData>{"".join(cells_xml)}</sheetData></worksheet>')

    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
        if shared:
            sst = "".join(f"<si><t>{escape(s)}</t></si>" for s in strings)
            zf.writestr("xl/sharedStrings.xml",
                        f'<?xml version="1.0"?><sst {_SHEET_XMLNS}>'
                        f'{sst}</sst>')


_WORKSHEET = [
    ["HAZOP Study — Air Compression Unit", "", "", "", ""],
    ["Node", "Deviation", "Causes", "Consequences", "Safeguards"],
    ["N1", "More Pressure", "Blocked discharge valve",
     "Overpressure of receiver", "PSV-001; high pressure trip"],
    ["N1", "No Flow", "Compressor trip", "Loss of instrument air",
     "Standby machine auto-start"],
    ["N2", "", "", "", ""],                      # empty -> skipped
    ["N2", "Fluctuation", "Control hunting", "Cyclic loading", ""],
]


class TestReadXlsx(unittest.TestCase):
    def _roundtrip(self, shared):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "ws.xlsx"
            _make_xlsx(p, _WORKSHEET, shared=shared)
            return read_xlsx_rows(p)

    def test_inline_strings(self):
        rows = self._roundtrip(shared=False)
        self.assertEqual(rows[1][1], "Deviation")
        self.assertEqual(rows[2][4], "PSV-001; high pressure trip")

    def test_shared_strings(self):
        rows = self._roundtrip(shared=True)
        self.assertEqual(rows[3][:2], ["N1", "No Flow"])


class TestWorksheetToDocument(unittest.TestCase):
    def _ingest(self, worksheet=_WORKSHEET):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "ws.xlsx"
            _make_xlsx(p, worksheet)
            return worksheet_to_document(p, doc_id="HIST-X",
                                         title="Air unit HAZOP 2019")

    def test_one_row_one_chunk(self):
        doc, report = self._ingest()
        self.assertEqual(len(doc.chunks), 3)          # DDR-04
        self.assertEqual(report.chunks, 3)
        first = doc.chunks[0]
        self.assertIn("Deviation: More Pressure", first.text)
        self.assertIn("Causes: Blocked discharge valve", first.text)
        self.assertIn("Safeguards: PSV-001", first.text)

    def test_guideword_parameter_parsed_from_deviation(self):
        doc, _ = self._ingest()
        self.assertEqual(doc.chunks[0].guidewords, ["MORE"])
        self.assertEqual(doc.chunks[0].parameters, ["pressure"])
        self.assertEqual(doc.chunks[1].guidewords, ["NO"])
        self.assertEqual(doc.chunks[1].parameters, ["flow"])

    def test_unparseable_deviation_indexes_untagged_not_guessed(self):
        doc, report = self._ingest()
        fluct = doc.chunks[2]
        self.assertEqual(fluct.guidewords, [])
        self.assertEqual(fluct.parameters, [])
        self.assertEqual(report.untagged, [6])

    def test_empty_rows_reported_not_silent(self):
        _, report = self._ingest()
        self.assertEqual(report.skipped_empty, [5])

    def test_explicit_guideword_parameter_columns_win(self):
        ws = [["Guideword", "Parameter", "Causes", "Consequences"],
              ["Reverse", "Flow", "Pump trip", "Backflow to suction"]]
        doc, _ = self._ingest(ws)
        self.assertEqual(doc.chunks[0].guidewords, ["REVERSE"])
        self.assertEqual(doc.chunks[0].parameters, ["flow"])
        self.assertIn("Deviation: Reverse Flow", doc.chunks[0].text)

    def test_missing_header_fails_loudly(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "ws.xlsx"
            _make_xlsx(p, [["Just", "Some", "Table"], ["a", "b", "c"]])
            with self.assertRaises(ValueError):
                worksheet_to_document(p, doc_id="X")

    def test_pending_by_default_and_curation_gate_holds(self):
        doc, _ = self._ingest()
        self.assertEqual(doc.curation, CurationStatus.PENDING)
        index = HybridIndex()
        report = index.ingest([doc])
        self.assertEqual(report.indexed, [])          # FR-AGM-2: not indexed
        # once a curator approves, the same document indexes
        doc.curation = CurationStatus.APPROVED
        report = index.ingest([doc])
        self.assertEqual(report.indexed, ["HIST-X"])
        hits = index.search("blocked discharge overpressure", k=2,
                            filters={"guideword": "MORE",
                                     "parameter": "pressure"})
        self.assertTrue(hits)
        self.assertTrue(hits[0].source_id.startswith("HIST-X#row-"))


if __name__ == "__main__":
    unittest.main()
