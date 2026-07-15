"""hazop.s6_rcm — Reporting & Compliance Module (stage 6).

Today this holds the Requirements Traceability Matrix (Fable §9); report
generation and action-item export (FR-RCM-1..5) are unbuilt.

Tracks which Fable requirements are done/partial/todo. The controlled
deliverable is data/rtm/requirements.json (human-owned statuses); rtm.py
derives everything else: code-citation scanning, rollups, the dashboard
view, and status updates from the "Tasks · RTM" tab.
"""
from .rtm import (RTM_PATH, VALID_STATUSES, load_rtm, rollup, rtm_view,
                  save_rtm, scan_citations, update_requirement)
