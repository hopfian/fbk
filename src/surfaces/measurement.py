"""Measurement surface: rate/latency characterization (docs/10 §8, docs/11 §8).

The harness that makes the docs/10 §8 measurement plan and the docs/11 §8
pacing engineering real, on three legs:

ARCHITECTURE:

  The three legs are deliberately separated by risk class. ``latency()``
  is the only network-touching leg: sequential canary probes timed with
  ``time.perf_counter()``, journaled per sample (docs/11 §7 journal
  discipline) so every run is auditable after the fact. ``pace()`` is pure
  math — deterministic per seed, no HTTP, no session state; the CALLER owns
  diurnal gating and ramp/warm-up scaling (docs/15 §P9-1: the same lognormal
  shape the RequestGovernor enforces, floor 4s / mean 12s / cv 0.7 at the
  transport layer — this module generates schedules, the governor enforces
  them). ``report()`` reads only journal files, so the pacing verdicts can
  be computed on any captured session offline.

CALIBRATION NOTES:

  * latency() canary — the verified cheapest read in the registry:
    ``CometNotificationsBadgeCountQuery`` (doc_id 9714526941947209) with
    ``{"environment": "MAIN_SURFACE"}`` — a read-only viewer badge the real
    web surface fires constantly, so extra calls are maximally boring to the
    risk engine. Typed errors are DATA, not aborts (docs/10 §3 symptom
    catalogue): a ``RateLimitedError`` at sample #3 is exactly the ceiling
    this harness exists to find, so every failure is journaled and counted,
    never retried (a retry would flatter the very latency being measured
    and double the request volume that tripped the limit). Live-calibrated
    reference window (docs/15 §P6-4, 10 samples): min 0.344s / mean 0.602s /
    p50 0.360s / p95 2.669s.
  * pace() shape — lognormal inter-arrival schedules (docs/11 §8): human
    gaps are heavy-tailed (bursts and long pauses), and a metronome
    ``sleep(N)`` gap is an L6 statistical bot giveaway measurable in a
    hundred events (docs/10 §4).
  * report() verdicts — post-run pacing analysis of a request journal:
    inter-arrival gaps, status distribution (2xx vs other), and a docs/10
    heuristic verdict ("burst-suspect" when the p95 gap collapses under
    0.25s, "sparse" when the mean gap stretches past 60s, else "healthy").

USER-DOC ANCHOR: cli/docs/07-reference-tooling.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import math
import random
import time
from itertools import pairwise
from typing import Any

from pydantic import BaseModel, Field

# The burst/sparse verdict thresholds and the nearest-rank percentile
# moved to their canonical homes (constants.py — the protocol-constant
# single source of truth; stats.py — the dependency-free statistics
# helper). They are re-exported here unchanged so the historical import
# path (``from surfaces.measurement import percentile, ...)``) keeps
# working; journal/analysis takes them from the new homes directly.
# The PEP 484 redundant-alias form marks the re-exports EXPLICITLY so
# the strict mypy gate (no_implicit_reexport) keeps honoring them.
from constants import BURST_P95_GAP_S as BURST_P95_GAP_S
from constants import SPARSE_MEAN_GAP_S as SPARSE_MEAN_GAP_S
from graphql.errors import FBGraphError
from journal.recorder import JSONLJournal
from stats import percentile as percentile

from .base import Surface

#: The verified cheapest read (docs/15 §P2-2, KNOWN_MUTATIONS ground truth).
CANARY_QUERY_NAME = "CometNotificationsBadgeCountQuery"
CANARY_DOC_ID = "9714526941947209"
CANARY_VARIABLES: dict[str, Any] = {"environment": "MAIN_SURFACE"}

#: docs/11 §8 default lognormal shape: cv≈0.6 puts realistic flurries and
#: pauses around the mean gap without the extreme tail of cv≈1.1.
DEFAULT_CV = 0.6
DEFAULT_SEED = 1234


class LatencyReport(BaseModel):
    """Summary of one latency measurement window (docs/10 §8).

    The typed outcome of ``MeasurementService.latency``: per-window sample
    accounting (ok vs failed — failed means a typed client error, which the
    docs/10 §8 plan treats as measurement DATA), the ok-call latency
    distribution (min/mean/p50/p95/max), and ``statuses_ok`` counting the
    ok calls whose payload additionally carried no GraphQL ``errors`` list.
    """

    samples: int = Field(ge=0)
    ok: int = Field(ge=0)
    failed: int = Field(ge=0)
    min_s: float = 0.0
    mean_s: float = 0.0
    p50_s: float = 0.0
    p95_s: float = 0.0
    max_s: float = 0.0
    statuses_ok: int = Field(ge=0, description="ok calls whose payload carried no GraphQL errors")


class PacingReport(BaseModel):
    """Inter-arrival analysis of one journal file (docs/10 §4 heuristics).

    The typed outcome of ``MeasurementService.report``: journal/request
    accounting, the HTTP status distribution (all classes, plus the 2xx
    subset), the computable inter-arrival gap statistics, and the verdict
    — ``"burst-suspect"`` / ``"sparse"`` / ``"healthy"`` per the docs/10 §4
    behavioral-detection heuristics (uniform inter-arrivals and
    machine-precision bursts are the classic bot signatures).
    """

    journal_name: str
    entries: int = Field(ge=0)
    requests: int = Field(ge=0, description="entries carrying an HTTP status (transport calls)")
    statuses: dict[str, int] = Field(default_factory=dict)
    statuses_ok: int = Field(ge=0, description="2xx-class responses")
    gaps: int = Field(ge=0, description="inter-arrival gaps computable from ts fields")
    mean_gap_s: float = 0.0
    max_gap_s: float = 0.0
    p95_gap_s: float = 0.0
    verdict: str


def lognormal_schedule(actions: int, mean_gap_s: float, cv: float = DEFAULT_CV,
                       seed: int = DEFAULT_SEED, *,
                       start_in: float = 0.0) -> list[float]:
    """Absolute action times with lognormal inter-arrivals (docs/11 §8).

    Human inter-arrivals are lognormal-ish (heavy right tail: long pauses
    with quick flurries); fixed/uniform gaps are the classic statistical
    bot signature (docs/10 §4). Standard parameterization: the underlying
    normal has mean ``ln(mean_gap_s)`` and variance ``ln(1 + cv^2)`` so the
    drawn gaps have mean ``mean_gap_s * sqrt(1 + cv^2)`` and relative
    spread ``cv``. ``cv=0`` degenerates to constant ``mean_gap_s`` gaps.

    Deterministic per seed (``random.Random(seed)``); pure — no HTTP, no
    session. ``start_in`` offsets the whole schedule (e.g. a ramp-curve or
    diurnal-gate offset chosen by the caller per docs/11 §8).

    Args:
        actions: How many action times to generate; must be >= 0.
        mean_gap_s: The target gap scale in seconds; must be > 0.
        cv: Coefficient of variation shaping the lognormal spread;
            ``cv=0`` degenerates to constant ``mean_gap_s`` gaps.
        seed: PRNG seed — identical seeds yield identical schedules,
            which is what makes pacing experiments reproducible offline.
        start_in: Absolute offset of the first action time (seconds).

    Returns:
        Monotonic action times as absolute offsets from schedule start.

    Raises:
        ValueError: On a negative ``actions`` or ``cv``, or a
            non-positive ``mean_gap_s``.
    """
    if actions < 0:
        raise ValueError(f"actions must be >= 0, got {actions}")
    if mean_gap_s <= 0.0:
        raise ValueError(f"mean_gap_s must be > 0, got {mean_gap_s}")
    if cv < 0.0:
        raise ValueError(f"cv must be >= 0, got {cv}")
    rng = random.Random(seed)
    # Lognormal parameterization (docs/11 §8): sigma = sqrt(ln(1 + cv^2))
    # holds the drawn gaps' relative spread at ``cv`` while the heavy
    # right tail produces the human flurries-and-pauses shape — the
    # anti-metronome invariant the risk engine measures (docs/10 §4).
    mu = math.log(mean_gap_s)
    sigma = math.sqrt(math.log(1.0 + cv * cv))
    times: list[float] = []
    t = float(start_in)
    for _ in range(actions):
        t += rng.lognormvariate(mu, sigma)
        times.append(t)
    return times


class MeasurementService(Surface):
    """docs/10 §8 measurement harness: canary latency, pacing math, journal verdicts."""

    # ------------------------------------------------------------------ latency
    def latency(self, samples: int = 10, *,
                query_name: str | None = None) -> LatencyReport:
        """Run ``samples`` sequential canary calls and summarize the timings.

        Default canary: the badge read (verified cheapest, docs/15 §P2-2).
        Each call is timed with ``time.perf_counter()``; typed client errors
        (``FBGraphError`` subclasses — rate limits, checkpoints, staleness)
        are counted as failed samples, never retried and never allowed to
        abort the window (docs/10 §8: failures are data). Every sample is
        journaled as ``{"event": "latency_sample", ...}``` so the run is
        auditable afterwards (docs/11 §7 journal discipline).

        Args:
            samples: How many sequential canary calls to fire; >= 0 (0
                yields an all-zero report without touching the network).
            query_name: Override the canary with any registry query name —
                e.g. to characterize a heavier read's latency profile.

        Returns:
            A ``LatencyReport`` over the window: ok/failed accounting,
            ok-call latency distribution, and the no-GraphQL-errors count.

        Raises:
            ValueError: On a negative ``samples`` count.
            FBGraphError: Never — typed canary errors are captured as
                failed samples (retriable signal classes are the data the
                harness exists to collect).
        """
        if samples < 0:
            raise ValueError(f"samples must be >= 0, got {samples}")
        name = query_name or CANARY_QUERY_NAME
        variables = dict(CANARY_VARIABLES)
        ok_seconds: list[float] = []
        statuses_ok = 0
        failed = 0
        for _ in range(samples):
            t0 = time.perf_counter()
            ok_call = False
            try:
                data = self.client.call(name, self.doc_id(name), variables)
                ok_call = True
                if not isinstance(data.get("errors"), list) or not data["errors"]:
                    statuses_ok += 1
            except FBGraphError:
                ok_call = False  # typed failure: data, not an abort (docs/10 §8)
            seconds = time.perf_counter() - t0
            self._journal({"event": "latency_sample", "query": name,
                           "ok": ok_call, "seconds": seconds})
            if ok_call:
                ok_seconds.append(seconds)
            else:
                failed += 1
        ok = len(ok_seconds)
        sorted_s = sorted(ok_seconds)
        mean_s = (sum(sorted_s) / len(sorted_s)) if sorted_s else 0.0
        return LatencyReport(
            samples=samples,
            ok=ok,
            failed=failed,
            min_s=sorted_s[0] if sorted_s else 0.0,
            mean_s=mean_s,
            p50_s=percentile(sorted_s, 50.0),
            p95_s=percentile(sorted_s, 95.0),
            max_s=sorted_s[-1] if sorted_s else 0.0,
            statuses_ok=statuses_ok,
        )

    # -------------------------------------------------------------------- pace
    def pace(self, actions: int, mean_gap_s: float, cv: float = DEFAULT_CV,
             seed: int = DEFAULT_SEED, *,
             start_in: float = 0.0) -> list[float]:
        """A lognormal inter-arrival schedule (docs/11 §8 pacing engineering).

        Delegates to :func:`lognormal_schedule` — deterministic per seed,
        pure (no HTTP). Use it to space real actions: gap shape stays
        lognormal even when the caller applies diurnal gates or ramp
        (warm-up) factors, per docs/11 §8/§3.

        Args:
            actions: Number of action times to schedule; >= 0.
            mean_gap_s: Target mean inter-arrival gap in seconds; > 0.
            cv: Gap coefficient of variation (lognormal spread).
            seed: PRNG seed — reproducible schedules per seed.
            start_in: Absolute offset of the first action (seconds).

        Returns:
            The monotonically increasing action-time schedule.

        Raises:
            ValueError: On invalid ``actions``/``mean_gap_s``/``cv`` per
                :func:`lognormal_schedule`.
        """
        return lognormal_schedule(actions, mean_gap_s, cv, seed, start_in=start_in)

    # ------------------------------------------------------------------ report
    def report(self, journal_name: str) -> PacingReport:
        """Analyze one session journal's pacing (docs/10 §4 heuristics).

        Reads ``<journal_dir>/<journal_name>.jsonl`` via
        ``JSONLJournal.read_all()`` and computes inter-arrival gaps from
        the ``ts`` fields, the request/status distribution (2xx vs other),
        and a verdict:

          * ``"burst-suspect"`` — p95 gap < 0.25s (docs/10 §4 burst window);
          * ``"sparse"``       — mean gap > 60s or fewer than 2 timestamps;
          * ``"healthy"``      — lognormal-ish human pacing in between.

        Args:
            journal_name: The session journal to analyze — resolved to
                ``<journal_dir>/<journal_name>.jsonl`` via the session
                config; entries need ``ts`` fields (gaps) and optional
                ``status`` fields (request distribution).

        Returns:
            A ``PacingReport`` with the gap statistics, the status
            distribution, and the docs/10 §4 heuristic verdict.

        Raises:
            ValueError: When the journal file is missing — a clean, typed
                message pointing at the expected path (run a journaled
                command first).
        """
        path = self.session.config.journal_file(journal_name)
        if not path.is_file():
            raise ValueError(
                f"journal not found: {journal_name!r} "
                f"(expected {path}) — run a journaled command first")
        entries = JSONLJournal(path).read_all()

        stamps = sorted(float(e["ts"]) for e in entries
                        if isinstance(e.get("ts"), (int, float)))
        gaps = [b - a for a, b in pairwise(stamps) if b > a]
        statuses: dict[str, int] = {}
        statuses_ok = 0
        requests = 0
        for entry in entries:
            status = entry.get("status")
            if status is None:
                continue
            requests += 1
            key = str(status)
            statuses[key] = statuses.get(key, 0) + 1
            try:
                if 200 <= int(status) < 300:
                    statuses_ok += 1
            except (TypeError, ValueError):
                continue

        sorted_gaps = sorted(gaps)
        mean_gap = (sum(sorted_gaps) / len(sorted_gaps)) if sorted_gaps else 0.0
        p95_gap = percentile(sorted_gaps, 95.0)
        if len(stamps) < 2:
            verdict = "sparse"
        elif p95_gap < BURST_P95_GAP_S:
            verdict = "burst-suspect"
        elif mean_gap > SPARSE_MEAN_GAP_S:
            verdict = "sparse"
        else:
            verdict = "healthy"
        return PacingReport(
            journal_name=journal_name,
            entries=len(entries),
            requests=requests,
            statuses=statuses,
            statuses_ok=statuses_ok,
            gaps=len(gaps),
            mean_gap_s=mean_gap,
            max_gap_s=sorted_gaps[-1] if sorted_gaps else 0.0,
            p95_gap_s=p95_gap,
            verdict=verdict,
        )

    # ----------------------------------------------------------------- internal
    def _journal(self, entry: dict[str, Any]) -> None:
        """Record one entry into the session journal when one is attached."""
        journal = getattr(self.session, "journal", None)
        if journal is not None:
            journal.record(entry)
