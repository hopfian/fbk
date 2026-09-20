"""Pure-statistics helpers shared across layers (docs/10 §8).

``percentile`` was born in the measurement surface (docs/10 §8
measurement context: the p50/p95 latency and gap columns of every
report), but it carries no surface semantics — the journal-driven
pacing audit needs the identical nearest-rank math (journal/analysis,
docs/11 §8) without importing the surfaces package, and the measurement
surface itself still serves the same helper to its callers.

ARCHITECTURE:

  This module is deliberately dependency-free (stdlib-only, no I/O, no
  FBK_* environment reads) so every layer — journal, surfaces, commands
  — can import it without violating the docs/12 §2 layering rule.
  surfaces/measurement.py re-exports ``percentile`` for backward
  compatibility; new importers should take it from here.

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import math


def percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile over an already-sorted list (no numpy).

    Args:
        sorted_values: Values in ascending order (caller sorts); the empty
            list yields 0.0 so empty windows type through cleanly.
        pct: Percentile in [0, 100] — e.g. 95.0 for p95.

    Returns:
        The nearest-rank element: rank = ceil(pct/100 * n), clamped to n.
    """
    if not sorted_values:
        return 0.0
    rank = max(1, math.ceil(pct / 100.0 * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]
