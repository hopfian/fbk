"""Notifications commands: badge count + dropdown list + pagination
(docs/02 §2.3; live-calibrated badge/dropdown queries, docs/15 §P2-2).

badge/list are plain reads; `list --page/--all` walks the pagination
query with cursor chaining (the /notifications/ page's own scroll
mechanics); `mark-seen` fires the exact mutation the notifications UI
fires on open — a mutation despite reading like one, and budgeted as
such (docs/10 §2).

USER-DOC ANCHOR: cli/docs/04-reference-feed.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""

from __future__ import annotations

import argparse
from typing import Any

from domain.common import Notification, NotificationCount
from surfaces.notifications import (
    MAX_WALK_PAGES,
    NotificationsService,
)

from .common import add_common_args, emit, new_session, with_session


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the notifications family onto the root parser: badge, list,
    mark-seen."""
    nf = sub.add_parser("notifications", help="notifications surface (badge + list)")
    nsub = nf.add_subparsers(dest="notifications_command", required=True)

    badge = nsub.add_parser("badge", help="unseen notifications count")
    add_common_args(badge)
    badge.set_defaults(fn=cmd_badge)

    listing = nsub.add_parser("list", help="recent notifications (dropdown query)")
    add_common_args(listing)
    listing.add_argument(
        "--limit", type=int, default=20,
        help="rows per page in --page/--all modes, or the dropdown cap "
             "otherwise (default 20)",
    )
    paging = listing.add_mutually_exclusive_group()
    paging.add_argument(
        "--page", type=int, metavar="N", default=None,
        help="show page N of the full paginated list (1-based; every "
             "earlier page is fetched for its walking cursor)",
    )
    paging.add_argument(
        "--all", action="store_true",
        help="walk the full paginated history (stops at "
             f"{MAX_WALK_PAGES} pages)",
    )
    listing.set_defaults(fn=cmd_list)

    mark = nsub.add_parser(
        "mark-seen",
        help="mark the listed notifications seen (exactly what opening the notifications UI does)",
    )
    add_common_args(mark)
    mark.set_defaults(fn=cmd_mark_seen)


def cmd_badge(args: argparse.Namespace) -> int:
    """Read the unseen-notifications count (the nav badge query — the
    verified cheapest live read, docs/15 §P2-2; the measure latency
    canary rides on it).

    Returns:
        0 — the count is valid whatever its value.
    """
    with with_session(new_session(args)) as session:
        service = NotificationsService(session)
        count: NotificationCount = service.badge()
        emit(args, count.model_dump(), human=lambda: print(f"unseen: {count.unseen}"))
        return 0


def cmd_list(args: argparse.Namespace) -> int:
    """List recent notifications — dropdown by default, paginated full
    history with --page/--all (docs/02 §2.3).

    Human lines show read/unread state, title, body, and the
    notification id; paginated modes annotate the header with the page
    or pages-walked context.

    Returns:
        0 — zero notifications is valid account state; 2 — a non-
        positive --page value (the index is 1-based).
    """
    if getattr(args, "page", None) is not None and args.page < 1:
        print("error: --page is 1-based, the first page is 1")
        return 2
    with with_session(new_session(args)) as session:
        service = NotificationsService(session)
        notifications, extra = _list_rows(service, args)

        def human() -> None:
            print(f"notifications: {len(notifications)}{extra['header']}")
            for n in notifications:
                state = "unread" if n.unseen else "read"
                print(f"[{state}] {n.title or n.typename or '?'}: {n.body or ''} ({n.id})")

        payload: dict[str, Any] = {
            "notifications": [n.model_dump() for n in notifications],
            "count": len(notifications),
        }
        payload.update(extra["fields"])
        emit(args, payload, human=human)
        return 0


def _list_rows(service: NotificationsService,
               args: argparse.Namespace) -> tuple[list[Notification], dict[str, Any]]:
    """Resolve the requested list mode into rows + emit context.

    Returns:
        ``(rows, context)`` where context carries the human header
        suffix and the extra JSON fields for the active mode
        (dropdown: neither; --page: page/has_next/next_cursor; --all:
        pages/has_next).
    """
    if getattr(args, "page", None) is not None:
        page = service.page_at(args.page, count=args.limit)
        rows: list[Notification] = page["rows"]
        context: dict[str, Any] = {
            "header": f" (page {page['page']})",
            "fields": {
                "page": page["page"],
                "has_next": page["has_next"],
                "next_cursor": page["next_cursor"],
            },
        }
        return rows, context
    if getattr(args, "all", False):
        result = service.paginate(count=args.limit)
        rows = result["notifications"]
        suffix = " (history ended)" if not result["has_next"] else \
            f" (capped at {result['pages']} pages, more remain)"
        return rows, {
            "header": f" (pages walked: {result['pages']}){suffix}",
            "fields": {
                "pages": result["pages"],
                "has_next": result["has_next"],
            },
        }
    rows = service.list(limit=args.limit)
    return rows, {"header": "", "fields": {}}


def cmd_mark_seen(args: argparse.Namespace) -> int:
    """Mark the listed notifications seen — exactly what opening the
    notifications UI does (docs/02 §2.3).

    A mutation in read's clothing: it draws on the shared write budget
    (docs/10 §2) despite feeling like a passive refresh.

    Returns:
        0 — the payload reports the marked count and the unseen
        remainder.
    """
    with with_session(new_session(args)) as session:
        service = NotificationsService(session)
        result = service.mark_seen()
        unseen = result.get("unseen")

        def human() -> None:
            print(f"marked seen: {result.get('marked')}")
            if unseen is not None:
                print(f"unseen now: {unseen}")

        emit(args, result, human=human)
        return 0
