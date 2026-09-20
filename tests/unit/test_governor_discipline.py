"""Governor discipline deep-dive: diurnal active-window gating (Phase 10),
warm-up transitions, cooldown boundaries, persistence robustness and
thread safety.

Complements test_governor.py (pacing floors, cap basics, restart
persistence). Everything is offline: injected FakeClock + no-op sleeper;
local wall-clock is frozen via monkeypatch for the diurnal-window tests.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

import governor as governor_module
from governor import (
    GovernorBlockedError,
    GovernorConfig,
    GovernorState,
    RequestGovernor,
)


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


def make_governor(tmp_path: Path, clock: FakeClock, sleeper, **overrides) -> RequestGovernor:
    cfg = GovernorConfig(enabled=True, min_gap_s=4.0, mean_gap_s=12.0,
                         gap_cv=0.7, hourly_cap=5, daily_cap=10,
                         mutation_daily_cap=2, cooldown_s=900.0)
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return RequestGovernor(config=cfg, state_path=tmp_path / "gov.json",
                           sleeper=sleeper, clock=clock)


def freeze_local_hour(monkeypatch, hour: int) -> None:
    """Pin governor-side localtime to a fixed day at ``hour`` (2026-09-19)."""
    st = time.struct_time((2026, 9, 19, hour, 0, 0, 0, 262, -1))
    monkeypatch.setattr(governor_module.time, "localtime", lambda ts: st)


# ------------------------------------------------------- diurnal active window
class TestQuietHoursGating:
    """Pins the diurnal active-window gate: hour boundaries (upper bound
    exclusive), midnight wraparound, and permissive degenerate specs."""

    def test_inside_window_allows(self, tmp_path, monkeypatch):
        freeze_local_hour(monkeypatch, 12)
        g = make_governor(tmp_path, FakeClock(), lambda s: None,
                          quiet_hours="9-17")
        g.before_request()  # not blocked

    def test_below_window_blocks(self, tmp_path, monkeypatch):
        freeze_local_hour(monkeypatch, 8)
        g = make_governor(tmp_path, FakeClock(), lambda s: None,
                          quiet_hours="9-17")
        with pytest.raises(GovernorBlockedError, match="outside active window"):
            g.before_request()

    def test_upper_bound_is_exclusive(self, tmp_path, monkeypatch):
        freeze_local_hour(monkeypatch, 17)
        g = make_governor(tmp_path, FakeClock(), lambda s: None,
                          quiet_hours="9-17")
        with pytest.raises(GovernorBlockedError, match="9-17"):
            g.before_request()

    def test_last_inside_hour_allowed(self, tmp_path, monkeypatch):
        freeze_local_hour(monkeypatch, 16)
        g = make_governor(tmp_path, FakeClock(), lambda s: None,
                          quiet_hours="9-17")
        g.before_request()

    def test_wraparound_window_spans_midnight(self, tmp_path, monkeypatch):
        g = make_governor(tmp_path, FakeClock(), lambda s: None,
                          quiet_hours="22-6")
        for hour in (23, 0, 2, 5):
            freeze_local_hour(monkeypatch, hour)
            g.before_request()

    def test_wraparound_blocked_outside(self, tmp_path, monkeypatch):
        g = make_governor(tmp_path, FakeClock(), lambda s: None,
                          quiet_hours="22-6")
        for hour in (6, 12, 21):
            freeze_local_hour(monkeypatch, hour)
            with pytest.raises(GovernorBlockedError, match="22-6"):
                g.before_request()

    def test_off_and_always_specs_disable_gating(self, tmp_path, monkeypatch):
        for spec in ("off", "always", "24", "0-24", ""):
            freeze_local_hour(monkeypatch, 3)
            g = make_governor(tmp_path, FakeClock(), lambda s: None,
                              quiet_hours=spec)
            g.before_request()

    def test_malformed_specs_stay_permissive(self, tmp_path, monkeypatch):
        freeze_local_hour(monkeypatch, 3)
        for spec in ("banana", "9-banana", "-17"):
            g = make_governor(tmp_path, FakeClock(), lambda s: None,
                              quiet_hours=spec)
            g.before_request()

    def test_status_reports_window_membership(self, tmp_path, monkeypatch):
        clock = FakeClock()
        g = make_governor(tmp_path, clock, lambda s: None, quiet_hours="9-17")
        freeze_local_hour(monkeypatch, 12)
        assert g.status()["in_active_window"] is True
        freeze_local_hour(monkeypatch, 3)
        assert g.status()["in_active_window"] is False
        assert g.status()["active_window"] == "9-17"


# ---------------------------------------------------------------- warm-up
class TestWarmup:
    """Pins the 2x mean-gap warm-up curve and its decay once the warm-up
    request count has been served."""

    def _drain(self, tmp_path, *, warmup_requests: int) -> list[float]:
        """Four requests 0.1s apart with near-zero jitter: the slept gaps
        reveal the mean (2x mean while inside the warm-up window)."""
        clock = FakeClock()
        slept: list[float] = []
        g = make_governor(tmp_path, clock, slept.append,
                          hourly_cap=99, daily_cap=99, gap_cv=1e-9,
                          warmup_requests=warmup_requests)
        for _ in range(4):
            g.before_request()
            clock.advance(0.1)
        return slept

    def test_warmup_doubles_mean_gap_then_decays(self, tmp_path):
        slept = self._drain(tmp_path, warmup_requests=3)
        assert len(slept) == 3  # first request of the day never waits
        assert 23.0 < slept[0] <= 24.0  # doubled mean (12 * 2)
        assert 23.0 < slept[1] <= 24.0
        assert 11.0 < slept[2] <= 12.0  # warm-up exhausted -> plain mean

    def test_zero_warmup_means_no_doubling(self, tmp_path):
        slept = self._drain(tmp_path, warmup_requests=0)
        assert 11.0 < slept[0] <= 12.0


# ------------------------------------------------------- cooldown boundaries
class TestCooldownBoundaries:
    """Pins soft-block cooldown semantics: expiry is a closed window (blocked
    1s before, allowed exactly at), and the warning prints exactly once."""

    def test_blocked_one_second_before_expiry(self, tmp_path):
        clock = FakeClock()
        g = make_governor(tmp_path, clock, lambda s: None, cooldown_s=60)
        g.observe_soft_block()
        clock.advance(59)
        with pytest.raises(GovernorBlockedError, match="cooldown active for 1s"):
            g.before_request()

    def test_allowed_exactly_at_expiry(self, tmp_path):
        clock = FakeClock()
        g = make_governor(tmp_path, clock, lambda s: None, cooldown_s=60)
        g.observe_soft_block()
        clock.advance(60)  # now == cooldown_until: the window is closed
        g.before_request()

    def test_soft_blocks_are_counted(self, tmp_path):
        clock = FakeClock()
        g = make_governor(tmp_path, clock, lambda s: None)
        for _ in range(3):
            g.observe_soft_block()
        assert g.status()["soft_blocks_seen"] == 3

    def test_quiet_suppresses_the_cooldown_warning(self, tmp_path, capsys):
        clock = FakeClock()
        g = make_governor(tmp_path, clock, lambda s: None)
        g.observe_soft_block()
        with pytest.raises(GovernorBlockedError):
            g.before_request(quiet=True)
        assert "cooldown" not in capsys.readouterr().out

    def test_cooldown_warning_prints_exactly_once(self, tmp_path, capsys):
        clock = FakeClock()
        g = make_governor(tmp_path, clock, lambda s: None)
        g.observe_soft_block()
        for _ in range(3):
            with pytest.raises(GovernorBlockedError):
                g.before_request()
        out = capsys.readouterr().out
        assert out.count("governor: cooldown active") == 1


# ------------------------------------------------------------ persistence
class TestPersistenceRobustness:
    """Pins the on-disk GovernorState shape and its fail-soft loading:
    corrupt files fall back, unknown keys are ignored, write failures
    never block the gate."""

    def test_state_file_shape_on_disk(self, tmp_path, monkeypatch):
        freeze_local_hour(monkeypatch, 14)
        clock = FakeClock()
        path = tmp_path / "gov.json"
        g = make_governor(tmp_path, clock, lambda s: None)
        g.before_request()
        g.before_request(is_mutation=True)
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["day"] == "2026-09-19"
        assert raw["day_count"] == 2
        assert raw["hour_count"] == 2
        assert raw["mutation_count"] == 1
        assert raw["hour_bucket"] == "2026-09-19-14"

    def test_corrupt_state_file_falls_back_to_defaults(self, tmp_path):
        (tmp_path / "gov.json").write_text("{oops", encoding="utf-8")
        g = make_governor(tmp_path, FakeClock(), lambda s: None)
        assert g.status()["day_count"] == 0

    def test_unknown_state_keys_are_ignored(self, tmp_path):
        day = time.strftime("%Y-%m-%d", time.localtime(1_000_000))
        hour = time.strftime("%Y-%m-%d-%H", time.localtime(1_000_000))
        (tmp_path / "gov.json").write_text(json.dumps({
            "day": day, "hour_bucket": hour, "day_count": 7,
            "bogus_key": "ignored"}), encoding="utf-8")
        g = make_governor(tmp_path, FakeClock(1_000_000), lambda s: None)
        assert g.status()["day_count"] == 7

    def test_persist_failure_never_blocks_the_gate(self, tmp_path):
        clock = FakeClock()
        dir_as_file = tmp_path / "gov.json"
        dir_as_file.mkdir()
        g = RequestGovernor(config=GovernorConfig(enabled=True),
                            state_path=dir_as_file,
                            sleeper=lambda s: None, clock=clock)
        assert g.before_request() == 0.0  # disk write fails, gate still ran


# ---------------------------------------------------------- thread safety
class TestThreadSafety:
    """Pins the governor's lock discipline under concurrent before_request()
    hammering: no lost counts, caps enforced atomically."""

    def _hammer(self, tmp_path, threads: int, per_thread: int,
                **cfg_overrides) -> tuple[list, RequestGovernor]:
        clock = FakeClock()
        caps = {"hourly_cap": threads * per_thread,
                "daily_cap": threads * per_thread,
                "mutation_daily_cap": threads * per_thread}
        caps.update(cfg_overrides)
        g = make_governor(tmp_path, clock, lambda s: None, **caps)
        results: list = []
        results_lock = threading.Lock()

        def worker() -> None:
            for _ in range(per_thread):
                try:
                    g.before_request()
                    outcome: object = "ok"
                except GovernorBlockedError as exc:
                    outcome = exc
                with results_lock:
                    results.append(outcome)

        ts = [threading.Thread(target=worker) for _ in range(threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        return results, g

    def test_concurrent_requests_never_lose_counts(self, tmp_path):
        results, g = self._hammer(tmp_path, threads=8, per_thread=25)
        assert all(r == "ok" for r in results)
        status = g.status()
        assert status["hour_count"] == 200
        assert status["day_count"] == 200

    def test_cap_is_atomic_under_concurrency(self, tmp_path):
        results, g = self._hammer(tmp_path, threads=10, per_thread=1,
                                  hourly_cap=5, daily_cap=99)
        ok = [r for r in results if r == "ok"]
        blocked = [r for r in results if r != "ok"]
        assert len(ok) == 5  # exactly the cap, never above
        assert len(blocked) == 5
        assert all(isinstance(r, GovernorBlockedError) for r in blocked)
        assert g.status()["hour_count"] == 5


# --------------------------------------------------------- env override breadth
class TestEnvOverrideBreadth:
    """Pins FBK_GOVERNOR_* env parsing breadth: off spellings, numeric
    coercion, and the safe defaults kept on malformed values."""

    def test_off_spellings_all_disable(self):
        for spelling in ("off", "0", "false", "no"):
            assert GovernorConfig.from_env(
                {"FBK_GOVERNOR": spelling}).enabled is False

    def test_float_and_int_env_overrides(self):
        cfg = GovernorConfig.from_env({
            "FBK_GOVERNOR_MEAN_GAP": "3.5",
            "FBK_GOVERNOR_COOLDOWN": "77",
            "FBK_GOVERNOR_WARMUP": "5",
            "FBK_GOVERNOR_QUIET_HOURS": "1-2",
        })
        assert cfg.mean_gap_s == 3.5
        assert cfg.cooldown_s == 77.0
        assert cfg.warmup_requests == 5
        assert cfg.quiet_hours == "1-2"

    def test_malformed_numeric_env_keeps_safe_default(self):
        cfg = GovernorConfig.from_env({
            "FBK_GOVERNOR_COOLDOWN": "soon",
            "FBK_GOVERNOR_WARMUP": "many"})
        assert cfg.cooldown_s == GovernorConfig().cooldown_s
        assert cfg.warmup_requests == GovernorConfig().warmup_requests

    def test_empty_env_values_keep_defaults(self):
        cfg = GovernorConfig.from_env({"FBK_GOVERNOR_HOURLY": ""})
        assert cfg.hourly_cap == GovernorConfig().hourly_cap


class TestGovernorStateRoll:
    """Pins the empty-bucket invariants a fresh GovernorState starts from."""

    def test_fresh_state_has_empty_buckets(self):
        st = GovernorState()
        assert st.day == ""
        assert st.hour_bucket == ""
        assert st.day_count == 0
