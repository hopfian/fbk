"""Saved commands: saved-items dashboard + save/unsave (docs/02 §2
saved-items family; docs/15 live calibration).

Each command builds a SavedService off a fresh Session and emits through
the shared CLI plumbing (commands/common.py). The save/unsave plane is the
bundle-decoded, live-verified CometSaveMutation/useUnsaveMutation pair
(surfaces/saved.py header for the useUpdateBookmarkBatchMutation finding).

USER-DOC ANCHOR: cli/docs/04-reference-feed.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse
from typing import Any

from surfaces.saved import SavedService

from .common import add_common_args, emit, new_session, with_session


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the `saved` family onto the root parser: list, save, unsave."""
    saved = sub.add_parser("saved", help="saved-items surface (list, save, unsave)")
    ssub = saved.add_subparsers(dest="saved_command", required=True)

    listing = ssub.add_parser("list", help="list saved items (dashboard query)")
    add_common_args(listing)
    listing.add_argument("--limit", type=int, default=20,
                         help="maximum items to emit (default 20)")
    listing.set_defaults(fn=cmd_list)

    save_p = ssub.add_parser("save", help="save an item (savable node id)")
    save_p.add_argument("--item-id", required=True,
                        help="savable node id (e.g. the photo fbid of a photo post)")
    add_common_args(save_p)
    save_p.set_defaults(fn=cmd_save)

    unsave_p = ssub.add_parser("unsave", help="unsave an item (savable node id)")
    unsave_p.add_argument("--item-id", required=True,
                          help="savable node id to unsave")
    add_common_args(unsave_p)
    unsave_p.set_defaults(fn=cmd_unsave)


def _save_state(response: dict[str, Any]) -> str:
    """viewer_saved_state from a save/unsave mutation response — the
    single authoritative field confirming which bookmark state the
    server now holds ("?" when the node shape is absent)."""
    node = response.get("data", {}).get("node_saved_state", {})
    save_node = node.get("save_node") if isinstance(node, dict) else None
    if isinstance(save_node, dict):
        return str(save_node.get("viewer_saved_state") or "?")
    return "?"


def cmd_list(args: argparse.Namespace) -> int:
    """List the saved-items dashboard rows.

    Returns:
        0 — an empty dashboard is valid account state.
    """
    with with_session(new_session(args)) as session:
        service = SavedService(session)
        items: list[dict[str, Any]] = service.list(limit=args.limit)

        def human() -> None:
            print(f"saved items: {len(items)}")
            for item in items:
                print(f"[{item['type'] or '?'}] {item['title'] or '?'} "
                      f"{item['url'] or ''} ({item['id']})")

        emit(args, {"items": items, "count": len(items)}, human=human)
        return 0


def cmd_save(args: argparse.Namespace) -> int:
    """Save an item by savable node id (CometSaveMutation — decoded,
    live-verified).

    The item id is the savable node id (e.g. a photo post's photo
    fbid), not the story key.

    Returns:
        0 — the emitted viewer_saved_state confirms the server-side
        outcome.
    """
    with with_session(new_session(args)) as session:
        service = SavedService(session)
        response = service.save(args.item_id)
        state = _save_state(response)
        emit(args, {"item_id": args.item_id, "viewer_saved_state": state},
             human=lambda: print(f"saved {args.item_id} -> {state}"))
        return 0


def cmd_unsave(args: argparse.Namespace) -> int:
    """Unsave a previously saved item by savable node id
    (useUnsaveMutation — decoded, live-verified).

    Returns:
        0 — the emitted viewer_saved_state confirms the server-side
        outcome.
    """
    with with_session(new_session(args)) as session:
        service = SavedService(session)
        response = service.unsave(args.item_id)
        state = _save_state(response)
        emit(args, {"item_id": args.item_id, "viewer_saved_state": state},
             human=lambda: print(f"unsaved {args.item_id} -> {state}"))
        return 0
