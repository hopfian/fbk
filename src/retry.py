"""Command-layer retry policy (docs/10 §7 backoff, docs/11 §5 pacing).

Deliberately NOT inside the transport: whether — and how — a failed call
should be retried is a per-command decision. A read-only command can
safely re-run after a soft block, while a mutation may prefer to fail
fast rather than double-send. commands/common.py therefore wires this
module around each command fn (``--retry N``); RateLimitedError is the
only typed error retried by default — session/checkpoint failures never
clear on their own (docs/10 §7 symptom catalogue), and retrying into a
soft block converts it into a hard one (docs/11 §5).

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from graphql.errors import RateLimitedError

T = TypeVar("T")

#: Delays never drop below this floor, even under heavy negative jitter —
#: a sub-250ms retry cadence is itself a machine-precision signal.
_MIN_DELAY_S = 0.25


@dataclass
class RetryPolicy:
    """Bounded exponential-backoff policy (docs/10 §7, docs/15 §P8-3).

    Encapsulates how many times a command may retry a failed call and how
    long to wait between attempts. The defaults are deliberately narrow —
    retry is a scalpel for transient RateLimitedError blips, not a
    hammer for enforcement: escalation is answered with backoff and
    stop, never persistence (docs/11 §5).
    """

    max_retries: int = 2                # attempts AFTER the first failure; 0 = fail fast
    base_delay_s: float = 2.0           # first backoff; doubles per attempt
    max_delay_s: float = 30.0           # exponential-growth ceiling
    jitter_cv: float = 0.5              # jitter spread as a fraction of the delay
    retry_on: tuple[type[BaseException], ...] = (RateLimitedError,)  # only retry
                                         # errors that can plausibly clear alone

    def delay_for(self, attempt: int) -> float:
        """Compute the bounded, jittered backoff for an attempt index.

        Formula (§5.3): ``delay = min(base * 2**attempt, max_delay_s)``,
        then symmetric uniform jitter of ``±cv * delay`` (§5.5: jitter
        keeps retries off a metronomic schedule), then clamped to the
        ``_MIN_DELAY_S`` floor.

        Args:
            attempt: Zero-based attempt index — the delay after the
                FIRST failure is ``base_delay_s``.

        Returns:
            Seconds to sleep before the next attempt, always in
            ``[0.25, max_delay_s + spread]``.
        """
        # Bounded exponential backoff: base * 2^n capped at max_delay_s. The
        # local needs an explicit annotation because current typeshed types
        # ``int.__pow__`` as ``-> Any`` (a negative exponent yields float, so
        # it cannot commit to int); ``attempt`` is always >= 0 here, so the
        # delay is a plain float either way.
        delay: float = min(self.base_delay_s * (2 ** attempt), self.max_delay_s)
        spread = delay * self.jitter_cv
        if spread > 0:
            delay += random.uniform(-spread, spread)
        # negative jitter can push near zero — the floor keeps it human-scale
        return max(delay, _MIN_DELAY_S)


def run_with_retry(fn: Callable[[], T], policy: RetryPolicy, *,
                   sleeper: Callable[[float], None] | None = None) -> T:
    """Run fn(); on an exception matching policy.retry_on, sleep
    delay_for(attempt) and retry up to max_retries times; otherwise
    re-raises. Returns fn()'s value. Attempts counted from 0.

    Args:
        fn: Zero-argument callable producing the command's result.
        policy: Retry budget and pacing (``--retry N`` maps to
            ``max_retries``).
        sleeper: Injected sleep function; resolved at call time (not
            import time) so tests can monkeypatch ``fbk.retry.time.sleep``
            globally as well as inject.

    Returns:
        Whatever ``fn()`` returns on its first non-matching or final
        successful attempt.

    Raises:
        BaseException: Whatever ``fn()`` raised, once the retry budget is
            exhausted or the error is not a member of
            ``policy.retry_on`` — non-matching errors (auth, checkpoint,
            KeyboardInterrupt) propagate on the FIRST occurrence, never
            silently swallowed.
    """
    if sleeper is None:
        sleeper = time.sleep
    attempt = 0
    while True:
        try:
            return fn()
        except BaseException as exc:
            if not isinstance(exc, policy.retry_on) \
                    or attempt >= policy.max_retries:
                raise
            sleeper(policy.delay_for(attempt))
            attempt += 1
