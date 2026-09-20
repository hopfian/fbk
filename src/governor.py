"""Adaptive request governor — the docs/10 §7 + docs/11 §8 discipline, made
structural instead of behavioural.

WHY THIS MODULE EXISTS (docs/15 §P8-1):

  During live calibration Phase 8, ~47 requests dispatched at a metronomic
  2.4-second mean interval over two minutes triggered a server-side session
  kill and escalated to an account-level "automated behaviour" warning
  (``constants.ACCOUNT_WARNING_MARKERS``). The only proven protective
  mechanism is volume and pacing discipline enforced at the architecture
  level; this module makes it impossible for any surface, command, or test
  to bypass that discipline by accident.

POLICY (all conservative defaults, individually overridable via
``FBK_GOVERNOR_*`` environment variables — see :meth:`GovernorConfig.from_env`):

  * Every request waits a lognormal inter-arrival gap:
      floor=4s, mean=12s, CV=0.7 — heavy-tailed, never metronomic.
  * Hourly and daily request caps abort batches before they execute
    (``GovernorBlockedError`` — a typed sentinel, not a silent skip).
  * Mutations share a separate, much lower daily budget aligned with the
    action ceilings in docs/10 §2.
  * A Warmup Curve (Phase 10, docs/16 §5) gates the first 8 requests of
    each calendar day at 2x the mean gap — a session that launches at full
    throughput from a cold start is itself a detectable anomaly.
  * A diurnal gate (``FBK_GOVERNOR_QUIET_HOURS``, Phase 10, docs/16 §5)
    can confine automation to an active window; the permissive default
    leaves the operator's own schedule as the coherence-safe choice
    (docs/11 §1 activity-schedule axis).
  * Any ``RateLimitedError``, empty-200 soft-block, or checkpoint signal
    engages a persistent 15-minute cooldown that blocks all subsequent
    calls. The correct response to enforcement is disengagement, not
    escalating retries (docs/11 §5 containment principle).

THREAD-SAFETY (docs/10 §7 invariant):

  One process-wide instance is shared by the HTTP transport, the GraphQL
  client, and the registry-harvest worker pool
  (:func:`default_governor`). All state mutation — pacing, counter
  increments, cooldown flips, status reads, and the singleton itself —
  happens under ``_lock`` / ``_GLOBAL_LOCK``. The lock is deliberately held
  across the pacing sleep so concurrent callers queue behind it and
  inter-arrival gaps stay correct under parallelism.

STATE PERSISTENCE:

  ``GovernorState`` serialises to ``cli/state/governor_state.json`` so
  hourly/daily counters and cooldowns survive process restarts within a
  calendar day. The write is atomic (temp file + ``os.replace``): a
  crash-truncated state file would fail-soft reset every cap and cooldown
  on the next launch — silently forgetting discipline exactly when the
  platform is already suspicious (see :meth:`RequestGovernor._persist`).

Efficacy honesty (docs/16 §4): gap/cap/cooldown discipline is the proven
lever — the only signal that has ever fired on this project was volume
plus metronomic timing (docs/15 §P8-1, docs/16 §P11-3). It is not magic
cloaking; the governor's job is to keep the client's behavioural
fingerprint inside human-plausible envelopes.

USER-DOC ANCHOR: cli/docs/09-safety-and-opsec.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class GovernorBlockedError(RuntimeError):
    """The governor refuses this request: caps hit or cooldown active.

    Operators should treat this as a STOP signal, not a retry signal —
    backing off further is the correct response (docs/11 §5 containment).
    """


@dataclass
class GovernorConfig:
    """Conservative-by-design request pacing parameters (docs/10 §7).

    Encapsulates the full behavioural policy of :class:`RequestGovernor`.
    All numeric defaults are grounded in the Phase-8 live-calibration
    finding (docs/15 §P8-1): the only proven protective lever is keeping
    request inter-arrival distributions inside human-plausible envelopes.

    All fields are individually overridable via ``FBK_GOVERNOR_*``
    environment variables; see :meth:`GovernorConfig.from_env`.
    """

    min_gap_s: float = 4.0            # absolute floor between ANY two requests
    mean_gap_s: float = 12.0          # lognormal inter-arrival mean
    gap_cv: float = 0.7              # human-ish heavy tail (never metronomic)
    hourly_cap: int = 120            # requests per rolling hour
    daily_cap: int = 500             # requests per calendar day
    mutation_daily_cap: int = 40      # mutations per calendar day (docs/10 §2
                                      # write ceilings are 1-2 orders below read)
    cooldown_s: float = 15 * 60.0    # disengagement after enforcement signal
    enabled: bool = True              # master kill-switch (FBK_GOVERNOR=off)
    # Behavioral hygiene (docs/16 §5, Phase 10):
    quiet_hours: str = "0-24"         # active window in local hours "H-H";
                                      # "off"/"0-24" disables gating — a 24h-scale
                                      # activity pattern is the human baseline, so
                                      # the operator's own schedule is the
                                      # coherence-safe default (docs/11 §1)
    warmup_requests: int = 8          # first N requests of the day get a
                                      # gentler gap (2x mean) — a session that
                                      # starts at full speed from silence is
                                      # itself an anomaly (docs/11 §3 ramp curves)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> GovernorConfig:
        """Build a config from ``FBK_GOVERNOR_*`` environment overrides.

        The ``FBK_GOVERNOR`` master switch accepts the falsy spellings
        ``off``/``0``/``false``/``no`` (case-insensitive); every other
        ``FBK_GOVERNOR_<FIELD>`` variable is cast to the field's type and
        applied only when present and well-formed.

        Args:
            environ: Explicit environment mapping; defaults to
                ``os.environ``. Injected by tests and by
                ``--no-journal``-style isolation contexts.

        Returns:
            A fully resolved config: defaults wherever an override is
            absent or malformed.

        Note:
            A malformed override is silently ignored in favor of the safe
            default (§5.5): a governor that crashes on bad env would take
            the whole transport down with it, and an unsafe default after
            a parse error is the strictly worse failure mode.
        """
        import os

        env = environ if environ is not None else dict(os.environ)
        cfg = cls()
        if env.get("FBK_GOVERNOR", "").lower() in ("off", "0", "false", "no"):
            cfg.enabled = False
        overrides = {
            "FBK_GOVERNOR_MIN_GAP": ("min_gap_s", float),
            "FBK_GOVERNOR_MEAN_GAP": ("mean_gap_s", float),
            "FBK_GOVERNOR_HOURLY": ("hourly_cap", int),
            "FBK_GOVERNOR_DAILY": ("daily_cap", int),
            "FBK_GOVERNOR_MUTATION_DAILY": ("mutation_daily_cap", int),
            "FBK_GOVERNOR_COOLDOWN": ("cooldown_s", float),
            "FBK_GOVERNOR_QUIET_HOURS": ("quiet_hours", str),
            "FBK_GOVERNOR_WARMUP": ("warmup_requests", int),
        }
        for key, (attr, convert) in overrides.items():
            if env.get(key):
                try:
                    # convert coerces the env STRING into the field's own type
                    setattr(cfg, attr, convert(env[key]))
                except (TypeError, ValueError):
                    continue  # malformed override: keep the safe default
        return cfg


@dataclass
class GovernorState:
    """Persisted request counters and cooldown deadline (the on-disk JSON
    contract of ``cli/state/governor_state.json``).

    Day and hour counters roll lazily on the next gate tick — no timer
    thread, no wall-clock dependency; a process that sleeps across a
    boundary wakes up to freshly zeroed budgets.
    """

    day: str = ""                     # YYYY-MM-DD the counters belong to
    hour_bucket: str = ""             # YYYY-MM-DD-HH
    hour_count: int = 0
    day_count: int = 0
    mutation_count: int = 0
    last_request_ts: float = 0.0      # epoch seconds; anchors inter-arrival gaps
    cooldown_until: float = 0.0       # epoch seconds; 0 = no cooldown active
    soft_blocks_seen: int = 0

    def _roll(self, now: float) -> None:
        """Reset counters whose calendar bucket has passed ``now``.

        Args:
            now: Epoch seconds from the injected clock (localtime-derived
                buckets, matching the diurnal gate's frame of reference).
        """
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        hour = time.strftime("%Y-%m-%d-%H", time.localtime(now))
        if day != self.day:
            self.day, self.day_count, self.mutation_count = day, 0, 0
        if hour != self.hour_bucket:
            self.hour_bucket, self.hour_count = hour, 0


class RequestGovernor:
    """One per process, shared by the transport, the GraphQL client, and
    the registry-harvest pool (:func:`default_governor`).

    Thread-safe (docs/10 §7): all state mutation happens under ``_lock``,
    including the signal observers and :meth:`status`. The lock is
    deliberately held across the pacing sleep so concurrent callers queue
    behind it — inter-arrival gaps stay correct even when the governor is
    shared by a listener thread, a harvest worker pool, and the CLI thread.
    """

    def __init__(self, config: GovernorConfig | None = None,
                 state_path: Path | str | None = None, *,
                 sleeper: Callable[[float], None] | None = None,
                 clock: Callable[[], float] | None = None):
        """Wire the governor together and load any persisted state.

        Args:
            config: Pacing policy; defaults to :meth:`GovernorConfig.from_env`
                (i.e. ``FBK_GOVERNOR_*`` overrides apply).
            state_path: JSON file the counters persist to; ``None`` keeps
                the governor purely in-memory (used by tests and by
                ``--no-journal`` runs that must not touch shared state).
            sleeper: Injected sleep function (tests pass a no-op recorder).
            clock: Injected clock (tests pass a deterministic counter).

        Note:
            A corrupt or hand-edited state file fails SOFT to a fresh
            ``GovernorState()`` (§5.5): a governor that crashed on bad
            state would take every command down with it, and the next
            successful ``_persist()`` repairs the file.
        """
        self.config = config or GovernorConfig.from_env()
        self._sleep = sleeper or time.sleep
        self._clock = clock or time.time
        self.state_path = Path(state_path) if state_path else None
        self._rng = random.Random()  # process-seeded; gaps stay heavy-tailed
        self._lock = threading.Lock()  # harvest pools share one governor
        self._warned: set[str] = set()
        if self.state_path and self.state_path.is_file():
            try:
                raw = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.state = GovernorState(**{
                    k: v for k, v in raw.items()
                    if k in GovernorState.__dataclass_fields__})
            except (json.JSONDecodeError, TypeError, ValueError):
                self.state = GovernorState()
        else:
            self.state = GovernorState()

    # ------------------------------------------------------------------ guards
    def _gate_quiet_hours(self) -> None:
        """Block requests outside the configured active window.

        Diurnal gating (Phase 10, docs/16 §5, docs/11 §3 diurnal gates): a
        client active 24/7/365 is the strongest single-account behavioral
        anomaly a scorer can see. The gate must be called under ``_lock``
        (it reads only config + clock, but callers serialize on it with
        every other gate stage).

        Raises:
            GovernorBlockedError: When the local hour falls outside the
                configured window. This is a STOP signal — the operator
                overrides with ``FBK_GOVERNOR_QUIET_HOURS`` or resumes
                inside the window; retrying immediately is the wrong
                response (docs/11 §5).
        """
        spec = (self.config.quiet_hours or "0-24").strip().lower()
        if spec in ("off", "0-24", "24", "always"):
            return
        try:
            lo_s, hi_s = spec.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
        except (ValueError, AttributeError):
            return  # malformed spec: keep the permissive default
        hour = int(time.strftime("%H", time.localtime(self._clock())))
        # lo > hi means an overnight window (e.g. "22-6"): inside is
        # hour >= lo OR hour < hi, not the plain between test.
        inside = (lo <= hi and (lo <= hour < hi)) or (lo > hi and (hour >= lo or hour < hi))
        if not inside:
            raise GovernorBlockedError(
                f"outside active window {self.config.quiet_hours!r} "
                f"(local hour {hour}) — set FBK_GOVERNOR_QUIET_HOURS "
                f"to override for this run")

    def before_request(self, *, is_mutation: bool = False,
                       quiet: bool = False) -> float:
        """Gate one request: roll counters, enforce cooldown/caps, pace.

        Args:
            is_mutation: The request carries a state-mutating GraphQL call —
                decrements the separate, much lower mutation budget
                (docs/10 §2 write/read asymmetry).
            quiet: Suppress the one-time operator warning while a cooldown
                is active (used by non-interactive probes that expect the
                block).

        Returns:
            The gap in seconds actually slept (0.0 when no wait was
            needed — first request of a process, or the disabled governor).

        Raises:
            GovernorBlockedError: When the cooldown is active, an hourly or
                daily cap is hit, the mutation budget is exhausted, or the
                diurnal gate rejects the hour. The lock is held across the
                pacing sleep by design (see class docstring).
        """
        if not self.config.enabled:
            return 0.0
        with self._lock:
            return self._before_request_locked(is_mutation=is_mutation, quiet=quiet)

    def _before_request_locked(self, *, is_mutation: bool, quiet: bool) -> float:
        """The gate body — caller must already hold ``_lock`` (§5.4).

        Order matters: cooldown → caps → diurnal gate → pacing sleep →
        counter increments → persist. Caps are checked BEFORE any sleep so
        a blocked batch aborts without burning wall-clock time inside the
        lock.
        """
        now = self._clock()
        self.state._roll(now)

        if now < self.state.cooldown_until:
            remaining = int(self.state.cooldown_until - now)
            if not quiet:
                self._warn_once(
                    "cooldown",
                    f"governor: cooldown active for {remaining}s — disengaging "
                    "(docs/11 §5 containment: do NOT hammer through this)")
            raise GovernorBlockedError(f"cooldown active for {remaining}s")
        if self.state.hour_count >= self.config.hourly_cap:
            raise GovernorBlockedError(
                f"hourly cap reached ({self.config.hourly_cap}/h) — resume next hour")
        if self.state.day_count >= self.config.daily_cap:
            raise GovernorBlockedError(
                f"daily cap reached ({self.config.daily_cap}/day) — resume tomorrow")
        if is_mutation and self.state.mutation_count >= self.config.mutation_daily_cap:
            raise GovernorBlockedError(
                f"mutation budget exhausted "
                f"({self.config.mutation_daily_cap}/day) — reads still allowed")

        # diurnal gate (Phase 10): a 24/7-active client is an anomaly;
        # the default active window keeps automation inside waking hours
        self._gate_quiet_hours()

        # inter-arrival discipline: lognormal above the floor, never uniform
        slept = 0.0
        if self.state.last_request_ts > 0:
            import math

            # warm-up (Phase 10, docs/16 §5): the first requests of a fresh
            # day are gentler — a session ramping from silence to full speed
            # is itself an anomaly (docs/11 §3 ramp curves)
            mean_gap = self.config.mean_gap_s
            if self.state.day_count < self.config.warmup_requests:
                mean_gap *= 2.0

            # Lognormal inter-arrival sampling (docs/10 §7, docs/15 §P6-4
            # harness convention):
            #   mu    = ln(mean_gap) - sigma^2/2
            #   sigma = sqrt(ln(1 + cv^2))
            # The mean-preserving correction term guarantees E[gap] =
            # mean_gap_s exactly (docs/11 §8 scheduler invariant); without
            # it E[gap] drifts to mean*exp(sigma^2/2) (~1.22x for cv=0.7)
            # — the min-gap floor still bounds the left tail and the CV
            # keeps the shape heavy-tailed, never metronomic (the exact
            # Phase-8 trigger, docs/15 §P8-1).
            sigma = math.sqrt(math.log(1 + self.config.gap_cv ** 2))
            mu = math.log(mean_gap) - 0.5 * sigma ** 2
            raw = self._rng.lognormvariate(mu, sigma)
            gap = max(self.config.min_gap_s, raw)
            elapsed = now - self.state.last_request_ts
            if elapsed < gap:
                slept = gap - elapsed
                self._sleep(slept)

        self.state.last_request_ts = self._clock()
        self.state.hour_count += 1
        self.state.day_count += 1
        if is_mutation:
            self.state.mutation_count += 1
        self._persist()
        return slept

    # ----------------------------------------------------------------- signals
    def observe_soft_block(self) -> None:
        """Record an enforcement signal and start a full cooldown.

        Called by the transport/GraphQL layer on ``RateLimitedError``,
        empty-200 soft-blocks, and related signals. Escalation is answered
        with disengagement: every subsequent gate tick raises until the
        cooldown expires (docs/11 §5 containment — never hammer through).

        Note:
            Thread-safe (§5.4): takes ``_lock`` because a listener thread
            can observe a soft-block concurrently with the CLI thread's
            next gate tick.
        """
        with self._lock:
            self.state.soft_blocks_seen += 1
            self.state.cooldown_until = self._clock() + self.config.cooldown_s
            self._persist()

    def observe_checkpoint(self) -> None:
        """Record a checkpoint: disengage for the day (6 hours).

        A checkpoint is strictly worse than a soft block — it is the rung-4
        challenge tier (docs/10 §7), so the cooldown outlasts any plausible
        challenge window instead of the soft-block default.

        Note:
            Thread-safe (§5.4): takes ``_lock`` for the same reason as
            :meth:`observe_soft_block`.
        """
        with self._lock:
            self.state.cooldown_until = self._clock() + 6 * 3600.0
            self._persist()

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        """A snapshot of every counter, cap, and cooldown for the operator.

        Returns:
            A plain dict of config + rolled state: ``enabled``, counter /
            cap pairs (``hour_count``/``hourly_cap``, ``day_count``/
            ``daily_cap``, ``mutation_count``/``mutation_daily_cap``),
            ``soft_blocks_seen``, ``cooldown_active`` and remaining
            seconds, the diurnal window (``active_window`` /
            ``in_active_window``), and ``warmup_requests``. Token and
            cookie material never appears here — the report is
            journal-safe by construction.

        Note:
            Thread-safe (§5.4): reads under ``_lock`` after rolling the
            counters, so concurrent gate ticks cannot tear the snapshot.
        """
        now = self._clock()
        with self._lock:
            self.state._roll(now)
            try:
                self._gate_quiet_hours()
                in_window = True
            except GovernorBlockedError:
                in_window = False
            return {
                "enabled": self.config.enabled,
                "hour_count": self.state.hour_count,
                "hourly_cap": self.config.hourly_cap,
                "day_count": self.state.day_count,
                "daily_cap": self.config.daily_cap,
                "mutation_count": self.state.mutation_count,
                "mutation_daily_cap": self.config.mutation_daily_cap,
                "soft_blocks_seen": self.state.soft_blocks_seen,
                "cooldown_active": now < self.state.cooldown_until,
                "cooldown_remaining_s": max(0, int(self.state.cooldown_until - now)),
                "active_window": self.config.quiet_hours,
                "in_active_window": in_window,
                "warmup_requests": self.config.warmup_requests,
            }

    # ------------------------------------------------------------------- plumbing
    def _persist(self) -> None:
        """Write the state file atomically (best-effort).

        Caller must hold ``_lock`` — every mutation path calls this under
        it, which serializes writers by construction.
        """
        if not self.state_path:
            return
        # ATOMIC replace (temp + os.replace): a truncate-in-place write
        # interrupted by a crash would corrupt the JSON, and the next
        # process would fail-soft reset to a fresh GovernorState() —
        # silently FORGETTING hourly/daily caps and any active cooldown,
        # which weakens the discipline exactly when the platform is
        # already suspicious. os.replace is atomic on POSIX and Windows.
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self.state.__dict__, indent=1), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError:
            pass  # persistence is best-effort; the in-memory gate still holds

    def _warn_once(self, key: str, message: str) -> None:
        """Print ``message`` the first time ``key`` fires (per process)."""
        if key not in self._warned:
            self._warned.add(key)
            print(message)


_GLOBAL: RequestGovernor | None = None
_GLOBAL_LOCK = threading.Lock()


def default_governor(state_path: Path | str | None = None) -> RequestGovernor:
    """Return the process-wide governor, constructing it on first call.

    The transport, the GraphQL client, and the harvest pool all share this
    one instance so there is exactly one pacing queue per process — a
    second governor would double the realized request rate. The default
    state path derives from :meth:`config.Config.discover` so the
    persistent counters land in the same ``state/`` directory as the
    journals.

    Args:
        state_path: Explicit state file; defaults to
            ``<cli/>state/governor_state.json``. Ignored on calls after the
            first — the singleton is immutable once constructed.

    Note:
        Thread-safe (§5.4): the singleton is constructed under
        ``_GLOBAL_LOCK``; concurrent first callers get the same instance.
    """
    global _GLOBAL
    with _GLOBAL_LOCK:
        if _GLOBAL is None:
            if state_path is None:
                from config import Config
                state_path = (Config.discover().journal_dir / "governor_state.json")
            _GLOBAL = RequestGovernor(state_path=state_path)
        return _GLOBAL


def reset_governor_for_tests() -> None:
    """Test hook: drop the cached singleton (never call in production)."""
    global _GLOBAL
    with _GLOBAL_LOCK:
        _GLOBAL = None
