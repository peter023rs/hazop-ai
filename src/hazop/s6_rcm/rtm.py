"""
rtm.py — Requirements Traceability Matrix (Fable section 9).

The Fable draft makes the RTM itself a requirement: "a requirements
traceability matrix SHALL be maintained linking every FR/MDL/NFR to design
elements, test cases, and validation evidence... a controlled deliverable."

Split of responsibilities:

  * data/rtm/requirements.json — the controlled deliverable. One entry per
    requirement ID with a human-owned STATUS (done/partial/todo/blocked/
    out_of_scope), notes, and manually curated evidence refs. Versioned,
    hand-editable, reviewable.
  * this module — everything derivable: it SCANS the source tree for
    requirement-ID citations (the codebase consistently cites IDs like
    "FR-ARE-9 / MDL-10" in docstrings), merges file:line evidence into each
    entry at read time, and computes per-section rollups. Scanned evidence
    is never written back into the JSON — it would go stale; it is re-derived
    on demand.

Status is deliberately NOT auto-derived: whether a requirement is "done" is
an engineering judgment; what the scanner can prove is only where the code
claims to address it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
RTM_PATH = REPO_ROOT / "data" / "rtm" / "requirements.json"

# directories scanned for requirement-ID citations (relative to repo root)
SCAN_DIRS = ("src", "tests")
SCAN_SUFFIXES = {".py", ".html", ".json", ".md"}
MAX_HITS_PER_REQ = 12

VALID_STATUSES = ("done", "partial", "todo", "blocked", "out_of_scope")

# progress weight per status (partial counts half, out_of_scope excluded)
_WEIGHT = {"done": 1.0, "partial": 0.5, "todo": 0.0, "blocked": 0.0}

_ID_PATTERN = re.compile(
    r"\b(?:FR-(?:DIM|PML|ARE|SW|RCM|AGM)|MDL|NFR|AR|DR|VV|OI|C|A)-\d{1,2}\b")


def load_rtm(path: str | Path = RTM_PATH) -> dict:
    with open(path) as f:
        data = json.load(f)
    seen = set()
    for r in data["requirements"]:
        if r["status"] not in VALID_STATUSES:
            raise ValueError(f"{r['id']}: invalid status {r['status']!r}")
        if r["id"] in seen:
            raise ValueError(f"duplicate requirement id {r['id']}")
        seen.add(r["id"])
    return data


def save_rtm(data: dict, path: str | Path = RTM_PATH) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def update_requirement(req_id: str, status: str | None = None,
                       notes: str | None = None,
                       path: str | Path = RTM_PATH) -> dict:
    """Update the human-owned fields of one requirement and persist.
    Returns the updated entry. Raises KeyError/ValueError on bad input."""
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(f"invalid status {status!r} "
                         f"(expected one of {VALID_STATUSES})")
    data = load_rtm(path)
    for r in data["requirements"]:
        if r["id"] == req_id:
            if status is not None:
                r["status"] = status
            if notes is not None:
                r["notes"] = notes
            save_rtm(data, path)
            return r
    raise KeyError(f"unknown requirement id {req_id!r}")


def scan_citations(known_ids: set[str],
                   root: str | Path = REPO_ROOT) -> dict[str, list[dict]]:
    """file:line evidence for every requirement ID cited in the source
    tree. Only IDs present in the RTM are kept, so prose that happens to
    look like an ID (e.g. an equipment tag) cannot invent a requirement."""
    root = Path(root)
    hits: dict[str, list[dict]] = {rid: [] for rid in known_ids}
    for scan_dir in SCAN_DIRS:
        base = root / scan_dir
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*")):
            if f.suffix not in SCAN_SUFFIXES or "__pycache__" in f.parts:
                continue
            rel = f.relative_to(root).as_posix()
            if rel == "data/rtm/requirements.json":
                continue          # the RTM itself is not evidence
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                for m in _ID_PATTERN.finditer(line):
                    rid = m.group(0)
                    if rid in known_ids and len(hits[rid]) < MAX_HITS_PER_REQ:
                        hits[rid].append({"file": rel, "line": lineno})
    return hits


def rollup(requirements: list[dict]) -> dict:
    """Per-section and overall progress. `progress` weighs done=1,
    partial=0.5 over all non-out_of_scope items."""
    sections: dict[str, dict] = {}
    for r in requirements:
        s = sections.setdefault(r["section"], {
            "counts": {st: 0 for st in VALID_STATUSES}, "total": 0})
        s["counts"][r["status"]] += 1
        s["total"] += 1

    def progress(counts: dict[str, int]) -> float:
        in_scope = sum(n for st, n in counts.items() if st != "out_of_scope")
        if not in_scope:
            return 1.0
        return sum(_WEIGHT.get(st, 0.0) * n for st, n in counts.items()
                   if st != "out_of_scope") / in_scope

    overall = {st: 0 for st in VALID_STATUSES}
    for s in sections.values():
        s["progress"] = round(progress(s["counts"]), 3)
        for st, n in s["counts"].items():
            overall[st] += n
    return {
        "sections": sections,
        "overall": {"counts": overall,
                    "total": sum(overall.values()),
                    "progress": round(progress(overall), 3)},
    }


def rtm_view(path: str | Path = RTM_PATH,
             with_citations: bool = True) -> dict:
    """The full RTM as served to the dashboard: entries (with scanned
    citations merged in), rollups, and metadata."""
    data = load_rtm(path)
    reqs = data["requirements"]
    citations = (scan_citations({r["id"] for r in reqs})
                 if with_citations else {})
    for r in reqs:
        r["citations"] = citations.get(r["id"], [])
    return {"meta": data["meta"], "requirements": reqs,
            "rollup": rollup(reqs)}
