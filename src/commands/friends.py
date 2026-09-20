"""Friends commands (docs/02 §2.9 friending family): list, requests,
suggestions + the request/cancel/accept/decline/unfriend mutations and
the nav-badge clear.

Friending mutations are the classic per-(uid, target) anti-stalking
bucket residents (docs/10 §1) — request velocity toward many fresh
targets is the shape Facebook's coordination detection was built
around. All mutation handlers emit through _emit_mutation's one-level
trim: enough payload to confirm which fields came back, without
dumping PII.

USER-DOC ANCHOR: cli/docs/06-reference-people.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""

from __future__ import annotations

import argparse
from typing import Any

from domain.common import User
from surfaces.friends import (
    CANCEL_MUTATION,
    CLEAR_BADGE_MUTATION,
    CONFIRM_MUTATION,
    DELETE_MUTATION,
    SEND_MUTATION,
    UNFRIEND_MUTATION,
    FriendsService,
)

from .common import add_common_args, emit, new_session, with_session


def _one_level(data: dict[str, Any]) -> dict[str, Any]:
    """data.* trimmed to one level: dict values -> their keys, lists -> a
    length fingerprint (emit-safe views of the mutation payloads)."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            out[key] = sorted(value)
        elif isinstance(value, list):
            out[key] = f"<list:{len(value)}>"
        else:
            out[key] = value
    return out


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the friends family onto the root parser: list, requests,
    suggestions, the five verb mutations, clear-badge."""
    fr = sub.add_parser(
        "friends",
        help="friending family (friends list, requests, request/cancel/accept/decline/unfriend)",
    )
    fsub = fr.add_subparsers(dest="friends_command", required=True)

    listing = fsub.add_parser("list", help="friend rows from the friends-page root query")
    add_common_args(listing)
    listing.add_argument(
        "--limit", type=int, default=50, help="maximum friend rows to emit (default 50)"
    )
    listing.set_defaults(fn=cmd_list)

    reqs = fsub.add_parser("requests", help="incoming + outgoing friend requests")
    add_common_args(reqs)
    reqs.add_argument(
        "--direction",
        choices=("incoming", "outgoing", "all"),
        default="all",
        help="which side to show (default all)",
    )
    reqs.set_defaults(fn=cmd_requests)

    sugg = fsub.add_parser("suggestions", help="people-you-may-know rows (pymk_grid)")
    add_common_args(sugg)
    sugg.add_argument(
        "--limit", type=int, default=10, help="maximum suggestion rows to emit (default 10)"
    )
    sugg.set_defaults(fn=cmd_suggestions)

    # Static verb->handler dispatch: mypy verifies every reference at
    # import time (the old globals() lookup drifted silently when a
    # verb and its cmd_<verb> diverged).
    verb_fns = {
        "request": cmd_request,
        "cancel": cmd_cancel,
        "accept": cmd_accept,
        "decline": cmd_decline,
        "unfriend": cmd_unfriend,
    }
    for verb, help_text in (
        ("request", "send a friend request"),
        ("cancel", "cancel your own outgoing request"),
        ("accept", "accept an incoming request"),
        ("decline", "decline/delete an incoming request"),
        ("unfriend", "remove an existing friend"),
    ):
        p = fsub.add_parser(verb, help=help_text)
        add_common_args(p)
        p.add_argument(
            "--user-id", required=True, dest="user_id", help="target user id (numeric fbid)"
        )
        p.set_defaults(fn=verb_fns[verb])

    badge = fsub.add_parser("clear-badge", help="clear the friends-nav badge counter")
    add_common_args(badge)
    badge.set_defaults(fn=cmd_clear_badge)


def cmd_list(args: argparse.Namespace) -> int:
    """List friend rows from the friends-page root query (docs/02 §2.9).

    Returns:
        0 — zero friends is valid account state.
    """
    with with_session(new_session(args)) as session:
        service = FriendsService(session)
        friends: list[User] = service.list(limit=args.limit)

        def human() -> None:
            print(f"friends: {len(friends)}")
            for f in friends:
                print(f"{f.name} {f.id}")

        emit(args, {"friends": [f.model_dump() for f in friends],
                   "count": len(friends)}, human=human)
        return 0


def cmd_requests(args: argparse.Namespace) -> int:
    """List incoming and/or outgoing friend requests.

    ``--direction`` filters to one side or both; the payload always
    carries the incoming/outgoing split regardless.

    Returns:
        0 — an empty request set is valid state.
    """
    with with_session(new_session(args)) as session:
        service = FriendsService(session)
        split = service.requests(direction=args.direction)

        def human() -> None:
            for direction, rows in (("incoming", split["incoming"]),
                                    ("outgoing", split["outgoing"])):
                print(f"{direction}: {len(rows)}")
                for row in rows:
                    print(f"{row.get('name')} {row.get('id')}")

        emit(args, split, human=human)
        return 0


def cmd_suggestions(args: argparse.Namespace) -> int:
    """List people-you-may-know rows (the pymk_grid read).

    Returns:
        0 — suggestion sets vary server-side; emptiness is not an
        error signal.
    """
    with with_session(new_session(args)) as session:
        service = FriendsService(session)
        suggestions: list[User] = service.suggestions(limit=args.limit)

        def human() -> None:
            print(f"suggestions: {len(suggestions)}")
            for u in suggestions:
                print(f"{u.name} {u.id}")

        emit(
            args,
            {"suggestions": [u.model_dump() for u in suggestions], "count": len(suggestions)},
            human=human,
        )
        return 0


def _emit_mutation(args: argparse.Namespace, friendly: str, response: dict[str, Any]) -> int:
    """Emit a friending-mutation result under the one-level trim.

    The trim keeps data's top-level keys — dict values reduced to
    their key sets, lists to length fingerprints — enough to confirm
    which fields came back without dumping PII.

    Returns:
        0 — in-band echo only. Friending soft-failures are invisible
        in-band (docs/10 §3.4): "no data" is the observable signature,
        never a confirmed effect.
    """
    data = response.get("data", {}) if isinstance(response, dict) else {}
    payload = {"mutation": friendly, "data": _one_level(data)}
    emit(args, payload, human=lambda: print(f"{friendly}: {sorted(data) if data else 'no data'}"))
    return 0


def cmd_request(args: argparse.Namespace) -> int:
    """Send a friend request (the send mutation). Draws on the
    per-target request buckets (docs/10 §1); acceptance-rate history
    weighs as heavily as raw count."""
    with with_session(new_session(args)) as session:
        response = FriendsService(session).request(args.user_id)
        return _emit_mutation(args, SEND_MUTATION, response)


def cmd_cancel(args: argparse.Namespace) -> int:
    """Cancel the viewer's own outgoing friend request (the cancel
    mutation) — the low-risk retraction path before a request ages."""
    with with_session(new_session(args)) as session:
        response = FriendsService(session).cancel(args.user_id)
        return _emit_mutation(args, CANCEL_MUTATION, response)


def cmd_accept(args: argparse.Namespace) -> int:
    """Accept an incoming friend request (the confirm mutation)."""
    with with_session(new_session(args)) as session:
        response = FriendsService(session).accept(args.user_id)
        return _emit_mutation(args, CONFIRM_MUTATION, response)


def cmd_decline(args: argparse.Namespace) -> int:
    """Decline (delete) an incoming friend request (the delete
    mutation)."""
    with with_session(new_session(args)) as session:
        response = FriendsService(session).decline(args.user_id)
        return _emit_mutation(args, DELETE_MUTATION, response)


def cmd_unfriend(args: argparse.Namespace) -> int:
    """Remove an existing friend (the unfriend mutation) — a durable
    graph change; unlike a declined request it is not silently
    reversible."""
    with with_session(new_session(args)) as session:
        response = FriendsService(session).unfriend(args.user_id)
        return _emit_mutation(args, UNFRIEND_MUTATION, response)


def cmd_clear_badge(args: argparse.Namespace) -> int:
    """Clear the friends-nav badge counter (the badge-clear mutation) —
    the exact write the UI fires on opening the friends nav."""
    with with_session(new_session(args)) as session:
        response = FriendsService(session).clear_badge()
        return _emit_mutation(args, CLEAR_BADGE_MUTATION, response)
