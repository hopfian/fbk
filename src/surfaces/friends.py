"""Friends surface (docs/02-endpoint-surface-map.md §2.9 — friending family).

One root read plus the six friending mutations, all replayed through the
governor-paced GraphQL client (docs/10 §7) and typed against
``domain.common.User``.

ARCHITECTURE:

  Every read shares ONE root payload: ``FriendingCometRootContentQuery``
  preloads on the /friends/ page and its selection carries friends,
  requests, suggestions and counts in one response — ``list``,
  ``requests`` and ``suggestions`` replay it and classify the same edges
  differently instead of issuing three queries. Mutations mint a fresh
  ``click_correlation_id`` (uuid4) per call — the per-call nonce this
  family uses in place of ``client_mutation_id`` — and template variables
  are schema-decoded from the owning bundles (docs/13 §2 archaeology).

CALIBRATION NOTES:

Live ground truth (all verified against the /friends/ page 2026-09):

* Root query: ``FriendingCometRootContentQuery`` (doc_id 28351661531162960)
  preloads on https://www.facebook.com/friends/ with the verbatim variables
  ``{"scale": 2}`` — replayed live as-is. The schema-decoded selection set
  carries:
    - ``viewer.friend_confirmed_notifications`` (edges[].node = User rows of
      recently-confirmed friends, no friendship_status field),
    - ``viewer.friend_requests`` — alias for
      ``friending_possibilities(first:20, friending_channel:"REQUESTS_JEWEL")``
      whose edges[].node are User rows with ``friendship_status``
      ("INCOMING_REQUEST" / "OUTGOING_REQUEST" / "CAN_REQUEST"),
    - ``viewer.pymk_grid`` — alias for
      ``people_you_may_know(first:20, location:"FRIENDS_HOME_MAIN")``
      (suggestions, not friends),
    - the container counts and ``max_friend_limit``.
* Mutations (schema-decoded 2026-09 from the owning rsrc bundles; NOT
  live-fired — they are socially visible):
    - Send       FriendingCometFriendRequestSendMutation     28400389149651601
    - Confirm    FriendingCometFriendRequestConfirmMutation   27351021931180810
    - Delete     FriendingCometFriendRequestDeleteMutation    27694540350183683
    - Cancel     FriendingCometFriendRequestCancelMutation    24453541284254355
    - Unfriend   FriendingCometUnfriendMutation               24028849793460009
    - ClearBadge FriendingCometFriendsBadgeCountClearMutation 10034776853248745

USER-DOC ANCHOR: cli/docs/06-reference-people.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""

from __future__ import annotations

import copy
from typing import Any, Literal
from uuid import uuid4

from domain.common import User

from .base import Surface

#: Root read — /friends/ page preload (live-replayed verbatim 2026-09;
#: doc_id from the live SSR preloader registration, docs/15 §P2-2 pattern).
ROOT_QUERY_NAME = "FriendingCometRootContentQuery"
ROOT_DOC_ID = "28351661531162960"

# Module-scope list aliases: the service's `list` method shadows the builtin
# inside class-body annotations, so signatures use these instead.
UserList = list[User]
NodeList = list[dict[str, Any]]

#: Verbatim page-preload variables (live-probed 2026-09).
DEFAULT_FRIENDS_VARIABLES: dict[str, Any] = {"scale": 2}

#: Row classification by the viewer-relative friendship_status enum
#: (live-decoded from the bundles: INCOMING_REQUEST / OUTGOING_REQUEST /
#: ARE_FRIENDS / CAN_REQUEST / CANNOT_REQUEST / RECEIVED_REQUEST).
STATUS_INCOMING = "INCOMING_REQUEST"
STATUS_OUTGOING = "OUTGOING_REQUEST"
STATUS_ARE_FRIENDS = "ARE_FRIENDS"

#: Friendly names of the six friending mutations — schema-decoded 2026-09
#: from the owning rsrc bundles (doc_ids in the module header table; the
#: variable templates below carry each bundle's decoded LocalArgument set).
SEND_MUTATION = "FriendingCometFriendRequestSendMutation"
CANCEL_MUTATION = "FriendingCometFriendRequestCancelMutation"
CONFIRM_MUTATION = "FriendingCometFriendRequestConfirmMutation"
DELETE_MUTATION = "FriendingCometFriendRequestDeleteMutation"
UNFRIEND_MUTATION = "FriendingCometUnfriendMutation"
CLEAR_BADGE_MUTATION = "FriendingCometFriendsBadgeCountClearMutation"

#: The generic profile-context friending channel used by the profile
#: FriendingButton (live-decoded bundle literals: PROFILE_BUTTON,
#: FRIENDS_HOME_MAIN, PYMK_FEED, RHC_FRIEND_REQUESTS, ...).
FRIENDING_CHANNEL = "PROFILE_BUTTON"

#: schema-decoded 2026-09 (bundle 2cce2ba4c30672f2cf6c.js):
#: LocalArguments "input" + "scale"; commit passes
#: {input: {attribution_id_v2, click_correlation_id, click_proof_validation_result,
#:   extra_data, friend_requestee_ids: [id], friending_channel,
#:   people_you_may_know_location, warn_ack_for_ids}, scale: WebPixelRatio}
#: response field: friend_request_send.friend_requestees
SEND_VARIABLES_TEMPLATE: dict[str, Any] = {
    "input": {
        "attribution_id_v2": None,
        "click_correlation_id": None,
        "click_proof_validation_result": None,
        "extra_data": None,
        "friend_requestee_ids": [],
        "friending_channel": FRIENDING_CHANNEL,
        "people_you_may_know_location": None,
        "warn_ack_for_ids": [],
    },
    "scale": 2,
}

#: schema-decoded 2026-09 (bundle 2cce2ba4c30672f2cf6c.js):
#: LocalArguments "input" + "scale"; commit passes
#: {input: {attribution_id_v2, cancelled_friend_requestee_id, click_correlation_id,
#:   click_proof_validation_result, friending_channel}, scale}
#: response field: friend_request_cancel.cancelled_friend_requestee
CANCEL_VARIABLES_TEMPLATE: dict[str, Any] = {
    "input": {
        "attribution_id_v2": None,
        "cancelled_friend_requestee_id": None,
        "click_correlation_id": None,
        "click_proof_validation_result": None,
        "friending_channel": FRIENDING_CHANNEL,
    },
    "scale": 2,
}

#: schema-decoded 2026-09 (bundle eu5mEY4dyC8…):
#: LocalArguments "input", "refresh_num", "scale", "should_fix_banner"; commit
#: passes {input: {attribution_id_v2, click_correlation_id,
#:   click_proof_validation_result, friend_requester_id, friending_channel,
#:   warn_ack}, refresh_num: 0, scale, should_fix_banner}
#: response field: friend_request_accept.friend_requester
CONFIRM_VARIABLES_TEMPLATE: dict[str, Any] = {
    "input": {
        "attribution_id_v2": None,
        "click_correlation_id": None,
        "click_proof_validation_result": None,
        "friend_requester_id": None,
        "friending_channel": FRIENDING_CHANNEL,
        "warn_ack": False,
    },
    "refresh_num": 0,
    "scale": 2,
    "should_fix_banner": False,
}

#: schema-decoded 2026-09 (bundle eu5mEY4dyC8…):
#: LocalArguments "input", "refresh_num", "scale"; commit passes
#: {input: {click_correlation_id, click_proof_validation_result,
#:   friend_requester_id, friending_channel}, refresh_num: 0, scale}
#: response field: friend_request_delete.friend_requester
DELETE_VARIABLES_TEMPLATE: dict[str, Any] = {
    "input": {
        "click_correlation_id": None,
        "click_proof_validation_result": None,
        "friend_requester_id": None,
        "friending_channel": FRIENDING_CHANNEL,
    },
    "refresh_num": 0,
    "scale": 2,
}

#: schema-decoded 2026-09 (bundle d2ve5JPtLYp.js / Cfbg9DlKMmQ.js):
#: LocalArguments "input" + "scale"; the manage-friends dialog commits with
#: source "friends_manage_list": {input: {source, unfriended_user_id}, scale}
#: response field: friend_remove.unfriended_person
UNFRIEND_VARIABLES_TEMPLATE: dict[str, Any] = {
    "input": {
        "source": "friends_manage_list",
        "unfriended_user_id": None,
    },
    "scale": 2,
}

#: schema-decoded 2026-09 (bundle eu5mEY4dyC8…):
#: LocalArguments "bookmarkIDs", "hasBookmark", "hasTopTab", "input"; the
#: friends-page commit passes {bookmarkIDs: ["2356318349"], hasBookmark: !0,
#: hasTopTab: !0, input: {}} (the friends bookmark clears the badge counter).
#: response field: viewer_friends_badge_count_clear.viewer_for_badge_count
CLEAR_BADGE_VARIABLES: dict[str, Any] = {
    "bookmarkIDs": ["2356318349"],
    "hasBookmark": True,
    "hasTopTab": True,
    "input": {},
}


class FriendsService(Surface):
    """Friending-family service (docs/02 §2.9): friends read + the six
    schema-decoded friending mutations."""

    # ------------------------------------------------------------------ reads
    def list(self, *, limit: int = 50) -> UserList:
        """Friend rows carried by the friends-page root payload.

        Replays ``FriendingCometRootContentQuery`` with its verbatim page
        variables (live-probed 2026-09), then walks the response for User
        nodes with names: the recently-confirmed friend rows
        (``friend_confirmed_notifications`` edges) plus any User node whose
        ``friendship_status`` is ``ARE_FRIENDS``. Request rows and
        people-you-may-know suggestions are not friends and stay out.

        Args:
            limit: Row cap applied after the walk.

        Returns:
            ``User`` rows (id + name) in payload order, deduplicated;
            ``[]`` when the account carries no friend rows.

        Raises:
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        data = self.client.call(
            ROOT_QUERY_NAME, self.doc_id(ROOT_QUERY_NAME), copy.deepcopy(DEFAULT_FRIENDS_VARIABLES)
        )
        return self._walk_friend_rows(data)[:limit]

    def requests(
        self, *, direction: Literal["incoming", "outgoing", "all"] = "all"
    ) -> dict[str, NodeList]:
        """Split the friending-possibilities rows of the same root payload.

        Incoming requests (viewer-relative ``friendship_status`` of
        ``INCOMING_REQUEST``) and the viewer's pending sends
        (``OUTGOING_REQUEST``) are separated into
        ``{"incoming": [...], "outgoing": [...]}``; ``direction`` keeps only
        the asked side populated.

        Args:
            direction: Which side to populate — ``"incoming"``, ``"outgoing"``
                or ``"all"`` (both); the dropped side is ``[]``.

        Returns:
            A two-key dict of plain serializable request rows
            (``{id, name, friendship_status, [expiration_time]}``).

        Raises:
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        data = self.client.call(
            ROOT_QUERY_NAME, self.doc_id(ROOT_QUERY_NAME), copy.deepcopy(DEFAULT_FRIENDS_VARIABLES)
        )
        incoming, outgoing = self._split_request_rows(data)
        if direction == "incoming":
            return {"incoming": incoming, "outgoing": []}
        if direction == "outgoing":
            return {"incoming": [], "outgoing": outgoing}
        return {"incoming": incoming, "outgoing": outgoing}

    def suggestions(self, *, limit: int = 10) -> UserList:
        """People-you-may-know rows from the same root payload.

        ``viewer.pymk_grid`` — alias for
        ``people_you_may_know(first:20, location:"FRIENDS_HOME_MAIN")`` —
        carries the suggestion rows (live-verified 2026-09: the /friends/
        page preloads the root query with ``{"scale": 2}`` and the payload
        includes the pymk_grid connection). Suggestions are not friends and
        carry no viewer-relative ``friendship_status`` requirement; rows
        without a name are skipped (User nodes with id + name only).

        Args:
            limit: Row cap applied after deduplication.

        Returns:
            ``User`` rows (id + name) in payload order, deduplicated by id.

        Raises:
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        data = self.client.call(
            ROOT_QUERY_NAME, self.doc_id(ROOT_QUERY_NAME), copy.deepcopy(DEFAULT_FRIENDS_VARIABLES)
        )
        out: list[User] = []
        seen: set[str] = set()
        for node in self._edge_nodes(self._viewer(data), "pymk_grid"):
            user = self._user(node)
            if user is not None and user.id not in seen:
                seen.add(user.id)
                out.append(user)
        return out[:limit]

    # -------------------------------------------------------------- mutations
    def request(self, user_id: str) -> dict[str, Any]:
        """Send a friend request (FriendingCometFriendRequestSendMutation).

        ``user_id`` travels in ``input.friend_requestee_ids``; a fresh
        ``click_correlation_id`` is minted per call (schema-decoded 2026-09).

        Args:
            user_id: The requestee's user id.

        Returns:
            The merged mutation response; the response field is
            ``friend_request_send.friend_requestees``.

        Raises:
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        variables = copy.deepcopy(SEND_VARIABLES_TEMPLATE)
        variables["input"]["friend_requestee_ids"] = [user_id]
        variables["input"]["click_correlation_id"] = str(uuid4())
        return self.client.call(SEND_MUTATION, self.doc_id(SEND_MUTATION), variables)

    def cancel(self, user_id: str) -> dict[str, Any]:
        """Cancel the viewer's own outgoing request
        (FriendingCometFriendRequestCancelMutation): ``user_id`` lands in
        ``input.cancelled_friend_requestee_id``.

        Args:
            user_id: The id the pending request targets.

        Returns:
            The merged mutation response; the response field is
            ``friend_request_cancel.cancelled_friend_requestee``.

        Raises:
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        variables = copy.deepcopy(CANCEL_VARIABLES_TEMPLATE)
        variables["input"]["cancelled_friend_requestee_id"] = user_id
        variables["input"]["click_correlation_id"] = str(uuid4())
        return self.client.call(CANCEL_MUTATION, self.doc_id(CANCEL_MUTATION), variables)

    def accept(self, user_id: str) -> dict[str, Any]:
        """Accept an incoming request
        (FriendingCometFriendRequestConfirmMutation): ``user_id`` (the
        sender's id) lands in ``input.friend_requester_id``.

        Args:
            user_id: The requesting user's id (from ``requests()``'s
                incoming rows).

        Returns:
            The merged mutation response; the response field is
            ``friend_request_accept.friend_requester``.

        Raises:
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        variables = copy.deepcopy(CONFIRM_VARIABLES_TEMPLATE)
        variables["input"]["friend_requester_id"] = user_id
        variables["input"]["click_correlation_id"] = str(uuid4())
        return self.client.call(CONFIRM_MUTATION, self.doc_id(CONFIRM_MUTATION), variables)

    def decline(self, user_id: str) -> dict[str, Any]:
        """Decline an incoming request / delete it
        (FriendingCometFriendRequestDeleteMutation): ``user_id`` (the
        sender's id) lands in ``input.friend_requester_id``.

        Args:
            user_id: The requesting user's id (from ``requests()``'s
                incoming rows).

        Returns:
            The merged mutation response; the response field is
            ``friend_request_delete.friend_requester``.

        Raises:
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        variables = copy.deepcopy(DELETE_VARIABLES_TEMPLATE)
        variables["input"]["friend_requester_id"] = user_id
        variables["input"]["click_correlation_id"] = str(uuid4())
        return self.client.call(DELETE_MUTATION, self.doc_id(DELETE_MUTATION), variables)

    def unfriend(self, user_id: str) -> dict[str, Any]:
        """Remove an existing friend (FriendingCometUnfriendMutation):
        ``user_id`` lands in ``input.unfriended_user_id``; ``source`` is the
        schema-decoded "friends_manage_list" channel.

        Args:
            user_id: The friend's id (from ``list()`` rows).

        Returns:
            The merged mutation response; the response field is
            ``friend_remove.unfriended_person``.

        Raises:
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        variables = copy.deepcopy(UNFRIEND_VARIABLES_TEMPLATE)
        variables["input"]["unfriended_user_id"] = user_id
        return self.client.call(UNFRIEND_MUTATION, self.doc_id(UNFRIEND_MUTATION), variables)

    def clear_badge(self) -> dict[str, Any]:
        """Clear the friends-nav badge counter
        (FriendingCometFriendsBadgeCountClearMutation) with the friends-page
        commit's variables (schema-decoded 2026-09; no user input).

        Returns:
            The merged mutation response; the response field is
            ``viewer_friends_badge_count_clear.viewer_for_badge_count``.

        Raises:
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        return self.client.call(
            CLEAR_BADGE_MUTATION,
            self.doc_id(CLEAR_BADGE_MUTATION),
            copy.deepcopy(CLEAR_BADGE_VARIABLES),
        )

    # ----------------------------------------------------------------- parsing
    @staticmethod
    def _viewer(data: dict[str, Any]) -> dict[str, Any]:
        """The merged payload's ``data.viewer`` dict ({} when absent)."""
        viewer = data.get("data", {}).get("viewer", {}) if isinstance(data, dict) else {}
        return viewer if isinstance(viewer, dict) else {}

    @classmethod
    def _edge_nodes(cls, viewer: dict[str, Any], field: str) -> NodeList:
        """User nodes under one connection's edges (live-observed shape)."""
        conn = viewer.get(field)
        if not isinstance(conn, dict):
            return []
        edges = conn.get("edges")
        if not isinstance(edges, list):
            return []
        nodes: list[dict[str, Any]] = []
        for edge in edges:
            node = edge.get("node") if isinstance(edge, dict) else None
            if isinstance(node, dict):
                nodes.append(node)
        return nodes

    @classmethod
    def _walk_friend_rows(cls, data: dict[str, Any]) -> UserList:
        """Typed friend rows: confirmed-friend edges + ARE_FRIENDS users."""
        out: list[User] = []
        seen: set[str] = set()
        viewer = cls._viewer(data)
        for node in cls._edge_nodes(viewer, "friend_confirmed_notifications"):
            user = cls._user(node)
            if user is not None and user.id not in seen:
                seen.add(user.id)
                out.append(user)
        for node in cls._iter_typed_users(data):
            if node.get("friendship_status") != STATUS_ARE_FRIENDS:
                continue
            user = cls._user(node)
            if user is not None and user.id not in seen:
                seen.add(user.id)
                out.append(user)
        return out

    @classmethod
    def _split_request_rows(cls, data: dict[str, Any]) -> tuple[NodeList, NodeList]:
        """Classify friending-possibility rows into (incoming, outgoing).

        ``expiration_time`` is an edge-level field in the live selection
        (edges carry it next to the User node)."""
        incoming: list[dict[str, Any]] = []
        outgoing: list[dict[str, Any]] = []
        viewer = cls._viewer(data)
        conn = viewer.get("friend_requests")
        edges = conn.get("edges") if isinstance(conn, dict) else None
        if not isinstance(edges, list):
            return incoming, outgoing
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            node = edge.get("node")
            if not isinstance(node, dict):
                continue
            row = cls._request_row(node)
            if row is None:
                continue
            if "expiration_time" in edge:
                row["expiration_time"] = edge.get("expiration_time")
            if node.get("friendship_status") == STATUS_INCOMING:
                incoming.append(row)
            elif node.get("friendship_status") == STATUS_OUTGOING:
                outgoing.append(row)
        return incoming, outgoing

    @staticmethod
    def _user(node: dict[str, Any]) -> User | None:
        """A typed User row — only __typename User nodes with id + name."""
        if node.get("__typename") != "User":
            return None
        uid = node.get("id")
        name = node.get("name")
        if not isinstance(uid, str) or not isinstance(name, str) or not name:
            return None
        return User(id=uid, name=name)

    @staticmethod
    def _request_row(node: dict[str, Any]) -> dict[str, Any] | None:
        """A request row (incoming or outgoing) as a plain serializable dict."""
        uid = node.get("id")
        if not isinstance(uid, str) or not uid:
            return None
        row: dict[str, Any] = {
            "id": uid,
            "name": node.get("name"),
            "friendship_status": node.get("friendship_status"),
        }
        return row

    @classmethod
    def _iter_typed_users(cls, node: Any) -> NodeList:
        """Every __typename User dict anywhere in a payload (name optional)."""
        found: list[dict[str, Any]] = []
        if isinstance(node, dict):
            if node.get("__typename") == "User":
                found.append(node)
            else:
                for value in node.values():
                    found.extend(cls._iter_typed_users(value))
        elif isinstance(node, list):
            for item in node:
                found.extend(cls._iter_typed_users(item))
        return found
