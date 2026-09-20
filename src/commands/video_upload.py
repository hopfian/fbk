"""Upload video command: rupload ingest + video posts (docs/02 §2.6
upload family; docs/15 §P9 video-upload ground truth).

`upload video --path FILE` ingests the video through the decoded three-stage
rupload plane (start -> chunk POST -> receive) and prints the video id; with
`--caption` it additionally publishes the video post through
ComposerStoryCreateMutation, wiring the uploaded video id into the decoded
VIDEO attachments element (surfaces/video_upload.py docstring). Attaches to
the existing `upload` family parser so photo and video share one `--help`
group.

USER-DOC ANCHOR: cli/docs/05-reference-content.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse
from typing import Any, cast

from domain.common import Privacy
from surfaces.video_upload import VideoUploadService

from .common import add_common_args, emit, new_session, with_session

_PRIVACY_NAMES = ["public", "friends", "private"]


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire `upload video` onto the `upload` family parser.

    Reaches into the live `upload` subparser tree to attach the video
    leaf next to the photo leaf — argparse offers no public API for
    augmenting an already-registered family, so the module joins via
    the _SubParsersAction seam (stable since 3.x, same seam
    app._disallow_abbrev relies on).
    """
    upload_parser = sub.choices["upload"]
    # typeshed types ArgumentParser._subparsers as OPTIONAL; the group
    # exists by construction here — commands.upload.register (which adds
    # the upload subparsers) runs before this module in app._COMMAND_MODULES
    # — so pin it for the _group_actions walk below.
    upload_group = cast("argparse._ArgumentGroup", upload_parser._subparsers)
    upload_sub = next(
        action for action in upload_group._group_actions
        if isinstance(action, argparse._SubParsersAction))
    video_p = upload_sub.add_parser(
        "video", help="upload a video (optionally post it)")
    video_p.add_argument("--path", required=True, help="video file path")
    video_p.add_argument("--caption", default=None,
                         help="when given, publish a video post with this text")
    video_p.add_argument("--privacy", default="friends", choices=_PRIVACY_NAMES,
                          help="post audience when --caption is given "
                               "(default friends)")
    video_p.add_argument("--group-id", default=None, help="group id override")
    add_common_args(video_p)
    video_p.set_defaults(fn=cmd_video)


def _story_id(publish: dict[str, Any]) -> str | None:
    """Best-effort story id from the composer mutation response — a
    missing id degrades to None (printed as '-') rather than failing,
    since the composer data shape varies with the deploy."""
    try:
        raw = publish["data"]["story_create"]["story"]["id"]
        return str(raw) if raw is not None else None
    except (KeyError, TypeError):
        return None


def cmd_video(args: argparse.Namespace) -> int:
    """Upload a video, optionally publishing it as a post.

    Without ``--caption``: three-stage rupload ingest only — prints the
    video id. With ``--caption``: ingest + publish through the composer
    mutation — ``--privacy`` selects the post audience, ``--group-id``
    retargets the publish to a group. Video ceilings are keyed
    separately from photos (docs/10 §1 (uid, content_type) buckets).

    Returns:
        0 — a missing post id in the publish echo is reported in-band
        (post id '-'), not as a failure.
    """
    with with_session(new_session(args)) as session:
        service = VideoUploadService(session)
        if args.caption is not None:
            result = service.post_video(args.path, args.caption,
                                         Privacy.from_name(args.privacy),
                                         group_id=args.group_id)
            payload: dict[str, Any] = {
                "video_id": result["video_id"],
                "post_id": _story_id(result["publish"]),
                "upload": result["upload"],
            }
            emit(args, payload,
                 human=lambda: print(
                     f"video id: {result['video_id']}\n"
                     f"post id: {payload['post_id'] or '-'}\n"
                     f"privacy: {args.privacy}"))
        else:
            upload = service.upload_video(args.path)
            emit(args, upload,
                 human=lambda: print(f"video id: {upload['video_id']}"))
        return 0
