"""Video commands: the /watch/ feed and the unseen badge (docs/02 §2.6
media family; docs/15 live calibration) — read-only.

Watch pagination chains off the last video id of the previous page
(--seed-video-id) in addition to the cursor — the /watch/ surface's
own chaining convention, unlike the feed's cursor-only pagination.

USER-DOC ANCHOR: cli/docs/05-reference-content.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse
from typing import Any

from surfaces.video import VideoService

from .common import add_common_args, emit, new_session, with_session


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the `video` family onto the root parser: feed, badge."""
    video = sub.add_parser("video", help="video/watch surface (feed + badge)")
    vsub = video.add_subparsers(dest="video_command", required=True)

    feed_p = vsub.add_parser("feed", help="browse the /watch/ video feed")
    feed_p.add_argument("--limit", type=int, default=10,
                        help="maximum videos to emit (default 10)")
    feed_p.add_argument("--cursor", default=None,
                        help="chaining cursor from a previous page")
    feed_p.add_argument("--seed-video-id", default=None,
                        help="seed video id for chaining pagination "
                             "(the last video id of the previous page)")
    add_common_args(feed_p)
    feed_p.set_defaults(fn=cmd_feed)

    badge_p = vsub.add_parser("badge", help="unseen video count for the Watch tab")
    add_common_args(badge_p)
    badge_p.set_defaults(fn=cmd_badge)


def _print_videos(videos: list[dict[str, Any]], count: int) -> None:
    """Human output: one line per video — id, title head, owner, url —
    preceded by the page-level count."""
    print(f"videos: {count}")
    for v in videos:
        print(f"[{v['id']}] {v['title'] or '(no title)'} — "
              f"{v['owner_name'] or v['owner_id'] or '?'} {v['url'] or ''}")


def cmd_feed(args: argparse.Namespace) -> int:
    """Browse the /watch/ video feed (cursor + seed-video chaining).

    Pass BOTH the previous page's end_cursor and its last video id to
    chain correctly — the /watch/ surface keys continuation on the
    pair (see module header).

    Returns:
        0 — an empty feed is valid state, not a failure.
    """
    with with_session(new_session(args)) as session:
        service = VideoService(session)
        page: dict[str, Any] = service.watch_feed(
            limit=args.limit, cursor=args.cursor,
            seed_video_id=args.seed_video_id)
        videos = page["videos"]
        emit(args, {"videos": videos, "count": page["count"],
                    "end_cursor": page["end_cursor"],
                    "has_next_page": page["has_next_page"]},
             human=lambda: _print_videos(videos, page["count"]))
        return 0


def cmd_badge(args: argparse.Namespace) -> int:
    """Read the unseen-video count for the Watch tab.

    Returns:
        0 — the count is valid whatever its value.
    """
    with with_session(new_session(args)) as session:
        service = VideoService(session)
        count: int = service.badge()
        emit(args, {"unseen": count},
             human=lambda: print(f"unseen videos: {count}"))
        return 0
