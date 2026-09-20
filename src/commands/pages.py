"""Pages commands: create, like, follow, feed (docs/02 §2.5 pages
namespace, §2.10 engagement family).

Each command builds a PagesService from the session, calls one
operation, and emits a trimmed payload: only the top-level ``data``
keys with id-ish fields survive the trim, so huge Relay payloads never
hit the terminal. `feed --page-id` accepts the numeric page id OR the
vanity slug (the /pages/... vanity namespace, docs/02 §2.5) — the
service resolves both.

USER-DOC ANCHOR: cli/docs/06-reference-people.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse
from typing import Any

from surfaces.pages import PagesService

from .common import add_common_args, emit, new_session, with_session
from .render import print_story_page, story_payload


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the `pages` family onto the CLI parser: create, like,
    follow, feed."""
    parser = sub.add_parser("pages", help="pages surface: create, like, follow")
    add_common_args(parser)
    group = parser.add_subparsers(dest="pages_command", required=True)

    create = group.add_parser("create", help="create a new Page")
    add_common_args(create)
    create.add_argument("--name", required=True, help="page name")
    create.add_argument("--category", default=None,
                        help="page category key (e.g. 'Public Figure')")
    create.set_defaults(fn=cmd_create)

    like = group.add_parser("like", help="like a Page")
    add_common_args(like)
    like.add_argument("--page-id", required=True, help="numeric page id")
    like.set_defaults(fn=cmd_like)

    follow = group.add_parser("follow", help="follow a Page")
    add_common_args(follow)
    follow.add_argument("--page-id", required=True, help="numeric page id")
    follow.set_defaults(fn=cmd_follow)

    feed = group.add_parser("feed", help="read a page's timeline feed")
    add_common_args(feed)
    feed.add_argument("--page-id", required=True,
                      help="page numeric id or vanity slug")
    feed.add_argument("--limit", type=int, default=20,
                      help="max stories to return (default 20)")
    feed.set_defaults(fn=cmd_feed)


def _trim(response: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy a merged GraphQL payload, keeping data's top-level keys and
    the id-ish fields inside (one nesting level deep)."""
    data = response.get("data") or {}
    trimmed: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            kept = {field: item for field, item in value.items()
                    if "id" in field.lower() or field == "__typename"}
            for field, item in value.items():
                if isinstance(item, dict):
                    nested = {name: sub for name, sub in item.items()
                              if "id" in name.lower() or name == "__typename"}
                    if nested:
                        kept[field] = nested
            trimmed[key] = kept
        else:
            trimmed[key] = value
    return {"data": trimmed}


def _first_id(response: dict[str, Any]) -> str:
    """The first id-shaped value in the payload's data (for humans)."""
    for value in (response.get("data") or {}).values():
        if isinstance(value, dict):
            for field in ("id", "page_id", "user_id"):
                if field in value:
                    return str(value[field])
            for nested in value.values():
                if isinstance(nested, dict) and "id" in nested:
                    return str(nested["id"])
    return "ok"


def cmd_create(args: argparse.Namespace) -> int:
    """Create a new Page (PagesService.create, docs/02 §2.5).

    ``--category`` seeds the page category key; an absent category
    lets the server default it.

    Returns:
        0 — the created page id prints from the first id-ish data
        field (_first_id).
    """
    with with_session(new_session(args)) as session:
        service = PagesService(session)
        response = service.create(args.name, category=args.category)
        page_id = _first_id(response)
        emit(args, {"response": _trim(response)},
             human=lambda: print(f"page created: {page_id}"))
        return 0


def cmd_like(args: argparse.Namespace) -> int:
    """Like a Page by numeric id (page-like mutation, docs/02 §2.10).

    Returns:
        0 — in-band echo; likes are mutations and draw on the
        reaction-class write budget (docs/10 §2).
    """
    with with_session(new_session(args)) as session:
        service = PagesService(session)
        response = service.like(args.page_id)
        emit(args, {"response": _trim(response)},
             human=lambda: print(f"page {args.page_id} liked: "
                                 f"{_first_id(response)}"))
        return 0


def cmd_follow(args: argparse.Namespace) -> int:
    """Follow a Page by numeric id (follow-toggle mutation family,
    docs/02 §2.18/§2.10).

    Returns:
        0 — in-band echo; same write-budget discipline as like.
    """
    with with_session(new_session(args)) as session:
        service = PagesService(session)
        response = service.follow(args.page_id)
        emit(args, {"response": _trim(response)},
             human=lambda: print(f"page {args.page_id} followed: "
                                 f"{_first_id(response)}"))
        return 0


def cmd_feed(args: argparse.Namespace) -> int:
    """Read a Page's timeline feed (docs/02 §2.5).

    ``--page-id`` takes the numeric page id OR the vanity slug —
    PagesService.feed_read resolves both to the same timeline query;
    ``--limit`` caps the emitted stories.

    Returns:
        0 — an empty page timeline is valid state, not a failure.
    """
    with with_session(new_session(args)) as session:
        service = PagesService(session)
        page = service.feed_read(args.page_id, limit=args.limit)
        payload = {
            "stories": [story_payload(s) for s in page.stories],
            "end_cursor": page.end_cursor,
            "has_next_page": page.has_next_page,
        }
        emit(args, payload, human=lambda: print_story_page(page))
        return 0
