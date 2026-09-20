"""Governor lognormal mean preservation (just-landed fix, wave 2).

The sampler must apply the mean-preserving correction
``mu = ln(mean) - sigma^2/2``; without it E[gap] drifts up to
``mean * exp(sigma^2/2)`` — ~1.22x for cv=0.7, a 22% overshoot that
silently inflates effective request volume and skews the whole pacing
envelope.

Complements test_governor.py (floors/caps/cooldowns) and
test_governor_discipline.py (warm-up/diurnal/persistence). Offline: a
frozen FakeClock + recorder sleeper + seeded RNG; ~20k samples per
draw; no network.
"""
from __future__ import annotations

import random
import statistics

import pytest

from governor import GovernorConfig, RequestGovernor


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


def draw_gaps(n: int, *, mean_gap_s: float = 12.0, cv: float = 0.7,
              seed: int = 20260919) -> list[float]:
    """Sample ``n`` inter-arrival gaps from the governor's own sampler.

    The clock is frozen and the sleeper records, so every slept value IS
    one raw lognormal draw (floor 0.0, warm-up off, caps wide open, no
    state file). The RNG is seeded for determinism.
    """
    cfg = GovernorConfig(enabled=True, min_gap_s=0.0, mean_gap_s=mean_gap_s,
                         gap_cv=cv, hourly_cap=n + 2, daily_cap=n + 2,
                         mutation_daily_cap=n + 2, warmup_requests=0)
    slept: list[float] = []
    g = RequestGovernor(config=cfg, state_path=None,
                        sleeper=slept.append, clock=FakeClock())
    g._rng = random.Random(seed)
    for _ in range(n + 1):  # the first request of a day never waits
        g.before_request()
    assert len(slept) == n  # every later request slept exactly its gap
    return slept


class TestLognormalMeanPreservation:
    """Pins E[gap] == mean_gap_s (the sigma^2/2 correction): ~20k samples
    land within a few percent of 12s — the uncorrected sampler's ~22%
    overshoot (~14.6s) fails every assertion here."""

    def test_sample_mean_matches_configured_mean(self):
        gaps = draw_gaps(20_000)
        mean = statistics.fmean(gaps)
        assert abs(mean - 12.0) / 12.0 < 0.025  # within 2.5%

    def test_pre_fix_drift_is_excluded(self):
        """The correction's reason to exist: 12*exp(ln(1.49)/2) is ~14.64s
        — the sampler must sit far below the uncorrected drift."""
        gaps = draw_gaps(20_000)
        assert statistics.fmean(gaps) < 12.0 * 1.10  # pre-fix ~14.6 fails

    @pytest.mark.parametrize("cv", [0.2, 0.5, 1.0])
    def test_mean_preserved_across_cvs(self, cv):
        """The correction holds for every CV, not just the default 0.7."""
        gaps = draw_gaps(20_000, cv=cv)
        mean = statistics.fmean(gaps)
        assert abs(mean - 12.0) / 12.0 < 0.04

    def test_two_seeds_agree_within_tolerance(self):
        """No lucky-seed fluke: independent seeds land the same mean."""
        for seed in (1, 42):
            gaps = draw_gaps(20_000, seed=seed)
            assert abs(statistics.fmean(gaps) - 12.0) / 12.0 < 0.025

    def test_shape_stays_heavy_tailed(self):
        """The mean fix must not flatten the distribution: lognormal
        inter-arrivals are right-skewed (median < mean), never metronomic."""
        gaps = draw_gaps(20_000)
        assert statistics.median(gaps) < statistics.fmean(gaps)
        assert statistics.pstdev(gaps) > 0.0
