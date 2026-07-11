"""
llm.py — Model-agnostic abstraction for the generative reasoning step.

Both specs require the LLM to be swappable (V1 5.2 configurable GPT-4o/LLaMA;
Fable AR-5 / MDL-1 model-agnostic layer). So the reasoner never calls an API
directly — it calls LLMInterface.generate_findings().

Ships with StubLLM: a deterministic, offline generator that produces plausible
structured causes/consequences/safeguards from the retrieved evidence + a few
rules. This lets the full pipeline run and be tested today with no API key.

Two real clients sit behind the same seam — the two branches of DDR-06's
swappable inference service:

  * AnthropicLLM — cloud API adapter (needs `pip install anthropic` +
    ANTHROPIC_API_KEY). Per AR-3 this branch transmits node context to a
    third-party provider: explicit configuration + legal review territory.
  * LocalLLM — self-hosted adapter speaking the OpenAI-compatible
    /v1/chat/completions protocol (vLLM, Ollama, LM Studio, llama.cpp
    server). Stdlib HTTP, no SDK. Process data never leaves the
    deployment (AR-3 on-prem / AR-4 air-gap).

Both use the same prompt, the same JSON schema, and the same composed
confidence — evidence backing, never model self-report (DDR-05) — and
referenced tags pass through untouched so the grounding gate (FR-ARE-9 /
MDL-10) stays the single enforcement point.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .guidewords import Deviation, Guideword
from .schema import Parameter, RetrievedEvidence


@dataclass
class GeneratedFinding:
    """Raw generator output before grounding/validation is applied."""
    text: str
    referenced_tags: list[str]
    confidence: float
    evidence_ids: list[str]


@dataclass
class GeneratedTriple:
    causes: list[GeneratedFinding]
    consequences: list[GeneratedFinding]
    safeguards: list[GeneratedFinding]


class LLMInterface(ABC):
    @abstractmethod
    def generate_findings(
        self,
        deviation: Deviation,
        node_context: dict,
        evidence: list[RetrievedEvidence],
    ) -> GeneratedTriple:
        ...


class StubLLM(LLMInterface):
    """
    Deterministic stand-in. Uses retrieved evidence + simple parameter rules to
    emit structured findings. Confidence reflects how much evidence backs each
    item, so the grounding/labelling logic downstream has something real to act
    on. This is NOT a model — it's a test double shaped like one.
    """

    def generate_findings(self, deviation, node_context, evidence):
        tags = node_context.get("equipment_tags", [])
        ev_ids = [e.source_id for e in evidence]
        # confidence scales with amount of retrieved evidence
        base_conf = min(0.95, 0.4 + 0.15 * len(evidence))

        causes: list[GeneratedFinding] = []
        consequences: list[GeneratedFinding] = []
        safeguards: list[GeneratedFinding] = []

        gw, param = deviation.guideword, deviation.parameter

        # ---- causes (evidence-driven where available) ----
        for e in evidence:
            causes.append(GeneratedFinding(
                text=f"Per {e.source_type}: {e.snippet.split('.')[0]}.",
                referenced_tags=tags[:1],
                confidence=round(min(0.95, e.score), 3),
                evidence_ids=[e.source_id],
            ))
        if not causes:
            # NFR-S-01: still produce a finding, but with no evidence -> flagged
            causes.append(GeneratedFinding(
                text=f"Possible cause of {deviation.label} (no matching precedent found).",
                referenced_tags=tags[:1],
                confidence=0.30,
                evidence_ids=[],
            ))

        # ---- consequences (rule of thumb per parameter/guideword) ----
        if param == Parameter.PRESSURE and gw == Guideword.MORE:
            consequences.append(GeneratedFinding(
                text="Overpressure of downstream vessel; potential loss of "
                     "containment if relief inadequate.",
                referenced_tags=tags,
                confidence=base_conf,
                evidence_ids=ev_ids,
            ))
        elif param == Parameter.FLOW and gw == Guideword.NO:
            consequences.append(GeneratedFinding(
                text="Loss of level/flow to downstream equipment; possible dry "
                     "running of pump.",
                referenced_tags=tags,
                confidence=base_conf,
                evidence_ids=ev_ids,
            ))
        elif param == Parameter.FLOW and gw == Guideword.REVERSE:
            consequences.append(GeneratedFinding(
                text="Back-flow into pump/suction on trip; potential reverse "
                     "rotation or contamination.",
                referenced_tags=tags,
                confidence=base_conf,
                evidence_ids=ev_ids,
            ))
        elif param == Parameter.TEMPERATURE and gw == Guideword.MORE:
            consequences.append(GeneratedFinding(
                text="Elevated temperature; possible thermal decomposition or "
                     "runaway per SDS thresholds.",
                referenced_tags=tags,
                confidence=base_conf,
                evidence_ids=ev_ids,
            ))
        else:
            consequences.append(GeneratedFinding(
                text=f"Process upset arising from {deviation.label}; "
                     f"consequence to be assessed against node intent.",
                referenced_tags=tags,
                confidence=round(base_conf * 0.6, 3),
                evidence_ids=ev_ids,
            ))

        # ---- safeguards (topology-derived hints are added by the reasoner;
        #      here the stub proposes generic ones) ----
        if param == Parameter.PRESSURE and gw == Guideword.MORE:
            safeguards.append(GeneratedFinding(
                text="Pressure relief valve on downstream vessel.",
                referenced_tags=tags,
                confidence=base_conf,
                evidence_ids=ev_ids,
            ))
        if param == Parameter.FLOW and gw == Guideword.REVERSE:
            safeguards.append(GeneratedFinding(
                text="Check valve on pump discharge prevents sustained reverse flow.",
                referenced_tags=tags,
                confidence=base_conf,
                evidence_ids=ev_ids,
            ))
        if param == Parameter.LEVEL and gw == Guideword.MORE:
            safeguards.append(GeneratedFinding(
                text="Independent high level alarm (LAH) on the vessel, "
                     "separate from the level control loop.",
                referenced_tags=tags,
                confidence=base_conf,
                evidence_ids=ev_ids,
            ))
        if not safeguards:
            safeguards.append(GeneratedFinding(
                text="Existing instrumentation/alarms to be confirmed from C&E matrix.",
                referenced_tags=[],
                confidence=0.35,
                evidence_ids=[],
            ))

        return GeneratedTriple(causes, consequences, safeguards)


# --------------------------------------------------------------------------
# Real model client (Anthropic API)
# --------------------------------------------------------------------------

_FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "referenced_tags": {"type": "array", "items": {"type": "string"}},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["text", "referenced_tags", "evidence_ids"],
    "additionalProperties": False,
}

_TRIPLE_SCHEMA = {
    "type": "object",
    "properties": {
        "causes": {"type": "array", "items": _FINDING_SCHEMA},
        "consequences": {"type": "array", "items": _FINDING_SCHEMA},
        "safeguards": {"type": "array", "items": _FINDING_SCHEMA},
    },
    "required": ["causes", "consequences", "safeguards"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = """\
You are a process-safety engineer contributing to a HAZOP worksheet.
For the given deviation and study node, propose credible causes,
consequences and safeguards.

Rules:
- Only reference equipment tags that appear in the node context; never
  invent tags. List every tag a finding mentions in referenced_tags.
- Base findings on the supplied evidence where possible and cite the
  matching source ids in evidence_ids (only ids from the evidence list).
  A finding with no supporting evidence is allowed but must have an
  empty evidence_ids list.
- One finding per distinct mechanism; no risk ranking (human-only).
- Write findings as short, self-contained worksheet entries.
"""


def _user_prompt(deviation, node_context, evidence) -> str:
    evidence_block = "\n".join(
        f"[{e.source_id}] ({e.source_type}) {e.snippet}" for e in evidence
    ) or "(no evidence retrieved)"
    return (
        f"Deviation: {deviation.label}\n"
        f"Study node: {node_context.get('node_id')}\n"
        f"Equipment tags: {', '.join(node_context.get('equipment_tags', []))}\n"
        f"Design intent: {node_context.get('design_intent', '')}\n\n"
        f"Retrieved evidence:\n{evidence_block}"
    )


def _triple_from_payload(data: dict, known_ids: set[str]) -> GeneratedTriple:
    """Model JSON -> GeneratedTriple. Confidence is composed here from the
    evidence actually cited (DDR-05: never model self-report); unknown
    evidence ids are dropped; referenced_tags pass through untouched for
    the grounding gate to judge."""

    def build(items: list[dict]) -> list[GeneratedFinding]:
        out = []
        for item in items:
            ev_ids = [i for i in item["evidence_ids"] if i in known_ids]
            out.append(GeneratedFinding(
                text=item["text"],
                referenced_tags=item["referenced_tags"],
                # composed, not self-reported: scales with the evidence
                # actually cited; unsupported findings sit below the
                # worksheet's flagging threshold
                confidence=(round(min(0.95, 0.4 + 0.15 * len(ev_ids)), 3)
                            if ev_ids else 0.30),
                evidence_ids=ev_ids,
            ))
        return out

    return GeneratedTriple(
        causes=build(data["causes"]),
        consequences=build(data["consequences"]),
        safeguards=build(data["safeguards"]),
    )


class AnthropicLLM(LLMInterface):
    """LLMInterface implementation backed by the Anthropic API (the cloud
    branch of DDR-06).

    The `anthropic` package is imported lazily so offline installs never
    need it; tests inject a fake client.
    """

    MODEL = "claude-opus-4-8"

    def __init__(self, model: str = MODEL, client=None, max_tokens: int = 16000):
        if client is None:
            import anthropic  # deferred: only the real client needs it
            client = anthropic.Anthropic()
        self.client = client
        self.model = model
        self.max_tokens = max_tokens

    def generate_findings(self, deviation, node_context, evidence):
        known_ids = {e.source_id for e in evidence}
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=_SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema",
                                      "schema": _TRIPLE_SCHEMA}},
            messages=[{"role": "user",
                       "content": _user_prompt(deviation, node_context,
                                               evidence)}],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError(
                f"model declined deviation '{deviation.label}' "
                f"(stop_reason=refusal)")
        if response.stop_reason == "max_tokens":
            raise RuntimeError(
                f"output truncated for deviation '{deviation.label}' — "
                f"raise max_tokens (currently {self.max_tokens})")
        text = next(b.text for b in response.content if b.type == "text")
        return _triple_from_payload(json.loads(text), known_ids)


# --------------------------------------------------------------------------
# Self-hosted model client (OpenAI-compatible protocol)
# --------------------------------------------------------------------------

class OpenAICompatClient:
    """Minimal stdlib client for any OpenAI-compatible /v1/chat/completions
    server — vLLM, Ollama, LM Studio, llama.cpp server. No SDK dependency;
    `transport` is injectable for tests.

    Structured output uses the OpenAI `response_format: json_schema` form,
    which all four servers support (vLLM guided decoding, Ollama >= 0.5
    structured outputs). Invalid JSON is retried once with the parse error
    fed back (DDR-03: retry-on-error), then refused — never repaired
    silently."""

    def __init__(self, base_url: str = "http://localhost:11434/v1",
                 model: str = "llama3.1:8b", timeout: float = 600.0,
                 transport=None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.transport = transport or self._http_post

    def _http_post(self, url: str, payload: dict) -> dict:
        import urllib.request
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def chat_json(self, system: str, user: str, schema: dict,
                  schema_name: str, max_tokens: int = 4000) -> dict:
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": schema_name,
                                                "schema": schema,
                                                "strict": True}},
        }
        last_err = None
        for _ in range(2):                       # one retry (DDR-03)
            resp = self.transport(f"{self.base_url}/chat/completions",
                                  payload)
            choice = resp["choices"][0]
            if choice.get("finish_reason") == "length":
                raise RuntimeError(
                    f"local model output truncated — raise max_tokens "
                    f"(currently {max_tokens})")
            text = choice["message"]["content"]
            try:
                return json.loads(text)
            except json.JSONDecodeError as err:
                last_err = err
                payload["messages"] = payload["messages"] + [
                    {"role": "assistant", "content": text},
                    {"role": "user",
                     "content": f"That was not valid JSON ({err}). "
                                f"Return ONLY the JSON object, nothing "
                                f"else."}]
        raise RuntimeError(
            f"local model returned invalid JSON twice: {last_err}")


class LocalLLM(LLMInterface):
    """LLMInterface over a self-hosted OpenAI-compatible server — the
    on-prem branch of DDR-06. Process data never leaves the deployment
    (AR-3), and an air-gapped site can run it entirely offline (AR-4).

    Same prompt, same schema, same composed confidence as AnthropicLLM;
    only the transport differs. Configure with HAZOP_LLM_URL /
    HAZOP_LLM_MODEL or constructor args.
    """

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 client: OpenAICompatClient | None = None,
                 max_tokens: int = 4000):
        if client is None:
            client = OpenAICompatClient(
                base_url=base_url or os.environ.get(
                    "HAZOP_LLM_URL", "http://localhost:11434/v1"),
                model=model or os.environ.get(
                    "HAZOP_LLM_MODEL", "llama3.1:8b"))
        self.client = client
        self.max_tokens = max_tokens

    def generate_findings(self, deviation, node_context, evidence):
        known_ids = {e.source_id for e in evidence}
        data = self.client.chat_json(
            _SYSTEM_PROMPT,
            _user_prompt(deviation, node_context, evidence),
            _TRIPLE_SCHEMA, "hazop_triple", max_tokens=self.max_tokens)
        return _triple_from_payload(data, known_ids)
