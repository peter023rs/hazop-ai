"""
Scale normalization front door: bring any vector P&ID of the unit-2401
drawing convention to the reference export scale, so the extraction
pipeline's absolute-point constants apply unchanged.

Calibration cascade (most reliable first):
  1. instrument-bubble radius - detect circle paths that contain text and
     take the dominant radius cluster; ISA bubbles are the most abundant,
     most rigidly sized symbol on any sheet (reference: 16.4 pt)
  2. page diagonal vs the reference A1 export - fallback, and a sanity
     check against (1); large disagreement is flagged, not guessed away

Normalization re-emits each page via show_pdf_page with a scaling matrix:
geometry, stroke widths and text sizes all scale together, so detectors
tuned on the reference need no changes.

API:    measure_scale(doc) -> report dict
        normalize(src_pdf, dst_pdf) -> report dict (writes dst only if needed)
Usage:  python3 scripts/normalize_pdf.py in.pdf out.pdf
"""
import json
import math
import sys
from collections import Counter

import fitz

REF_W, REF_H = 2383.94, 1683.78          # unit-2401 A1 export page size (pt)
REF_DIAG = math.hypot(REF_W, REF_H)
REF_BUBBLE_R = 16.4                       # ISA instrument bubble radius (pt)
SCALE_TOL = 0.02                          # |s-1| below this: no rewrite
AGREE_TOL = 0.15                          # bubble vs page scale disagreement


def _circles_with_text(page):
    """Radii of circle-shaped paths that contain at least one text span."""
    spans = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                if span["text"].strip():
                    x0, y0, x1, y1 = span["bbox"]
                    spans.append(((x0 + x1) / 2, (y0 + y1) / 2))
    radii = []
    for d in page.get_drawings():
        items = d["items"]
        if len(items) < 8:
            continue
        pts = []
        for it in items:
            if it[0] == "l":
                pts.append(it[1]); pts.append(it[2])
            elif it[0] == "c":
                pts.append(it[1]); pts.append(it[4])
        if len(pts) < 8:
            continue
        cx = sum(p.x for p in pts) / len(pts)
        cy = sum(p.y for p in pts) / len(pts)
        rs = [math.hypot(p.x - cx, p.y - cy) for p in pts]
        rm = sum(rs) / len(rs)
        if rm == 0:
            continue
        rstd = math.sqrt(sum((r - rm) ** 2 for r in rs) / len(rs))
        if rstd / rm > 0.03:
            continue
        if any(math.hypot(sx - cx, sy - cy) <= rm for sx, sy in spans):
            radii.append(rm)
    return radii


def measure_scale(doc, max_sample_pages=5):
    """Estimate drawing scale vs the reference convention."""
    # page-size estimate from the first page (warn on mixed sizes)
    dims = {(round(pg.rect.width), round(pg.rect.height)) for pg in doc}
    r0 = doc[0].rect
    s_page = math.hypot(r0.width, r0.height) / REF_DIAG

    # bubble estimate: sample pages spread through the doc
    n = len(doc)
    sample = sorted({int(i) for i in
                     [n * k / (max_sample_pages + 1) for k in
                      range(1, max_sample_pages + 1)]})
    radii = []
    for i in sample:
        radii += _circles_with_text(doc[i])
    s_bubble, n_bubbles = None, 0
    if radii:
        # Dominant radius cluster (0.5pt bins). Circles-with-text include
        # purge marks, equipment circles etc. at various sizes, but ISA
        # instrument bubbles are by far the most numerous - the biggest
        # cluster IS the bubble population. Require >=5 members so a
        # near-empty drawing can't calibrate off noise.
        binned = Counter(round(r * 2) / 2 for r in radii)
        r_dom, count = binned.most_common(1)[0]
        cluster = [r for r in radii if abs(r - r_dom) <= 1.0]
        if len(cluster) >= 5:
            s_bubble = (sum(cluster) / len(cluster)) / REF_BUBBLE_R
            n_bubbles = len(cluster)

    if s_bubble is not None:
        method = "bubble-radius"
        s = s_bubble
        agree = abs(s_bubble - s_page) / s_page <= AGREE_TOL
    else:
        method = "page-size"
        s = s_page
        agree = None
    return {
        "scale": round(s, 4),
        "method": method,
        "s_page": round(s_page, 4),
        "s_bubble": round(s_bubble, 4) if s_bubble else None,
        "n_calibration_bubbles": n_bubbles,
        "bubble_page_agree": agree,
        "mixed_page_sizes": len(dims) > 1,
        "needs_normalization": abs(s - 1) > SCALE_TOL,
    }


def normalize(src_pdf, dst_pdf):
    """Write a reference-scale copy of src_pdf to dst_pdf when needed.
    Returns the measurement report with 'normalized' and 'output' fields."""
    doc = fitz.open(src_pdf)
    rep = measure_scale(doc)
    s = rep["scale"]
    if not rep["needs_normalization"]:
        rep["normalized"] = False
        rep["output"] = src_pdf
        return rep
    # Re-emit every page scaled by 1/s onto a smaller/larger page:
    # show_pdf_page applies the transform to the whole content stream, so
    # geometry, stroke widths AND text sizes shrink/grow together - which is
    # exactly why the downstream absolute-point constants stay valid.
    out = fitz.open()
    for page in doc:
        w, h = page.rect.width / s, page.rect.height / s
        newp = out.new_page(width=w, height=h)
        newp.show_pdf_page(newp.rect, doc, page.number)
    out.save(dst_pdf)
    rep["normalized"] = True
    rep["output"] = dst_pdf
    return rep


if __name__ == "__main__":
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else "normalized.pdf"
    print(json.dumps(normalize(src, dst), indent=1))
