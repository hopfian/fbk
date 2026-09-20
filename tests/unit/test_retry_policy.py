"""RetryPolicy boundary + custom-taxonomy tests (docs/10 §7 backoff).

Complements test_retry_overview.py: covers the defaults, the exact
cap boundary, custom retry_on tuples, and the 0.25s floor under a
worst-case negative jitter (monkeypatched RNG for determinism).
"""
from __future__ import annotations

import pytest

import retry as retry_module
from graphql.errors import RateLimitedError
from retry import RetryPolicy, run_with_retry


class TestDefaults:
    """Pins the conservative default policy shape (docs/10 §7)."""

    def test_default_policy_shape(self):
        p = RetryPolicy()
        assert p.max_retries == 2
        assert p.base_delay_s == 2.0
        assert p.max_delay_s == 30.0
        assert p.jitter_cv == 0.5
        assert p.retry_on == (RateLimitedError,)


class TestDelayBoundaries:
    """Pins the exact cap boundary and the floor/ceiling survival under
    worst-case jitter draws (monkeypatched RNG for determinism)."""

    def test_cap_hit_exactly_at_the_boundary(self):
        """base * 2^attempt == max: the min() keeps it, never overshoots."""
        p = RetryPolicy(base_delay_s=2.0, max_delay_s=16.0, jitter_cv=0.0)
        assert p.delay_for(3) == 16.0
        assert p.delay_for(4) == 16.0  # already at the cap

    def test_floor_survives_worst_case_negative_jitter(self, monkeypatch):
        monkeypatch.setattr(retry_module.random, "uniform",
                            lambda lo, hi: lo)  # always draw -spread
        p = RetryPolicy(base_delay_s=0.5, max_delay_s=1.0, jitter_cv=1.0)
        assert p.delay_for(0) == 0.25  # clamped at _MIN_DELAY_S

    def test_ceiling_survives_worst_case_positive_jitter(self, monkeypatch):
        monkeypatch.setattr(retry_module.random, "uniform",
                            lambda lo, hi: hi)  # always draw +spread
        p = RetryPolicy(base_delay_s=1.0, max_delay_s=1.0, jitter_cv=1.0)
        assert p.delay_for(0) == 2.0  # 1.0 + full spread, above the cap


class TestRunWithRetryTaxonomy:
    """Pins custom retry_on taxonomies, max_retries=0 fail-fast, sentinel
    result passthrough, and KeyboardInterrupt transparency."""

    def test_custom_retry_on_retries_the_configured_type(self):
        p = RetryPolicy(max_retries=2, retry_on=(ValueError,), jitter_cv=0.0)
        sleeps: list[float] = []
        calls = []

        def fn():
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("retry me")
            return "ok"

        assert run_with_retry(fn, p, sleeper=sleeps.append) == "ok"
        assert sleeps == [p.base_delay_s]

    def test_custom_retry_on_excludes_the_default(self):
        p = RetryPolicy(max_retries=3, retry_on=(ValueError,), jitter_cv=0.0)
        sleeps: list[float] = []
        calls = []

        def fn():
            calls.append(1)
            raise RateLimitedError("not in this policy's taxonomy")

        with pytest.raises(RateLimitedError):
            run_with_retry(fn, p, sleeper=sleeps.append)
        assert len(calls) == 1
        assert sleeps == []

    def test_max_retries_zero_fails_fast(self):
        p = RetryPolicy(max_retries=0, jitter_cv=0.0)
        sleeps: list[float] = []
        calls = []

        def fn():
            calls.append(1)
            raise RateLimitedError("one shot only")

        with pytest.raises(RateLimitedError):
            run_with_retry(fn, p, sleeper=sleeps.append)
        assert len(calls) == 1
        assert sleeps == []

    def test_result_object_flows_through_untouched(self):
        sentinel = {"data": {"viewer": {"ok": True}}}
        calls = []

        def fn():
            calls.append(1)
            if len(calls) == 1:
                raise RateLimitedError("transient")
            return sentinel

        out = run_with_retry(fn, RetryPolicy(jitter_cv=0.0), sleeper=lambda s: None)
        assert out is sentinel

    def test_keyboard_interrupt_is_never_swallowed(self):
        calls = []

        def fn():
            calls.append(1)
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            run_with_retry(fn, RetryPolicy(max_retries=5), sleeper=lambda s: None)
        assert len(calls) == 1

    def test_attempt_counter_starts_at_zero(self):
        p = RetryPolicy(max_retries=1, base_delay_s=1.0, jitter_cv=0.0)
        sleeps: list[float] = []
        calls = []

        def fn():
            calls.append(1)
            if len(calls) <= 1:
                raise RateLimitedError("b")
            return "ok"

        run_with_retry(fn, p, sleeper=sleeps.append)
        assert sleeps == [1.0]  # delay_for(0), the first backoff step
