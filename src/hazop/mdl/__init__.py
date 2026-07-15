"""hazop.mdl — the Fable Section 4 (AI/ML Model Development) package.

Everything that measures or records model behaviour, as opposed to the
reasoner that produces it (hazop.s3_are):

  gold / metrics        gold-standard recall gates    (MDL-7 / MDL-9)
  grounding             output tag-grounding audit    (MDL-10)
  fabrication           audit proxies + expert sheet  (MDL-11)
  latency               per-deviation P95 harness     (MDL-12)
  seeded_omissions      critic detection gate         (MDL-13)
  telemetry             accept/edit/reject events     (MDL-14 / FR-SW-2)
  mdl_scorecard         one-pass 4.3 gate runner      (python -m hazop.mdl.mdl_scorecard)
"""
from .gold import GoldNode, load_gold, PUMP_VESSEL_GOLD
from .metrics import evaluate_node, EvalResult, SlotRecall
from .grounding import audit_grounding, GroundingAudit
from .fabrication import (build_fabrication_report, FabricationReport,
                          write_audit_sheet_csv, write_audit_sheet_json)
from .latency import measure_latency, LatencyResult
from .seeded_omissions import run_seeded_omission_eval, OmissionEvalResult
from .telemetry import SuggestionAction, SuggestionEvent, TelemetryLog
