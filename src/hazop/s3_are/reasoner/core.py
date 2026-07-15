"""
core.py — The AI Reasoner orchestration (stage 3).

Pipeline per study node:
  1. Generate full guideword x parameter deviation matrix   (FR-03-01 / FR-ARE-1)
  2. For each deviation: retrieve evidence from KB           (FR-03-02 / FR-ARE-2, RAG)
  3. Generate candidate causes/consequences/safeguards       (FR-03-03 / FR-ARE-3/4)
  4. Stage A critic: ground every referenced tag against
     topology; reject findings citing non-existent tags      (FR-ARE-9 / MDL-10 hard gate)
  5. Stage B critic (optional seam): judge every claim
     against its cited evidence — contradicted -> refused,
     unsupporting citations stripped, confidence re-composed (DDR-02 / MDL-11)
  6. Enrich safeguards with topology-derived facts
     (relief path present, check valve present)              (deterministic, MDL-3)
  7. Attach confidence + evidence, set provenance            (NFR-S-02 / FR-ARE-5, AR-1)
  8. Emit worksheet rows; risk ranking left blank            (FR-ARE-6)

Runs today end-to-end on mock topology + mock retriever + stub LLM.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .evidence_critic import EvidenceCriticInterface, EvidenceVerdict
from .guidewords import Deviation, Guideword, deviations_for_parameters
from .llm import LLMInterface, GeneratedFinding
from .schema import RetrieverInterface, StudyNode, TopologyGraph, Parameter
from .topology import TopologyReasoner
from .worksheet import Finding, RejectedFinding, WorksheetRow, Provenance

# confidence floor for findings whose citations all failed Stage B — same
# floor the generators use for findings that never cited evidence
_UNSUPPORTED_CONFIDENCE = 0.30


class AIReasoner:
    def __init__(
        self,
        topology: TopologyGraph,
        retriever: RetrieverInterface,
        llm: LLMInterface,
        grounding_required: bool = True,
        evidence_critic: EvidenceCriticInterface | None = None,
        max_workers: int = 1,
    ):
        self.topology = topology
        self.topo_reasoner = TopologyReasoner(topology)
        self.retriever = retriever
        self.llm = llm
        self.grounding_required = grounding_required
        self.evidence_critic = evidence_critic
        # parallel fan-out per guideword x parameter (DDR-11) toward the
        # <=10 s P95/deviation live-session target (MDL-12). Deviations are
        # independent by construction; retriever/LLM/critic implementations
        # behind the seams must be thread-safe (read-only index, API client)
        self.max_workers = max_workers

    # ---- public API ------------------------------------------------------

    def analyze_node(self, node: StudyNode) -> list[WorksheetRow]:
        deviations = deviations_for_parameters(node.parameters)
        if self.max_workers <= 1:
            return [self._analyze_deviation(node, d) for d in deviations]
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            # map() keeps worksheet order identical to the serial run and
            # re-raises the first failure, matching serial semantics
            return list(pool.map(
                lambda d: self._analyze_deviation(node, d), deviations))

    def analyze_deviation(self, node: StudyNode,
                          deviation: Deviation) -> WorksheetRow:
        """One deviation through the full pipeline. Public so the latency
        harness (MDL-12) can time the per-deviation unit the spec's P95
        target is written against."""
        return self._analyze_deviation(node, deviation)

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

        # Stage B critic (DDR-02): claims vs their cited evidence
        causes = self._evidence_checked(causes, "cause", rejected)
        consequences = self._evidence_checked(consequences, "consequence",
                                              rejected)
        safeguards = self._evidence_checked(safeguards, "safeguard", rejected)

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

    def _evidence_checked(self, findings: list[Finding], kind: str,
                          rejected: list[RejectedFinding]) -> list[Finding]:
        """
        Stage B critic (DDR-02 / MDL-11). For every finding that cites
        evidence: any contradicted citation refuses the whole finding
        (audit-recorded, never fabricated around); citations judged
        insufficient are stripped so no suggestion cites evidence that
        does not support it; confidence is re-composed from the surviving
        citations and can only fall. Findings without citations pass
        through — they are already labelled unsupported inferences.
        """
        if self.evidence_critic is None:
            return findings
        out: list[Finding] = []
        for f in findings:
            if not f.evidence:
                out.append(f)
                continue
            checks = self.evidence_critic.check_claim(f.text, f.evidence)
            f.evidence_checks = [c.to_dict() for c in checks]
            contradicted = [c.evidence_id for c in checks
                            if c.verdict == EvidenceVerdict.CONTRADICTED]
            if contradicted:
                rejected.append(RejectedFinding(
                    kind=kind,
                    text=f.text,
                    confidence=f.confidence,
                    reason="evidence_contradicted",
                    failed_evidence=contradicted,
                ))
                continue
            supported_ids = {c.evidence_id for c in checks
                             if c.verdict == EvidenceVerdict.SUPPORTED}
            surviving = [e for e in f.evidence
                         if e.source_id in supported_ids]
            if len(surviving) < len(f.evidence):
                f.evidence = surviving
                recomposed = (0.4 + 0.15 * len(surviving) if surviving
                              else _UNSUPPORTED_CONFIDENCE)
                f.confidence = round(min(f.confidence, recomposed), 3)
            out.append(f)
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
