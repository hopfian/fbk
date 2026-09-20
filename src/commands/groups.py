"""Groups commands: create, join, add-members, request-join, feed, post,
members (docs/02 §2.7 groups family).

Each command builds a GroupsService (or, for posting, the shared
FeedService) from the session, calls one operation, and emits a trimmed
payload: only the top-level ``data`` keys with id-ish fields survive the
trim, so huge Relay payloads never hit the terminal. Group posting
replays the live-verified composer locations (GROUP/group) through
FeedService.publish — the same mutation the group UI fires.

USER-DOC ANCHOR: cli/docs/06-reference-people.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse
from typing import Any

from domain.common import Privacy
from surfaces.feed import FeedService
from surfaces.groups import GroupsService

from .common import add_common_args, emit, new_session, with_session
from .render import print_story_page, story_payload

# The group composer locations (live-probed 2026-09): the group-root feed
# preload carries feedLocation "GROUP" / renderLocation "group", and the
# live-verified group comment captures use renderLocation "group" — these
# are the values the groups post command replays through
# FeedService.publish (docs/15 §P3 composer ground truth).
GROUP_POST_FEED_LOCATION = "GROUP"
GROUP_POST_RENDER_LOCATION = "group"


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the `groups` family onto the CLI parser: create, join,
    add-members, request-join, feed, post, members."""
    parser = sub.add_parser("groups", help="groups surface: create, join, "
                                           "invite, request to participate")
    add_common_args(parser)
    group = parser.add_subparsers(dest="groups_command", required=True)

    create = group.add_parser("create", help="create a new group")
    add_common_args(create)
    create.add_argument("--name", required=True, help="group name")
    create.add_argument("--visibility", choices=("public", "private"),
                        default="private",
                         help="group privacy (default private)")
    create.add_argument("--description", default=None,
                        help="group description (not carried by the decoded "
                             "2026-09 create input)")
    create.add_argument("--member-ids", default=None,
                        help="comma-separated user ids to invite")
    create.set_defaults(fn=cmd_create)

    join = group.add_parser("join", help="join a forum-style group")
    add_common_args(join)
    join.add_argument("--group-id", required=True, help="numeric group id")
    join.set_defaults(fn=cmd_join)

    add_members = group.add_parser("add-members",
                                   help="invite members to a group")
    add_common_args(add_members)
    add_members.add_argument("--group-id", required=True,
                            help="numeric group id")
    add_members.add_argument("--member-ids", required=True,
                             help="comma-separated user ids to invite")
    add_members.set_defaults(fn=cmd_add_members)

    request = group.add_parser("request-join",
                               help="request to participate in a gated group")
    add_common_args(request)
    request.add_argument("--group-id", required=True, help="numeric group id")
    request.set_defaults(fn=cmd_request_join)

    feed = group.add_parser("feed", help="read a group's feed")
    add_common_args(feed)
    feed.add_argument("--group-id", required=True, help="numeric group id")
    feed.add_argument("--cursor", default=None,
                      help="end_cursor from the previous page")
    feed.add_argument("--limit", type=int, default=20,
                      help="max stories to return (default 20)")
    feed.set_defaults(fn=cmd_feed)

    post = group.add_parser("post", help="publish a post to a group")
    add_common_args(post)
    post.add_argument("--group-id", required=True, help="numeric group id")
    post.add_argument("--text", required=True, help="post text")
    post.add_argument("--privacy", choices=("public", "friends", "private"),
                      default="public",
                      help="audience base_state (default public)")
    post.set_defaults(fn=cmd_post)

    members = group.add_parser("members", help="read a group's member heads")
    add_common_args(members)
    members.add_argument("--group-id", required=True, help="numeric group id")
    members.add_argument("--limit", type=int, default=20,
                         help="max member rows to return (default 20)")
    members.set_defaults(fn=cmd_members)


def _split_ids(raw: str | None) -> list[str]:
    """Comma-separated id string -> id list (empty when unset)."""
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


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
            for field in ("id", "group_id", "user_id", "page_id"):
                if field in value:
                    return str(value[field])
            for nested in value.values():
                if isinstance(nested, dict) and "id" in nested:
                    return str(nested["id"])
    return "ok"


def cmd_create(args: argparse.Namespace) -> int:
    """Create a group (decoded GroupCreate input, docs/02 §2.7).

    ``--visibility`` maps to the live uppercase enum; ``--member-ids``
    invites in the same mutation. ``--description`` is accepted for
    CLI symmetry but the decoded 2026-09 create input carries no
    description field, so the flag never reaches the wire.

    Returns:
        0 — the created group id prints from the first id-ish data
        field (_first_id).
    """
    with with_session(new_session(args)) as session:
        service = GroupsService(session)
        response = service.create(args.name,
                                  visibility=args.visibility.upper(),
                                  description=args.description,
                                  member_ids=_split_ids(args.member_ids))
        group_id = _first_id(response)
        emit(args, {"response": _trim(response)},
             human=lambda: print(f"group created: {group_id}"))
        return 0


def cmd_join(args: argparse.Namespace) -> int:
    """Join a forum-style group (CometGroup join mutation family,
    docs/02 §2.7).

    Returns:
        0 — in-band echo; gated groups respond with the request flow,
        the payload distinguishes which path fired.
    """
    with with_session(new_session(args)) as session:
        service = GroupsService(session)
        response = service.join(args.group_id)
        emit(args, {"response": _trim(response)},
             human=lambda: print(f"join requested for group {args.group_id}: "
                                 f"{_first_id(response)}"))
        return 0


def cmd_add_members(args: argparse.Namespace) -> int:
    """Invite members to a group (bulk_invitee_members mutation).

    Invitations draw on the per-(uid, target) anti-stalking buckets
    (docs/10 §1) — bulk-inviting fresh accounts is a classic
    coordinated-abuse shape; keep volumes human-scale.

    Returns:
        0 — the emitted payload reports acceptance, not delivery.
    """
    with with_session(new_session(args)) as session:
        service = GroupsService(session)
        response = service.add_members(args.group_id, _split_ids(args.member_ids))
        emit(args, {"response": _trim(response)},
             human=lambda: print(f"invited {len(_split_ids(args.member_ids))} "
                                 f"member(s) to group {args.group_id}"))
        return 0


def cmd_request_join(args: argparse.Namespace) -> int:
    """Request to participate in a gated group (the gated-group answer
    to `join`; docs/02 §2.7).

    Returns:
        0 — in-band echo of the participation request.
    """
    with with_session(new_session(args)) as session:
        service = GroupsService(session)
        response = service.request_to_participate(args.group_id)
        emit(args, {"response": _trim(response)},
             human=lambda: print(f"participation requested for group "
                                 f"{args.group_id}: {_first_id(response)}"))
        return 0


def cmd_feed(args: argparse.Namespace) -> int:
    """Read a group's feed (cursor-paginated, docs/02 §2.7).

    ``--cursor`` chains off a previous page's end_cursor; rendering
    goes through the shared render layer (commands.render).

    Returns:
        0 — an empty group feed is valid state, not a failure.
    """
    with with_session(new_session(args)) as session:
        service = GroupsService(session)
        page = service.feed_read(args.group_id, cursor=args.cursor,
                                limit=args.limit)
        payload = {
            "stories": [story_payload(s) for s in page.stories],
            "end_cursor": page.end_cursor,
            "has_next_page": page.has_next_page,
        }
        emit(args, payload, human=lambda: print_story_page(page))
        return 0


def cmd_post(args: argparse.Namespace) -> int:
    """Publish a post to a group through the shared composer mutation
    (ComposerStoryCreateMutation).

    Returns:
        0 — in-band echo; the mutation counts against the shared
        mutation budget (docs/10 §2).
    """
    with with_session(new_session(args)) as session:
        # groups post delegates to the shared FeedService.publish — the
        # ComposerStoryCreateMutation already carries the group context via
        # groupID + the group feed/render locations (docs/15 §P3).
        service = FeedService(session)
        response = service.publish(args.text, Privacy.from_name(args.privacy),
                                   feed_location=GROUP_POST_FEED_LOCATION,
                                   render_location=GROUP_POST_RENDER_LOCATION,
                                   group_id=args.group_id)
        emit(args, {"response": _trim(response)},
             human=lambda: print(f"posted to group {args.group_id}: "
                                 f"{_first_id(response)}"))
        return 0


def cmd_members(args: argparse.Namespace) -> int:
    """Read a group's member head rows (members read, docs/02 §2.7).

    Human lines mark the viewer (you) and each row's role.

    Returns:
        0 — member rows are a bounded head, not the full roster.
    """
    with with_session(new_session(args)) as session:
        service = GroupsService(session)
        rows = service.members(args.group_id, limit=args.limit)

        def human() -> None:
            print(f"members: {len(rows)}")
            for row in rows:
                role = f" [{row['role']}]" if row.get("role") else ""
                self_mark = " (you)" if row.get("is_self") else ""
                print(f"{row.get('name') or '?'} {row['id']}{role}{self_mark}")

        emit(args, {"members": rows, "count": len(rows)}, human=human)
        return 0
