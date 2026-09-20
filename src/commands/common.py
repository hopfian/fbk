"""Shared CLI plumbing: session construction, unified output, and the
typed-error → exit-code contract every command family inherits.

ARCHITECTURE:

  Contract hub for all 27 command modules under commands/. No command
  touches cookies, transport, or GraphQL directly — this module is the
  only sanctioned bridge into the layers below (docs/12 §2 layering
  rule: commands may import downward; nothing imports commands):

  * ``add_common_args`` — the plumbing flag set every (sub)command
    accepts, orthogonal to any surface's own semantics.
  * ``build_config`` — resolves :class:`config.Config` with CLI path
    overrides layered over FBK_ROOT / FBK_COOKIES environment
    discovery (docs/12 §4 path resolution).
  * ``new_session`` — builds the :class:`session.Session` facade
    (cookie jar → FBTransport → governor → journal) with the JSONL
    journal named after the invoking command (per-surface journals,
    docs/11 §7).
  * ``with_session`` — the lifecycle scope for that facade: yields
    the Session and closes it on scope exit, so the pooled curl
    connections every Session holds are released instead of dropped
    at process exit.
  * ``emit`` — the stdout discipline: payload data on stdout, error
    text never; run_command owns stderr.
  * ``run_command`` — the single exception barrier mapping typed
    errors to stable process exit codes (the authoritative contract
    table lives in its docstring).

CALIBRATION NOTES:

  Exit codes are a public interface — scripts and CI gates match on
  them (docs/12 §7 operator-action matrix); renumbering any of them
  is a breaking change, not a refactor.

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from config import Config
from graphql.errors import (
    CheckpointError,
    DryRunComplete,
    DryRunRawSeamError,
    FBGraphError,
    NotLoggedInError,
    RateLimitedError,
    RegistryMissError,
)
from retry import RetryPolicy, run_with_retry
from session import Session
from transport.cookies import CookieLoadError
from transport.session import FingerprintRejectedError

# The stable typed-error → exit-code mapping; run_command's docstring is
# the authoritative contract table. Exact-class keys — _exit_code's
# isinstance walk resolves subclasses to their family's code.
_EXIT_CODES = {
    NotLoggedInError: 3,
    CheckpointError: 4,
    RateLimitedError: 5,
    RegistryMissError: 6,
    CookieLoadError: 7,
    FingerprintRejectedError: 8,
}


def add_common_args(parser: argparse.ArgumentParser) -> None:
    """Register the plumbing flags every command and subcommand accepts.

    ``--root``/``--cookies`` override Config discovery (the CLI-flag
    equivalents of FBK_ROOT / FBK_COOKIES); ``--no-journal`` detaches
    the JSONL journal sink for this run (docs/11 §7); ``--json`` flips
    emit() to machine output; ``--retry N`` enables the only sanctioned
    retry class — RateLimitedError with respect-backoff (docs/10 §7;
    session, checkpoint, and registry failures never clear on their own).

    ``--dry-run`` switches the run to plan-and-abort: every GraphQL
    call builds its complete request plan, prints it (variables
    redacted, docs/11 §7), and stops before touching the transport,
    governor, journal, or ``q`` counter (docs/11 §8) — the first
    planned call ends the run with exit 0. Raw-seam commands (upload,
    video upload) cannot be planned and exit 1 with a refusal instead.

    Args:
        parser: The (sub)parser being wired; flags land on it directly.
            Must be called before set_defaults binds the handler.
    """
    parser.add_argument("--root", default=None,
                        help="project root (default discovered / FBK_ROOT)")
    parser.add_argument("--cookies", default=None,
                        help="Netscape cookies.txt path (default <root>/cookies.txt)")
    parser.add_argument("--no-journal", action="store_true",
                        help="disable request journaling for this run")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="machine output: raw JSON only")
    parser.add_argument("--retry", type=int, default=0, metavar="N",
                        help="retry on RateLimitedError up to N times with "
                             "exponential backoff (docs/10 §7; default 0)")
    parser.add_argument("--dry-run", action="store_true",
                        help="build and print each GraphQL request plan "
                             "without sending anything (first planned call "
                             "ends the run; raw-seam commands refuse)")


def build_config(args: argparse.Namespace) -> Config:
    """Resolve the runtime Config with CLI overrides layered on top.

    ``--root`` seeds Config.discover (the FBK_ROOT environment override
    still applies when the flag is absent); ``--cookies`` is applied via
    model_copy as an absolute path so the rest of the resolved config
    (data/, state/, profile) stays untouched. Environment variables keep
    precedence over defaults regardless of the flags.

    Args:
        args: Parsed namespace. Both overrides are optional; handlers on
            parsers that never registered them still pass (getattr-guarded).

    Returns:
        A fully resolved Config with absolute paths (docs/12 §4).
    """
    cfg = Config.discover(args.root) if getattr(args, "root", None) else Config.discover()
    if getattr(args, "cookies", None):
        cfg = cfg.model_copy(update={"cookies_path": _abspath(args.cookies)})
    return cfg


def new_session(args: argparse.Namespace, *, journal_name: str | None = None) -> Session:
    """Build the Session facade for one command invocation.

    The JSONL journal is named after the command so each surface gets
    its own file under state/ (docs/11 §7), unless ``--no-journal`` was
    passed (journal sink disabled entirely) or the caller pins an
    explicit journal_name.

    Args:
        args: Parsed namespace; ``args.command`` names the journal file,
            ``args.no_journal`` disables journaling for the run, and
            ``args.dry_run`` (getattr-guarded: some probe parsers do
            not register the common flag set) forwards the plan-only
            mode into the Session.
        journal_name: Overrides the journal file name when one handler
            serves several logical commands.

    Returns:
        A lazily bootstrapping Session: the persistent token cache is
        consulted first, so a warm invocation costs one API call — the
        full homepage bootstrap runs only on TTL miss or token
        rejection (docs/16 §P11-1).

    Raises:
        CookieLoadError: The jar is missing, unreadable, or carries no
            parseable auth-cookie rows (maps to exit code 7 via
            run_command).
    """
    name = None if getattr(args, "no_journal", False) else (journal_name or args.command)
    return Session(build_config(args), journal_name=name,
                   dry_run=getattr(args, "dry_run", False))


@contextmanager
def with_session(args: argparse.Namespace | Session,
                 *, journal_name: str | None = None) -> Iterator[Session]:
    """Scope one Session to a command body, closing it on scope exit.

    Sessions pool curl_cffi connections that were previously never
    released — every cmd_* site just dropped its Session at process
    exit. ``Session.close()`` only releases the transport (idempotent;
    the token cache and governor persist on their own, journal
    appends are per-line), so closing at scope exit — normal return
    or exception — never loses state.

    Two accepted call forms, both closing on exit:

    * ``with_session(args)`` — build via this module's
      :func:`new_session`, resolved at call time so tests
      monkeypatching ``commands.common.new_session`` keep working.
    * ``with_session(session)`` — adopt an already-built Session.
      This is the form the command modules use: their call sites keep
      resolving ``new_session`` through their OWN module namespace —
      the seam the command-layer tests monkeypatch
      (``monkeypatch.setattr(commands.feed, "new_session", ...)``) —
      and hand the built session here for lifecycle only. Building
      it here instead would silently bypass those stubs.

    Args:
        args: Parsed namespace (build form) or an already-built
            Session (adopt form).
        journal_name: Build-form journal override forwarded to
            :func:`new_session`; ignored in the adopt form.

    Yields:
        The session, closed on scope exit whatever the exit reason.
        Close failures are suppressed — a teardown error must not
        mask the command's result or its own typed error.
    """
    # Discriminate on the NAMESPACE, not on Session: tests inject stub
    # sessions (tests/fakes.StubSession) that are not Session subclasses —
    # anything that is not an argparse.Namespace IS the adopt form.
    session = (new_session(args, journal_name=journal_name)
               if isinstance(args, argparse.Namespace) else args)
    try:
        yield session
    finally:
        with suppress(Exception):
            session.close()


def emit(args: argparse.Namespace, payload: dict[str, Any],
         human: Callable[[], None] | None = None) -> None:
    """Write one command's payload under the stdout discipline.

    Payload data always goes to stdout: ``--json`` prints the indented
    payload alone (machine consumption); the default mode runs the
    human renderer first, then the compact one-line JSON — human
    summaries are decoration, scripts can always pipe the last line.
    Error text never flows through here; run_command owns stderr.

    Args:
        args: Parsed namespace; ``args.as_json`` selects machine mode.
        payload: JSON-serializable dict. ``default=str`` tolerates Path
            and Pydantic exotic values; ``ensure_ascii=False`` keeps
            Bangla/emoji text readable (the app.py UTF-8 stream
            reconfiguration makes this safe on Windows).
        human: Optional zero-arg renderer for the human summary;
            skipped entirely in --json mode.
    """
    if getattr(args, "as_json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        if human:
            human()
        print(json.dumps(payload, ensure_ascii=False, default=str))


def run_command(fn: Callable[[argparse.Namespace], int],
                args: argparse.Namespace) -> int:
    """Run one command handler under the uniform typed-error barrier.

    This is the authoritative exit-code contract for the entire CLI
    (the dispatch layer of the docs/12 §7 error matrix). Scripts and CI
    gates match on these codes; renumbering is a breaking change:

    ===  =============================================================
    0    Success — the handler returned 0 (or None, coerced to 0).
         Also DryRunComplete: ``--dry-run`` printed the request plan
         and sent nothing (the success sentinel of the dry-run mode —
         deliberately NOT an FBGraphError, so surface degradation
         handlers never swallow it; not in _EXIT_CODES either).
    1    Failed precondition — the HANDLER itself returns 1, no
         exception involved: an unconfirmed mutation (messenger send
         with no echoed message rows, events delete/create without an
         id, settings verify-password reauth rejected) or a session
         that is not LOGGED_IN (auth whoami/state). Also
         DryRunRawSeamError: a raw-seam command (upload, video
         upload) run under ``--dry-run`` — dry-run covers GraphQL
         calls only and the command cannot proceed in this mode.
    2    Any other FBGraphError (GraphQLProtocolError, DocIdStaleError,
         unclassified wire failures) and any unexpected exception that
         escapes main(); argparse usage errors also exit 2 via
         argparse's own SystemExit.
    3    NotLoggedInError — session invalid/expired; re-auth the jar.
    4    CheckpointError — integrity challenge; halt and contain
         (docs/11 §5), do not retry the jar.
    5    RateLimitedError — soft block; respect-backoff (docs/10 §7).
    6    RegistryMissError — friendly_name absent from every harvested
         registry; run ``fbk registry refresh``.
    7    CookieLoadError — jar missing/unreadable/no auth rows.
    8    FingerprintRejectedError — the edge rejected the TLS/h2
         identity itself (docs/08); retrying the same session cannot
         succeed.
    130  KeyboardInterrupt — operator interrupt, emitted without a
         traceback (also caught in app.main around parser assembly).

    ``--retry N`` wraps the whole handler in retry.run_with_retry:
    RateLimitedError — the only typed error that clears on its own
    (docs/10 §7 recovery table) — re-invokes the handler after a
    jittered exponential backoff, up to N times. Session, checkpoint,
    cookie, fingerprint, and registry failures never clear on their
    own and map straight to their codes without retry.

    Args:
        fn: The command handler; invoked with ``args``. Its int return
            value is the exit code; None is coerced to 0.
        args: The parsed namespace, carrying the handler's own flags
            plus the common flags (``retry`` is read here).

    Returns:
        The process exit code per the contract table above.

    Note:
        Typed-error messages go to stderr; payload data stays on
        stdout (the emit() discipline) — stdout/stderr stay cleanly
        separable for piping.
    """
    retries = getattr(args, "retry", 0)
    try:
        if retries > 0:
            policy = RetryPolicy(max_retries=retries,
                                 retry_on=(RateLimitedError,))
            return run_with_retry(lambda: fn(args), policy) or 0
        return fn(args) or 0
    except DryRunComplete:
        # success sentinel, not an error: the plan itself is already on
        # stdout; one short line closes the run. Never retried and never
        # swallowed — it sits outside the FBGraphError family precisely
        # so surface degradation handlers cannot eat it before it lands.
        print("dry-run complete — 0 requests sent")
        return 0
    except DryRunRawSeamError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except FBGraphError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _exit_code(exc)
    except (CookieLoadError, FingerprintRejectedError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _exit_code(exc)
    except KeyboardInterrupt:
        return 130


def _exit_code(exc: FBGraphError | CookieLoadError | FingerprintRejectedError) -> int:
    """Isinstance-ordered exit-code lookup for one typed error.

    An exact-class ``_EXIT_CODES.get(type(exc))`` misses SUBCLASSES:
    ``RegistryLoadError`` (a ``RegistryMissError`` subclass raised by a
    corrupt-but-present registry file) resolved to the generic 2 instead
    of its family's 6. Walking the table with isinstance restores the
    contract for every typed family — GraphQL and cookie/fingerprint
    alike: any future subclass exits with its family's code, never the
    generic 2.
    """
    for etype, code in _EXIT_CODES.items():
        if isinstance(exc, etype):
            return code
    return 2


def _abspath(p: str) -> Path:
    """Absolute, symlink-resolved form of a user-supplied path override."""
    return Path(p).resolve()
