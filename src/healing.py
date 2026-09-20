"""Self-healing coordinator — the recursive, terminating recovery layer.

WHY THIS MODULE EXISTS:

  fbk's failure modes are dominated by three decaying inputs: the doc_id
  registry (Facebook rotates persisted-query registrations on every build
  push, docs/13 §2, docs/04 §10), the persistent token cache (a corrupt or
  identity-mismatched entry), and the network itself (transient TLS/connect
  blips that no behavioural discipline can prevent). Before this module,
  each failure surfaced as a typed error with a MANUAL recovery action
  (``fbk registry refresh --save``, delete the cache file, rerun). This
  module executes those same recovery actions automatically, under the
  same discipline the manual path follows — paced by the governor, capped
  per invocation, and cooled down across invocations — and records every
  action it takes in a redacted JSONL log the operator can audit.

RECURSIVE-BUT-TERMINATING DESIGN:

  Healing is layered: a registry refresh bootstraps the live homepage,
  which may exercise the token-cache path, which may itself need the
  transport. Each layer may therefore trigger lower-level healing, and a
  naive implementation could recurse forever on a persistent fault. The
  :class:`HealingContext` makes termination structural, not hopeful:

  * per-kind attempt counters cap every healing kind per invocation
    (a kind that already fired its quota re-raises the original error),
  * the expensive kind (registry re-harvest) additionally carries a
    cross-invocation cooldown read from the healing log, so a broken
    deploy cannot turn every command into a harvest storm,
  * the coordinator never swallows the triggering error: healing either
    produces a recovery (the caller retries once with healed inputs) or
    the original typed error propagates untouched.

POLICY (defaults; every knob env-overridable, mirroring the governor's
``FBK_GOVERNOR_*`` convention):

  * Master switch ``FBK_HEAL`` — ``off`` disables every heal (tests,
    dry runs, debugging); healing is ON by default because every action
    it takes is one the documented manual procedure would take anyway.
  * Registry re-harvest is the only expensive heal: capped
    (``FBK_HEAL_MAX_BUNDLES``, default 60 bundles) and cooled down
    (``FBK_HEAL_REGISTRY_HOURS``, default 6) — one deploy roll per
    cooldown window is the realistic fault rate (docs/04 §10).
  * Transport-level retries exist only for connection-phase failures on
    READS; a mutation is never re-fired at the transport layer (a
    timeout after send cannot distinguish "request lost" from "response
    lost" — the double-post hazard outweighs the recovery, docs/11 §5).
  * Every heal event is appended to ``state/healing.jsonl`` (gitignored,
    redacted by construction — the log carries operation names, doc
    ids, and counts, never tokens) and mirrored to stderr so the
    operator sees the self-repair in real time.

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # heavy imports stay out of the eager graph (lazy discipline)
    from config import Config

# ── Healing-kind vocabulary ──────────────────────────────────────────────────
# Stable string tags used in the log, in stderr notes, and by the doctor's
# self-healing readout; the kind set is closed so log consumers can rely on it.
KIND_REGISTRY_REFRESH = "registry-refresh"   # governed re-harvest + v3 rewrite
KIND_DOC_ID_RETRY = "doc-id-retry"           # retry once on the fresh doc_id
KIND_TOKEN_CACHE_REBUILD = "token-cache-rebuild"  # corrupt cache -> bootstrap
KIND_TRANSPORT_RETRY = "transport-retry"     # read-only connection-phase retry
KIND_GOVERNOR_STATE_REBUILD = "governor-state-rebuild"  # corrupt counters reset
KIND_DOCTOR_FIX = "doctor-fix"               # `fbk doctor --fix` quarantine
KIND_COOKIE_JAR_HEAL = "cookie-jar-heal"     # tolerant reparse of a torn jar
KIND_BOOTSTRAP_RETRY = "bootstrap-retry"     # degenerate page -> one retry
KIND_REALTIME_RECONNECT = "realtime-reconnect"  # listen drop -> governed re-dial

#: Per-invocation attempt caps. The registry re-harvest and the doc-id retry
#: are one-shot recoveries per command run — a second failure means the heal
#: did not cure the fault and the typed error must reach the operator.
_INVOCATION_CAPS: dict[str, int] = {
    KIND_REGISTRY_REFRESH: 1,
    KIND_DOC_ID_RETRY: 1,
    KIND_TOKEN_CACHE_REBUILD: 2,
}

# ── Environment overrides (mirrors governor.GovernorConfig.from_env) ─────────
ENV_MASTER = "FBK_HEAL"                       # "off" disables every heal
ENV_REGISTRY_HOURS = "FBK_HEAL_REGISTRY_HOURS"   # cooldown between harvests
ENV_MAX_BUNDLES = "FBK_HEAL_MAX_BUNDLES"      # per-harvest bundle cap
ENV_TRANSPORT_RETRIES = "FBK_HEAL_TRANSPORT_RETRIES"  # read retries per call

DEFAULT_REGISTRY_HOURS = 6.0
DEFAULT_MAX_BUNDLES = 60
DEFAULT_TRANSPORT_RETRIES = 1
DEFAULT_REALTIME_RECONNECTS = 3

#: Realtime reconnect ceiling for one listen session (env-overridable via
#: FBK_HEAL_REALTIME_RECONNECTS; a persistent broker outage must terminate
#: the listen, not loop forever).
MAX_REALTIME_RECONNECTS = 10

#: Verification floor for a healed registry: a re-harvest producing fewer
#: pairs than this is treated as a degenerate parse (soft-blocked or
#: shape-drifted page) and rolled back — the shipped v3 carries 1031 pairs,
#: a healthy homepage harvest yields hundreds at minimum (docs/15 §P2-1).
MIN_HARVEST_PAIRS = 200


def _env_float(name: str, default: float) -> float:
    """Read one float env override; malformed values keep the safe default.

    The governor's from_env contract (malformed overrides are ignored in
    favor of the conservative default) is mirrored here so the two
    subsystems behave identically under operator misconfiguration.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _env_int(name: str, default: int) -> int:
    """Read one non-negative int env override; malformed keeps the default."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def healing_enabled() -> bool:
    """Whether the master switch permits healing for this process.

    ``FBK_HEAL=off`` (also ``0``/``false``) disables every heal; anything
    else — including the variable being unset — leaves healing enabled.
    """
    raw = os.environ.get(ENV_MASTER, "").strip().lower()
    return raw not in ("off", "0", "false")


def transport_retry_limit() -> int:
    """Read-only connection-phase retries the transport may spend per call."""
    if not healing_enabled():
        return 0
    return min(_env_int(ENV_TRANSPORT_RETRIES, DEFAULT_TRANSPORT_RETRIES), 3)


def realtime_reconnect_limit() -> int:
    """Bounded reconnects one listen session may spend on drops.

    ``FBK_HEAL_REALTIME_RECONNECTS`` overrides the default (3); values
    above the hard ceiling (10) clamp, malformed values keep the default,
    and the master switch off returns 0 — a listen under FBK_HEAL=off
    keeps its pre-healing single-dial behavior.
    """
    if not healing_enabled():
        return 0
    return min(_env_int("FBK_HEAL_REALTIME_RECONNECTS",
                        DEFAULT_REALTIME_RECONNECTS),
               MAX_REALTIME_RECONNECTS)


@dataclass(frozen=True)
class HealingEvent:
    """One recorded self-healing action (the ``healing.jsonl`` row shape).

    Represents a single recovery the coordinator executed (or attempted):
    what kind, what triggered it, and a redacted human summary. Only
    operation names, doc ids, error codes, and counts may enter
    ``trigger``/``detail`` — token values are structurally excluded by the
    callers' construction (the same discipline as the dry-run plan, docs/11
    §7), so the log is journal-safe without a redaction pass.
    """

    ts: float          # epoch seconds when the action was recorded
    kind: str          # one of the KIND_* vocabulary tags
    trigger: str       # the observed failure that fired the heal
    detail: str        # outcome summary (counts / new doc_id provenance)

    def as_dict(self) -> dict[str, Any]:
        """The JSONL-serializable view (insertion-ordered, log-stable)."""
        return {"ts": self.ts, "kind": self.kind,
                "trigger": self.trigger, "detail": self.detail}


class HealingLog:
    """Append-only, redacted JSONL record of self-healing actions.

    The log lives at ``state/healing.jsonl`` (the state directory is
    gitignored — docs/12 §4). Reads are fail-soft: a corrupt or unreadable
    log yields whatever prefix parses, because the log informs cooldowns
    and the doctor's readout but must never break a live command. The
    cooldown semantics make the append the durability point: a harvest
    that recorded its event cannot be re-triggered by a later invocation
    inside the cooldown window, even across process restarts.

    The log heals ITSELF (the recursive layer): growth is bounded by a
    self-prune on append — once the file passes :attr:`PRUNE_BYTES`, it
    is rewritten keeping only the newest :attr:`KEEP_ROWS` rows, so a
    long-lived checkout cannot grow the log without bound and the
    cooldown windows (which read only recent rows) stay correct. Pruning
    is itself fail-soft: a failed prune leaves the file untouched.
    """

    #: size threshold that triggers a self-prune on the next append
    PRUNE_BYTES = 256 * 1024
    #: rows retained by a prune (newest first — the cooldown read window)
    KEEP_ROWS = 400

    def __init__(self, path: Path | str):
        """Bind the log to one JSONL file (created lazily by append)."""
        self.path = Path(path)

    def append(self, kind: str, trigger: str, detail: str = "",
               *, ts: float | None = None) -> dict[str, Any]:
        """Record one healing event; returns the written row.

        Args:
            kind: One of the ``KIND_*`` vocabulary tags.
            trigger: The observed failure (error text minus any
                secret-bearing interpolation).
            detail: Outcome summary (e.g. harvest diff counts).
            ts: Explicit clock for tests; defaults to ``time.time()``.

        Returns:
            The row exactly as written (also what stderr mirrors).

        Note:
            Write failures are suppressed (§5.5 fail-soft): a read-only
            state directory degrades to stderr-only visibility — healing
            itself must never crash the command it is healing.
        """
        event = HealingEvent(ts=ts if ts is not None else time.time(),
                             kind=kind, trigger=trigger, detail=detail)
        row = json.dumps(event.as_dict(), ensure_ascii=False)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(row + "\n")
        except OSError:
            pass  # log-less healing: stderr mirror below is still emitted
        else:
            self._prune_if_large()
        print(f"[heal] {kind}: {trigger}"
              + (f" — {detail}" if detail else ""), file=sys.stderr)
        return event.as_dict()

    def _prune_if_large(self) -> None:
        """Self-prune once the log outgrows PRUNE_BYTES (fail-soft).

        The heal log healing itself: rewrite the file keeping only the
        newest KEEP_ROWS parseable rows. Torn tail lines are dropped by
        the same pass. Any I/O failure leaves the file exactly as it was
        — pruning is an optimization, never a correctness step.
        """
        try:
            if self.path.stat().st_size <= self.PRUNE_BYTES:
                return
            rows = self._rows()
            kept = rows[-self.KEEP_ROWS:]
            tmp = self.path.with_suffix(".jsonl.tmp")
            tmp.write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n"
                        for r in kept),
                encoding="utf-8")
            os.replace(tmp, self.path)
        except (OSError, TypeError, ValueError):
            pass  # unreadable/unwritable state dir: keep the fat log

    def _rows(self) -> list[dict[str, Any]]:
        """Every parseable row, oldest first (fail-soft prefix read)."""
        if not self.path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue  # torn tail line from a killed process: skip
            if isinstance(parsed, dict):
                rows.append(parsed)
        return rows

    def last_ts(self, kind: str) -> float | None:
        """Epoch seconds of the most recent event of ``kind``, or None.

        Args:
            kind: The ``KIND_*`` tag to search for.

        Returns:
            The newest matching row's ``ts``, or ``None`` when the kind
            never fired (the cooldown allow-path treats None as "allowed").
        """
        newest: float | None = None
        for row in self._rows():
            if row.get("kind") == kind:
                ts = row.get("ts")
                if isinstance(ts, (int, float)) and (newest is None or ts > newest):
                    newest = float(ts)
        return newest

    def last_event(self) -> dict[str, Any] | None:
        """The newest recorded event — the last parseable row — or None.

        Returns:
            The final parseable row in file order (the log is append-only,
            so the last line is the most recent event; the doctor's
            self-healing readout summarizes it), or ``None`` when the log
            is absent or no line parses (the doctor's torn-log signal).
        """
        rows = self._rows()
        return rows[-1] if rows else None

    def count_since(self, within_s: float, kind: str | None = None,
                    *, now: float | None = None) -> int:
        """How many events (optionally of one kind) fired inside the window.

        Args:
            within_s: Window length in seconds (e.g. 86400 for a day).
            kind: Restrict to one ``KIND_*`` tag; None counts all.
            now: Explicit clock for tests; defaults to ``time.time()``.

        Returns:
            The count of parseable rows inside the window — the doctor's
            self-healing readout aggregates with this.
        """
        clock = now if now is not None else time.time()
        return sum(1 for row in self._rows()
                   if isinstance(row.get("ts"), (int, float))
                   and clock - float(row["ts"]) <= within_s
                   and (kind is None or row.get("kind") == kind))


class HealingContext:
    """Per-invocation healing coordinator with termination guarantees.

    One context per Session. The attempt counters (``_INVOCATION_CAPS``)
    make the recursive layering terminate: a heal may trigger lower-level
    work (the harvest bootstraps, which may touch the cache path), but no
    kind can fire more than its cap per invocation, and the expensive
    harvest kind is additionally cooled down across invocations via the
    log. Every healing method returns a recovery artifact or ``None`` —
    never raises on its own behalf; the caller decides whether the
    original error propagates.
    """

    def __init__(self, config: Config, *, log_path: Path | str,
                 now: float | None = None):
        """Bind the coordinator to one config + healing log.

        Args:
            config: Resolved runtime config (assets/state directories).
            log_path: The ``healing.jsonl`` path (state/healing.jsonl).
            now: Explicit clock for tests; defaults to ``time.time()``.
        """
        self.config = config
        self.log = HealingLog(log_path)
        self._now: Callable[[], float] = (
            (lambda: now) if now is not None else time.time)
        self._attempts: dict[str, int] = {}

    def allow(self, kind: str) -> bool:
        """Whether ``kind`` may fire now (cap + cooldown + master switch).

        Args:
            kind: The ``KIND_*`` tag to admit.

        Returns:
            True only when healing is enabled, the invocation cap for the
            kind is unspent, and — for the harvest kind — the
            cross-invocation cooldown has elapsed. Unknown kinds are
            denied (closed vocabulary: a typo must fail visibly, not
            heal silently).
        """
        if not healing_enabled():
            return False
        if kind not in _INVOCATION_CAPS:
            return False
        if self._attempts.get(kind, 0) >= _INVOCATION_CAPS[kind]:
            return False
        if kind == KIND_REGISTRY_REFRESH:
            last = self.log.last_ts(kind)
            hours = _env_float(ENV_REGISTRY_HOURS, DEFAULT_REGISTRY_HOURS)
            if last is not None and self._now() - last < hours * 3600.0:
                return False
        return True

    def record(self, kind: str, trigger: str, detail: str = "") -> None:
        """Spend one attempt of ``kind`` and append its log row.

        Args:
            kind: The ``KIND_*`` tag (must be in the closed vocabulary —
                unknown kinds raise, mirroring :meth:`allow`'s denial).
            trigger: The observed failure that fired the heal.
            detail: Outcome summary (counts / provenance).
        """
        if kind not in _INVOCATION_CAPS:
            raise ValueError(f"unknown healing kind {kind!r}")
        self._attempts[kind] = self._attempts.get(kind, 0) + 1
        self.log.append(kind, trigger, detail)

    def log_event(self, kind: str, trigger: str, detail: str = "") -> None:
        """Record an uncapped, audit-only healing event (master-switch aware).

        The audit-only kinds (``governor-state-rebuild``,
        ``cookie-jar-heal``, ``bootstrap-retry`` — events describing
        self-repair that happened outside the invocation-capped flows) go
        through this: they honor the ``FBK_HEAL`` master switch but carry
        no per-invocation cap, because the repairs they record happen at
        construction/loading time, not in the call loop.
        """
        if not healing_enabled():
            return
        self.log.append(kind, trigger, detail)

    # ------------------------------------------------------------- the heals
    def refresh_registry(self) -> Any:
        """Re-harvest the live deploy's doc_ids and rewrite registry v3.

        The expensive heal: bootstraps the current homepage, harvests the
        deploy's rsrc.php bundles (governor-paced inside
        ``graphql.registry_refresh``), and writes ``doc_id_registry_v3.json``
        (v2 is never overwritten — docs/13 §2). The bundle cap keeps a
        single heal inside a bounded request envelope.

        The heal verifies ITSELF before declaring success: the previous
        v3 is backed up to ``doc_id_registry_v3.prev.json`` first, and the
        reloaded registry must clear :data:`MIN_HARVEST_PAIRS` — a
        degenerate harvest (a soft-blocked or shape-drifted page parsed
        into a handful of pairs) would otherwise replace a working
        registry with garbage. On any failure the backup is restored (or
        the fresh v3 dropped, when no previous file existed, falling back
        to v2 exactly as before the heal) and the event records the
        rollback. On success the FRESH registry object is returned for
        the caller to adopt; every failure path returns ``None`` so the
        caller's original typed error propagates untouched.

        Returns:
            The reloaded :class:`~graphql.registry.DocIdRegistry` after the
            verified heal, or ``None`` when the heal was refused/not
            attempted, the harvest failed, or verification rolled it back.
        """
        if not self.allow(KIND_REGISTRY_REFRESH):
            return None
        self._attempts[KIND_REGISTRY_REFRESH] = (
            self._attempts.get(KIND_REGISTRY_REFRESH, 0) + 1)
        from graphql.registry import DocIdRegistry  # lazy: heavy import chain
        current = Path(self.config.assets_dir) / "doc_id_registry_v3.json"
        backup = current.with_name("doc_id_registry_v3.prev.json")
        had_backup = False
        try:
            if current.is_file():
                # shutil.copy2: preserve bytes exactly — the rollback path
                # must restore the working registry bit-for-bit
                import shutil
                shutil.copy2(current, backup)
                had_backup = True
        except OSError as exc:
            # no backup -> no heal: overwriting the only good registry
            # without a rollback path trades a known-good state for a
            # maybe; the caller's typed error is the honest outcome
            self.log.append(KIND_REGISTRY_REFRESH,
                            "re-harvest skipped — no backup possible",
                            f"{type(exc).__name__}: {exc}")
            return None
        try:
            from graphql.registry_refresh import refresh_registry
            # 0 or a malformed override falls back to the default cap: an
            # uncapped harvest would fire hundreds of requests per heal,
            # exactly the volume anomaly the governor exists to prevent.
            max_bundles = _env_int(ENV_MAX_BUNDLES, DEFAULT_MAX_BUNDLES)
            if max_bundles <= 0:
                max_bundles = DEFAULT_MAX_BUNDLES
            diff = refresh_registry(self.config, save=True,
                                    max_bundles=max_bundles)
            detail = (f"added={len(diff.added)} changed={len(diff.changed)} "
                      f"bundles={diff.bundles_fetched} "
                      f"errors={diff.fetch_errors}")
        except Exception as exc:  # heal must never mask the caller's typed
            # error with a heal-engine crash; v3 was not written (refresh
            # saves only after a complete harvest), so nothing to roll back
            self.log.append(KIND_REGISTRY_REFRESH,
                            "re-harvest failed",
                            f"{type(exc).__name__}: {exc}")
            return None
        # verification: reload from disk and refuse a degenerate harvest
        try:
            fresh = DocIdRegistry.from_assets(self.config.assets_dir)
        except Exception as exc:
            self._rollback_v3(current, backup, had_backup)
            self.log.append(KIND_REGISTRY_REFRESH,
                            "harvest verification failed — rolled back",
                            f"{type(exc).__name__}: {exc}")
            return None
        pair_count = sum(1 for _ in fresh)
        if pair_count < MIN_HARVEST_PAIRS:
            self._rollback_v3(current, backup, had_backup)
            self.log.append(KIND_REGISTRY_REFRESH,
                            "degenerate harvest refused — rolled back",
                            f"{pair_count} pairs < floor {MIN_HARVEST_PAIRS}"
                            f"; {detail}")
            return None
        self.log.append(KIND_REGISTRY_REFRESH,
                        "doc_id registry re-harvested (verified)",
                        f"pairs={pair_count}; {detail}")
        return fresh

    def _rollback_v3(self, current: Path, backup: Path,
                     had_backup: bool) -> None:
        """Restore the pre-heal registry state (fail-soft best effort).

        With a backup: bit-for-bit restore via ``os.replace``. Without
        one (first-ever heal): drop the fresh v3 so the registry descent
        falls back to v2 exactly as before the heal. Failures here are
        suppressed — the caller's typed error is what reaches the
        operator either way; this only bounds the damage.
        """
        import os
        try:
            if had_backup and backup.is_file():
                os.replace(backup, current)
            elif current.is_file():
                current.unlink()
        except OSError:
            pass

    def record_token_cache_rebuild(self, trigger: str) -> None:
        """Record that a corrupt token cache was discarded and rebuilt.

        The cache path already fails soft (``TokenCache.load`` yields None
        on a corrupt file and the next bootstrap regenerates it — the heal
        is the discard itself); this records the event so the operator and
        the doctor see WHY the extra bootstrap happened.
        """
        if not self.allow(KIND_TOKEN_CACHE_REBUILD):
            return
        self.record(KIND_TOKEN_CACHE_REBUILD, trigger,
                    "cache discarded; fresh bootstrap regenerated tokens")
