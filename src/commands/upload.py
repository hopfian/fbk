"""Upload commands: photo ingest + photo posts (docs/02 §2.6 two-phase
upload family).

`upload photo --path FILE` ingests the image and prints the photo id; with
`--caption` it additionally publishes the photo post through
ComposerStoryCreateMutation (docs/15 §P3 ground truth), wiring the uploaded
photo id into input.attachments. Two-phase discipline per docs/02 §2.6:
phase 1 binary ingest, phase 2 GraphQL attach/publish — the CLI hides the
seam but the phases remain independently observable in the journal.

USER-DOC ANCHOR: cli/docs/05-reference-content.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse
from typing import Any

from domain.common import Privacy
from surfaces.upload import UploadService

from .common import add_common_args, emit, new_session, with_session

_PRIVACY_NAMES = ["public", "friends", "private"]


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the `upload` family onto the root parser (photo; the video
    subcommand is attached by commands/video_upload.py)."""
    upload = sub.add_parser("upload", help="media upload: ingest + post")
    upload_sub = upload.add_subparsers(dest="upload_command", required=True)

    photo_p = upload_sub.add_parser("photo", help="upload an image (optionally post it)")
    photo_p.add_argument("--path", required=True, help="image file path")
    photo_p.add_argument("--caption", default=None,
                         help="when given, publish a photo post with this text")
    photo_p.add_argument("--privacy", default="friends", choices=_PRIVACY_NAMES,
                         help="post audience when --caption is given "
                              "(docs/15 P3 base_state enum; default friends)")
    photo_p.add_argument("--group-id", default=None, help="group id override")
    add_common_args(photo_p)
    photo_p.set_defaults(fn=cmd_photo)


def _story_id(publish: dict[str, Any]) -> str | None:
    """Best-effort story id from the composer mutation response.

    A missing id is not an error — the mutation's data shape varies
    with the composer version, so extraction failures degrade to None
    (printed as '-') rather than failing the command.
    """
    try:
        raw = publish["data"]["story_create"]["story"]["id"]
        return str(raw) if raw is not None else None
    except (KeyError, TypeError):
        return None


def cmd_photo(args: argparse.Namespace) -> int:
    """Upload a photo, optionally publishing it as a post.

    Without ``--caption``: ingest only (phase 1) — prints the photo id
    for later use. With ``--caption``: ingest + publish (phases 1+2) —
    ``--privacy`` selects the post audience, ``--group-id`` retargets
    the publish to a group. Upload ceilings are keyed separately from
    content mutations (docs/10 §1 (uid, content_type) buckets).

    Returns:
        0 — a missing post id in the publish echo is reported in-band
        (post id '-'), not as a failure.
    """
    with with_session(new_session(args)) as session:
        service = UploadService(session)
        if args.caption is not None:
            result = service.post_photo(args.path, args.caption,
                                         Privacy.from_name(args.privacy),
                                         group_id=args.group_id)
            payload: dict[str, Any] = {
                "photo_id": result["photo_id"],
                "post_id": _story_id(result["publish"]),
                "upload": result["upload"],
            }
            emit(args, payload,
                 human=lambda: print(
                     f"photo id: {result['photo_id']}\n"
                     f"post id: {payload['post_id'] or '-'}\n"
                     f"privacy: {args.privacy}"))
        else:
            upload = service.upload_photo(args.path)
            emit(args, upload,
                 human=lambda: print(f"photo id: {upload['photo_id']}"))
        return 0
