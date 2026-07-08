"""
Build a self-contained HTML validation viewer for the extracted topology.

Renders each process sheet to PNG, embeds it (base64) together with the
per-sheet topology JSON, and emits one static HTML file with:
  - page selector, zoom, layer toggles per element kind
  - hover tooltips (tags, line numbers, labels)
  - click a node/edge -> highlight; Accept (A) / Reject (R) verdicts
  - "mark missing" mode to record elements the pipeline failed to find
  - live review stats (accept/reject/missing per kind)
  - label persistence: POST /labels/<slug> when served by app.py,
    localStorage otherwise; JSON export button always available

No server needed for viewing: open output/viewer.html in a browser.

Usage:  python3 scripts/build_viewer.py
"""
import base64
import json

import fitz

PDF_PATH = "2401单元工艺管道及仪表流程图.pdf"
PROCESS_PAGES = range(4, 13)
ZOOM = 1.6  # render scale (pdf pt -> px)


def build():
    # Everything (page renders + topology JSON + JS) is embedded into ONE
    # HTML file so the viewer works from a file:// open with no server.
    # Overlay coordinates: topology is in PDF points; images are rendered at
    # ZOOM px/pt, so the JS multiplies every coordinate by Z.
    doc = fitz.open(PDF_PATH)
    pages = []
    for p in PROCESS_PAGES:
        page = doc[p - 1]
        pix = page.get_pixmap(matrix=fitz.Matrix(ZOOM, ZOOM))
        png_b64 = base64.b64encode(pix.tobytes("png")).decode()
        topo = json.load(open(f"output/topology_page{p}.json"))
        pages.append({
            "page": p,
            "w": pix.width, "h": pix.height,
            "img": png_b64,
            "topo": topo,
        })

    data_js = json.dumps(pages, ensure_ascii=False)

    # compatibility verdict (written by run_pipeline's pre-check, if any)
    try:
        compat = json.load(open("output/compat_report.json"))
    except FileNotFoundError:
        compat = None
    compat_js = json.dumps(compat, ensure_ascii=False)

    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>HAZOP L1 - Topology Validation Viewer</title>
<style>
  body { margin:0; font-family: -apple-system, sans-serif; display:flex;
         height:100vh; overflow:hidden; background:#1e1e24; color:#ddd; }
  #sidebar { width:265px; min-width:265px; padding:14px; background:#26262e;
             overflow-y:auto; border-right:1px solid #3a3a44; }
  #main { flex:1; overflow:auto; position:relative; }
  #stage { position:relative; transform-origin: 0 0; }
  #stage img { display:block; }
  #stage svg { position:absolute; inset:0; }
  h1 { font-size:15px; margin:0 0 10px; }
  h2 { font-size:12px; text-transform:uppercase; letter-spacing:.08em;
       color:#999; margin:16px 0 6px; }
  select, input[type=range] { width:100%; margin-bottom:6px; }
  label.layer { display:flex; align-items:center; gap:6px; font-size:13px;
                padding:2px 0; cursor:pointer; }
  .sw { width:12px; height:12px; border-radius:3px; display:inline-block; }
  #counts, #review { font-size:12px; color:#aaa; line-height:1.7; }
  #tip { position:fixed; pointer-events:none; background:#000c;
         color:#fff; padding:5px 9px; border-radius:5px; font-size:12px;
         display:none; z-index:10; max-width:320px; }
  #info { font-size:12px; background:#1a1a20; border:1px solid #3a3a44;
          border-radius:6px; padding:8px; margin-top:10px; min-height:60px;
          line-height:1.5; }
  .node, .edgehit { cursor:pointer; }
  .dim { opacity:0.12; }
  .hl  { stroke-width:4 !important; }
  button { background:#3a3a46; color:#ddd; border:1px solid #4a4a56;
           border-radius:6px; padding:5px 10px; font-size:12px;
           cursor:pointer; margin:2px 2px 2px 0; }
  button.acc { background:#1e5c33; } button.rej { background:#6e2323; }
  button.on  { outline:2px solid #4fc3f7; }
  #saveState { color:#7a7; font-size:11px; }
</style>
</head>
<body>
<div id="sidebar">
  <h1>Topology Validator</h1>
  <div id="compat"></div>
  <h2>Sheet</h2>
  <select id="pageSel"></select>
  <h2>Zoom</h2>
  <input type="range" id="zoom" min="0.2" max="2.5" step="0.05" value="0.55">
  <h2>Layers</h2>
  <div id="layers"></div>
  <h2>Selection</h2>
  <div id="info">Click a node or line.</div>
  <div>
    <button class="acc" onclick="verdict('accept')">Accept (A)</button>
    <button class="rej" onclick="verdict('reject')">Reject (R)</button>
    <button onclick="verdict(null)">Clear (U)</button>
  </div>
  <h2>Review</h2>
  <button id="missBtn" onclick="toggleMissing()">+ Mark missing</button>
  <select id="missKind">
    <option>valve</option><option>instrument</option><option>equipment</option>
    <option>nozzle</option><option>pipe-run</option><option>signal-line</option>
    <option>off-page</option><option>other</option>
  </select>
  <div id="review"></div>
  <button onclick="exportLabels()">Export labels JSON</button>
  <span id="saveState"></span>
  <h2>Counts</h2>
  <div id="counts"></div>
</div>
<div id="main"><div id="stage">
  <img id="sheet"><svg id="ov" xmlns="http://www.w3.org/2000/svg"></svg>
</div></div>
<div id="tip"></div>
<script>
const PAGES = __DATA__;
const Z = __ZOOM__;
const COMPAT = __COMPAT__;
if (COMPAT) {
  const col = {compatible:"#2ecc71", degraded:"#f39c12",
               unsupported:"#e74c3c"}[COMPAT.verdict] || "#999";
  const warns = COMPAT.checks.filter(c=>c.level!=="pass")
    .map(c=>c.name+": "+c.detail).join("<br>");
  document.getElementById("compat").innerHTML =
    '<div style="font-size:12px;border:1px solid '+col+';border-radius:6px;'+
    'padding:6px 8px;margin-bottom:6px;">format check: <b style="color:'+col+
    '">'+COMPAT.verdict+"</b>"+(warns?("<br><span style='color:#999'>"+warns+
    "</span>"):"")+"</div>";
}
const KIND_STYLE = {
  equipment:  {color:"#2ecc71", label:"Equipment"},
  nozzle:     {color:"#f39c12", label:"Nozzles"},
  valve:      {color:"#e91ee9", label:"Valves"},
  instrument: {color:"#3498db", label:"Instruments"},
  junction:   {color:"#b8860b", label:"Junctions"},
  "off-page": {color:"#e74c3c", label:"Off-page connectors"},
  "dead-end": {color:"#8b3a3a", label:"Dead ends"},
};
const EDGE_STYLE = {
  solid:    {color:"#ff5252", label:"Pipe runs"},
  dashed:   {color:"#4fc3f7", label:"Signal lines"},
  internal: {color:"#9e9e9e", label:"Nozzle links"},
};
let cur = 0, layerState = {}, selection = null, missingMode = false;
let labels = {};   // key -> {verdict, kind, tag, xy, sheet, ts}
let missSeq = 0;

// ---- persistence -------------------------------------------------------
const SERVER = location.pathname.startsWith("/view/");
const SLUG = SERVER ? decodeURIComponent(location.pathname.split("/")[2]) : null;
const LSKEY = "hazop-labels-" + (SLUG || "static-viewer");
async function loadLabels(){
  if (SERVER) {
    try { const r = await fetch("/labels/"+encodeURIComponent(SLUG));
          if (r.ok) labels = await r.json() || {}; } catch(e){}
  } else {
    try { labels = JSON.parse(localStorage.getItem(LSKEY)||"{}"); } catch(e){}
  }
  missSeq = Object.keys(labels).filter(k=>k.includes(":m")).length + 1;
}
let saveTimer=null;
function saveLabels(){
  document.getElementById("saveState").textContent="saving…";
  clearTimeout(saveTimer);
  saveTimer=setTimeout(async ()=>{
    if (SERVER) {
      try {
        await fetch("/labels/"+encodeURIComponent(SLUG), {method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify(labels)});
        document.getElementById("saveState").textContent="saved to server";
      } catch(e){ document.getElementById("saveState").textContent="save failed"; }
    } else {
      localStorage.setItem(LSKEY, JSON.stringify(labels));
      document.getElementById("saveState").textContent="saved locally";
    }
  }, 400);
}
function exportLabels(){
  const blob=new Blob([JSON.stringify(labels,null,1)],{type:"application/json"});
  const a=document.createElement("a"); a.href=URL.createObjectURL(blob);
  a.download="labels.json"; a.click();
}

// ---- UI scaffolding ----------------------------------------------------
const pageSel = document.getElementById("pageSel");
PAGES.forEach((p,i)=>{
  const o=document.createElement("option"); o.value=i;
  o.textContent="Sheet "+p.page;
  pageSel.appendChild(o);
});
pageSel.onchange = ()=>{ cur=+pageSel.value; selection=null; render(); };

const layersDiv=document.getElementById("layers");
[...Object.entries(KIND_STYLE).map(([k,v])=>["n:"+k,v]),
 ...Object.entries(EDGE_STYLE).map(([k,v])=>["e:"+k,v])].forEach(([key,st])=>{
  layerState[key]=true;
  const l=document.createElement("label"); l.className="layer";
  const c=document.createElement("input"); c.type="checkbox"; c.checked=true;
  c.onchange=()=>{ layerState[key]=c.checked; applyLayers(); };
  const sw=document.createElement("span"); sw.className="sw";
  sw.style.background=st.color;
  l.append(c,sw,st.label);
  layersDiv.appendChild(l);
});

document.getElementById("zoom").oninput = e=>{
  document.getElementById("stage").style.transform="scale("+e.target.value+")";
};

const tip=document.getElementById("tip");
function showTip(evt,text){ tip.style.display="block"; tip.innerHTML=text;
  tip.style.left=(evt.clientX+14)+"px"; tip.style.top=(evt.clientY+8)+"px"; }
function hideTip(){ tip.style.display="none"; }

function nodeTitle(n){
  let t="<b>"+n.kind+"</b>";
  if(n.full_tag||n.tag) t+=" "+(n.full_tag||n.tag);
  if(n.nozzle_label) t+=" "+n.nozzle_label;
  if(n.labels) t+="<br>"+n.labels.join("<br>");
  if(n.ball) t+=" (ball)";
  return t;
}
function edgeTitle(e){
  let t="<b>"+(e.style==="dashed"?"signal line":e.style==="internal"?"nozzle link":"pipe run")+"</b>";
  if(e.line_numbers) t+=" "+e.line_numbers.join(", ");
  t+="<br>len "+(e.length||0)+" pt";
  return t;
}
const SVGNS="http://www.w3.org/2000/svg";

// ---- render ------------------------------------------------------------
function render(){
  const P=PAGES[cur];
  document.getElementById("sheet").src="data:image/png;base64,"+P.img;
  const svg=document.getElementById("ov");
  svg.setAttribute("viewBox","0 0 "+P.w+" "+P.h);
  svg.setAttribute("width",P.w); svg.setAttribute("height",P.h);
  svg.innerHTML="";
  const adj={};
  P.topo.edges.forEach((e,ei)=>{
    const st=EDGE_STYLE[e.style]||EDGE_STYLE.solid;
    const ptsAttr=(e.points||[]).map(pt=>(pt[0]*Z)+","+(pt[1]*Z)).join(" ");
    const pl=document.createElementNS(SVGNS,"polyline");
    pl.setAttribute("points",ptsAttr);
    pl.setAttribute("fill","none");
    pl.setAttribute("stroke",st.color);
    pl.setAttribute("stroke-width",e.style==="internal"?1.2:2.2);
    if(e.style==="dashed") pl.setAttribute("stroke-dasharray","6 4");
    if(e.style==="internal") pl.setAttribute("stroke-dasharray","2 3");
    pl.dataset.layer="e:"+(e.style in EDGE_STYLE?e.style:"solid");
    pl.dataset.eid=ei;
    svg.appendChild(pl);
    // fat invisible twin for reliable clicking
    const hit=document.createElementNS(SVGNS,"polyline");
    hit.setAttribute("points",ptsAttr);
    hit.setAttribute("fill","none");
    hit.setAttribute("stroke","#000"); hit.setAttribute("stroke-opacity","0");
    hit.setAttribute("stroke-width",9);
    hit.classList.add("edgehit");
    hit.dataset.layer=pl.dataset.layer;
    hit.addEventListener("mousemove",evt=>showTip(evt,edgeTitle(e)));
    hit.addEventListener("mouseleave",hideTip);
    hit.addEventListener("click",evt=>{ evt.stopPropagation(); selectEdge(e,ei,adj,svg); });
    svg.appendChild(hit);
    (adj[e.a]=adj[e.a]||[]).push(ei); (adj[e.b]=adj[e.b]||[]).push(ei);
  });
  P.topo.nodes.forEach(n=>{
    const st=KIND_STYLE[n.kind]; if(!st) return;
    let el;
    if(n.kind==="equipment" && n.bbox){
      el=document.createElementNS(SVGNS,"rect");
      el.setAttribute("x",n.bbox[0]*Z); el.setAttribute("y",n.bbox[1]*Z);
      el.setAttribute("width",(n.bbox[2]-n.bbox[0])*Z);
      el.setAttribute("height",(n.bbox[3]-n.bbox[1])*Z);
      el.setAttribute("fill","none");
    } else {
      el=document.createElementNS(SVGNS,"circle");
      el.setAttribute("cx",n.xy[0]*Z); el.setAttribute("cy",n.xy[1]*Z);
      el.setAttribute("r",n.kind==="junction"||n.kind==="dead-end"?4:8);
      el.setAttribute("fill",n.kind==="junction"||n.kind==="dead-end"?st.color:"none");
    }
    el.setAttribute("stroke",st.color);
    el.setAttribute("stroke-width",2);
    el.classList.add("node");
    el.dataset.layer="n:"+n.kind;
    el.dataset.nid=n.id;
    el.addEventListener("mousemove",evt=>showTip(evt,nodeTitle(n)));
    el.addEventListener("mouseleave",hideTip);
    el.addEventListener("click",evt=>{ evt.stopPropagation(); selectNode(n,adj,svg); });
    svg.appendChild(el);
  });
  drawBadges(svg);
  const kc={}, ec={};
  P.topo.nodes.forEach(n=>kc[n.kind]=(kc[n.kind]||0)+1);
  P.topo.edges.forEach(e=>ec[e.style]=(ec[e.style]||0)+1);
  document.getElementById("counts").innerHTML =
    "prefix: <b>"+(P.topo.sheet_prefix||"?")+"</b><br>"+
    Object.entries(kc).map(([k,v])=>k+": <b>"+v+"</b>").join("<br>")+"<br>"+
    Object.entries(ec).map(([k,v])=>k+" edges: <b>"+v+"</b>").join("<br>");
  applyLayers();
  updateReview();
}

// ---- verdict badges ----------------------------------------------------
function labelKey(type,id){ return "p"+PAGES[cur].page+":"+type+id; }
function badgePos(rec){ return rec.xy; }
function drawBadges(svg){
  svg.querySelectorAll(".badge").forEach(b=>b.remove());
  const P=PAGES[cur];
  const nodesById={}; P.topo.nodes.forEach(n=>nodesById[n.id]=n);
  Object.entries(labels).forEach(([key,rec])=>{
    if(!key.startsWith("p"+P.page+":")) return;
    let xy=rec.xy;
    if(!xy) return;
    const g=document.createElementNS(SVGNS,"g");
    g.classList.add("badge");
    const c=document.createElementNS(SVGNS,"circle");
    c.setAttribute("cx",xy[0]*Z); c.setAttribute("cy",xy[1]*Z);
    c.setAttribute("r",6);
    c.setAttribute("fill",rec.verdict==="accept"?"#2ecc71":
                          rec.verdict==="reject"?"#e74c3c":"#f1c40f");
    c.setAttribute("stroke","#111"); c.setAttribute("stroke-width",1);
    const t=document.createElementNS(SVGNS,"text");
    t.setAttribute("x",xy[0]*Z); t.setAttribute("y",xy[1]*Z+3.5);
    t.setAttribute("text-anchor","middle");
    t.setAttribute("font-size","9"); t.setAttribute("fill","#111");
    t.textContent=rec.verdict==="accept"?"✓":rec.verdict==="reject"?"✗":"?";
    g.append(c,t);
    if(rec.verdict==="missing"){
      g.style.cursor="pointer";
      g.addEventListener("click",evt=>{
        evt.stopPropagation();
        if(confirm("Remove this missing-marker?")){ delete labels[key];
          saveLabels(); render(); }
      });
      g.addEventListener("mousemove",evt=>showTip(evt,
        "<b>missing "+(rec.kind||"?")+"</b><br>click to remove"));
      g.addEventListener("mouseleave",hideTip);
    }
    svg.appendChild(g);
  });
}

// ---- selection + verdicts ---------------------------------------------
function clearHl(svg){
  svg.querySelectorAll(".dim,.hl").forEach(el=>el.classList.remove("dim","hl"));
}
function selectNode(n,adj,svg){
  clearHl(svg);
  selection={type:"n", id:n.id, key:labelKey("n",n.id),
             kind:n.kind, tag:n.full_tag||n.tag||n.nozzle_label||null,
             xy:n.xy};
  const eids=new Set(adj[n.id]||[]);
  const P=PAGES[cur];
  const nbr=new Set([n.id]);
  eids.forEach(ei=>{ nbr.add(P.topo.edges[ei].a); nbr.add(P.topo.edges[ei].b); });
  svg.querySelectorAll("[data-eid]").forEach(el=>{
    if(eids.has(+el.dataset.eid)) el.classList.add("hl");
    else el.classList.add("dim");
  });
  svg.querySelectorAll("[data-nid]").forEach(el=>{
    if(!nbr.has(+el.dataset.nid)) el.classList.add("dim");
  });
  showSelection(nodeTitle(n)+"<br>degree: "+(adj[n.id]||[]).length);
}
function selectEdge(e,ei,adj,svg){
  clearHl(svg);
  const pts=e.points||[]; const mid=pts[Math.floor(pts.length/2)]||pts[0];
  selection={type:"e", id:ei, key:labelKey("e",ei),
             kind:e.style==="dashed"?"signal-line":"pipe-run",
             tag:(e.line_numbers||[]).join(",")||null, xy:mid};
  svg.querySelectorAll("[data-eid]").forEach(el=>{
    if(+el.dataset.eid===ei) el.classList.add("hl");
    else el.classList.add("dim");
  });
  svg.querySelectorAll("[data-nid]").forEach(el=>{
    if(+el.dataset.nid!==e.a && +el.dataset.nid!==e.b) el.classList.add("dim");
  });
  showSelection(edgeTitle(e));
}
function showSelection(html){
  const rec=labels[selection.key];
  document.getElementById("info").innerHTML = html +
    (rec?("<br>verdict: <b>"+rec.verdict+"</b>"):"<br>verdict: -");
}
function verdict(v){
  if(!selection) return;
  if(v===null){ delete labels[selection.key]; }
  else labels[selection.key]={verdict:v, kind:selection.kind,
    tag:selection.tag, xy:selection.xy, sheet:PAGES[cur].page,
    ts:Date.now()};
  saveLabels();
  const svg=document.getElementById("ov");
  drawBadges(svg); updateReview();
  if(selection) showSelection(document.getElementById("info").innerHTML.split("<br>verdict")[0]);
}
document.addEventListener("keydown",e=>{
  if(e.target.tagName==="INPUT"||e.target.tagName==="SELECT") return;
  if(e.key==="a"||e.key==="A") verdict("accept");
  if(e.key==="r"||e.key==="R") verdict("reject");
  if(e.key==="u"||e.key==="U") verdict(null);
});

// ---- missing mode ------------------------------------------------------
function toggleMissing(){
  missingMode=!missingMode;
  document.getElementById("missBtn").classList.toggle("on",missingMode);
}
document.getElementById("ov").addEventListener("click",e=>{
  if(!missingMode) return;
  const svg=document.getElementById("ov");
  const pt=svg.createSVGPoint(); pt.x=e.clientX; pt.y=e.clientY;
  const loc=pt.matrixTransform(svg.getScreenCTM().inverse());
  const kind=document.getElementById("missKind").value;
  const key="p"+PAGES[cur].page+":m"+(missSeq++);
  labels[key]={verdict:"missing", kind:kind, tag:null,
    xy:[loc.x/Z, loc.y/Z], sheet:PAGES[cur].page, ts:Date.now()};
  saveLabels(); drawBadges(svg); updateReview();
});

// ---- review stats ------------------------------------------------------
function updateReview(){
  const per={accept:0,reject:0,missing:0};
  const byKind={};
  Object.values(labels).forEach(r=>{
    per[r.verdict]=(per[r.verdict]||0)+1;
    const k=r.kind||"?";
    byKind[k]=byKind[k]||{accept:0,reject:0,missing:0};
    byKind[k][r.verdict]=(byKind[k][r.verdict]||0)+1;
  });
  let html="all sheets — ✓ "+per.accept+" · ✗ "+per.reject+
           " · missing "+per.missing;
  const rows=Object.entries(byKind).map(([k,c])=>{
    const denom=c.accept+c.reject;
    const prec=denom?Math.round(100*c.accept/denom):null;
    return k+": ✓"+c.accept+" ✗"+c.reject+
      (c.missing?(" ?"+c.missing):"")+
      (prec!==null?(" — <b>"+prec+"%</b>"):"");
  });
  if(rows.length) html+="<br>"+rows.join("<br>");
  document.getElementById("review").innerHTML=html;
}

document.getElementById("main").addEventListener("click",e=>{
  if(e.target.id==="main"||e.target.id==="sheet"){
    if(missingMode) return;
    clearHl(document.getElementById("ov"));
    selection=null;
    document.getElementById("info").textContent="Click a node or line.";
  }
});

function applyLayers(){
  document.querySelectorAll("#ov [data-layer]").forEach(el=>{
    el.style.display = layerState[el.dataset.layer]===false ? "none":"";
  });
}

document.getElementById("stage").style.transform="scale(0.55)";
loadLabels().then(render);
</script>
</body>
</html>"""
    html = (html.replace("__DATA__", data_js)
                .replace("__ZOOM__", str(ZOOM))
                .replace("__COMPAT__", compat_js))
    with open("output/viewer.html", "w", encoding="utf-8") as f:
        f.write(html)
    import os
    size = os.path.getsize("output/viewer.html") / 1e6
    print(f"viewer -> output/viewer.html ({size:.1f} MB, {len(pages)} sheets)")


if __name__ == "__main__":
    build()
