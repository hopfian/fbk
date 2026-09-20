"""Notifications surface (docs/02-endpoint-surface-map.md §2.3; docs/15 §P2-2, §P3-5).

The unseen-count badge, the dropdown list read, and the mark-all-seen
mutation — the same three operations the real notifications UI performs.

ARCHITECTURE:

  Both reads are single persisted queries through the governor-paced
  GraphQL client; ``mark_seen`` composes them — it replays the dropdown
  query to harvest the notif ids plus the UI's own ``query_id`` /
  ``last_notif_sync_time`` context, then fires the mutation the decoded
  ``useHandleUpdateMultiNotifSeenState`` hook commits when the list is
  opened. The id-harvest guard mirrors the decoded hook: with no
  notifications the mutation is never fired.

CALIBRATION NOTES:

  Live-verified pairs:
  * badge: ``CometNotificationsBadgeCountQuery`` (doc_id 9714526941947209,
    registry/KNOWN_MUTATIONS) with ``{"environment": "MAIN_SURFACE"}`` ->
    ``data.viewer.notifications_unseen_count``. The environment enum's live
    literals are ``MAIN_SURFACE`` / ``COMET`` (docs/15 §3).
  * list: ``CometNotificationsDropdownQuery`` (doc_id 28272355145709625,
    registry-harvested) with ``{"count": N, "environment": "MAIN_SURFACE",
    "scale": 1}`` — the scale argument was live-discovered by iterating
    VARIABLE_COERCION (1675012) rejections on the read-only query. Rows
    arrive as ``data.viewer.notifications_page.edges[].node`` with
    ``NotifPageNotificationRow`` nodes carrying the ``notif`` block
    (base64 id, seen_state, body.text, url).
  * pagination: ``CometNotificationsListPaginationQuery`` (doc_id
    28762112060039555, registry-harvested) — the /notifications/ page's
    full-list walker. Variable set schema-decoded 2026-09-20 from the
    carrier bundle's relay module (LocalArguments ``count``, ``cursor``,
    ``environment``, ``filter_tokens`` [], ``notif_cache_ids`` [],
    ``notif_query_flags``, ``scale``; the notifications_page field maps
    ``after<-cursor``, ``first<-count``, plus a literal
    ``source:"unknown"``). The page_info.has_next_page flag and each
    edge's own ``cursor`` drive the walk — the next request passes the
    LAST edge's cursor as ``after`` (standard relay connection walking).

USER-DOC ANCHOR: cli/docs/04-reference-feed.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""

from __future__ import annotations

import copy
from typing import Any

from domain.common import Notification, NotificationCount

from .base import Surface

#: Badge-count query — live-verified pair (docs/15 §P2-2/§3) and the
#: cheapest verified read in the registry (measurement canary).
BADGE_QUERY_NAME = "CometNotificationsBadgeCountQuery"
BADGE_DOC_ID = "9714526941947209"
BADGE_VARIABLES: dict[str, Any] = {"environment": "MAIN_SURFACE"}

#: Dropdown list query — doc_id registry-harvested, variable set iterated
#: live against the 1675012 coercion gate (see CALIBRATION NOTES).
LIST_QUERY_NAME = "CometNotificationsDropdownQuery"
LIST_DOC_ID = "28272355145709625"
#: Live-discovered default variables (bundle archaeology + name-guided
#: iteration on the read-only dropdown query).
DEFAULT_NOTIFICATIONS_VARIABLES: dict[str, Any] = {
    "count": 15,
    "environment": "MAIN_SURFACE",
    "scale": 1,
}

#: Row typenames that carry a real notification payload (live-observed).
_ROW_TYPENAME = "NotifPageNotificationRow"

#: Full-list pagination query — the /notifications/ page's walker (the
#: dropdown caps at what the bell shows; this one pages through history).
#: Variable set schema-decoded from the carrier bundle's relay module
#: (see CALIBRATION NOTES above); doc_id registry-harvested.
PAGINATION_QUERY_NAME = "CometNotificationsListPaginationQuery"
PAGINATION_DOC_ID = "28762112060039555"
PAGINATION_VARIABLES_TEMPLATE: dict[str, Any] = {
    "count": 20,
    "cursor": None,
    "environment": "MAIN_SURFACE",
    "filter_tokens": [],
    "notif_cache_ids": [],
    "notif_query_flags": ["IS_COMET", "INCLUDE_WA_P2B_NOTIFS"],
    "scale": 1,
}

#: Safety cap for full-history walks: stop after this many pages even if
#: the wire keeps reporting has_next_page (a stuck cursor loop would
#: otherwise burn the whole request budget on repeated pages).
MAX_WALK_PAGES = 50

# Module-scope list aliases: the service's `list` method shadows the builtin
# inside class-body annotations, so signatures use these instead.
NotificationList = list[Notification]
EdgeList = list[dict[str, Any]]

#: Mark notifications seen (docs/15 §P2-3 bundle archaeology): the /notifications/
#: page bundle set carries the mutation's graphql module with LocalArguments
#: ``environment`` (default null) + ``input`` (default null), operation
#: ``notifications_update_seen_or_read(data: $input)`` ->
#: ``{notifications[{id,seen_state,cache_timestamp}],
#: viewer{notifications_unseen_count(environment:$environment)}}``.
MARK_SEEN_MUTATION = "CometNotificationsUpdateSeenStateMutation"
MARK_SEEN_DOC_ID = "32345368141745868"

#: schema-decoded 2026-09 (bundle `useHandleUpdateMultiNotifSeenState` hook:
#: the MARK_ALL_SEEN commit the notifications UI itself fires when its list
#: is opened): variables are {environment, input}; ``input`` carries
#: {actor_id, environment, is_comet, last_notif_sync_time, notif_ids,
#: query_id, source, update_type}. The hook's query_id is the LIST
#: response's ``notifications_page.query_id`` and last_notif_sync_time its
#: viewer's ``last_update_timestamp``; the decoded JS commit does not show
#: actor_id (the browser relay injects the viewer id server-side of the
#: commit), but the wire input REQUIRES it — live-verified 2026-09: without
#: actor_id the mutation is rejected with variable-coercion 1675012, with
#: the session viewer id it returns notifications[].seen_state plus
#: viewer.notifications_unseen_count; no client_mutation_id is needed.
MARK_SEEN_VARIABLES_TEMPLATE: dict[str, Any] = {
    "environment": "MAIN_SURFACE",
    "input": {
        "actor_id": None,
        "environment": "MAIN_SURFACE",
        "is_comet": True,
        "last_notif_sync_time": 0,
        "notif_ids": [],
        "query_id": None,
        "source": "unknown",
        "update_type": "MARK_ALL_SEEN",
    },
}

#: How many recent notifications feed the mark-seen id list.
_MARK_SEEN_LIST_COUNT = 50


class NotificationsService(Surface):
    """Notifications surface: badge count + dropdown list, both typed."""

    # ------------------------------------------------------------------ public
    def badge(self) -> NotificationCount:
        """The unseen-count badge (live-verified query, docs/15 §P2-2).

        The cheapest verified read in the registry — the same badge call
        the web surface fires constantly, which is why it also serves as
        the measurement harness canary (surfaces/measurement.py).

        Returns:
            A ``NotificationCount`` whose ``unseen`` is the int badge
            value; a missing/None count degrades to 0.

        Raises:
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        data = self.client.call(BADGE_QUERY_NAME, self.doc_id(BADGE_QUERY_NAME), BADGE_VARIABLES)
        viewer = data.get("data", {}).get("viewer", {})
        count = viewer.get("notifications_unseen_count")
        return NotificationCount(unseen=int(count or 0))

    def list(self, *, limit: int = 20) -> NotificationList:
        """The notifications dropdown list, newest first, up to ``limit``.

        Args:
            limit: Row cap; rides the wire as the dropdown query's
                ``count`` variable and re-applies as a post-slice.

        Returns:
            Typed ``Notification`` rows (id, typename, title, body,
            unseen) — only ``NotifPageNotificationRow`` nodes with a
            well-shaped ``notif`` block type through; ``[]`` for an empty
            list or a degraded payload.

        Raises:
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        variables: dict[str, Any] = {**DEFAULT_NOTIFICATIONS_VARIABLES, "count": limit}
        data = self.client.call(LIST_QUERY_NAME, self.doc_id(LIST_QUERY_NAME), variables)
        return self._walk_notifications(data)[:limit]

    def page(self, *, count: int = 20,
             cursor: str | None = None) -> dict[str, Any]:
        """Fetch one full-list page via the pagination query.

        The /notifications/ page's own walker (the dropdown caps at what
        the bell shows; this pages through history). The decoded variable
        set rides verbatim with ``count`` and ``cursor`` substituted —
        page one passes a null cursor, later pages pass the previous
        page's LAST edge cursor as relay's ``after``.

        Args:
            count: Rows per page; rides the wire as the ``first`` arg
                of the notifications_page connection.
            cursor: The previous page's last-edge cursor; None for page
                one.

        Returns:
            ``{"rows": <NotificationList>, "next_cursor": <str | None>,
            "has_next": <bool>}`` — the typed notification rows of this
            page, the cursor to request the next page with (None when
            the page carried no edges), and page_info.has_next_page.

        Raises:
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        variables: dict[str, Any] = {
            **PAGINATION_VARIABLES_TEMPLATE,
            "count": count,
            "cursor": cursor,
        }
        data = self.client.call(
            PAGINATION_QUERY_NAME, self.doc_id(PAGINATION_QUERY_NAME), variables)
        edges = self._edges(data)
        page = data.get("data", {}).get("viewer", {}).get(
            "notifications_page", {}) if isinstance(data, dict) else {}
        page_info = page.get("page_info") if isinstance(page, dict) else None
        has_next = bool(
            page_info.get("has_next_page")) if isinstance(page_info, dict) else False
        last_edge = edges[-1] if edges else None
        next_cursor = (last_edge.get("cursor")
                       if isinstance(last_edge, dict) else None)
        return {
            "rows": self._walk_notifications(data),
            "next_cursor": next_cursor,
            "has_next": has_next,
        }

    def page_at(self, index: int, *, count: int = 20) -> dict[str, Any]:
        """Fetch the index-th page of the full list (1-based).

        Walks pages one through ``index`` with cursor chaining — the
        wire has no random access; every earlier page must be fetched
        to obtain its walking cursor. A history that ends earlier
        stops at the deepest reachable page, reported as ``page``.

        Args:
            index: 1-based page number; 1 is the newest page.
            count: Rows per page (rides the pagination query's ``first``).

        Returns:
            The page() payload of the deepest page reached plus
            ``"page": <int>`` (equal to ``index`` when the walk
            succeeded, the last existing page otherwise); a
            non-positive index raises ValueError.

        Raises:
            ValueError: When ``index`` is not at least 1.
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        if index < 1:
            raise ValueError(f"page index is 1-based, got {index}")
        result: dict[str, Any] = {"rows": [], "next_cursor": None, "has_next": False}
        cursor: str | None = None
        fetched = 0
        while fetched < index:
            result = self.page(count=count, cursor=cursor)
            fetched += 1
            cursor = result["next_cursor"]
            # end of history: no next page exists, whatever the cursor
            if cursor is None or not result["has_next"]:
                break
        result["page"] = fetched
        return result

    def paginate(self, *, count: int = 20,
                 max_pages: int = MAX_WALK_PAGES) -> dict[str, Any]:
        """Walk the full notification history until the wire ends.

        Cursor-chains the pagination query the way the /notifications/
        page does on scroll: each request passes the last edge cursor
        as ``after`` until page_info.has_next_page goes false, an edge
        cursor is missing, or the safety cap stops a stuck walk. Rows
        are deduped by notif id across page boundaries (relay page
        seams can repeat rows).

        Args:
            count: Rows per page (rides the pagination query's
                ``first``).
            max_pages: Safety cap on pages fetched; MAX_WALK_PAGES is
                the default so a full walk can never burn the whole
                daily request budget on a stuck cursor loop.

        Returns:
            ``{"notifications": <NotificationList>, "pages": <int>,
            "has_next": <bool>}`` — the deduped rows in walk order, how
            many pages were fetched, and whether the wire still
            reports more (True only when the cap stopped the walk).

        Raises:
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        out: list[Notification] = []
        seen: set[str] = set()
        pages = 0
        cursor: str | None = None
        has_next = False
        while True:
            result = self.page(count=count, cursor=cursor)
            pages += 1
            for row in result["rows"]:
                if row.id is not None and row.id in seen:
                    continue
                if row.id is not None:
                    seen.add(row.id)
                out.append(row)
            cursor = result["next_cursor"]
            has_next = bool(result["has_next"]) and cursor is not None
            if not has_next or pages >= max_pages:
                break
        return {"notifications": out, "pages": pages, "has_next": has_next}

    # ----------------------------------------------------------------- parsing
    @classmethod
    def _walk_notifications(cls, data: dict[str, Any]) -> NotificationList:
        """Extract NotifPageNotificationRow rows into typed Notifications."""
        out: list[Notification] = []
        page = data.get("data", {}).get("viewer", {}).get("notifications_page", {})
        edges = page.get("edges") if isinstance(page, dict) else None
        if not isinstance(edges, list):
            return out
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            node = edge.get("node")
            if not isinstance(node, dict) or node.get("__typename") != _ROW_TYPENAME:
                continue
            notif = node.get("notif")
            if not isinstance(notif, dict):
                continue
            body = notif.get("body")
            text = body.get("text") if isinstance(body, dict) else None
            seen_state = str(notif.get("seen_state") or "")
            out.append(
                Notification(
                    id=(notif.get("id") or notif.get("notif_id") or None),
                    typename=str(node.get("__typename")),
                    title=(notif.get("notif_type") or None),
                    body=text if isinstance(text, str) else None,
                    unseen=seen_state.startswith("UNSEEN"),
                )
            )
        return out

    # --------------------------------------------------------------- mutations
    def mark_seen(self) -> dict[str, Any]:
        """Mark the listed notifications seen (CometNotificationsUpdateSeenStateMutation).

        Exactly what the notifications UI does when its list is opened: the
        ids of the notifications the viewer received ride ``input.notif_ids``
        with ``update_type:"MARK_ALL_SEEN"`` (schema-decoded 2026-09 from the
        owning bundle hook), alongside the viewer id as ``input.actor_id``
        (wire-required, live-verified: the mutation rejects a missing
        actor_id with variable-coercion 1675012) and the list response's own
        ``notifications_page.query_id`` and viewer ``last_update_timestamp``.
        The id list is harvested from the same dropdown query `list` uses;
        with no notifications at all the mutation is not fired (the decoded
        hook guards on a non-empty id list).

        Returns:
            ``{"marked": <int>, "unseen": <int | None>, "data": <trimmed
            mutation response>}`` — the marked count, the post-mutation
            unseen count (the mutation response carries
            ``viewer.notifications_unseen_count``), and the trimmed
            mutation response payload. No notifications yields
            ``{"marked": 0, "unseen": None, "data": {}}`` without firing.

        Raises:
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        data = self.client.call(
            LIST_QUERY_NAME,
            self.doc_id(LIST_QUERY_NAME),
            {**DEFAULT_NOTIFICATIONS_VARIABLES, "count": _MARK_SEEN_LIST_COUNT},
        )
        viewer = data.get("data", {}).get("viewer", {}) if isinstance(data, dict) else {}
        page = viewer.get("notifications_page") if isinstance(viewer, dict) else {}
        if not isinstance(page, dict):
            page = {}
        ids: list[str] = []
        for edge in self._edges(data):
            node = edge.get("node") if isinstance(edge, dict) else None
            if not isinstance(node, dict) or node.get("__typename") != _ROW_TYPENAME:
                continue
            notif = node.get("notif")
            if isinstance(notif, dict) and isinstance(notif.get("id"), str):
                ids.append(notif["id"])
        if not ids:
            return {"marked": 0, "unseen": None, "data": {}}
        variables = copy.deepcopy(MARK_SEEN_VARIABLES_TEMPLATE)
        variables["input"]["notif_ids"] = ids
        variables["input"]["actor_id"] = self.session.user_id()
        query_id = page.get("query_id")
        if isinstance(query_id, str) and query_id:
            variables["input"]["query_id"] = query_id
        sync_time = viewer.get("last_update_timestamp") if isinstance(viewer, dict) else None
        if isinstance(sync_time, (int, float)):
            variables["input"]["last_notif_sync_time"] = int(sync_time)
        response = self.client.call(MARK_SEEN_MUTATION, self.doc_id(MARK_SEEN_MUTATION), variables)
        payload = response.get("data") or {} if isinstance(response, dict) else {}
        unseen = (
            (payload.get("notifications_update_seen_or_read") or {})
            .get("viewer", {})
            .get("notifications_unseen_count")
        )
        return {
            "marked": len(ids),
            "unseen": int(unseen) if isinstance(unseen, (int, float)) else None,
            "data": payload,
        }

    # ----------------------------------------------------------------- parsing
    @staticmethod
    def _edges(data: dict[str, Any]) -> EdgeList:
        """The raw notifications_page edges (any row shape, untyped)."""
        page = data.get("data", {}).get("viewer", {}).get("notifications_page", {})
        edges = page.get("edges") if isinstance(page, dict) else None
        return edges if isinstance(edges, list) else []
