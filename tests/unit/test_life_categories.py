"""Life-event category listing tests — offline, against the synthetic
calibrated-shape response.

The query itself is LIVE-CALIBRATED (2026-09-20, docs/15 §P3 composer
ground truth): CometComposerLifeEventCategoryListQuery, doc_id
32116841221248541, variables {"scale": 1}, answered with the category
tree at data.viewer.life_event_categories.nodes. The payload below
fixtures that exact shape (the calibration finding: categories like
{id: "WORK", name: "Work", icon_id, life_event_types: {nodes: [{id,
life_event_type_identifier: "STARTED_JOB", ...}]}}). The publish-side
input for life events is UNKNOWN — nothing here invents one.
"""
from __future__ import annotations

from typing import Any

import pytest
from fakes import StubSession

from app import build_parser
from surfaces.feed import FeedService

LIFE_CATEGORIES_DOC_ID = "32116841221248541"
LIFE_CATEGORIES_QUERY = "CometComposerLifeEventCategoryListQuery"

# The live-calibrated response shape (2026-09-20 finding), fixtured:
# one category whose first event type carries a name and whose second
# does not (the name is optional in the calibration — "name if present").
LIFE_CATEGORIES_PAYLOAD: dict[str, Any] = {
    "data": {
        "viewer": {
            "life_event_categories": {
                "nodes": [
                    {
                        "id": "WORK",
                        "name": "Work",
                        "icon_id": "6182223",
                        "life_event_types": {
                            "nodes": [
                                {"id": "103489852356",
                                 "life_event_type_identifier": "STARTED_JOB",
                                 "name": "Started a new job"},
                                {"id": "103489852357",
                                 "life_event_type_identifier": "LEFT_JOB"},
                            ],
                        },
                    },
                    {
                        "id": "FAMILY",
                        "name": "Family",
                        "life_event_types": {"nodes": []},
                    },
                ],
            },
        },
    },
}


def _last_call(stub: StubSession) -> tuple[str, str, dict]:
    assert stub.graphql.calls, "service made no client call"
    return stub.graphql.calls[-1]


class TestLifeEventCategories:
    """Pins the calibrated query replay and the typed row parse."""

    def test_replays_the_calibrated_query_and_parses_the_tree(self):
        stub = StubSession(responses={
            LIFE_CATEGORIES_QUERY: LIFE_CATEGORIES_PAYLOAD})
        categories = FeedService(stub).life_event_categories()

        friendly, doc_id, variables = _last_call(stub)
        assert friendly == LIFE_CATEGORIES_QUERY
        assert doc_id == LIFE_CATEGORIES_DOC_ID
        # the live-calibrated variables, exactly {"scale": 1}
        assert variables == {"scale": 1}

        assert categories == [
            {"id": "WORK", "name": "Work", "icon_id": "6182223",
             "types": [
                 {"id": "103489852356", "identifier": "STARTED_JOB",
                  "name": "Started a new job"},
                 {"id": "103489852357", "identifier": "LEFT_JOB",
                  "name": None},
             ]},
            {"id": "FAMILY", "name": "Family", "icon_id": None,
             "types": []},
        ]

    def test_absent_viewer_yields_an_empty_list(self):
        stub = StubSession(responses={LIFE_CATEGORIES_QUERY: {}})
        assert FeedService(stub).life_event_categories() == []
        assert _last_call(stub)[2] == {"scale": 1}


class TestFeedLifeCategoriesWiring:
    """Pins the `feed life-categories` CLI wiring on the real app
    parser (offline: parse-only, no session ever built)."""

    def test_life_categories_parses(self):
        args = build_parser().parse_args(["feed", "life-categories"])
        assert args.feed_command == "life-categories"
        assert callable(args.fn)

    def test_life_categories_accepts_the_common_flag_set(self):
        args = build_parser().parse_args(
            ["feed", "life-categories", "--json"])
        assert args.as_json is True


class TestPublishFlagWiring:
    """Pins the publish enrichment flags on the real app parser: dests,
    repeatable --tag pairs, and the malformed-pair usage error."""

    def test_publish_flags_parse_to_their_dests(self):
        args = build_parser().parse_args([
            "feed", "publish", "--text", "hi", "--privacy", "friends",
            "--tag", "100:Jane", "--tag", "200:Bob",
            "--feeling", "123", "--place", "999",
            "--ai-label", "on", "--background", "0"])
        assert args.tags == [("100", "Jane"), ("200", "Bob")]
        assert args.feeling == "123"
        assert args.activity is None
        assert args.place_id == "999"
        assert args.ai_label == "on"
        assert args.background == "0"

    def test_publish_flag_defaults_are_absent(self):
        args = build_parser().parse_args(
            ["feed", "publish", "--text", "hi", "--privacy", "public"])
        assert args.tags is None
        assert args.feeling is None
        assert args.activity is None
        assert args.place_id is None
        assert args.ai_label is None
        assert args.background is None

    def test_publish_tag_display_name_may_contain_colons(self):
        args = build_parser().parse_args(
            ["feed", "publish", "--text", "hi", "--privacy", "public",
             "--tag", "615937:Jane:Doe"])
        assert args.tags == [("615937", "Jane:Doe")]

    def test_publish_rejects_a_malformed_tag_pair(self):
        with pytest.raises(SystemExit) as ei:
            build_parser().parse_args(
                ["feed", "publish", "--text", "x", "--privacy", "public",
                 "--tag", "nocolon"])
        assert ei.value.code == 2

    def test_publish_rejects_an_unknown_ai_label(self):
        with pytest.raises(SystemExit) as ei:
            build_parser().parse_args(
                ["feed", "publish", "--text", "x", "--privacy", "public",
                 "--ai-label", "maybe"])
        assert ei.value.code == 2
