"""Offline tests for the MEASUREMENT harness surface (docs/10 §8, docs/11 §8).

Latency runs replay against StubSession with a canned badge response (and a
typed-error response for the failure path); pacing is pure math asserted
against the standard lognormal parameterization; report verdicts come from
synthetic journals written through the real JSONLJournal.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import itertools

from fakes import StubSession

from config import Config
from graphql.errors import NotLoggedInError
from journal.recorder import JSONLJournal
from surfaces.measurement import (
    CANARY_QUERY_NAME,
    MeasurementService,
    lognormal_schedule,
)

BADGE_RESPONSE = {"data": {"viewer": {"notifications_unseen_count": 3}}}


# ----------------------------------------------------------------------- pace
def test_pace_deterministic_same_seed() -> None:
    """Same seed -> identical schedule; different seed -> different one."""
    a = lognormal_schedule(10, 5.0, cv=0.6, seed=1234)
    b = lognormal_schedule(10, 5.0, cv=0.6, seed=1234)
    assert a == b
    c = lognormal_schedule(10, 5.0, cv=0.6, seed=99)
    assert a != c


def test_pace_count_and_monotonic() -> None:
    """Exactly `actions` absolute times, strictly increasing (gaps > 0)."""
    times = lognormal_schedule(7, 3.0, cv=0.9, seed=42)
    assert len(times) == 7
    assert all(t2 > t1 for t1, t2 in itertools.pairwise(times))
    assert times[0] > 0.0


def test_pace_mean_gap_matches_lognormal_mean() -> None:
    """Seed-stable: sample mean gap ~= mean_gap * sqrt(1 + cv^2) (the
    standard lognormal parameterization with mu=ln(m), var=ln(1+cv^2))."""
    n, mean_gap, cv, seed = 2000, 5.0, 0.6, 1234
    times = lognormal_schedule(n, mean_gap, cv=cv, seed=seed)
    gaps = [t2 - t1 for t1, t2 in itertools.pairwise(times)]
    expected = mean_gap * math.sqrt(1.0 + cv * cv)
    assert abs(sum(gaps) / len(gaps) - expected) / expected < 0.30


def test_pace_cv_zero_degenerates_to_constant_gaps() -> None:
    """cv=0 -> sigma=0 -> every gap is exactly mean_gap_s (metronome edge)."""
    times = lognormal_schedule(5, 2.0, cv=0.0, seed=7)
    assert [round(t, 9) for t in times] == [2.0, 4.0, 6.0, 8.0, 10.0]


def test_pace_start_in_offsets_schedule() -> None:
    """start_in shifts every action by a constant offset."""
    base = lognormal_schedule(5, 2.0, cv=0.0, seed=1)
    shifted = lognormal_schedule(5, 2.0, cv=0.0, seed=1, start_in=10.0)
    assert [s - b for s, b in zip(shifted, base, strict=False)] == [10.0] * 5


def test_pace_rejects_bad_params() -> None:
    with pytest.raises(ValueError):
        lognormal_schedule(-1, 5.0)
    with pytest.raises(ValueError):
        lognormal_schedule(1, 0.0)
    with pytest.raises(ValueError):
        lognormal_schedule(1, 5.0, cv=-0.1)


def test_pace_service_method_delegates() -> None:
    svc = MeasurementService(StubSession({}))
    assert svc.pace(4, 2.0, cv=0.0, seed=5) == lognormal_schedule(4, 2.0, cv=0.0, seed=5)


# -------------------------------------------------------------------- latency
def test_latency_ok_counts_and_journal(tmp_path: Path) -> None:
    stub = StubSession({CANARY_QUERY_NAME: BADGE_RESPONSE})
    journal = JSONLJournal(tmp_path / "lat.jsonl")
    stub.journal = journal  # StubSession has no journal slot — attach a real one
    svc = MeasurementService(stub)

    report = svc.latency(samples=5)

    assert report.samples == 5
    assert report.ok == 5
    assert report.failed == 0
    assert report.statuses_ok == 5
    assert 0.0 <= report.min_s <= report.mean_s <= report.max_s
    assert report.min_s <= report.p50_s <= report.p95_s <= report.max_s
    # exactly the canary was called, with the verified cheap variables
    assert [c[0] for c in stub.graphql.calls] == [CANARY_QUERY_NAME] * 5
    assert all(c[2] == {"environment": "MAIN_SURFACE"} for c in stub.graphql.calls)
    # every sample journaled as a latency_sample entry
    entries = journal.read_all()
    assert len(entries) == 5
    assert all(e["event"] == "latency_sample" and e["ok"] is True
               and isinstance(e["seconds"], float) and "ts" in e
               for e in entries)


def test_latency_typed_failures_are_data_not_abort(tmp_path: Path) -> None:
    stub = StubSession({CANARY_QUERY_NAME: NotLoggedInError("xs rejected")})
    journal = JSONLJournal(tmp_path / "lat_fail.jsonl")
    stub.journal = journal
    svc = MeasurementService(stub)

    report = svc.latency(samples=4)

    assert report.samples == 4
    assert report.ok == 0
    assert report.failed == 4
    assert report.statuses_ok == 0
    assert report.min_s == report.mean_s == report.p50_s == report.p95_s == report.max_s == 0.0
    failed_entries = journal.read_all()
    assert len(failed_entries) == 4
    assert all(e["ok"] is False for e in failed_entries)


def test_latency_zero_samples() -> None:
    svc = MeasurementService(StubSession({}))
    report = svc.latency(samples=0)
    assert report.samples == 0 and report.ok == 0 and report.failed == 0


# --------------------------------------------------------------------- report
def _stub_with_state_dir(tmp_path: Path) -> StubSession:
    stub = StubSession({})
    stub.config = Config.discover().model_copy(update={"state_dir": tmp_path})
    return stub


def test_report_healthy_pacing(tmp_path: Path) -> None:
    journal = JSONLJournal(tmp_path / "pace.jsonl")
    for ts, status in ((1000.0, 200), (1000.1, 200), (1010.0, 200),
                       (1020.0, 500), (1030.0, 200)):
        journal.record({"ts": ts, "method": "POST", "status": status})
    svc = MeasurementService(_stub_with_state_dir(tmp_path))

    report = svc.report("pace")

    assert report.journal_name == "pace"
    assert report.entries == 5
    assert report.requests == 5
    assert report.statuses == {"200": 4, "500": 1}
    assert report.statuses_ok == 4
    assert report.gaps == 4
    assert report.mean_gap_s == pytest.approx((0.1 + 9.9 + 10.0 + 10.0) / 4)
    assert report.max_gap_s == pytest.approx(10.0)
    assert report.p95_gap_s == pytest.approx(10.0)  # > 0.25s burst threshold
    assert report.verdict == "healthy"


def test_report_burst_suspect(tmp_path: Path) -> None:
    journal = JSONLJournal(tmp_path / "burst.jsonl")
    for i in range(5):
        journal.record({"ts": 2000.0 + i * 0.05, "status": 200})
    svc = MeasurementService(_stub_with_state_dir(tmp_path))

    report = svc.report("burst")

    assert report.gaps == 4
    assert report.p95_gap_s < 0.25
    assert report.verdict == "burst-suspect"


def test_report_missing_journal(tmp_path: Path) -> None:
    svc = MeasurementService(_stub_with_state_dir(tmp_path))
    with pytest.raises(ValueError, match="journal not found"):
        svc.report("nope")


# ------------------------------------------------- compat re-export contract
def test_compat_reexports_pin_the_new_homes() -> None:
    """percentile and the burst/sparse thresholds moved to stats.py /
    constants.py (their canonical homes); surfaces.measurement re-exports
    them unchanged so the historical import path keeps working."""
    import constants as C
    import stats
    import surfaces.measurement as measurement

    assert measurement.percentile is stats.percentile
    assert measurement.BURST_P95_GAP_S is C.BURST_P95_GAP_S
    assert measurement.SPARSE_MEAN_GAP_S is C.SPARSE_MEAN_GAP_S
    assert measurement.BURST_P95_GAP_S == 0.25
    assert measurement.SPARSE_MEAN_GAP_S == 60.0
