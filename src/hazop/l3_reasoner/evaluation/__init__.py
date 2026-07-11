"""Evaluation harnesses for the Section 4.3 model-performance gates:
gold-standard recall (MDL-7/9), output tag grounding (MDL-10), fabrication
audit prep (MDL-11), latency (MDL-12), seeded-omission detection (MDL-13)."""
from .gold import GoldNode, load_gold, PUMP_VESSEL_GOLD
from .metrics import evaluate_node, EvalResult, SlotRecall
from .grounding import audit_grounding, GroundingAudit
from .fabrication import (build_fabrication_report, FabricationReport,
                          write_audit_sheet_csv, write_audit_sheet_json)
from .latency import measure_latency, LatencyResult
from .seeded_omissions import run_seeded_omission_eval, OmissionEvalResult
