"""Offline unit tests for the friends surface
(docs/02-endpoint-surface-map.md §2.9 — friending family). Everything runs
against StubSession with canned root-payload structures crafted from the
2026-09 live probe of the /friends/ page: friend rows ride
``friend_confirmed_notifications`` edges, request rows ride the
``friend_requests`` connection (aliased friending_possibilities,
REQUESTS_JEWEL) with viewer-relative ``friendship_status``. No network, no
cookie access, and NO mutation is ever live-fired."""
from __future__ import annotations

from fakes import ASSETS, StubSession

from graphql.registry import DocIdRegistry
from surfaces.friends import (
    CANCEL_MUTATION,
    CLEAR_BADGE_MUTATION,
    CLEAR_BADGE_VARIABLES,
    CONFIRM_MUTATION,
    CONFIRM_VARIABLES_TEMPLATE,
    DEFAULT_FRIENDS_VARIABLES,
    DELETE_MUTATION,
    DELETE_VARIABLES_TEMPLATE,
    ROOT_DOC_ID,
    ROOT_QUERY_NAME,
    SEND_MUTATION,
    SEND_VARIABLES_TEMPLATE,
    UNFRIEND_MUTATION,
    UNFRIEND_VARIABLES_TEMPLATE,
    FriendsService,
)

UID = "12345678901234"
ALICE = "1000000000000001"
BOB = "1000000000000002"
CAROL = "1000000000000003"
DAVE = "1000000000000004"


def user_node(uid: str, name: str, **extra: object) -> dict:
    node: dict = {"__typename": "User", "id": uid, "name": name}
    node.update(extra)
    return node


def root_payload(*, friends: list[dict] | None = None,
                 requests: list[dict] | None = None,
                 pymk: list[dict] | None = None) -> dict:
    """A canned FriendingCometRootContentQuery response, crafted from the
    live-probed selection set (2026-09): confirmed-friend rows carry no
    friendship_status; request rows carry the viewer-relative status."""
    return {"data": {"viewer": {
        "friends_container_request_count": {"count": len(requests or [])},
        "friends_container_pymk_count": {"count": len(pymk or [])},
        "friend_confirmed_notifications": {
            "count": len(friends or []),
            "edges": [{"node": node, "cursor": f"cursor-{i}"}
                      for i, node in enumerate(friends or [])],
            "page_info": {"end_cursor": None, "has_next_page": False},
        },
        "should_show_proactive_friending_alert": False,
        "max_friend_limit": 5000,
        "friend_requests": {
            "edges": [{"node": node, "expiration_time": 1789736000 + i}
                      for i, node in enumerate(requests or [])],
            "page_info": {"has_next_page": False, "end_cursor": None},
        },
        "pymk_grid": {
            "tracking_signature": None,
            "edges": [{"node": node} for node in pymk or []],
            "page_info": {"has_next_page": False, "end_cursor": ""},
        },
        "should_show_osa_explainer": False,
    }}}


class TestFriendsList:
    """Pins the typed friends list off the live-probed root payload,
    excluding requests and pymk suggestions."""

    def test_list_typed_rows_with_default_variables(self):
        session = StubSession({
            ROOT_QUERY_NAME: root_payload(
                friends=[user_node(ALICE, "Alice Alison"),
                         user_node(BOB, "Bob Bobson")]),
        })
        service = FriendsService(session)
        friends = service.list()

        assert [(f.name, f.id) for f in friends] == [
            ("Alice Alison", ALICE), ("Bob Bobson", BOB)]
        assert all(f.is_self is False for f in friends)
        assert len(session.graphql.calls) == 1
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == ROOT_QUERY_NAME
        assert doc_id == ROOT_DOC_ID == "28351661531162960"
        assert variables == DEFAULT_FRIENDS_VARIABLES

    def test_list_includes_are_friends_rows_deeper_in_payload(self):
        payload = root_payload(friends=[user_node(ALICE, "Alice Alison")])
        # a second friend list section the walker must still find
        payload["data"]["viewer"]["friends_tab"] = {
            "edges": [{"node": user_node(
                DAVE, "Dave Dave", friendship_status="ARE_FRIENDS")}],
        }
        session = StubSession({ROOT_QUERY_NAME: payload})
        friends = FriendsService(session).list()

        assert [f.id for f in friends] == [ALICE, DAVE]

    def test_list_excludes_requests_and_suggestions(self):
        session = StubSession({
            ROOT_QUERY_NAME: root_payload(
                friends=[user_node(ALICE, "Alice Alison")],
                requests=[user_node(CAROL, "Carol Carolson",
                                    friendship_status="INCOMING_REQUEST"),
                          user_node(BOB, "Bob Bobson",
                                    friendship_status="OUTGOING_REQUEST")],
                pymk=[user_node(DAVE, "Dave Dave",
                                friendship_status="CAN_REQUEST")]),
        })
        friends = FriendsService(session).list()

        assert [f.id for f in friends] == [ALICE]

    def test_list_limit(self):
        session = StubSession({
            ROOT_QUERY_NAME: root_payload(
                friends=[user_node(ALICE, "A"), user_node(BOB, "B"),
                         user_node(CAROL, "C")]),
        })
        friends = FriendsService(session).list(limit=2)
        assert [f.id for f in friends] == [ALICE, BOB]

    def test_list_empty_payload(self):
        session = StubSession({ROOT_QUERY_NAME: {"data": {"viewer": {}}}})
        assert FriendsService(session).list() == []


class TestFriendsRequests:
    """Pins the incoming/outgoing request split keyed by the
    viewer-relative friendship_status."""

    def test_requests_split_incoming_and_outgoing(self):
        session = StubSession({
            ROOT_QUERY_NAME: root_payload(
                requests=[user_node(CAROL, "Carol Carolson",
                                    friendship_status="INCOMING_REQUEST"),
                          user_node(DAVE, "Dave Dave",
                                    friendship_status="OUTGOING_REQUEST"),
                          user_node(BOB, "Bob Bobson",
                                    friendship_status="CAN_REQUEST")]),
        })
        split = FriendsService(session).requests()

        assert [r["id"] for r in split["incoming"]] == [CAROL]
        assert split["incoming"][0]["name"] == "Carol Carolson"
        assert split["incoming"][0]["friendship_status"] == "INCOMING_REQUEST"
        assert split["incoming"][0]["expiration_time"] == 1789736000
        assert [r["id"] for r in split["outgoing"]] == [DAVE]
        friendly, doc_id, _variables = session.graphql.calls[0]
        assert friendly == ROOT_QUERY_NAME
        assert doc_id == "28351661531162960"

    def test_requests_direction_incoming(self):
        session = StubSession({
            ROOT_QUERY_NAME: root_payload(
                requests=[user_node(CAROL, "Carol Carolson",
                                    friendship_status="INCOMING_REQUEST"),
                          user_node(DAVE, "Dave Dave",
                                    friendship_status="OUTGOING_REQUEST")]),
        })
        split = FriendsService(session).requests(direction="incoming")
        assert [r["id"] for r in split["incoming"]] == [CAROL]
        assert split["outgoing"] == []

    def test_requests_direction_outgoing(self):
        session = StubSession({
            ROOT_QUERY_NAME: root_payload(
                requests=[user_node(CAROL, "Carol Carolson",
                                    friendship_status="INCOMING_REQUEST"),
                          user_node(DAVE, "Dave Dave",
                                    friendship_status="OUTGOING_REQUEST")]),
        })
        split = FriendsService(session).requests(direction="outgoing")
        assert split["incoming"] == []
        assert [r["id"] for r in split["outgoing"]] == [DAVE]


class TestFriendMutations:
    """Each mutation: exact doc_id, user_id in the right variable path,
    fresh click_correlation_id (this schema's per-call nonce — the
    friending family carries no client_mutation_id), and nothing else fired."""

    def test_request_send(self):
        session = StubSession({
            SEND_MUTATION: {"data": {"friend_request_send": {
                "friend_requestees": [{"__typename": "User", "id": DAVE}]}}},
        })
        response = FriendsService(session).request(DAVE)

        assert response["data"]["friend_request_send"]["friend_requestees"][0]["id"] == DAVE
        assert len(session.graphql.calls) == 1
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == SEND_MUTATION
        assert doc_id == "28400389149651601"
        assert variables["input"]["friend_requestee_ids"] == [DAVE]
        assert variables["scale"] == 2
        assert variables["input"]["friending_channel"] == "PROFILE_BUTTON"
        assert variables["input"]["attribution_id_v2"] is None

    def test_request_fresh_click_correlation_id(self):
        session = StubSession({SEND_MUTATION: {"data": {}}})
        service = FriendsService(session)
        service.request(DAVE)
        service.request(DAVE)

        first = session.graphql.calls[0][2]["input"]["click_correlation_id"]
        second = session.graphql.calls[1][2]["input"]["click_correlation_id"]
        assert first != second
        assert first  # both non-empty

    def test_cancel(self):
        session = StubSession({
            CANCEL_MUTATION: {"data": {"friend_request_cancel": {
                "cancelled_friend_requestee": {"__typename": "User", "id": DAVE}}}},
        })
        response = FriendsService(session).cancel(DAVE)

        assert response["data"]["friend_request_cancel"]["cancelled_friend_requestee"]["id"] == DAVE
        assert len(session.graphql.calls) == 1
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == CANCEL_MUTATION
        assert doc_id == "24453541284254355"
        assert variables["input"]["cancelled_friend_requestee_id"] == DAVE
        assert variables["scale"] == 2

    def test_accept(self):
        session = StubSession({
            CONFIRM_MUTATION: {"data": {"friend_request_accept": {
                "friend_requester": {"__typename": "User", "id": CAROL}}}},
        })
        response = FriendsService(session).accept(CAROL)

        assert response["data"]["friend_request_accept"]["friend_requester"]["id"] == CAROL
        assert len(session.graphql.calls) == 1
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == CONFIRM_MUTATION
        assert doc_id == "27351021931180810"
        assert variables["input"]["friend_requester_id"] == CAROL
        assert variables["refresh_num"] == 0
        assert variables["scale"] == 2
        assert variables["should_fix_banner"] is False

    def test_decline(self):
        session = StubSession({
            DELETE_MUTATION: {"data": {"friend_request_delete": {
                "friend_requester": {"__typename": "User", "id": CAROL}}}},
        })
        response = FriendsService(session).decline(CAROL)

        assert response["data"]["friend_request_delete"]["friend_requester"]["id"] == CAROL
        assert len(session.graphql.calls) == 1
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == DELETE_MUTATION
        assert doc_id == "27694540350183683"
        assert variables["input"]["friend_requester_id"] == CAROL
        assert variables["refresh_num"] == 0
        assert variables["scale"] == 2

    def test_unfriend(self):
        session = StubSession({
            UNFRIEND_MUTATION: {"data": {"friend_remove": {
                "unfriended_person": {"__typename": "User", "id": ALICE}}}},
        })
        response = FriendsService(session).unfriend(ALICE)

        assert response["data"]["friend_remove"]["unfriended_person"]["id"] == ALICE
        assert len(session.graphql.calls) == 1
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == UNFRIEND_MUTATION
        assert doc_id == "24028849793460009"
        assert variables["input"]["unfriended_user_id"] == ALICE
        assert variables["input"]["source"] == "friends_manage_list"
        assert variables["scale"] == 2

    def test_clear_badge(self):
        session = StubSession({
            CLEAR_BADGE_MUTATION: {"data": {"viewer_friends_badge_count_clear": {
                "viewer_for_badge_count": {"viewer": {"bookmarks": {}}}}}},
        })
        response = FriendsService(session).clear_badge()

        assert response["data"]["viewer_friends_badge_count_clear"]["viewer_for_badge_count"] \
            is not None
        assert len(session.graphql.calls) == 1
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == CLEAR_BADGE_MUTATION
        assert doc_id == "10034776853248745"
        assert variables == CLEAR_BADGE_VARIABLES
        assert variables["bookmarkIDs"] == ["2356318349"]
        assert variables["input"] == {}

    def test_templates_are_never_mutated(self):
        for template in (SEND_VARIABLES_TEMPLATE, CONFIRM_VARIABLES_TEMPLATE,
                         DELETE_VARIABLES_TEMPLATE, UNFRIEND_VARIABLES_TEMPLATE):
            snapshot = {k: (dict(v) if isinstance(v, dict) else v)
                        for k, v in template.items()}
            session = StubSession({SEND_MUTATION: {"data": {}},
                                   CONFIRM_MUTATION: {"data": {}},
                                   DELETE_MUTATION: {"data": {}},
                                   UNFRIEND_MUTATION: {"data": {}}})
            service = FriendsService(session)
            service.request(UID)
            service.accept(UID)
            service.decline(UID)
            service.unfriend(UID)
            for key, value in snapshot.items():
                assert template[key] == value


class TestRegistryGroundTruth:
    """Pins every friending doc_id against the real asset registry."""

    def test_all_friending_doc_ids_resolve_from_assets(self):
        registry = DocIdRegistry.from_assets(ASSETS)
        expected = {
            "FriendingCometRootContentQuery": "28351661531162960",
            "FriendingCometFriendRequestSendMutation": "28400389149651601",
            "FriendingCometFriendRequestConfirmMutation": "27351021931180810",
            "FriendingCometFriendRequestDeleteMutation": "27694540350183683",
            "FriendingCometFriendRequestCancelMutation": "24453541284254355",
            "FriendingCometUnfriendMutation": "24028849793460009",
            "FriendingCometFriendsBadgeCountClearMutation": "10034776853248745",
        }
        for name, doc_id in expected.items():
            assert registry.doc_id(name) == doc_id, name

    def test_service_uses_registry_lookup(self):
        # doc_id() must flow through the session registry (StubSession loads
        # the real assets registry), not a hard-coded bypass.
        session = StubSession({ROOT_QUERY_NAME: root_payload()})
        service = FriendsService(session)
        service.list()
        friendly, doc_id, _variables = session.graphql.calls[0]
        assert doc_id == session.registry.doc_id(friendly)
