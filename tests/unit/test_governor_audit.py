"""``fbk governor audit`` — the post-hoc pacing self-audit (docs/11 §8).

Offline: synthetic journals in tmp_path driven through the REAL app
parser via ``--root``. Verdict flags are DATA (exit 0 even when every
flag fires — the audit is output, like ``measure report``); a missing
or empty journal is a failed precondition (exit 1). The metronomic case
reproduces the Phase-8 kill shape (docs/15 §P8-1: ~47 requests at a
metronomic 2.4s mean gap -> session kill + account warning).
"""
from __future__ import annotations

import json
import math
from itertools import pairwise
from pathlib import Path

import pytest

from app import build_parser
from governor import GovernorConfig, reset_governor_for_tests
from journal.analysis import (
    METRONOMIC_MIN_GAPS,
    audit_pacing,
    request_gaps,
)
from surfaces.measurement import lognormal_schedule

T0 = 1_700_000_000.0

_ENV_VARS = (
    "FBK_ROOT", "FBK_COOKIES", "FBK_IMPERSONATE", "FBK_GOVERNOR",
    "FBK_GOVERNOR_MIN_GAP", "FBK_GOVERNOR_MEAN_GAP", "FBK_GOVERNOR_HOURLY",
    "FBK_GOVERNOR_DAILY", "FBK_GOVERNOR_MUTATION_DAILY",
    "FBK_GOVERNOR_COOLDOWN", "FBK_GOVERNOR_QUIET_HOURS", "FBK_GOVERNOR_WARMUP",
)


@pytest.fixture(autouse=True)
def _clean_governor_env(monkeypatch):
    """Default governor policy + no env overrides, per test.

    The process-wide governor singleton caches the config it was
    constructed with, so it is dropped too: otherwise another test's
    FBK_GOVERNOR_* overrides (reverted from os.environ but retained by
    the singleton) would silently re-point this audit's policy.
    """
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    reset_governor_for_tests()


def _run(capsys, argv):
    """Drive one CLI invocation end to end; return (rc, out, err, payload).

    The payload parses from the WHOLE stdout in --json mode and from
    the trailing compact line in human mode (the emit contract); an
    empty stdout (the exit-1 precondition paths) yields payload None.
    """
    args = build_parser().parse_args(argv)
    rc = args.fn(args)
    captured = capsys.readouterr()
    if not captured.out:
        return rc, captured.out, captured.err, None
    try:
        payload = json.loads(captured.out)
    except json.JSONDecodeError:
        payload = json.loads(captured.out.splitlines()[-1])
    return rc, captured.out, captured.err, payload


def _request_entry(ts, method="POST", status=200):
    """One transport-shaped journal record (the docs/11 §7 request line)."""
    return {"ts": ts, "method": method,
            "url": "https://www.facebook.com/api/graphql/", "status": status,
            "content_length": 1024, "ctx": {"surface": "feed"}}


def _stamps(gaps, t0=T0):
    """Absolute ts values with the given consecutive gaps (cumulative
    addition, so each realized delta stays the exact gap float)."""
    stamps = [t0]
    for g in gaps:
        stamps.append(stamps[-1] + g)
    return stamps


def _write_journal(root: Path, name: str, entries, torn: str | None = None) -> Path:
    """Write a synthetic JSONL journal under ``<root>/state/``."""
    path = root / "state" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(e) + "\n" for e in entries)
    if torn is not None:
        body += torn  # a crash-torn trailing line: no newline, invalid JSON
    path.write_text(body, encoding="utf-8")
    return path


# --------------------------------------------------------- pinned math
class TestAuditMathPinned:
    """Pins the exact p50/p95/mean/cv math on a deterministic gap
    sequence, plus the all-zero empty-sequence contract."""

    def test_pins_every_stat_on_a_small_sequence(self):
        a = audit_pacing([4.0, 8.0, 12.0, 16.0, 20.0], GovernorConfig())
        assert a.count == 5
        assert a.min_gap_s == 4.0
        assert a.max_gap_s == 20.0
        # nearest-rank percentiles (surfaces.measurement.percentile):
        # p50 rank ceil(0.5*5)=3 -> 12.0; p95 rank ceil(0.95*5)=5 -> 20.0
        assert a.p50_gap_s == 12.0
        assert a.p95_gap_s == 20.0
        assert a.mean_gap_s == 12.0
        assert a.gap_cv == pytest.approx(math.sqrt(32.0) / 12.0)
        assert not a.burst_suspect
        assert not a.sparse_anomaly
        assert not a.metronomic_suspect
        assert not a.policy_drift
        assert a.floor_violations == 0

    def test_empty_gap_sequence_types_through(self):
        a = audit_pacing([], GovernorConfig())
        assert a.count == 0
        assert a.p50_gap_s == 0.0 and a.p95_gap_s == 0.0
        assert a.mean_gap_s == 0.0 and a.gap_cv == 0.0
        assert not (a.burst_suspect or a.sparse_anomaly
                    or a.metronomic_suspect or a.policy_drift)
        assert a.floor_violations == 0

    def test_sparse_and_burst_thresholds(self):
        # mean > 60s (SPARSE_MEAN_GAP_S, docs/10 §8) fires sparse_anomaly
        a = audit_pacing([90.0, 90.0, 90.0, 90.0], GovernorConfig())
        assert a.sparse_anomaly
        assert not a.burst_suspect
        # p95 < 0.25s (BURST_P95_GAP_S, docs/10 §4) fires burst_suspect
        b = audit_pacing([0.1] * 20, GovernorConfig())
        assert b.burst_suspect
        assert b.count >= METRONOMIC_MIN_GAPS and b.metronomic_suspect

    def test_metronomy_needs_the_reliability_floor(self):
        # cv 0 over only 4 gaps is noise, not evidence (docs/10 §4 puts
        # the signal at "measurable in one hundred events")
        a = audit_pacing([10.0, 10.0, 10.0, 10.0], GovernorConfig())
        assert not a.metronomic_suspect


class TestRequestGapsFiltering:
    """Pins request_gaps: only transport-shaped entries are spaced,
    ts-less requests are skipped, equal stamps yield a 0.0 gap."""

    def test_only_request_entries_are_spaced(self):
        entries = [
            {"event": "session_start", "ts": 100.0},
            {"ts": 100.0, "method": "GET", "url": "https://x/", "status": 200,
             "content_length": 1, "ctx": {"surface": "feed"}},
            {"event": "latency_sample", "ts": 101.0, "query": "q", "ok": True,
             "seconds": 0.1},
            {"ts": 106.0, "method": "POST", "url": "https://x/api/graphql/",
             "status": 200},
            {"method": "POST", "url": "https://x/api/graphql/"},  # no ts
            {"ts": 116.0, "method": "POST", "url": "https://x/api/graphql/",
             "status": 200},
            {"ts": 116.0, "method": "GET", "url": "https://x/other",
             "status": 200},
        ]
        assert request_gaps(entries) == [6.0, 10.0, 0.0]

    def test_two_requests_minimum(self):
        assert request_gaps([]) == []
        assert request_gaps([_request_entry(T0)]) == []
        assert request_gaps([_request_entry(T0), _request_entry(T0 + 5.0)]) == [5.0]


# ------------------------------------------------- the Phase-8 signature
class TestPhase8MetronomicSignature:
    """The docs/15 §P8-1 kill shape: ~47 requests at a metronomic 2.4s
    gap. That sequence fires metronomic_suspect (cv 0 under the 0.1
    heuristic floor) and trips every floor violation — the governor
    floors at 4s (docs/11 §8), so a 2.4s metronome means the discipline
    was bypassed. burst_suspect stays FALSE: docs/10 §4's burst window
    is p95 < 0.25s (sub-second, machine-precision), which a 2.4s
    metronome cannot reach — its signature is the CV heuristic."""

    def test_metronomic_2_4s_fires_metronomic_and_floor(self, tmp_path, capsys):
        _write_journal(tmp_path, "session",
                       [_request_entry(ts) for ts in _stamps([2.4] * 47)])
        rc, out, err, payload = _run(capsys, ["governor", "audit",
                                             "--root", str(tmp_path)])
        assert rc == 0
        assert payload["count"] == 47
        assert payload["metronomic_suspect"] is True
        assert payload["floor_violations"] == 47
        assert payload["policy_drift"] is True  # mean 2.4s << policy 12s
        assert payload["burst_suspect"] is False
        assert payload["sparse_anomaly"] is False
        assert "metronomic-suspect" in out and "floor-violations(47)" in out
        assert err == ""

    def test_sub_second_burst_fires_burst_suspect(self, tmp_path, capsys):
        _write_journal(tmp_path, "session",
                       [_request_entry(ts) for ts in _stamps([0.1] * 20)])
        rc, _, _, payload = _run(capsys, ["governor", "audit",
                                          "--root", str(tmp_path)])
        assert rc == 0
        assert payload["burst_suspect"] is True
        assert payload["p95_gap_s"] == pytest.approx(0.1)


class TestCleanLognormalPass:
    """A seeded lognormal schedule (the docs/11 §8 shape the governor
    enforces) audits clean: every flag False, zero floor violations."""

    def test_seeded_lognormal_schedule_audits_clean(self, tmp_path, capsys):
        times = lognormal_schedule(31, 12.0, cv=0.7, seed=1234)
        gaps = [max(4.0, b - a) for a, b in pairwise(times)]  # governor floors
        _write_journal(tmp_path, "session",
                       [_request_entry(ts) for ts in _stamps(gaps)])
        rc, out, _, payload = _run(capsys, ["governor", "audit",
                                            "--root", str(tmp_path)])
        assert rc == 0
        assert payload["count"] == 30
        assert payload["burst_suspect"] is False
        assert payload["sparse_anomaly"] is False
        assert payload["metronomic_suspect"] is False
        assert payload["policy_drift"] is False
        assert payload["floor_violations"] == 0
        assert "clean" in out


class TestFloorViolations:
    """All-below-floor gaps: every one counted (should be zero by
    construction — the governor floors; docs/11 §8)."""

    def test_all_below_floor_counts_every_gap(self, tmp_path, capsys):
        gaps = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5]
        _write_journal(tmp_path, "session",
                       [_request_entry(ts) for ts in _stamps(gaps)])
        rc, _, _, payload = _run(capsys, ["governor", "audit",
                                          "--root", str(tmp_path)])
        assert rc == 0
        assert payload["floor_violations"] == 6
        assert payload["burst_suspect"] is False
        assert payload["sparse_anomaly"] is False
        # count 6 < METRONOMIC_MIN_GAPS: too few gaps to judge metronomy
        assert payload["metronomic_suspect"] is False
        assert payload["policy_drift"] is True  # mean 2.25s << policy 12s


# ------------------------------------------------------ preconditions (exit 1)
class TestPreconditions:
    """Nothing to audit is a failed precondition: exit 1, message on
    stderr, NOTHING on stdout (the emit discipline holds payload data
    back when there is none)."""

    def test_missing_journal_exits_1(self, tmp_path, capsys):
        rc, out, err, payload = _run(capsys, ["governor", "audit",
                                              "--root", str(tmp_path)])
        assert rc == 1
        assert out == "" and payload is None
        assert "nothing to audit" in err and "session" in err

    def test_empty_journal_exits_1(self, tmp_path, capsys):
        _write_journal(tmp_path, "session", [])
        rc, out, err, _ = _run(capsys, ["governor", "audit",
                                        "--root", str(tmp_path)])
        assert rc == 1
        assert out == ""
        assert "nothing to audit" in err

    def test_single_request_exits_1(self, tmp_path, capsys):
        _write_journal(tmp_path, "session", [_request_entry(T0)])
        rc, _, err, _ = _run(capsys, ["governor", "audit",
                                      "--root", str(tmp_path)])
        assert rc == 1
        assert "fewer than two spaced requests" in err

    def test_event_only_journal_exits_1(self, tmp_path, capsys):
        # session_start events carry ts but no method/url: they are not
        # requests and never inject phantom gaps
        _write_journal(tmp_path, "session", [
            {"event": "session_start", "ts": T0},
            {"event": "session_start", "ts": T0 + 5.0}])
        rc, _, err, _ = _run(capsys, ["governor", "audit",
                                      "--root", str(tmp_path)])
        assert rc == 1
        assert "fewer than two spaced requests" in err

    def test_alternate_file_flag(self, tmp_path, capsys):
        _write_journal(tmp_path, "feed", [_request_entry(ts)
                                          for ts in _stamps([10.0] * 3)])
        rc, _, _, payload = _run(capsys, ["governor", "audit", "--file", "feed",
                                          "--root", str(tmp_path)])
        assert rc == 0
        assert payload["journal"] == "feed"
        assert payload["count"] == 3


# ------------------------------------------------------ torn trailing line
class TestTornTrailingLineTolerated:
    """The recorder's no-fsync trade-off: a crash can tear the FINAL
    line. read_all(strict=False) skips it with a stderr note and the
    audit proceeds on the intact records."""

    def test_torn_trailing_line_is_skipped(self, tmp_path, capsys):
        _write_journal(tmp_path, "session",
                       [_request_entry(ts) for ts in _stamps([10.0] * 20)],
                       torn='{"ts": 1700000210.0, "method": "POST", ')
        rc, _, err, payload = _run(capsys, ["governor", "audit",
                                            "--root", str(tmp_path)])
        assert rc == 0
        assert payload["count"] == 20  # 21 intact requests -> 20 gaps
        assert "torn" in err


# ------------------------------------------------------------- CLI wiring
class TestAuditWiring:
    """Parser wiring: audit exists on the governor family, defaults to
    the session journal, and --json stays machine-only."""

    def test_parses_with_default_journal(self):
        args = build_parser().parse_args(["governor", "audit"])
        assert callable(args.fn)
        assert args.file == "session"  # Session's default journal (docs/11 §7)

    def test_help_lists_audit(self, capsys):
        with pytest.raises(SystemExit) as ei:
            build_parser().parse_args(["governor", "--help"])
        assert ei.value.code == 0
        assert "audit" in capsys.readouterr().out

    def test_json_mode_is_machine_only(self, tmp_path, capsys):
        _write_journal(tmp_path, "session",
                       [_request_entry(ts) for ts in _stamps([2.4] * 47)])
        rc, out, _, payload = _run(capsys, ["governor", "audit",
                                            "--root", str(tmp_path), "--json"])
        assert rc == 0
        assert json.loads(out) == payload  # WHOLE stdout parses: no decoration
        assert payload["metronomic_suspect"] is True
        assert "gaps  " not in out  # no human decoration lines

    def test_governor_family_still_requires_a_child(self, capsys):
        with pytest.raises(SystemExit) as ei:
            build_parser().parse_args(["governor"])
        assert ei.value.code == 2
        assert "required" in capsys.readouterr().err
