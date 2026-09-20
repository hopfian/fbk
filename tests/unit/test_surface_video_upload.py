"""VIDEO UPLOAD surface service tests — offline, canned-wire replays.

Every wire shape asserted here is the bundle-decoded one documented in
surfaces/video_upload.py: the three-stage rupload ingest (start form POST ->
rupload offset GET + chunk POST -> receive form POST) and the
ComposerStoryCreateMutation publish with the minimal VIDEO attachments
element. Raw HTTP is faked at the service's _raw_post/_raw_get seams; the
config queries and the composer mutation replay through StubSession.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from fakes import StubGraphQLClient, StubSession

from domain.common import Privacy
from surfaces.base import load_template
from surfaces.video_upload import (
    COMPOSER_MUTATION,
    START_URI_DEFAULT,
    VideoUploadError,
    VideoUploadService,
    _form_encode,
    _rupload_session_key,
    _video_attachment,
)

COMPOSER_DOC_ID = "28778531428503134"
STUB_USER_ID = "12345678901234"
START_URI = "https://vupload-edge.facebook.com/ajax/video/upload/requests/start/"
RECEIVE_URI = "https://vupload-edge.facebook.com/ajax/video/upload/requests/receive/"
RUPLOAD_HOST = "rupload-ccu2-1.up.facebook.com"
CONFIG_QUERY = "useComposerVideoUploaderConfigQuery"
SERVER_CONFIG_QUERY = "MediaUploadFBDefaultServerConfigurationRetrieverQuery"


def _composer_config_response() -> dict[str, Any]:
    blob = {
        "source": "composer",
        "composer_entry_point_ref": "feed",
        "start_uri": "https://vupload2.facebook.com/ajax/video/upload/requests/start/",
        "chunk_start_uri": START_URI,
        "receive_uri": "https://vupload2.facebook.com/ajax/video/upload/requests/receive/",
        "chunk_receive_uri": RECEIVE_URI,
        "resumable_service_name": "rupload-from-blob",
        "resumable_service_domain": "facebook.com",
    }
    return {"data": {"viewer": {
        "comet_composer_video_uploader_config": json.dumps(blob)}}}


def _server_config_response() -> dict[str, Any]:
    return {"data": {"media_upload_config": {"network_upload_service": {
        "default": {"service_name": "rupload", "service_domain": "facebook.com"},
        "targeted": {"service_name": "rupload-ccu2-1.up",
                     "service_domain": "facebook.com"},
    }}}}


class FakeRawResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.status_code = status_code
        self.text = text
        self.content = text.encode()
        self.url = ""


START_OK = FakeRawResponse(
    'for (;;);{"payload":{"video_id":"987654321000111","start_offset":0,'
    '"end_offset":9,"upload_session_id":"sess-4242","region_hint":"atn1",'
    '"skip_upload":false}}')
START_SKIP = FakeRawResponse(
    'for (;;);{"payload":{"video_id":"987654321000111","skip_upload":true}}')
RUPLOAD_OFFSET_0 = FakeRawResponse('{"offset":0}')
RUPLOAD_OFFSET_3 = FakeRawResponse('{"offset":3}')
RUPLOAD_HANDLE = FakeRawResponse('{"h":"everstore:://chunk-handle-1"}')
RECEIVE_OK = FakeRawResponse(
    'for (;;);{"payload":{"video_id":"987654321000111","status":"ready"}}')


class RawRecorder:
    """_raw_post/_raw_get monkeypatch: records calls, returns queued responses."""

    def __init__(self, responses: list[FakeRawResponse]):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, Any, dict]] = []

    def post(self, url: str, data: Any, headers: dict):
        self.calls.append(("POST", url, data, dict(headers)))
        return self.responses.pop(0)

    def get(self, url: str, headers: dict):
        self.calls.append(("GET", url, None, dict(headers)))
        return self.responses.pop(0)


def _service(stub: StubSession,
             responses: list[FakeRawResponse]) -> tuple[VideoUploadService, RawRecorder]:
    rec = RawRecorder(responses)
    service = VideoUploadService(stub)
    service._raw_post = rec.post  # type: ignore[method-assign]
    service._raw_get = rec.get    # type: ignore[method-assign]
    return service, rec


def _stubbed() -> StubSession:
    return StubSession(responses={
        CONFIG_QUERY: _composer_config_response(),
        SERVER_CONFIG_QUERY: _server_config_response(),
    })


def _clip(tmp_path: Path) -> Path:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"012345678")
    return clip


def _parse_form(body: bytes) -> dict[str, str]:
    """PHPQuerySerializer's inverse: bare keys map to ""."""
    out: dict[str, str] = {}
    for part in body.decode().split("&"):
        if "=" in part:
            key, _, value = part.partition("=")
        else:
            key, value = part, ""
        out[unquote(key)] = unquote(value)
    return out


# ------------------------------------------------------------- form serializer
class TestFormEncode:
    """Pins the PHPQuerySerializer form semantics: bare keys map to "",
    booleans spell true/false, values are url-encoded."""

    def test_php_query_serializer_semantics(self):
        body = _parse_form(_form_encode({
            "supports_chunking": True, "has_file_been_replaced": False,
            "file_size": 9, "waterfall_id": "abc",
            "composer_dialog_version": None,
        }))
        assert body == {
            "supports_chunking": "true",
            "has_file_been_replaced": "false",
            "file_size": "9",
            "waterfall_id": "abc",
            "composer_dialog_version": "",
        }

    def test_values_are_url_encoded(self):
        body = _parse_form(_form_encode({"k": "a b&c=d"}))
        assert body["k"] == "a b&c=d"


# ------------------------------------------------------------- session key
class TestSessionKey:
    """Pins the rupload session-key shape: the md5-offset composite,
    deterministic per (file, session), session-unique."""

    def test_key_shape_is_md5_offsets(self, tmp_path: Path):
        clip = _clip(tmp_path)
        key = _rupload_session_key(clip, "video/mp4", 9, "sess-1", 0, 9)
        assert re.fullmatch(r"[0-9a-f]{32}-0-9", key)
        again = _rupload_session_key(clip, "video/mp4", 9, "sess-1", 0, 9)
        assert key == again
        other = _rupload_session_key(clip, "video/mp4", 9, "sess-2", 0, 9)
        assert other != key


# ------------------------------------------------------------- upload_video
class TestUploadVideo:
    """Pins the three-stage ingest wire (start form -> rupload offset/chunk
    -> receive form), config plumbing, resume offsets, and the typed
    VideoUploadError taxonomy."""

    def test_full_ingest_wire(self, tmp_path: Path):
        clip = _clip(tmp_path)
        stub = _stubbed()
        service, rec = _service(stub, [START_OK, RUPLOAD_OFFSET_0,
                                       RUPLOAD_HANDLE, RECEIVE_OK])

        result = service.upload_video(clip)

        assert result["video_id"] == "987654321000111"
        assert result["chunk_handle"] == "everstore:://chunk-handle-1"
        assert [c[0] for c in rec.calls] == ["POST", "GET", "POST", "POST"]
        method, url, data, headers = rec.calls[0]
        assert url.startswith(START_URI + "?")
        query = {k: v[0] for k, v in
                 parse_qs(urlparse(url).query).items()}
        assert query["av"] == STUB_USER_ID
        assert query["__user"] == STUB_USER_ID
        assert query["__a"] == "1"
        assert query["fb_dtsg"] == "NAfTEST:1:1789723298"
        assert query["lsd"] == "TESTLSD00000000000"
        assert query["jazoest"].isdigit()
        assert headers["X_FB_VIDEO_WATERFALL_ID"] == result["session_id"]

        form = _parse_form(data)
        assert form["waterfall_id"] == result["session_id"]
        assert form["target_id"] == STUB_USER_ID
        assert form["source"] == "composer"
        assert form["composer_entry_point_ref"] == "feed"
        assert form["supports_chunking"] == "true"
        assert form["supports_file_api"] == "true"
        assert form["file_size"] == "9"
        assert form["file_extension"] == "mp4"
        assert form["partition_start_offset"] == "0"
        assert form["partition_end_offset"] == "9"
        assert form["has_file_been_replaced"] == "false"
        assert form["composer_dialog_version"] == ""
        assert form["video_publisher_action_source"] == ""
        assert form["composer_work_shared_draft_mode"] == ""

        method, url, _, _ = rec.calls[1]
        assert method == "GET"
        parsed = urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.netloc == RUPLOAD_HOST
        assert parsed.path.startswith("/fb_video/")
        assert re.fullmatch(r"/fb_video/[0-9a-f]{32}-0-9", parsed.path)
        get_query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        assert get_query["__user"] == STUB_USER_ID
        assert "fb_dtsg" not in get_query

        method, url, data, headers = rec.calls[2]
        assert method == "POST"
        assert urlparse(url).netloc == RUPLOAD_HOST
        assert urlparse(url).path == parsed.path
        post_query = {k: v[0] for k, v in
                      parse_qs(urlparse(url).query).items()}
        assert post_query["fb_dtsg"] == "NAfTEST:1:1789723298"
        assert post_query["lsd"] == "TESTLSD00000000000"
        assert headers["X-Entity-Name"] == "clip.mp4"
        assert headers["X-Entity-Type"] == "video/mp4"
        assert headers["X-Entity-Length"] == "9"
        assert headers["Offset"] == "0"
        assert headers["start_offset"] == "0"
        assert headers["end_offset"] == "9"
        assert headers["composer_session_id"] == result["session_id"]
        assert headers["id"] == "sess-4242"
        assert headers["product_media_id"] == "987654321000111"
        assert headers["X-Total-Asset-Size"] == "9"
        assert headers["X-FB-Region"] == "atn1"
        assert data == b"012345678"

        method, url, data, headers = rec.calls[3]
        assert method == "POST"
        assert url.startswith(RECEIVE_URI + "?")
        form = _parse_form(data)
        assert form["waterfall_id"] == result["session_id"]
        assert form["video_id"] == "987654321000111"
        assert form["supports_upload_service"] == "true"
        assert form["fbuploader_video_file_chunk"] == "everstore:://chunk-handle-1"
        assert form["start_offset"] == "0"
        assert form["end_offset"] == "9"
        assert form["upload_speed"].isdigit()
        assert headers["X_FB_VIDEO_WATERFALL_ID"] == result["session_id"]
        assert result["receive"]["status"] == "ready"

    def test_config_queries_wire(self, tmp_path: Path):
        clip = _clip(tmp_path)
        stub = _stubbed()
        service, _ = _service(stub, [START_OK, RUPLOAD_OFFSET_0,
                                     RUPLOAD_HANDLE, RECEIVE_OK])
        service.upload_video(clip)
        calls = {c[0]: c for c in stub.graphql.calls}
        assert CONFIG_QUERY in calls
        assert SERVER_CONFIG_QUERY in calls
        _, _, variables = calls[CONFIG_QUERY]
        assert variables == {"actorID": STUB_USER_ID,
                             "entryPoint": "feed_composer", "targetID": ""}
        _, _, variables = calls[SERVER_CONFIG_QUERY]
        assert variables == {"source_type": "composer"}

    def test_server_config_targeted_wins_over_blob(self, tmp_path: Path):
        clip = _clip(tmp_path)
        stub = _stubbed()
        service, rec = _service(stub, [START_OK, RUPLOAD_OFFSET_0,
                                       RUPLOAD_HANDLE, RECEIVE_OK])
        service.upload_video(clip)
        assert urlparse(rec.calls[1][1]).netloc == RUPLOAD_HOST

    def test_config_fallback_uses_bundle_defaults(self, tmp_path: Path):
        clip = _clip(tmp_path)
        stub = StubSession()
        stub.graphql = StubGraphQLClient({}, strict=False)
        service, rec = _service(stub, [START_OK, RUPLOAD_OFFSET_0,
                                       RUPLOAD_HANDLE, RECEIVE_OK])
        service.upload_video(clip)
        assert rec.calls[0][1].startswith(START_URI_DEFAULT + "?")
        assert urlparse(rec.calls[1][1]).netloc == RUPLOAD_HOST

    def test_skip_upload_skips_chunk_and_receive(self, tmp_path: Path):
        clip = _clip(tmp_path)
        stub = _stubbed()
        service, rec = _service(stub, [START_SKIP])

        result = service.upload_video(clip)

        assert len(rec.calls) == 1
        assert result["video_id"] == "987654321000111"
        assert result["chunk_handle"] is None
        assert result["receive"] == {"skip_upload": True}

    def test_resume_offset_slices_the_body(self, tmp_path: Path):
        clip = _clip(tmp_path)
        stub = _stubbed()
        service, rec = _service(stub, [START_OK, RUPLOAD_OFFSET_3,
                                       RUPLOAD_HANDLE, RECEIVE_OK])
        service.upload_video(clip)
        _, _, data, headers = rec.calls[2]
        assert headers["Offset"] == "3"
        assert data == b"345678"

    def test_offset_probe_failure_defaults_to_zero(self, tmp_path: Path):
        clip = _clip(tmp_path)
        stub = _stubbed()
        service, rec = _service(stub, [START_OK,
                                       FakeRawResponse("<html>", 404),
                                       RUPLOAD_HANDLE, RECEIVE_OK])
        result = service.upload_video(clip)
        _, _, data, headers = rec.calls[2]
        assert headers["Offset"] == "0"
        assert data == b"012345678"
        assert result["chunk_handle"] == "everstore:://chunk-handle-1"

    def test_start_without_video_id_raises(self, tmp_path: Path):
        clip = _clip(tmp_path)
        stub = _stubbed()
        service, _ = _service(stub, [FakeRawResponse(
            'for (;;);{"payload":{"start_offset":0}}')])
        with pytest.raises(VideoUploadError):
            service.upload_video(clip)

    def test_start_http_error_raises(self, tmp_path: Path):
        clip = _clip(tmp_path)
        stub = _stubbed()
        service, _ = _service(stub, [FakeRawResponse("nope", 502)])
        with pytest.raises(VideoUploadError) as err:
            service.upload_video(clip)
        assert err.value.code == 502

    def test_start_error_envelope_raises(self, tmp_path: Path):
        clip = _clip(tmp_path)
        stub = _stubbed()
        service, _ = _service(stub, [FakeRawResponse(
            'for (;;);{"error":1357004,"errorSummary":"Not logged in"}')])
        with pytest.raises(VideoUploadError):
            service.upload_video(clip)

    def test_chunk_without_handle_raises(self, tmp_path: Path):
        clip = _clip(tmp_path)
        stub = _stubbed()
        service, _ = _service(stub, [START_OK, RUPLOAD_OFFSET_0,
                                     FakeRawResponse('{"offset":9}')])
        with pytest.raises(VideoUploadError):
            service.upload_video(clip)

    def test_receive_without_payload_raises(self, tmp_path: Path):
        clip = _clip(tmp_path)
        stub = _stubbed()
        service, _ = _service(stub, [START_OK, RUPLOAD_OFFSET_0,
                                     RUPLOAD_HANDLE,
                                     FakeRawResponse("for (;;);{}")])
        with pytest.raises(VideoUploadError):
            service.upload_video(clip)

    def test_missing_file_raises(self, tmp_path: Path):
        stub = _stubbed()
        service, _ = _service(stub, [])
        with pytest.raises(VideoUploadError):
            service.upload_video(tmp_path / "absent.mp4")

    def test_empty_file_raises(self, tmp_path: Path):
        clip = tmp_path / "empty.mp4"
        clip.write_bytes(b"")
        stub = _stubbed()
        service, _ = _service(stub, [])
        with pytest.raises(VideoUploadError):
            service.upload_video(clip)

    def test_upload_only_never_touches_composer_mutation(self, tmp_path: Path):
        clip = _clip(tmp_path)
        stub = _stubbed()
        service, _ = _service(stub, [START_OK, RUPLOAD_OFFSET_0,
                                     RUPLOAD_HANDLE, RECEIVE_OK])
        service.upload_video(clip)
        composer_calls = [c for c in stub.graphql.calls
                          if c[0] == COMPOSER_MUTATION]
        assert composer_calls == []


# ------------------------------------------------------------------ post_video
class TestPostVideo:
    """Pins the composer publish after ingest: the decoded VIDEO
    attachments element, privacy mapping, and session-id threading."""

    def _stub(self) -> StubSession:
        return StubSession(responses={
            CONFIG_QUERY: _composer_config_response(),
            SERVER_CONFIG_QUERY: _server_config_response(),
            COMPOSER_MUTATION: {
                "data": {"story_create": {"story": {"id": "UzpfSToz"}}}},
        })

    def _post(self, stub: StubSession, clip: Path, caption: str,
              privacy: Privacy, group_id: str | None = None) -> Any:
        service, _ = _service(stub, [START_OK, RUPLOAD_OFFSET_0,
                                     RUPLOAD_HANDLE, RECEIVE_OK])
        return service.post_video(clip, caption, privacy, group_id=group_id)

    def test_attachments_wire_the_decoded_video_element(self, tmp_path: Path):
        clip = _clip(tmp_path)
        stub = self._stub()

        result = self._post(stub, clip, "my clip", Privacy.PRIVATE)

        calls = [c for c in stub.graphql.calls
                 if c[0] == COMPOSER_MUTATION]
        assert len(calls) == 1
        friendly, doc_id, variables = calls[0]
        assert friendly == COMPOSER_MUTATION
        assert doc_id == COMPOSER_DOC_ID
        assert variables["input"]["attachments"] == [
            {"video": _video_attachment("987654321000111")}]
        assert variables["input"]["message"]["text"] == "my clip"
        assert variables["input"]["audience"]["privacy"]["base_state"] == "SELF"
        assert variables["input"]["actor_id"] == STUB_USER_ID
        assert variables["input"]["logging"]["composer_session_id"] == \
            result["upload"]["session_id"]
        assert variables["feedLocation"] == "NEWSFEED"
        assert variables["renderLocation"] == "homepage_stream"
        assert variables["input"]["composer_entry_point"] == "inline_composer"
        assert variables["input"]["idempotence_token"].endswith("_FEED")

        assert result["video_id"] == "987654321000111"
        assert result["publish"]["data"]["story_create"]["story"]["id"] == "UzpfSToz"

    def test_video_attachment_element_shape(self):
        element = _video_attachment("123")
        assert element == {
            "id": "123",
            "audio_descriptions": None,
            "transcriptions": None,
            "notify_when_processed": True,
            "was_created_via_unified_video_flow": None,
            "additional_video_metadata": {},
        }

    def test_privacy_base_states(self, tmp_path: Path):
        clip = _clip(tmp_path)
        for privacy, base in ((Privacy.PUBLIC, "EVERYONE"),
                              (Privacy.FRIENDS, "FRIENDS"),
                              (Privacy.PRIVATE, "SELF")):
            stub = self._stub()
            self._post(stub, clip, f"post {privacy}", privacy)
            _, _, variables = next(c for c in stub.graphql.calls
                                   if c[0] == COMPOSER_MUTATION)
            assert variables["input"]["audience"]["privacy"]["base_state"] == base

    def test_group_id_wiring(self, tmp_path: Path):
        clip = _clip(tmp_path)
        stub = self._stub()
        self._post(stub, clip, "to the group", Privacy.FRIENDS,
                   group_id="12345678901234567")
        _, _, variables = next(c for c in stub.graphql.calls
                              if c[0] == COMPOSER_MUTATION)
        assert variables["groupID"] == "12345678901234567"

    def test_ingest_then_exactly_one_mutation(self, tmp_path: Path):
        clip = _clip(tmp_path)
        stub = self._stub()
        self._post(stub, clip, "order", Privacy.FRIENDS)
        composer_calls = [c for c in stub.graphql.calls
                          if c[0] == COMPOSER_MUTATION]
        assert len(composer_calls) == 1

    def test_reels_remix_block_rides_the_capture_verbatim(self,
                                                           tmp_path: Path):
        """REELS PARKED pin: the captured input's ``reels_remix`` block is
        relay-provider/GK gating that rides verbatim in every feed
        composer publish — it is NOT a reels-publish switch. The video
        post replays it untouched; no reels variant is invented (a reels
        publish needs a reels-composer capture that does not exist —
        see the module docstring's REELS note in
        surfaces/video_upload.py)."""
        clip = _clip(tmp_path)
        stub = self._stub()

        self._post(stub, clip, "my clip", Privacy.FRIENDS)
        _, _, variables = next(c for c in stub.graphql.calls
                               if c[0] == COMPOSER_MUTATION)
        template = load_template("captured_composer.json", COMPOSER_MUTATION)
        assert variables["input"]["reels_remix"] == \
            template["input"]["reels_remix"]
        # the publish stays the captured FEED composer context
        assert variables["feedLocation"] == "NEWSFEED"
        assert variables["renderLocation"] == "homepage_stream"
