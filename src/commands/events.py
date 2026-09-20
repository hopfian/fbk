"""Events commands: list, feed, delete, create (docs/02 §2 events family;
decoded mutation inputs from the 2026-09-18 discovery).

Delete and create are MUTATIONS — schema-decoded during the 2026-09-18
discovery but never live-fired; execution is the operator's call
(docs/13 single-variable probe discipline: never fire a newly decoded
mutation incidentally). Both return 1 when the response carries no
confirming id — an unconfirmed mutation is a failed precondition
(docs/10 §3.4).

USER-DOC ANCHOR: cli/docs/05-reference-content.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse

from surfaces.events import (
    CREATE_MUTATION,
    DELETE_MUTATION,
    EventsService,
)

from .common import add_common_args, emit, new_session, with_session


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the events family onto the root parser: list, feed, delete,
    create."""
    events = sub.add_parser("events", help="events surface (list/feed/delete/create)")
    esub = events.add_subparsers(dest="events_command", required=True)

    listing = esub.add_parser("list", help="the /events/ list (preload replay)")
    add_common_args(listing)
    listing.add_argument("--limit", type=int, default=20,
                         help="maximum event rows to emit (default 20)")
    listing.set_defaults(fn=cmd_list)

    feed = esub.add_parser("feed", help="an event's discussion feed")
    add_common_args(feed)
    feed.add_argument("--event-id", required=True,
                     help="event id (numeric or Relay base64 form)")
    feed.add_argument("--limit", type=int, default=10,
                      help="maximum feed stories to emit (default 10)")
    feed.add_argument("--cursor", default=None,
                      help="pagination cursor from a previous feed page")
    feed.set_defaults(fn=cmd_feed)

    delete = esub.add_parser("delete", help="delete an event you host "
                            "(useEventCometDeleteMutation)")
    add_common_args(delete)
    delete.add_argument("--event-id", required=True,
                       help="event id to delete (numeric or Relay form)")
    delete.set_defaults(fn=cmd_delete)

    create = esub.add_parser("create", help="create an event "
                             "(EventCometLightweightCreateMutation)")
    add_common_args(create)
    create.add_argument("--name", required=True, help="event name")
    create.add_argument("--start-date", required=True,
                        help="start date (YYYY-MM-DD)")
    create.add_argument("--start-time", required=True,
                        help="start time (HH:MM, 24h, event timezone)")
    create.add_argument("--end-date", default=None, help="end date (YYYY-MM-DD)")
    create.add_argument("--end-time", default=None, help="end time (HH:MM)")
    create.add_argument("--timezone", default="UTC",
                        help="IANA timezone name (default UTC)")
    create.add_argument("--description", default=None, help="event description")
    create.add_argument("--privacy", default="public",
                        choices=("public", "private"),
                         help="event privacy (default public)")
    create.set_defaults(fn=cmd_create)


def cmd_list(args: argparse.Namespace) -> int:
    """List the viewer's events (the /events/ list preload replay).

    Human lines mark hosted events ([host]) — the ones `delete` may
    legally target.

    Returns:
        0 — zero events is valid account state.
    """
    with with_session(new_session(args)) as session:
        service = EventsService(session)
        rows = service.list(limit=args.limit)

        def human() -> None:
            print(f"events: {len(rows)}")
            for r in rows:
                tag = "[host]" if r.get("is_viewer_host") else "      "
                print(f"{tag} {r.get('name') or '?'} — {r.get('date_text') or ''} "
                      f"({r['id']})")

        emit(args, {"events": rows, "count": len(rows)}, human=human)
        return 0


def cmd_feed(args: argparse.Namespace) -> int:
    """Read one event's discussion feed (cursor-paginated).

    ``--event-id`` accepts the numeric or Relay base64 form;
    ``--cursor`` chains off a previous page.

    Returns:
        0 — an empty discussion feed is valid state.
    """
    with with_session(new_session(args)) as session:
        service = EventsService(session)
        page = service.feed(args.event_id, limit=args.limit, cursor=args.cursor)
        stories = page.get("stories", [])

        def human() -> None:
            print(f"event feed: {len(stories)} stories "
                  f"(next: {bool(page.get('has_next_page'))})")
            for s in stories:
                print(f"[{s.get('creation_time') or ''}] "
                      f"{(s.get('actor') or {}).get('name') or '?'}: "
                      f"{s.get('text') or ''}")

        emit(args, {"event_id": args.event_id,
                    "stories": stories,
                    "count": len(stories),
                    "end_cursor": page.get("end_cursor"),
                    "has_next_page": page.get("has_next_page")}, human=human)
        return 0


def cmd_delete(args: argparse.Namespace) -> int:
    """Delete an event the viewer hosts (useEventCometDeleteMutation —
    decoded, never live-fired; see module header).

    Returns:
        0 when the response carries canceled_event_id; 1 otherwise —
        an unconfirmed delete is a failed precondition and must be
        verified out-of-band before being believed (docs/10 §3.4).
    """
    with with_session(new_session(args)) as session:
        service = EventsService(session)
        response = service.delete(args.event_id)
        cancel = (response.get("data") or {}).get("event_cancel") or {}
        canceled = cancel.get("canceled_event_id")
        emit(args, {
            "mutation": DELETE_MUTATION,
            "event_id": args.event_id,
            "canceled_event_id": canceled,
            "response": response,
        }, human=lambda: print(f"deleted: {canceled}"))
        return 0 if canceled else 1


def cmd_create(args: argparse.Namespace) -> int:
    """Create an event (EventCometLightweightCreateMutation — decoded,
    never live-fired; see module header).

    ``--start-time`` is the event's own timezone (``--timezone``, IANA
    name); privacy maps to the PUBLIC_TYPE/PRIVATE_TYPE enum.

    Returns:
        0 when the response carries the created event id; 1 otherwise
        — an unconfirmed create is a failed precondition.
    """
    with with_session(new_session(args)) as session:
        service = EventsService(session)
        privacy = "PUBLIC_TYPE" if args.privacy == "public" else "PRIVATE_TYPE"
        response = service.create(
            args.name, args.start_date, args.start_time,
            end_date=args.end_date, end_time=args.end_time,
            timezone=args.timezone, description=args.description,
            privacy=privacy)
        event = ((response.get("data") or {}).get("fb_event_create") or {}).get("event") or {}
        created = event.get("id")
        emit(args, {
            "mutation": CREATE_MUTATION,
            "name": args.name,
            "event_id": created,
            "response": response,
        }, human=lambda: print(f"created: {args.name} ({created})"))
        return 0 if created else 1
