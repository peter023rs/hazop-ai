"""hazop.requirementTracker — Requirements Traceability Matrix (Fable §9).

Tracks which Fable requirements are done/partial/todo. The controlled
deliverable is data/rtm/requirements.json (human-owned statuses); rtm.py
derives everything else: code-citation scanning, rollups, the dashboard
view, and status updates from the "Tasks · RTM" tab.
"""
from .rtm import (RTM_PATH, VALID_STATUSES, load_rtm, rollup, rtm_view,
                  save_rtm, scan_citations, update_requirement)
