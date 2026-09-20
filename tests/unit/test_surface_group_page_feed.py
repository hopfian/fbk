"""Offline tests for the GROUP FEED, PAGE FEED, and PRESENCE surfaces
(docs/02 §2, docs/15 §P2-2).

StubSession-based: the feed reads replay a crafted SSR preload block
through the real extract_preload_registry (canned HTML via a monkeypatched
_fetch), assert group/page id substitution into the verbatim preload
variables, and walk canned payloads into typed FeedPage rows. The presence
tests assert the bundle-decoded wire shape (empty variables, registry
doc_id). Command wiring is exercised through the real argparse parsers
with the session constructor stubbed out.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import StubSession

from commands import groups as groups_cmd
from commands import pages as pages_cmd
from commands import presence as presence_cmd
from config import Config
from graphql.registry import DocIdRegistry
from surfaces.groups import (
    DEFAULT_GROUP_FEED_VARIABLES,
    GROUP_FEED_QUERY,
    GroupsService,
    _dedupe_stories,
)
from surfaces.pages import (
    DEFAULT_PAGE_FEED_VARIABLES,
    PAGE_FEED_QUERY,
    PagesService,
)
from surfaces.presence import (
    PRESENCE_QUERY,
    PresenceService,
)

ACTOR_ID = "12345678901234"

# The live-probed wire ids (docs/15 §P2-2 ground truth, 2026-09).
GROUP_FEED_DOC_ID = "28650596084564206"      # CometGroupDiscussionRootSuccessQuery
PAGE_FEED_DOC_ID = "28117370721250101"       # ProfileCometTimelineListViewRootQuery
PAGE_TIMELINE_FEED_DOC_ID = "28736035046001218"  # ProfileCometTimelineFeedQuery
GROUP_PERMALINK_DOC_ID = "38640785042203305"  # CometSinglePostDialogContentQuery
PRESENCE_DOC_ID = "9899572666749146"          # useFBChatVisibility_PresenceStatusChatVisibilityQuery

PAIRS = {
    GROUP_FEED_QUERY: GROUP_FEED_DOC_ID,
    PAGE_FEED_QUERY: PAGE_FEED_DOC_ID,
    "ProfileCometTimelineFeedQuery": PAGE_TIMELINE_FEED_DOC_ID,
    "CometSinglePostDialogContentQuery": GROUP_PERMALINK_DOC_ID,
    PRESENCE_QUERY: PRESENCE_DOC_ID,
}

# The queries above + the ones the existing groups/pages commands replay.
for _existing in {
    "useGroupsCometCreateMutation": "37309644325300927",
    "GroupCometJoinForumMutation": "28829108476693589",
    "useGroupAddMembersMutation": "26949495548074408",
    "GroupsCometRequestToParticipateMutation": "27259944290349383",
    "AdditionalProfilePlusCreationMutation": "23863457623296585",
    "CometPageLikeCommitMutation": "9647968328590344",
    "CometPageFollowCommitMutation": "29690201327260308",
}.items():
    PAIRS.setdefault(*_existing)

STORY_ID = "UzpfSUZTOjE6LTYxMzgxMDE5OTY5MzUzMzI0MjM6ZUp3"
FB_SAMPLE = "ZmVlZGJhY2s6MTUzMjkzMDQ1ODg4MTY4Mw"


def stub(responses: dict) -> StubSession:
    return StubSession(responses, PAIRS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fbk-test")
    sub = parser.add_subparsers(dest="command", required=True)
    groups_cmd.register(sub)
    pages_cmd.register(sub)
    presence_cmd.register(sub)
    return parser


# ------------------------------------------------------------------ fixtures
def _preload_html(query_name: str, doc_id: str,
                  variables: dict) -> str:
    """A page embedding one SSR preloader registration in the exact wire
    format extract_preload_registry harvests (docs/15 §P2-2)."""
    registration = {
        "actorID": ACTOR_ID,
        "preloaderID": f"adp_{query_name}RelayPreloader_0123456789abcdef",
        "queryID": doc_id,
        "variables": variables,
        "queryName": query_name,
    }
    return ('<html><script type="application/json">'
            + json.dumps(registration, separators=(",", ":"))
            + '</script></html>')


def _group_page_html(preload_group_id: str = "888777") -> str:
    return _preload_html(GROUP_FEED_QUERY, GROUP_FEED_DOC_ID, {
        "groupID": preload_group_id,
        "feedLocation": "GROUP",
        "feedType": "DISCUSSION",
        "renderLocation": "group",
        "scale": 2,
    })


def _page_html(preload_user_id: str = "12345678901234568") -> str:
    return _preload_html(PAGE_FEED_QUERY, PAGE_FEED_DOC_ID, {
        "userID": preload_user_id,
        "renderLocation": "timeline",
        "scale": 2,
    })


def _story_node(text: str, story_id: str = STORY_ID,
                actor: str = "Person A",
                feedback_id: str = FB_SAMPLE,
                permalink: str = "https://www.facebook.com/story.php") -> dict:
    return {
        "__typename": "Story",
        "id": story_id,
        "creation_time": 1789712173,
        "actors": [{"__typename": "User", "id": "12345678901234568",
                    "name": actor}],
        "feedback": {"__typename": "Feedback", "id": feedback_id,
                     "reaction_count": {"count": 5},
                     "comment_count": {"count": 1}},
        "comet_sections": {"content": {"story": {"message": {"text": {
            "__typename": "TextWithEntities", "text": text}}}}},
        "permalink_url": permalink,
    }


GROUP_FEED_PAYLOAD = {
    "data": {
        "node": {"__typename": "Group", "id": "990011", "feed_units": {
            "edges": [
                {"node": _story_node("group post one")},
                {"node": _story_node("group post two",
                                     story_id=STORY_ID + "K",
                                     actor="Another Member")},
            ],
            "page_info": {"__typename": "PageInfo",
                          "end_cursor": "GROUPCURSOR",
                          "has_next_page": True},
        }},
    },
}


def _highlight_unit(unit_id: str, text: str, url: str,
                    feedback_id: str) -> dict:
    """One HighlightPostUnit node — the page-timeline story spelling
    (live-probed 2026-09: the nested story carries feedback/message/url)."""
    return {
        "__typename": "HighlightPostUnit",
        "id": unit_id,
        "type": "POST",
        "if_has_active_content_for_viewer": {
            "__typename": "HighlightPostUnit",
            "story": {
                "id": unit_id,
                "url": url,
                "actors": [{"__typename": "User", "id": "12345678901234568",
                            "name": "Sample Page"}],
                "feedback": {"__typename": "Feedback", "id": feedback_id,
                             "reaction_count": {"count": 5},
                             "comment_count": {"count": 2}},
                "comet_sections": {"content": {"story": {"message": {
                    "text": {"__typename": "TextWithEntities",
                             "text": text}}}}},
            },
        },
    }


PAGE_FEED_PAYLOAD = {
    "data": {
        "user": {"id": "12345678901234568", "timeline_list": {
            "edges": [
                {"node": _highlight_unit(
                    "1532996765541719", "page post one",
                    "https://www.facebook.com/reel/1856023965378356/",
                    "ZmVlZGJhY2s6MTUzMjk5Njc2NTU0MTcxOQ==")},
                {"node": _highlight_unit(
                    "1532311112276951", "page post two",
                    "https://www.facebook.com/reel/904876085813708/",
                    "ZmVlZGJhY2s6MTUzMjMxMTExMjc2NzU5MQ==")},
            ],
            "page_info": {"__typename": "PageInfo",
                          "end_cursor": "PAGECURSOR",
                          "has_next_page": True},
        }},
    },
}

PRESENCE_PAYLOAD = {"data": {"viewer": {"chat_visibility": True}}}


def _last_call(session: StubSession) -> tuple[str, str, dict]:
    assert session.graphql.calls, "service made no client call"
    return session.graphql.calls[-1]


# ------------------------------------------------------------------ registry
class TestRegistryPresence:
    """Pins every ground-truth doc_id in the real asset registry."""

    def test_every_new_doc_id_is_harvested(self):
        """Every ground-truth doc_id resolves in the real asset registry."""
        registry = DocIdRegistry.from_assets(Config.discover().assets_dir)
        for name, doc_id in {
            GROUP_FEED_QUERY: GROUP_FEED_DOC_ID,
            PAGE_FEED_QUERY: PAGE_FEED_DOC_ID,
            "ProfileCometTimelineFeedQuery": PAGE_TIMELINE_FEED_DOC_ID,
            "CometSinglePostDialogContentQuery": GROUP_PERMALINK_DOC_ID,
            PRESENCE_QUERY: PRESENCE_DOC_ID,
        }.items():
            assert registry.doc_id(name) == doc_id


# ------------------------------------------------------------- groups feed
class TestGroupsFeedRead:
    """Pins the group feed read: preload groupID substitution, typed rows,
    and the stub/duplicate-echo filter."""

    def test_feed_read_replays_page_preload_with_group_substituted(self):
        session = stub({GROUP_FEED_QUERY: GROUP_FEED_PAYLOAD})
        service = GroupsService(session)
        fetched: list[str] = []
        service._fetch = lambda url: (fetched.append(url)
                                      or _group_page_html("888777"))
        page = service.feed_read("990011")

        # the group page was fetched at the group's root URL
        assert fetched == ["https://www.facebook.com/groups/990011/"]
        friendly, doc_id, variables = _last_call(session)
        assert friendly == GROUP_FEED_QUERY
        assert doc_id == GROUP_FEED_DOC_ID
        # the requested group id replaces the preload's groupID (probe:
        # the group id rides ONLY as the top-level groupID variable)
        assert variables["groupID"] == "990011"
        assert variables["feedLocation"] == "GROUP"
        assert variables["feedType"] == "DISCUSSION"
        assert variables["renderLocation"] == "group"
        # verbatim PRELOAD variables (not the baked template: the canned
        # registration carries no sortingSetting key)
        assert "sortingSetting" not in variables

        # typed FeedPage with 2 walked stories
        assert len(page.stories) == 2
        first = page.stories[0]
        assert first.actor is not None and first.actor.name == "Person A"
        assert first.text == "group post one"
        assert first.permalink == "https://www.facebook.com/story.php"
        assert first.key is not None and first.key.raw.startswith("Uzpf")
        assert first.creation_time == 1789712173
        assert first.feedback is not None
        assert first.feedback.reaction_count == 5
        assert first.feedback.comment_count == 1
        assert str(first.feedback.id) == FB_SAMPLE
        assert page.end_cursor == "GROUPCURSOR"
        assert page.has_next_page is True
        assert page.raw_size > 100

    def test_feed_read_merges_cursor(self):
        session = stub({GROUP_FEED_QUERY: GROUP_FEED_PAYLOAD})
        service = GroupsService(session)
        service._fetch = lambda url: _group_page_html()
        service.feed_read("990011", cursor="XYZCURSOR")
        _, _, variables = _last_call(session)
        assert variables["cursor"] == "XYZCURSOR"
        assert variables["groupID"] == "990011"

    def test_feed_read_limit_slices_stories(self):
        session = stub({GROUP_FEED_QUERY: GROUP_FEED_PAYLOAD})
        service = GroupsService(session)
        service._fetch = lambda url: _group_page_html()
        page = service.feed_read("990011", limit=1)
        assert len(page.stories) == 1
        assert page.stories[0].text == "group post one"
        # the wire variables are never edited for the local limit
        _, _, variables = _last_call(session)
        assert "limit" not in variables

    def test_feed_read_falls_back_to_baked_template(self):
        session = stub({GROUP_FEED_QUERY: GROUP_FEED_PAYLOAD})
        service = GroupsService(session)

        def boom(url: str) -> str:
            raise RuntimeError("network down")

        service._fetch = boom
        page = service.feed_read("990011")

        friendly, doc_id, variables = _last_call(session)
        assert friendly == GROUP_FEED_QUERY
        assert doc_id == GROUP_FEED_DOC_ID
        # the baked live-probed template, group id substituted
        assert variables["groupID"] == "990011"
        assert variables["feedLocation"] == "GROUP"
        assert variables["sortingSetting"] == "TOP_POSTS"
        assert variables["regular_stories_stream_initial_count"] == 1
        assert variables["__relay_internal__pv__GroupsCometGYSJFeedItemHeightrelayprovider"] == 206
        assert len(page.stories) == 2

    def test_baked_template_has_one_parameterized_field(self):
        placeholders = [v for v in DEFAULT_GROUP_FEED_VARIABLES.values()
                       if isinstance(v, str)
                       and v.startswith("{") and v.endswith("}")]
        assert placeholders == ["{group_id}"]

    def test_feed_read_drops_stub_duplicate_story(self):
        """Live symptom (2026-09): the group walker emits an empty second
        Story — actor/text/feedback all None, only a permalink — echoing
        the SAME key/id as the real story (attachment sub-node). The
        shared stub/duplicate filter drops it, the real story survives."""
        real = _story_node("real group post")
        # the stub hangs inside the real story as an attachment sub-node,
        # exactly like the live payload
        real["attachments"] = [{"styles": {"attachment": {"media": {
            "__typename": "Story", "id": STORY_ID,
            "permalink_url": "https://www.facebook.com/story.php"}}}}]
        payload = {"data": {"node": {"feed_units": {"edges": [
            {"node": real},
            {"node": _story_node("group post two",
                                 story_id=STORY_ID + "K",
                                 actor="Another Member")}],
            "page_info": {"end_cursor": "GROUPCURSOR",
                          "has_next_page": True}}}}}
        session = stub({GROUP_FEED_QUERY: payload})
        service = GroupsService(session)
        service._fetch = lambda url: _group_page_html()
        page = service.feed_read("990011")

        assert len(page.stories) == 2  # 3 walked -> the stub echo dropped
        kept = page.stories[0]
        assert kept.text == "real group post"
        assert kept.actor is not None and kept.actor.name == "Person A"
        assert str(kept.feedback.id) == FB_SAMPLE

    def test_dedupe_stories_rule_set(self):
        """Direct rule-set check on the shared helper (Story.model_dump
        row shape): stub duplicates and full identity duplicates drop;
        a distinct-key permalink stub survives; a contentless stub never
        does."""
        full = {"id": "S1", "key": {"raw": "UzpfS1"},
                "actor": {"id": "1", "name": "A"}, "text": "hello",
                "feedback": {"id": {"raw": "F1"}, "reaction_count": 1,
                             "comment_count": 0}, "permalink": "p1",
                "creation_time": 1789712173}
        rows = [
            dict(full),
            # stub echoing S1's key: same id/key, no content -> dropped
            {"id": "S1", "key": {"raw": "UzpfS1"}, "actor": None,
             "text": None, "feedback": None, "permalink": "p1",
             "creation_time": None},
            # a distinct-key stub WITH a permalink survives
            {"id": "S2", "key": None, "actor": None, "text": None,
             "feedback": None, "permalink": "p2", "creation_time": None},
            # a contentless stub (no permalink) is dropped even with a key
            {"id": "S3", "key": None, "actor": None, "text": None,
             "feedback": None, "permalink": None, "creation_time": None},
            # a full duplicate of S1's (key, feedback) identity -> dropped
            dict(full),
        ]
        kept = _dedupe_stories(rows)
        assert [row["id"] for row in kept] == ["S1", "S2"]
        assert kept[0]["text"] == "hello"


# ------------------------------------------------------------- groups post
class TestGroupsPost:
    """Pins the group post command's delegation to feed publish with the
    group context threaded through."""

    def _run_post(self, monkeypatch, session, argv) -> tuple[int, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        monkeypatch.setattr(groups_cmd, "new_session", lambda _a: session)
        code = args.fn(args)
        return code

    def test_post_delegates_to_feed_publish_with_group_context(self,
                                                               monkeypatch):
        session = stub({"ComposerStoryCreateMutation":
                        {"data": {"composer_story_create": {"story_id": 42}}}})
        code = self._run_post(
            monkeypatch, session,
            ["groups", "post", "--group-id", "990011",
             "--text", "hello group", "--privacy", "public"])
        assert code == 0
        friendly, _doc_id, variables = _last_call(session)
        assert friendly == "ComposerStoryCreateMutation"
        # group context per the live group-feed preload locations
        assert variables["groupID"] == "990011"
        assert variables["feedLocation"] == "GROUP"
        assert variables["renderLocation"] == "group"
        assert variables["input"]["message"]["text"] == "hello group"
        assert variables["input"]["audience"]["privacy"]["base_state"] == "EVERYONE"

    def test_post_privacy_private_maps_to_self(self, monkeypatch):
        session = stub({"ComposerStoryCreateMutation": {"data": {}}})
        self._run_post(
            monkeypatch, session,
            ["groups", "post", "--group-id", "990011",
             "--text", "secret post", "--privacy", "private"])
        _, _, variables = _last_call(session)
        assert variables["input"]["audience"]["privacy"]["base_state"] == "SELF"
        assert variables["groupID"] == "990011"


# -------------------------------------------------------------- pages feed
class TestPagesFeedRead:
    """Pins the page feed read: vanity preload replay, userID
    substitution, and the stub-echo dedup."""

    def test_feed_read_replays_page_preload_typed_rows(self):
        session = stub({PAGE_FEED_QUERY: PAGE_FEED_PAYLOAD})
        service = PagesService(session)
        fetched: list[str] = []
        service._fetch = lambda url: (fetched.append(url)
                                      or _page_html("12345678901234568"))
        page = service.feed_read("sample.page")

        # vanity slug builds the page URL directly
        assert fetched == ["https://www.facebook.com/sample.page"]
        friendly, doc_id, variables = _last_call(session)
        assert friendly == PAGE_FEED_QUERY
        assert doc_id == PAGE_FEED_DOC_ID
        # the page's own preload userID stands (vanity arg, verbatim replay)
        assert variables["userID"] == "12345678901234568"
        assert variables["renderLocation"] == "timeline"

        # typed rows walked out of the HighlightPostUnit nodes
        assert len(page.stories) == 2
        first = page.stories[0]
        assert first.id == "1532996765541719"
        assert first.actor is not None and first.actor.name == \
            "Sample Page"
        assert first.text == "page post one"
        assert first.permalink == "https://www.facebook.com/reel/1856023965378356/"
        assert first.feedback is not None
        assert first.feedback.reaction_count == 5
        assert first.feedback.comment_count == 2
        assert page.end_cursor == "PAGECURSOR"
        assert page.has_next_page is True

    def test_feed_read_numeric_id_substitutes_user_id(self):
        session = stub({PAGE_FEED_QUERY: PAGE_FEED_PAYLOAD})
        service = PagesService(session)
        service._fetch = lambda url: _page_html("000000000")
        service.feed_read("12345678901234568")
        _, _, variables = _last_call(session)
        assert variables["userID"] == "12345678901234568"

    def test_feed_read_limit_slices_rows(self):
        session = stub({PAGE_FEED_QUERY: PAGE_FEED_PAYLOAD})
        service = PagesService(session)
        service._fetch = lambda url: _page_html()
        page = service.feed_read("sample.page", limit=1)
        assert len(page.stories) == 1
        assert page.stories[0].text == "page post one"

    def test_feed_read_falls_back_to_baked_template(self):
        session = stub({PAGE_FEED_QUERY: PAGE_FEED_PAYLOAD})
        service = PagesService(session)

        def boom(url: str) -> str:
            raise RuntimeError("network down")

        service._fetch = boom
        page = service.feed_read("12345678901234568")

        friendly, doc_id, variables = _last_call(session)
        assert friendly == PAGE_FEED_QUERY
        assert doc_id == PAGE_FEED_DOC_ID
        assert variables["userID"] == "12345678901234568"
        assert variables["renderLocation"] == "timeline"
        assert len(page.stories) == 2

    def test_feed_read_vanity_without_preload_is_typed_error(self):
        session = stub({PAGE_FEED_QUERY: PAGE_FEED_PAYLOAD})
        service = PagesService(session)

        def boom(url: str) -> str:
            raise RuntimeError("network down")

        service._fetch = boom
        with pytest.raises(ValueError):
            service.feed_read("sample.page")

    def test_feed_read_drops_stub_duplicate_story(self):
        """Live symptom (2026-09): the page walker emits an empty second
        Story (actor/text/feedback all None, only a permalink) echoing
        the real unit's id — the shared filter drops it."""
        unit = _highlight_unit(
            "1532996765541719", "page post one",
            "https://www.facebook.com/reel/1856023965378356/",
            "ZmVlZGJhY2s6MTUzMjk5Njc2NTU0MTcxOQ==")
        stub_story = {"__typename": "Story", "id": "1532996765541719",
                      "permalink_url":
                          "https://www.facebook.com/reel/1856023965378356/"}
        payload = {"data": {"user": {"timeline_list": {"edges": [
            {"node": unit}, {"node": stub_story}],
            "page_info": {"end_cursor": "PAGECURSOR",
                          "has_next_page": False}}}}}
        session = stub({PAGE_FEED_QUERY: payload})
        service = PagesService(session)
        service._fetch = lambda url: _page_html()
        page = service.feed_read("sample.page")

        assert len(page.stories) == 1  # 2 walked -> the stub echo dropped
        kept = page.stories[0]
        assert kept.actor is not None and kept.actor.name == \
            "Sample Page"
        assert kept.text == "page post one"
        assert str(kept.feedback.id) == "ZmVlZGJhY2s6MTUzMjk5Njc2NTU0MTcxOQ=="

    def test_baked_template_has_one_parameterized_field(self):
        placeholders = [v for v in DEFAULT_PAGE_FEED_VARIABLES.values()
                        if isinstance(v, str)
                        and v.startswith("{") and v.endswith("}")]
        assert placeholders == ["{page_id}"]


# ---------------------------------------------------------------- presence
class TestPresenceService:
    """Pins the presence query: empty decoded variables, registry
    doc_id, and trim-to-shape."""

    def test_status_replays_empty_variables_and_trims(self):
        session = stub({PRESENCE_QUERY: dict(PRESENCE_PAYLOAD)})
        status = PresenceService(session).status()
        assert status == {"chat_visibility": True}
        friendly, doc_id, variables = _last_call(session)
        assert friendly == PRESENCE_QUERY
        assert doc_id == PRESENCE_DOC_ID
        # bundle-decoded 2026-09: argumentDefinitions are empty
        assert variables == {}

    def test_status_offline_visibility(self):
        session = stub({PRESENCE_QUERY:
                        {"data": {"viewer": {"chat_visibility": False}}}})
        assert PresenceService(session).status() == {"chat_visibility": False}

    def test_status_missing_viewer_is_none(self):
        session = stub({PRESENCE_QUERY: {"data": {"viewer": None}}})
        assert PresenceService(session).status() == {"chat_visibility": None}


# ------------------------------------------------------------ command wiring
class TestCommands:
    """Pins the groups/pages/presence argparse wiring with the session
    constructor stubbed out."""

    def _run(self, monkeypatch, capsys, session, argv,
             *, fetch: str | None = None) -> tuple[int, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        monkeypatch.setattr(groups_cmd, "new_session", lambda _a: session)
        monkeypatch.setattr(pages_cmd, "new_session", lambda _a: session)
        monkeypatch.setattr(presence_cmd, "new_session", lambda _a: session)
        if fetch is not None:
            monkeypatch.setattr(GroupsService, "_fetch",
                                lambda self, url: fetch)
            monkeypatch.setattr(PagesService, "_fetch",
                                lambda self, url: fetch)
        code = args.fn(args)
        captured = capsys.readouterr().out
        return code, captured

    def test_groups_feed_command(self, monkeypatch, capsys):
        session = stub({GROUP_FEED_QUERY: GROUP_FEED_PAYLOAD})
        code, out = self._run(
            monkeypatch, capsys, session,
            ["groups", "feed", "--group-id", "990011", "--limit", "1"],
            fetch=_group_page_html("888777"))
        assert code == 0
        assert "Person A | group post one" in out
        assert "-- 1 stories" in out
        _, doc_id, variables = _last_call(session)
        assert doc_id == GROUP_FEED_DOC_ID
        assert variables["groupID"] == "990011"

    def test_groups_post_command(self, monkeypatch, capsys):
        session = stub({"ComposerStoryCreateMutation":
                        {"data": {"composer_story_create": {"story_id": 7}}}})
        code, out = self._run(
            monkeypatch, capsys, session,
            ["groups", "post", "--group-id", "990011",
             "--text", "cli group post", "--privacy", "public"])
        assert code == 0
        assert "posted to group 990011" in out
        _, _, variables = _last_call(session)
        assert variables["groupID"] == "990011"
        assert variables["feedLocation"] == "GROUP"
        assert variables["renderLocation"] == "group"

    def test_pages_feed_command(self, monkeypatch, capsys):
        session = stub({PAGE_FEED_QUERY: PAGE_FEED_PAYLOAD})
        code, out = self._run(
            monkeypatch, capsys, session,
            ["pages", "feed", "--page-id", "sample.page"],
            fetch=_page_html("12345678901234568"))
        assert code == 0
        assert "Sample Page | page post one" in out
        _, doc_id, variables = _last_call(session)
        assert doc_id == PAGE_FEED_DOC_ID
        assert variables["userID"] == "12345678901234568"

    def test_presence_status_command(self, monkeypatch, capsys):
        session = stub({PRESENCE_QUERY: dict(PRESENCE_PAYLOAD)})
        code, out = self._run(
            monkeypatch, capsys, session,
            ["presence", "status"])
        assert code == 0
        assert "chat visibility: online (visible)" in out
        assert '"chat_visibility": true' in out

    def test_presence_status_command_offline(self, monkeypatch, capsys):
        session = stub({PRESENCE_QUERY:
                        {"data": {"viewer": {"chat_visibility": False}}}})
        code, out = self._run(
            monkeypatch, capsys, session,
            ["presence", "status"])
        assert code == 0
        assert "chat visibility: offline" in out
