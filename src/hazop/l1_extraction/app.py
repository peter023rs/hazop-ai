"""
Tier 2 validation website: upload a P&ID vector PDF (same drawing convention),
run the Stage 1 extraction pipeline, and view the labeled topology overlay.

Run:    .venv312/bin/python app.py
Open:   http://127.0.0.1:8777
"""
import os
import re
import time

from flask import (Flask, redirect, render_template_string, request,
                   send_file, url_for)

from hazop.l1_extraction.pipeline import run as run_pipeline

BASE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(BASE, "runs")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 80 * 1024 * 1024

INDEX_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>HAZOP L1 - P&ID Topology Extractor</title>
<style>
 body{font-family:-apple-system,sans-serif;background:#1e1e24;color:#ddd;
      max-width:760px;margin:40px auto;padding:0 20px;}
 h1{font-size:22px} h2{font-size:15px;color:#999;margin-top:30px}
 .card{background:#26262e;border:1px solid #3a3a44;border-radius:10px;
       padding:20px;margin:14px 0;}
 input[type=file],input[type=text]{width:100%;margin:6px 0 14px;color:#ddd;}
 input[type=text]{background:#1a1a20;border:1px solid #3a3a44;
       border-radius:6px;padding:8px;}
 button{background:#3498db;color:#fff;border:0;border-radius:6px;
       padding:10px 22px;font-size:14px;cursor:pointer;}
 a{color:#4fc3f7;text-decoration:none} a:hover{text-decoration:underline}
 .run{display:flex;justify-content:space-between;padding:9px 4px;
      border-bottom:1px solid #333;} .meta{color:#888;font-size:12px}
 .note{color:#999;font-size:13px;line-height:1.5}
</style></head><body>
<h1>P&amp;ID Topology Extractor <span style="color:#666;font-size:13px">Stage 1 validator</span></h1>
<div class="card">
 <form method="post" action="/upload" enctype="multipart/form-data">
  <label>Vector P&amp;ID PDF (same drawing convention as the 2401 unit set)</label>
  <input type="file" name="pdf" accept=".pdf" required>
  <label>Pages to process ("all", or e.g. "4-12")</label>
  <input type="text" name="pages" value="all">
  <button type="submit">Extract topology</button>
 </form>
 <p class="note">Extraction runs the deterministic pipeline: instrument
 bubbles &rarr; valves &rarr; line tracing &rarr; equipment &rarr; assembled
 topology graph, then opens an interactive overlay viewer. Legend sheets add
 noise &mdash; exclude them via the pages field if you know where they are.</p>
</div>
<h2>Previous extractions</h2>
{% for r in runs %}
 <div class="run"><a href="/view/{{r.slug}}">{{r.name}}</a>
  <span class="meta">
   {% if r.verdict %}<span style="color:{{r.vcolor}}">&#9679; {{r.verdict}}</span> &nbsp;{% endif %}
   {{r.when}}</span></div>
{% else %}<p class="note">None yet.</p>{% endfor %}
</body></html>"""


VERDICT_COLORS = {"compatible": "#2ecc71", "degraded": "#f39c12",
                  "unsupported": "#e74c3c"}


def list_runs():
    out = []
    if os.path.isdir(RUNS):
        for slug in sorted(os.listdir(RUNS), reverse=True):
            viewer = os.path.join(RUNS, slug, "output", "viewer.html")
            if os.path.exists(viewer):
                t = time.strftime("%Y-%m-%d %H:%M",
                                  time.localtime(os.path.getmtime(viewer)))
                name = slug.split("_", 1)[-1] or slug
                verdict = None
                compat = os.path.join(RUNS, slug, "output",
                                      "compat_report.json")
                if os.path.exists(compat):
                    import json as _json
                    verdict = _json.load(open(compat)).get("verdict")
                out.append({"slug": slug, "name": name, "when": t,
                            "verdict": verdict,
                            "vcolor": VERDICT_COLORS.get(verdict, "#999")})
    return out


@app.get("/")
def index():
    return render_template_string(INDEX_HTML, runs=list_runs())


@app.post("/upload")
def upload():
    # Each upload becomes an isolated run directory under runs/:
    # source.pdf + (normalized.pdf) + output/{indexes, graphs, topology,
    # viewer.html} + labels.json once the engineer starts reviewing.
    f = request.files.get("pdf")
    if f is None or not f.filename.lower().endswith(".pdf"):
        return "Please upload a PDF.", 400
    safe = re.sub(r"[^\w.一-鿿-]+", "_", f.filename)[:80]
    slug = f"{time.strftime('%Y%m%d-%H%M%S')}_{safe}"
    run_dir = os.path.join(RUNS, slug)
    os.makedirs(run_dir, exist_ok=True)
    pdf_path = os.path.join(run_dir, "source.pdf")
    f.save(pdf_path)
    try:
        stats = run_pipeline(pdf_path, run_dir,
                             request.form.get("pages", "all"))
    except Exception as ex:  # surface pipeline failures to the reviewer
        return (f"<pre>extraction failed: {type(ex).__name__}: {ex}</pre>"
                f'<a href="/">back</a>', 500)
    print("extracted:", slug, stats)
    return redirect(url_for("view", slug=slug))


@app.get("/view/<slug>")
def view(slug):
    if not re.fullmatch(r"[\w.一-鿿-]+", slug):
        return "bad run id", 400
    viewer = os.path.join(RUNS, slug, "output", "viewer.html")
    if not os.path.exists(viewer):
        return "run not found", 404
    return send_file(viewer)


@app.get("/labels/<slug>")
def get_labels(slug):
    if not re.fullmatch(r"[\w.一-鿿-]+", slug):
        return "bad run id", 400
    path = os.path.join(RUNS, slug, "labels.json")
    if not os.path.exists(path):
        return {}
    return send_file(path, mimetype="application/json")


@app.post("/labels/<slug>")
def post_labels(slug):
    if not re.fullmatch(r"[\w.一-鿿-]+", slug):
        return "bad run id", 400
    if not os.path.isdir(os.path.join(RUNS, slug)):
        return "run not found", 404
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return "expected JSON object", 400
    import json as _json
    with open(os.path.join(RUNS, slug, "labels.json"), "w",
              encoding="utf-8") as f:
        _json.dump(data, f, ensure_ascii=False, indent=1)
    return {"ok": True, "count": len(data)}


if __name__ == "__main__":
    os.makedirs(RUNS, exist_ok=True)
    app.run(host="127.0.0.1", port=8777, debug=False)
