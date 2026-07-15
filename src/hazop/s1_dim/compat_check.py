"""
Compatibility pre-check: score an incoming PDF against the pipeline's
assumptions BEFORE extraction, so a foreign-format document is flagged
instead of producing silently wrong topology.

Runs on the scale-normalized document (widths/radii already at reference
scale). Five checks, three severities:

  1. vector-ness      raster coverage must be negligible; drawings abundant
  2. text layer       real text spans must exist (tags come from text)
  3. bubble anchor    the ISA-bubble radius cluster must exist (also the
                      scale calibration anchor)
  4. width scheme     most strokes must match the known width roles
                      (0.14/0.43/0.71/1.13/1.70 pt at reference scale)
  5. tag grammar      the plant's tag/line-number regexes must actually hit

Verdict: "compatible"  - all checks pass
         "degraded"    - warnings only; extraction runs but needs review
         "unsupported" - a hard requirement failed; extraction is refused

API:    check(doc, norm_report) -> report dict
Usage:  python3 scripts/compat_check.py some.pdf
"""
import json
import math
import re
import sys

import fitz

# reference width roles (pt, at reference scale) - see trace_lines.py
KNOWN_WIDTHS = (0.14, 0.43, 0.71, 1.13, 1.70)
REF_BUBBLE_R = 16.4

LINE_NO_RE = re.compile(r"^\d{2,3}-[A-Z]{1,4}-\d{4,5}-[A-Z0-9]+(-[A-Z0-9]+)?$")
EQUIP_TAG_RE = re.compile(r"^24\d{2}-[A-Z]{1,3}-\d{3,4}[A-Z]?(/[A-Z])?$")
FUNC_RE = re.compile(r"^[A-Z]{2,5}$")   # ISA function letters (PT, LICA...)
LOOP_RE = re.compile(r"^\d{3,5}$")      # loop numbers


def _sample_pages(doc, k=5):
    n = len(doc)
    return sorted({int(n * i / (k + 1)) for i in range(1, k + 1)})


def check(doc, norm_report=None):
    checks = []

    def add(name, level, ok, detail):
        checks.append({"name": name, "level": level, "ok": ok,
                       "detail": detail})

    sample = _sample_pages(doc)

    # -- 1. vector-ness ------------------------------------------------------
    img_cover, n_draw = [], []
    for i in sample:
        page = doc[i]
        area = page.rect.width * page.rect.height
        cover = 0.0
        for img in page.get_images(full=True):
            for r in page.get_image_rects(img[0]):
                cover += r.width * r.height
        img_cover.append(cover / area)
        n_draw.append(len(page.get_drawings()))
    med_cover = sorted(img_cover)[len(img_cover) // 2]
    med_draw = sorted(n_draw)[len(n_draw) // 2]
    add("vector-pages", "fail" if (med_cover > 0.3 or med_draw < 50) else "pass",
        med_cover <= 0.3 and med_draw >= 50,
        f"median raster coverage {med_cover:.0%}, {med_draw} vector paths/page")

    # -- 2. text layer -------------------------------------------------------
    n_spans = []
    all_texts = []
    for i in sample:
        spans = []
        for block in doc[i].get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    if span["text"].strip():
                        spans.append(span["text"].strip())
        n_spans.append(len(spans))
        all_texts += spans
    med_spans = sorted(n_spans)[len(n_spans) // 2]
    add("text-layer", "fail" if med_spans < 20 else "pass", med_spans >= 20,
        f"median {med_spans} text spans/page")

    # -- 3. bubble anchor ----------------------------------------------------
    nb = (norm_report or {}).get("n_calibration_bubbles", 0)
    agree = (norm_report or {}).get("bubble_page_agree")
    if nb >= 5:
        lvl, ok = ("warn", False) if agree is False else ("pass", True)
        det = f"{nb} calibration bubbles" + (
            "; bubble/page scale disagree" if agree is False else "")
    else:
        lvl, ok, det = "warn", False, \
            f"only {nb} ISA-bubble candidates - calibration fell back to page size"
    add("bubble-anchor", lvl, ok, det)

    # -- 4. width scheme -----------------------------------------------------
    known = unknown = 0
    for i in sample:
        for d in doc[i].get_drawings():
            w = d.get("width")
            if w is None or w == 0:
                continue
            if any(abs(w - kw) < 0.05 for kw in KNOWN_WIDTHS):
                known += 1
            else:
                unknown += 1
    total = known + unknown
    frac = known / total if total else 0.0
    lvl = "pass" if frac >= 0.7 else ("warn" if frac >= 0.4 else "fail")
    add("width-scheme", lvl, frac >= 0.7,
        f"{frac:.0%} of {total} stroked paths match the reference width roles")

    # -- 5. tag grammar ------------------------------------------------------
    n_line = sum(1 for t in all_texts if LINE_NO_RE.match(t))
    n_equip = sum(1 for t in all_texts if EQUIP_TAG_RE.match(t))
    n_func = sum(1 for t in all_texts if FUNC_RE.match(t))
    n_loop = sum(1 for t in all_texts if LOOP_RE.match(t))
    hits = (n_line > 0) + (n_equip > 0) + (n_func >= 3 and n_loop >= 3)
    lvl = "pass" if hits >= 2 else ("warn" if hits == 1 else "fail")
    add("tag-grammar", lvl, hits >= 2,
        f"line-numbers {n_line}, equipment tags {n_equip}, "
        f"instrument texts {min(n_func, n_loop)}")

    # -- verdict --------------------------------------------------------------
    if any(c["level"] == "fail" for c in checks):
        verdict = "unsupported"
    elif any(c["level"] == "warn" for c in checks):
        verdict = "degraded"
    else:
        verdict = "compatible"
    return {"verdict": verdict, "checks": checks,
            "sampled_pages": [p + 1 for p in sample]}


if __name__ == "__main__":
    from hazop.s1_dim import normalize_pdf as NP
    doc = fitz.open(sys.argv[1])
    rep = NP.measure_scale(doc)
    print(json.dumps(check(doc, rep), indent=1))
