"""Draft commands: ``fbk draft save/list/show/publish/delete``.

A draft is a CLI-LOCAL composed-post spec under state/drafts/
(src/drafts.py — the module docstring carries the native-draft recon
gap: the harvested registry has no ComposerDraft mutations, so the
server's own draft surface is unreachable and this store is the honest,
fully-offline substitute). save/list/show/delete never touch a session;
``draft publish`` resolves the stored spec into the exact call
``fbk feed publish`` makes — FeedService.publish with the spec's text
and privacy (plus the ai_label/background extras when set) — so the
draft is published through the same live-verified mutation path
(ComposerStoryCreateMutation, docs/15 §P3).

The fields no captured mutation can carry yet (media, tags, feeling,
activity, place) are STORED but not sent at publish time — they
round-trip in the spec until a capture lands (the recon gap again;
publish reports only what actually went out).

Name handling matches the journal family: a name is a BARE basename,
validated before any path resolves (invalid → exit 1); an absent draft
is a failed precondition (exit 1, the house convention); an existing
draft is never silently overwritten without --force (the journal-export
refusal, exit 1).

USER-DOC ANCHOR: cli/docs/05-reference-content.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from typing import Any

from domain.common import Privacy
from drafts import DraftSpec, DraftStore, is_valid_draft_name
from surfaces.feed import FeedService

from .common import add_common_args, build_config, emit, new_session, with_session

_PRIVACY_NAMES = ["public", "friends", "private"]


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the `draft` family onto the root parser: save, list, show,
    publish, delete."""
    p = sub.add_parser(
        "draft", help="draft store: save/list/publish composed posts "
                      "(CLI-local; state/drafts/)")
    csub = p.add_subparsers(dest="draft_command", required=True)

    save = csub.add_parser("save", help="save a composed post spec as a draft")
    save.add_argument("--name", required=True,
                      help="draft name (bare; state/drafts/<name>.json)")
    save.add_argument("--text", required=True, help="post text")
    save.add_argument("--privacy", default="friends", choices=_PRIVACY_NAMES,
                      help="audience when published (default friends)")
    save.add_argument("--ai-label", dest="ai_label", default=None,
                      choices=("on", "off"),
                      help="AI-disclosure toggle for this post")
    save.add_argument("--background", default=None, metavar="ID",
                      help="background-color preset id (small-text posts)")
    save.add_argument("--media", default=None, metavar="PATH[,PATH]...",
                      help="comma-separated media paths (stored, not yet "
                           "publishable)")
    save.add_argument("--tag", action="append", dest="tags", metavar="UID:NAME",
                      help="a tagged person (repeatable; stored, not yet "
                           "publishable)")
    save.add_argument("--feeling", default=None, metavar="ID",
                      help="feeling/activity emotion id (stored)")
    save.add_argument("--activity", default=None, metavar="ID",
                      help="activity id (stored)")
    save.add_argument("--place", default=None, metavar="ID",
                      help="place id (stored)")
    save.add_argument("--force", action="store_true",
                      help="allow saving over an existing draft of this name")
    add_common_args(save)
    save.set_defaults(fn=cmd_save)

    listing = csub.add_parser("list", help="enumerate drafts under state/drafts/")
    add_common_args(listing)
    listing.set_defaults(fn=cmd_list)

    show = csub.add_parser("show", help="print one draft's full spec")
    show.add_argument("--name", required=True, help="draft name")
    add_common_args(show)
    show.set_defaults(fn=cmd_show)

    publish = csub.add_parser("publish", help="publish one draft to the feed")
    publish.add_argument("--name", required=True, help="draft name")
    publish.add_argument("--delete", action="store_true",
                         help="delete the draft after a successful publish "
                              "(default keeps it)")
    add_common_args(publish)
    publish.set_defaults(fn=cmd_publish)

    delete = csub.add_parser("delete", help="remove one draft")
    delete.add_argument("--name", required=True, help="draft name")
    add_common_args(delete)
    delete.set_defaults(fn=cmd_delete)


def _store(args: argparse.Namespace) -> DraftStore:
    """The DraftStore under the invocation's resolved state/ directory."""
    return DraftStore(build_config(args).state_dir)


def _invalid_name(name: str) -> int:
    """The shared path-shaped-name refusal (exit 1, journal convention)."""
    print(f"error: invalid draft name {name!r} — a bare name, no path "
          f"separators or dot-segments", file=sys.stderr)
    return 1


def _absent(name: str, state_dir: str) -> int:
    """The shared absent-draft / path-shaped-name precondition (exit 1)."""
    print(f"error: no draft {name!r} under {state_dir} — --name takes a "
          f"bare draft NAME (state/drafts/<name>.json); see `fbk draft "
          f"list`", file=sys.stderr)
    return 1


def cmd_save(args: argparse.Namespace) -> int:
    """Save one composed-post spec as a draft (pure offline state).

    The name is validated at the boundary; an existing draft of the same
    name is never silently overwritten (exit 1, the journal-export
    refusal — ``--force`` is the explicit opt-in).

    Returns:
        0 on save; 1 when the name is path-shaped or the draft exists
        and ``--force`` was not passed.
    """
    if not is_valid_draft_name(args.name):
        return _invalid_name(args.name)
    store = _store(args)
    if store.exists(args.name) and not args.force:
        print(f"error: refusing to overwrite draft {args.name!r} — pass "
              f"--force to overwrite an existing draft", file=sys.stderr)
        return 1
    media = [p.strip() for p in (args.media or "").split(",") if p.strip()]
    spec = DraftSpec(
        name=args.name,
        text=args.text,
        privacy=args.privacy,
        ai_label=None if args.ai_label is None else args.ai_label == "on",
        background=args.background,
        media=media,
        tags=list(args.tags or []),
        feeling=args.feeling,
        activity=args.activity,
        place=args.place,
        created_at=datetime.now(UTC).isoformat(),
    )
    path = store.save(spec, force=args.force)
    emit(args, {"draft": spec.name, "path": str(path),
                "text_head": spec.text.splitlines()[0][:60] if spec.text else ""},
         human=lambda: print(f"saved draft {spec.name!r} → {path}"))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """Enumerate every draft under the resolved drafts directory.

    Zero drafts is a REPORTED fact ("no drafts"), never an error — the
    journal list contract.

    Returns:
        0 always: an empty store is payload data.
    """
    store = _store(args)
    entries = store.list()
    payload: dict[str, Any] = {
        "state_dir": str(store.state_dir),
        "count": len(entries),
        "drafts": entries,
    }

    def human() -> None:
        if not entries:
            print(f"no drafts under {store.dir}")
            return
        print(f"{len(entries)} draft(s) under {store.dir}")
        for e in entries:
            print(f"{e['name']:<28} {e['created_at']:<28} "
                  f"[{e['media_count']} media] {e['text_head']}")

    emit(args, payload, human=human)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """Print one draft's full spec (``--json`` emits the raw spec dict).

    Returns:
        0 on success; 1 when the name is path-shaped or no such draft
        exists (failed precondition, the journal show convention).
    """
    store = _store(args)
    spec = store.load(args.name)
    if spec is None:
        return _absent(args.name, str(store.state_dir))
    payload = {"draft": spec.name, "spec": spec.model_dump()}

    def human() -> None:
        print(f"draft {spec.name!r} (created {spec.created_at})")
        print(f"text: {spec.text}")
        print(f"privacy: {spec.privacy}  ai_label: {spec.ai_label}  "
              f"background: {spec.background}")
        if spec.media:
            print("media:")
            for m in spec.media:
                print(f"  {m}")
        if spec.tags:
            print("tags: " + ", ".join(spec.tags))
        for key in ("feeling", "activity", "place"):
            value = getattr(spec, key)
            if value is not None:
                print(f"{key}: {value}")

    emit(args, payload, human=human)
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    """Publish one draft: the exact call ``fbk feed publish`` makes.

    Delegation choice (the concurrent edit on commands/feed.py): no
    shared helper was touched — this handler MIRRORS cmd_publish's body
    against the same service path, constructing FeedService on the
    command's own session and calling ``service.publish(text,
    Privacy.from_name(privacy))`` with the spec's stored text and
    privacy (plus ai_label → ai_generated and background →
    text_format_preset_id, the two composer extras the service already
    accepts). Sequential by design like every mutation — the governor
    paces inside the transport (docs/15 §P8-1).

    ``--delete`` removes the draft AFTER a successful publish; a failed
    publish propagates its typed error (run_command's exit contract)
    and the draft is kept.

    Returns:
        0 on success; 1 when the name is path-shaped or no such draft
        exists (failed precondition, the journal convention).
    """
    store = _store(args)
    spec = store.load(args.name)
    if spec is None:
        return _absent(args.name, str(store.state_dir))
    with with_session(new_session(args)) as session:
        service = FeedService(session)
        response = service.publish(
            spec.text, Privacy.from_name(spec.privacy),
            ai_generated=spec.ai_label,
            text_format_preset_id=spec.background)
    payload: dict[str, Any] = {"draft": spec.name, "published": response}
    if args.delete:
        payload["draft_deleted"] = store.delete(spec.name)
    emit(args, payload, human=lambda: print(
        f"published draft {spec.name!r}" +
        (" (draft deleted)" if payload.get("draft_deleted") else "")))
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    """Remove one draft from the store (pure offline state).

    Returns:
        0 on deletion; 1 when the name is path-shaped or no such draft
        exists (failed precondition, the house convention).
    """
    store = _store(args)
    if not store.delete(args.name):
        return _absent(args.name, str(store.state_dir))
    emit(args, {"draft": args.name, "deleted": True},
         human=lambda: print(f"deleted draft {args.name!r}"))
    return 0
