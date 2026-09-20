"""Governor commands: status of and manual disengagement from the
pacing/budget engine (docs/10 §7 recovery dynamics; docs/11 §5
containment), plus the post-hoc pacing self-audit (docs/11 §8).

`status` reads today's counters, caps, and cooldown state from the
persistent governor state (state/governor_state.json — counters survive
process restarts within the calendar day). `cooldown` is the
operator-initiated containment move: enter the disengagement window
now, without waiting for a server-side soft-block signal. `audit` is
the closed loop the other two cannot be: it reads a request journal's
ts sequence back and verdicts the pacing that ACTUALLY happened
against the policy the governor was configured with — did the run look
like the Phase-8 metronome kill (docs/15 §P8-1) or like a human?

All three commands construct the governor DIRECTLY (no Session): none
of them touches the wire, so requiring cookies.txt — what the pre-audit
Session-based form did — was an audit finding, not a security
property. `audit` is strictly read-only: it never mutates governor
state, never journals, never networks.

USER-DOC ANCHOR: cli/docs/07-reference-tooling.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse
import sys

from governor import GovernorConfig, RequestGovernor, default_governor
from journal.analysis import PacingAudit, audit_pacing, request_gaps
from journal.recorder import JSONLJournal

from .common import add_common_args, build_config, emit


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the governor family onto the root parser: status, cooldown,
    audit."""
    p = sub.add_parser(
        "governor",
        help="request-governor state: pacing, caps, mutation budget, cooldowns")
    gsub = p.add_subparsers(dest="governor_command", required=True)

    status = gsub.add_parser("status", help="today's counters, caps, cooldown state")
    add_common_args(status)
    status.set_defaults(fn=cmd_status)

    cooldown = gsub.add_parser("cooldown", help="manually enter a cooldown "
                                                "(disengage — docs/11 §5)")
    add_common_args(cooldown)
    cooldown.add_argument("--minutes", type=float, default=15.0, metavar="MIN",
                          help="cooldown length in minutes (default 15.0)")
    cooldown.set_defaults(fn=cmd_cooldown)

    audit = gsub.add_parser("audit", help="post-hoc pacing self-audit of a "
                                          "request journal (offline, docs/11 §8)")
    add_common_args(audit)
    audit.add_argument("--file", default="session", metavar="NAME",
                       help="journal name under state/ (without .jsonl; "
                            "default session)")
    audit.set_defaults(fn=cmd_audit)


def _governor() -> RequestGovernor | None:
    """The process-global governor, constructed directly — no Session.

    The pre-audit form built a full Session just to read
    ``session.transport.governor``, which required cookies.txt to
    exist; but Session wires this exact ``governor.default_governor``
    singleton, so reading it here is equivalent and keeps both
    governor commands working with no cookie jar at all. The disabled
    case (FBK_GOVERNOR=off) is an enabled=False instance surfaced by
    ``status()`` — not None; the None branch in the callers stays for
    the governor-less-transport shape.
    """
    return default_governor()


def cmd_status(args: argparse.Namespace) -> int:
    """Print the governor's live state: today's counters vs caps, the
    separate mutation budget, soft blocks seen, and cooldown remaining.

    Returns:
        0 — a disabled governor (None) is a valid configuration, not
        an error: the payload reports enabled=false.
    """
    g = _governor()
    if g is None:
        emit(args, {"enabled": False,
                    "note": "governor disabled (FBK_GOVERNOR=off)"})
        return 0
    s = g.status()

    def human() -> None:
        print(f"enabled       : {s['enabled']}")
        print(f"requests      : {s['hour_count']} this hour "
              f"(cap {s['hourly_cap']}) | {s['day_count']} today "
              f"(cap {s['daily_cap']})")
        print(f"mutations     : {s['mutation_count']} today "
              f"(budget {s['mutation_daily_cap']})")
        print(f"soft blocks   : {s['soft_blocks_seen']} seen")
        state = ("ACTIVE — " + str(s["cooldown_remaining_s"]) + "s left"
                 if s["cooldown_active"] else "inactive")
        print(f"cooldown      : {state}")

    emit(args, s, human=human)
    return 0


def cmd_cooldown(args: argparse.Namespace) -> int:
    """Enter a governor cooldown manually — the docs/11 §5 containment
    move, operator-initiated.

    Mechanism: ``--minutes`` overrides the configured cooldown length,
    then observe_soft_block() engages the cooldown as if a soft block
    had been seen. Every subsequent governed request blocks until the
    window expires — the correct response to enforcement is
    disengagement, never escalating retries (docs/10 §7).

    Returns:
        0 on success; 1 when the governor is disabled (nothing to cool
        down — a failed precondition, reported to stderr).
    """
    g = _governor()
    if g is None:
        print("error: governor disabled — nothing to cool down", file=sys.stderr)
        return 1
    g.config.cooldown_s = args.minutes * 60.0
    g.observe_soft_block()
    print(f"cooldown set: {args.minutes:.0f} minutes — automated activity "
          "should stop for this window (docs/11 §5 containment)")
    return 0


def _audit_config() -> GovernorConfig:
    """The pacing policy an audit verdicts against — the live governor's.

    ``default_governor()`` is the exact instance the transport paces
    through, so its config carries the same FBK_GOVERNOR_* overrides the
    run being audited was paced with. The governor-less shape (None —
    kept for the same legacy reason as in the callers above) falls back
    to the same env resolution; reading config never mutates state.
    """
    g = _governor()
    return g.config if g is not None else GovernorConfig.from_env()


def _verdict_line(audit: PacingAudit) -> str:
    """The human one-line verdict: fired flags, or the clean-passage note."""
    fired = [name for name, hit in (
        ("burst-suspect", audit.burst_suspect),
        ("sparse-anomaly", audit.sparse_anomaly),
        ("metronomic-suspect", audit.metronomic_suspect),
        ("policy-drift", audit.policy_drift),
    ) if hit]
    if audit.floor_violations:
        fired.append(f"floor-violations({audit.floor_violations})")
    if fired:
        return ", ".join(fired)
    return ("clean — inter-arrival pacing inside the human-plausible "
            "envelope (docs/10 §4)")


def cmd_audit(args: argparse.Namespace) -> int:
    """Post-hoc pacing self-audit of one request journal (docs/11 §8).

    Fully offline: reads ``<state>/<file>.jsonl`` via
    ``JSONLJournal.read_all(strict=False)`` — a crash-torn trailing line
    (the recorder's no-fsync trade-off) is skipped, never fatal — spaces
    the request entries' ts sequence into inter-arrival gaps, and
    verdicts them against the live governor's config per the documented
    heuristics (burst/sparse/metronomic/policy-drift/floor — see
    ``journal.analysis`` for every threshold's citation).

    Returns:
        0 — the verdict is DATA, like `measure report`: flags firing is
        the audit working, not the command failing.
        1 — failed precondition: the journal is missing, or carries
        fewer than two spaced requests (nothing to audit).
    """
    path = build_config(args).journal_file(args.file)
    if not path.is_file():
        print(f"error: nothing to audit — no journal {args.file!r} at {path}; "
              "run a journaled command first", file=sys.stderr)
        return 1
    entries = JSONLJournal(path).read_all(strict=False)
    gaps = request_gaps(entries)
    if not gaps:
        print(f"error: nothing to audit — journal {args.file!r} carries "
              f"fewer than two spaced requests ({len(entries)} entries)",
              file=sys.stderr)
        return 1
    config = _audit_config()
    audit = audit_pacing(gaps, config)

    def human() -> None:
        print(f"journal       : {args.file} ({audit.count} request gaps)")
        print(f"gaps          : min {audit.min_gap_s:.3f}s  "
              f"p50 {audit.p50_gap_s:.3f}s  mean {audit.mean_gap_s:.3f}s  "
              f"p95 {audit.p95_gap_s:.3f}s  max {audit.max_gap_s:.3f}s")
        print(f"gap cv        : {audit.gap_cv:.3f}  "
              f"(policy mean {config.mean_gap_s}s, floor {config.min_gap_s}s)")
        print(f"verdict       : {_verdict_line(audit)}")

    emit(args, {"journal": args.file, **audit.model_dump()}, human=human)
    return 0
