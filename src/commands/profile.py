"""Profile commands: `profile me` + `profile view` (docs/02 §2.5
people/profiles surface).

Both read through ProfileService; `view` targets any profile's
timeline feed, and ``--user-id`` accepts the numeric user/page id OR
the vanity slug (the ``www.facebook.com/<vanity>`` namespace,
docs/02 §2.5) — the service resolves both to the same timeline query.
Story rendering goes through the shared render layer (commands.render)
so every timeline prints identically.

USER-DOC ANCHOR: cli/docs/04-reference-feed.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""

from __future__ import annotations

import argparse

from domain.common import Story
from surfaces.profile import ProfileService, ProfileView

from .common import add_common_args, emit, new_session, with_session
from .render import print_story_page, story_payload


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the profile me/view (sub)commands onto the root parser."""
    profile = sub.add_parser("profile", help="own-profile surface (/me)")
    psub = profile.add_subparsers(dest="profile_cmd", required=True)
    me = psub.add_parser("me", help="show the logged-in user's profile")
    add_common_args(me)
    me.set_defaults(fn=cmd_profile_me)

    view = psub.add_parser("view", help="read any profile's timeline feed")
    add_common_args(view)
    view.add_argument("--user-id", required=True,
                      help="target user/page id (numeric) or vanity slug")
    view.add_argument("--limit", type=int, default=10, help="max stories to return (default 10)")
    view.set_defaults(fn=cmd_profile_view)


def cmd_profile_me(args: argparse.Namespace) -> int:
    """Read the logged-in viewer's own profile (/me surface).

    Emits the full raw profile model plus a human summary keyed by the
    decoded identity fields; ``data keys`` lists the raw payload's
    top-level fields for template-harvest debugging.

    Returns:
        0 — identity shape is never treated as a failure condition.
    """
    with with_session(new_session(args)) as session:
        profile = ProfileService(session).me()

        def human() -> None:
            print(f"id       : {profile.user.id}")
            print(f"name     : {profile.user.name}")
            print(f"business : {profile.is_business}")
            print(f"data keys: {sorted(profile.raw)}")

        emit(args, {"profile": profile.model_dump()}, human=human)
        return 0


def cmd_profile_view(args: argparse.Namespace) -> int:
    """Read any profile's timeline feed by numeric id or vanity slug
    (docs/02 §2.5).

    ``--user-id`` takes the numeric user/page id OR the vanity slug —
    ProfileService.view resolves both. ``--limit`` caps the emitted
    stories; the payload carries end_cursor/has_next_page so callers
    can chain pagination.

    Returns:
        0 — an empty timeline is valid account state, not a failure.
    """
    with with_session(new_session(args)) as session:
        view: ProfileView = ProfileService(session).view(args.user_id, limit=args.limit)
        stories: list[Story] = view.feed.stories
        payload = {
            "user": {"id": view.user.id, "name": view.user.name},
            "stories": [story_payload(s) for s in stories],
            "end_cursor": view.feed.end_cursor,
            "has_next_page": view.feed.has_next_page,
            "count": len(stories),
        }

        def human() -> None:
            print(f"user     : {view.user.name or '?'} ({view.user.id})")
            print_story_page(view.feed)

        emit(args, payload, human=human)
        return 0
