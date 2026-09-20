"""Offline tests for the SAVED + DEEP-COMMENTS + VIDEO/WATCH surfaces
(docs/02 §2, docs/04 §6, docs/15 live calibration).

Every read replays the REAL discovery path (page GET -> preload registry
harvest -> verbatim-variable replay -> typed parse) against canned wire
data shaped from the live 2026-09 probes; every mutation is recorded
(never fired) and its variable assembly is verified against the decoded /
live-verified templates. Command wiring is exercised through the real
argparse parsers with the session constructor stubbed out.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import StubSession

import constants as C
from config import Config
from domain.common import CommentID, FeedbackID, ReactionType
from graphql.registry import DocIdRegistry
from surfaces.comments import CommentsService
from surfaces.saved import SavedService
from surfaces.video import VideoService

ACTOR_ID = "12345678901234"

# ---------------------------------------------------------------- doc ids
# Registry-backed ground truth (data/doc_id_registry_v2.json).
BOOKMARK_BATCH_DOC_ID = "27293619020331978"   # decodes to the SHORTCUTS editor
POST_CONTENT_DOC_ID = "38640785042203305"
EDIT_COMMENT_DOC_ID = "28765227863112159"
HOOK_DELETE_COMMENT_DOC_ID = "27386493047638332"
VOTE_COMMENT_DOC_ID = "33995920373387348"
UNVOTE_COMMENT_DOC_ID = "25722792667420652"
WATCH_VIDEO_DOC_ID = "28867491862843621"
WATCH_CHAINING_DOC_ID = "28164369823234560"
WATCH_BADGE_DOC_ID = "23979318198368825"
WATCH_FEED_DOC_ID = "38063999043248030"        # FBUnifiedVideoRootWithEntrypointQuery
# Live-probed / bundle-decoded + live-verified (docs/15), NOT registry-backed:
SAVED_DASHBOARD_DOC_ID = "26929010753443830"   # page-harvested off /saved/
SAVE_MUTATION_DOC_ID = "9855506394526824"      # CometSaveMutation
UNSAVE_MUTATION_DOC_ID = "8500826123375303"    # useUnsaveMutation
DELETE_COMMENT_DOC_ID = "28058620387108821"   # live-proven (KNOWN_MUTATIONS)

# Real live-probed wire ids (docs/15 ground truth).
FEEDBACK_SAMPLE_POST = "ZmVlZGJhY2s6MzkzNzAyNDMyOTc3NDAzNg"
FEEDBACK_COMMENT = "ZmVlZGJhY2s6MTUzMjkzMDQ1ODg4MTY4M18yMDM5MzM4MjU2NzY3OTQ3"
COMMENT_SAMPLE_B64 = "Y29tbWVudDoxNTMyOTMwNDU4ODgxNjgzXzIwMzkzMzgyNTY3Njc5NDc="
COMMENT_PERSON_E_B64 = "Y29tbWVudDoxNTMyOTMwNDU4ODgxNjgzXzIxOTE2MDM5NzgxMTY0NzI="
STORY_KEY_B64 = ("UzpfSTEwMDA2NDk0MjI5MDE2MzoxNTMyOTMwNDU4ODgxNjgz6"
                 "OjE1MzI5MzA0NTg4ODE2ODM=")
PHOTO_SAVABLE_ID = "1532930378881691"


# ------------------------------------------------------------------ helpers
def load_asset(name: str) -> Any:
    """Load a data/ JSON file (same contract as tests/conftest.py)."""
    data = Path(__file__).resolve().parents[2] / "data"
    path = data / name
    if not path.is_file():
        raise FileNotFoundError(f"missing captured fixture: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def preload_html(query_name: str, doc_id: str, variables: dict[str, Any]) -> str:
    """A REAL-format SSR preloader registration block (docs/15 §P2-2)."""
    return (
        '<html><body><script>'
        '{"actorID":"' + ACTOR_ID + '",'
        '"preloaderID":"adp_' + query_name + 'RelayPreloader_6aad3496ac9920a72557597",'
        '"queryID":"' + doc_id + '",'
        '"variables":' + json.dumps(variables, separators=(",", ":")) + ","
        '"queryName":"' + query_name + '"}'
        '</script></body></html>'
    )


def _last_call(stub: StubSession) -> tuple[str, str, dict]:
    assert stub.graphql.calls, "service made no client call"
    return stub.graphql.calls[-1]


# ------------------------------------------------------------ saved fixtures
SAVED_PRELOAD_VARIABLES: dict[str, Any] = {
    "content_filter": None,
    "hoisted_item_id": None,
    "notif_id": None,
    "scale": 2,
}

SAVED_HTML = preload_html("CometSaveDashboardRootQuery",
                          SAVED_DASHBOARD_DOC_ID, SAVED_PRELOAD_VARIABLES)

#: The live-probed dashboard payload shape (save of a public sample post).
SAVED_PAYLOAD: dict[str, Any] = {
    "data": {"viewer": {"saver_info": {"all_saves": {
        "edges": [
            {"node": {"id": "122111510415458110", "__typename": "Save",
                      "savable": {
                          "__typename": "Photo",
                          "id": PHOTO_SAVABLE_ID,
                          "savable_title": {"text":
                                            "Sample Page's photo"},
                          "savable_attributes": [{"text": "Link"}],
                          "savable_permalink":
                              "https://www.facebook.com/photo.php"
                              "?fbid=1532930378881691"
                              "&set=a.374275834747157&type=3",
                          "savable_default_category": "LINK",
                          "url": "https://www.facebook.com/photo.php"
                                 "?fbid=1532930378881691"
                                 "&set=a.374275834747157&type=3"}},
             "cursor": "AQHTYYGkTgG5zOM"},
            {"node": {"id": "122111510415458999", "__typename": "Save",
                      "savable": {
                          "__typename": "Video",
                          "id": "1090654616829573",
                          "savable_title": {"text": "line one\nline two"},
                          "savable_permalink":
                              "https://www.facebook.com/watch/"
                              "?v=1090654616829573"}}},
        ],
        "page_info": {"has_next_page": False,
                      "end_cursor": "AQHTEn3Q88TkeejEGntHRsA"}}}}},
    "extensions": {"is_final": True},
}

SAVE_RESPONSE: dict[str, Any] = {
    "data": {"node_saved_state": {
        "save_node": {"__typename": "Photo", "id": PHOTO_SAVABLE_ID,
                      "viewer_saved_state": "SAVED"},
        "save": {"id": "122111510415458110"}}}}

UNSAVE_RESPONSE: dict[str, Any] = {
    "data": {"node_saved_state": {
        "save_node": {"__typename": "Photo", "id": PHOTO_SAVABLE_ID,
                      "viewer_saved_state": "NOT_SAVED"},
        "save_id": "122111510415458110"}}}

# --------------------------------------------------------- comments fixtures
POST_CONTENT_PRELOAD_VARIABLES: dict[str, Any] = {
    "feedbackSource": 2,
    "feedLocation": "POST_PERMALINK_DIALOG",
    "focusCommentID": None,
    "privacySelectorRenderLocation": "COMET_STREAM",
    "renderLocation": "permalink",
    "scale": 2,
    "shouldChangeNodeFieldName": True,
    "storyID": STORY_KEY_B64,
    "useDefaultActor": False,
}

PERMALINK_HTML = preload_html("CometSinglePostDialogContentQuery",
                              POST_CONTENT_DOC_ID,
                              POST_CONTENT_PRELOAD_VARIABLES)

#: The live-probed comment-list path (docs/15), two real comments plus a
#: reply-expander stub node that must not surface as a comment.
COMMENTS_PAYLOAD: dict[str, Any] = {
    "data": {"node_v2": {"comet_sections": {"feedback": {"story": {
        "story_ufi_container": {"story": {"feedback_context": {
            "feedback_target_with_context": {"comment_list_renderer": {
                "feedback": {"comment_rendering_instance_for_feed_location": {
                    "comments": {"edges": [
                        {"node": {"__typename": "Comment",
                                  "id": COMMENT_SAMPLE_B64,
                                  "author": {"__typename": "User",
                                             "id": "12345678901234568",
                                             "name": "Sample Page"},
                                  "body": {"text":
                                      "https://www.sample.net/bangla/"
                                      "international/news-details-540541"}}},
                        {"node": {"__typename": "Comment",
                                  "id": COMMENT_PERSON_E_B64,
                                  "author": {"__typename": "User",
                                             "id": "10000000000000004",
                                             "name": "Person E"},
                                  "body": {"text": "বাহ চমৎকার লেখা"}}},
                        {"node": {"__typename": "Comment",
                                  "id": "Y29tbWVudDoxNTMyOTMwNDU4ODgxNjgz"
                                        "XzAwMDAwMDAwMDAwMDAwMDA=",
                                  "feedback": {"id": FEEDBACK_COMMENT},
                                  "inline_replies_expander_renderer": {}}},
                    ]}}}}}}}}}}}}},
    "extensions": {"is_final": True},
}

# ------------------------------------------------------------- video fixtures
WATCH_PRELOAD_VARIABLES: dict[str, Any] = {
    "count": 1,
    "initial_node_id": "",
    "page_id": "",
    "root_video_id": "",
    "scale": 2,
    "should_use_stream": True,
    "shouldIncludeInitialNodeFetch": False,
    "stream_initial_count": 1,
    "useDefaultActor": False,
    "user_id": "",
    "video_feed_context_data": {
        "referral_source": "fb_shorts_tab",
        "request_type": "NORMAL",
        "seed_video_id": None,
        "surface_type": "TAB",
        "video_channel_entry_point": "VIDEOS_TAB",
    },
}

WATCH_HTML = preload_html("FBUnifiedVideoRootWithEntrypointQuery",
                          WATCH_FEED_DOC_ID, WATCH_PRELOAD_VARIABLES)

#: Live-probed Video node shapes (the /watch/ replay + the chaining replay).
WATCH_PAYLOAD: dict[str, Any] = {
    "data": {"viewer": {"video_feed": {"edges": [
        {"node": {"__typename": "Video", "id": "1090654616829573",
                  "permalink_url":
                      "https://www.facebook.com/reel/1090654616829573/",
                  "owner": {"__typename": "User", "id": "100070015506924",
                            "name": "Reel Owner"},
                  "length_in_second": 14.535}},
        {"node": {"__typename": "Video", "id": "1484346430381822",
                  "shareable_url":
                      "https://www.facebook.com/watch/?v=1484346430381822",
                  "owner": {"__typename": "User", "id": "100071115506925"},
                  "length_in_second": 21.0,
                  "video_view_count": 4096}},
        # duplicate id: the unified player repeats nodes; dedup applies
        {"node": {"__typename": "Video", "id": "1090654616829573",
                  "permalink_url": "https://www.facebook.com/reel/dup/"}},
    ],
        "page_info": {"has_next_page": True,
                      "end_cursor": "AQHTCHAININGTOKEN1234567890"}}}},
    "extensions": {"is_final": True},
}

CHAINING_PAYLOAD: dict[str, Any] = {
    "data": {"video_channel_feed": {"edges": [
        {"node": {"__typename": "Video", "id": "1555000111000222",
                   "permalink_url":
                       "https://www.facebook.com/reel/1555000111000222/",
                   "owner": {"__typename": "User", "id": "100070015506926",
                             "name": "Chained Creator"},
                   "length_in_second": 9.5}}]}},
    "extensions": {"is_final": True},
}

#: Live-probed unified-player story wrap (2026-09-18): the merged response
#: wraps the primary Video inside a feed-story node (NO __typename —
#: identified by its message/feedback shape) that carries the post caption
#: and UFI feedback id, while the shorts-scrubber echo variants carry a
#: top-level track_title and an owner with a name.
WATCH_STORY_PAYLOAD: dict[str, Any] = {
    "data": {
        "id": "UzpfSUZTOjE6LTYxMzcyOTQ2ODA3NDYyOTI2NjI6ZUp3",
        "creation_time": 1790000000,
        "message": {"text": "Watch this reel\n#foryou"},
        "feedback": {"id": "ZmVlZGJhY2s6MTIyMTEwNDkzOTAxNDU3NTc1"},
        "attachments": [{"media": {
            "__typename": "Video", "id": "2111827749761133",
            "permalink_url":
                "https://www.facebook.com/reel/2111827749761133/",
            "owner": {"__typename": "User", "id": "10000000000000006"},
            "length_in_second": 504.726}}],
        "node": {"attachments": [{"media": {
            "__typename": "Video", "id": "1609244043946552",
            "permalink_url":
                "https://www.facebook.com/reel/1609244043946552/",
            "owner": {"__typename": "User", "id": "10000000000000005",
                      "name": "Person F"},
            "length_in_second": 14.956,
            "track_title": "Person F \u00b7 Original audio"}}]},
    },
    "extensions": {"is_final": True},
}

BADGE_PAYLOAD: dict[str, Any] = {
    "data": {"viewer": {"bookmarks": {"edges": [
        {"node": {"id": "Ym9va21hcms6NjE1OTM3NDMzMjA4NjQ6NjQ0NzE1NDQ1"
                       "NjUwOTI0OjIzOTI5NTAxMzc6Ojo6dW5rbm93bg==",
                  "bookmarked_node": {"__typename": "Application",
                                       "id": "2392950137"},
                  "unread_count": 3}}]}}},
    "extensions": {"is_final": True},
}


# -------------------------------------------------------------- registry
class TestRegistryPresence:
    """Pins the registry-backed doc_ids and the SHORTCUTS-editor vs
    save-plane distinction for the bookmark-batch mutation."""

    def test_every_registry_backed_doc_id_is_harvested(self):
        """Every registry-backed ground-truth doc_id resolves in the real
        asset registry (docs/13 §2)."""
        registry = DocIdRegistry.from_assets(Config.discover().assets_dir)
        for name, doc_id in [
            ("useUpdateBookmarkBatchMutation", BOOKMARK_BATCH_DOC_ID),
            ("CometSinglePostDialogContentQuery", POST_CONTENT_DOC_ID),
            ("useCometUFIEditCommentMutation", EDIT_COMMENT_DOC_ID),
            ("useCometUFIDeleteCommentMutation", HOOK_DELETE_COMMENT_DOC_ID),
            ("useCometUFICommentVoteMutation", VOTE_COMMENT_DOC_ID),
            ("useCometUFICommentUnvoteMutation", UNVOTE_COMMENT_DOC_ID),
            ("CometWatchAndScrollVideoQuery", WATCH_VIDEO_DOC_ID),
            ("CometWatchAndScrollChainingQuery", WATCH_CHAINING_DOC_ID),
            ("useCometWatchBadgeCountQuery", WATCH_BADGE_DOC_ID),
            ("FBUnifiedVideoRootWithEntrypointQuery", WATCH_FEED_DOC_ID),
        ]:
            assert registry.doc_id(name) == doc_id

    def test_bookmark_batch_decodes_as_shortcuts_editor_not_save(self):
        """The registry's useUpdateBookmarkBatchMutation is the left-rail
        SHORTCUTS mutation (bundle-decoded 2026-09) — the real save plane is
        the live-verified CometSaveMutation/useUnsaveMutation pair."""
        assert BOOKMARK_BATCH_DOC_ID == "27293619020331978"
        assert SAVE_MUTATION_DOC_ID != BOOKMARK_BATCH_DOC_ID
        assert UNSAVE_MUTATION_DOC_ID != BOOKMARK_BATCH_DOC_ID


# ---------------------------------------------------------- saved service
class TestSavedService:
    """Pins the saved dashboard read and the save/unsave mutations'
    decoded envelopes, including fresh per-call client ids."""

    def _service(self, monkeypatch, html: str,
                 responses: dict[str, Any] | None = None,
                 ) -> tuple[StubSession, SavedService]:
        session = StubSession(responses or
                              {"CometSaveDashboardRootQuery": SAVED_PAYLOAD})
        service = SavedService(session)
        monkeypatch.setattr(service, "_fetch", lambda url: html)
        return session, service

    def test_list_replays_page_preload_and_types_rows(self, monkeypatch):
        session, service = self._service(monkeypatch, SAVED_HTML)
        items = service.list(limit=20)

        assert len(items) == 2
        first = items[0]
        assert first["id"] == "122111510415458110"
        assert first["title"] == "Sample Page's photo"
        assert first["url"] == ("https://www.facebook.com/photo.php"
                                "?fbid=1532930378881691"
                                "&set=a.374275834747157&type=3")
        assert first["type"] == "Photo"
        # multiline titles are head-cut
        assert items[1]["title"] == "line one"
        assert items[1]["type"] == "Video"

        friendly, doc_id, variables = _last_call(session)
        assert friendly == "CometSaveDashboardRootQuery"
        assert doc_id == SAVED_DASHBOARD_DOC_ID
        assert variables == SAVED_PRELOAD_VARIABLES   # verbatim replay

    def test_list_limit_truncates(self, monkeypatch):
        _session, service = self._service(monkeypatch, SAVED_HTML)
        assert len(service.list(limit=1)) == 1

    def test_list_falls_back_to_baked_variables(self, monkeypatch):
        session, service = self._service(monkeypatch, "<html>no preload</html>")
        service.list()
        _, doc_id, variables = _last_call(session)
        assert doc_id == SAVED_DASHBOARD_DOC_ID
        assert variables == {"content_filter": None, "hoisted_item_id": None,
                             "notif_id": None, "scale": 2}

    def test_save_substitutes_node_id_and_fresh_client_id(self):
        session = StubSession({"CometSaveMutation": SAVE_RESPONSE})
        response = SavedService(session).save(PHOTO_SAVABLE_ID)
        assert response == SAVE_RESPONSE

        friendly, doc_id, variables = _last_call(session)
        assert friendly == "CometSaveMutation"
        assert doc_id == SAVE_MUTATION_DOC_ID
        assert variables["input"]["node_id"] == PHOTO_SAVABLE_ID
        assert variables["input"]["save_action"] == "SAVE"
        assert variables["input"]["save_mechanism"] == "CARET_MENU"
        assert variables["input"]["surface"] == "STORY"
        uuid.UUID(variables["input"]["client_mutation_id"])  # uuid-shaped

    def test_unsave_decoded_envelope(self):
        session = StubSession({"useUnsaveMutation": UNSAVE_RESPONSE})
        response = SavedService(session).unsave(PHOTO_SAVABLE_ID)
        assert response == UNSAVE_RESPONSE

        friendly, doc_id, variables = _last_call(session)
        assert friendly == "useUnsaveMutation"
        assert doc_id == UNSAVE_MUTATION_DOC_ID
        assert variables["input"]["node_id"] == PHOTO_SAVABLE_ID
        assert variables["input"]["save_action"] == "UNSAVE"
        assert variables["contributorRoles"] == ["CONTRIBUTOR"]
        assert variables["scale"] == 2

    def test_save_unsave_client_ids_are_fresh_per_call(self):
        session = StubSession({"CometSaveMutation": SAVE_RESPONSE,
                               "useUnsaveMutation": UNSAVE_RESPONSE})
        service = SavedService(session)
        service.save("111")
        service.unsave("111")
        service.save("222")
        ids = [call[2]["input"]["client_mutation_id"]
               for call in session.graphql.calls]
        assert len(ids) == 3 and len(set(ids)) == 3


# -------------------------------------------------------- comments service
class TestCommentsRead:
    """Pins the permalink comments read: preload-harvested verbatim
    variables, typed rows, and reply-expander stub skipping."""

    def _read(self, monkeypatch, limit: int = 30):
        session = StubSession(
            {"CometSinglePostDialogContentQuery": COMMENTS_PAYLOAD})
        service = CommentsService(session)
        monkeypatch.setattr(service, "_fetch", lambda url: PERMALINK_HTML)
        return session, service.read(
            "https://www.facebook.com/sample.page/posts/pfbid02Test", limit=limit)

    def test_read_types_comments_from_real_payload_path(self, monkeypatch):
        session, comments = self._read(monkeypatch)

        assert len(comments) == 2  # the expander stub is skipped
        first, second = comments
        # b64 comment ids decode to the (post, comment) fbid pair
        assert first.id.decoded == ("1532930458881683", "2039338256767947")
        assert str(first.id) == COMMENT_SAMPLE_B64
        assert first.actor is not None
        assert first.actor.name == "Sample Page"
        assert first.actor.id == "12345678901234568"
        assert first.text is not None and first.text.startswith("https://")
        assert second.actor is not None and second.actor.name == "Person E"
        assert second.text == "বাহ চমৎকার লেখা"

        friendly, doc_id, variables = _last_call(session)
        assert friendly == "CometSinglePostDialogContentQuery"
        assert doc_id == POST_CONTENT_DOC_ID
        # the replayed variables are the page's VERBATIM preloaded variables
        assert variables == POST_CONTENT_PRELOAD_VARIABLES
        assert variables["storyID"] == STORY_KEY_B64

    def test_read_limit_truncates_document_order(self, monkeypatch):
        _, comments = self._read(monkeypatch, limit=1)
        assert len(comments) == 1
        assert comments[0].id.decoded[1] == "2039338256767947"

    def test_read_rejects_non_url(self):
        with pytest.raises(ValueError):
            CommentsService(StubSession({})).read(FEEDBACK_SAMPLE_POST)

    def test_read_requires_preload(self, monkeypatch):
        session = StubSession({})
        service = CommentsService(session)
        monkeypatch.setattr(service, "_fetch", lambda url: "<html></html>")
        with pytest.raises(RuntimeError):
            service.read("https://www.facebook.com/some/posts/x")


class TestCommentsReact:
    """Pins comment-context reaction: the OBJECT feedback_source (live
    verified), verbatim tracking replay, and template-cache integrity."""

    def _captured_like(self) -> dict[str, Any]:
        return next(m for m in load_asset("captured_mutations.json")["mutations"]
                    if m["friendly_name"] == "CometUFIFeedbackReactMutation"
                    and m["variables"]["input"]["feedback_reaction_id"]
                    == "1635855486666999")["variables"]

    def test_react_substitutes_comment_feedback_and_decoded_source(self):
        session = StubSession(
            {"CometUFIFeedbackReactMutation": {"data": {}}})
        CommentsService(session).react(FEEDBACK_COMMENT, ReactionType.LOVE)

        friendly, doc_id, variables = _last_call(session)
        assert friendly == "CometUFIFeedbackReactMutation"
        assert doc_id == "27646120298312844"
        # the comment's own feedback id, verbatim
        assert variables["input"]["feedback_id"] == FEEDBACK_COMMENT
        assert variables["input"]["feedback_reaction_id"] == "1678524932434102"
        # decoded comment-context enum value (live-verified), not "NEWS_FEED"
        assert variables["input"]["feedback_source"] == "OBJECT"
        # tracking blobs replay verbatim from the captured LIKE template
        captured = self._captured_like()
        assert variables["input"]["tracking"] == captured["input"]["tracking"]
        assert variables["input"]["is_tracking_encrypted"] is True

    def test_react_normalizes_decoded_feedback_form(self):
        session = StubSession(
            {"CometUFIFeedbackReactMutation": {"data": {}}})
        CommentsService(session).react(
            "feedback:1532930458881683_2039338256767947", ReactionType.LIKE)
        _, _, variables = _last_call(session)
        assert variables["input"]["feedback_id"] == FEEDBACK_COMMENT

    def test_react_feedback_source_override(self):
        session = StubSession(
            {"CometUFIFeedbackReactMutation": {"data": {}}})
        CommentsService(session).react(FEEDBACK_COMMENT, ReactionType.SUPPORT,
                                       feedback_source="NEWS_FEED")
        _, _, variables = _last_call(session)
        assert variables["input"]["feedback_source"] == "NEWS_FEED"
        assert variables["input"]["feedback_reaction_id"] == "613557422527858"

    def test_unreact_uses_remove_capture_and_object_source(self):
        session = StubSession(
            {"CometUFIFeedbackReactMutation": {"data": {}}})
        CommentsService(session).unreact(FEEDBACK_COMMENT)

        friendly, doc_id, variables = _last_call(session)
        assert friendly == "CometUFIFeedbackReactMutation"
        assert doc_id == "27646120298312844"
        assert variables["input"]["feedback_reaction_id"] == "0"
        assert variables["input"]["feedback_id"] == FEEDBACK_COMMENT
        assert variables["input"]["feedback_source"] == "OBJECT"
        captured_remove = next(
            m for m in load_asset("captured_mutations.json")["mutations"]
            if m["friendly_name"] == "CometUFIFeedbackReactMutation"
            and m["variables"]["input"]["feedback_reaction_id"] == "0"
        )["variables"]
        assert variables["input"]["tracking"] == \
            captured_remove["input"]["tracking"]

    def test_react_template_cache_not_contaminated(self):
        session = StubSession(
            {"CometUFIFeedbackReactMutation": {"data": {}}})
        service = CommentsService(session)
        service.react(FEEDBACK_COMMENT, ReactionType.LOVE)
        service.react(FEEDBACK_SAMPLE_POST, ReactionType.LIKE)
        first, second = session.graphql.calls[0][2], session.graphql.calls[1][2]
        assert first["input"]["feedback_id"] == FEEDBACK_COMMENT
        assert second["input"]["feedback_id"] == FEEDBACK_SAMPLE_POST


class TestCommentsEditDeleteVote:
    """Pins edit/delete/vote/unvote variable assembly against the decoded
    and live-proven shapes."""

    def test_edit_decoded_envelope(self):
        session = StubSession(
            {"useCometUFIEditCommentMutation":
                 {"data": {"comment_edit": {"comment": {"id": 1}}}}})
        expected = CommentID.from_numeric(
            "3937341566408979", "3938247886318347").raw
        CommentsService(session).edit("3937341566408979_3938247886318347",
                                      "edited text")

        friendly, doc_id, variables = _last_call(session)
        assert friendly == "useCometUFIEditCommentMutation"
        assert doc_id == EDIT_COMMENT_DOC_ID  # registry-backed
        assert variables["input"]["comment_id"] == expected
        assert variables["input"]["message"] == {"ranges": [], "text": "edited text"}
        assert variables["input"]["formatting_style"] == "PLAIN_TEXT"
        assert variables["input"]["attachments"] is None
        assert variables["input"]["tracking"] == []
        assert variables["scale"] == 1
        assert variables["translationType"] == "ORIGINAL"
        assert variables["useDefaultActor"] is False
        assert variables["__relay_internal__pv__"
                         "CometUFICommentActionLinksRewriteEnabledrelayprovider"] is True
        assert "attribution_id_v2" in variables["input"]

    def test_delete_live_proven_shape(self):
        session = StubSession(
            {"CometUFIDeleteCommentMutation":
                 {"data": {"comment_delete": {"deleted_comment_id": 1,
                                              "success": True}}}})
        expected = CommentID.from_numeric(
            "3937341566408979", "3938247886318347").raw
        CommentsService(session).delete(expected)

        friendly, doc_id, variables = _last_call(session)
        assert friendly == "CometUFIDeleteCommentMutation"
        # doc_id from KNOWN_MUTATIONS (the registry lacks this name) — the
        # LIVE-PROVEN variant, not the hook-based registry sibling
        assert doc_id == DELETE_COMMENT_DOC_ID
        assert doc_id == C.KNOWN_MUTATIONS["CometUFIDeleteCommentMutation"]
        assert variables["input"]["comment_id"] == expected
        assert variables["input"]["actor_id"] == ACTOR_ID
        assert variables["input"]["client_mutation_id"] == "1"
        assert variables["groupID"] == "12345678901234567"
        assert variables["renderLocation"] == "group"
        assert variables["scale"] == 2
        assert variables["__relay_internal__pv__groups_comet_use_glvrelayprovider"] is False

    def test_delete_render_location_override(self):
        session = StubSession(
            {"CometUFIDeleteCommentMutation": {"data": {}}})
        CommentsService(session).delete(COMMENT_SAMPLE_B64,
                                         render_location="permalink")
        _, _, variables = _last_call(session)
        assert variables["renderLocation"] == "permalink"
        # b64 passes through untouched
        assert variables["input"]["comment_id"] == COMMENT_SAMPLE_B64

    def test_vote_and_unvote_decoded_input(self):
        session = StubSession(
            {"useCometUFICommentVoteMutation":
                 {"data": {"comment_vote": {"comment": {
                     "viewer_comment_vote_state": "UPVOTE"}}}},
             "useCometUFICommentUnvoteMutation":
                 {"data": {"comment_unvote": {"comment": {
                     "viewer_comment_vote_state": "NONE"}}}}})
        service = CommentsService(session)
        service.vote(COMMENT_SAMPLE_B64, "UPVOTE")
        service.unvote(COMMENT_SAMPLE_B64)

        vote_call = session.graphql.calls[0]
        assert vote_call[0] == "useCometUFICommentVoteMutation"
        assert vote_call[1] == VOTE_COMMENT_DOC_ID
        assert vote_call[2] == {"input": {"comment_id": COMMENT_SAMPLE_B64,
                                          "new_vote_state": "UPVOTE"}}
        unvote_call = session.graphql.calls[1]
        assert unvote_call[0] == "useCometUFICommentUnvoteMutation"
        assert unvote_call[1] == UNVOTE_COMMENT_DOC_ID
        assert unvote_call[2] == {"input": {"comment_id": COMMENT_SAMPLE_B64}}

    def test_vote_state_enum_only(self):
        service = CommentsService(StubSession({}))
        with pytest.raises(ValueError, match="UPVOTE or DOWNVOTE"):
            service.vote(COMMENT_SAMPLE_B64, "SIDEVOTE")  # type: ignore[arg-type]


# ---------------------------------------------------------- video service
class TestVideoFeed:
    """Pins the watch feed: preload replay, chaining pagination, duplicate
    dedup, and the story-wrapper title plumbing."""

    def _service(self, monkeypatch, html: str,
                 responses: dict[str, Any]) -> tuple[StubSession, VideoService]:
        session = StubSession(responses)
        service = VideoService(session)
        monkeypatch.setattr(service, "_fetch", lambda url: html)
        return session, service

    def test_feed_replays_watch_preload_and_types_rows(self, monkeypatch):
        session, service = self._service(
            monkeypatch, WATCH_HTML,
            {"FBUnifiedVideoRootWithEntrypointQuery": WATCH_PAYLOAD})
        page = service.watch_feed(limit=10)

        videos = page["videos"]
        assert page["count"] == 2  # duplicate id deduped
        assert videos[0]["id"] == "1090654616829573"
        assert videos[0]["url"] == "https://www.facebook.com/reel/1090654616829573/"
        assert videos[0]["owner_name"] == "Reel Owner"
        assert videos[0]["length_s"] == 14.535
        assert videos[1]["url"] == "https://www.facebook.com/watch/?v=1484346430381822"
        assert videos[1]["view_count"] == 4096
        assert videos[1]["owner_name"] is None
        assert page["end_cursor"] == "AQHTCHAININGTOKEN1234567890"
        assert page["has_next_page"] is True
        assert page["raw_size"] > 100

        friendly, doc_id, variables = _last_call(session)
        assert friendly == "FBUnifiedVideoRootWithEntrypointQuery"
        assert doc_id == WATCH_FEED_DOC_ID
        # verbatim preload variables with count promoted to the limit
        assert variables == {**WATCH_PRELOAD_VARIABLES, "count": 10}
        assert variables["video_feed_context_data"]["surface_type"] == "TAB"

    def test_feed_limit_truncates(self, monkeypatch):
        _session, service = self._service(
            monkeypatch, WATCH_HTML,
            {"FBUnifiedVideoRootWithEntrypointQuery": WATCH_PAYLOAD})
        page = service.watch_feed(limit=1)
        assert page["count"] == 1

    def test_feed_chaining_pagination(self, monkeypatch):
        session, service = self._service(
            monkeypatch, WATCH_HTML,
            {"CometWatchAndScrollChainingQuery": CHAINING_PAYLOAD})
        page = service.watch_feed(limit=3, cursor="AQHTCHAINEDCURSOR",
                                  seed_video_id="1090654616829573")

        assert page["count"] == 1
        assert page["videos"][0]["id"] == "1555000111000222"
        friendly, doc_id, variables = _last_call(session)
        assert friendly == "CometWatchAndScrollChainingQuery"
        assert doc_id == WATCH_CHAINING_DOC_ID
        # decoded watch-and-scroll hook variables (live-verified)
        assert variables["caller"] == "WNS"
        assert variables["channelEntryPoint"] == "WNS"
        assert variables["chainingCursor"] == "AQHTCHAINEDCURSOR"
        assert variables["seedVideoID"] == "1090654616829573"
        assert variables["count"] == 3
        assert variables["scale"] == 2

    def test_feed_falls_back_to_baked_variables(self, monkeypatch):
        session, service = self._service(
            monkeypatch, "<html>no preload</html>",
            {"FBUnifiedVideoRootWithEntrypointQuery": WATCH_PAYLOAD})
        service.watch_feed(limit=4)
        _, doc_id, variables = _last_call(session)
        assert doc_id == WATCH_FEED_DOC_ID
        assert variables["count"] == 4
        assert variables["video_feed_context_data"][
            "video_channel_entry_point"] == "VIDEOS_TAB"

    def test_feed_titles_from_story_wrapper_and_track_title(self, monkeypatch):
        """Live-probed title plumbing (2026-09-18): the primary Video node
        carries no title of its own — the enclosing feed-story wrapper's
        message.text (head-cut) and feedback.id ride in; the scrubber
        variant keeps its own top-level track_title."""
        _session, service = self._service(
            monkeypatch, WATCH_HTML,
            {"FBUnifiedVideoRootWithEntrypointQuery": WATCH_STORY_PAYLOAD})
        page = service.watch_feed(limit=10)

        videos = page["videos"]
        assert page["count"] == 2
        primary = videos[0]
        assert primary["id"] == "2111827749761133"
        assert primary["title"] == "Watch this reel"  # head of the caption
        assert primary["feedback_id"] == "ZmVlZGJhY2s6MTIyMTEwNDkzOTAxNDU3NTc1"
        assert primary["owner_id"] == "10000000000000006"
        assert primary["owner_name"] is None
        assert primary["url"] == \
            "https://www.facebook.com/reel/2111827749761133/"
        second = videos[1]
        assert second["id"] == "1609244043946552"
        assert second["title"] == "Person F \u00b7 Original audio"
        assert second["owner_name"] == "Person F"


class TestVideoBadge:
    """Pins the watch badge: the decoded operation carries no
    LocalArguments (empty variable replay) and an empty payload reads 0."""

    def test_badge_walks_bookmark_unread_count(self):
        session = StubSession(
            {"useCometWatchBadgeCountQuery": BADGE_PAYLOAD})
        count = VideoService(session).badge()
        assert count == 3

        friendly, doc_id, variables = _last_call(session)
        assert friendly == "useCometWatchBadgeCountQuery"
        assert doc_id == WATCH_BADGE_DOC_ID
        # the decoded operation has NO LocalArguments — every argument is a
        # query-text literal (bookmark id 2392950137), so the replay is {}
        assert variables == {}

    def test_badge_empty_payload_defaults_to_zero(self):
        session = StubSession({"useCometWatchBadgeCountQuery": {"data": {}}})
        assert VideoService(session).badge() == 0


# ------------------------------------------------------------- command wiring
def build_parser() -> argparse.ArgumentParser:
    from commands import comments as comments_cmd
    from commands import saved as saved_cmd
    from commands import video as video_cmd

    parser = argparse.ArgumentParser(prog="fbk-test")
    sub = parser.add_subparsers(dest="command", required=True)
    saved_cmd.register(sub)
    comments_cmd.register(sub)
    video_cmd.register(sub)
    return parser


class TestCommands:
    """Pins the saved/comments/video argparse wiring with the session
    constructor stubbed out."""

    def _run(self, monkeypatch, capsys, session, argv,
             page_html: str | None = None) -> tuple[int, str]:
        from commands import comments as comments_cmd
        from commands import saved as saved_cmd
        from commands import video as video_cmd
        from surfaces.comments import CommentsService
        from surfaces.saved import SavedService
        from surfaces.video import VideoService

        parser = build_parser()
        args = parser.parse_args(argv)
        monkeypatch.setattr(saved_cmd, "new_session", lambda _a: session)
        monkeypatch.setattr(comments_cmd, "new_session", lambda _a: session)
        monkeypatch.setattr(video_cmd, "new_session", lambda _a: session)
        if page_html is not None:
            monkeypatch.setattr(SavedService, "_fetch",
                                lambda self, url: page_html)
            monkeypatch.setattr(CommentsService, "_fetch",
                                lambda self, url: page_html)
            monkeypatch.setattr(VideoService, "_fetch",
                                lambda self, url: page_html)
        code = args.fn(args)
        captured = capsys.readouterr().out
        return code, captured

    def test_saved_list_command(self, monkeypatch, capsys):
        session = StubSession(
            {"CometSaveDashboardRootQuery": SAVED_PAYLOAD})
        code, out = self._run(
            monkeypatch, capsys, session,
            ["saved", "list", "--limit", "5"], page_html=SAVED_HTML)
        assert code == 0
        assert "saved items: 2" in out
        assert "Sample Page's photo" in out
        assert '"count": 2' in out

    def test_saved_save_command(self, monkeypatch, capsys):
        session = StubSession({"CometSaveMutation": SAVE_RESPONSE})
        code, out = self._run(
            monkeypatch, capsys, session,
            ["saved", "save", "--item-id", PHOTO_SAVABLE_ID])
        assert code == 0
        assert f"saved {PHOTO_SAVABLE_ID} -> SAVED" in out
        _, doc_id, variables = session.graphql.calls[0]
        assert doc_id == SAVE_MUTATION_DOC_ID
        assert variables["input"]["node_id"] == PHOTO_SAVABLE_ID

    def test_saved_unsave_command(self, monkeypatch, capsys):
        session = StubSession({"useUnsaveMutation": UNSAVE_RESPONSE})
        code, out = self._run(
            monkeypatch, capsys, session,
            ["saved", "unsave", "--item-id", PHOTO_SAVABLE_ID])
        assert code == 0
        assert f"unsaved {PHOTO_SAVABLE_ID} -> NOT_SAVED" in out
        _, doc_id, _ = session.graphql.calls[0]
        assert doc_id == UNSAVE_MUTATION_DOC_ID

    def test_comments_read_command(self, monkeypatch, capsys):
        session = StubSession(
            {"CometSinglePostDialogContentQuery": COMMENTS_PAYLOAD})
        code, out = self._run(
            monkeypatch, capsys, session,
            ["comments", "read", "--permalink",
             "https://www.facebook.com/sample.page/posts/pfbid02Test",
             "--limit", "30"],
            page_html=PERMALINK_HTML)
        assert code == 0
        assert "comments: 2" in out
        assert "Sample Page" in out
        assert "Person E" in out
        assert '"count": 2' in out

    def test_comments_react_command(self, monkeypatch, capsys):
        session = StubSession(
            {"CometUFIFeedbackReactMutation": {"data": {}}})
        code, _out = self._run(
            monkeypatch, capsys, session,
            ["comments", "react", "--feedback-id", FEEDBACK_COMMENT,
             "--reaction", "LOVE"])
        assert code == 0
        _, _, variables = session.graphql.calls[0]
        assert variables["input"]["feedback_id"] == FEEDBACK_COMMENT
        assert variables["input"]["feedback_reaction_id"] == "1678524932434102"
        assert variables["input"]["feedback_source"] == "OBJECT"

    def test_comments_unreact_command(self, monkeypatch, capsys):
        session = StubSession(
            {"CometUFIFeedbackReactMutation": {"data": {}}})
        code, _ = self._run(
            monkeypatch, capsys, session,
            ["comments", "unreact", "--feedback-id", FEEDBACK_COMMENT])
        assert code == 0
        _, _, variables = session.graphql.calls[0]
        assert variables["input"]["feedback_reaction_id"] == "0"

    def test_comments_edit_command(self, monkeypatch, capsys):
        session = StubSession(
            {"useCometUFIEditCommentMutation": {"data": {}}})
        code, _ = self._run(
            monkeypatch, capsys, session,
            ["comments", "edit", "--comment-id",
             "3937341566408979_3938247886318347", "--text", "new body"])
        assert code == 0
        _, doc_id, variables = session.graphql.calls[0]
        assert doc_id == EDIT_COMMENT_DOC_ID
        assert variables["input"]["message"]["text"] == "new body"

    def test_comments_delete_command(self, monkeypatch, capsys):
        session = StubSession(
            {"CometUFIDeleteCommentMutation": {"data": {}}})
        code, _ = self._run(
            monkeypatch, capsys, session,
            ["comments", "delete", "--comment-id", COMMENT_SAMPLE_B64])
        assert code == 0
        _, doc_id, variables = session.graphql.calls[0]
        assert doc_id == DELETE_COMMENT_DOC_ID
        assert variables["input"]["comment_id"] == COMMENT_SAMPLE_B64
        assert variables["renderLocation"] == "group"

    def test_video_feed_command(self, monkeypatch, capsys):
        session = StubSession(
            {"FBUnifiedVideoRootWithEntrypointQuery": WATCH_PAYLOAD})
        code, out = self._run(
            monkeypatch, capsys, session,
            ["video", "feed", "--limit", "10"], page_html=WATCH_HTML)
        assert code == 0
        assert "videos: 2" in out
        assert "Reel Owner" in out
        # human print: "[<id>] <title head> — <owner name or id> <url>"
        assert "[1090654616829573] (no title) — Reel Owner" in out
        assert '"end_cursor": "AQHTCHAININGTOKEN1234567890"' in out

    def test_video_feed_command_titles_from_story_wrapper(self, monkeypatch,
                                                          capsys):
        session = StubSession(
            {"FBUnifiedVideoRootWithEntrypointQuery": WATCH_STORY_PAYLOAD})
        code, out = self._run(
            monkeypatch, capsys, session,
            ["video", "feed", "--limit", "10"], page_html=WATCH_HTML)
        assert code == 0
        assert ("[2111827749761133] Watch this reel — "
                "10000000000000006 "
                "https://www.facebook.com/reel/2111827749761133/") in out
        assert ("[1609244043946552] Person F \u00b7 Original audio — "
                "Person F "
                "https://www.facebook.com/reel/1609244043946552/") in out

    def test_video_badge_command(self, monkeypatch, capsys):
        session = StubSession(
            {"useCometWatchBadgeCountQuery": BADGE_PAYLOAD})
        code, out = self._run(
            monkeypatch, capsys, session, ["video", "badge"])
        assert code == 0
        assert "unseen videos: 3" in out
        assert '"unseen": 3' in out


# --------------------------------------------------- template-integrity checks
class TestTemplateIntegrity:
    """Pins that the captured mutation assets still carry the templates
    the replay paths depend on."""

    def test_captured_react_templates_present(self):
        doc = load_asset("captured_mutations.json")
        names = [m["friendly_name"] for m in doc["mutations"]]
        assert names.count("CometUFIFeedbackReactMutation") == 2

    def test_feedback_comment_id_roundtrip(self):
        """The comment-level feedback b64 decodes to the (post, comment)
        pair prefix (docs/04 §7)."""
        import base64
        inner = base64.b64decode(
            FEEDBACK_COMMENT + "=" * (-len(FEEDBACK_COMMENT) % 4)).decode()
        assert inner == "feedback:1532930458881683_2039338256767947"
        assert CommentID.from_b64(COMMENT_SAMPLE_B64).decoded == \
            ("1532930458881683", "2039338256767947")
        assert FeedbackID.from_numeric("3937024329774036").raw == \
            FEEDBACK_SAMPLE_POST
