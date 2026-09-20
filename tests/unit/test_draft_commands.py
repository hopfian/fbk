"""``fbk draft save/list/show/publish/delete`` — the CLI-local draft
family.

Unit (offline): the family is driven through DIRECT parser construction
(drafts are not yet wired onto the app parser — the orchestrator owns
app.py) under run_command so every exit code pins the real contract:
0 success, 1 failed precondition (path-shaped name, absent draft,
overwrite refusal), 5 a typed publish error. Publish stubs the service
seam (commands.drafts.new_session / FeedService — the monkeypatch seam
the with_session docstring documents) and pins that the draft's stored
spec reaches the exact FeedService.publish call.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fakes import StubSession

import commands.drafts as drafts_cmd
from commands.common import run_command
from domain.common import Privacy
from graphql.errors import RateLimitedError


def _clear_env(monkeypatch) -> None:
    monkeypatch.delenv("FBK_ROOT", raising=False)
    monkeypatch.delenv("FBK_COOKIES", raising=False)
    monkeypatch.delenv("FBK_IMPERSONATE", raising=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    _clear_env(monkeypatch)


def _parser() -> argparse.ArgumentParser:
    """The draft family on a standalone parser (not yet on app.py)."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    drafts_cmd.register(sub)
    return parser


def _run(capsys, argv: list[str]) -> tuple[int, str, str, dict[str, Any]]:
    """Drive one draft subcommand under run_command; return (rc, stdout,
    stderr, payload) — the payload parses from the trailing compact
    JSON line (the emit contract); {} when no payload was printed."""
    args = _parser().parse_args(argv)
    rc = run_command(args.fn, args)
    captured = capsys.readouterr()
    out = captured.out
    if not out.strip():
        return rc, out, captured.err, {}
    try:
        return rc, out, captured.err, json.loads(out)
    except json.JSONDecodeError:
        return rc, out, captured.err, json.loads(out.splitlines()[-1])


def _draft_file(tmp_path: Path, name: str) -> Path:
    return tmp_path / "state" / "drafts" / f"{name}.json"


class TestSaveListShowDelete:
    """The offline lifecycle under run_command, all on a tmp root."""

    def test_save_list_show_delete_cycle(self, tmp_path, capsys):
        rc, out, _err, payload = _run(capsys, [
            "draft", "save", "--name", "promo", "--text", "hello world",
            "--root", str(tmp_path)])
        assert rc == 0
        assert payload["draft"] == "promo"
        assert _draft_file(tmp_path, "promo").is_file()

        rc, out, _, payload = _run(capsys, [
            "draft", "list", "--root", str(tmp_path)])
        assert rc == 0
        assert payload["count"] == 1
        assert payload["drafts"][0]["name"] == "promo"
        assert payload["drafts"][0]["text_head"] == "hello world"
        assert "1 draft" in out

        rc, _, _, payload = _run(capsys, [
            "draft", "show", "--name", "promo", "--root", str(tmp_path)])
        assert rc == 0
        assert payload["spec"]["text"] == "hello world"
        assert payload["spec"]["privacy"] == "friends"
        assert payload["spec"]["media"] == []
        assert payload["spec"]["ai_label"] is None

        rc, _, _, payload = _run(capsys, [
            "draft", "delete", "--name", "promo", "--root", str(tmp_path)])
        assert rc == 0
        assert payload["deleted"] is True
        assert not _draft_file(tmp_path, "promo").exists()

    def test_show_json_is_machine_only(self, tmp_path, capsys):
        _run(capsys, ["draft", "save", "--name", "j", "--text", "t",
                      "--root", str(tmp_path)])
        rc, out, _, payload = _run(capsys, [
            "draft", "show", "--name", "j", "--root", str(tmp_path), "--json"])
        assert rc == 0
        assert json.loads(out) == payload
        assert "draft 'j'" not in out  # no human decoration in --json mode

    def test_save_stores_every_composer_extra(self, tmp_path, capsys):
        rc, _, _, _ = _run(capsys, [
            "draft", "save", "--name", "full", "--text", "rich",
            "--privacy", "public", "--ai-label", "on", "--background", "7",
            "--media", "a.jpg, b.jpg", "--tag", "1:Ann", "--tag", "2:Bob",
            "--feeling", "f1", "--activity", "ac1", "--place", "pl1",
            "--root", str(tmp_path)])
        assert rc == 0
        spec = json.loads(_draft_file(tmp_path, "full").read_text("utf-8"))
        assert spec["privacy"] == "public"
        assert spec["ai_label"] is True
        assert spec["background"] == "7"
        assert spec["media"] == ["a.jpg", "b.jpg"]
        assert spec["tags"] == ["1:Ann", "2:Bob"]
        assert spec["feeling"] == "f1"
        assert spec["activity"] == "ac1"
        assert spec["place"] == "pl1"
        assert spec["created_at"]

    def test_ai_label_off_persists_false(self, tmp_path, capsys):
        _run(capsys, ["draft", "save", "--name", "off", "--text", "t",
                      "--ai-label", "off", "--root", str(tmp_path)])
        spec = json.loads(_draft_file(tmp_path, "off").read_text("utf-8"))
        assert spec["ai_label"] is False


class TestPreconditions:
    """The exit-1 contract: path-shaped names, absent drafts, and the
    --force overwrite refusal (the journal-export convention)."""

    @pytest.mark.parametrize("bad", ("", ".", "..", "../cookies", "sub/dir",
                                     "..\\evil"))
    def test_save_rejects_path_shaped_names(self, tmp_path, capsys, bad):
        rc, _out, err, _ = _run(capsys, [
            "draft", "save", "--name", bad, "--text", "t",
            "--root", str(tmp_path)])
        assert rc == 1
        assert "invalid draft name" in err
        assert not (tmp_path / "state").exists()  # nothing written anywhere

    @pytest.mark.parametrize("bad", ("../cookies", "sub/dir", "..\\evil"))
    def test_show_and_delete_reject_path_shaped_names(self, tmp_path, capsys, bad):
        for argv in (("show",), ("delete",)):
            rc, _, err, _ = _run(capsys, [
                "draft", *argv, "--name", bad, "--root", str(tmp_path)])
            assert rc == 1
            assert "no draft" in err

    def test_show_absent_exits_one(self, tmp_path, capsys):
        rc, _out, err, _ = _run(capsys, [
            "draft", "show", "--name", "nope", "--root", str(tmp_path)])
        assert rc == 1
        assert "no draft" in err

    def test_delete_absent_exits_one(self, tmp_path, capsys):
        rc, _, err, _ = _run(capsys, [
            "draft", "delete", "--name", "nope", "--root", str(tmp_path)])
        assert rc == 1
        assert "no draft" in err

    def test_save_over_existing_without_force_refuses(self, tmp_path, capsys):
        _run(capsys, ["draft", "save", "--name", "dup", "--text", "one",
                      "--root", str(tmp_path)])
        rc, _, err, _ = _run(capsys, [
            "draft", "save", "--name", "dup", "--text", "two",
            "--root", str(tmp_path)])
        assert rc == 1
        assert "refusing to overwrite" in err
        spec = json.loads(_draft_file(tmp_path, "dup").read_text("utf-8"))
        assert spec["text"] == "one"  # the original is untouched

    def test_save_with_force_overwrites(self, tmp_path, capsys):
        _run(capsys, ["draft", "save", "--name", "dup", "--text", "one",
                      "--root", str(tmp_path)])
        rc, _, _, _ = _run(capsys, [
            "draft", "save", "--name", "dup", "--text", "two",
            "--force", "--root", str(tmp_path)])
        assert rc == 0
        spec = json.loads(_draft_file(tmp_path, "dup").read_text("utf-8"))
        assert spec["text"] == "two"

    def test_list_empty_is_a_reported_fact_exit_zero(self, tmp_path, capsys):
        rc, out, _, payload = _run(capsys, [
            "draft", "list", "--root", str(tmp_path)])
        assert rc == 0
        assert payload["count"] == 0
        assert "no drafts" in out


class _RecordingService:
    """FeedService stub: records publish calls, plays scripted outcomes."""

    def __init__(self, session: Any, calls: list, outcomes: list) -> None:
        self.session = session
        self._calls = calls
        self._outcomes = list(outcomes)

    def publish(self, text: str, privacy: Privacy,
                **kw: Any) -> dict[str, Any]:
        self._calls.append((text, privacy, kw))
        outcome = self._outcomes[len(self._calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return {"data": {"story_create": {"feed_story_edge": {
            "node": {"post_id": f"p{len(self._calls)}"}}}}}


class TestPublish:
    """draft publish delegates to the exact FeedService.publish call the
    feed publish command makes, under the stubbed service seam."""

    def _install(self, monkeypatch, outcomes: list) -> list:
        calls: list = []

        def make_service(session: Any) -> _RecordingService:
            return _RecordingService(session, calls, outcomes)

        monkeypatch.setattr(drafts_cmd, "FeedService", make_service)
        monkeypatch.setattr(drafts_cmd, "new_session",
                            lambda args: StubSession())
        return calls

    def test_publish_sends_the_stored_spec(self, tmp_path, capsys, monkeypatch):
        calls = self._install(monkeypatch, [None])
        _run(capsys, ["draft", "save", "--name", "go", "--text", "publish me",
                      "--privacy", "public", "--root", str(tmp_path)])
        rc, _, _, payload = _run(capsys, [
            "draft", "publish", "--name", "go", "--root", str(tmp_path)])
        assert rc == 0
        assert payload["draft"] == "go"
        [text, privacy, kw] = calls[0]
        assert text == "publish me"
        assert privacy is Privacy.PUBLIC
        assert kw == {"ai_generated": None, "text_format_preset_id": None}

    def test_publish_forwards_ai_label_and_background(self, tmp_path, capsys,
                                                      monkeypatch):
        calls = self._install(monkeypatch, [None])
        _run(capsys, ["draft", "save", "--name", "go", "--text", "t",
                      "--ai-label", "on", "--background", "7",
                      "--root", str(tmp_path)])
        rc, _, _, _ = _run(capsys, [
            "draft", "publish", "--name", "go", "--root", str(tmp_path)])
        assert rc == 0
        assert calls[0][2] == {"ai_generated": True,
                               "text_format_preset_id": "7"}

    def test_publish_keeps_the_draft_by_default(self, tmp_path, capsys,
                                                monkeypatch):
        self._install(monkeypatch, [None])
        _run(capsys, ["draft", "save", "--name", "keep", "--text", "t",
                      "--root", str(tmp_path)])
        rc, _, _, payload = _run(capsys, [
            "draft", "publish", "--name", "keep", "--root", str(tmp_path)])
        assert rc == 0
        assert "draft_deleted" not in payload
        assert _draft_file(tmp_path, "keep").is_file()

    def test_publish_delete_removes_after_success(self, tmp_path, capsys,
                                                  monkeypatch):
        self._install(monkeypatch, [None])
        _run(capsys, ["draft", "save", "--name", "burn", "--text", "t",
                      "--root", str(tmp_path)])
        rc, _, _, payload = _run(capsys, [
            "draft", "publish", "--name", "burn", "--delete",
            "--root", str(tmp_path)])
        assert rc == 0
        assert payload["draft_deleted"] is True
        assert not _draft_file(tmp_path, "burn").exists()

    def test_publish_failure_keeps_the_draft_and_maps_the_exit_code(
            self, tmp_path, capsys, monkeypatch):
        self._install(monkeypatch, [RateLimitedError("soft block")])
        _run(capsys, ["draft", "save", "--name", "held", "--text", "t",
                      "--root", str(tmp_path)])
        rc, _, _err, _ = _run(capsys, [
            "draft", "publish", "--name", "held", "--root", str(tmp_path)])
        assert rc == 5  # the run_command contract for RateLimitedError
        assert _draft_file(tmp_path, "held").is_file()

    def test_publish_absent_draft_exits_one(self, tmp_path, capsys, monkeypatch):
        self._install(monkeypatch, [])
        rc, _, err, _ = _run(capsys, [
            "draft", "publish", "--name", "nope", "--root", str(tmp_path)])
        assert rc == 1
        assert "no draft" in err


class TestWiring:
    """The family's parser shape on the standalone parser."""

    @pytest.mark.parametrize("argv,child", (
        (["draft", "save", "--name", "x", "--text", "t"], "save"),
        (["draft", "list"], "list"),
        (["draft", "show", "--name", "x"], "show"),
        (["draft", "publish", "--name", "x"], "publish"),
        (["draft", "delete", "--name", "x"], "delete"),
    ))
    def test_family_parses_with_children(self, argv, child):
        args = _parser().parse_args(argv)
        assert callable(args.fn)
        assert args.draft_command == child

    def test_family_requires_a_child(self, capsys):
        with pytest.raises(SystemExit) as ei:
            _parser().parse_args(["draft"])
        assert ei.value.code == 2

    def test_save_requires_name_and_text(self, capsys):
        with pytest.raises(SystemExit) as ei:
            _parser().parse_args(["draft", "save"])
        assert ei.value.code == 2
