"""Gold-standard evaluation harness (README next-step 2)."""
from .gold import GoldNode, load_gold, PUMP_VESSEL_GOLD
from .metrics import evaluate_node, EvalResult, SlotRecall
