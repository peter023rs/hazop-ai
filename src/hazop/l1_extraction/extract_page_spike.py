"""
Per-sheet extraction spike: dump one P&ID page's text (with coordinates) and
vector geometry into inspectable JSON, plus a rendered PNG for visual reference.

Usage:
    python3 scripts/extract_page_spike.py [page_number]

Output (in output/page<N>_spike/):
    page<N>.png              - rendered page image
    page<N>_text_spans.json  - every text span: text, bbox, font, size, color
    page<N>_vector_paths.json- every vector path ("drawing"): style + geometry summary
    page<N>_summary.json     - aggregate stats to gauge symbol/line separability
"""
import fitz
import json
import sys
import os
from collections import Counter

PDF_PATH = "2401单元工艺管道及仪表流程图.pdf"


def extract_page(pdf_path, page_num, out_dir):
    doc = fitz.open(pdf_path)
    page = doc[page_num - 1]
    os.makedirs(out_dir, exist_ok=True)

    # 1. Rendered image, for visual cross-reference against the JSON dumps
    zoom = 150 / 72
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    pix.save(os.path.join(out_dir, f"page{page_num}.png"))

    # 2. Text spans with coordinates
    text_dict = page.get_text("dict")
    spans = []
    for block in text_dict["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                spans.append({
                    "text": span["text"],
                    "bbox": [round(v, 2) for v in span["bbox"]],
                    "font": span["font"],
                    "size": round(span["size"], 2),
                    "color": span["color"],
                })
    with open(os.path.join(out_dir, f"page{page_num}_text_spans.json"), "w", encoding="utf-8") as f:
        json.dump(spans, f, ensure_ascii=False, indent=2)

    # 3. Vector paths ("drawings" = one moveto..stroke/fill sequence, often one CAD entity)
    drawings = page.get_drawings()
    path_records = []
    for d in drawings:
        n_line = n_curve = n_rect = n_quad = 0
        for item in d["items"]:
            op = item[0]
            if op == "l":
                n_line += 1
            elif op == "c":
                n_curve += 1
            elif op == "re":
                n_rect += 1
            elif op == "qu":
                n_quad += 1
        rect = d["rect"]
        w, h = round(rect.width, 2), round(rect.height, 2)
        path_records.append({
            "type": d.get("type"),
            "color": d.get("color"),
            "fill": d.get("fill"),
            "width": d.get("width"),
            "dashes": d.get("dashes"),
            "closePath": d.get("closePath"),
            "n_items": len(d["items"]),
            "n_line": n_line, "n_curve": n_curve, "n_rect": n_rect, "n_quad": n_quad,
            "bbox": [round(v, 2) for v in rect],
            "bbox_w": w, "bbox_h": h,
            "aspect": round(max(w, h) / max(min(w, h), 0.01), 1),
        })
    with open(os.path.join(out_dir, f"page{page_num}_vector_paths.json"), "w") as f:
        json.dump(path_records, f, indent=2)

    # 4. Aggregate stats: do symbols vs. lines separate on style or geometry alone?
    color_width_counter = Counter((r["color"], r["width"]) for r in path_records)
    n_items_hist = Counter(r["n_items"] for r in path_records)
    bbox_area_sorted = sorted(
        ((r["bbox_w"] * r["bbox_h"], i) for i, r in enumerate(path_records)), reverse=True
    )
    elongated = [r for r in path_records if r["aspect"] > 10]
    compact = [r for r in path_records if r["aspect"] <= 2 and r["n_items"] <= 6]

    summary = {
        "page": page_num,
        "n_text_spans": len(spans),
        "n_vector_paths": len(path_records),
        "n_images": len(page.get_images()),
        "top_color_width_combos": color_width_counter.most_common(10),
        "n_items_histogram": dict(n_items_hist.most_common(15)),
        "largest_bboxes_by_area": [
            {"area": round(a, 1), "bbox": path_records[i]["bbox"], "n_items": path_records[i]["n_items"]}
            for a, i in bbox_area_sorted[:10]
        ],
        "n_elongated_paths_aspect_gt_10": len(elongated),
        "n_compact_paths_aspect_lte_2_and_few_items": len(compact),
    }
    with open(os.path.join(out_dir, f"page{page_num}_summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    return summary


if __name__ == "__main__":
    page_num = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    out_dir = f"output/page{page_num}_spike"
    summary = extract_page(PDF_PATH, page_num, out_dir)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
