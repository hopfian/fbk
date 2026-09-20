"""FEED surface service tests — offline, against the REAL captured assets.

Every mutation test replays the actual browser captures in assets/
(docs/15 §P2-3, §P3-3) through StubSession, asserting that FeedService
substitutes exactly the target fields and preserves the verbatim tracking
blobs. Feed reads parse the real live-captured payload (feed_page1_sample,
docs/15 §P2-2) with the walker from surfaces.feed (docs/04 §5-§7).
"""
from __future__ import annotations

import base64
import uuid
from typing import Any

import pytest
from fakes import StubSession

from auth.bootstrap import PreloadEntry
from domain.common import CommentID, FeedbackID, Privacy, ReactionType
from surfaces.feed import (
    DEFAULT_FEED_VARIABLES,
    FEED_QUERY,
    FeedService,
)

# The real live-captured wire ids (docs/15 ground truth).
FEED_DOC_ID = "28044109855291494"
PAGINATION_DOC_ID = "28827638856853689"
REACT_DOC_ID = "27646120298312844"
CREATE_COMMENT_DOC_ID = "39607465588840384"  # rotated with revision 1047963790
DELETE_COMMENT_DOC_ID = "28058620387108821"
COMPOSER_DOC_ID = "28778531428503134"
TYPING_START_DOC_ID = "9815271091886179"
TYPING_STOP_DOC_ID = "9972315006159780"

STUB_USER_ID = "12345678901234"

# Feedback id from the real capture: feedback:3937024329774036 (unpadded b64).
FB_SAMPLE = "ZmVlZGJhY2s6MzkzNzAyNDMyOTc3NDAzNg"

# A minimal but shape-faithful page-2 payload: one Story node with every
# walker field (docs/04 §5 page_info, §6 message, §7 identifiers).
PAGE2_PAYLOAD = {
    "data": {
        "node": {
            "__typename": "Story",
            "id": "UzpfSUZTOjE6LTYxMzgxMDE5OTY5MzUzMzI0MjM6ZUp3",
            "creation_time": 1789712173,
            "actors": [{"__typename": "User", "id": "12345678901234568",
                        "name": "Sample Page"}],
            "feedback": {"id": "ZmVlZGJhY2s6MTUzMjkzMDQ1ODg4MTY4Mw"},
            "comet_sections": {"content": {"story": {"message": {"text": {
                "__typename": "TextWithEntities",
                "text": "page two headline"}}}}},
            "permalink_url": "https://www.facebook.com/story.php",
        },
        "page_info": {"__typename": "PageInfo", "end_cursor": "PAGE2CURSOR",
                      "has_next_page": True},
    }
}


def _last_call(stub: StubSession) -> tuple[str, str, dict]:
    assert stub.graphql.calls, "service made no client call"
    return stub.graphql.calls[-1]


class TestRead:
    """Pins the feed read against the real captured page-1 payload, the
    verbatim preload precedence, and the baked live-probed defaults."""

    def test_read_real_fixture(self, feed_page1):
        stub = StubSession(responses={FEED_QUERY: feed_page1})
        page = FeedService(stub).read()

        assert len(page.stories) >= 1
        assert page.stories[0].feedback is not None
        assert page.end_cursor is not None
        assert page.has_next_page is True
        assert page.raw_size > 100_000  # the live capture was 923KB

        friendly, doc_id, variables = _last_call(stub)
        assert friendly == FEED_QUERY
        assert doc_id == FEED_DOC_ID
        # StubSession has empty preloads -> the baked live-probed defaults
        assert variables["feedLocation"] == "NEWSFEED"
        assert variables["refreshMode"] == "COLD_START"

    def test_read_default_variables_are_live_shape(self):
        assert DEFAULT_FEED_VARIABLES["feedLocation"] == "NEWSFEED"
        assert DEFAULT_FEED_VARIABLES["feedbackSource"] == 1
        assert DEFAULT_FEED_VARIABLES["scale"] == 2

    def test_read_merges_overrides(self, feed_page1):
        stub = StubSession(responses={FEED_QUERY: feed_page1})
        FeedService(stub).read(variables={"feedInitialFetchSize": 9})
        _, _, variables = _last_call(stub)
        assert variables["feedInitialFetchSize"] == 9
        assert variables["feedLocation"] == "NEWSFEED"  # base preserved

    def test_read_prefers_bootstrap_preload(self, feed_page1):
        stub = StubSession(responses={FEED_QUERY: feed_page1})
        stub._bootstrap.preloads.append(PreloadEntry(
            actor_id=STUB_USER_ID,
            preloader_id="adp_CometModernHomeFeedQueryRelayPreloader_deadbeef",
            doc_id=FEED_DOC_ID, query_name=FEED_QUERY,
            variables={"feedLocation": "NEWSFEED", "sentinel": "preload"}))
        FeedService(stub).read()
        _, doc_id, variables = _last_call(stub)
        # verbatim preload variables win, not merged with the baked defaults
        assert variables["sentinel"] == "preload"
        assert "refreshMode" not in variables
        assert doc_id == FEED_DOC_ID

    def test_read_empty_feed_never_raises(self):
        stub = StubSession(responses={FEED_QUERY: {}})
        page = FeedService(stub).read()
        assert page.stories == []
        assert page.end_cursor is None
        assert page.has_next_page is False
        assert page.raw_size == len("{}")


class TestPaginate:
    """Pins the pagination replay: cursor passthrough and typed page decode."""

    def test_paginate_sends_cursor_and_parses_page(self):
        stub = StubSession(
            responses={"CometNewsFeedPaginationQuery": PAGE2_PAYLOAD})
        page = FeedService(stub).paginate("PAGE1CURSOR")

        friendly, doc_id, variables = _last_call(stub)
        assert friendly == "CometNewsFeedPaginationQuery"
        assert doc_id == PAGINATION_DOC_ID
        assert variables["cursor"] == "PAGE1CURSOR"
        assert variables["feedLocation"] == "NEWSFEED"

        assert len(page.stories) == 1
        story = page.stories[0]
        assert story.actor is not None and story.actor.name == "Sample Page"
        assert story.text == "page two headline"
        assert story.permalink == "https://www.facebook.com/story.php"
        assert story.key is not None and story.key.raw.startswith("Uzpf")
        assert story.creation_time == 1789712173
        assert story.feedback is not None
        assert str(story.feedback.id) == "ZmVlZGJhY2s6MTUzMjkzMDQ1ODg4MTY4Mw"
        assert page.end_cursor == "PAGE2CURSOR"
        assert page.has_next_page is True


class TestReact:
    """Pins reaction substitution over the captured LIKE template: id
    normalization, referrer override, and template-cache integrity."""

    def test_react_love_replays_captured_template(self, captured_react_mutations):
        stub = StubSession(responses={
            "CometUFIFeedbackReactMutation":
                {"data": {"feedback": {"reaction_count": 7}}}})
        response = FeedService(stub).react(FB_SAMPLE, ReactionType.LOVE)

        assert response["data"]["feedback"]["reaction_count"] == 7
        friendly, doc_id, variables = _last_call(stub)
        assert friendly == "CometUFIFeedbackReactMutation"
        assert doc_id == REACT_DOC_ID
        # the b64 id is passed through verbatim
        assert variables["input"]["feedback_id"] == FB_SAMPLE
        assert variables["input"]["feedback_reaction_id"] == "1678524932434102"
        # tracking blobs replay verbatim from the captured LIKE mutation
        like_capture = captured_react_mutations[0]["variables"]
        assert variables["input"]["tracking"] == like_capture["input"]["tracking"]
        assert len(variables["input"]["tracking"]) > 0
        # no override -> the captured referrer is kept
        assert variables["input"]["feedback_referrer"] == \
            like_capture["input"]["feedback_referrer"]

    def test_react_normalizes_and_overrides_referrer(self, captured_react_mutations):
        stub = StubSession(responses={
            "CometUFIFeedbackReactMutation": {"data": {}}})
        svc = FeedService(stub)
        svc.react("feedback:3937024329774036", ReactionType.LIKE,
                  feedback_referrer="/fbk")
        svc.react(FB_SAMPLE, ReactionType.SUPPORT)
        first, second = stub.graphql.calls[0][2], stub.graphql.calls[1][2]

        # 'feedback:<numeric>' decoded form -> wire b64
        assert first["input"]["feedback_id"] == \
            FeedbackID.from_numeric("3937024329774036").raw
        assert first["input"]["feedback_referrer"] == "/fbk"
        assert first["input"]["feedback_reaction_id"] == "1635855486666999"
        # the cached capture is not contaminated by the first substitution
        assert second["input"]["feedback_id"] == FB_SAMPLE
        assert second["input"]["feedback_referrer"] != "/fbk"
        assert second["input"]["tracking"] == \
            captured_react_mutations[0]["variables"]["input"]["tracking"]


class TestUnreact:
    """Pins the REMOVE capture as the unreact template (reaction id 0,
    padded b64 passthrough)."""

    def test_unreact_uses_remove_capture(self, captured_react_mutations):
        stub = StubSession(responses={
            "CometUFIFeedbackReactMutation": {"data": {}}})
        FeedService(stub).unreact(FB_SAMPLE + "==")

        friendly, doc_id, variables = _last_call(stub)
        assert friendly == "CometUFIFeedbackReactMutation"
        assert doc_id == REACT_DOC_ID
        assert variables["input"]["feedback_reaction_id"] == "0"
        # the REMOVE capture (mutations[1]) is the template
        remove_capture = captured_react_mutations[1]["variables"]
        assert variables["input"]["client_mutation_id"] == \
            remove_capture["input"]["client_mutation_id"]
        assert variables["input"]["tracking"] == remove_capture["input"]["tracking"]
        # padded b64 passes through untouched
        assert variables["input"]["feedback_id"] == FB_SAMPLE + "=="


class TestComment:
    """Pins comment creation: fresh uuid4 client_mutation_id, decoded
    feedback-id normalization, and captured groupID retention."""

    def test_comment_substitutes_and_normalizes(self):
        stub = StubSession(responses={
            "useCometUFICreateCommentMutation":
                {"data": {"feedback": {"comment_count": 1}}}})
        svc = FeedService(stub)
        svc.comment("feedback:3937341566408979", "hello world",
                    group_id="123")

        friendly, doc_id, variables = _last_call(stub)
        assert friendly == "useCometUFICreateCommentMutation"
        assert doc_id == CREATE_COMMENT_DOC_ID
        assert variables["input"]["message"] == {"ranges": [], "text": "hello world"}
        assert variables["groupID"] == "123"
        # decoded 'feedback:<numeric>' form -> wire b64
        assert variables["input"]["feedback_id"] == \
            FeedbackID.from_numeric("3937341566408979").raw
        # fresh client_mutation_id (uuid shape), never the captured "1"
        cmid = variables["input"]["client_mutation_id"]
        assert cmid != "1"
        assert uuid.UUID(cmid).version == 4

    def test_comment_keeps_captured_group_and_fresh_ids(self):
        stub = StubSession(responses={
            "useCometUFICreateCommentMutation": {"data": {}}})
        svc = FeedService(stub)
        svc.comment(FB_SAMPLE, "first")
        svc.comment(FB_SAMPLE, "second")

        first, second = stub.graphql.calls[0][2], stub.graphql.calls[1][2]
        # no override -> groupID is None, NEVER the captured group id: the
        # capture is a GROUP-context comment and replaying its group id
        # against a non-group feedback is field_exception 1357010 (live
        # finding 2026-09-20)
        assert first["groupID"] is None
        assert first["input"]["feedback_id"] == FB_SAMPLE
        assert first["input"]["message"]["text"] == "first"
        assert second["input"]["message"]["text"] == "second"
        assert first["input"]["client_mutation_id"] != \
            second["input"]["client_mutation_id"]


class TestDeleteComment:
    """Pins comment deletion: the decoded pair and CommentID both hit the
    same b64 wire form."""

    def test_delete_comment_id_roundtrip(self):
        stub = StubSession(responses={
            "CometUFIDeleteCommentMutation":
                {"data": {"comment_delete": {"deleted_comment_id": 1,
                                              "success": True}}}})
        expected = CommentID.from_numeric(
            "3937341566408979", "3938247886318347").raw

        svc = FeedService(stub)
        svc.delete_comment("3937341566408979_3938247886318347")
        svc.delete_comment(
            CommentID.from_numeric("3937341566408979", "3938247886318347"))

        first = stub.graphql.calls[0]
        assert first[0] == "CometUFIDeleteCommentMutation"
        # doc_id from KNOWN_MUTATIONS (the v2 registry lacks this name)
        assert first[1] == DELETE_COMMENT_DOC_ID

        for call in stub.graphql.calls:
            variables = call[2]
            # decoded pair and CommentID both hit the same b64 wire form
            assert variables["input"]["comment_id"] == expected
            assert variables["input"]["actor_id"] == STUB_USER_ID
            assert variables["input"]["client_mutation_id"] == "1"
            assert variables["groupID"] == "12345678901234567"
            assert variables["renderLocation"] == "group"
            assert variables["scale"] == 2
            assert variables["__relay_internal__pv__groups_comet_use_glvrelayprovider"] is False

        # the wire id decodes back to the (post, comment) pair
        assert CommentID(raw=expected).decoded == \
            ("3937341566408979", "3938247886318347")


class TestPublish:
    """Pins ComposerStoryCreateMutation over the captured template: privacy
    base_state mapping and fresh idempotence/composer-session tokens."""

    def test_publish_private(self, captured_composer):
        stub = StubSession(responses={
            "ComposerStoryCreateMutation":
                {"data": {"composer_story_create": {"story_id": 1}}}})
        FeedService(stub).publish("cli test post", Privacy.PRIVATE)

        friendly, doc_id, variables = _last_call(stub)
        assert friendly == "ComposerStoryCreateMutation"
        assert doc_id == COMPOSER_DOC_ID
        assert variables["input"]["message"]["text"] == "cli test post"
        assert variables["input"]["message"]["ranges"] == []
        assert variables["input"]["audience"]["privacy"]["base_state"] == "SELF"
        assert variables["input"]["actor_id"] == STUB_USER_ID
        assert variables["feedLocation"] == "NEWSFEED"
        assert variables["renderLocation"] == "homepage_stream"
        # template defaults survive where not overridden
        assert variables["groupID"] is None

        # fresh idempotence/composer-session tokens, never the captured ones
        captured = next(m for m in captured_composer["mutations"]
                        if m["friendly_name"] == "ComposerStoryCreateMutation")
        token = variables["input"]["idempotence_token"]
        assert token.endswith("_FEED")
        assert token != captured["variables"]["input"]["idempotence_token"]
        assert uuid.UUID(token.removesuffix("_FEED")).version == 4
        assert variables["input"]["logging"]["composer_session_id"] != \
            captured["variables"]["input"]["logging"]["composer_session_id"]

    def test_publish_public_group_override(self):
        stub = StubSession(responses={
            "ComposerStoryCreateMutation": {"data": {}}})
        FeedService(stub).publish("hello group", Privacy.PUBLIC,
                                  feed_location="GROUP",
                                  render_location="group",
                                  group_id="12345678901234567")
        _, _, variables = _last_call(stub)
        assert variables["input"]["audience"]["privacy"]["base_state"] == "EVERYONE"
        assert variables["input"]["message"]["text"] == "hello group"
        assert variables["feedLocation"] == "GROUP"
        assert variables["renderLocation"] == "group"
        assert variables["groupID"] == "12345678901234567"


class TestPublishEnrichment:
    """Pins the composer enrichment kwargs: the live-confirmed
    ai_generated/text_format_preset_id rides, the tags range
    construction, and the CANDIDATE feeling/activity/place fields
    (added only when requested; the 1675012 coercion gate rejects
    unknown fields cleanly, so a wrong candidate fails safe)."""

    def _publish(self, **kwargs: Any) -> tuple[str, str, dict]:
        stub = StubSession(responses={
            "ComposerStoryCreateMutation": {"data": {}}})
        FeedService(stub).publish("body", Privacy.FRIENDS, **kwargs)
        return _last_call(stub)

    def test_ai_generated_true_flips_the_captured_bool(self):
        _, _, variables = self._publish(ai_generated=True)
        assert variables["input"]["ai_generated_self_disclosure_metadata"][
            "was_self_disclosed_as_ai_generated"] is True

    def test_ai_generated_none_leaves_the_captured_bool(self):
        _, _, variables = self._publish()
        assert variables["input"]["ai_generated_self_disclosure_metadata"][
            "was_self_disclosed_as_ai_generated"] is False

    def test_background_rides_text_format_preset_id(self):
        _, _, variables = self._publish(text_format_preset_id="7")
        assert variables["input"]["text_format_preset_id"] == "7"

    def test_background_defaults_to_the_captured_zero(self):
        _, _, variables = self._publish()
        assert variables["input"]["text_format_preset_id"] == "0"

    def test_tags_build_mention_ranges_against_the_composed_text(self):
        stub = StubSession(responses={
            "ComposerStoryCreateMutation": {"data": {}}})
        FeedService(stub).publish(
            "hello", Privacy.FRIENDS,
            tags=[("12345678901234568", "Jane"), ("12345678901234569", "Bob")])
        _, _, variables = _last_call(stub)
        # markers append space-separated: "hello @Jane @Bob"
        assert variables["input"]["message"]["text"] == "hello @Jane @Bob"
        assert variables["input"]["message"]["ranges"] == [
            {"entity": {"id": "12345678901234568"}, "length": 5, "offset": 6,
             "render_type": "mention"},
            {"entity": {"id": "12345678901234569"}, "length": 4, "offset": 12,
             "render_type": "mention"},
        ]

    def test_tags_compose_from_empty_text(self):
        stub = StubSession(responses={
            "ComposerStoryCreateMutation": {"data": {}}})
        FeedService(stub).publish("", Privacy.PRIVATE,
                                  tags=[("615937", "Jane")])
        _, _, variables = _last_call(stub)
        assert variables["input"]["message"]["text"] == "@Jane"
        assert variables["input"]["message"]["ranges"] == [
            {"entity": {"id": "615937"}, "length": 5, "offset": 0,
             "render_type": "mention"},
        ]

    def test_tags_reject_empty_pair_halves(self):
        svc = FeedService(StubSession(responses={}))
        with pytest.raises(ValueError):
            svc.publish("x", Privacy.PRIVATE, tags=[("", "Jane")])
        with pytest.raises(ValueError):
            svc.publish("x", Privacy.PRIVATE, tags=[("615937", "")])

    def test_feeling_rides_the_candidate_fields(self):
        _, _, variables = self._publish(feeling="123456")
        assert variables["input"]["target_type"] == "FEELING"
        assert variables["input"]["target_id"] == "123456"

    def test_activity_rides_the_candidate_fields(self):
        _, _, variables = self._publish(activity="654321")
        assert variables["input"]["target_type"] == "ACTIVITY"
        assert variables["input"]["target_id"] == "654321"

    def test_place_rides_the_candidate_field(self):
        _, _, variables = self._publish(place_id="999888")
        assert variables["input"]["place_id"] == "999888"

    def test_feeling_and_activity_are_mutually_exclusive(self):
        svc = FeedService(StubSession(responses={}))
        with pytest.raises(ValueError):
            svc.publish("x", Privacy.PRIVATE,
                        feeling="1", activity="2")

    def test_no_enrichment_omits_the_candidate_fields(self):
        _, _, variables = self._publish()
        # the candidate fields ride ONLY when requested — unrequested
        # candidates stay off the wire (the 1675012 gate rejects
        # unknown/extra fields, docs/15 §P3-3)
        assert "target_type" not in variables["input"]
        assert "target_id" not in variables["input"]
        assert "place_id" not in variables["input"]


class TestSetPrivacy:
    """Pins the per-post audience save: the story-scoped write id
    (b64 privacy_scope_renderer with the POST'S OWN id - live-verified
    2026-09-20 when the picker query resolved that exact scope for a
    published story), base_state substitution, and verbatim replay of
    every other captured field."""

    PRIVACY_SAVE_DOC_ID = "27802157519437974"

    def test_write_id_targets_the_posts_own_scope(self):
        stub = StubSession(responses={
            "CometPrivacySelectorSavePrivacyMutation": {"data": {}}})
        FeedService(stub).set_privacy("122112020481458110", Privacy.PRIVATE)

        friendly, doc_id, variables = _last_call(stub)
        assert friendly == "CometPrivacySelectorSavePrivacyMutation"
        assert doc_id == self.PRIVACY_SAVE_DOC_ID
        # the scope id rides UNQUOTED inside the b64 JSON (captured shape)
        scope = base64.b64decode(
            variables["input"]["privacy_write_id"]).decode()
        assert scope == 'privacy_scope_renderer:{"id":122112020481458110}'
        assert variables["input"]["privacy_row_input"]["base_state"] == "SELF"
        assert variables["input"]["actor_id"] == STUB_USER_ID
        # fresh mutation id, never the captured one
        assert variables["input"]["client_mutation_id"] != "1"

    def test_everything_else_rides_verbatim(self, captured_composer):
        stub = StubSession(responses={
            "CometPrivacySelectorSavePrivacyMutation": {"data": {}}})
        FeedService(stub).set_privacy("111", Privacy.FRIENDS)

        _, _, variables = _last_call(stub)
        captured = next(m for m in captured_composer["mutations"]
                        if m["friendly_name"] ==
                        "CometPrivacySelectorSavePrivacyMutation")
        cap = captured["variables"]
        # untouched: allow/deny/tag_expansion_state, render locations,
        # relay-provider gates, scale, storyRenderLocation
        assert variables["input"]["privacy_row_input"]["allow"] == []
        assert variables["input"]["privacy_row_input"]["deny"] == []
        assert variables["input"]["privacy_row_input"]["tag_expansion_state"] \
            == cap["input"]["privacy_row_input"]["tag_expansion_state"]
        assert variables["input"]["render_location"] \
            == cap["input"]["render_location"]
        assert variables["privacySelectorRenderLocation"] \
            == cap["privacySelectorRenderLocation"]
        assert variables["scale"] == 1
        assert variables["storyRenderLocation"] is None
        assert variables["tags"] is None

    def test_friends_audience_maps_to_the_friends_enum(self):
        stub = StubSession(responses={
            "CometPrivacySelectorSavePrivacyMutation": {"data": {}}})
        FeedService(stub).set_privacy("111", Privacy.FRIENDS)
        _, _, variables = _last_call(stub)
        assert variables["input"]["privacy_row_input"]["base_state"] == "FRIENDS"


class TestTyping:
    """Pins the live-typing broadcast pair: fresh uuid4 session ids,
    explicit stop passthrough, and state validation."""

    def test_typing_start(self):
        stub = StubSession(responses={
            "CometUFILiveTypingBroadcastMutation_StartMutation": {"data": {}}})
        FeedService(stub).typing(FB_SAMPLE, "start")

        friendly, doc_id, variables = _last_call(stub)
        assert friendly == "CometUFILiveTypingBroadcastMutation_StartMutation"
        assert doc_id == TYPING_START_DOC_ID
        assert variables["input"]["feedback_id"] == FB_SAMPLE
        assert variables["input"]["actor_id"] == STUB_USER_ID
        assert variables["input"]["client_mutation_id"] == "1"
        # fresh session id in uuid4 shape
        session_id = variables["input"]["session_id"]
        assert uuid.UUID(session_id).version == 4

    def test_typing_stop_explicit_session(self):
        stub = StubSession(responses={
            "CometUFILiveTypingBroadcastMutation_StopMutation": {"data": {}}})
        FeedService(stub).typing(
            FB_SAMPLE, "stop",
            session_id="ab1066e3-a2f1-453c-ba08-4536f0dc114e")

        friendly, doc_id, variables = _last_call(stub)
        assert friendly == "CometUFILiveTypingBroadcastMutation_StopMutation"
        assert doc_id == TYPING_STOP_DOC_ID
        assert variables["input"]["session_id"] == "ab1066e3-a2f1-453c-ba08-4536f0dc114e"

    def test_typing_rejects_bad_state(self):
        svc = FeedService(StubSession(responses={}))
        with pytest.raises(ValueError):
            svc.typing(FB_SAMPLE, "nonsense")  # type: ignore[arg-type]
