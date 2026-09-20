"""Journal review: ``fbk journal list/show/stats/export`` — the
activity-review tool for the study's own dataset, fully OFFLINE
(docs/12 §8: "the journals are the dataset of the study" — versioned,
greppable, safe to keep).

Every governed HTTP request lands as one JSONL line under state/
(docs/11 §7). This family reads that dataset back — enumerate the
journals, show one's entries, aggregate one's pacing-relevant stats —
with no network, no session, and no cookie jar required.

SECURITY BOUNDARY:

  Display relies ENTIRELY on write-time redaction: JSONLJournal.record
  redacts every secret-named value BEFORE it touches disk, so every
  stored entry is already display-safe and is printed VERBATIM here —
  this module adds no redaction logic of its own and must never need
  any. ``--file`` takes a bare journal NAME resolved through
  Config.journal_file (state/<name>.jsonl), never a raw path: names
  carrying path separators or dot-segments are rejected before
  resolution, so no invocation can point the reader outside state/.

  A crash can tear a journal's final line (the recorder's no-fsync
  design trade — one record of forensic continuity, never a secret).
  All four subcommands read with strict=False, so a torn TRAILING
  line is skipped with the recorder's own stderr note and the file
  still reviews. Interior corruption stays loud: read_all raises in
  both modes and the dispatch layer maps that to exit 2.

  Export (``journal export``) is the dataset's path OUT for external
  analysis (docs/12 §8) and relies on the same write-time redaction:
  it copies already-display-safe entries, adding no redaction logic
  of its own — and none it could weaken. Its ``--out PATH`` is an
  operator-chosen WRITE target: unlike ``--file`` it may legitimately
  point anywhere (that is what exporting is for), but an existing
  file is never overwritten without ``--force`` (refusal → exit 1).

USER-DOC ANCHOR: cli/docs/07-reference-tooling.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

from journal.recorder import JSONLJournal, iter_journals
from surfaces.measurement import percentile

from .common import add_common_args, build_config, emit


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the journal family onto the root parser: list/show/stats/export."""
    p = sub.add_parser(
        "journal",
        help="review state/ request journals — the study dataset (offline)")
    csub = p.add_subparsers(dest="journal_command", required=True)

    listing = csub.add_parser(
        "list", help="enumerate journals under state/ (name, size, entries, span)")
    add_common_args(listing)
    listing.set_defaults(fn=cmd_list)

    show = csub.add_parser(
        "show", help="print one journal's entries (already redacted at write)")
    add_common_args(show)
    show.add_argument("--file", required=True, metavar="NAME",
                      help="journal name without extension (state/<NAME>.jsonl)")
    show.add_argument("--limit", type=int, default=20, metavar="N",
                      help="show N entries (default 20)")
    show.add_argument("--tail", action="store_true",
                      help="show the LAST N entries instead of the first N")
    show.set_defaults(fn=cmd_show)

    stats = csub.add_parser(
        "stats", help="aggregate one journal: surfaces, statuses, arrival gaps")
    add_common_args(stats)
    stats.add_argument("--file", required=True, metavar="NAME",
                       help="journal name without extension (state/<NAME>.jsonl)")
    stats.set_defaults(fn=cmd_stats)

    export = csub.add_parser(
        "export", help="export one journal as CSV/JSON for external analysis")
    add_common_args(export)
    export.add_argument("--file", required=True, metavar="NAME",
                        help="journal name without extension (state/<NAME>.jsonl)")
    export.add_argument("--out", default=None, metavar="PATH",
                        help="write the export to PATH (default stdout)")
    export.add_argument("--format", default="csv", choices=("csv", "json"),
                        help="export format (default csv)")
    export.add_argument("--force", action="store_true",
                        help="allow --out to overwrite an existing file")
    export.set_defaults(fn=cmd_export)


def _journal_path(cfg_dir: Path, name: str) -> Path | None:
    """Resolve one journal NAME to its state/ path, or None to refuse it.

    The security boundary of the whole family: ``name`` must be a bare
    basename. Anything carrying a path separator or a dot-segment is
    rejected BEFORE Config.journal_file is ever consulted, so the
    resolution can never be steered outside state/.

    Args:
        cfg_dir: The resolved state directory from Config.
        name: The operator-supplied ``--file`` value.

    Returns:
        The intended ``<state>/<name>.jsonl`` path, or None when the
        name could escape the state directory (invalid, never an error
        on disk — the caller reports and exits 1).
    """
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        return None
    return cfg_dir / f"{name}.jsonl"


def _load_entries(args: argparse.Namespace) -> tuple[str, list[dict[str, Any]]] | None:
    """Resolve and read one journal by NAME; None means exit 1.

    The name is validated and resolved through the state/ boundary
    (see :func:`_journal_path`); a valid-but-absent journal is a failed
    precondition (exit 1, the house convention), while a present file
    is read with ``strict=False`` so a crash-torn trailing line is
    skipped with the recorder's own stderr note instead of blocking
    the review.

    Args:
        args: Parsed namespace; ``args.file`` carries the journal name.

    Returns:
        (name, entries) on success, or None after a stderr note when
        the name is path-shaped or no such journal exists.
    """
    cfg = build_config(args)
    name: str = args.file
    path = _journal_path(cfg.state_dir, name)
    if path is None or not path.is_file():
        print(f"error: no journal {name!r} under {cfg.state_dir} — "
              "--file takes a bare journal NAME (state/<name>.jsonl); "
              "see `fbk journal list`", file=sys.stderr)
        return None
    return name, JSONLJournal(path).read_all(strict=False)


def _fmt_ts(ts: Any) -> str:
    """Render one entry timestamp as UTC ISO text ('-' when absent)."""
    if isinstance(ts, int | float):
        return (datetime.fromtimestamp(ts, tz=UTC)
                .strftime("%Y-%m-%dT%H:%M:%SZ"))
    return "-" if ts is None else str(ts)


def _fmt_entry(entry: Mapping[str, Any]) -> str:
    """One human journal line: request entries as ts/method/status/url,
    event entries (session_start, latency_sample) as ts/event, anything
    else as the compact raw JSON — all verbatim, write-time redaction
    already made every shape display-safe."""
    ts = _fmt_ts(entry.get("ts"))
    if "method" in entry:
        return (f"{ts}  {entry.get('method', '?')!s:<4} "
                f"{entry.get('status', '?')!s:>3}  {entry.get('url', '')}")
    if "event" in entry:
        return f"{ts}  event {entry['event']}"
    return f"{ts}  {json.dumps(entry, ensure_ascii=False, default=str)}"


def _has_torn_trailing_line(path: Path) -> bool:
    """True when the file's final line lacks its terminating newline —
    the exact shape a crash mid-append can leave (the no-ffsync trade);
    a terminated-but-corrupt interior line stays loud via read_all."""
    raw = path.read_bytes()
    return bool(raw) and not raw.endswith(b"\n")


def _entry_surface(entry: Mapping[str, Any]) -> str:
    """The entry's ctx.surface tag, '(none)' when untagged or non-dict."""
    ctx = entry.get("ctx")
    surface = ctx.get("surface") if isinstance(ctx, Mapping) else None
    return str(surface) if surface else "(none)"


def cmd_list(args: argparse.Namespace) -> int:
    """Enumerate every journal under the resolved state/ directory.

    Per file: name, size in bytes, parsed entry count, first/last
    entry ts, and whether the file ends with a torn trailing line.
    Zero journals is a REPORTED fact ("no journals"), never an error —
    this is the entry point for auditing the dataset after a scrub.

    Returns:
        0 always: an empty state/ (or a missing one) is payload data.
    """
    cfg = build_config(args)
    journals: list[dict[str, Any]] = []
    for path in iter_journals(cfg.state_dir):
        entries = JSONLJournal(path).read_all(strict=False)
        stamps = [float(e["ts"]) for e in entries
                  if isinstance(e.get("ts"), int | float)]
        journals.append({
            "name": path.stem,
            "size_bytes": path.stat().st_size,
            "entries": len(entries),
            "first_ts": stamps[0] if stamps else None,
            "last_ts": stamps[-1] if stamps else None,
            "torn_trailing_line": _has_torn_trailing_line(path),
        })
    payload = {"state_dir": str(cfg.state_dir),
               "count": len(journals), "journals": journals}

    def human() -> None:
        if not journals:
            print(f"no journals under {cfg.state_dir}")
            return
        print(f"{len(journals)} journal(s) under {cfg.state_dir}")
        for j in journals:
            torn = "  [torn trailing line]" if j["torn_trailing_line"] else ""
            print(f"{j['name']:<28} {j['entries']:>7} entries "
                  f"{j['size_bytes']:>10} B  "
                  f"{_fmt_ts(j['first_ts'])} → {_fmt_ts(j['last_ts'])}{torn}")

    emit(args, payload, human=human)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """Print one journal's entries verbatim (redacted at write time).

    ``--limit N`` keeps the first N entries (default 20); ``--tail``
    flips the window to the last N instead. Entries print one per
    line (ts, method, status, url) in human mode; ``--json`` carries
    the raw entries array — every shape is display-safe on disk.

    Returns:
        0 on success; 1 when the name is path-shaped or no such journal
        exists under state/ (failed precondition, house convention).
    """
    loaded = _load_entries(args)
    if loaded is None:
        return 1
    name, entries = loaded
    n = max(0, args.limit)
    if n == 0:
        selected: list[dict[str, Any]] = []
    elif args.tail:
        selected = entries[-n:]
    else:
        selected = entries[:n]
    payload = {"file": name, "total": len(entries),
               "shown": len(selected), "entries": selected}

    def human() -> None:
        which = "last" if args.tail else "first"
        print(f"journal {name}: showing {len(selected)} of {len(entries)} "
              f"entries ({which} {n})")
        for entry in selected:
            print(_fmt_entry(entry))

    emit(args, payload, human=human)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """Aggregate one journal into the pacing-review primitives.

    Purely descriptive (the sibling ``governor audit`` owns verdicts):
    total entries, per-surface request counts (ctx.surface, '(none)'
    when untagged), status-code distribution, inter-arrival gap
    summary over consecutive entry timestamps (min/p50/p95/max —
    ``percentile`` from surfaces.measurement, the one percentile
    implementation), and the first→last request span.

    Returns:
        0 on success; 1 when the name is path-shaped or no such journal
        exists under state/ (failed precondition, house convention).
    """
    loaded = _load_entries(args)
    if loaded is None:
        return 1
    name, entries = loaded
    surfaces: Counter[str] = Counter(_entry_surface(e) for e in entries)
    statuses: Counter[str] = Counter(
        str(e["status"]) for e in entries if "status" in e)
    stamps = [float(e["ts"]) for e in entries
              if isinstance(e.get("ts"), int | float)]
    gaps = sorted(b - a for a, b in pairwise(stamps))
    span = stamps[-1] - stamps[0] if len(stamps) > 1 else 0.0
    gaps_block: dict[str, Any] = {
        "count": len(gaps),
        "min_s": round(min(gaps), 3) if gaps else None,
        "p50_s": round(percentile(gaps, 50.0), 3),
        "p95_s": round(percentile(gaps, 95.0), 3),
        "max_s": round(max(gaps), 3) if gaps else None,
    }
    payload: dict[str, Any] = {
        "file": name,
        "total": len(entries),
        "surfaces": dict(sorted(surfaces.items())),
        "status_codes": dict(sorted(statuses.items())),
        "gap_seconds": gaps_block,
        "span_seconds": round(span, 3),
        "first_ts": stamps[0] if stamps else None,
        "last_ts": stamps[-1] if stamps else None,
    }

    def human() -> None:
        print(f"journal {name}: {len(entries)} entries, "
              f"span {payload['span_seconds']}s")
        print("surfaces:")
        for surface, count in sorted(surfaces.items()):
            print(f"  {surface:<24} {count}")
        print("status codes:")
        for status, count in sorted(statuses.items()):
            print(f"  {status:<24} {count}")
        g = gaps_block
        print(f"arrival gaps: n={g['count']} min={g['min_s']}s "
              f"p50={g['p50_s']}s p95={g['p95_s']}s max={g['max_s']}s")

    emit(args, payload, human=human)
    return 0


# The CSV export's fixed column set (docs/12 §8: the journals are the
# dataset of the study — the columns are the transport's own metadata
# fields, exactly what FBTransport._note writes, with ctx.surface
# flattened alongside).
_EXPORT_COLUMNS: tuple[str, ...] = ("ts", "method", "status", "content_length",
                                    "surface", "url")


def _cell(value: Any) -> str:
    """One CSV cell: the raw value as text, '' for absent/None."""
    return "" if value is None else str(value)


def _export_row(entry: Mapping[str, Any]) -> dict[str, str]:
    """Flatten one journal entry to the export row (all six columns).

    Exactly the fields the transport writes (ts, method, url, status,
    content_length, ctx) with ctx.surface hoisted to its own column;
    every value was redacted at write time, so the row is safe as-is.
    """
    ctx = entry.get("ctx")
    surface = ctx.get("surface") if isinstance(ctx, Mapping) else None
    return {
        "ts": _cell(entry.get("ts")),
        "method": _cell(entry.get("method")),
        "status": _cell(entry.get("status")),
        "content_length": _cell(entry.get("content_length")),
        "surface": _cell(surface),
        "url": _cell(entry.get("url")),
    }


def cmd_export(args: argparse.Namespace) -> int:
    """Export one journal for external analysis (docs/12 §8 — the
    journals ARE the dataset; this is the dataset's path out).

    ``--format csv`` (default) flattens every entry to the transport's
    own metadata columns (_EXPORT_COLUMNS — ts, method, status,
    content_length, surface, url), exactly the fields FBTransport._note
    writes (URLs, sizes, statuses, friendly names; docs/11 §7).
    ``--format json`` emits the raw entries array, verbatim like
    ``show --json``. A crash-torn trailing line is tolerated exactly
    like show/stats (read_all strict=False, the recorder's own stderr
    note); interior corruption still raises and maps to exit 2.

    REDACTION IS CARRIED, NEVER RE-APPLIED: every entry was redacted
    at write time by JSONLJournal.record (docs/11 §7 — every
    secret-named value replaced by a salted fingerprint BEFORE disk),
    so this export adds no redaction logic of its own and cannot
    weaken the guarantee: it copies already-display-safe data.

    ``--out PATH`` is an operator-chosen write target — unlike
    ``--file`` (bare NAME, state/-confined) it may legitimately point
    anywhere, because exporting to an analysis target is the point.
    An EXISTING file at that path is never silently overwritten: the
    command refuses (exit 1, the failed-precondition code; no house
    precedent for clobbering — ``--force`` is the explicit opt-in).
    Without ``--out`` the export body prints on stdout alone — no
    decoration line, or a CSV body would stop being parseable.

    Returns:
        0 on success; 1 when the journal is missing or the name is
        path-shaped (same precondition as show), or when ``--out``
        targets an existing file without ``--force``.
    """
    loaded = _load_entries(args)
    if loaded is None:
        return 1
    name, entries = loaded
    fmt: str = args.format
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(_EXPORT_COLUMNS)
        for entry in entries:
            row = _export_row(entry)
            writer.writerow([row[c] for c in _EXPORT_COLUMNS])
        body = buf.getvalue()
    else:
        body = json.dumps(entries, ensure_ascii=False, indent=2, default=str)
    if not body.endswith("\n"):
        body += "\n"

    out: str | None = args.out
    if out is None:
        print(body, end="")
        return 0
    target = Path(out)
    if target.exists() and not args.force:
        print(f"error: refusing to overwrite {target} — pass --force to "
              f"overwrite an existing export target", file=sys.stderr)
        return 1
    target.write_text(body, encoding="utf-8", newline="\n")
    emit(args, {"file": name, "format": fmt, "out": str(target),
                "rows": len(entries)},
         human=lambda: print(f"exported {len(entries)} entries to {target} "
                             f"({fmt})"))
    return 0
