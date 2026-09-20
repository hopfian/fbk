"""Overview command: the aggregate dashboard (docs/15 cheap verified
reads).

One command, one pass: identity, notifications/watch badges, presence,
feed head, thread count, registry size — every component individually
guarded by surfaces/overview.py so one broken surface never blanks the
dashboard (a degraded component renders as an ERROR line, never fails
the command). Pattern follows commands/auth.py (add_common_args + emit).

USER-DOC ANCHOR: cli/docs/04-reference-feed.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse
from typing import Any

from surfaces.overview import OverviewService

from .common import add_common_args, emit, new_session, with_session


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the single `overview` dashboard command onto the root parser."""
    ov = sub.add_parser(
        "overview",
        help="aggregate dashboard: identity, badges, presence, feed head, threads")
    add_common_args(ov)
    ov.set_defaults(fn=cmd_overview)


def _is_error(value: Any) -> bool:
    """The {"error": "<type>: <msg>"} envelope a guarded failure yields."""
    return isinstance(value, dict) and "error" in value


def _presence_line(value: Any) -> str:
    """Render the tri-state chat-visibility flag (or a guarded error
    envelope) as its human dashboard line."""
    if _is_error(value):
        return f"ERROR: {value['error']}"
    vis = value.get("chat_visibility")
    if vis is True:
        return "online (visible)"
    if vis is False:
        return "offline"
    return "unknown"


def _count_line(label: str, value: Any, suffix: str) -> str:
    """Render one badge/count row — the guarded-error envelope renders
    as an ERROR line, a live count as "<label>: <n> <suffix>"."""
    if _is_error(value):
        return f"{label:<14}: ERROR: {value['error']}"
    return f"{label:<14}: {value} {suffix}"


def cmd_overview(args: argparse.Namespace) -> int:
    """Collect and render the aggregate dashboard in one pass.

    Every component (identity, badges, presence, feed head, threads,
    registry size) is an independent, guarded read — a broken surface
    degrades to an ERROR line and the rest still render. The command
    never fails on component errors by design; only typed transport
    errors (session, checkpoint) propagate to run_command.

    Returns:
        0 — always, short of a typed error from the session layer.
    """
    with with_session(new_session(args)) as session:
        data = OverviewService(session).collect()

        ident = data["identity"]
        if _is_error(ident):
            ident_line = f"ERROR: {ident['error']}"
        else:
            ident_line = (f"{ident.get('user_name') or '?'} "
                          f"({ident.get('user_id') or '?'}) [{ident.get('state') or '?'}]")

        feed = data["feed_head"]

        def human() -> None:
            print(f"identity      : {ident_line}")
            print(_count_line("notifications", data["notifications_badge"], "unseen"))
            print(_count_line("watch badge", data["watch_badge"], "unseen videos"))
            print(f"presence      : {_presence_line(data['presence'])}")
            if _is_error(feed):
                print(f"feed head     : ERROR: {feed['error']}")
            else:
                print(f"feed head     : {feed['count']} stories on page 1")
                for head in feed["heads"]:
                    print(f"                {head['actor'] or '?'} | "
                          f"{(head['text'] or '')[:60]}")
            print(_count_line("threads", data["thread_count"], "recent threads"))
            print(_count_line("registry", data["registry_size"], "doc ids"))

        emit(args, data, human=human)
        return 0
