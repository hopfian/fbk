"""SAVED surface service: the saved-items dashboard and the save/unsave
actions (docs/02 §2 endpoint families, docs/04 §6, docs/15 live calibration).

ARCHITECTURE:

  The dashboard read follows the docs/15 §P2-2 preload pattern — GET /saved/,
  harvest the ``CometSaveDashboardRootQuery`` SSR registration, replay it
  verbatim — with a baked fallback doc_id/variables pair for sessions whose
  page fetch fails. The save/unsave actions replay the bundle-decoded
  browser shapes with a fresh ``client_mutation_id`` per call. All traffic
  flows through Session/GraphQLClient (docs/12 §3), so the service is fully
  offline-testable against StubSession (tests/fakes.py).

CALIBRATION NOTES:

  Ground truth, live-verified 2026-09:

  * LIST: GET /saved/ preloads ``CometSaveDashboardRootQuery``
    (doc_id 26929010753443830, page-harvested — not in the bundle registry)
    with verbatim variables ``{content_filter, hoisted_item_id, notif_id,
    scale}`` (docs/15 §P2-2 preload pattern). Replaying it returns
    ``viewer.saver_info.all_saves.edges[]`` where every edge node is a ``Save``
    (docs/04 §7 identifier model) wrapping its ``savable`` (title, permalink,
    typename). The dashboard SSR result is also embedded server-side in the
    page (``adp_CometSaveDashboardRootQueryRelayPreloader_…__bbox``), which
    proves the replay payload shape.
  * SAVE / UNSAVE — schema-DECODED from the owning rsrc.php bundles and then
    LIVE-VERIFIED end-to-end (save -> dashboard count 1 -> unsave -> count 0,
    state-neutral, on a public sample post):

    - The registry's ``useUpdateBookmarkBatchMutation`` (27293619020331978)
      decodes (bundle rsrc.php/v4i5pN4/…/6WCSHyauNTP.js) to the left-rail
      SHORTCUTS editor — ``bookmark_update_shortcuts_state_batch`` with
      ``input.bookmark_state_batch = [{bookmark_id, state:
      AUTOMATIC|PINNED|HIDDEN}]`` (CometEditShortcutsDialog). It is NOT the
      post save/unsave action, so this service deliberately does not use it
      (docs/15 §P4-1 save-plane correction).
    - The real save plane is ``CometSaveMutation`` (doc_id 9855506394526824,
      ``node_saved_state``) — input ``{client_mutation_id, node_id,
      save_action: "SAVE", save_mechanism, surface}`` where ``node_id`` is the
      SAVABLE node id (for a photo post: the photo fbid carried by the
      story's ``save_info.savable.id``). Live response:
      ``node_saved_state.save_node.viewer_saved_state == "SAVED"``.
    - Unsave is the sibling ``useUnsaveMutation`` (doc_id 8500826123375303):
      same input family with ``save_action: "UNSAVE"`` plus
      ``contributorRoles: ["CONTRIBUTOR"]`` (CometSaveCollectionConstants)
      and ``scale`` (WebPixelRatio). Live response:
      ``node_saved_state.save_node.viewer_saved_state == "NOT_SAVED"``.

USER-DOC ANCHOR: cli/docs/04-reference-feed.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import uuid
from typing import Any

from auth.bootstrap import extract_preload_registry

from .base import Surface, _walk_preorder

# --------------------------------------------------------------------- names
#: The saved-items dashboard page URL — GET carries the SSR preloader
#: registration that supplies the faithful read (docs/15 §P2-2).
SAVED_URL = "https://www.facebook.com/saved/"
DASHBOARD_QUERY = "CometSaveDashboardRootQuery"
#: Live-probed 2026-09 off the /saved/ SSR preloader registration; this
#: query is not in the harvested bundle registry (docs/15 §P2-2 pattern).
DASHBOARD_FALLBACK_DOC_ID = "26929010753443830"
DEFAULT_SAVED_VARIABLES: dict[str, Any] = {
    "content_filter": None,
    "hoisted_item_id": None,
    "notif_id": None,
    "scale": 2,
}

# The real save plane (bundle-decoded + live-verified, docs/15):
SAVE_MUTATION = "CometSaveMutation"
SAVE_MUTATION_DOC_ID = "9855506394526824"
UNSAVE_MUTATION = "useUnsaveMutation"
UNSAVE_MUTATION_DOC_ID = "8500826123375303"

#: Decoded from CometSaveCollectionConstants (bundle): the only contributor
#: role the unsave mutation requests.
CONTRIBUTOR_ROLES: list[str] = ["CONTRIBUTOR"]


def _savable_row(node: dict[str, Any]) -> dict[str, Any] | None:
    """One ``Save`` edge node -> a typed row, or None when shapeless.

    Row shape (from the live-probed dashboard payload): the Save id, the
    savable's title head, its permalink, and its type (savable __typename
    with the decoded ``savable_default_category`` as fallback).
    """
    savable = node.get("savable")
    if not isinstance(savable, dict):
        return None
    title = savable.get("savable_title")
    if isinstance(title, dict):
        title = title.get("text")
    url = savable.get("savable_permalink") or savable.get("url")
    return {
        "id": str(node.get("id") or savable.get("id") or ""),
        "title": (str(title).splitlines()[0][:120] if title else None),
        "url": url if isinstance(url, str) else None,
        "type": (savable.get("__typename")
                 or savable.get("savable_default_category") or None),
    }


class SavedService(Surface):
    """The saved-items surface: dashboard read plus save/unsave (docs/02 §2)."""

    # ------------------------------------------------------------------ reads
    def _dashboard_preload(self) -> tuple[str, dict[str, Any]]:
        """(doc_id, variables) from the /saved/ preloader registration.

        docs/15 §P2-2: the SSR registers the dashboard query with the
        VERBATIM variables the server itself used; falls back to the
        live-baked defaults when the page carries no preload.
        """
        html = self._fetch(SAVED_URL)
        for entry in extract_preload_registry(html):
            if entry.query_name == DASHBOARD_QUERY:
                return entry.doc_id, dict(entry.variables)
        return DASHBOARD_FALLBACK_DOC_ID, dict(DEFAULT_SAVED_VARIABLES)

    def list(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """The saved-items dashboard, newest first, up to ``limit`` rows.

        GETs /saved/, harvests the CometSaveDashboardRootQuery preload and
        replays it (the faithful read, docs/15 §P2-2), then walks
        ``viewer.saver_info.all_saves.edges`` into typed rows
        ``{id, title, url, type}``.

        Args:
            limit: Row cap applied after the deduplicating walk.

        Returns:
            ``{id, title, url, type}`` dicts — the Save id, the savable's
            title head (first line, 120-char cap), its permalink and its
            typename (or decoded fallback category); ``[]`` for an empty
            collection.

        Raises:
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        doc_id, variables = self._dashboard_preload()
        data = self.client.call(DASHBOARD_QUERY, doc_id, variables)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for node in _walk_preorder(data):
            if node.get("__typename") != "Save":
                continue
            row = _savable_row(node)
            if row is None or not row["id"] or row["id"] in seen:
                continue
            seen.add(row["id"])
            rows.append(row)
        return rows[:limit]

    # ---------------------------------------------------------------- actions
    def save(self, story_or_item_id: str) -> dict[str, Any]:
        """Save an item (CometSaveMutation, live-verified: docs/15).

        ``story_or_item_id`` is the SAVABLE node id — for a feed post the
        id carried by the story's ``save_info.savable.id`` (e.g. the photo
        fbid on a photo post), not the story key. Replays the decoded
        browser shape with a fresh client_mutation_id; the merged response
        carries ``node_saved_state.save_node.viewer_saved_state == "SAVED"``.

        Args:
            story_or_item_id: The savable node id from the story's
                ``save_info.savable.id`` (docs/15 §P4-1).

        Returns:
            The merged mutation response — verify the
            ``viewer_saved_state`` field, not the success envelope alone
            (docs/10 §3.4 soft-suppression caveat).

        Raises:
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        variables: dict[str, Any] = {
            "input": {
                "client_mutation_id": str(uuid.uuid4()),
                "node_id": story_or_item_id,
                "save_action": "SAVE",
                "save_mechanism": "CARET_MENU",
                "surface": "STORY",
            },
        }
        return self.client.call(SAVE_MUTATION, SAVE_MUTATION_DOC_ID, variables)

    def unsave(self, story_or_item_id: str) -> dict[str, Any]:
        """Unsave an item (useUnsaveMutation, live-verified: docs/15).

        The decoded sibling of the save mutation: same node_id semantics,
        ``save_action: "UNSAVE"``, plus the decoded ``contributorRoles`` and
        ``scale`` envelope. The merged response carries
        ``node_saved_state.save_node.viewer_saved_state == "NOT_SAVED"``.

        Args:
            story_or_item_id: The same savable node id :meth:`save`
                accepted (idempotent when the item is already unsaved).

        Returns:
            The merged mutation response carrying the post-mutation
            ``viewer_saved_state``.

        Raises:
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        variables: dict[str, Any] = {
            "contributorRoles": list(CONTRIBUTOR_ROLES),
            "input": {
                "client_mutation_id": str(uuid.uuid4()),
                "node_id": story_or_item_id,
                "save_action": "UNSAVE",
                "save_mechanism": "CARET_MENU",
                "surface": "STORY",
            },
            "scale": 2,
        }
        return self.client.call(UNSAVE_MUTATION, UNSAVE_MUTATION_DOC_ID,
                                variables)
