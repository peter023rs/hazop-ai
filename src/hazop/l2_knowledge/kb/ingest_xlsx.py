"""
ingest_xlsx.py — historical HAZOP worksheet (XLSX) -> KB document.

The real KB build-out is historical studies, and they live in spreadsheet
worksheets. Following DDR-04, ONE WORKSHEET ROW = ONE CHUNK: a retrieval
hit is a single deviation's record (causes/consequences/safeguards), never
a whole study.

Guardrails preserved on the way in:
  * documents enter as `pending` unless the caller says otherwise — the
    curation gate (FR-AGM-2) means a curator approves them before they can
    ever be indexed;
  * guideword/parameter tags come from explicit columns when present, else
    are parsed from the deviation cell ("More Pressure" -> MORE/pressure);
    unparseable deviations get no tags rather than guessed ones — they
    still index on text, just without the structured boost;
  * rows with no content are skipped and reported, not silently dropped.

The reader is a minimal stdlib XLSX parser (an .xlsx is a zip of XML):
shared strings, inline strings and numbers — the shapes worksheet tables
actually use. No new dependency for the offline package.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from .schema import (Applicability, CurationStatus, KBChunk, KBDocument,
                     SourceType)

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


# --------------------------------------------------------------------------
# minimal XLSX reading
# --------------------------------------------------------------------------

def _column_index(cell_ref: str) -> int:
    """'B7' -> 1 (zero-based column)."""
    n = 0
    for ch in cell_ref:
        if not ch.isalpha():
            break
        n = n * 26 + (ord(ch.upper()) - ord("A") + 1)
    return n - 1


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        root = ElementTree.fromstring(zf.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    out = []
    for si in root.findall("m:si", _NS):
        out.append("".join(t.text or "" for t in si.iter(
            "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")))
    return out


def _cell_text(cell, shared: list[str]) -> str:
    ctype = cell.get("t", "n")
    if ctype == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(
            "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"))
    v = cell.find("m:v", _NS)
    if v is None or v.text is None:
        return ""
    if ctype == "s":
        return shared[int(v.text)]
    if ctype == "b":
        return "TRUE" if v.text == "1" else "FALSE"
    text = v.text
    # numbers: strip the float artifacts spreadsheets add to integers
    if re.fullmatch(r"-?\d+\.0+", text):
        text = text.split(".")[0]
    return text


def read_xlsx_rows(path: str | Path, sheet_index: int = 0) -> list[list[str]]:
    """Rows of the given worksheet as lists of strings (gaps -> "")."""
    with zipfile.ZipFile(path) as zf:
        sheets = sorted(
            (n for n in zf.namelist()
             if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
            key=lambda n: int(re.search(r"(\d+)", n).group(1)))
        if not sheets:
            raise ValueError(f"no worksheets found in {path}")
        shared = _shared_strings(zf)
        root = ElementTree.fromstring(zf.read(sheets[sheet_index]))
        rows: list[list[str]] = []
        for row in root.iter(
                "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row"):
            cells: dict[int, str] = {}
            for c in row.findall("m:c", _NS):
                idx = _column_index(c.get("r", ""))
                if idx >= 0:
                    cells[idx] = _cell_text(c, shared)
            width = max(cells) + 1 if cells else 0
            rows.append([cells.get(i, "") for i in range(width)])
        return rows


# --------------------------------------------------------------------------
# worksheet -> KB document
# --------------------------------------------------------------------------

# canonical column -> header spellings seen in real worksheets
_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "node": ("node", "study node", "node no", "node no."),
    "deviation": ("deviation",),
    "guideword": ("guideword", "guide word"),
    "parameter": ("parameter",),
    "causes": ("cause", "causes", "possible causes"),
    "consequences": ("consequence", "consequences"),
    "safeguards": ("safeguard", "safeguards", "existing safeguards",
                   "protection", "protections"),
    "recommendations": ("recommendation", "recommendations", "action",
                        "actions"),
}

# deviation-cell prefix -> stage 3 guideword name (longest match first)
_GUIDEWORD_PREFIXES: tuple[tuple[str, str], ...] = (
    ("as well as", "AS_WELL_AS"),
    ("other than", "OTHER_THAN"),
    ("part of", "PART_OF"),
    ("no/not", "NO"),
    ("reverse", "REVERSE"),
    ("before", "BEFORE"),
    ("early", "EARLY"),
    ("after", "AFTER"),
    ("less", "LESS"),
    ("more", "MORE"),
    ("late", "LATE"),
    ("not", "NO"),
    ("no", "NO"),
)


@dataclass
class XlsxIngestReport:
    rows_read: int = 0
    chunks: int = 0
    skipped_empty: list[int] = field(default_factory=list)   # 1-based rows
    untagged: list[int] = field(default_factory=list)        # no gw/param

    def summary(self) -> str:
        parts = [f"{self.chunks} chunk(s) from {self.rows_read} data row(s)"]
        if self.skipped_empty:
            parts.append(f"skipped empty rows: {self.skipped_empty}")
        if self.untagged:
            parts.append(f"rows without guideword/parameter tags "
                         f"(text-only retrieval): {self.untagged}")
        return "; ".join(parts)


def _map_headers(header_row: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for i, raw in enumerate(header_row):
        name = raw.strip().lower()
        for canonical, aliases in _HEADER_ALIASES.items():
            if name in aliases and canonical not in mapping:
                mapping[canonical] = i
    return mapping


def _split_deviation(label: str) -> tuple[str, str]:
    """'More Pressure' -> ('MORE', 'pressure'); unparseable -> ('', '')."""
    text = label.strip().lower()
    for prefix, name in _GUIDEWORD_PREFIXES:
        if text.startswith(prefix):
            param = text[len(prefix):].strip(" -—:")
            return name, param
    return "", ""


def worksheet_to_document(
    path: str | Path,
    doc_id: str,
    title: str = "",
    date: str = "",
    source_type: SourceType = SourceType.HISTORICAL_HAZOP,
    applicability: Applicability | None = None,
    curation: CurationStatus = CurationStatus.PENDING,
    sheet_index: int = 0,
) -> tuple[KBDocument, XlsxIngestReport]:
    """Read one HAZOP worksheet and emit a KB document, one chunk per row.

    The first row bearing a recognizable header (deviation or
    guideword/parameter column) starts the table; everything above is
    treated as sheet furniture and ignored.
    """
    rows = read_xlsx_rows(path, sheet_index)
    report = XlsxIngestReport()

    header_at, columns = None, {}
    for i, row in enumerate(rows):
        m = _map_headers(row)
        if "deviation" in m or ("guideword" in m and "parameter" in m):
            header_at, columns = i, m
            break
    if header_at is None:
        raise ValueError(
            f"{path}: no header row with a deviation or guideword/parameter "
            f"column — cannot map worksheet columns")

    def cell(row: list[str], name: str) -> str:
        i = columns.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    chunks: list[KBChunk] = []
    for i, row in enumerate(rows[header_at + 1:], start=header_at + 2):
        report.rows_read += 1
        deviation = cell(row, "deviation")
        guideword = cell(row, "guideword")
        parameter = cell(row, "parameter")
        if deviation and not (guideword or parameter):
            guideword, parameter = _split_deviation(deviation)
        elif guideword and not deviation:
            deviation = f"{guideword} {parameter}".strip()

        parts = []
        node = cell(row, "node")
        if node:
            parts.append(f"Node {node}")
        if deviation:
            parts.append(f"Deviation: {deviation}")
        for name, label in (("causes", "Causes"),
                            ("consequences", "Consequences"),
                            ("safeguards", "Safeguards"),
                            ("recommendations", "Recommendations")):
            value = cell(row, name)
            if value:
                parts.append(f"{label}: {value}")

        has_content = any(cell(row, n) for n in
                          ("causes", "consequences", "safeguards"))
        if not (deviation and has_content):
            report.skipped_empty.append(i)
            continue

        gw = guideword.strip().upper().replace(" ", "_").replace("/", "_")
        gw = {"NO_NOT": "NO"}.get(gw, gw)
        if not gw or not parameter:
            report.untagged.append(i)
        chunks.append(KBChunk(
            chunk_id=f"row-{i}",
            text=". ".join(parts) + ".",
            guidewords=[gw] if gw else [],
            parameters=[parameter.lower()] if parameter else [],
        ))
    report.chunks = len(chunks)

    doc = KBDocument(
        doc_id=doc_id,
        source_type=source_type,
        title=title or f"Historical HAZOP worksheet ({Path(path).name})",
        date=date,
        curation=curation,          # pending by default: curator gate
        applicability=applicability or Applicability(),
        chunks=chunks,
    )
    return doc, report
