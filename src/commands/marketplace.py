"""Marketplace commands: browse the home feed, search listings, and decode
one listing in full (docs/02 §2.8 — live-pattern, read-only).

Marketplace pagination is among the tightest surfaces (docs/10 §2) —
deep walks degrade to empty/challenge payloads before reads elsewhere
show any pressure. browse/search emit typed listing rows; `item`
decodes one listing's full field set (title, price, seller, images).

USER-DOC ANCHOR: cli/docs/05-reference-content.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""

from __future__ import annotations

import argparse

from domain.common import SearchResult
from surfaces.marketplace import MarketplaceService

from .common import add_common_args, emit, new_session, with_session


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the marketplace family onto the root parser: browse, search,
    item."""
    mp = sub.add_parser("marketplace", help="marketplace surface (browse feed)")
    msub = mp.add_subparsers(dest="marketplace_command", required=True)
    browse = msub.add_parser("browse", help="browse the marketplace home feed")
    add_common_args(browse)
    browse.add_argument(
        "--limit", type=int, default=20, help="maximum listings to emit (default 20)"
    )
    browse.set_defaults(fn=cmd_browse)

    search = msub.add_parser("search", help="search marketplace listings")
    add_common_args(search)
    search.add_argument("query", help="search term")
    search.add_argument(
        "--limit", type=int, default=10, help="maximum listings to emit (default 10)"
    )
    search.set_defaults(fn=cmd_search)

    item = msub.add_parser("item", help="decode one listing in full")
    add_common_args(item)
    item.add_argument("--item-id", required=True, help="numeric marketplace item id")
    item.set_defaults(fn=cmd_item)


def cmd_browse(args: argparse.Namespace) -> int:
    """Browse the marketplace home feed (CometMarketplace home query,
    docs/02 §2.8).

    Returns:
        0 — an empty feed is a valid result (and, per docs/10 §2, a
        possible soft-throttle signature on a hot session).
    """
    with with_session(new_session(args)) as session:
        service = MarketplaceService(session)
        listings = service.browse(limit=args.limit)

        def human() -> None:
            print(f"listings: {len(listings)}")
            for item in listings:
                print(f"[{item.typename}] {item.name or '?'} {item.snippet or ''} {item.url or ''}")

        emit(
            args,
            {"listings": [item.model_dump() for item in listings], "count": len(listings)},
            human=human,
        )
        return 0


def cmd_search(args: argparse.Namespace) -> int:
    """Search marketplace listings by free-text query (docs/02 §2.8).

    Returns:
        0 — zero hits is valid; search surfaces enforce the tightest
        per-datr quotas (docs/10 §2), so thin results on repeated
        queries are an enforcement signal, not a bug.
    """
    with with_session(new_session(args)) as session:
        service = MarketplaceService(session)
        listings: list[SearchResult] = service.search(args.query, limit=args.limit)

        def human() -> None:
            print(f"listings for {args.query!r}: {len(listings)}")
            for item in listings:
                print(f"[{item.typename}] {item.name or '?'} {item.snippet or ''} {item.url or ''}")

        emit(
            args,
            {
                "query": args.query,
                "listings": [item.model_dump() for item in listings],
                "count": len(listings),
            },
            human=human,
        )
        return 0


def cmd_item(args: argparse.Namespace) -> int:
    """Decode one listing in full by numeric item id (the
    /marketplace/item/<id>/ data plane, docs/02 §2.8).

    Human output prints the decoded fields — seller, price, location,
    description head, image URLs — all of which stay intact in the
    --json payload.

    Returns:
        0 — an unparsable/missing listing surfaces as a typed error
        via run_command, not as a silent zero-field success.
    """
    with with_session(new_session(args)) as session:
        service = MarketplaceService(session)
        detail = service.item_detail(args.item_id)

        def human() -> None:
            seller = detail.get("seller") or {}
            print(f"item {detail.get('id')}: {detail.get('title') or '?'}")
            print(f"price: {detail.get('price') or '?'}"
                  f"  location: {detail.get('location') or '?'}")
            print(f"seller: {seller.get('name') or '?'} {seller.get('id') or ''}")
            description = detail.get("description") or ""
            print(f"description: {description[:280] or '?'}")
            images = detail.get("images") or []
            print(f"images: {len(images)}")
            for url in images:
                print(f"  {url}")

        emit(args, {"item": detail}, human=human)
        return 0
