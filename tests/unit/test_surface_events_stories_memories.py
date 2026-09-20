"""Offline unit tests for the events + stories + memories surfaces
(docs/02-endpoint-surface-map.md §2, docs/15 §P2-2 verbatim-preload
methodology). Everything runs against StubSession with `_fetch`
monkeypatched and crafted bootstrap preloads — no network, no cookies.

Canned payloads mirror the live-probed wire shapes (2026-09-18):
  * /events/ preloads EventCometHomeRootQuery; rows arrive as
    viewer.actor.upcoming_events.edges[].node + content_tab.requested_tab.
    events.edges[].node with id/name/day_time_sentence/start_timestamp/
    is_past/is_viewer_host (+going_count when the variant carries one).
  * the event discussion feed wraps Story nodes under
    data.node.event_stories.edges with a connection page_info.
  * useEventCometDeleteMutation commits {input:{acontext,event_id},scale}
    (schema-decoded — no client_mutation_id) -> event_cancel.canceled_event_id.
  * EventCometLightweightCreateMutation commits {input:<decoded dialog
    object>} (no client_mutation_id) -> fb_event_create.event.id.
  * the stories tray tiles ride data.me.unified_stories_buckets.edges[]
    .node with story_bucket_owner/unified_stories.nodes.
  * the memories feed cards ride viewer.throwback.throwback_units.edges[]
    .node with a connection page_info.
"""
from __future__ import annotations

import base64
import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import ASSETS, StubSession

from auth.bootstrap import Bootstrap, PreloadEntry
from auth.state import LoginState
from graphql.errors import GraphQLProtocolError
from graphql.registry import DocIdRegistry
from surfaces.events import (
    CREATE_DOC_ID,
    CREATE_INPUT_TEMPLATE,
    CREATE_MUTATION,
    DEFAULT_EVENT_FEED_VARIABLES,
    DEFAULT_EVENTS_VARIABLES,
    DELETE_DOC_ID,
    DELETE_MUTATION,
    EVENTS_ACTION_CONTEXT,
    EVENTS_URL,
    FEED_DOC_ID,
    FEED_QUERY,
    LIST_DOC_ID,
    LIST_QUERY,
    EventsService,
    event_node_id,
)
from surfaces.memories import (
    DEFAULT_MEMORIES_FEED_VARIABLES,
    MemoriesService,
    parse_memories_feed,
)
from surfaces.memories import (
    FEED_DOC_ID as MEMORIES_DOC_ID,
)
from surfaces.memories import (
    FEED_QUERY as MEMORIES_QUERY,
)
from surfaces.stories import (
    DEFAULT_STORIES_TRAY_VARIABLES,
    TRAY_DOC_ID,
    TRAY_QUERY,
    StoriesService,
    parse_tray_tiles,
)

UID = "12345678901234"
EVENT_ID = "987654321012345"
EVENT_RELAY_ID = base64.b64encode(f"Event:{EVENT_ID}".encode()).decode().rstrip("=")


def craft_preload_html(*blocks: tuple[str, str, dict]) -> str:
    """Canned Comet page HTML embedding SSR preload registrations in the
    live wire format (docs/15 §P2-2)."""
    parts = []
    for query_name, doc_id, variables in blocks:
        block = {
            "actorID": UID,
            "preloaderID": f"adp_{query_name}RelayPreloader_deadbeef1234abcd",
            "queryID": doc_id,
            "variables": variables,
            "queryName": query_name,
        }
        parts.append(json.dumps(block, separators=(",", ":")))
    return "<html><head></head><body>" + "".join(parts) + "</body></html>"


def stub_fetch(monkeypatch, service, html: str) -> list[str]:
    """Monkeypatch a service's `_fetch`; return the recorded URLs."""
    urls: list[str] = []

    def fake(url: str) -> str:
        urls.append(url)
        return html

    monkeypatch.setattr(service, "_fetch", fake)
    return urls


def craft_bootstrap(preloads: list[PreloadEntry]) -> Bootstrap:
    return Bootstrap(
        state=LoginState.LOGGED_IN,
        fb_dtsg="NAfTEST:1:1789723298",
        lsd="TESTLSD00000000000",
        user_id=UID,
        user_name="Test User",
        preloads=preloads,
    )


def craft_events_list_payload() -> dict[str, Any]:
    """The live-probed EventCometHomeRootQuery response shape, populated
    with two rows (one hosted upcoming event, one discover row carrying a
    going-count variant)."""
    return {"data": {"viewer": {"actor": {
        "__typename": "User",
        "id": UID,
        "upcoming_events": {"edges": [{"node": {
            "__typename": "Event",
            "id": EVENT_ID,
            "name": "Release party",
            "day_time_sentence": "Friday, September 25 at 6:00 PM",
            "start_timestamp": 1790424000,
            "is_past": False,
            "is_viewer_host": True,
        }}]},
        "content_tab": {"requested_tab": {"events": {"edges": [{"node": {
            "__typename": "Event",
            "id": "1122334455667788",
            "name": "City tech meetup",
            "day_time_sentence": "Saturday, September 26 at 10:00 AM",
            "start_timestamp": 1790460000,
            "is_past": False,
            "is_viewer_host": False,
            "going_count": 42,
        }}],
            "page_info": {"end_cursor": "cursor-not-required", "has_next_page": False},
        }}},
    }}}}


def craft_event_feed_payload(cursor: str = "NEXT") -> dict[str, Any]:
    """The schema-decoded discussion-feed response: Story nodes under
    data.node.event_stories.edges plus the connection page_info."""
    def story(i: int) -> dict[str, Any]:
        return {"__typename": "Story", "id": f"UzpfSUZTOjE6{i}:9",
                "creation_time": 1790400000 + i,
                "message": {"text": f"see you at {i}"},
                "comet_sections": {},
                "actors": [{"__typename": "User", "id": UID, "name": "Test User"}]}
    return {"data": {"node": {"__typename": "Event", "id": EVENT_RELAY_ID,
                              "event_stories": {
                                  "edges": [{"node": story(i)} for i in range(2)],
                                  "page_info": {"end_cursor": cursor,
                                                "has_next_page": True}}}}}


def craft_delete_payload() -> dict[str, Any]:
    return {"data": {"event_cancel": {"canceled_event_id": EVENT_ID,
                                      "canceled_child_event_ids": []}}}


def craft_create_payload() -> dict[str, Any]:
    return {"data": {"fb_event_create": {"event": {"id": EVENT_ID}}}}


def craft_tray_payload() -> dict[str, Any]:
    """The live-probed StoriesTrayRectangularRootQuery tray shape
    (bucket nodes under me.unified_stories_buckets.edges)."""
    def bucket(bid: str, name: str, cards: int) -> dict[str, Any]:
        return {"node": {
            "__typename": "UserStoryBucket",
            "__isStoryBucket": "UserStoryBucket",
            "id": bid,
            "story_bucket_owner": {
                "__typename": "User", "id": f"100{bid[-4:]}", "name": name,
                "profilePic": {"uri": f"https://scontent.example/pic-{bid}.png"},
            },
            "unified_stories": {"nodes": [{"id": f"card-{i}"} for i in range(cards)],
                                "is_empty": cards == 0},
            "is_bucket_seen_by_viewer": True,
            "is_bucket_live": False,
            "story_bucket_type": "STORY",
        }}
    return {"data": {"me": {"__typename": "User", "id": UID,
                            "unified_stories_buckets": {"edges": [
                                bucket("122100842439458110", "Test User", 3),
                                bucket("122100842439458999", "Ada Lovelace", 1)],
                                "page_info": {"end_cursor": "TRAYCURSOR",
                                              "has_next_page": False}}}}}


def craft_memories_payload() -> dict[str, Any]:
    """The bundle-decoded CometMemoriesFeedQuery response: Story /
    GoodwillCometStory units under viewer.throwback.throwback_units."""
    return {"data": {"viewer": {"throwback": {"throwback_units": {"edges": [
        {"node": {"__typename": "Story", "id": "UzpfSUZTOjE6MTIz:1",
                   "creation_time": 1461024000,
                   "message": {"text": "throwback text"},
                   "feedback": {"id": "ZmVlZGJhY2s6MzkzNzAy0"}}},
        {"node": {"__typename": "GoodwillCometStory", "id": "GW1",
                   "date_text": "On this day in 2016"}},
    ],
        "page_info": {"end_cursor": "MTc4OTczNjE4MDoxMA==",
                      "has_next_page": True}}}}}}


# ---------------------------------------------------------------------------
class TestEventsList:
    """Pins the events list replay: verbatim preload variables and typed
    rows from both the upcoming_events and discover sections."""

    def test_list_from_live_preload_replay(self, monkeypatch):
        html = craft_preload_html((LIST_QUERY, LIST_DOC_ID,
                                   DEFAULT_EVENTS_VARIABLES))
        session = StubSession({LIST_QUERY: craft_events_list_payload()})
        service = EventsService(session)
        urls = stub_fetch(monkeypatch, service, html)
        rows = service.list()

        assert urls == [EVENTS_URL]
        assert len(rows) == 2
        assert rows[0] == {"id": EVENT_ID, "name": "Release party",
                           "date_text": "Friday, September 25 at 6:00 PM",
                           "start_timestamp": 1790424000, "is_past": False,
                           "is_viewer_host": True, "going_count": None,
                           "typename": "Event", "source": "upcoming_events"}
        assert rows[1]["going_count"] == 42
        assert rows[1]["source"] == "discover"
        assert len(session.graphql.calls) == 1
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == LIST_QUERY
        assert doc_id == LIST_DOC_ID
        assert variables == DEFAULT_EVENTS_VARIABLES  # verbatim replay

    def test_list_fallback_baked_variables(self, monkeypatch):
        session = StubSession({LIST_QUERY: craft_events_list_payload()})
        service = EventsService(session)
        stub_fetch(monkeypatch, service, "<html><body>no preloads</body></html>")
        rows = service.list(limit=1)

        assert len(rows) == 1
        _friendly, doc_id, variables = session.graphql.calls[0]
        assert doc_id == self._registry_doc_id(LIST_QUERY)
        assert variables == DEFAULT_EVENTS_VARIABLES

    def test_list_survives_fetch_failure(self, monkeypatch):
        session = StubSession({LIST_QUERY: craft_events_list_payload()})
        service = EventsService(session)

        def boom(url: str) -> str:
            raise RuntimeError("edge soft-block")

        monkeypatch.setattr(service, "_fetch", boom)
        rows = service.list()

        # no preload -> the baked live-probed variables drive the replay
        assert len(rows) == 2
        friendly, _doc_id, variables = session.graphql.calls[0]
        assert friendly == LIST_QUERY
        assert variables == DEFAULT_EVENTS_VARIABLES

    @staticmethod
    def _registry_doc_id(name: str) -> str:
        return DocIdRegistry.from_assets(ASSETS).doc_id(name)


class TestEventsFeed:
    """Pins the event discussion feed: b64/numeric id normalization,
    cursor passthrough, and Story decode off event_stories.edges."""

    def test_feed_decodes_stories_and_cursor(self):
        session = StubSession({FEED_QUERY: craft_event_feed_payload()})
        service = EventsService(session)
        page = service.feed(EVENT_ID, limit=2)

        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == FEED_QUERY
        assert doc_id == FEED_DOC_ID
        assert variables["id"] == EVENT_RELAY_ID
        assert variables["count"] == 2
        assert variables["cursor"] is None
        assert variables["scale"] == 2
        assert set(variables) == set(DEFAULT_EVENT_FEED_VARIABLES)
        assert page["end_cursor"] == "NEXT"
        assert page["has_next_page"] is True
        assert len(page["stories"]) == 2
        assert page["stories"][0]["id"] == "UzpfSUZTOjE60:9"
        assert page["stories"][0]["creation_time"] == 1790400000
        assert page["stories"][1]["id"] == "UzpfSUZTOjE61:9"

    def test_feed_honors_cursor_and_b64_ids(self):
        session = StubSession({FEED_QUERY: craft_event_feed_payload("NEXT2")})
        service = EventsService(session)
        service.feed(EVENT_RELAY_ID, limit=10, cursor="PREVCURSOR")

        _friendly, doc_id, variables = session.graphql.calls[0]
        assert doc_id == FEED_DOC_ID
        assert variables["id"] == EVENT_RELAY_ID  # passthrough for b64 form
        assert variables["cursor"] == "PREVCURSOR"
        assert variables["count"] == 10

    def test_feed_accepts_numeric_id_normalization(self):
        assert event_node_id(EVENT_ID) == EVENT_RELAY_ID
        assert event_node_id(EVENT_RELAY_ID) == EVENT_RELAY_ID


class TestEventsDelete:
    """Pins useEventCometDeleteMutation: the decoded input shape (no
    client_mutation_id invented) and template integrity across calls."""

    def test_delete_schema_shape_and_substitution(self):
        session = StubSession({DELETE_MUTATION: craft_delete_payload()})
        service = EventsService(session)
        response = service.delete(EVENT_ID)

        assert response["data"]["event_cancel"]["canceled_event_id"] == EVENT_ID
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == DELETE_MUTATION
        assert doc_id == DELETE_DOC_ID
        assert variables["input"]["event_id"] == EVENT_ID
        assert variables["input"]["acontext"] == EVENTS_ACTION_CONTEXT
        assert variables["scale"] == 2
        # the decoded schema carries NO client_mutation_id — none invented
        assert "client_mutation_id" not in variables["input"]

    def test_delete_template_not_mutated_between_calls(self):
        session = StubSession({DELETE_MUTATION: craft_delete_payload()})
        service = EventsService(session)
        service.delete("111")
        service.delete("222")
        first, second = session.graphql.calls
        assert first[2]["input"]["event_id"] == "111"
        assert second[2]["input"]["event_id"] == "222"


class TestEventsCreate:
    """Pins EventCometLightweightCreateMutation: full decoded dialog input
    and the GROUP-privacy rejection."""

    def test_create_substitutes_fields(self):
        session = StubSession({CREATE_MUTATION: craft_create_payload()})
        service = EventsService(session)
        response = service.create("Launch night", "2026-09-25", "18:00",
                                  end_date="2026-09-25", end_time="21:00",
                                  timezone="Asia/Dhaka",
                                  description="celebrate",
                                  privacy="PRIVATE_TYPE")

        assert response["data"]["fb_event_create"]["event"]["id"] == EVENT_ID
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == CREATE_MUTATION
        assert doc_id == CREATE_DOC_ID
        inp = variables["input"]
        assert inp["name"] == "Launch night"
        assert inp["start_date"] == "2026-09-25"
        assert inp["start_time"] == "18:00"
        assert inp["end_date"] == "2026-09-25"
        assert inp["end_time"] == "21:00"
        assert inp["timezone"] == "Asia/Dhaka"
        assert inp["description"] == "celebrate"
        assert inp["event_privacy_type"] == "PRIVATE_TYPE"
        # schema-decoded shape: the full decoded dialog input, no cmid
        assert set(inp) == set(CREATE_INPUT_TEMPLATE)
        assert "client_mutation_id" not in inp

    def test_create_rejects_group_privacy(self):
        session = StubSession({})
        service = EventsService(session)
        with pytest.raises(ValueError):
            service.create("x", "2026-09-25", "18:00", privacy="GROUP")


class TestStoriesTray:
    """Pins the stories tray: preload doc_id precedence, the baked
    fallback, and empty-tolerance in the tile walkers."""

    def test_tray_from_bootstrap_preload_replay(self):
        entry = PreloadEntry(actor_id=UID,
                             preloader_id="adp_StoriesTrayRectangularRootQueryRelayPreloader_ab",
                             doc_id=TRAY_DOC_ID, query_name=TRAY_QUERY,
                             variables=copy.deepcopy(DEFAULT_STORIES_TRAY_VARIABLES))
        session = StubSession({TRAY_QUERY: craft_tray_payload()})
        session._bootstrap = craft_bootstrap([entry])
        service = StoriesService(session)
        tiles = service.tray(limit=20)

        assert len(tiles) == 2
        assert tiles[0]["owner_name"] == "Test User"
        assert tiles[0]["card_count"] == 3
        assert tiles[0]["thumbnail"] == "https://scontent.example/pic-122100842439458110.png"
        assert tiles[0]["is_seen"] is True
        assert tiles[0]["typename"] == "UserStoryBucket"
        assert tiles[1]["card_count"] == 1
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == TRAY_QUERY
        assert doc_id == TRAY_DOC_ID  # the preload's queryID wins
        assert variables["bucketsToFetch"] >= 20

    def test_tray_fallback_baked_variables(self):
        session = StubSession({TRAY_QUERY: craft_tray_payload()})
        service = StoriesService(session)
        tiles = service.tray(limit=6)

        _friendly, doc_id, variables = session.graphql.calls[0]
        assert doc_id == DocIdRegistry.from_assets(ASSETS).doc_id(TRAY_QUERY)
        assert variables["bucketsToFetch"] == 6
        for key in DEFAULT_STORIES_TRAY_VARIABLES:
            assert variables[key] == DEFAULT_STORIES_TRAY_VARIABLES[key]
        assert len(tiles) == 2

    def test_parse_tray_tiles_never_raises_on_empty(self):
        assert parse_tray_tiles({"data": {}}) == []
        assert parse_tray_tiles({"data": {"me": {"unified_stories_buckets": {
            "edges": []}}}}) == []


class TestMemoriesFeed:
    """Pins the memories feed decode and the live field_type_no_match ->
    empty-feed mapping (2026-09-18 account symptom)."""

    def test_feed_decodes_cards(self):
        session = StubSession({MEMORIES_QUERY: craft_memories_payload()})
        service = MemoriesService(session)
        cards = service.feed(limit=10)

        assert len(cards) == 2
        assert cards[0]["typename"] == "Story"
        assert cards[0]["text"] == "throwback text"
        assert cards[0]["creation_time"] == 1461024000
        assert cards[0]["feedback_id"] == "ZmVlZGJhY2s6MzkzNzAy0"
        assert cards[1]["typename"] == "GoodwillCometStory"
        assert cards[1]["date_text"] == "On this day in 2016"
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == MEMORIES_QUERY
        assert doc_id == MEMORIES_DOC_ID
        assert variables == {**DEFAULT_MEMORIES_FEED_VARIABLES, "count": 10}

    def test_feed_limit_passes_count(self):
        session = StubSession({MEMORIES_QUERY: craft_memories_payload()})
        service = MemoriesService(session)
        cards = service.feed(limit=1)

        assert len(cards) == 1
        assert session.graphql.calls[0][2]["count"] == 1

    def test_parse_memories_feed_cursor(self):
        page = parse_memories_feed(craft_memories_payload())
        assert page.end_cursor == "MTc4OTczNjE4MDoxMA=="
        assert page.has_next_page is True
        assert len(page.cards) == 2
        assert page.raw_size > 0

    def test_parse_never_raises_on_empty(self):
        assert parse_memories_feed({"data": {"viewer": {"throwback": {
            "throwback_units": {"edges": []}}}}}).cards == []
        assert parse_memories_feed({}).cards == []

    def test_feed_empty_throwback_field_type_no_match_yields_empty(self):
        """Live symptom (2026-09-18): accounts with no throwback data get
        GraphQLProtocolError 'field_type_no_match' from
        CometMemoriesFeedQuery — mapped to an empty feed, not a crash."""
        err = GraphQLProtocolError(
            "CometMemoriesFeedQuery: A server error field_type_no_match "
            "occured.")
        session = StubSession({MEMORIES_QUERY: err})
        cards = MemoriesService(session).feed(limit=10)
        assert cards == []

    def test_feed_other_protocol_errors_still_raise(self):
        err = GraphQLProtocolError(
            "CometMemoriesFeedQuery: A server error "
            "missing_required_variable_value occured.")
        session = StubSession({MEMORIES_QUERY: err})
        with pytest.raises(GraphQLProtocolError):
            MemoriesService(session).feed(limit=10)


class TestRegistryPresence:
    """Pins that every doc_id the three surfaces use resolves in the real
    harvested registry."""

    def test_every_used_doc_id_is_in_the_registry(self):
        registry = DocIdRegistry.from_assets(ASSETS)
        expected = {
            LIST_QUERY: LIST_DOC_ID,
            FEED_QUERY: FEED_DOC_ID,
            DELETE_MUTATION: DELETE_DOC_ID,
            CREATE_MUTATION: CREATE_DOC_ID,
            TRAY_QUERY: TRAY_DOC_ID,
            MEMORIES_QUERY: MEMORIES_DOC_ID,
        }
        for name, doc_id in expected.items():
            assert registry.doc_id(name) == doc_id, name
        # the sibling tray refetch query is present too (ground truth)
        assert registry.doc_id("StoriesTrayRectangularQuery") == "28261665493524355"
        assert registry.doc_id("CometMemoriesSetThrowbackSettingsMutation") \
            == "9494473390636819"
        assert registry.doc_id("EventCometLightweightCreateRootQuery") \
            == "27034661236175738"
