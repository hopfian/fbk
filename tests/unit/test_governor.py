"""Governor tests: pacing discipline, caps, cooldowns, UA sanitation,
warning detection — the docs/10 §7 + §11 §5 enforcement-response layer
built after the P8-1 account warning (docs/15 §P9).

Unit (offline): every governor is constructed with an injected FakeClock
and a list-appending sleeper — the suite never sleeps, never touches the
network, and keeps its state files under tmp_path.
"""
from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from constants import ACCOUNT_WARNING_MARKERS
from governor import (
    GovernorBlockedError,
    GovernorConfig,
    RequestGovernor,
    default_governor,
    reset_governor_for_tests,
)
from transport.profile import sanitize_user_agent


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


class TestPacing:
    """Pins the lognormal inter-arrival discipline: free first request,
    absolute min-gap floor, and non-metronomic variance."""

    def test_first_request_never_sleeps(self, tmp_path):
        clock = FakeClock()
        slept: list[float] = []
        g = make_governor(tmp_path, clock, lambda s: slept.append(s))
        assert g.before_request() == 0.0
        assert slept == []

    def test_gaps_respect_the_absolute_floor(self, tmp_path):
        """Even a tiny elapsed time still waits >= min_gap (lognormal above)."""
        clock = FakeClock()
        slept: list[float] = []
        g = make_governor(tmp_path, clock, lambda s: slept.append(s))
        g.before_request()
        clock.advance(0.5)  # half a second since the last request
        g.before_request()
        assert slept and slept[0] >= 4.0 - 0.5

    def test_gaps_are_never_metronomic(self, tmp_path):
        """docs/10 §4: uniform inter-arrivals are the bot giveaway — the
        governor's lognormal gaps must vary."""
        clock = FakeClock()
        slept: list[float] = []
        g = make_governor(tmp_path, clock, lambda s: slept.append(s),
                          hourly_cap=99, daily_cap=99)
        for _ in range(12):
            g.before_request()
            clock.advance(0.1)
        assert len({round(s, 1) for s in slept}) > 3

    def test_elapsed_time_eats_the_gap(self, tmp_path):
        clock = FakeClock()
        slept: list[float] = []
        g = make_governor(tmp_path, clock, lambda s: slept.append(s))
        g.before_request()
        clock.advance(600)  # ten minutes later: gap already served
        assert g.before_request() == 0.0


class TestCaps:
    """Pins hourly/daily/mutation budget enforcement and hour-bucket rollover."""

    def test_hourly_cap_blocks(self, tmp_path):
        clock = FakeClock()
        g = make_governor(tmp_path, clock, lambda s: None, hourly_cap=2)
        g.before_request()
        g.before_request()
        with pytest.raises(GovernorBlockedError, match="hourly cap"):
            g.before_request()

    def test_hourly_window_rolls(self, tmp_path):
        clock = FakeClock()
        g = make_governor(tmp_path, clock, lambda s: None, hourly_cap=2)
        g.before_request()
        g.before_request()
        clock.advance(3601)  # next hour bucket
        g.before_request()  # allowed again

    def test_daily_cap_blocks(self, tmp_path):
        clock = FakeClock()
        g = make_governor(tmp_path, clock, lambda s: None, daily_cap=2, hourly_cap=99)
        g.before_request()
        g.before_request()
        with pytest.raises(GovernorBlockedError, match="daily cap"):
            g.before_request()

    def test_mutation_budget_independent_of_reads(self, tmp_path):
        clock = FakeClock()
        g = make_governor(tmp_path, clock, lambda s: None,
                          hourly_cap=99, daily_cap=99, mutation_daily_cap=1)
        g.before_request(is_mutation=True)
        with pytest.raises(GovernorBlockedError, match="mutation budget"):
            g.before_request(is_mutation=True)
        g.before_request()  # reads still allowed
        assert g.status()["mutation_count"] == 1

    def test_disabled_governor_is_a_no_op(self, tmp_path):
        clock = FakeClock()
        g = make_governor(tmp_path, clock, lambda s: None, enabled=False,
                          hourly_cap=0)
        for _ in range(10):
            g.before_request()  # never raises, never sleeps


class TestCooldownAndPersistence:
    """Pins soft-block/checkpoint cooldown engagement and counter survival
    across a simulated process restart (same state file, fresh governor)."""

    def test_soft_block_flips_cooldown(self, tmp_path):
        clock = FakeClock()
        g = make_governor(tmp_path, clock, lambda s: None)
        g.observe_soft_block()
        with pytest.raises(GovernorBlockedError, match="cooldown"):
            g.before_request()
        assert g.status()["cooldown_active"] is True

    def test_cooldown_expires(self, tmp_path):
        clock = FakeClock()
        g = make_governor(tmp_path, clock, lambda s: None, cooldown_s=60)
        g.observe_soft_block()
        clock.advance(61)
        g.before_request()  # allowed again

    def test_checkpoint_disengages_for_hours(self, tmp_path):
        clock = FakeClock()
        g = make_governor(tmp_path, clock, lambda s: None)
        g.observe_checkpoint()
        s = g.status()
        assert s["cooldown_remaining_s"] > 5 * 3600

    def test_state_survives_a_process_restart(self, tmp_path):
        path = tmp_path / "gov.json"
        clock = FakeClock()
        g = make_governor(tmp_path, clock, lambda s: None)
        g.before_request()
        g.before_request(is_mutation=True)
        g.observe_soft_block()
        # a "new process": fresh governor, same state file
        clock2 = FakeClock(clock.now + 1)
        g2 = RequestGovernor(config=GovernorConfig(enabled=True), state_path=path,
                             sleeper=lambda s: None, clock=clock2)
        st = g2.status()
        assert st["day_count"] == 2
        assert st["mutation_count"] == 1
        assert st["cooldown_active"] is True
        with pytest.raises(GovernorBlockedError):
            g2.before_request()

    def test_day_rolls_and_resets_counters(self, tmp_path):
        clock = FakeClock()
        g = make_governor(tmp_path, clock, lambda s: None, hourly_cap=99)
        g.before_request()
        clock.advance(86400 + 100)  # next day
        st = g.status()
        assert st["day_count"] == 0
        assert st["hour_count"] == 0


class TestConfigEnv:
    """Pins FBK_GOVERNOR_* env overrides and the safe defaults kept on
    malformed values."""

    def test_env_overrides_apply(self, tmp_path):
        cfg = GovernorConfig.from_env({
            "FBK_GOVERNOR_MIN_GAP": "9",
            "FBK_GOVERNOR_HOURLY": "42",
            "FBK_GOVERNOR_MUTATION_DAILY": "7",
        })
        assert cfg.min_gap_s == 9.0
        assert cfg.hourly_cap == 42
        assert cfg.mutation_daily_cap == 7

    def test_env_off_disables(self):
        assert GovernorConfig.from_env({"FBK_GOVERNOR": "off"}).enabled is False

    def test_malformed_overrides_keep_safe_defaults(self):
        cfg = GovernorConfig.from_env({"FBK_GOVERNOR_HOURLY": "banana"})
        assert cfg.hourly_cap == GovernorConfig().hourly_cap


class TestSanitizeUA:
    """Pins UA sanitation: the HeadlessChrome token is rewritten, clean
    UAs pass through verbatim."""

    def test_headless_chrome_rewritten(self):
        assert "Headless" not in sanitize_user_agent(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) HeadlessChrome/152.0.0.0 Safari/537.36")
        assert "Chrome/152" in sanitize_user_agent(
            "…HeadlessChrome/152.0.0.0…")

    def test_clean_ua_untouched(self):
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")
        assert sanitize_user_agent(ua) == ua


class TestWarningDetection:
    """Pins ACCOUNT_WARNING marker scanning and its loud propagation into
    Bootstrap.markers_seen."""

    def test_account_warning_markers_scan(self):
        from auth.bootstrap import extract_account_warnings
        html = "some page … We suspect automated behaviour on your account …"
        assert extract_account_warnings(html) == [ACCOUNT_WARNING_MARKERS[0]]
        assert extract_account_warnings("a clean page") == []

    def test_warning_bubbles_into_bootstrap_markers(self):
        from auth.bootstrap import bootstrap_homepage
        from tests.fakes import StubTransportResponse

        html = ('CurrentUserInitialData DTSGInitData '
                '"is_checkpointed":false '
                'We suspect automated behaviour')

        class PageStub:
            cookies: ClassVar[dict[str, str]] = {"c_user": "1", "xs": "x", "datr": "d"}

            def get(self, url, **kw):
                return StubTransportResponse(
                    text=html, url="https://www.facebook.com/")

        boot = bootstrap_homepage(PageStub(), {"c_user": "1", "xs": "x", "datr": "d"})
        assert any(m.startswith("ACCOUNT_WARNING:") for m in boot.markers_seen)


class TestTransportWiring:
    """Pins FBTransport's governor feedback: empty-200 and 429 responses
    flip the soft-block state (docs/11 §5 containment)."""

    def test_transport_observes_empty_200(self, tmp_path):
        from transport.session import FBTransport

        clock = FakeClock()
        gov = make_governor(tmp_path, clock, lambda s: None)
        transport = FBTransport({"c_user": "1", "xs": "x"}, journal=None,
                                timeout=5, impersonate="chrome136", governor=gov)

        class Empty200:
            status_code = 200
            content = b""

        transport._observe_governor_response(Empty200())
        assert gov.status()["soft_blocks_seen"] == 1
        with pytest.raises(GovernorBlockedError):
            gov.before_request()

    def test_transport_observes_429(self, tmp_path):
        from transport.session import FBTransport

        clock = FakeClock()
        gov = make_governor(tmp_path, clock, lambda s: None)
        transport = FBTransport({"c_user": "1", "xs": "x"}, journal=None,
                                timeout=5, impersonate="chrome136", governor=gov)

        class RateLimited:
            status_code = 429
            content = b"no"

        transport._observe_governor_response(RateLimited())
        assert gov.status()["soft_blocks_seen"] == 1

    def test_default_governor_singleton(self, tmp_path):
        reset_governor_for_tests()
        g1 = default_governor(tmp_path / "s.json")
        g2 = default_governor()
        assert g1 is g2
        reset_governor_for_tests()


class TestGovernorCommand:
    """Pins the ``governor status`` CLI command against the singleton."""

    def test_governor_status_command_runs(self, capsys):
        import argparse

        from commands.governor import cmd_status

        args = argparse.Namespace(command="governor", root=None, cookies=None,
                                  no_journal=True, as_json=True,
                                  governor_command="status")
        assert cmd_status(args) == 0
        out = capsys.readouterr().out
        assert "requests" in out or "enabled" in out
