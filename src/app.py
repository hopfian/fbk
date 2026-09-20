"""fbk entrypoint: parser assembly, stream preparation, command dispatch.

Builds the complete argparse tree by importing one module per command
family (_COMMAND_MODULES, mirroring the docs/02 surface map) and letting
each ``register(sub)`` attach its subparsers (docs/12 §2 layering).
Also owns the three process-level concerns that must hold before any
command logic runs:

  * ``--version`` — single-sourced from the installed distribution
    metadata via ``importlib.metadata.version("fbk")``; the literal
    serves only uninstalled source trees, so a release never needs to
    touch this file.
  * UTF-8 stream reconfiguration — Windows consoles default to cp1252
    and raise UnicodeEncodeError on the multilingual payloads this
    client emits (Bangla/emoji feed text); every output stream is
    forced to UTF-8 before parsing begins.
  * ``allow_abbrev=False`` across the entire parser tree — renamed
    legacy flags (e.g. --page → --page-id) must fail loudly, never
    survive as silently accepted prefix abbreviations
     (see _disallow_abbrev).

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import sys
import traceback
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from commands.common import run_command

# Single-sourced from installed distribution metadata (pyproject [project]
# name "fbk"); the literal is only the run-from-source fallback.
try:
    _VERSION = _pkg_version("fbk")
except PackageNotFoundError:  # running from an uninstalled source tree
    _VERSION = "2.2.0"

# One module per surface family. Each module exposes register(subparsers).
_COMMAND_MODULES = (
    "commands.auth",
    "commands.registry",
    "commands.feed",
    "commands.profile",
    "commands.messenger",
    "commands.groups",
    "commands.pages",
    "commands.search",
    "commands.marketplace",
    "commands.notifications",
    "commands.settings",
    "commands.friends",
    "commands.events",
    "commands.stories",
    "commands.memories",
    "commands.saved",
    "commands.comments",
    "commands.video",
    "commands.presence",
    "commands.upload",
    "commands.video_upload",
    "commands.photos",
    "commands.overview",
    "commands.measure",
    "commands.governor",
    # operator introspection (offline): environment, jar, journals, registry state
    "commands.config",
    "commands.cookies",
    "commands.journal",
    "commands.fingerprint",
    "commands.drafts",
    "commands.doctor",
    "commands.templates",
    # LAST by design: completions' hidden-verb metavar snapshot is taken at
    # registration time — registering it earlier would drop later families
    # from the usage braces (commands/completions.py _hide_from_help).
    "commands.completions",
)


_EPILOG = """\
examples:
  fbk whoami --no-journal
  fbk feed read --pages 2 --json
  fbk messenger threads --limit 10
  fbk messenger history --thread-id <id> --limit 20
  fbk pages feed --page-id <id> --json
  fbk governor status
  fbk measure pace --actions 5 --mean-gap 4.0
"""


def _disallow_abbrev(parser: argparse.ArgumentParser) -> None:
    """Recursively set allow_abbrev=False across a parser tree.

    argparse subparsers do NOT inherit the parent's allow_abbrev — each
    add_parser() builds a fresh ArgumentParser defaulting to True — so
    the top-level flag alone leaves subcommand flags abbreviatable.
    Without this, a renamed legacy flag (e.g. "--page" → "--page-id")
    keeps silently working as a prefix abbreviation.

    Args:
        parser: Any node of the argparse tree; recursion covers every
            registered subparser choice beneath it.

    Note:
        Must run AFTER all register() calls — subparsers created later
        would keep the permissive default. Iterating ``parser._actions``
        is the stable argparse seam for enumerating _SubParsersAction
        nodes; no public accessor exists.
    """
    parser.allow_abbrev = False
    for action in parser._actions:  # (stable argparse seam)
        if isinstance(action, argparse._SubParsersAction):
            for sub_parser in action.choices.values():
                _disallow_abbrev(sub_parser)


def build_parser() -> argparse.ArgumentParser:
    """Assemble the complete fbk argument parser.

    Imports each family module in _COMMAND_MODULES order and hands it
    the root subparsers action — the tuple's order defines the --help
    listing (kept aligned with the docs/02 surface map). Prefix
    abbreviation is disabled tree-wide LAST, after every subparser
    exists (see _disallow_abbrev).

    Returns:
        The fully wired parser: prog "fbk", the --version action, and
        one required subcommand whose handler rides in ``args.fn``
        (dispatched by main via commands.common.run_command).
    """
    parser = argparse.ArgumentParser(
        prog="fbk",
        description="Facebook web-surface client — authenticated GraphQL, "
                    "MQTT realtime, DGW transport (grounded in docs/01-16).",
        epilog=_EPILOG,
        # flags must be typed IN FULL — prefix abbreviation would
        # silently accept renamed legacy flags (see _disallow_abbrev).
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version",
                        version=f"fbk {_VERSION}")
    parser.add_argument("--debug", action="store_true",
                        help="full tracebacks for unexpected errors instead "
                             "of the terse error line (global; before the "
                             "subcommand)")
    sub = parser.add_subparsers(dest="command", required=True)
    for mod_name in _COMMAND_MODULES:
        mod = importlib.import_module(mod_name)
        mod.register(sub)
    _disallow_abbrev(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Process entry: prepare streams, parse, dispatch, map exit codes.

    Args:
        argv: Explicit argument vector (tests, alternate callers);
            None means sys.argv[1:].

    Returns:
        run_command's typed-error contract for dispatched commands;
        130 on KeyboardInterrupt at any stage; 2 for any unexpected
        exception, with its message on stderr — or, under ``--debug``,
        the full traceback on stderr (stderr only: payload data on
        stdout stays clean for piping either way). argparse usage
        errors exit 2 on their own via SystemExit before dispatch
        reaches this function's handlers.

    Note:
        ``stream.reconfigure`` raises ValueError/OSError on handles
        whose encoding is already fixed (redirected pipes) —
        suppressed, the pipe keeps its own encoding.
    """
    # Windows consoles default to cp1252 and choke on the multilingual payloads
    # (Bangla/emoji feed text) — force UTF-8 on every output stream up front.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            # already-redirected pipes with a fixed encoding raise here; ignore
            with contextlib.suppress(ValueError, OSError):
                stream.reconfigure(encoding="utf-8", errors="replace")
    # args is None until parse_args succeeds: an exception raised while the
    # parser itself is being assembled carries no namespace to consult.
    args: argparse.Namespace | None = None
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        return run_command(args.fn, args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        if args is not None and args.debug:
            traceback.print_exc()  # stderr — the full --debug traceback
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
