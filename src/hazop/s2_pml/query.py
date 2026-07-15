"""
query.py — natural-language + Cypher query layer over the plant graph
(the IYP-style "query the database" experience for the HAZOP graph).

Three layers, mirroring the package philosophy (offline-first, LLM optional):

  parse_question(text, graph)   deterministic rule parser: NL -> Intent.
                                Every tag mention is grounded against the
                                actual graph — partial tags resolve, unknown
                                or ambiguous ones raise QueryError, nothing
                                is guessed.
  GraphQuery(graph)             executes an Intent (or a question string)
                                against the in-memory equipment graph via
                                the direction-aware topology reasoner and
                                returns a QueryResult: answer sentence,
                                table rows, the subgraph to draw, AND the
                                equivalent Cypher — every answer doubles as
                                a Cypher lesson, IYP-gallery style.
  run_cypher(cypher, ...)       raw READ-ONLY Cypher passthrough to a live
                                Neo4j server (optional `neo4j` driver), for
                                questions that outgrow the NL grammar.

An LLM can be plugged behind the `translator=` seam for phrasings the rule
grammar misses; the model only ever fills a typed Intent — it never writes
Cypher — so tag grounding and the read-only guarantee hold regardless of
what it emits.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

from .neo4j_store import _label

MAX_HOPS = 10          # Cypher variable-length bound shown for traversals
MAX_PATHS = 5          # flow paths reported per source/target pair


class QueryError(ValueError):
    """User-facing query failure: unknown/ambiguous tag, unparseable
    question. `hint` is safe to show verbatim in a UI."""

    def __init__(self, hint: str):
        super().__init__(hint)
        self.hint = hint


@dataclass
class Intent:
    """A typed graph question. `kind` is one of KINDS; which of the other
    fields apply depends on the kind (see _REQUIRES)."""
    kind: str
    tag: str | None = None            # downstream/upstream/neighbours/relief/info
    source: str | None = None         # path
    target: str | None = None         # path
    equipment_type: str | None = None  # list, or a filter on traversals


KINDS = ("downstream", "upstream", "neighbours", "path",
         "list", "count", "relief", "info")

_REQUIRES = {"downstream": ("tag",), "upstream": ("tag",),
             "neighbours": ("tag",), "relief": ("tag",), "info": ("tag",),
             "path": ("source", "target"), "list": ("equipment_type",),
             "count": ()}


@dataclass
class QueryResult:
    question: str
    intent: str
    cypher: str
    answer: str
    rows: list[dict] = field(default_factory=list)
    nodes: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# tag + equipment-type grounding
# --------------------------------------------------------------------------

def find_tags(text: str, known_tags: list[str]) -> list[str]:
    """Tags mentioned in `text`, resolved against the graph. Exact matches
    win; a partial like 'K-001A' resolves when exactly one tag ends with
    '-K-001A'; an ambiguous partial raises with the candidates."""
    by_upper = {t.upper(): t for t in known_tags}
    found: list[str] = []
    for token in re.findall(r"[A-Za-z0-9][\w-]*", text):
        upper = token.upper()
        if not any(c.isdigit() for c in upper):
            continue
        if upper in by_upper:
            if by_upper[upper] not in found:
                found.append(by_upper[upper])
            continue
        suffix = [t for u, t in by_upper.items() if u.endswith("-" + upper)]
        if len(suffix) == 1 and suffix[0] not in found:
            found.append(suffix[0])
        elif len(suffix) > 1:
            raise QueryError(
                f"'{token}' is ambiguous — did you mean one of: "
                f"{', '.join(sorted(suffix)[:8])}?")
    return found


# longest phrases first so 'relief valve' wins over 'valve'
_TYPE_SYNONYMS = [
    ("heat exchangers", "heat_exchanger"), ("heat exchanger", "heat_exchanger"),
    ("relief valves", "relief_valve"), ("relief valve", "relief_valve"),
    ("safety valves", "relief_valve"), ("safety valve", "relief_valve"),
    ("check valves", "check_valve"), ("check valve", "check_valve"),
    ("non-return valves", "check_valve"), ("non-return valve", "check_valve"),
    ("psvs", "relief_valve"), ("psv", "relief_valve"),
    ("compressors", "compressor"), ("compressor", "compressor"),
    ("instruments", "instrument"), ("instrument", "instrument"),
    ("vessels", "vessel"), ("vessel", "vessel"),
    ("drums", "vessel"), ("drum", "vessel"),
    ("tanks", "tank"), ("tank", "tank"),
    ("pumps", "pump"), ("pump", "pump"),
    ("valves", "valve"), ("valve", "valve"),
    ("lines", "line"), ("line", "line"),
]


def _find_type(question_lower: str) -> str | None:
    for phrase, etype in _TYPE_SYNONYMS:
        if re.search(rf"\b{re.escape(phrase)}\b", question_lower):
            return etype
    return None


# --------------------------------------------------------------------------
# NL -> Intent (deterministic rule grammar)
# --------------------------------------------------------------------------

def parse_question(text: str, graph: dict) -> Intent | None:
    """Parse a natural-language question into an Intent, or None when the
    grammar has no reading (a plugged-in LLM translator may still). Raises
    QueryError for a recognizable question with a broken tag."""
    q = text.lower().strip()
    tags = find_tags(text, [n["tag"] for n in graph["nodes"]])
    etype = _find_type(q)

    if re.search(r"\bhow many\b|\bcount\b", q):
        return Intent("count", equipment_type=etype)

    if len(tags) >= 2 and re.search(
            r"\bpath\b|\broute\b|\breach\b|\bbetween\b|\bget to\b|\bto\b", q):
        return Intent("path", source=tags[0], target=tags[1])

    def need_tag() -> str:
        if not tags:
            raise QueryError(
                "I couldn't find an equipment tag in that question — "
                "mention one like 2401-K-001A (partials like K-001A work).")
        return tags[0]

    if re.search(r"\bdownstream\b|where does .* (go|flow|end up)"
                 r"|what does .* feed\b", q):
        return Intent("downstream", tag=need_tag(), equipment_type=etype)

    if re.search(r"\bupstream\b|\bwhat (feeds|flows into|supplies)\b"
                 r"|\bsources? of\b|\bfed (from|by)\b", q):
        return Intent("upstream", tag=need_tag(), equipment_type=etype)

    if re.search(r"\brelief path\b|\bprotected\b|\boverpressure\b", q) or (
            tags and re.search(r"\brelief\b", q)):
        return Intent("relief", tag=need_tag())

    if re.search(r"\bconnected\b|\bneighbou?rs?\b|\badjacent\b"
                 r"|\battached\b|\baround\b", q) and (tags or not etype):
        return Intent("neighbours", tag=need_tag(), equipment_type=etype)

    if etype and (re.search(r"\b(list|show|all|which|what|every|find)\b", q)
                  or not tags):
        return Intent("list", equipment_type=etype)

    if tags and (re.search(r"\bwhat is\b|\btell me about\b|\binfo\b"
                           r"|\bdescribe\b|\babout\b", q)
                 or len(q.split()) <= 3):
        return Intent("info", tag=tags[0])

    return None


# --------------------------------------------------------------------------
# Intent -> equivalent Cypher (display / copy into Neo4j Browser)
# --------------------------------------------------------------------------

def _lit(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def to_cypher(intent: Intent) -> str:
    """The Cypher a user would paste into Neo4j Browser to ask the same
    question of the loaded graph (FLOWS_TO = verified flow direction,
    CONNECTED_TO = drawing order only)."""
    flt = f":{_label(intent.equipment_type)}" if intent.equipment_type else ""
    k, tag = intent.kind, intent.tag
    if k == "downstream":
        return (f"MATCH (s:PlantItem {{tag: {_lit(tag)}}})"
                f"-[:FLOWS_TO*1..{MAX_HOPS}]->(x{flt})\n"
                f"RETURN DISTINCT x.tag, x.equipment_type ORDER BY x.tag")
    if k == "upstream":
        return (f"MATCH (x{flt})-[:FLOWS_TO*1..{MAX_HOPS}]->"
                f"(s:PlantItem {{tag: {_lit(tag)}}})\n"
                f"RETURN DISTINCT x.tag, x.equipment_type ORDER BY x.tag")
    if k == "neighbours":
        return (f"MATCH (n:PlantItem {{tag: {_lit(tag)}}})"
                f"-[r:FLOWS_TO|CONNECTED_TO]-(m{flt})\n"
                f"RETURN type(r), coalesce(r.direction, 'known') AS "
                f"direction, m.tag, m.equipment_type")
    if k == "path":
        return (f"MATCH p = (a:PlantItem {{tag: {_lit(intent.source)}}})"
                f"-[:FLOWS_TO*1..{MAX_HOPS + 2}]->"
                f"(b:PlantItem {{tag: {_lit(intent.target)}}})\n"
                f"RETURN p LIMIT {MAX_PATHS}")
    if k == "list":
        return (f"MATCH (n:{_label(intent.equipment_type)})\n"
                f"RETURN n.tag, n.name, n.sheets ORDER BY n.tag")
    if k == "count":
        if intent.equipment_type:
            return (f"MATCH (n:{_label(intent.equipment_type)}) "
                    f"RETURN count(n)")
        return ("MATCH (n:PlantItem)\n"
                "RETURN n.equipment_type, count(*) ORDER BY count(*) DESC")
    if k == "relief":
        return (f"MATCH p = (s:PlantItem {{tag: {_lit(tag)}}})"
                f"-[:FLOWS_TO*1..{MAX_HOPS}]->(psv:ReliefValve)\n"
                f"RETURN p LIMIT {MAX_PATHS}")
    if k == "info":
        return (f"MATCH (n:PlantItem {{tag: {_lit(tag)}}})\n"
                f"OPTIONAL MATCH (n)-[r:FLOWS_TO|CONNECTED_TO]-(m)\n"
                f"RETURN n, r, m")
    raise QueryError(f"unknown intent kind '{k}'")


# --------------------------------------------------------------------------
# execution against the in-memory graph
# --------------------------------------------------------------------------

class GraphQuery:
    """Bind the equipment-level graph once, answer many questions.

    `translator`, if given, is any object with
    `translate(question: str) -> Intent | None` — consulted only when the
    rule grammar fails; its tag fields are re-grounded before execution.
    """

    def __init__(self, graph: dict, translator=None):
        # both imports resolve inside the package; TopologyReasoner is the
        # single owner of direction-aware traversal semantics (MDL-3)
        from hazop.s3_are.reasoner.topology import TopologyReasoner

        from .adapter import to_l3_topology
        self.graph = graph
        self.reasoner = TopologyReasoner(to_l3_topology(graph))
        self.node_by_tag = {n["tag"]: n for n in graph["nodes"]}
        self.translator = translator

    # -- public API --------------------------------------------------------

    def ask(self, question: str) -> QueryResult:
        intent = parse_question(question, self.graph)
        if intent is None and self.translator is not None:
            intent = self.translator.translate(question)
            if intent is not None:
                intent = self._reground(intent)
        if intent is None:
            raise QueryError(
                "I couldn't map that to a graph question. Try one of the "
                "examples, e.g. 'what is downstream of 2401-K-001A?' or "
                "'list all relief valves'.")
        return self.run(intent, question=question)

    def run(self, intent: Intent, question: str = "") -> QueryResult:
        for field_name in _REQUIRES.get(intent.kind, ()):
            value = getattr(intent, field_name)
            if not value:
                raise QueryError(
                    f"'{intent.kind}' needs {field_name} — none found.")
            if field_name != "equipment_type" and \
                    value not in self.node_by_tag:
                raise QueryError(f"tag '{value}' is not in the graph.")
        runner = getattr(self, f"_run_{intent.kind}")
        result = runner(intent)
        result.question = question or result.question
        return result

    # -- helpers -------------------------------------------------------

    def _reground(self, intent: Intent) -> Intent:
        """Resolve possibly-partial tag mentions from a translator against
        the real graph; QueryError propagates for unknowns."""
        known = [n["tag"] for n in self.graph["nodes"]]
        for field_name in ("tag", "source", "target"):
            value = getattr(intent, field_name)
            if value and value not in self.node_by_tag:
                hits = find_tags(value, known)
                if not hits:
                    raise QueryError(f"tag '{value}' is not in the graph.")
                setattr(intent, field_name, hits[0])
        return intent

    def _node_payload(self, tag: str, **extra) -> dict:
        n = self.node_by_tag[tag]
        return {"tag": tag, "type": n["equipment_type"],
                "label": _label(n["equipment_type"]), "name": n["name"],
                "sheets": n["attributes"].get("sheets", []), **extra}

    def _edge_payload(self, e: dict) -> dict:
        direction = e["attributes"].get("direction", "unknown")
        return {"source": e["source"], "target": e["target"],
                "rel": "FLOWS_TO" if direction == "known" else "CONNECTED_TO",
                "direction": direction,
                "lines": e["attributes"].get("line_numbers", [])}

    def _induced_edges(self, tags: set[str]) -> list[dict]:
        return [self._edge_payload(e) for e in self.graph["edges"]
                if e["source"] in tags and e["target"] in tags]

    def _matches_filter(self, tag: str, etype: str | None) -> bool:
        return etype is None or \
            self.node_by_tag[tag]["equipment_type"] == etype

    def _result(self, intent: Intent, answer: str, rows: list[dict],
                node_tags: set[str], edges: list[dict] | None = None
                ) -> QueryResult:
        return QueryResult(
            question="", intent=intent.kind, cypher=to_cypher(intent),
            answer=answer, rows=rows,
            nodes=[self._node_payload(t) for t in sorted(node_tags)],
            edges=self._induced_edges(node_tags) if edges is None else edges)

    # -- intent runners ------------------------------------------------

    def _trace(self, intent: Intent, forward: bool) -> QueryResult:
        detail = (self.reasoner.downstream_detail(intent.tag) if forward
                  else self.reasoner.upstream_detail(intent.tag))
        hits = {t: ok for t, ok in detail.items()
                if self._matches_filter(t, intent.equipment_type)}
        rows = [{"tag": t, "equipment_type":
                 self.node_by_tag[t]["equipment_type"],
                 "flow": "verified" if ok else "unverified"}
                for t, ok in sorted(hits.items(),
                                    key=lambda kv: (not kv[1], kv[0]))]
        n_verified = sum(1 for ok in hits.values() if ok)
        word = "downstream of" if forward else "upstream of"
        flt = (f" {intent.equipment_type.replace('_', ' ')}(s)"
               if intent.equipment_type else " items")
        answer = (f"{len(hits)}{flt} {word} {intent.tag} — {n_verified} "
                  f"over fully verified flow, "
                  f"{len(hits) - n_verified} reachable only across "
                  f"unverified connections.")
        return self._result(intent, answer, rows,
                            {intent.tag, *detail})

    def _run_downstream(self, intent):
        return self._trace(intent, forward=True)

    def _run_upstream(self, intent):
        return self._trace(intent, forward=False)

    def _run_neighbours(self, intent: Intent) -> QueryResult:
        rows, tags = [], {intent.tag}
        for e in self.graph["edges"]:
            if intent.tag not in (e["source"], e["target"]):
                continue
            other = e["target"] if e["source"] == intent.tag else e["source"]
            if not self._matches_filter(other, intent.equipment_type):
                continue
            direction = e["attributes"].get("direction", "unknown")
            if direction == "known":
                flow = ("out" if e["source"] == intent.tag else "in")
                flow = f"FLOWS_TO ({flow})"
            else:
                flow = f"CONNECTED_TO ({direction})"
            tags.add(other)
            rows.append({"tag": other, "equipment_type":
                         self.node_by_tag[other]["equipment_type"],
                         "relationship": flow,
                         "lines": ", ".join(
                             e["attributes"].get("line_numbers", []))})
        answer = (f"{len(rows)} direct connections at {intent.tag} "
                  f"(FLOWS_TO = verified flow direction, CONNECTED_TO = "
                  f"drawing order only).")
        return self._result(intent, answer, sorted(
            rows, key=lambda r: r["tag"]), tags)

    def _run_path(self, intent: Intent) -> QueryResult:
        paths = self.reasoner.flow_paths(intent.source, intent.target,
                                         cutoff=MAX_HOPS + 2)
        paths = sorted(paths, key=len)[:MAX_PATHS]
        verified = {tuple(p) for p in self.reasoner.flow_paths(
            intent.source, intent.target, cutoff=MAX_HOPS + 2,
            verified_only=True)}
        rows = [{"hops": len(p) - 1, "flow": "verified"
                 if tuple(p) in verified else "unverified",
                 "path": " → ".join(p)} for p in paths]
        tags = {t for p in paths for t in p} or {intent.source, intent.target}
        if paths:
            answer = (f"{len(paths)} flow path(s) from {intent.source} to "
                      f"{intent.target}; shortest is {rows[0]['hops']} hops"
                      f" ({rows[0]['flow']}).")
        else:
            answer = (f"No flow path from {intent.source} to "
                      f"{intent.target} within {MAX_HOPS + 2} hops.")
        return self._result(intent, answer, rows, tags)

    def _run_list(self, intent: Intent) -> QueryResult:
        nodes = [n for n in self.graph["nodes"]
                 if n["equipment_type"] == intent.equipment_type]
        rows = [{"tag": n["tag"], "name": n["name"],
                 "sheets": ", ".join(map(str,
                                         n["attributes"].get("sheets", [])))}
                for n in sorted(nodes, key=lambda n: n["tag"])]
        pretty = intent.equipment_type.replace("_", " ")
        answer = f"{len(nodes)} {pretty}(s) in the plant graph."
        return self._result(intent, answer, rows,
                            {n["tag"] for n in nodes})

    def _run_count(self, intent: Intent) -> QueryResult:
        counts: dict[str, int] = {}
        for n in self.graph["nodes"]:
            counts[n["equipment_type"]] = counts.get(
                n["equipment_type"], 0) + 1
        if intent.equipment_type:
            k = counts.get(intent.equipment_type, 0)
            pretty = intent.equipment_type.replace("_", " ")
            return self._result(
                intent, f"{k} {pretty}(s) in the plant graph.",
                [{"equipment_type": intent.equipment_type, "count": k}],
                set())
        rows = [{"equipment_type": t, "count": c} for t, c in
                sorted(counts.items(), key=lambda kv: -kv[1])]
        answer = (f"{len(self.graph['nodes'])} equipment items across "
                  f"{len(counts)} types.")
        return self._result(intent, answer, rows, set())

    def _run_relief(self, intent: Intent) -> QueryResult:
        protected = self.reasoner.has_relief_path(intent.tag)
        drawn = self.reasoner.has_relief_path(intent.tag,
                                              verified_only=False)
        downstream = self.reasoner.downstream_detail(intent.tag)
        psvs = [t for t in downstream
                if self.node_by_tag[t]["equipment_type"] == "relief_valve"]
        rows = [{"relief_valve": t,
                 "flow": "verified" if downstream[t] else "unverified"}
                for t in sorted(psvs)]
        tags = {intent.tag}
        for psv in psvs:
            for p in sorted(self.reasoner.flow_paths(
                    intent.tag, psv, cutoff=MAX_HOPS), key=len)[:1]:
                tags.update(p)
        if protected:
            answer = (f"Yes — {intent.tag} has an unblockable relief path "
                      f"over verified flow directions.")
        elif drawn:
            answer = (f"Drawn but not verified — a relief path exists on "
                      f"the drawing, but at least one hop's flow direction "
                      f"is unverified.")
        elif psvs:
            answer = (f"Not credited — relief valve(s) are reachable "
                      f"downstream of {intent.tag}, but every path crosses "
                      f"equipment that can block relief flow (closable "
                      f"valve, pump or compressor), so it fails the "
                      f"blocked-outlet case. Flag for the HAZOP team.")
        else:
            answer = (f"No relief valve anywhere downstream of "
                      f"{intent.tag} — flag for the HAZOP team.")
        return self._result(intent, answer, rows, tags)

    def _run_info(self, intent: Intent) -> QueryResult:
        n = self.node_by_tag[intent.tag]
        rows = [{"property": "tag", "value": n["tag"]},
                {"property": "equipment_type", "value": n["equipment_type"]},
                {"property": "name", "value": n["name"]},
                {"property": "detection_confidence",
                 "value": n["detection_confidence"]}]
        for key, value in n.get("attributes", {}).items():
            if value not in (None, "", []):
                rows.append({"property": key, "value": value})
        tags = {intent.tag}
        for e in self.graph["edges"]:
            if intent.tag == e["source"]:
                tags.add(e["target"])
            elif intent.tag == e["target"]:
                tags.add(e["source"])
        answer = (f"{intent.tag}: {n['equipment_type'].replace('_', ' ')} "
                  f"with {len(tags) - 1} direct connections.")
        return self._result(intent, answer, rows, tags)


# --------------------------------------------------------------------------
# example gallery (IYP documentation/gallery.md equivalent)
# --------------------------------------------------------------------------

def examples(graph: dict) -> list[dict]:
    """Clickable example questions built from tags that actually exist in
    this graph, each paired with its equivalent Cypher."""
    def first_tag(etype: str) -> str | None:
        for n in graph["nodes"]:
            if n["equipment_type"] == etype:
                return n["tag"]
        return None

    compressor = first_tag("compressor") or first_tag("pump")
    vessel = first_tag("vessel") or first_tag("tank")
    out = []

    def add(question: str):
        intent = parse_question(question, graph)
        if intent is not None:
            out.append({"question": question, "cypher": to_cypher(intent)})

    if compressor:
        add(f"What is downstream of {compressor}?")
    if vessel:
        add(f"What feeds {vessel}?")
        add(f"Does {vessel} have a relief path?")
        add(f"What is connected to {vessel}?")
    if compressor and vessel:
        add(f"Show the path from {compressor} to {vessel}")
    add("List all relief valves")
    add("Which vessels are downstream of "
        f"{compressor}?" if compressor else "List all vessels")
    add("How many of each equipment type?")
    return out


# --------------------------------------------------------------------------
# raw Cypher passthrough (live Neo4j, read-only)
# --------------------------------------------------------------------------

_WRITE_CLAUSE = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|FOREACH|LOAD\s+CSV"
    r"|CALL\s*\{|apoc\.)\b", re.IGNORECASE)


def run_cypher(cypher: str, uri: str = "bolt://localhost:7687",
               user: str = "neo4j", password: str | None = None,
               database: str = "neo4j", driver=None, limit: int = 500
               ) -> dict:
    """Execute a read-only Cypher query against a live server and return
    {"columns", "rows", "nodes", "edges"} — rows for the table view, any
    nodes/relationships/paths in the result flattened for the graph view.

    Write clauses are rejected before anything touches the server; the
    `neo4j` driver import is deferred (optional extra), and `driver` is
    injectable for tests.
    """
    if _WRITE_CLAUSE.search(cypher):
        raise QueryError("read-only: write clauses (CREATE/MERGE/SET/"
                         "DELETE/...) are not allowed from the explorer.")
    owns_driver = driver is None
    if driver is None:
        from neo4j import GraphDatabase  # optional dependency
        driver = GraphDatabase.driver(uri, auth=(user, password))

    nodes: dict[str, dict] = {}
    edges: dict[tuple, dict] = {}

    def harvest(value):
        # duck-typed so fake drivers in tests work: Node has labels+items,
        # Relationship has type+nodes, Path has nodes+relationships
        if hasattr(value, "nodes") and hasattr(value, "relationships"):
            for n in value.nodes:
                harvest(n)
            for r in value.relationships:
                harvest(r)
            return " → ".join(_tag(n) for n in value.nodes)
        if hasattr(value, "type") and hasattr(value, "start_node"):
            a, b = _tag(value.start_node), _tag(value.end_node)
            harvest(value.start_node)
            harvest(value.end_node)
            edges[(a, b, value.type)] = {
                "source": a, "target": b, "rel": value.type,
                "direction": dict(value).get("direction", "known")}
            return f"{a}-[:{value.type}]->{b}"
        if hasattr(value, "labels"):
            props = dict(value)
            tag = _tag(value)
            labels = [l for l in value.labels if l != "PlantItem"]
            nodes[tag] = {"tag": tag,
                          "type": props.get("equipment_type", "?"),
                          "label": labels[0] if labels else "PlantItem",
                          "name": props.get("name", tag),
                          "sheets": props.get("sheets", [])}
            return props
        return value

    def _tag(node) -> str:
        return dict(node).get("tag", str(getattr(node, "element_id", "?")))

    try:
        with driver.session(database=database) as session:
            result = session.run(cypher)
            columns = list(result.keys())
            rows = []
            for record in result:
                if len(rows) >= limit:
                    break
                rows.append({col: harvest(record[col]) for col in columns})
        return {"columns": columns, "rows": rows,
                "nodes": list(nodes.values()), "edges": list(edges.values())}
    finally:
        if owns_driver:
            driver.close()


# --------------------------------------------------------------------------
# optional LLM translator (DDR-06 seam; fills an Intent, never writes Cypher)
# --------------------------------------------------------------------------

_INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "tag": {"type": ["string", "null"]},
        "source": {"type": ["string", "null"]},
        "target": {"type": ["string", "null"]},
        "equipment_type": {"type": ["string", "null"]},
    },
    "required": ["kind", "tag", "source", "target", "equipment_type"],
    "additionalProperties": False,
}

_TRANSLATOR_PROMPT = """\
You translate a plant engineer's question about a process plant graph into
one typed query intent. Kinds: downstream/upstream (flow tracing from a
tag), neighbours (direct connections of a tag), path (source tag to target
tag), list (all items of an equipment type), count (tally by type), relief
(does the tag have a pressure-relief path), info (describe one tag).
Copy tag mentions verbatim from the question — do not invent or complete
tags. equipment_type must be one of: {types}. Unused fields are null.
"""


class AnthropicTranslator:
    """NL -> Intent via the Anthropic API — the cloud branch of DDR-06,
    same lazy-import/injectable-client pattern as AnthropicLLM. The model
    fills a typed Intent; GraphQuery re-grounds every tag it returns, so a
    hallucinated tag fails closed with a QueryError."""

    MODEL = "claude-opus-4-8"

    def __init__(self, equipment_types: list[str],
                 model: str = MODEL, client=None):
        if client is None:
            import anthropic  # deferred: only the real client needs it
            client = anthropic.Anthropic()
        self.client = client
        self.model = model
        self.system = _TRANSLATOR_PROMPT.format(
            types=", ".join(sorted(equipment_types)))

    def translate(self, question: str) -> Intent | None:
        response = self.client.messages.create(
            model=self.model, max_tokens=1000, system=self.system,
            output_config={"format": {"type": "json_schema",
                                      "schema": _INTENT_SCHEMA}},
            messages=[{"role": "user", "content": question}])
        if response.stop_reason == "refusal":
            return None
        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)
        if data.get("kind") not in KINDS:
            return None
        return Intent(kind=data["kind"], tag=data.get("tag"),
                      source=data.get("source"), target=data.get("target"),
                      equipment_type=data.get("equipment_type"))
