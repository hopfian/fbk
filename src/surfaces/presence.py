"""Presence surface service: the viewer's chat-visibility status
(docs/02 §2, docs/15 §P2-2).

ARCHITECTURE:

  A single zero-variable read through the governor-paced GraphQL client.
  This is a deliberately read-only surface: the CLI reports the status, it
  never writes the visibility setting (that would be a CometSettings
  mutation, out of scope — flipping presence is both a mutation-budget
  consumer and a socially visible act).

CALIBRATION NOTES:

  Ground truth (all live-verified 2026-09):

  * The chat online-status setting query is
    ``useFBChatVisibility_PresenceStatusChatVisibilityQuery``
    (doc_id 9899572666749146). It is NOT preloaded by /messages/ (that page
    only preloads MWThreadListQPQuery and the encrypted-backups upsell
    query), so its wire shape was bundle-decoded from the owning rsrc.php
    module (``useFBChatVisibility_PresenceStatusChatVisibilityQuery.graphql``):
    ``argumentDefinitions`` is EMPTY — the query takes NO variables — and
    the selection is a single ``viewer { chat_visibility }`` scalar.
  * Live replay (read-only): {"data": {"viewer": {"chat_visibility": true}}}
    where chat_visibility true == the viewer appears online (chat active).

USER-DOC ANCHOR: cli/docs/04-reference-feed.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

from typing import Any

from .base import Surface

PRESENCE_QUERY = "useFBChatVisibility_PresenceStatusChatVisibilityQuery"
PRESENCE_DOC_ID = "9899572666749146"

# bundle-decoded 2026-09: the query's argumentDefinitions list is empty —
# the hook commits createOperationDescriptor(request, {}) with no variables.
DEFAULT_PRESENCE_VARIABLES: dict[str, Any] = {}


class PresenceService(Surface):
    """The presence surface: viewer chat-visibility status reads."""

    def status(self) -> dict[str, Any]:
        """Replay the chat-visibility query and trim to the status fields.

        Returns ``{"chat_visibility": <bool | None>}`` — the live response
        carries exactly one visibility field (``viewer.chat_visibility``,
        true = online/visible); None means the payload carried no viewer
        node (logged-out or soft-blocked responses do).

        Returns:
            The one-key status dict; ``chat_visibility`` is ``None`` when
            the replayed payload carried no well-shaped viewer node.

        Raises:
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        payload = self.client.call(PRESENCE_QUERY, self.doc_id(PRESENCE_QUERY),
                                   dict(DEFAULT_PRESENCE_VARIABLES))
        data = payload.get("data") if isinstance(payload, dict) else None
        viewer = data.get("viewer") if isinstance(data, dict) else None
        visibility = viewer.get("chat_visibility") \
            if isinstance(viewer, dict) else None
        return {"chat_visibility": visibility if isinstance(visibility, bool)
                else None}
