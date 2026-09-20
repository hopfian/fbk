"""`fbk feed publish --media` — the unified media dispatch (docs/02 §2.6).

Pins the publish verb's --media wiring: comma/repeat list parsing, the
dispatch table (one image -> UploadService.post_photo, an image list ->
post_album, one .mp4/.mov -> VideoUploadService.post_video — each
through the upload surfaces' OWN composer templates, never
FeedService.publish, which cannot carry attachments), the mixed-list /
multi-video / media+enrichment rejections, and that the no-media text
path is unchanged. Dispatch is faked at the service-method seam; the
single-image case additionally rides the REAL UploadService wire
(stubbed raw transport) to prove the composer attach carries
input.attachments — the proof the attach does not go through
FeedService.publish.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from fakes import StubSession

import commands.feed as feed_commands
from commands.feed import _media_paths, register
from domain.common import Privacy
from surfaces.upload import UploadService
from surfaces.video_upload import VideoUploadService

COMPOSER_MUTATION = "ComposerStoryCreateMutation"
PHOTO_UPLOAD_ENDPOINT = (
    "https://upload.facebook.com/ajax/react_composer/attachments/photo/upload")


# --------------------------------------------------------------------- harness
def _parser() -> argparse.ArgumentParser:
    """The feed family wired the way app.py wires it (direct register())."""
    parser = argparse.ArgumentParser(prog="fbk", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    register(sub)
    return parser


def _stub() -> StubSession:
    return StubSession(responses={COMPOSER_MUTATION: {
        "data": {"story_create": {"story": {"id": "UzpfSToz"}}}}})


def _run(monkeypatch, capsys, stub: StubSession,
         argv: list[str]) -> tuple[int, str, str]:
    """Parse+run one publish invocation; return (rc, stdout, stderr)."""
    args = _parser().parse_args(argv)
    monkeypatch.setattr(feed_commands, "new_session", lambda a: stub)
    rc = args.fn(args)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


class DispatchRecorder:
    """A service-method monkeypatch stand-in: records calls, returns one
    canned result (the album/video/photo surface wires are pinned by
    their own suites; here only the DISPATCH is under test).

    Assigned as an INSTANCE onto the service class, it is NOT a
    descriptor — the service instance is never bound into the call —
    so the recorded args are exactly the method's parameters.
    """

    def __init__(self, result: dict[str, Any]):
        self.result = result
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((args, kwargs))
        return self.result


def _photo_result(photo_id: str) -> dict[str, Any]:
    return {"photo_id": photo_id,
            "upload": {"photo_id": photo_id},
            "publish": {"data": {"story_create": {"story": {"id": "S1"}}}}}


def _album_result(*photo_ids: str) -> dict[str, Any]:
    return {"photo_ids": list(photo_ids),
            "uploads": [{"photo_id": p} for p in photo_ids],
            "publish": {"data": {"story_create": {"story": {"id": "S2"}}}}}


def _video_result(video_id: str) -> dict[str, Any]:
    return {"video_id": video_id,
            "upload": {"video_id": video_id},
            "publish": {"data": {"story_create": {"story": {"id": "S3"}}}}}


# ------------------------------------------------------------- --media parsing
class TestMediaFlagParsing:
    """Pins the --media flag wiring: repeatable, comma-split, and flat."""

    def test_flag_parses_comma_and_repeat_forms(self):
        args = _parser().parse_args(
            ["feed", "publish", "--text", "t", "--privacy", "public",
             "--media", "a.png,b.png", "--media", "c.png"])
        assert args.media == ["a.png,b.png", "c.png"]
        assert _media_paths(args.media) == [Path("a.png"), Path("b.png"),
                                            Path("c.png")]

    def test_empty_segments_are_dropped(self):
        assert _media_paths(["a.png,,b.png, "]) == [Path("a.png"),
                                                   Path("b.png")]

    def test_absent_flag_flattens_to_no_media(self):
        args = _parser().parse_args(
            ["feed", "publish", "--text", "t", "--privacy", "public"])
        assert args.media is None
        assert _media_paths(None) == []
        assert _media_paths([]) == []


# ----------------------------------------------------------------- dispatch
class TestMediaDispatch:
    """Pins the --media dispatch table at the service-method seam."""

    def _publish_argv(self, *media: str) -> list[str]:
        return ["feed", "publish", "--text", "hello", "--privacy", "public",
                "--json", *[part for m in media for part in ("--media", m)]]

    def test_single_image_dispatches_post_photo(self, monkeypatch, capsys):
        rec = DispatchRecorder(_photo_result("P1"))
        monkeypatch.setattr(UploadService, "post_photo", rec)

        rc, out, _ = _run(monkeypatch, capsys, _stub(),
                          self._publish_argv("one.png"))

        assert rc == 0
        assert len(rec.calls) == 1
        args, kwargs = rec.calls[0]
        assert args == (Path("one.png"), "hello", Privacy.PUBLIC)
        assert kwargs == {"group_id": None}
        payload = json.loads(out)
        assert payload["media"] == "photo"
        assert payload["photo_id"] == "P1"
        assert payload["post_id"] == "S1"

    def test_multi_image_dispatches_post_album(self, monkeypatch, capsys):
        rec = DispatchRecorder(_album_result("P1", "P2"))
        monkeypatch.setattr(UploadService, "post_album", rec)

        rc, out, _ = _run(monkeypatch, capsys, _stub(),
                          self._publish_argv("a.png,b.png"))

        assert rc == 0
        assert len(rec.calls) == 1
        args, kwargs = rec.calls[0]
        assert args == ([Path("a.png"), Path("b.png")], "hello",
                        Privacy.PUBLIC)
        assert kwargs == {"group_id": None}
        payload = json.loads(out)
        assert payload["media"] == "album"
        assert payload["photo_ids"] == ["P1", "P2"]
        assert payload["post_id"] == "S2"

    def test_repeated_flags_flatten_into_one_album(self, monkeypatch, capsys):
        rec = DispatchRecorder(_album_result("P1", "P2"))
        monkeypatch.setattr(UploadService, "post_album", rec)

        rc, _, _ = _run(monkeypatch, capsys, _stub(),
                        self._publish_argv("a.png", "b.png"))

        assert rc == 0
        assert rec.calls[0][0][0] == [Path("a.png"), Path("b.png")]

    def test_single_mp4_dispatches_post_video(self, monkeypatch, capsys):
        rec = DispatchRecorder(_video_result("V1"))
        monkeypatch.setattr(VideoUploadService, "post_video", rec)

        rc, out, _ = _run(monkeypatch, capsys, _stub(),
                          self._publish_argv("clip.mp4"))

        assert rc == 0
        assert len(rec.calls) == 1
        args, kwargs = rec.calls[0]
        assert args == (Path("clip.mp4"), "hello", Privacy.PUBLIC)
        assert kwargs == {"group_id": None}
        payload = json.loads(out)
        assert payload["media"] == "video"
        assert payload["video_id"] == "V1"
        assert payload["post_id"] == "S3"

    def test_single_mov_dispatches_post_video(self, monkeypatch, capsys):
        rec = DispatchRecorder(_video_result("V2"))
        monkeypatch.setattr(VideoUploadService, "post_video", rec)

        rc, _, _ = _run(monkeypatch, capsys, _stub(),
                        self._publish_argv("clip.mov"))

        assert rc == 0
        assert rec.calls[0][0] == (Path("clip.mov"), "hello", Privacy.PUBLIC)


# ----------------------------------------------------------------- rejections
class TestMediaRejection:
    """Pins the clean rejections: mixed lists, multi-video, and media
    combined with an enrichment flag — nothing is sent in any case."""

    def test_mixed_image_and_video_rejected(self, monkeypatch, capsys):
        stub = _stub()

        rc, out, err = _run(monkeypatch, capsys, stub,
                            ["feed", "publish", "--text", "x",
                             "--privacy", "public", "--json",
                             "--media", "a.png,b.mp4"])

        assert rc == 1
        assert err.startswith("error:")
        assert "mixed image+video" in err
        assert out == ""                    # payload discipline: no stdout
        assert stub.graphql.calls == []      # nothing was sent

    def test_multiple_videos_rejected(self, monkeypatch, capsys):
        stub = _stub()

        rc, _, err = _run(monkeypatch, capsys, stub,
                          ["feed", "publish", "--text", "x",
                           "--privacy", "public", "--media", "a.mp4,b.mov"])

        assert rc == 1
        assert "at most one video" in err
        assert stub.graphql.calls == []

    def test_media_with_enrichment_flag_rejected(self, monkeypatch, capsys):
        stub = _stub()

        rc, _, err = _run(monkeypatch, capsys, stub,
                          ["feed", "publish", "--text", "x",
                           "--privacy", "public", "--media", "a.png",
                           "--feeling", "123", "--place", "456"])

        assert rc == 1
        assert "--feeling" in err and "--place" in err
        assert stub.graphql.calls == []


# ------------------------------------------------------- the real composer wire
class TestMediaComposerWire:
    """The single-image media post rides the REAL UploadService wire
    (ingest + composer mutation), proving the attach goes through the
    upload surface's own composer template — the mutation carries
    input.attachments, which FeedService.publish never sets."""

    def test_single_image_attach_carries_input_attachments(
            self, monkeypatch, capsys, tmp_path):
        png = tmp_path / "dot.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n")
        raw_calls: list[str] = []

        def fake_raw_post(_self: Any, url: str, data: Any,
                         headers: dict[str, str]) -> Any:
            class Resp:
                status_code = 200
                text = ('for (;;);{"payload":{"photoID":"1029384756584738",'
                        '"imageSrc":"https://scontent.example/fb.jpg",'
                        '"width":1,"height":1}}')
                content = text.encode()
                url = PHOTO_UPLOAD_ENDPOINT
            raw_calls.append(url)
            return Resp()

        monkeypatch.setattr(UploadService, "_raw_post", fake_raw_post)
        stub = _stub()

        rc, out, _ = _run(monkeypatch, capsys, stub,
                          ["feed", "publish", "--text", "with a photo",
                           "--privacy", "private", "--json",
                           "--media", str(png)])

        assert rc == 0
        assert len(raw_calls) == 1                      # the ingest
        assert raw_calls[0].startswith(PHOTO_UPLOAD_ENDPOINT + "?")
        assert len(stub.graphql.calls) == 1             # one composer mutation
        friendly, _, variables = stub.graphql.calls[0]
        assert friendly == COMPOSER_MUTATION
        # the proof: the attach rode the upload surface's template
        assert variables["input"]["attachments"] == [
            {"photo": {"id": "1029384756584738"}}]
        assert variables["input"]["message"]["text"] == "with a photo"
        assert variables["input"]["audience"]["privacy"]["base_state"] == "SELF"
        payload = json.loads(out)
        assert payload["media"] == "photo"
        assert payload["post_id"] == "UzpfSToz"

    def test_no_media_text_publish_is_unchanged(self, monkeypatch, capsys):
        """Without --media the verb is still the plain FeedService.publish
        text post (the composer-enrichment path, untouched by media)."""
        stub = _stub()

        rc, out, _ = _run(monkeypatch, capsys, stub,
                          ["feed", "publish", "--text", "plain words",
                           "--privacy", "friends", "--json"])

        assert rc == 0
        payload = json.loads(out)
        assert payload["data"]["story_create"]["story"]["id"] == "UzpfSToz"
        _, _, variables = stub.graphql.calls[0]
        # the text path carries NO attachments — the media seam stayed out
        assert "attachments" not in variables["input"] or \
            not variables["input"]["attachments"]
