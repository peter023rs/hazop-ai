"""
evidence_critic.py — Stage B of the two-stage critic (DDR-02 / MDL-11).

Stage A (core._grounded) validates *tags* deterministically against the
topology graph. Stage B validates *claims* against their cited evidence:
every (finding, cited excerpt) pair gets an NLI-style judgment —
supported / contradicted / insufficient — and the reasoner acts on it:

  * any CONTRADICTED citation  -> the finding is refused: excluded from
    the worksheet body, kept as a RejectedFinding audit record (refuse,
    never fabricate — and never silently drop);
  * INSUFFICIENT citations are stripped from the finding — a suggestion
    may not cite evidence that does not support it (the MDL-11
    fabrication-rate definition); a finding left with no surviving
    citation is relabelled an unsupported inference (NFR-S-01: flagged,
    not suppressed) and its confidence falls to the unsupported floor;
  * confidence is re-composed from the citations that survive (DDR-05 —
    composed from signals, never model self-report), and can only drop.

Two implementations behind one interface:

  * LexicalEvidenceCritic — deterministic, offline: content-word
    containment between claim and excerpt. It can establish lexical
    support or abstain (insufficient); it can NOT detect contradiction —
    that needs real NLI, i.e. the LLM critic. A test double shaped like
    the real thing, same role StubLLM plays for generation.
  * AnthropicEvidenceCritic — the real Stage B: a *different* model and
    prompt regime than the generator (breaking correlated error, the
    DDR-02 mitigation), constrained to judge ONLY from the excerpt text,
    structured output, abstention -> insufficient on refusal.

LocalEvidenceCritic is the self-hosted variant of the real pass (the
on-prem branch of DDR-06): same NLI framing over an OpenAI-compatible
server, defaulting to a *smaller, different* model than the local
generator so the correlated-error break survives the move on-prem.
"""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from .schema import RetrievedEvidence


class EvidenceVerdict(str, Enum):
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    INSUFFICIENT = "insufficient"


@dataclass
class ClaimCheck:
    """One (claim, cited excerpt) judgment — the Stage B audit unit."""
    evidence_id: str
    verdict: EvidenceVerdict
    rationale: str = ""

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "verdict": self.verdict.value,
            "rationale": self.rationale,
        }


class EvidenceCriticInterface(ABC):
    @abstractmethod
    def check_claim(
        self,
        claim_text: str,
        evidence: list[RetrievedEvidence],
    ) -> list[ClaimCheck]:
        """One ClaimCheck per evidence item, in order."""
        ...


# --------------------------------------------------------------------------
# Deterministic offline critic
# --------------------------------------------------------------------------

_STOP = frozenset(
    "a an and are as at be but by for from has have if in into is it its "
    "may of on or per possible potential such that the their then this to "
    "was were will with".split())


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower())
            if len(w) > 2 and w not in _STOP}


class LexicalEvidenceCritic(EvidenceCriticInterface):
    """Offline stand-in: a citation lexically supports a claim when at
    least `support_threshold` of the claim's content words appear in the
    cited excerpt. Anything weaker is an abstention (insufficient) — this
    critic never claims contradiction, because token overlap cannot see
    negation. NOT a model; a deterministic double shaped like one."""

    def __init__(self, support_threshold: float = 0.5):
        self.support_threshold = support_threshold

    def check_claim(self, claim_text, evidence):
        claim = _content_words(claim_text)
        out = []
        for e in evidence:
            excerpt = _content_words(e.snippet)
            overlap = (len(claim & excerpt) / len(claim)) if claim else 0.0
            verdict = (EvidenceVerdict.SUPPORTED
                       if overlap >= self.support_threshold
                       else EvidenceVerdict.INSUFFICIENT)
            out.append(ClaimCheck(
                evidence_id=e.source_id,
                verdict=verdict,
                rationale=f"{overlap:.0%} of claim content words appear "
                          f"in the cited excerpt",
            ))
        return out


# --------------------------------------------------------------------------
# Real model critic (Anthropic API)
# --------------------------------------------------------------------------

_CHECKS_SCHEMA = {
    "type": "object",
    "properties": {
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "evidence_id": {"type": "string"},
                    "verdict": {"type": "string",
                                "enum": ["supported", "contradicted",
                                         "insufficient"]},
                    "rationale": {"type": "string"},
                },
                "required": ["evidence_id", "verdict", "rationale"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["checks"],
    "additionalProperties": False,
}

_CRITIC_SYSTEM_PROMPT = """\
You are an evidence auditor for HAZOP worksheet suggestions. You are NOT
the author of the claim and you must not improve or complete it.

For each cited evidence excerpt, judge whether the excerpt supports the
claim, judging ONLY from the excerpt text:
- "supported": the excerpt states or directly entails the claim's content.
- "contradicted": the excerpt states the opposite of the claim.
- "insufficient": the excerpt neither supports nor contradicts it.

Rules:
- Use no outside knowledge. If you would need any, answer "insufficient".
- Judge each excerpt independently; return one check per excerpt, with a
  one-sentence rationale quoting the deciding words.
- When unsure, abstain with "insufficient" — never guess "supported".
"""


class AnthropicEvidenceCritic(EvidenceCriticInterface):
    """The real Stage B pass. Runs a different model + prompt regime than
    the generator (DDR-02: break correlated error between writer and
    checker). Refusal is treated as abstention: every citation comes back
    insufficient, which downstream strips the citations and flags the
    finding rather than fabricating support."""

    MODEL = "claude-sonnet-4-6"   # deliberately not the generator's model

    def __init__(self, model: str = MODEL, client=None, max_tokens: int = 4000):
        if client is None:
            import anthropic  # deferred: only the real client needs it
            client = anthropic.Anthropic()
        self.client = client
        self.model = model
        self.max_tokens = max_tokens

    def check_claim(self, claim_text, evidence):
        if not evidence:
            return []
        evidence_block = "\n".join(
            f"[{e.source_id}] ({e.source_type}) {e.snippet}"
            for e in evidence)
        user = (f"Claim:\n{claim_text}\n\n"
                f"Cited evidence excerpts:\n{evidence_block}")

        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=_CRITIC_SYSTEM_PROMPT,
            output_config={"format": {"type": "json_schema",
                                      "schema": _CHECKS_SCHEMA}},
            messages=[{"role": "user", "content": user}],
        )
        if response.stop_reason == "refusal":
            return [ClaimCheck(e.source_id, EvidenceVerdict.INSUFFICIENT,
                               "critic declined to judge (abstention)")
                    for e in evidence]
        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)

        return _checks_from_payload(data, evidence)


def _checks_from_payload(data: dict,
                         evidence: list[RetrievedEvidence]) -> list[ClaimCheck]:
    """Critic JSON -> one ClaimCheck per citation, in order. Verdicts for
    unknown ids are ignored; citations the critic said nothing about get
    an abstention (insufficient), never a guessed verdict."""
    known = {e.source_id for e in evidence}
    by_id = {c["evidence_id"]: c for c in data["checks"]
             if c["evidence_id"] in known}
    out = []
    for e in evidence:
        c = by_id.get(e.source_id)
        if c is None:
            out.append(ClaimCheck(e.source_id,
                                  EvidenceVerdict.INSUFFICIENT,
                                  "no verdict returned (abstention)"))
        else:
            out.append(ClaimCheck(e.source_id,
                                  EvidenceVerdict(c["verdict"]),
                                  c["rationale"]))
    return out


class LocalEvidenceCritic(EvidenceCriticInterface):
    """Stage B over a self-hosted OpenAI-compatible server (vLLM, Ollama,
    LM Studio, llama.cpp). Defaults to a smaller model than LocalLLM's
    generator (HAZOP_CRITIC_MODEL, falling back to llama3.2:3b) so writer
    and checker stay different models even fully on-prem (DDR-02)."""

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 client=None, max_tokens: int = 2000):
        if client is None:
            from .llm import OpenAICompatClient
            client = OpenAICompatClient(
                base_url=base_url or os.environ.get(
                    "HAZOP_LLM_URL", "http://localhost:11434/v1"),
                model=model or os.environ.get(
                    "HAZOP_CRITIC_MODEL", "llama3.2:3b"))
        self.client = client
        self.max_tokens = max_tokens

    def check_claim(self, claim_text, evidence):
        if not evidence:
            return []
        evidence_block = "\n".join(
            f"[{e.source_id}] ({e.source_type}) {e.snippet}"
            for e in evidence)
        user = (f"Claim:\n{claim_text}\n\n"
                f"Cited evidence excerpts:\n{evidence_block}")
        try:
            data = self.client.chat_json(
                _CRITIC_SYSTEM_PROMPT, user, _CHECKS_SCHEMA,
                "evidence_checks", max_tokens=self.max_tokens)
        except RuntimeError:
            # the checker failing must never fabricate support: abstain
            return [ClaimCheck(e.source_id, EvidenceVerdict.INSUFFICIENT,
                               "critic failed to produce verdicts "
                               "(abstention)") for e in evidence]
        return _checks_from_payload(data, evidence)
