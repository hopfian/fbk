"""Stories commands: the tray read + the story lifecycle (docs/02 §2 media
family; tray replay docs/15 §P2-2; create/viewers/reply bundle-decoded
2026-09-20 per the surface CALIBRATION NOTES).

create publishes a text (SATP) story by default, a photo story with
--photo, or a video story with --video; viewers/reply operate on an
existing story card id from the tray.

USER-DOC ANCHOR: cli/docs/04-reference-feed.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse
from typing import Any

from surfaces.stories import StoriesService

from .common import add_common_args, emit, new_session, with_session

#: CLI audience spellings -> the wire privacy base-state table keys
#: (constants.PRIVACY_BASE_STATES does the CLI->wire mapping).
_AUDIENCE_NAMES = ["public", "friends", "private"]


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the stories family onto the root parser: tray, create,
    viewers, reply."""
    stories = sub.add_parser(
        "stories", help="stories surface (tray + create/viewers/reply)")
    ssub = stories.add_subparsers(dest="stories_command", required=True)

    tray = ssub.add_parser(
        "tray",
        help="the stories tray tiles (StoriesTrayRectangularRootQuery replay)")
    add_common_args(tray)
    tray.add_argument("--limit", type=int, default=20,
                      help="maximum tray tiles to emit (default 20)")
    tray.set_defaults(fn=cmd_tray)

    create = ssub.add_parser(
        "create",
        help="publish a story: text by default, --photo or --video for media")
    add_common_args(create)
    media = create.add_mutually_exclusive_group()
    media.add_argument("--text", metavar="TEXT", default=None,
                       help="publish a TEXT story with this text (the default mode)")
    media.add_argument("--photo", metavar="PATH", default=None,
                       help="publish a PHOTO story from an image file")
    media.add_argument("--video", metavar="PATH", default=None,
                       help="publish a VIDEO story from a video file")
    create.add_argument("--story-text", metavar="TEXT", default=None,
                        help="text for the TEXT story (alternative to --text)")
    create.add_argument("--audience", choices=_AUDIENCE_NAMES, default=None,
                        help="story audience (default: the account's own)")
    create.add_argument("--ai-label", choices=["on", "off"], default=None,
                        help="mark the story as self-disclosed AI content")
    create.add_argument("--font-id", default=None,
                        help="custom font id for TEXT stories (SATP font picker)")
    create.add_argument("--preset-id", default=None,
                        help="background style preset id for TEXT stories")
    create.set_defaults(fn=cmd_create)

    viewers = ssub.add_parser(
        "viewers", help="a story's seen-by viewer list (viewer-sheet query)")
    add_common_args(viewers)
    viewers.add_argument("--story-id", required=True,
                         help="the story card id (from the tray tiles)")
    viewers.add_argument("--limit", type=int, default=20,
                         help="maximum viewer rows (default 20)")
    viewers.set_defaults(fn=cmd_viewers)

    reply = ssub.add_parser("reply", help="reply to a story with text")
    add_common_args(reply)
    reply.add_argument("--story-id", required=True,
                       help="the story card id (from the tray tiles)")
    reply.add_argument("--text", required=True, help="the reply text")
    reply.set_defaults(fn=cmd_reply)

    presets = ssub.add_parser(
        "presets", help="the SATP text-story background style presets "
                        "(live composer catalog)")
    add_common_args(presets)
    presets.set_defaults(fn=cmd_presets)

    fonts = ssub.add_parser(
        "fonts", help="the SATP text-story custom font catalog (live read)")
    add_common_args(fonts)
    fonts.set_defaults(fn=cmd_fonts)

    audience = ssub.add_parser(
        "audience", help="the story audience state: default mode + catalog")
    add_common_args(audience)
    audience.set_defaults(fn=cmd_audience)


def _story_id_of(result: dict[str, Any]) -> str | None:
    """The new story's id from a create response (story_create.story_id or
    the first items[].story.id - both decoded selection paths)."""
    create = result.get("data", {}).get("story_create", {})
    if isinstance(create, dict):
        sid = create.get("story_id")
        if isinstance(sid, str):
            return sid
        items = create.get("items")
        if isinstance(items, list) and items:
            story = items[0].get("story", {}) if isinstance(items[0], dict) else {}
            if isinstance(story, dict) and isinstance(story.get("id"), str):
                return str(story["id"])
    return None


def cmd_tray(args: argparse.Namespace) -> int:
    """Read the stories tray tiles (preload replay).

    Human lines show live/seen/new state per tile - the exact states
    the UI surfaces.

    Returns:
        0 - an empty tray is valid account state.
    """
    with with_session(new_session(args)) as session:
        service = StoriesService(session)
        tiles = service.tray(limit=args.limit)

        def human() -> None:
            print(f"tray tiles: {len(tiles)}")
            for t in tiles:
                state = ("live" if t.get("is_live") else "seen" if t.get("is_seen")
                         else "new")
                print(f"[{state}] {t.get('owner_name') or t.get('owner_id') or '?'} "
                      f"({t.get('card_count')} cards)")

        emit(args, {"tiles": tiles, "count": len(tiles)}, human=human)
        return 0


def cmd_create(args: argparse.Namespace) -> int:
    """Publish a story - text (SATP), photo, or video.

    Returns:
        0 on a successful publish; 2 when no story text/content was
        given at all.
    """
    privacy = args.audience.upper() if args.audience else None
    ai_label = args.ai_label == "on"
    with with_session(new_session(args)) as session:
        service = StoriesService(session)
        text = args.text if args.text is not None else args.story_text
        if args.photo is not None:
            result = service.create_photo(
                args.photo, privacy=privacy, ai_label=ai_label)
            kind, media_id = "photo", result.get("photo_id")
            story = result.get("story", {})
        elif args.video is not None:
            result = service.create_video(
                args.video, privacy=privacy, ai_label=ai_label)
            kind, media_id = "video", result.get("video_id")
            story = result.get("story", {})
        else:
            if not text:
                print("error: a story needs --text (or --photo/--video)")
                return 2
            story = service.create_text(
                text, privacy=privacy, ai_label=ai_label,
                font_id=args.font_id, preset_id=args.preset_id)
            kind, media_id = "text", None
        story_id = _story_id_of(story)

        def human() -> None:
            label = kind if media_id is None else f"{kind} ({media_id})"
            print(f"story created: {label}")
            print(f"story id: {story_id or '(not echoed)'}")

        emit(args, {"kind": kind, "media_id": media_id,
                    "story_id": story_id, "story": story}, human=human)
        return 0


def cmd_viewers(args: argparse.Namespace) -> int:
    """Read a story's seen-by list.

    Returns:
        0 - zero viewers is valid for a fresh or unseen story.
    """
    with with_session(new_session(args)) as session:
        service = StoriesService(session)
        rows = service.viewers(args.story_id, limit=args.limit)

        def human() -> None:
            print(f"viewers: {len(rows)}")
            for r in rows:
                print(f"{r.get('name') or '?'} ({r.get('user_id')})")

        emit(args, {"viewers": rows, "count": len(rows)}, human=human)
        return 0


def cmd_reply(args: argparse.Namespace) -> int:
    """Reply to a story with text (the viewer-sheet's own commit).

    Returns:
        0 on a successful reply.
    """
    with with_session(new_session(args)) as session:
        service = StoriesService(session)
        result = service.reply(args.story_id, args.text)

        def human() -> None:
            print(f"reply sent to story {args.story_id}")

        emit(args, {"story_id": args.story_id, "result": result}, human=human)
        return 0


def cmd_presets(args: argparse.Namespace) -> int:
    """The SATP background style preset catalog (live composer read).

    Returns:
        0 - an empty catalog degrades to a valid empty list.
    """
    with with_session(new_session(args)) as session:
        rows = StoriesService(session).presets()

        def human() -> None:
            print(f"presets: {len(rows)}")
            for r in rows:
                font = f" (font {r.get('font_id')})" if r.get("font_id") else ""
                bg = ", background image" if r.get("has_background_image") else ""
                print(f"{r.get('preset_id')}{font}{bg}")

        emit(args, {"presets": rows, "count": len(rows)}, human=human)
        return 0


def cmd_fonts(args: argparse.Namespace) -> int:
    """The SATP custom font catalog (live read).

    Returns:
        0 - an empty catalog degrades to a valid empty list.
    """
    with with_session(new_session(args)) as session:
        rows = StoriesService(session).fonts()

        def human() -> None:
            print(f"fonts: {len(rows)}")
            for r in rows:
                print(f"{r.get('name')} ({r.get('id')})")

        emit(args, {"fonts": rows, "count": len(rows)}, human=human)
        return 0


def cmd_audience(args: argparse.Namespace) -> int:
    """The story audience state: default mode + the mode catalog.

    Returns:
        0 - the payload reports both reads.
    """
    with with_session(new_session(args)) as session:
        state = StoriesService(session).audience()

        def human() -> None:
            print(f"default story audience: {state.get('default_mode') or '?'}")
            for m in state.get("modes", []):
                mark = " *" if m.get("mode") == state.get("default_mode") else ""
                print(f"{m.get('header') or m.get('mode')}{mark}: "
                      f"{m.get('description') or ''}")

        emit(args, state, human=human)
        return 0
