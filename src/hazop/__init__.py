"""HAZOP-AI package.

One subpackage per subsystem of the requirements doc's §2.1 architecture:

    s1_dim   Document Intelligence   P&ID PDF -> topology graph
    s2_pml   Process Model Layer     plant graph, node proposal, screening
    s3_are   AI Reasoning Engine     guideword reasoning -> worksheet
    s4_kb    Knowledge Base          curated corpus, hybrid retrieval
    s5_sw    Study Workspace         integrated dashboard (:8780)
    s6_rcm   Reporting & Compliance  requirements traceability matrix
    s7_agm   Admin & Governance      placeholder

plus `mdl`, the §4 model-development harness that measures the ARE.
"""
