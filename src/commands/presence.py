"""Presence commands: the chat-visibility status read (docs/02 §2
presence family) — read-only.

The chat_visibility flag is genuinely tri-state on the wire: True
(online/visible), False (offline), or absent (unknown) — `status`
renders all three distinctly rather than coercing to a boolean.

USER-DOC ANCHOR: cli/docs/04-reference-feed.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse

from surfaces.presence import PresenceService

from .common import add_common_args, emit, new_session, with_session


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the `presence` family (status) onto the CLI parser."""
    parser = sub.add_parser("presence",
                            help="presence surface: chat-visibility status")
    add_common_args(parser)
    group = parser.add_subparsers(dest="presence_command", required=True)

    status = group.add_parser("status",
                              help="read the viewer's chat-visibility setting")
    add_common_args(status)
    status.set_defaults(fn=cmd_status)


def cmd_status(args: argparse.Namespace) -> int:
    """Read the viewer's chat-visibility setting.

    Returns:
        0 — the tri-state flag (online/offline/unknown) is a valid
        observation in every branch; it is never a failure condition.
    """
    with with_session(new_session(args)) as session:
        service = PresenceService(session)
        status = service.status()
        visible = status["chat_visibility"]

        def human() -> None:
            if visible is True:
                state = "online (visible)"
            elif visible is False:
                state = "offline"
            else:
                state = "unknown"
            print(f"chat visibility: {state}")

        emit(args, {"status": status}, human=human)
        return 0
