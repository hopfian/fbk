"""Journal package: redacting JSONL recorders (docs/11 §7).

Every request, session start, and probe result lands in an append-only
journal line — the forensic record of each run — and every entry passes
the mandatory ALL_SECRETS redaction before it touches disk.

Public API:
  * JSONLJournal: append-only journal with mandatory secret redaction
  * redact_entry: deep-redact a dict with secret-name matching
  * PacingAudit / audit_pacing / request_gaps: the journal-driven
    pacing-audit primitives (docs/11 §8) behind ``governor audit``

USER-DOC ANCHOR: cli/docs/07-reference-tooling.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

# analysis (the pacing audit) imports EAGERLY: since analysis takes its
# statistics from stats.py and its thresholds from constants.py (no
# surfaces import at all), the historical circular-import edge —
# analysis -> surfaces.measurement -> journal.recorder re-entering this
# barrel mid-initialization — is gone. Plain ``from .analysis import``
# re-exports serve both runtime and mypy (with __all__ marking them
# explicit), so the PEP 562 __getattr__ indirection was retired with it.
from .analysis import PacingAudit, audit_pacing, request_gaps
from .recorder import JSONLJournal, redact_entry

__all__ = [
    "JSONLJournal",
    "PacingAudit",
    "audit_pacing",
    "redact_entry",
    "request_gaps",
]
