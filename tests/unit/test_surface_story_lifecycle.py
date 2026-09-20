"""Story lifecycle tests: create/viewers/reply + the catalog reads.

Pins every wire shape decoded from the /stories/create/ chunk graph
(2026-09-20 archaeology; surfaces/stories.py CALIBRATION NOTES) and the
live-verified composer-root catalog reads.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from fakes import StubSession

from surfaces.stories import (
    COMPOSER_ROOT_DOC_ID,
    COMPOSER_ROOT_QUERY,
    COMPOSER_ROOT_VARIABLES,
    STORY_CREATE_DOC_ID,
    STORY_CREATE_MUTATION,
    STORY_PRIVACY_QUERY,
    STORY_REPLY_DOC_ID,
    STORY_REPLY_MUTATION,
    VIEWERS_DOC_ID,
    VIEWERS_QUERY,
    StoriesService,
)

UID = "12345678901234"
STORY_ID = base64.b64encode(
    f"S:_I{UID}:122109000000000:122109000000000".encode()).decode()


def _create_response() -> dict:
    return {"data": {"story_create": {
        "story_id": "122109000000000",
        "logging_token": "LTOKEN",
        "items": [{"story": {"id": STORY_ID}}],
    }}}


def _composer_root_payload() -> dict:
    # live paths (2026-09-20): the preset collections ride data directly;
    # the font/audience data ride data.viewer
    return {"data": {
        "viewer": {
            "unified_stories_setting": {"audience_mode": "FRIENDS",
                                        "id": "122100842433458110"},
            "inspirations_data": {"custom_font": {"nodes": [
                {"id": "240532164481720", "font_name": "Barlow SemiBold",
                 "font_url": "https://x/font.ttf"},
            ]}},
        },
        "visual_composer_satp_collections": [{"presets": [
            {"preset_id": "401372137331149",
             "inspirations_custom_font_object": None,
             "portrait_background_image": {"uri": "https://x/bg.jpg"}},
            {"preset_id": "276148839666236",
             "inspirations_custom_font_object": {
                 "id": "233490655168261",
                 "font_postscript_name": "FacebookSansApp-Regular"},
             "portrait_background_image": None},
        ]}],
    }}


def _privacy_payload() -> dict:
    return {"data": {"viewer": {"stories_data": {"audience_mode_list": [
        {"unified_stories_audience_mode": "PUBLIC",
         "description": "Anyone on Facebook or Messenger", "header": "Public"},
        {"unified_stories_audience_mode": "FRIENDS",
         "description": "Only your Facebook friends", "header": "Friends"},
    ]}}}}


class TestCreateText:
    """Pins the decoded SATP input: base pipeline fields + the SATP trio."""

    def _service(self) -> tuple[StoriesService, StubSession]:
        session = StubSession(
            {STORY_CREATE_MUTATION: _create_response()})
        return StoriesService(session), session

    def test_create_text_decoded_input(self):
        service, session = self._service()
        service.create_text("hello story")

        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == STORY_CREATE_MUTATION
        assert doc_id == "26770527039211553" == STORY_CREATE_DOC_ID
        inp = variables["input"]
        # the decoded pipeline's base fields; the privacy transformer
        # (qex-gated) always seeds audience_info with its two client flags
        assert inp["audiences"] == [{"stories": {"self": {
            "target_id": UID,
            "audience_info": {
                "client_has_per_story_experience": True,
                "disable_server_fallback_to_default_privacy": False,
            },
        }}}]
        assert inp["audiences_is_complete"] is True
        assert inp["source"] == "WWW"
        assert inp["navigation_data"] is None
        assert isinstance(inp["tracking"], list)
        assert set(inp["logging"]) == {"composer_session_id"}
        # the SATP transformer trio
        assert inp["message"] == {"ranges": [], "text": "hello story"}
        # the GenAI label transformer runs unconditionally
        assert inp["ai_generated_self_disclosure_metadata"] == {
            "was_self_disclosed_as_ai_generated": False}

    def test_create_text_with_privacy_row(self):
        service, session = self._service()
        service.create_text("private story", privacy="PRIVATE")

        inp = session.graphql.calls[0][2]["input"]
        self_entry = inp["audiences"][0]["stories"]["self"]
        assert self_entry["target_id"] == UID
        info = self_entry["audience_info"]
        # the privacy transformer's decoded shape: the SAME privacy_row_input
        # as the composer's privacy-save mutation (base_state/allow/deny/
        # tag_expansion_state)
        assert info["story_privacy_row"] == {
            "allow": [], "base_state": "SELF", "deny": [],
            "tag_expansion_state": "TAGGEES"}
        assert info["disable_server_fallback_to_default_privacy"] is True
        assert info["client_has_per_story_experience"] is True

    def test_create_text_with_font_and_preset(self):
        service, session = self._service()
        service.create_text("styled", font_id="240532164481720",
                            preset_id="401372137331149")
        inp = session.graphql.calls[0][2]["input"]
        assert inp["text_format_metadata"] == {
            "inspirations_custom_font_id": "240532164481720"}
        assert inp["text_format_preset_id"] == "401372137331149"

    def test_create_text_ai_label(self):
        service, session = self._service()
        service.create_text("ai story", ai_label=True)
        inp = session.graphql.calls[0][2]["input"]
        assert inp["ai_generated_self_disclosure_metadata"] == {
            "was_self_disclosed_as_ai_generated": True}

    def test_create_rejects_unknown_audience(self):
        service, _session = self._service()
        with pytest.raises(ValueError, match="unknown audience"):
            service.create_text("x", privacy="everyone-but-bob")


class TestCreateMedia:
    """Pins the photo/video attachment shapes (upload delegate + the
    decoded photo/video transformer fields)."""

    def test_create_photo_attachment(self, monkeypatch):
        session = StubSession({STORY_CREATE_MUTATION: _create_response()})
        service = StoriesService(session)

        captured: dict = {}

        def fake_upload(self, path):
            captured["path"] = path
            return {"photo_id": "122112047121458110"}

        monkeypatch.setattr("surfaces.upload.UploadService.upload_photo",
                            fake_upload)
        result = service.create_photo(Path("C:/pics/test.png"))

        assert captured["path"] == Path("C:/pics/test.png")
        assert result["photo_id"] == "122112047121458110"
        inp = session.graphql.calls[0][2]["input"]
        # the decoded photo transformer: attachments[].photo with the
        # uploaded fbid + an empty overlays list
        assert inp["attachments"] == [
            {"photo": {"id": "122112047121458110", "overlays": []}}]

    def test_create_video_attachment(self, monkeypatch):
        session = StubSession({STORY_CREATE_MUTATION: _create_response()})
        service = StoriesService(session)

        def fake_upload(self, path):
            return {"video_id": "122112048999458110"}

        monkeypatch.setattr(
            "surfaces.video_upload.VideoUploadService.upload_video",
            fake_upload)
        result = service.create_video(Path("C:/vids/test.mp4"))

        assert result["video_id"] == "122112048999458110"
        inp = session.graphql.calls[0][2]["input"]
        # the decoded video transformer: attachments[].video.id only
        assert inp["attachments"] == [{"video": {"id": "122112048999458110"}}]


class TestViewers:
    """Pins the viewer-sheet query: decoded args + the tolerant walker."""

    def test_viewers_variables_and_parse(self):
        payload = {"data": {"viewer": {"viewers": {"edges": [
            {"node": {"id": "1001", "name": "Alice", "__typename": "User"}},
            {"node": {"id": "1002", "name": "Bob", "__typename": "User"}},
        ]}}}}
        session = StubSession({VIEWERS_QUERY: payload})
        rows = StoriesService(session).viewers(STORY_ID, limit=10)

        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == VIEWERS_QUERY
        assert doc_id == "38550750487903492" == VIEWERS_DOC_ID
        # the decoded LocalArguments: cursor/id/viewerCount
        assert variables == {"cursor": None, "id": STORY_ID, "viewerCount": 10}
        assert [r["user_id"] for r in rows] == ["1001", "1002"]
        assert rows[0]["name"] == "Alice"

    def test_viewers_empty_degrades(self):
        session = StubSession({VIEWERS_QUERY: {"data": {}}})
        assert StoriesService(session).viewers(STORY_ID) == []


class TestReply:
    """Pins the decoded text-reply input."""

    def test_reply_input(self):
        session = StubSession({STORY_REPLY_MUTATION: {"data": {}}})
        StoriesService(session).reply(STORY_ID, "nice story!")

        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == STORY_REPLY_MUTATION
        assert doc_id == "27836479672650087" == STORY_REPLY_DOC_ID
        # the decoded useStoriesSendReply TEXT commit: message + story id +
        # the reply type enum
        assert variables == {"input": {
            "message": {"ranges": [], "text": "nice story!"},
            "story_id": STORY_ID,
            "story_reply_type": "TEXT",
        }}


class TestCatalogs:
    """Pins the live-verified composer-root + privacy-selector reads."""

    def test_presets(self):
        session = StubSession({COMPOSER_ROOT_QUERY: _composer_root_payload()})
        rows = StoriesService(session).presets()

        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == COMPOSER_ROOT_QUERY
        assert doc_id == COMPOSER_ROOT_DOC_ID
        assert variables == COMPOSER_ROOT_VARIABLES
        assert rows == [
            {"preset_id": "401372137331149", "font_id": None,
             "has_background_image": True},
            {"preset_id": "276148839666236", "font_id": "233490655168261",
             "has_background_image": False},
        ]

    def test_fonts(self):
        session = StubSession({COMPOSER_ROOT_QUERY: _composer_root_payload()})
        rows = StoriesService(session).fonts()
        assert rows == [{"id": "240532164481720", "name": "Barlow SemiBold",
                         "url": "https://x/font.ttf"}]

    def test_audience(self):
        session = StubSession({
            COMPOSER_ROOT_QUERY: _composer_root_payload(),
            STORY_PRIVACY_QUERY: _privacy_payload(),
        })
        state = StoriesService(session).audience()

        assert state["default_mode"] == "FRIENDS"
        assert state["modes"] == [
            {"mode": "PUBLIC", "header": "Public",
             "description": "Anyone on Facebook or Messenger"},
            {"mode": "FRIENDS", "header": "Friends",
             "description": "Only your Facebook friends"},
        ]
        assert len(session.graphql.calls) == 2

    def test_catalogs_degrade_on_empty(self):
        session = StubSession({
            COMPOSER_ROOT_QUERY: {"data": {}},
            STORY_PRIVACY_QUERY: {"data": {}},
        })
        service = StoriesService(session)
        assert service.presets() == []
        assert service.fonts() == []
        assert service.audience() == {"default_mode": None, "modes": []}


# ------------------------------------------------------------ command wiring
class TestStoryCommands:
    """Pins the stories CLI wiring with the session stubbed."""

    def _run(self, monkeypatch, capsys, session, argv):
        import argparse as ap

        from commands import stories as stories_cmd
        parser = ap.ArgumentParser(prog="fbk-test")
        sub = parser.add_subparsers(dest="command", required=True)
        stories_cmd.register(sub)
        args = parser.parse_args(["stories", *argv])
        monkeypatch.setattr(stories_cmd, "new_session", lambda _a: session)
        code = args.fn(args)
        captured = capsys.readouterr().out
        return code, captured

    def test_create_requires_content(self, monkeypatch, capsys):
        code, out = self._run(monkeypatch, capsys, StubSession({}),
                               ["create"])
        assert code == 2
        assert "--text" in out

    def test_create_text_emits_story_id(self, monkeypatch, capsys):
        session = StubSession({STORY_CREATE_MUTATION: _create_response()})
        code, out = self._run(monkeypatch, capsys, session,
                              ["create", "--text", "hello"])
        assert code == 0
        assert "story created: text" in out
        assert STORY_ID in out

    def test_viewers_human_output(self, monkeypatch, capsys):
        payload = {"data": {"viewer": {"viewers": {"edges": [
            {"node": {"id": "1001", "name": "Alice"}}]}}}}
        session = StubSession({VIEWERS_QUERY: payload})
        code, out = self._run(monkeypatch, capsys, session,
                              ["viewers", "--story-id", STORY_ID])
        assert code == 0
        assert "viewers: 1" in out
        assert "Alice" in out

    def test_audience_command_marks_default(self, monkeypatch, capsys):
        session = StubSession({
            COMPOSER_ROOT_QUERY: _composer_root_payload(),
            STORY_PRIVACY_QUERY: _privacy_payload(),
        })
        code, out = self._run(monkeypatch, capsys, session, ["audience"])
        assert code == 0
        assert "default story audience: FRIENDS" in out
        assert "Friends *" in out

    def test_json_mode_carries_fields(self, monkeypatch, capsys):
        session = StubSession({
            COMPOSER_ROOT_QUERY: _composer_root_payload(),
        })
        code, out = self._run(monkeypatch, capsys, session, ["fonts", "--json"])
        assert code == 0
        payload = json.loads(out)
        assert payload["fonts"][0]["name"] == "Barlow SemiBold"
