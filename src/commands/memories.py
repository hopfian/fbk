"""Memories commands: the throwback feed read (docs/02 §2 media family;
CometMemoriesFeedQuery replay, docs/15 §P2-2) — read-only.

USER-DOC ANCHOR: cli/docs/04-reference-feed.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse
from typing import Any

from surfaces.memories import MemoriesService

from .common import add_common_args, emit, new_session, with_session


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the memories family (feed) onto the root parser."""
    memories = sub.add_parser("memories", help="memories surface (feed)")
    msub = memories.add_subparsers(dest="memories_command", required=True)

    feed = msub.add_parser("feed", help="the memories feed "
                            "(CometMemoriesFeedQuery replay)")
    add_common_args(feed)
    feed.add_argument("--limit", type=int, default=10,
                      help="maximum memory cards to emit (default 10)")
    feed.set_defaults(fn=cmd_feed)


def _print_memories(cards: list[dict[str, Any]]) -> None:
    """Human output: one line per memory card; an empty throwback (the
    live-observed field_type_no_match account state) prints a graceful
    'no memories yet' instead of a bare zero count."""
    if not cards:
        print("no memories yet")
        return
    print(f"memories: {len(cards)}")
    for c in cards:
        print(f"[{c.get('typename') or '?'}] "
              f"{c.get('date_text') or c.get('creation_time') or ''} "
              f"{c.get('text') or ''} ({c.get('id')})")


def cmd_feed(args: argparse.Namespace) -> int:
    """Read the memories (throwback) feed.

    Returns:
        0 — an empty feed prints "no memories yet" (the live-observed
        field_type_no_match account state is normal, not an error).
    """
    with with_session(new_session(args)) as session:
        service = MemoriesService(session)
        cards = service.feed(limit=args.limit)
        emit(args, {"memories": cards, "count": len(cards)},
             human=lambda: _print_memories(cards))
        return 0
