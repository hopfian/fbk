"""Feed commands: read, paginate, react, comment, publish (docs/02 §2.1
feed surface).

Each command builds a FeedService off a fresh Session and emits through
the shared CLI plumbing (commands/common.py). Wire shapes and semantics
follow docs/04 §5-§7 (pagination, UFI, identifiers) and the
live-calibrated ground truth in docs/15 §P2-3 / §P3-3. Multi-page reads
(``read --pages N``) pace themselves between pages per docs/11 §3
(jittered cursor pagination): the requested gap jittered +-50%, never
fixed-interval — metronomic cursor fetches are a crawler signature
(docs/10 §4).
The publish verb additionally accepts ``--media`` (docs/02 §2.6 upload
family): the media attach dispatches to the upload surfaces' OWN
composer-template publishes (surfaces/upload.py post_photo/post_album,
surfaces/video_upload.py post_video) — FeedService.publish cannot carry
attachments — with --text as the caption and --privacy as the audience.

USER-DOC ANCHOR: cli/docs/04-reference-feed.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from domain.common import Privacy, ReactionType, Story
from governor import GovernorBlockedError
from graphql.errors import FBGraphError
from surfaces.feed import EditablePost, FeedService, feedback_for_post
from surfaces.saved import SavedService
from surfaces.upload import UploadService, _inter_upload_gap
from surfaces.video_upload import VideoUploadService

from .common import add_common_args, emit, new_session, with_session
from .render import print_stories, story_payload

if TYPE_CHECKING:  # pragma: no cover
    from session import Session

_REACTION_NAMES = [r.name for r in ReactionType]
_PRIVACY_NAMES = ["public", "friends", "private"]
# The --media video dispatch set: .mp4/.mov extensions (the rupload chunk
# plane's live-verified container family); anything else falls to the photo
# pipeline, whose ingest rejects non-image MIME types itself.
_VIDEO_SUFFIXES = {".mp4", ".mov"}
# The publish verb's enrichment flags by namespace destination (the
# composer-enrichment seam): media posts ride the upload surfaces'
# composer templates, which carry none of these candidate shapes —
# combining them would silently drop operator intent, so --media
# rejects any enrichment flag instead.
_ENRICHMENT_FLAGS = {"tags": "--tag", "feeling": "--feeling",
                     "activity": "--activity", "place_id": "--place",
                     "ai_label": "--ai-label", "background": "--background"}


def _tag_pair(value: str) -> tuple[str, str]:
    """Parse a ``--tag USER_ID:NAME`` flag value into the service's
    (user_id, display_name) pair.

    Splits on the FIRST colon so display names may contain colons; both
    halves must be non-empty (an empty marker would mispoint the mention
    range the service builds).

    Args:
        value: The raw flag value, e.g. ``"615937:Jane Doe"``.

    Returns:
        The (user_id, display_name) pair.

    Raises:
        argparse.ArgumentTypeError: When the value lacks a colon or
            either half is empty — argparse turns this into a usage
            error (exit 2).
    """
    user_id, sep, name = value.partition(":")
    if not sep or not user_id or not name:
        raise argparse.ArgumentTypeError(
            f"expected USER_ID:NAME (e.g. 615937:Jane), got {value!r}")
    return user_id, name


def _resolve_feedback(args: argparse.Namespace) -> str:
    """The UFI target for react/unreact/comment: the positional feedback
    id OR ``--post-id``, exactly one of the two (docs/04 §6-§7).

    ``--post-id`` is the 3-dot UX's post-centric spelling: the feedback
    handle is derived as b64("feedback:<post_id>") through the domain
    codec (surfaces.feed.feedback_for_post — GROUNDED; live fixture pin
    "ZmVlZGJhY2s6MzkzNzAyNDMyOTc3NDAzNg" == feedback:3937024329774036).
    The positional form accepts every id spelling the service
    normalizes (b64, decoded prefix, bare numeric).

    Args:
        args: The parsed namespace carrying ``feedback_id`` (positional,
            optional) and ``post_id`` (--post-id, optional).

    Returns:
        The feedback target in a form FeedService.react/comment accepts.

    Raises:
        ValueError: When both id forms or neither was given.
    """
    post_id = getattr(args, "post_id", None)
    feedback = getattr(args, "feedback_id", None)
    if post_id is not None and feedback is not None:
        raise ValueError("pass either --post-id or the feedback id, not both")
    if post_id is None and feedback is None:
        raise ValueError("a target is required: pass --post-id ID or a "
                         "feedback id (b64 'ZmVlZGJhY2s6…', "
                         "'feedback:<numeric>', or numeric)")
    if post_id is not None:
        return feedback_for_post(str(post_id)).raw
    return str(feedback)


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the `feed` family onto the root parser: read, paginate,
    react, unreact, comment, delete-comment, publish, life-categories,
    set-privacy, edit, publish-batch, share, save, notify."""
    feed = sub.add_parser("feed", help="feed surface: read, react, comment, publish")
    feed_sub = feed.add_subparsers(dest="feed_command", required=True)

    read_p = feed_sub.add_parser("read", help="fetch one page of the home feed")
    read_p.add_argument("--pages", type=int, default=1, metavar="N",
                        help="walk N pages automatically (read + paginate "
                             "chaining; default 1)")
    read_p.add_argument("--page-gap", type=float, default=2.0, metavar="SECONDS",
                        dest="page_gap",
                        help="seconds between pages in a multi-page walk, "
                             "jittered +-50%% per docs/11 pacing (default 2.0)")
    add_common_args(read_p)
    read_p.set_defaults(fn=cmd_read)

    paginate_p = feed_sub.add_parser(
        "paginate", help="fetch the next feed page by cursor (chained reads)")
    paginate_p.add_argument("--cursor", required=True, metavar="CURSOR",
                           help="end_cursor from a previous read/paginate")
    paginate_p.add_argument("--limit", type=int, default=None, metavar="N",
                           help="emit at most N stories from the page")
    add_common_args(paginate_p)
    paginate_p.set_defaults(fn=cmd_paginate)

    react_p = feed_sub.add_parser("react", help="react to a post's feedback id")
    react_p.add_argument("feedback_id", nargs="?", default=None,
                         help="feedback id (b64 'ZmVlZGJhY2s6…', "
                              "'feedback:<numeric>', or numeric) — "
                              "alternative to --post-id")
    react_p.add_argument("--post-id", dest="post_id", metavar="ID",
                         help="the target post's numeric id; derives the "
                              "feedback id via the b64 codec (exactly one "
                              "of this or the positional feedback id)")
    react_p.add_argument("--reaction", required=True, choices=_REACTION_NAMES,
                         help="reaction type (docs/15 P3-2 enum)")
    react_p.add_argument("--referrer", default="",
                         help="feedback_referrer override (default captured value)")
    add_common_args(react_p)
    react_p.set_defaults(fn=cmd_react)

    unreact_p = feed_sub.add_parser("unreact", help="remove your reaction")
    unreact_p.add_argument("feedback_id", nargs="?", default=None,
                           help="feedback id (b64 'ZmVlZGJhY2s6…', "
                                "'feedback:<numeric>', or numeric) — "
                                "alternative to --post-id")
    unreact_p.add_argument("--post-id", dest="post_id", metavar="ID",
                           help="the target post's numeric id; derives the "
                                "feedback id via the b64 codec (exactly one "
                                "of this or the positional feedback id)")
    add_common_args(unreact_p)
    unreact_p.set_defaults(fn=cmd_unreact)

    comment_p = feed_sub.add_parser("comment", help="comment on a post")
    comment_p.add_argument("feedback_id", nargs="?", default=None,
                           help="feedback id (b64 'ZmVlZGJhY2s6…', "
                                "'feedback:<numeric>', or numeric) — "
                                "alternative to --post-id")
    comment_p.add_argument("--post-id", dest="post_id", metavar="ID",
                           help="the target post's numeric id; derives the "
                                "feedback id via the b64 codec (exactly one "
                                "of this or the positional feedback id)")
    comment_p.add_argument("--text", required=True, help="comment text")
    comment_p.add_argument("--group-id", default=None, help="group id override")
    add_common_args(comment_p)
    comment_p.set_defaults(fn=cmd_comment)

    delete_p = feed_sub.add_parser("delete-comment", help="delete a comment")
    delete_p.add_argument("comment_id",
                          help="comment id (b64, decoded 'comment:<post>_<cid>', "
                               "or '<post>_<cid>')")
    delete_p.add_argument("--group-id", default=None, help="group id override")
    add_common_args(delete_p)
    delete_p.set_defaults(fn=cmd_delete_comment)

    publish_p = feed_sub.add_parser("publish", help="publish a post to the feed")
    publish_p.add_argument("--text", required=True, help="post text")
    publish_p.add_argument("--privacy", required=True, choices=_PRIVACY_NAMES,
                           help="audience (docs/15 P3 base_state enum)")
    publish_p.add_argument("--tag", action="append", type=_tag_pair,
                           default=None, dest="tags", metavar="USER_ID:NAME",
                           help="tag a friend (repeatable): appends an "
                                "@NAME mention to the text — candidate "
                                "shape, unverified against live")
    publish_p.add_argument("--feeling", default=None, metavar="ID",
                           help="feeling target id — candidate shape, "
                                "unverified against live")
    publish_p.add_argument("--activity", default=None, metavar="ID",
                           help="activity target id — candidate shape, "
                                "unverified against live (mutually "
                                "exclusive with --feeling)")
    publish_p.add_argument("--place", default=None, dest="place_id",
                           metavar="ID",
                           help="check-in place id — candidate shape, "
                                "unverified against live")
    publish_p.add_argument("--ai-label", choices=["on", "off"], default=None,
                           dest="ai_label",
                           help="self-disclose the post as AI-generated "
                                "(maps to the captured disclosure bool)")
    publish_p.add_argument("--background", default=None, metavar="PRESET_ID",
                           help="background-color preset id from FB's gated "
                                "swatch picker (\"0\" clears the background; "
                                "the valid-id census is a parked recon item)")
    # --media (docs/02 §2.6 upload family) — the publish verb's own additive
    # flag block, composed with --text (the caption) and --privacy above and
    # with the enrichment flags on this same parser (mutually exclusive:
    # the upload surfaces' composer publishes carry no enrichment shapes).
    publish_p.add_argument(
        "--media", action="append", default=None, dest="media",
        metavar="PATH[,PATH...]",
        help="attach media instead of a plain text post: one image (photo "
             "post), a comma-separated or repeated image list (album), or "
             "a single .mp4/.mov (video post); mixed image+video lists "
             "and media+enrichment combinations are rejected")
    add_common_args(publish_p)
    publish_p.set_defaults(fn=cmd_publish)

    life_p = feed_sub.add_parser(
        "life-categories",
        help="list life-event categories (live-calibrated query)")
    add_common_args(life_p)
    life_p.set_defaults(fn=cmd_life_categories)

    set_privacy_p = feed_sub.add_parser(
        "set-privacy", help="change an existing post's audience")
    set_privacy_p.add_argument("--post-id", required=True, dest="post_id",
                               metavar="ID", help="the target post's numeric id "
                               "(story_create.feed_story_edge.node.post_id)")
    set_privacy_p.add_argument("--privacy", required=True, choices=_PRIVACY_NAMES,
                               help="the new audience (base_state enum)")
    add_common_args(set_privacy_p)
    set_privacy_p.set_defaults(fn=cmd_set_privacy)

    # The edit verb — full composer parity with publish (every publish
    # flag that makes sense for a delta), plus --photo (upload+attach via
    # the existing upload surface) and --show (the safe preview). The
    # underlying variable shapes are CANDIDATES pending the live probe;
    # the surfaces module carries the probe-correctable constants.
    edit_p = feed_sub.add_parser(
        "edit", help="edit an existing post (full composer parity)")
    edit_p.add_argument("--post-id", required=True, dest="post_id",
                        metavar="ID",
                        help="the target post's numeric id "
                             "(story_create.feed_story_edge.node.post_id)")
    edit_p.add_argument("--text", default=None,
                        help="replacement post text (omit to keep the "
                             "current text)")
    edit_p.add_argument("--privacy", default=None, choices=_PRIVACY_NAMES,
                        help="new audience (omit to keep the current one)")
    edit_p.add_argument("--ai-label", choices=["on", "off"], default=None,
                       dest="ai_label",
                       help="self-disclose the post as AI-generated (maps to "
                            "the captured disclosure bool)")
    edit_p.add_argument("--background", default=None, metavar="PRESET_ID",
                        help="background-color preset id (\"0\" clears the "
                             "background; valid-id census is a parked recon "
                             "item)")
    edit_p.add_argument("--tag", action="append", type=_tag_pair,
                        default=None, dest="tags", metavar="USER_ID:NAME",
                        help="tag a friend (repeatable): appends an @NAME "
                             "mention to --text — candidate shape, "
                             "unverified against live; requires --text")
    edit_p.add_argument("--feeling", default=None, metavar="ID",
                        help="feeling target id — candidate shape, "
                             "unverified against live")
    edit_p.add_argument("--activity", default=None, metavar="ID",
                        help="activity target id — candidate shape, "
                             "unverified against live (mutually exclusive "
                             "with --feeling)")
    edit_p.add_argument("--place", default=None, dest="place_id",
                        metavar="ID",
                        help="check-in place id — candidate shape, "
                             "unverified against live")
    edit_p.add_argument("--photo", action="append", default=None,
                        dest="photos", metavar="PATH",
                        help="upload one image via the existing photo "
                             "ingest and attach it to the post "
                             "(repeatable; uploads are paced between like "
                             "an album — docs/15 P9-1)")
    edit_p.add_argument("--show", action="store_true",
                        help="fetch and print the editable state WITHOUT "
                             "mutating (the safe preview)")
    add_common_args(edit_p)
    edit_p.set_defaults(fn=cmd_edit)

    batch_p = feed_sub.add_parser(
        "publish-batch",
        help="publish several posts in ONE run, sequentially under the "
             "governor")
    batch_p.add_argument("--text", required=True, action="append",
                         metavar="TEXT",
                         help="one post's text; repeat the flag per post "
                              "(min 2: --text A --text B)")
    batch_p.add_argument("--privacy", default="friends",
                         choices=_PRIVACY_NAMES,
                         help="audience for every post in the batch "
                              "(default friends)")
    batch_p.add_argument("--batch-gap", type=float, default=0.0,
                         dest="batch_gap", metavar="SECONDS",
                         help="EXTRA jittered gap between posts on top of "
                              "the governor's own pacing (default 0: the "
                              "governor paces; docs/15 P8-1)")
    add_common_args(batch_p)
    batch_p.set_defaults(fn=cmd_publish_batch)

    # -- the 3-dot post-centric action verbs (share / save / notify) ------
    share_p = feed_sub.add_parser(
        "share", help="share a post to your own timeline (candidate "
                      "attachment shape — probe pending)")
    share_p.add_argument("--post-id", required=True, dest="post_id",
                        metavar="ID", help="the shared post's numeric id")
    share_p.add_argument("--text", default="",
                         help="optional share comment (default: none)")
    share_p.add_argument("--privacy", default="friends",
                         choices=_PRIVACY_NAMES,
                         help="audience for the shared post (default "
                              "friends)")
    add_common_args(share_p)
    share_p.set_defaults(fn=cmd_share)

    save_p = feed_sub.add_parser(
        "save", help="save a post (delegates to the saved surface)")
    save_p.add_argument("--post-id", required=True, dest="post_id",
                        metavar="ID",
                        help="the savable node id: for a plain post this "
                             "IS the post id; for photo/video posts use the "
                             "media fbid (the story's save_info.savable.id)")
    add_common_args(save_p)
    save_p.set_defaults(fn=cmd_save)

    notify_p = feed_sub.add_parser(
        "notify", help="turn post notifications from an actor on/off "
                       "(candidate variables — probe pending)")
    notify_p.add_argument("--actor-id", required=True, dest="actor_id",
                          metavar="ID", help="the actor (user/page) id")
    notify_p.add_argument("state", choices=["on", "off"],
                          help="'on' subscribes to the actor's posts, "
                               "'off' unsubscribes")
    add_common_args(notify_p)
    notify_p.set_defaults(fn=cmd_notify)


def _sleep_page_gap(gap_s: float) -> None:
    """docs/11 §3 jittered cursor pagination: the requested gap jittered
    multiplicatively +-50% so successive page fetches never land at a
    fixed interval (docs/10 §4 pagination-regularity detection)."""
    time.sleep(gap_s * random.uniform(0.5, 1.5))


def cmd_read(args: argparse.Namespace) -> int:
    """Read the home feed, optionally walking N pages by cursor chaining.

    Page 1 is the plain read; each further page replays the previous
    end_cursor through paginate() after a jittered page gap. The walk
    stops early when the stream runs dry (no cursor or has_next_page
    false); stories aggregate across pages.

    Returns:
        0 — an empty or thin feed is a valid observation (and, on a
        throttled session, itself a diagnostic — degraded payloads,
        docs/10 §3.5), not a failure.
    """
    with with_session(new_session(args)) as session:
        service = FeedService(session)

        # Page 1 is always the plain read; further pages chain paginate() off
        # the previous end_cursor, stopping early when the stream runs dry
        # (no cursor / has_next_page false). Stories aggregate across pages.
        page = service.read()
        stories: list[Story] = list(page.stories)
        pages_read = 1
        while pages_read < args.pages and page.end_cursor and page.has_next_page:
            _sleep_page_gap(args.page_gap)
            page = service.paginate(page.end_cursor)
            stories.extend(page.stories)
            pages_read += 1

        payload = {
            "stories": [story_payload(s) for s in stories],
            "end_cursor": page.end_cursor,
            "has_next_page": page.has_next_page,
            "pages": pages_read,
        }
        emit(args, payload, human=lambda: print_stories(stories, page))
        return 0


def cmd_paginate(args: argparse.Namespace) -> int:
    """Fetch the next feed page by explicit cursor.

    The cursor is opaque server state (end_cursor of a previous
    read/paginate); ``--limit`` slices the emitted stories without
    affecting the payload's own end_cursor chaining value.

    Returns:
        0 — an exhausted cursor (empty page) is a valid result.
    """
    with with_session(new_session(args)) as session:
        service = FeedService(session)
        page = service.paginate(args.cursor)
        stories = page.stories if args.limit is None else page.stories[:args.limit]
        payload = {
            "page": {
                "stories": [story_payload(s) for s in stories],
                "end_cursor": page.end_cursor,
                "has_next_page": page.has_next_page,
            },
            "next_cursor": page.end_cursor,
        }
        emit(args, payload, human=lambda: print_stories(stories, page))
        return 0


def cmd_react(args: argparse.Namespace) -> int:
    """Apply one reaction to a post via its feedback id (docs/02 §2.11
    reaction family; feedback_id is the b64 ``ZmVlZGJhY2s6...`` handle,
    docs/02 §9.1). ``--referrer`` overrides the captured
    feedback_referrer when replaying a template captured in a different
    surface context.

    Returns:
        0 — the mutation's in-band echo. Soft-success filtering
        (docs/10 §3.4) means a success echo is NOT proof the reaction
        landed; out-of-band verification stays the operator's concern.
    """
    with with_session(new_session(args)) as session:
        service = FeedService(session)
        response = service.react(_resolve_feedback(args),
                                  ReactionType.from_name(args.reaction),
                                  feedback_referrer=args.referrer)
        emit(args, response)
        return 0


def cmd_unreact(args: argparse.Namespace) -> int:
    """Remove the viewer's reaction from a post (the same reaction
    mutation family with the REMOVE value, docs/15 §P3-2).

    Returns:
        0 — in-band echo only; the docs/10 §3.4 soft-success caveat
        applies exactly as for react.
    """
    with with_session(new_session(args)) as session:
        service = FeedService(session)
        response = service.unreact(_resolve_feedback(args))
        emit(args, response)
        return 0


def cmd_comment(args: argparse.Namespace) -> int:
    """Create a comment on a post via its feedback id (Comet comment
    create family, docs/02 §2.1). ``--group-id`` overrides the captured
    group context when the target post lives in a group.

    Returns:
        0 — in-band echo; comments are the most soft-filtered mutation
        class (docs/10 §2), so verify out-of-band before counting it.
    """
    with with_session(new_session(args)) as session:
        service = FeedService(session)
        response = service.comment(_resolve_feedback(args), args.text,
                                    group_id=args.group_id)
        emit(args, response)
        return 0


def cmd_delete_comment(args: argparse.Namespace) -> int:
    """Delete a comment (decoded and live-proven mutation shape,
    surfaces/comments.py). The comment id accepts b64, the decoded
    ``comment:<post>_<cid>`` form, or the bare ``<post>_<cid>`` pair —
    the story_fbid + id legacy key family (docs/02 §3.1).
    ``--group-id`` overrides the captured group context.

    Returns:
        0 — in-band echo of the deletion mutation.
    """
    with with_session(new_session(args)) as session:
        service = FeedService(session)
        response = service.delete_comment(args.comment_id, group_id=args.group_id)
        emit(args, response)
        return 0


def _media_paths(values: list[str] | None) -> list[Path]:
    """--media flag values -> one flat path list, in operator order.

    Each --media occurrence takes a comma-separated list and the flag
    itself is repeatable, so ``--media a.png,b.png --media c.png`` is
    three paths. Empty segments are dropped so a trailing comma does
    not invent a phantom file.

    Args:
        values: The raw ``args.media`` list (None when the flag is
            absent).

    Returns:
        The flattened, de-blanked path list; ``[]`` when no media was
        given.
    """
    paths: list[Path] = []
    for value in values or []:
        for part in value.split(","):
            if part.strip():
                paths.append(Path(part.strip()))
    return paths


def _story_id(publish: dict[str, Any]) -> str | None:
    """Best-effort story id from the composer mutation echo — same
    degradation contract as the upload command family (a missing id
    prints '-' rather than failing the command, since the composer
    data shape varies with the deploy)."""
    try:
        raw = publish["data"]["story_create"]["story"]["id"]
        return str(raw) if raw is not None else None
    except (KeyError, TypeError):
        return None


def _publish_media(session: Session, media: list[Path],
                   args: argparse.Namespace) -> int:
    """Dispatch one --media list to the upload surfaces and emit.

    The attach rides the upload surfaces' OWN composer-template
    publishes (surfaces/upload.py post_photo / post_album,
    surfaces/video_upload.py post_video) — never FeedService.publish,
    which cannot carry attachments. Classification is by extension:
    .mp4/.mov are videos, everything else falls to the photo pipeline
    (whose ingest rejects non-image MIME types itself). Mixed
    image+video and multi-video lists are uncalibrated attach flows —
    parked per the no-invention rule (docs/15 §P9), rejected cleanly
    before anything is sent.
    """
    videos = [p for p in media if p.suffix.lower() in _VIDEO_SUFFIXES]
    images = [p for p in media if p.suffix.lower() not in _VIDEO_SUFFIXES]
    if videos and images:
        print("error: mixed image+video --media lists are not a calibrated "
              "flow (docs/15 §P9) — attach images OR one video, not both",
              file=sys.stderr)
        return 1
    if len(videos) > 1:
        print("error: --media accepts at most one video (multi-video attach "
              "is not a calibrated flow, docs/15 §P9)", file=sys.stderr)
        return 1
    privacy = Privacy.from_name(args.privacy)
    group_id = getattr(args, "group_id", None)
    if videos:
        result = VideoUploadService(session).post_video(
            videos[0], args.text, privacy, group_id=group_id)
        payload: dict[str, Any] = {
            "media": "video",
            "video_id": result["video_id"],
            "post_id": _story_id(result["publish"]),
            "upload": result["upload"],
        }
        emit(args, payload, human=lambda: print(
            f"video id: {result['video_id']}\n"
            f"post id: {payload['post_id'] or '-'}\n"
            f"privacy: {args.privacy}"))
        return 0
    upload_service = UploadService(session)
    if len(images) == 1:
        result = upload_service.post_photo(
            images[0], args.text, privacy, group_id=group_id)
        payload = {
            "media": "photo",
            "photo_id": result["photo_id"],
            "post_id": _story_id(result["publish"]),
            "upload": result["upload"],
        }
        emit(args, payload, human=lambda: print(
            f"photo id: {result['photo_id']}\n"
            f"post id: {payload['post_id'] or '-'}\n"
            f"privacy: {args.privacy}"))
        return 0
    # widen the album list to the upload surface's accepted element union
    # (list invariance: list[Path] is not a list[Path | str] for mypy)
    album: list[Path | str] = list(images)
    result = upload_service.post_album(
        album, args.text, privacy, group_id=group_id)
    payload = {
        "media": "album",
        "photo_ids": result["photo_ids"],
        "post_id": _story_id(result["publish"]),
        "uploads": result["uploads"],
    }
    emit(args, payload, human=lambda: print(
        f"photo ids: {', '.join(result['photo_ids'])}\n"
        f"post id: {payload['post_id'] or '-'}\n"
        f"privacy: {args.privacy}"))
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    """Publish a text post to the viewer's feed
    (ComposerStoryCreateMutation, docs/15 §P3). ``--privacy`` maps to
    the live-calibrated base_state audience enum. The enrichment flags
    (``--tag``/``--feeling``/``--activity``/``--place``) ride CANDIDATE
    input shapes not yet verified against live; the 1675012 coercion
    gate rejects a wrong candidate cleanly (typed error, no post
    created), so a bad flag value fails safe.

    ``--media`` switches the publish to the two-phase upload family
    (docs/02 §2.6): the attach goes through the upload surfaces' own
    composer templates with --text as the caption and --privacy as the
    audience. The enrichment flags are text-post-only: their candidate
    shapes are not carried by the upload surfaces' publishes, so a
    media+enrichment combination is rejected up front (nothing sent).

    Returns:
        0 — in-band echo; the mutation counts against the shared
        mutation budget (docs/10 §2), which the governor enforces.
        1 — a rejected --media list (mixed image+video, multiple
        videos, or media combined with an enrichment flag): a
        precondition failure reported on stderr; nothing was sent.
    """
    media = _media_paths(getattr(args, "media", None))
    if media:
        enrichment = sorted(
            flag for dest, flag in _ENRICHMENT_FLAGS.items()
            if getattr(args, dest, None) is not None)
        if enrichment:
            print(f"error: --media cannot combine with the enrichment "
                  f"flags ({', '.join('--' + d for d in enrichment)}) — "
                  f"the upload surfaces' composer publishes carry none "
                  f"of those candidate shapes; nothing was sent",
                  file=sys.stderr)
            return 1
        with with_session(new_session(args)) as session:
            return _publish_media(session, media, args)
    with with_session(new_session(args)) as session:
        service = FeedService(session)
        response = service.publish(
            args.text, Privacy.from_name(args.privacy),
            tags=args.tags,
            feeling=args.feeling,
            activity=args.activity,
            place_id=args.place_id,
            ai_generated=None if args.ai_label is None
            else args.ai_label == "on",
            text_format_preset_id=args.background)
        emit(args, response)
        return 0


def _print_life_categories(categories: list[dict[str, Any]]) -> None:
    """Human renderer for life-categories: one category line, one bullet
    per event type (the type's name when present, else its identifier)."""
    for category in categories:
        label = category.get("name") or category.get("id")
        print(f"{label} ({category.get('id')})")
        for event_type in category.get("types") or []:
            type_label = event_type.get("name") or event_type.get("identifier")
            print(f"  - {type_label} ({event_type.get('id')})")


def cmd_life_categories(args: argparse.Namespace) -> int:
    """List the composer's life-event categories with their event types
    (CometComposerLifeEventCategoryListQuery — the live-calibrated
    2026-09-20 query: doc_id 32116841221248541, variables {"scale": 1}).
    The publish-side input for life events is UNKNOWN and intentionally
    not invented — this command lists only.

    Returns:
        0 — an empty category list is a valid observation.
    """
    with with_session(new_session(args)) as session:
        service = FeedService(session)
        categories = service.life_event_categories()
        payload = {"categories": categories}
        emit(args, payload,
             human=lambda: _print_life_categories(categories))
        return 0


def cmd_set_privacy(args: argparse.Namespace) -> int:
    """Change one existing post's audience
    (CometPrivacySelectorSavePrivacyMutation re-targeted at the story's
    own privacy_scope_renderer id - live-verified 2026-09-20).

    Returns:
        0 - in-band echo; the mutation counts against the shared
        mutation budget (docs/10 2), which the governor enforces.
    """
    with with_session(new_session(args)) as session:
        service = FeedService(session)
        response = service.set_privacy(args.post_id,
                                       Privacy.from_name(args.privacy))
        emit(args, response)
        return 0


def _edit_flags_given(args: argparse.Namespace) -> list[str]:
    """The edit flags the operator actually passed, in flag order.

    Drives the edit verb's two non-mutating contracts: the natural
    default (no edit flags -> show the current editable state, exit 0)
    and the --show guard (--show with edit flags is a contradictory
    invocation, rejected before anything is sent).

    Args:
        args: The parsed edit-verb namespace.

    Returns:
        The flag spellings of every edit flag present (e.g.
        ``["--text", "--tag"]``); ``[]`` when none were given.
    """
    given: list[str] = []
    if args.text is not None:
        given.append("--text")
    if args.privacy is not None:
        given.append("--privacy")
    if args.ai_label is not None:
        given.append("--ai-label")
    if args.background is not None:
        given.append("--background")
    if args.tags:
        given.append("--tag")
    if args.feeling is not None:
        given.append("--feeling")
    if args.activity is not None:
        given.append("--activity")
    if args.place_id is not None:
        given.append("--place")
    if args.photos:
        given.append("--photo")
    return given


def _print_editable(post_id: str, state: EditablePost) -> None:
    """Human renderer for the editable-state preview: one line per field,
    '-' where the dialog carried nothing for it (absence is a valid
    observation, never an error)."""
    print(f"post: {post_id}")
    print(f"text: {state.text if state.text is not None else '-'}")
    print(f"privacy: {state.privacy_base_state or '-'}")
    ids = ", ".join(
        f"{next(iter(element))}:{next(iter(element.values()))['id']}"
        for element in state.attachments)
    print(f"attachments: {len(state.attachments)}"
          + (f" ({ids})" if ids else ""))
    print(f"feeling: {state.feeling or '-'}")
    print(f"activity: {state.activity or '-'}")
    print(f"place: {state.place_id or '-'}")


def _sleep_upload_gap() -> None:
    """One governor-shaped gap between the edit verb's raw photo ingests.

    The raw ingest plane is NOT governor-gated (docs/15 §P9-1), so N
    back-to-back --photo uploads would be a burst — the one live-observed
    kill trigger (docs/15 §P8-1). The gap is the upload surface's OWN
    album pacing reused verbatim (surfaces.upload._inter_upload_gap:
    lognormal, floor 4s, mean 12s, cv 0.7 — GovernorConfig-shaped), never
    a duplicated formula. Module-level so tests patch it out.
    """
    time.sleep(_inter_upload_gap(random.Random()))


def cmd_edit(args: argparse.Namespace) -> int:
    """Edit one existing post — full composer parity with publish
    (ComposerStoryEditMutation, candidate shapes probe-pending;
    surfaces.feed carries the probe-correctable constants).

    The safe preview contract: ``--show`` fetches and prints the
    editable state WITHOUT mutating; when NO edit flag was given at all,
    that preview is the natural default (show current state, exit 0).
    Any edit flag switches the verb to a mutating delta: only the passed
    flags change the post, everything else rides untouched. ``--photo``
    uploads first through the EXISTING upload surface (paced between
    like an album) and passes the resulting ids as edit attachments.

    Returns:
        0 — the in-band echo of the dialog query (preview) or the edit
        mutation (the write counts against the shared mutation budget,
        which the governor enforces).
        1 — ``--show`` combined with edit flags (contradictory: nothing
        was sent).
    """
    edit_flags = _edit_flags_given(args)
    if args.show and edit_flags:
        print(f"error: --show cannot combine with the edit flags "
              f"({', '.join(edit_flags)}) — --show only previews, it "
              f"never mutates; nothing was sent", file=sys.stderr)
        return 1
    with with_session(new_session(args)) as session:
        service = FeedService(session)
        if args.show or not edit_flags:
            state = service.fetch_editable(args.post_id)
            payload = {"post_id": args.post_id,
                       "editable": state.model_dump()}
            emit(args, payload,
                 human=lambda: _print_editable(args.post_id, state))
            return 0
        photo_ids: list[str] = []
        if args.photos:
            upload_service = UploadService(session)
            for index, path in enumerate(args.photos):
                if index:
                    _sleep_upload_gap()
                photo_ids.append(
                    str(upload_service.upload_photo(path)["photo_id"]))
        response = service.edit(
            args.post_id,
            text=args.text,
            privacy=None if args.privacy is None
            else Privacy.from_name(args.privacy),
            ai_generated=None if args.ai_label is None
            else args.ai_label == "on",
            text_format_preset_id=args.background,
            tags=args.tags,
            feeling=args.feeling,
            activity=args.activity,
            place_id=args.place_id,
            attachments=photo_ids or None)
        emit(args, response)
        return 0


def _batch_head(text: str, limit: int = 50) -> str:
    """A one-line summary head of one batch post's text."""
    first = text.splitlines()[0] if text.splitlines() else text
    return first if len(first) <= limit else first[: limit - 1] + "…"


def _batch_post_id(response: dict[str, Any]) -> str | None:
    """The created story's post_id from a publish echo, when present.

    Tolerant of a missing/oddly-shaped echo: the documented location is
    ``data.story_create.feed_story_edge.node.post_id`` (the set-privacy
    contract), but the batch report must never fail on an echo variant —
    a published post with no extractable id is still a published post.
    """
    node = (response.get("data", {}).get("story_create", {})
            .get("feed_story_edge", {}).get("node", {}))
    post_id = node.get("post_id") if isinstance(node, dict) else None
    return str(post_id) if post_id else None


def cmd_publish_batch(args: argparse.Namespace) -> int:
    """Publish several posts in ONE invocation, strictly SEQUENTIALLY.

    "Parallel posts" means multiple posts submitted together — never
    concurrent network calls: true parallelism is a metronomic burst,
    the exact Phase-8 kill signature (docs/15 §P8-1), and is never
    attempted. The governor serializes by design — each text is ONE
    FeedService.publish call in order inside ONE session, and the
    transport's governor gate (FBTransport._govern → before_request,
    src/transport/session.py) inserts the lognormal inter-arrival gaps
    NATURALLY between the mutations; NO explicit sleeps are needed.
    ``--batch-gap SECONDS`` adds an operator-chosen EXTRA jittered gap
    between posts (±50%, the docs/11 §3 pagination jitter) on top of
    the governor's pacing — default 0 relies on the governor alone.

    Per-post fail-soft: a typed FBGraphError on post k is recorded
    ({text head, error, status}) and the batch CONTINUES — the operator
    gets the full report. GovernorBlockedError is FATAL: the cap is the
    cap (docs/11 §5 containment) — the batch aborts immediately, the
    un-attempted remainder is marked skipped, and what got out is
    reported.

    Returns:
        0 when every text published; 1 when fewer than 2 --text posts
        were given, any post failed, or the governor aborted the batch.
    """
    texts: list[str] = list(args.text or [])
    if len(texts) < 2:
        print("error: publish-batch needs at least 2 --text posts "
              "(repeat the flag: --text A --text B)", file=sys.stderr)
        return 1
    privacy = Privacy.from_name(args.privacy)
    results: list[dict[str, Any]] = []
    aborted: GovernorBlockedError | None = None
    with with_session(new_session(args)) as session:
        service = FeedService(session)
        for i, text in enumerate(texts):
            head = _batch_head(text)
            if aborted is not None:
                results.append({"index": i, "text_head": head,
                               "status": "skipped", "post_id": None,
                               "error": None})
                continue
            if i > 0 and args.batch_gap > 0:
                _sleep_page_gap(args.batch_gap)
            try:
                response = service.publish(text, privacy)
            except GovernorBlockedError as exc:
                aborted = exc
                results.append({"index": i, "text_head": head,
                                "status": "governor_blocked", "post_id": None,
                                "error": str(exc)})
            except FBGraphError as exc:
                results.append({"index": i, "text_head": head,
                                "status": "failed", "post_id": None,
                                "error": str(exc),
                                "error_type": type(exc).__name__})
            else:
                results.append({"index": i, "text_head": head,
                                "status": "published",
                                "post_id": _batch_post_id(response),
                                "error": None})
    published = sum(1 for r in results if r["status"] == "published")
    failed = sum(1 for r in results if r["status"] == "failed")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    payload: dict[str, Any] = {
        "total": len(texts),
        "published": published,
        "failed": failed,
        "skipped": skipped,
        "aborted": aborted is not None,
        "governor_error": str(aborted) if aborted is not None else None,
        "results": results,
    }

    def human() -> None:
        for r in results:
            line = f"[{r['index'] + 1}/{len(texts)}] {r['status']}: {r['text_head']}"
            if r["status"] == "published":
                line += f" (post_id {r['post_id']})"
            elif r["error"]:
                line += f" — {r['error']}"
            print(line)
        print(f"summary: {published} published, {failed} failed, "
              f"{skipped} skipped" + (" (governor aborted the batch)"
                                       if aborted is not None else ""))

    emit(args, payload, human=human)
    return 0 if published == len(texts) and aborted is None else 1


def cmd_share(args: argparse.Namespace) -> int:
    """Share one post to the viewer's own timeline through the composer
    (ComposerStoryCreateMutation carrying the SHARE_ATTACHMENT_CANDIDATE
    element — CANDIDATE SHAPE - UNVERIFIED, probe pending: the dialog
    probes ShareToFeedComposerCometDialogQuery /
    CometUnifiedShareSheetDialogQuery must confirm the share field; a
    wrong candidate fails safe through the 1675012 coercion gate —
    typed error, no post created).

    Returns:
        0 — the mutation's in-band echo (docs/10 §3.4 soft-success
        caveat applies exactly as for publish).
    """
    with with_session(new_session(args)) as session:
        service = FeedService(session)
        response = service.share(args.post_id, args.text,
                                 Privacy.from_name(args.privacy))
        emit(args, response)
        return 0


def _viewer_saved_state(response: dict[str, Any]) -> str:
    """viewer_saved_state from a save mutation echo — the single
    authoritative field confirming the server-side bookmark state
    ("?" when the node shape is absent); same contract as the saved
    command family (commands/saved.py)."""
    node = response.get("data", {}).get("node_saved_state", {})
    save_node = node.get("save_node") if isinstance(node, dict) else None
    if isinstance(save_node, dict):
        return str(save_node.get("viewer_saved_state") or "?")
    return "?"


def cmd_save(args: argparse.Namespace) -> int:
    """Save a post — the 3-dot convenience delegating to the saved
    surface's live-verified CometSaveMutation (surfaces/saved.py).

    ``--post-id`` is the SAVABLE node id the save plane consumes: for a
    plain post this IS the post id; for photo/video posts the media
    fbid (the story's ``save_info.savable.id``, docs/15 §P4-1) — the
    convenience layer passes it through unchanged (the id-form finding:
    save() takes the savable node id, never the story key).

    Returns:
        0 — the emitted viewer_saved_state confirms the server-side
        outcome.
    """
    with with_session(new_session(args)) as session:
        response = SavedService(session).save(args.post_id)
        state = _viewer_saved_state(response)
        emit(args, {"post_id": args.post_id, "viewer_saved_state": state},
             human=lambda: print(f"saved {args.post_id} -> {state}"))
        return 0


def cmd_notify(args: argparse.Namespace) -> int:
    """Turn post notifications from one actor on or off
    (CommitActorSubscribeStatusSubscribe/UnsubscribeMutation —
    registry-grounded v3 "carried" doc_ids; variables CANDIDATE -
    UNVERIFIED, probe pending).

    Returns:
        0 — the mutation's in-band echo (an ack envelope; verify
        out-of-band per docs/10 §3.4).
    """
    with with_session(new_session(args)) as session:
        service = FeedService(session)
        response = service.notify(args.actor_id, args.state)
        emit(args, {"actor_id": args.actor_id, "state": args.state,
                    "response": response},
             human=lambda: print(
                 f"notifications {args.state} for actor {args.actor_id}"))
        return 0
