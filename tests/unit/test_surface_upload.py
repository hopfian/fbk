"""UPLOAD surface service tests — offline, against the REAL captured assets.

The photo-upload wire shapes here are the live-decoded ones documented in
surfaces/upload.py (docs/02 upload family + docs/15): the
react_composer multipart ingest endpoint and the
ComposerStoryCreateMutation attachments wiring. Raw HTTP is faked at the
service's `_raw_post` seam; the composer side replays the real
assets/captured_composer.json template through StubSession.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

import pytest
from fakes import StubSession

from domain.common import Privacy
from surfaces.upload import (
    COMPOSER_MUTATION,
    PHOTO_UPLOAD_ENDPOINT,
    UploadError,
    UploadService,
    _build_multipart,
    _parse_async_payload,
)

COMPOSER_DOC_ID = "28778531428503134"   # live-verified (docs/15 §P3)
STUB_USER_ID = "12345678901234"


# --------------------------------------------------------------------- helpers
def _tiny_png() -> bytes:
    """A valid 1x1 grayscale PNG, generated at runtime (no binary fixtures)."""
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    raw = b"\x00\x80"  # filter byte + one gray pixel

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def _tiny_gif() -> bytes:
    """A valid 1x1 GIF89a, generated at runtime (no binary fixtures).

    Header + logical screen (1x1, GCT of 2 colors) + one image
    descriptor + the minimal LZW sub-block + trailer — every field per
    the GIF89a spec, small enough to assert byte-for-byte.
    """
    return (
        b"GIF89a"
        + struct.pack("<HH", 1, 1)   # logical screen: 1x1
        + b"\x80\x00\x00"            # GCT flag (2 colors), bg 0, aspect 0
        + b"\x00\x00\x00"            # GCT color 0
        + b"\xff\xff\xff"            # GCT color 1
        + b","                       # image descriptor separator
        + struct.pack("<HHHH", 0, 0, 1, 1)
        + b"\x00"                    # no local color table
        + b"\x02"                    # LZW minimum code size
        + b"\x02\x44\x01"            # one data sub-block (2 code bytes)
        + b"\x00"                    # sub-block terminator
        + b";"                       # trailer
    )


class FakeRawResponse:
    """Response stand-in for the _raw_post seam (text + status + url)."""

    def __init__(self, text: str, status_code: int = 200):
        self.status_code = status_code
        self.text = text
        self.content = text.encode()
        self.url = PHOTO_UPLOAD_ENDPOINT


UPLOAD_OK = FakeRawResponse(
    'for (;;);{"payload":{"photoID":"1029384756584738","imageSrc":'
    '"https://scontent.example/fb.jpg","width":1,"height":1}}')


def _upload_ok(photo_id: str) -> FakeRawResponse:
    """A synthetic ingest success carrying any photoID (album ordering)."""
    return FakeRawResponse(
        f'for (;;);{{"payload":{{"photoID":"{photo_id}",'
        f'"imageSrc":"https://scontent.example/fb.jpg",'
        f'"width":1,"height":1}}}}')


class RawRecorder:
    """_raw_post monkeypatch: records calls, returns queued responses."""

    def __init__(self, responses: list[FakeRawResponse]):
        self.responses = list(responses)
        self.calls: list[tuple[str, Any, dict]] = []

    def __call__(self, url: str, data: Any, headers: dict):
        self.calls.append((url, data, dict(headers)))
        return self.responses.pop(0)


def _recorder(service: UploadService, responses: list[FakeRawResponse]) -> RawRecorder:
    """Patch the service's _raw_post seam with a recording fake."""
    rec = RawRecorder(responses)
    service._raw_post = rec  # type: ignore[method-assign]
    return rec


def _service(stub: StubSession, responses: list[FakeRawResponse]) -> UploadService:
    """UploadService with _raw_post patched to return queued responses."""
    service = UploadService(stub)
    _recorder(service, responses)
    return service


def _composer_calls(stub: StubSession) -> list[tuple[str, str, dict]]:
    return [c for c in stub.graphql.calls if c[0] == COMPOSER_MUTATION]


def _decode_multipart(body: bytes, content_type: str) -> dict[str, Any]:
    """Split a recorded multipart body into fields + the file part."""
    boundary = content_type.split("boundary=", 1)[1]
    parts: dict[str, Any] = {}
    for segment in body.split(f"--{boundary}".encode()):
        segment = segment.strip(b"\r\n")
        if not segment or segment == b"--":
            continue
        head, _, value = segment.partition(b"\r\n\r\n")
        head_text = head.decode()
        name_start = head_text.index('name="') + len('name="')
        name = head_text[name_start:head_text.index('"', name_start)]
        parts[name] = value
    return parts


# ------------------------------------------------------------ upload_photo
class TestUploadPhoto:
    """Pins the photo ingest wire: the live-verified query set, multipart
    fields, and the typed UploadError taxonomy."""

    def test_request_shape_is_the_decoded_wire(self, tmp_path: Path):
        png = tmp_path / "dot.png"
        png.write_bytes(_tiny_png())
        stub = StubSession()
        service = _service(stub, [UPLOAD_OK])

        upload = service.upload_photo(png)

        assert len(service._raw_post.calls) == 1  # type: ignore[attr-defined]
        url, body, headers = service._raw_post.calls[0]  # type: ignore[attr-defined]
        assert url.startswith(PHOTO_UPLOAD_ENDPOINT + "?")
        query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        # the live-verified async query set (module docstring):
        assert query["av"] == STUB_USER_ID
        assert query["__user"] == STUB_USER_ID
        assert query["__a"] == "1"
        assert query["fb_dtsg"] == "NAfTEST:1:1789723298"
        assert query["lsd"] == "TESTLSD00000000000"
        assert query["jazoest"].isdigit()
        assert headers["origin"] == "https://www.facebook.com"
        assert headers["referer"] == "https://www.facebook.com/"
        assert headers["content-type"].startswith("multipart/form-data; boundary=")

        parts = _decode_multipart(body, headers["content-type"])
        assert parts["source"] == b"8"
        assert parts["waterfallxapp"] == b"comet"
        assert parts["profile_id"] == STUB_USER_ID.encode()
        assert parts["upload_id"].isdigit()          # PhotosUploadID shape
        assert b'Content-Disposition: form-data; name="farr"; filename="dot.png"' \
            in body
        assert b"Content-Type: image/png" in body
        assert parts["farr"] == _tiny_png()

        assert upload["photo_id"] == "1029384756584738"
        assert upload["upload_id"] == parts["upload_id"].decode()
        assert upload["width"] == 1 and upload["height"] == 1
        assert upload["image_url"] == "https://scontent.example/fb.jpg"

    def test_response_without_loop_guard_prefix(self, tmp_path: Path):
        png = tmp_path / "dot.png"
        png.write_bytes(_tiny_png())
        stub = StubSession()
        service = _service(stub, [FakeRawResponse(
            '{"payload":{"photoID":"55500111000"}}')])

        upload = service.upload_photo(png)
        assert upload["photo_id"] == "55500111000"

    def test_missing_photo_id_raises(self, tmp_path: Path):
        png = tmp_path / "dot.png"
        png.write_bytes(_tiny_png())
        stub = StubSession()
        service = _service(stub, [FakeRawResponse(
            'for (;;);{"payload":{"height":1}}')])

        with pytest.raises(UploadError):
            service.upload_photo(png)

    def test_http_error_status_raises(self, tmp_path: Path):
        png = tmp_path / "dot.png"
        png.write_bytes(_tiny_png())
        stub = StubSession()
        service = _service(stub, [FakeRawResponse("nope", status_code=429)])

        with pytest.raises(UploadError) as err:
            service.upload_photo(png)
        assert err.value.code == 429

    def test_non_json_body_raises(self, tmp_path: Path):
        png = tmp_path / "dot.png"
        png.write_bytes(_tiny_png())
        stub = StubSession()
        service = _service(stub, [FakeRawResponse("<html>")])

        with pytest.raises(UploadError):
            service.upload_photo(png)

    def test_missing_file_raises(self, tmp_path: Path):
        stub = StubSession()
        with pytest.raises(UploadError):
            UploadService(stub).upload_photo(tmp_path / "absent.png")

    def test_upload_only_never_touches_graphql(self, tmp_path: Path):
        png = tmp_path / "dot.png"
        png.write_bytes(_tiny_png())
        stub = StubSession()
        service = _service(stub, [UPLOAD_OK])

        service.upload_photo(png)
        assert stub.graphql.calls == []

    def test_gif_rides_image_gif_mime(self, tmp_path: Path):
        """GIF acceptance pin: mimetypes maps .gif -> image/gif, so the
        photo ingest already accepts GIFs — the guessed MIME rides the
        multipart part headers verbatim, exactly like any other image."""
        gif = tmp_path / "spin.gif"
        gif.write_bytes(_tiny_gif())
        stub = StubSession()
        service = _service(stub, [UPLOAD_OK])

        upload = service.upload_photo(gif)

        assert upload["photo_id"] == "1029384756584738"
        assert len(service._raw_post.calls) == 1  # type: ignore[attr-defined]
        url, body, headers = service._raw_post.calls[0]  # type: ignore[attr-defined]
        assert url.startswith(PHOTO_UPLOAD_ENDPOINT + "?")
        assert b'filename="spin.gif"' in body
        assert b"Content-Type: image/gif\r\n" in body
        parts = _decode_multipart(body, headers["content-type"])
        assert parts["farr"] == _tiny_gif()

    def test_non_image_mime_rejected_before_any_wire_call(self, tmp_path: Path):
        """The uploader's file-input accept filter, enforced client-side:
        a guessed non-image MIME (video/*, text/*, the octet-stream
        fallback) never reaches the ingest endpoint."""
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"000000")
        note = tmp_path / "note.txt"
        note.write_bytes(b"hello")
        stub = StubSession()
        service = _service(stub, [])

        for path in (clip, note):
            with pytest.raises(UploadError):
                service.upload_photo(path)
        assert service._raw_post.calls == []  # type: ignore[attr-defined]


# --------------------------------------------------------------- post_photo
class TestPostPhoto:
    """Pins the composer publish after ingest: decoded attachments wiring,
    privacy mapping, and fresh idempotence tokens."""

    def _stub(self) -> StubSession:
        return StubSession(responses={COMPOSER_MUTATION: {
            "data": {"story_create": {"story": {"id": "UzpfSToz"}}}}})

    def _post(self, stub: StubSession, png: Path, caption: str,
              privacy: Privacy, group_id: str | None = None) -> Any:
        service = _service(stub, [UPLOAD_OK])
        return service.post_photo(png, caption, privacy, group_id=group_id)

    def test_attachments_wire_the_uploaded_photo_id(self, tmp_path: Path):
        png = tmp_path / "dot.png"
        png.write_bytes(_tiny_png())
        stub = self._stub()

        result = self._post(stub, png, "caption text", Privacy.PRIVATE)

        calls = _composer_calls(stub)
        assert len(calls) == 1
        friendly, doc_id, variables = calls[0]
        assert friendly == COMPOSER_MUTATION
        assert doc_id == COMPOSER_DOC_ID   # live-verified KNOWN_MUTATIONS id

        # the decoded attachments shape: [{"photo": {"id": photoID}}]
        assert variables["input"]["attachments"] == [
            {"photo": {"id": "1029384756584738"}}]
        assert variables["input"]["message"]["text"] == "caption text"
        # privacy mapping: PRIVATE -> the live-captured "SELF" base_state
        assert variables["input"]["audience"]["privacy"]["base_state"] == "SELF"
        assert variables["input"]["actor_id"] == STUB_USER_ID
        assert variables["feedLocation"] == "NEWSFEED"
        assert variables["renderLocation"] == "homepage_stream"
        # captured-template fields ride verbatim
        assert variables["input"]["composer_entry_point"] == "inline_composer"
        assert variables["input"]["source"] == "WWW"
        # fresh idempotence token, not the captured one
        assert variables["input"]["idempotence_token"].endswith("_FEED")
        assert variables["input"]["idempotence_token"] != \
            "be80ba06-2640-452b-a5f7-1fb89e84aa7b_FEED"

        assert result["photo_id"] == "1029384756584738"
        assert result["publish"]["data"]["story_create"]["story"]["id"] == "UzpfSToz"

    def test_privacy_base_states(self, tmp_path: Path):
        png = tmp_path / "dot.png"
        png.write_bytes(_tiny_png())
        for privacy, base in ((Privacy.PUBLIC, "EVERYONE"),
                              (Privacy.FRIENDS, "FRIENDS"),
                              (Privacy.PRIVATE, "SELF")):
            stub = self._stub()
            self._post(stub, png, f"post {privacy}", privacy)
            _, _, variables = _composer_calls(stub)[0]
            assert variables["input"]["audience"]["privacy"]["base_state"] == base

    def test_group_id_wiring(self, tmp_path: Path):
        png = tmp_path / "dot.png"
        png.write_bytes(_tiny_png())
        stub = self._stub()

        self._post(stub, png, "to the group", Privacy.FRIENDS,
                   group_id="12345678901234567")
        _, _, variables = _composer_calls(stub)[0]
        assert variables["groupID"] == "12345678901234567"

    def test_ingest_then_exactly_one_mutation(self, tmp_path: Path):
        png = tmp_path / "dot.png"
        png.write_bytes(_tiny_png())
        stub = self._stub()

        self._post(stub, png, "order", Privacy.FRIENDS)
        # exactly one composer mutation after the ingest
        assert len(stub.graphql.calls) == 1


# ------------------------------------------------------------------ post_album
class TestPostAlbum:
    """Pins the album publish: N sequential ingests spaced by
    governor-shaped gaps (via the injected sleeper — the suite never
    sleeps real time), then ONE composer mutation carrying the
    N-element attachments list — the list extension of the calibrated
    single-photo shape (docs/15 §P9)."""

    ALBUM_IDS: ClassVar[list[str]] = [
        "900100200300400", "900100200300401", "900100200300402"]

    def _stub(self) -> StubSession:
        return StubSession(responses={COMPOSER_MUTATION: {
            "data": {"story_create": {"story": {"id": "UzpfSToz"}}}}})

    def _pngs(self, tmp_path: Path, count: int) -> list[Path]:
        paths = [tmp_path / f"{name}.png" for name in "abc"[:count]]
        for path in paths:
            path.write_bytes(_tiny_png())
        return paths

    def test_n_uploads_then_one_mutation_with_list_attachments(
            self, tmp_path: Path):
        pngs = self._pngs(tmp_path, 3)
        stub = self._stub()
        rec = RawRecorder([_upload_ok(pid) for pid in self.ALBUM_IDS])
        service = UploadService(stub)
        service._raw_post = rec  # type: ignore[method-assign]
        sleeps: list[float] = []

        result = service.post_album(pngs, "album caption", Privacy.FRIENDS,
                                    sleep=sleeps.append)

        # N ingests on the raw plane, in list order, nothing else
        assert len(rec.calls) == 3
        assert result["photo_ids"] == self.ALBUM_IDS
        # exactly ONE composer mutation for the whole album
        calls = _composer_calls(stub)
        assert len(calls) == 1
        _, doc_id, variables = calls[0]
        assert doc_id == COMPOSER_DOC_ID
        # the one novel wire assertion: the list extension of the
        # calibrated single-photo element (docs/15 §P9)
        assert variables["input"]["attachments"] == [
            {"photo": {"id": "900100200300400"}},
            {"photo": {"id": "900100200300401"}},
            {"photo": {"id": "900100200300402"}},
        ]
        assert variables["input"]["message"]["text"] == "album caption"
        assert variables["input"]["audience"]["privacy"]["base_state"] == \
            "FRIENDS"
        assert variables["input"]["actor_id"] == STUB_USER_ID
        assert variables["feedLocation"] == "NEWSFEED"
        assert variables["renderLocation"] == "homepage_stream"
        assert variables["input"]["idempotence_token"].endswith("_FEED")
        # anti-burst pacing: exactly N-1 gaps between the N ingests, each
        # at or above the governor's 4s floor (docs/11 §8 + docs/15
        # §P8-1) — never zero, never metronomic
        assert len(sleeps) == 2
        assert all(gap >= 4.0 for gap in sleeps)
        # the publish echo and per-upload records ride through
        assert result["publish"]["data"]["story_create"]["story"]["id"] == \
            "UzpfSToz"
        assert [u["photo_id"] for u in result["uploads"]] == self.ALBUM_IDS

    def test_single_image_album_never_sleeps(self, tmp_path: Path):
        """A one-image album is just post_photo's wire: no inter-upload
        gaps (the concluding mutation is governor-gated on its own)."""
        pngs = self._pngs(tmp_path, 1)
        stub = self._stub()
        rec = RawRecorder([_upload_ok("911100200300400")])
        service = UploadService(stub)
        service._raw_post = rec  # type: ignore[method-assign]
        sleeps: list[float] = []

        result = service.post_album(pngs, "solo", Privacy.PUBLIC,
                                    sleep=sleeps.append)

        assert sleeps == []
        _, _, variables = _composer_calls(stub)[0]
        assert variables["input"]["attachments"] == [
            {"photo": {"id": "911100200300400"}}]
        assert result["photo_ids"] == ["911100200300400"]

    def test_album_group_id_wiring(self, tmp_path: Path):
        pngs = self._pngs(tmp_path, 2)
        stub = self._stub()
        rec = RawRecorder([_upload_ok("900100200300400"),
                           _upload_ok("900100200300401")])
        service = UploadService(stub)
        service._raw_post = rec  # type: ignore[method-assign]

        service.post_album(pngs, "to the group", Privacy.FRIENDS,
                           group_id="12345678901234567", sleep=lambda s: None)
        _, _, variables = _composer_calls(stub)[0]
        assert variables["groupID"] == "12345678901234567"

    def test_empty_album_raises(self):
        stub = StubSession()
        with pytest.raises(UploadError):
            UploadService(stub).post_album([], "x", Privacy.FRIENDS,
                                           sleep=lambda s: None)

    def test_album_ingest_rejects_non_image_member(self, tmp_path: Path):
        """The per-image MIME guard holds inside the album flow too: a
        non-image member aborts the album before its ingest (nothing
        after it is uploaded either)."""
        png = tmp_path / "a.png"
        png.write_bytes(_tiny_png())
        clip = tmp_path / "b.mp4"
        clip.write_bytes(b"000000")
        stub = self._stub()
        service = UploadService(stub)
        service._raw_post = RawRecorder([_upload_ok("900100200300400")])  # type: ignore[method-assign]

        with pytest.raises(UploadError):
            service.post_album([png, clip], "bad mix", Privacy.FRIENDS,
                               sleep=lambda s: None)
        # the png ingested, the mp4 aborted before the wire, no publish
        assert len(service._raw_post.calls) == 1  # type: ignore[attr-defined]
        assert stub.graphql.calls == []


# ------------------------------------------------------------ multipart builder
class TestMultipartBuilder:
    """Pins _build_multipart: field order, the file part's disposition,
    and closing boundary termination."""

    def test_field_order_and_boundary_termination(self):
        body, ctype = _build_multipart(
            {"source": "8", "upload_id": "1025"}, "farr", "p.png",
            "image/png", b"PNGDATA")
        assert ctype.startswith("multipart/form-data; boundary=----fbk")
        boundary = ctype.split("boundary=", 1)[1].encode()
        assert body.startswith(b"--" + boundary)
        assert body.endswith(b"\r\n--" + boundary + b"--\r\n")
        assert b'name="source"\r\n\r\n8\r\n' in body
        assert b'name="upload_id"\r\n\r\n1025\r\n' in body
        assert (b'Content-Disposition: form-data; name="farr"; '
                b'filename="p.png"\r\nContent-Type: image/png\r\n\r\nPNGDATA'
                in body)


# ------------------------------------------------------------------ json safety
def test_async_payload_parser_prefixes():
    doc = _parse_async_payload('for (;;);{"payload":{"photoID":"1"}}')
    assert doc["payload"]["photoID"] == "1"
    doc = _parse_async_payload('while(1);{"payload":{}}')
    assert doc == {"payload": {}}
    with pytest.raises(UploadError):
        _parse_async_payload("not json at all")


def test_tiny_png_is_valid():
    png = _tiny_png()
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert png.endswith(b"IEND\xaeB`\x82")
