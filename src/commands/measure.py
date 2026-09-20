"""Measurement commands: the docs/10 §8 harness surface
(latency/pace/report).

  * ``measure latency``  — live canary latency run (default: the verified
    cheapest badge read, docs/15 §P2-2), journaled per sample.
  * ``measure pace``     — lognormal inter-arrival schedule (docs/11 §8):
    dry math, no HTTP, no session, no cookies loaded.
  * ``measure report``   — pacing verdict over a journaled run: mean/p95
    inter-arrival gaps vs the human-plausible envelope (docs/10 §4
    uniformity signature).

This is the calibration tooling the docs/10 §8 measurement plan names:
canaries before batches, journal everything, one dimension at a time.

USER-DOC ANCHOR: cli/docs/07-reference-tooling.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse

from surfaces.measurement import (
    DEFAULT_CV,
    DEFAULT_SEED,
    MeasurementService,
    lognormal_schedule,
)

from .common import add_common_args, emit, new_session, with_session


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the measure family onto the root parser: latency, pace,
    report."""
    meas = sub.add_parser("measure", help="rate/latency measurement harness (docs/10 §8)")
    msub = meas.add_subparsers(dest="measure_command", required=True)

    lat = msub.add_parser("latency", help="sequential canary latency run")
    add_common_args(lat)
    lat.add_argument("--samples", type=int, default=10,
                     help="canary calls to time (default 10)")
    lat.set_defaults(fn=cmd_latency)

    pace = msub.add_parser("pace", help="lognormal inter-arrival schedule (dry, no HTTP)")
    add_common_args(pace)
    pace.add_argument("--actions", type=int, default=10,
                      help="actions to schedule (default 10)")
    pace.add_argument("--mean-gap", type=float, default=5.0,
                      help="mean inter-arrival gap in seconds (default 5.0)")
    pace.add_argument("--cv", type=float, default=DEFAULT_CV,
                      help="coefficient of variation of the gaps (default 0.6)")
    pace.add_argument("--seed", type=int, default=DEFAULT_SEED,
                      help="schedule seed — deterministic (default 1234)")
    pace.set_defaults(fn=cmd_pace)

    rep = msub.add_parser("report", help="pacing verdict for one journal file")
    add_common_args(rep)
    rep.add_argument("--journal", required=True,
                     help="journal name under the journals dir (without .jsonl)")
    rep.set_defaults(fn=cmd_report)


def cmd_latency(args: argparse.Namespace) -> int:
    """Run the sequential live canary latency measurement.

    ``--samples`` sequential badge reads, each journaled; the summary
    carries min/mean/p50/p95/max. Use this before any batch escalation
    window (docs/10 §8: canaries gate batches).

    Returns:
        0 — failed samples report in the failed counter; the run itself
        only fails on typed errors.
    """
    with with_session(new_session(args)) as session:
        service = MeasurementService(session)
        report = service.latency(samples=args.samples)
        emit(args, report.model_dump(),
             human=lambda: print(
                 f"canary latency ({report.ok} ok / {report.failed} failed of "
                 f"{report.samples} samples): "
                 f"min={report.min_s:.3f}s mean={report.mean_s:.3f}s "
                 f"p50={report.p50_s:.3f}s p95={report.p95_s:.3f}s "
                 f"max={report.max_s:.3f}s"))
        return 0


def cmd_pace(args: argparse.Namespace) -> int:
    """Emit a lognormal inter-arrival schedule — dry math only.

    Prints the planned action times (t=… action#N) for wiring into an
    operator's own batch pacing; the parameters mirror the governor's
    gap model (mean gap, CV, deterministic seed for reproducibility,
    docs/11 §8 scheduler).

    Returns:
        0 — pure computation; cannot fail short of a bad flag value,
        which argparse rejects with usage exit 2.
    """
    # Pure pacing math (docs/11 §8): no session, no cookies, no HTTP.
    times = lognormal_schedule(args.actions, args.mean_gap, args.cv, args.seed)

    def human() -> None:
        for k, t in enumerate(times, start=1):
            print(f"t={t:.3f}s  action#{k}")

    emit(args, {"actions": args.actions, "times": [round(t, 3) for t in times],
                "mean_gap": args.mean_gap, "cv": args.cv, "seed": args.seed},
         human=human)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Render the pacing verdict over one journal file.

    ``--journal`` names a journal under state/ (without .jsonl) — the
    run just measured. The verdict classifies the journaled inter-arrival
    distribution: metronomic gaps are the classic bot signature
    (docs/10 §4 uniformity), so the report is the go/no-go check
    before an escalated batch.

    Returns:
        0 — the verdict is data, not a pass/fail flag.
    """
    with with_session(new_session(args)) as session:
        service = MeasurementService(session)
        report = service.report(args.journal)
        emit(args, report.model_dump(),
             human=lambda: print(
                 f"journal {report.journal_name!r}: {report.verdict} "
                 f"({report.requests} requests, {report.statuses_ok} ok-2xx, "
                 f"mean gap {report.mean_gap_s:.3f}s, "
                 f"p95 gap {report.p95_gap_s:.3f}s)"))
        return 0
