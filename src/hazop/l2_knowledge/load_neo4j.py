"""
load_neo4j.py — persist the 2401 plant graph to Neo4j (handoff item 4).

    python3 load_neo4j.py                  # contract stage 1 output and write
                                           # output/plant_graph_2401.cypher
    python3 load_neo4j.py --load           # ALSO push to a live server
        [--uri bolt://localhost:7687] [--user neo4j]
        [--password ...] [--database neo4j]

The .cypher file needs nothing installed — paste it into Neo4j Browser or
pipe it to cypher-shell. --load needs `pip install neo4j` and a running
server.
"""

import argparse
import os
from pathlib import Path

from hazop.l2_knowledge.plant_graph import build_equipment_graph, load_plant_model
from hazop.l2_knowledge.plant_graph.neo4j_store import load, to_cypher

# Runtime data ships with the repo under data/ (override with HAZOP_DATA,
# e.g. the Docker image sets HAZOP_DATA=/app/data).
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]               # .../hazop-ai
DATA_DIR = Path(os.environ.get("HAZOP_DATA", REPO_ROOT / "data"))
L1_MODEL = DATA_DIR / "l1_output" / "plant_model_dexpi.json"
OUT_CYPHER = REPO_ROOT / "outputs" / "plant_graph_2401.cypher"

SAMPLE_QUERIES = """\
// verified flow downstream of the first-stage compressor
MATCH (k:PlantItem {tag:'2401-K-001A'})-[:FLOWS_TO*1..6]->(x)
RETURN DISTINCT x.tag, labels(x);

// every relief valve on the unit and which sheet it lives on
MATCH (psv:ReliefValve) RETURN psv.tag, psv.sheets;

// full neighbourhood of a vessel, regardless of direction confidence
MATCH (n:PlantItem {tag:'2401-V-001'})-[r:FLOWS_TO|CONNECTED_TO]-(m)
RETURN type(r), coalesce(r.direction,'known') AS direction, m.tag;

// the whole unit at a glance
MATCH (n:PlantItem) RETURN n.equipment_type, count(*) ORDER BY count(*) DESC;"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--load", action="store_true",
                        help="push to a live Neo4j server as well")
    parser.add_argument("--uri", default="bolt://localhost:7687")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--password", default=None)
    parser.add_argument("--database", default="neo4j")
    args = parser.parse_args()

    graph = build_equipment_graph(load_plant_model(L1_MODEL))
    stats = graph["stats"]
    print(f"contracted 2401 drawing: {stats['terminals']} nodes, "
          f"{stats['connections']} connections "
          f"({stats['directed_connections']} with verified direction)")

    OUT_CYPHER.parent.mkdir(exist_ok=True)
    OUT_CYPHER.write_text(to_cypher(graph), encoding="utf-8")
    print(f"cypher script -> {OUT_CYPHER}")

    if args.load:
        counts = load(graph, uri=args.uri, user=args.user,
                      password=args.password, database=args.database)
        print(f"loaded into {args.uri}: {counts['nodes']} nodes, "
              f"{counts['relationships']} relationships")
        print("\ntry these in Neo4j Browser (http://localhost:7474):\n")
        print(SAMPLE_QUERIES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
