"""Post-hoc pacing audit: journal-driven, fully OFFLINE (docs/11 §8).

The governor paces every request (lognormal gaps, floor 4s, mean 12s,
cv 0.7 — docs/11 §8), and the journals record every HTTP request with a
``ts`` — but nothing closes the loop: the operator cannot audit, after
the fact, whether their ACTUAL pacing looked human. This module is that
loop: it turns a request journal's ``ts`` sequence into inter-arrival
gap statistics and typed verdict flags, so `fbk governor audit` can
answer "did my run look like the Phase-8 kill?" (docs/15 §P8-1: ~47
metronomic 2.4s requests triggered a session kill + account warning)
without touching the wire, the governor state, or another journal line.

VERDICT HEURISTICS (every constant cited):

  * burst_suspect      — p95 gap < BURST_P95_GAP_S (0.25s): the docs/10
    §4 machine-precision burst window, the exact constants.py constant
    shared with the measurement surface's report() verdict (docs/10 §4/§8).
  * sparse_anomaly     — mean gap > SPARSE_MEAN_GAP_S (60s): the docs/10
    §8 anomalously-sparse signature.
  * metronomic_suspect — gap CV under METRONOMIC_CV_FLOOR: docs/10 §4
    names "near-zero coefficient of variation" inter-arrivals as the
    classic bot giveaway but gives NO numeric floor, so 0.1 is a
    deliberately conservative heuristic (labeled as such; the Phase-8
    kill gaps had cv 0.0 — docs/15 §P8-1), gated on at least
    METRONOMIC_MIN_GAPS gaps because a CV over a handful of samples is
    noise, not evidence (docs/10 §4: "measurable in one hundred events").
  * policy_drift       — observed mean gap materially off the configured
    mean_gap_s: outside [DRIFT_LOW_FACTOR, DRIFT_HIGH_FACTOR] of the
    policy mean. The band is wide on purpose: the governor's own warm-up
    curve tops out at exactly 2x mean_gap_s for the first requests of a
    day (docs/16 §5, docs/11 §3 ramp curves), so a short warm-up-only
    journal must NOT flag; beyond 2x nothing in policy can explain the
    mean, and below half the mean the pacing ran materially hotter than
    the governor's own target (docs/11 §8).
  * floor_violations   — gaps below min_gap_s: the governor floors every
    gap at min_gap_s (docs/11 §8), so this count should be ZERO by
    construction; any nonzero value means the discipline was bypassed
    (e.g. FBK_GOVERNOR=off runs, or a pre-governor journal).

USER-DOC ANCHOR: cli/docs/07-reference-tooling.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from itertools import pairwise
from statistics import fmean, pstdev
from typing import Any

from pydantic import BaseModel, Field

from constants import BURST_P95_GAP_S, SPARSE_MEAN_GAP_S
from governor import GovernorConfig
from stats import percentile

#: Metronomy CV floor (HEURISTIC — docs/10 §4 names near-zero CV as the
#: bot signature without a numeric threshold; 0.1 is conservative so only
#: genuinely metronomic sequences fire. The Phase-8 kill gaps were exactly
#: uniform — cv 0.0 — docs/15 §P8-1.)
METRONOMIC_CV_FLOOR = 0.1

#: Minimum gap count before the CV statistic is trusted as metronomy
#: evidence (HEURISTIC — docs/10 §4 puts the signal at "measurable in
#: one hundred events"; 10 gaps is the reliability floor below which
#: sample CV is noise, not evidence).
METRONOMIC_MIN_GAPS = 10

#: Policy-drift band around the configured mean gap: the observed mean
#: must stay within [DRIFT_LOW_FACTOR, DRIFT_HIGH_FACTOR] x mean_gap_s.
#: The upper bound matches the governor's warm-up curve ceiling (exactly
#: 2x mean — docs/16 §5), so warm-up-heavy journals never false-positive;
#: the lower bound is the symmetric "materially hotter than policy" line.
DRIFT_LOW_FACTOR = 0.5
DRIFT_HIGH_FACTOR = 2.0


class PacingAudit(BaseModel):
    """Typed outcome of one post-hoc pacing audit (docs/11 §8).

    Raw inter-arrival statistics over the request gaps of one journal
    plus the verdict flags per the documented heuristics (see module
    docstring for every threshold's citation). The flags are DATA —
    `governor audit` exits 0 whether or not they fire; only a journal
    with nothing to audit (missing, or fewer than two requests) is a
    failed precondition.
    """

    count: int = Field(ge=0, description="inter-arrival gaps audited")
    p50_gap_s: float = 0.0
    p95_gap_s: float = 0.0
    min_gap_s: float = 0.0
    max_gap_s: float = 0.0
    mean_gap_s: float = 0.0
    gap_cv: float = 0.0
    burst_suspect: bool = False
    sparse_anomaly: bool = False
    metronomic_suspect: bool = False
    policy_drift: bool = False
    floor_violations: int = Field(
        default=0, ge=0,
        description="gaps under the configured min_gap_s (should be zero "
                    "by construction — the governor floors; docs/11 §8)")


def request_gaps(entries: Iterable[Mapping[str, Any]]) -> list[float]:
    """Consecutive inter-arrival gaps of a journal's request entries.

    Only entries that represent outbound HTTP are spaced — the transport
    journal shape carries ``method`` and ``url`` ({"ts", "method",
    "url", "status", "content_length", "ctx": {"surface": ...}}), so
    event markers (``session_start``, ``latency_sample``, ...) never
    inject phantom requests into the gap sequence. Entries are
    stable-sorted by ``ts`` (the journal is append-ordered, so equal
    stamps keep their on-disk order); entries without a numeric ``ts``
    are skipped — they cannot be spaced. Equal adjacent stamps yield a
    0.0 gap: two requests in the same instant is the strongest burst
    evidence there is, not noise to filter away.

    Args:
        entries: Journal records in on-disk order (e.g.
            ``JSONLJournal.read_all(strict=False)`` output).

    Returns:
        The consecutive ts deltas (seconds) of the request entries, in
        chronological order; empty when fewer than two spaced requests
        exist.
    """
    stamps = sorted(
        float(e["ts"])
        for e in entries
        if "method" in e and "url" in e
        and isinstance(e.get("ts"), (int, float))
        and not isinstance(e.get("ts"), bool))
    return [b - a for a, b in pairwise(stamps)]


def audit_pacing(gaps: Sequence[float], config: GovernorConfig) -> PacingAudit:
    """Compute the gap statistics and the documented verdict flags.

    Args:
        gaps: Inter-arrival gaps in seconds (e.g. :func:`request_gaps`
            output). An empty sequence types through as an all-zero audit
            with no flags — the caller decides whether "nothing to
            audit" is a failed precondition (the command does).
        config: The pacing policy audited against (min_gap_s floor,
            mean_gap_s target) — pass ``default_governor().config`` so
            FBK_GOVERNOR_* overrides apply exactly as they did at run
            time.

    Returns:
        A :class:`PacingAudit` with count, p50/p95/min/max/mean gap, the
        gap coefficient of variation, and the verdict flags per the
        module docstring's cited heuristics.
    """
    count = len(gaps)
    if count == 0:
        return PacingAudit(count=0)
    ordered = sorted(gaps)
    mean = fmean(gaps)
    sigma = pstdev(gaps)
    cv = (sigma / mean) if mean > 0.0 else 0.0
    burst_suspect = percentile(ordered, 95.0) < BURST_P95_GAP_S
    sparse_anomaly = mean > SPARSE_MEAN_GAP_S
    metronomic_suspect = (count >= METRONOMIC_MIN_GAPS
                          and cv < METRONOMIC_CV_FLOOR)
    policy_drift = (mean < DRIFT_LOW_FACTOR * config.mean_gap_s
                    or mean > DRIFT_HIGH_FACTOR * config.mean_gap_s)
    floor_violations = sum(1 for g in gaps if g < config.min_gap_s)
    return PacingAudit(
        count=count,
        p50_gap_s=percentile(ordered, 50.0),
        p95_gap_s=percentile(ordered, 95.0),
        min_gap_s=ordered[0],
        max_gap_s=ordered[-1],
        mean_gap_s=mean,
        gap_cv=cv,
        burst_suspect=burst_suspect,
        sparse_anomaly=sparse_anomaly,
        metronomic_suspect=metronomic_suspect,
        policy_drift=policy_drift,
        floor_violations=floor_violations,
    )
