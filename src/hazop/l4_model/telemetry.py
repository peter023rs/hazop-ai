"""
telemetry.py — MDL-14 / FR-SW-2: accept-edit-reject suggestion telemetry.

Every scribe decision on an AI suggestion (accept / edit / reject) is an
event, captured append-only with user identity, timestamp, and the version
triplet (model, prompt template, KB snapshot) that produced the suggestion —
the FR-RCM-4 provenance-annex fields, so offline evaluation can slice by
exactly what generated each suggestion.

Design constraints from the spec:

  * MDL-14: schema suitable for OFFLINE evaluation and future preference
    tuning — hence flat JSONL (one event per line, append-only, trivially
    diffable/streamable), a schema_version stamp on every record, and no
    coupling to the live worksheet objects;
  * FR-SW-2: the log doubles as the accept/edit/reject audit trail, so
    events are never updated or deleted — corrections are new events;
  * any use for model TRAINING (as opposed to evaluation) requires customer
    opt-in per contract — that is a policy gate on consumers of this file,
    noted here because this file is the artifact that gate covers.

The action -> provenance mapping is the AR-1 state transition the workspace
applies to the worksheet row when the event is recorded: accepted ->
ai_generated_human_approved, edited -> ai_generated_human_modified,
rejected -> the finding leaves the worksheet body (no provenance state;
the event itself is the audit record).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from hazop.l3_reasoner.reasoner.worksheet import Provenance

SCHEMA_VERSION = 1


class SuggestionAction(str, Enum):
    ACCEPTED = "accepted"
    EDITED = "edited"
    REJECTED = "rejected"


# AR-1 provenance transition applied by the workspace on each action.
ACTION_PROVENANCE: dict[SuggestionAction, Optional[Provenance]] = {
    SuggestionAction.ACCEPTED: Provenance.AI_GENERATED_HUMAN_APPROVED,
    SuggestionAction.EDITED: Provenance.AI_GENERATED_HUMAN_MODIFIED,
    SuggestionAction.REJECTED: None,
}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SuggestionEvent:
    """One scribe decision on one AI suggestion."""
    study_id: str
    node_id: str
    deviation_label: str
    kind: str                    # "cause" | "consequence" | "safeguard"
    suggestion_text: str         # the AI text as offered (immutable record)
    action: SuggestionAction
    user: str                    # FR-SW-2: logged with user identity
    edited_text: str = ""        # required iff action == EDITED
    session_id: str = ""
    confidence: Optional[float] = None
    # FR-RCM-4 version triplet — what produced this suggestion
    model_version: str = ""
    prompt_template_version: str = ""
    kb_snapshot_version: str = ""
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(default_factory=_now_utc)

    def __post_init__(self):
        self.action = SuggestionAction(self.action)
        if self.action == SuggestionAction.EDITED and not self.edited_text:
            raise ValueError("EDITED event requires edited_text")
        if not self.user:
            raise ValueError("telemetry requires user identity (FR-SW-2)")

    @property
    def resulting_provenance(self) -> Optional[Provenance]:
        return ACTION_PROVENANCE[self.action]

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "study_id": self.study_id,
            "node_id": self.node_id,
            "deviation_label": self.deviation_label,
            "kind": self.kind,
            "suggestion_text": self.suggestion_text,
            "action": self.action.value,
            "edited_text": self.edited_text,
            "user": self.user,
            "session_id": self.session_id,
            "confidence": self.confidence,
            "model_version": self.model_version,
            "prompt_template_version": self.prompt_template_version,
            "kb_snapshot_version": self.kb_snapshot_version,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "SuggestionEvent":
        data = {k: v for k, v in raw.items() if k != "schema_version"}
        return cls(**data)


@dataclass
class TelemetrySummary:
    total: int
    by_action: dict[str, int]
    by_kind: dict[str, dict[str, int]]     # kind -> action -> count

    def rate(self, action: SuggestionAction) -> float:
        return (self.by_action.get(action.value, 0) / self.total
                if self.total else 0.0)

    def summary(self) -> str:
        lines = [f"Suggestion telemetry (MDL-14): {self.total} events"]
        for a in SuggestionAction:
            lines.append(f"  {a.value}: {self.by_action.get(a.value, 0)} "
                         f"({100 * self.rate(a):.1f}%)")
        for kind in sorted(self.by_kind):
            parts = ", ".join(f"{a}={n}" for a, n in
                              sorted(self.by_kind[kind].items()))
            lines.append(f"    {kind}: {parts}")
        return "\n".join(lines)


class TelemetryLog:
    """Append-only JSONL event log. One file per study is the intended
    granularity, but nothing enforces it — the path is the caller's."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def record(self, event: SuggestionEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(event.to_dict()) + "\n")

    def events(self) -> list[SuggestionEvent]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(SuggestionEvent.from_dict(json.loads(line)))
        return out

    def summarize(self) -> TelemetrySummary:
        events = self.events()
        by_action: dict[str, int] = {}
        by_kind: dict[str, dict[str, int]] = {}
        for e in events:
            by_action[e.action.value] = by_action.get(e.action.value, 0) + 1
            kind = by_kind.setdefault(e.kind, {})
            kind[e.action.value] = kind.get(e.action.value, 0) + 1
        return TelemetrySummary(total=len(events), by_action=by_action,
                                by_kind=by_kind)
