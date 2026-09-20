"""Comments commands: read, react, edit, delete on post comments
(docs/02 §2.1 UFI family; docs/04 §6 UFI id semantics; docs/15 live
calibration).

Reads GET the permalink page for the CometSinglePostDialogContentQuery
preload; reactions replay the live-verified captured templates with the
COMMENT's own feedback id (b64 ``ZmVlZGJhY2s6<post>_<cid>`` — a
different handle from the post's feedback id, docs/04 §6); edit/delete
use the decoded / live-proven mutation shapes (surfaces/comments.py).

USER-DOC ANCHOR: cli/docs/04-reference-feed.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse
from typing import Any

from domain.common import Comment, ReactionType
from surfaces.comments import CommentsService

from .common import add_common_args, emit, new_session, with_session

_REACTION_NAMES = [r.name for r in ReactionType]


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the `comments` family onto the root parser: read, react,
    unreact, edit, delete."""
    comments = sub.add_parser("comments",
                              help="deep-comments surface (read, react, edit, delete)")
    csub = comments.add_subparsers(dest="comments_command", required=True)

    read_p = csub.add_parser("read", help="read a post's comments from its permalink")
    read_p.add_argument("--permalink", required=True,
                        help="full post permalink URL")
    read_p.add_argument("--limit", type=int, default=30,
                        help="maximum comments to emit (default 30)")
    add_common_args(read_p)
    read_p.set_defaults(fn=cmd_read)

    react_p = csub.add_parser("react", help="react to a comment's feedback id")
    react_p.add_argument("--feedback-id", required=True,
                         help="the COMMENT's own feedback id (b64 'ZmVlZGJhY2s6<post>_<cid>')")
    react_p.add_argument("--reaction", required=True, choices=_REACTION_NAMES,
                         help="reaction type (docs/15 P3-2 enum)")
    add_common_args(react_p)
    react_p.set_defaults(fn=cmd_react)

    unreact_p = csub.add_parser("unreact", help="remove your reaction on a comment")
    unreact_p.add_argument("--feedback-id", required=True,
                           help="the COMMENT's own feedback id (b64)")
    add_common_args(unreact_p)
    unreact_p.set_defaults(fn=cmd_unreact)

    edit_p = csub.add_parser("edit", help="edit a comment's text")
    edit_p.add_argument("--comment-id", required=True,
                        help="comment id (b64, decoded 'comment:<post>_<cid>', or '<post>_<cid>')")
    edit_p.add_argument("--text", required=True, help="new comment text")
    add_common_args(edit_p)
    edit_p.set_defaults(fn=cmd_edit)

    delete_p = csub.add_parser("delete", help="delete a comment")
    delete_p.add_argument("--comment-id", required=True,
                         help="comment id (b64, decoded 'comment:<post>_<cid>', "
                              "or '<post>_<cid>')")
    delete_p.add_argument("--render-location", default="group",
                          help="renderLocation (default group)")
    add_common_args(delete_p)
    delete_p.set_defaults(fn=cmd_delete)


def _comment_payload(comment: Comment) -> dict[str, Any]:
    """JSON-safe projection of one Comment for emit(): the raw id, its
    decoded ``<post>_<cid>`` pair (the delete/edit key), author, author
    id, and text."""
    return {
        "id": str(comment.id),
        "decoded": "_".join(comment.id.decoded),
        "author": comment.actor.name if comment.actor else None,
        "author_id": (comment.actor.id if comment.actor else None),
        "text": comment.text,
    }


def cmd_read(args: argparse.Namespace) -> int:
    """Read a post's comments from its full permalink URL.

    The permalink is fetched live and its single-post preload replayed
    — the same query the permalink page itself runs.

    Returns:
        0 — zero comments (or a collapsed thread) is valid state.
    """
    with with_session(new_session(args)) as session:
        service = CommentsService(session)
        comments: list[Comment] = service.read(args.permalink, limit=args.limit)

        def human() -> None:
            print(f"comments: {len(comments)}")
            for c in comments:
                print(f"{(c.actor.name if c.actor else '?')} | "
                      f"{(c.text or '').replace(chr(10), ' ')[:60]} | {c.id}")

        emit(args, {"comments": [_comment_payload(c) for c in comments],
                    "count": len(comments)}, human=human)
        return 0


def cmd_react(args: argparse.Namespace) -> int:
    """React to a comment via the COMMENT's own feedback id
    (docs/15 P3-2 reaction enum) — not the post's.

    Returns:
        0 — in-band echo; the docs/10 §3.4 soft-success caveat applies
        (comments are the most soft-filtered mutation class, docs/10
        §2).
    """
    with with_session(new_session(args)) as session:
        service = CommentsService(session)
        response = service.react(args.feedback_id,
                                 ReactionType.from_name(args.reaction))
        emit(args, response)
        return 0


def cmd_unreact(args: argparse.Namespace) -> int:
    """Remove the viewer's reaction from a comment (REMOVE reaction
    value on the same mutation family).

    Returns:
        0 — in-band echo; same soft-success caveat as react.
    """
    with with_session(new_session(args)) as session:
        service = CommentsService(session)
        response = service.unreact(args.feedback_id)
        emit(args, response)
        return 0


def cmd_edit(args: argparse.Namespace) -> int:
    """Edit a comment's text (decoded update-mutation shape,
    surfaces/comments.py).

    Returns:
        0 — in-band echo.
    """
    with with_session(new_session(args)) as session:
        service = CommentsService(session)
        response = service.edit(args.comment_id, args.text)
        emit(args, response)
        return 0


def cmd_delete(args: argparse.Namespace) -> int:
    """Delete a comment (decoded delete-mutation shape,
    surfaces/comments.py). ``--render-location`` defaults to the
    live-verified group context; override for feed-context comments.

    Returns:
        0 — in-band echo.
    """
    with with_session(new_session(args)) as session:
        service = CommentsService(session)
        response = service.delete(args.comment_id,
                                   render_location=args.render_location)
        emit(args, response)
        return 0
