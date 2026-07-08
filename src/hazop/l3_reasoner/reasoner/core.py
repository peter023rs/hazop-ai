"""
core.py — The AI Reasoner orchestration (stage 3).

Pipeline per study node:
  1. Generate full guideword x parameter deviation matrix   (FR-03-01 / FR-ARE-1)
  2. For each deviation: retrieve evidence from KB           (FR-03-02 / FR-ARE-2, RAG)
  3. Generate candidate causes/consequences/safeguards       (FR-03-03 / FR-ARE-3/4)
  4. Ground every referenced tag against topology; reject
     findings that cite non-existent tags                    (FR-ARE-9 / MDL-10 hard gate)
  5. Enrich safeguards with topology-derived facts
     (relief path present, check valve present)              (deterministic, MDL-3)
  6. Attach confidence + evidence, set provenance            (NFR-S-02 / FR-ARE-5, AR-1)
  7. Emit worksheet rows; risk ranking left blank            (FR-ARE-6)

Runs today end-to-end on mock topology + mock retriever + stub LLM.
"""

from __future__ import annotations

from .guidewords import Deviation, Guideword, deviations_for_parameters
from .llm import LLMInterface, GeneratedFinding
from .schema import RetrieverInterface, StudyNode, TopologyGraph, Parameter
from .topology import TopologyReasoner
from .worksheet import Finding, RejectedFinding, WorksheetRow, Provenance


class AIReasoner:
    def __init__(
        self,
        topology: TopologyGraph,
        retriever: RetrieverInterface,
        llm: LLMInterface,
        grounding_required: bool = True,
    ):
        self.topology = topology
        self.topo_reasoner = TopologyReasoner(topology)
        self.retriever = retriever
        self.llm = llm
        self.grounding_required = grounding_required

    # ---- public API ------------------------------------------------------

    def analyze_node(self, node: StudyNode) -> list[WorksheetRow]:
        rows: list[WorksheetRow] = []
        for deviation in deviations_for_parameters(node.parameters):
            rows.append(self._analyze_deviation(node, deviation))
        return rows

    # ---- per-deviation pipeline -----------------------------------------

    def _analyze_deviation(self, node: StudyNode, deviation: Deviation) -> WorksheetRow:
        query = f"{deviation.guideword.value} {deviation.parameter.value}"
        evidence = self.retriever.retrieve(query, k=5, filters={
            "guideword": deviation.guideword.name,
            "parameter": deviation.parameter.value,
        })

        node_context = {
            "node_id": node.node_id,
            "equipment_tags": node.equipment_tags,
            "design_intent": node.design_intent,
        }
        generated = self.llm.generate_findings(deviation, node_context, evidence)

        ev_by_id = {e.source_id: e for e in evidence}

        rejected: list[RejectedFinding] = []
        causes = self._grounded(generated.causes, ev_by_id, "cause", rejected)
        consequences = self._grounded(generated.consequences, ev_by_id,
                                      "consequence", rejected)
        safeguards = self._grounded(generated.safeguards, ev_by_id,
                                    "safeguard", rejected)

        # deterministic topology-derived safeguards (MDL-3)
        safeguards.extend(self._topology_safeguards(node, deviation))

        return WorksheetRow(
            node_id=node.node_id,
            deviation=deviation,
            causes=causes,
            consequences=consequences,
            safeguards=safeguards,
            provenance=Provenance.AI_GENERATED,
            rejected_findings=rejected,
        )

    # ---- helpers ---------------------------------------------------------

    def _grounded(self, generated: list[GeneratedFinding], ev_by_id: dict,
                  kind: str, rejected: list[RejectedFinding]) -> list[Finding]:
        """
        Apply the grounding gate and wrap survivors as Findings with evidence.
        Rejects are recorded on `rejected`, not silently dropped — the audit
        trail is what the hallucination-rate evaluation (MDL-10) counts.
        """
        out: list[Finding] = []
        for g in generated:
            if self.grounding_required and g.referenced_tags:
                _, invalid = self.topo_reasoner.validate_tags(g.referenced_tags)
                if invalid:
                    # Hard gate (FR-ARE-9): a finding citing a tag not in the
                    # topology is a hallucination -> excluded from the
                    # worksheet body, kept in the audit record.
                    rejected.append(RejectedFinding(
                        kind=kind,
                        text=g.text,
                        invalid_tags=invalid,
                        confidence=g.confidence,
                    ))
                    continue
            evidence = [ev_by_id[eid] for eid in g.evidence_ids if eid in ev_by_id]
            out.append(Finding(
                text=g.text,
                confidence=g.confidence,
                evidence=evidence,
                provenance=Provenance.AI_GENERATED,
            ))
        return out

    def _topology_safeguards(self, node: StudyNode, deviation: Deviation) -> list[Finding]:
        """
        Facts derived purely from graph structure — high confidence, evidence =
        the topology itself. Distinguishes real safeguards present in the design.
        """
        out: list[Finding] = []
        param, gw = deviation.parameter, deviation.guideword

        # Over-pressure: is there actually a relief path in the design?
        # Verified flow directions carry the 0.99 claim; a relief path that
        # is merely drawn (direction unverified) is flagged for confirmation
        # rather than credited or dropped (NFR-S-01).
        if param == Parameter.PRESSURE and gw == Guideword.MORE:
            for tag in node.equipment_tags:
                if self.topo_reasoner.has_relief_path(tag):
                    out.append(Finding(
                        text=f"Unblockable relief path present downstream of "
                             f"{tag} (topology-confirmed; no closable valve, "
                             f"pump or compressor between them).",
                        confidence=0.99,
                        evidence=[],
                        provenance=Provenance.AI_GENERATED,
                        topology_grounded=True,
                    ))
                    break
            else:
                for tag in node.equipment_tags:
                    if self.topo_reasoner.has_relief_path(
                            tag, verified_only=False):
                        out.append(Finding(
                            text=f"Relief path drawn downstream of {tag}, "
                                 f"but its flow direction is not verified "
                                 f"from the drawing — confirm relief "
                                 f"line-up before crediting.",
                            confidence=0.60,
                            evidence=[],
                            provenance=Provenance.AI_GENERATED,
                            topology_grounded=True,
                        ))
                        break

        # Reverse flow: is there a check valve on the path? equipment_tags
        # carries no ordering contract, so try every pair — flow direction
        # comes from the topology, not from the tag list order. Verified
        # paths first at 0.99; unverified-direction paths as flagged
        # candidates at 0.60.
        if param == Parameter.FLOW and gw == Guideword.REVERSE:
            seen_cvs: set[str] = set()
            for verified, conf, note in (
                    (True, 0.99, "topology-confirmed reverse-flow safeguard"),
                    (False, 0.60, "on a path whose flow direction is not "
                                  "verified — confirm before crediting")):
                for src in node.equipment_tags:
                    for dst in node.equipment_tags:
                        if src == dst:
                            continue
                        for cv in self.topo_reasoner.check_valves_between(
                                src, dst, verified_only=verified):
                            if cv in seen_cvs:
                                continue
                            seen_cvs.add(cv)
                            out.append(Finding(
                                text=f"Check valve {cv} on flow path "
                                     f"({note}).",
                                confidence=conf,
                                evidence=[],
                                provenance=Provenance.AI_GENERATED,
                                topology_grounded=True,
                            ))
        return out
