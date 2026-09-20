"""`fbk feed edit` — the edit-post wave (fetch_editable + edit + CLI).

Service-level pins run against the REAL captured create template
(data/captured_composer.json through StubSession): the edit mutation is
a DELTA over the create-side sibling — omitted kwargs keep the captured
values verbatim, provided kwargs substitute — with the story identifier
riding the CANDIDATE EDIT_STORY_ID_PATH and fresh idempotence tokens.
The dialog query's variable shape and response are CANDIDATES too
(probe pending), so every test pins THROUGH the module constants: when
the orchestrator's live probe adjusts EDIT_DIALOG_VARIABLES /
EDIT_DIALOG_STORY_KEY / EDIT_STORY_ID_PATH, these tests still pin the
NEW values, never stale literals.

CLI-level pins drive the real app parser under run_command with the
commands.feed.new_session seam stubbed (the monkeypatch seam the
with_session docstring documents): --help renders, the no-flags default
previews the editable state, --show guards against edit flags, the
mutating delta wires every publish-parity flag, and --photo uploads
through the EXISTING upload surface (paced between like an album) then
passes the ids as attachments.
"""
from __future__ import annotations

import base64
import copy
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from fakes import StubSession

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import commands.feed as feed_cmd
from app import build_parser
from commands.common import run_command
from domain.common import Privacy
from surfaces.feed import (
    EDIT_DIALOG_QUERY,
    EDIT_DIALOG_STORY_KEY,
    EDIT_DIALOG_VARIABLES,
    EDIT_MUTATION,
    EDIT_STORY_ID_PATH,
    EditablePost,
    FeedService,
    _set_by_path,
)
from surfaces.upload import UploadService

# Registry-grounded doc_ids (data/doc_id_registry_v3.json, revision
# 1047963790 — the fresh full harvest; StubSession loads that registry).
EDIT_MUTATION_DOC_ID = "27456358844037239"
EDIT_DIALOG_DOC_ID = "38644593395156159"
STUB_USER_ID = "12345678901234"
POST_ID = "122112020481458110"

# A synthetic edit-dialog response crafted FROM THE CANDIDATE SHAPE (no
# live capture exists — probe pending): the parser only walks known
# composer-family keys, so this exercises each walker plus the dedupe
# path (the photo id echoes twice) and the video element variant.
EDIT_DIALOG_PAYLOAD: dict[str, Any] = {
    "data": {
        "comet_edit_post_dialog": {
            "__typename": "CometEditPostComposerDialog",
            "story": {
                "__typename": "Story",
                "id": "UzpfSTozMTM4MDE5OTY5",
                "message": {"text": {
                    "__typename": "TextWithEntities",
                    "text": "the current words"}},
                "comet_sections": {"privacy": {"base_state": "FRIENDS"}},
                "attachments": {"nodes": [
                    {"photo": {"id": "1029384756584738"}},
                    {"photo": {"id": "1029384756584738"}},
                    {"video": {"id": "9876543210987654",
                               "playable_url": "https://x.example/v.mp4"}},
                ]},
            },
            "composer_state": {"target_type": "FEELING",
                               "target_id": "628782513844951"},
            "place_id": "12345678901234570",
        },
    },
}


def _last_call(stub: StubSession) -> tuple[str, str, dict]:
    assert stub.graphql.calls, "service made no client call"
    return stub.graphql.calls[-1]


def _get_by_path(root: dict[str, Any], path: tuple[str, ...]) -> Any:
    """Walk a dotted path (the test-side twin of _set_by_path)."""
    node: Any = root
    for key in path:
        node = node[key]
    return node


def _captured_create_variables(captured_composer: dict) -> dict[str, Any]:
    """The real captured ComposerStoryCreateMutation variables."""
    return next(m for m in captured_composer["mutations"]
                if m["friendly_name"] == "ComposerStoryCreateMutation"
                )["variables"]


class TestCandidateConstants:
    """The probe-correctable module constants are importable and
    well-formed — the orchestrator's live probe adjusts THEM, and every
    other test in this file pins behavior through them."""

    def test_constants_exist_and_are_well_formed(self):
        assert isinstance(EDIT_DIALOG_VARIABLES, dict)
        assert isinstance(EDIT_DIALOG_STORY_KEY, str) and EDIT_DIALOG_STORY_KEY
        assert isinstance(EDIT_STORY_ID_PATH, tuple) and EDIT_STORY_ID_PATH


class TestFetchEditable:
    """Pins fetch_editable: the dialog query replay (registry doc_id,
    CONSTANT-driven variables) and the DEFENSIVE parse of the candidate
    response shape — missing keys tolerated, never a crash."""

    def test_dialog_replay_and_full_parse(self):
        stub = StubSession(
            responses={EDIT_DIALOG_QUERY: EDIT_DIALOG_PAYLOAD})
        state = FeedService(stub).fetch_editable(POST_ID)

        friendly, doc_id, variables = _last_call(stub)
        assert friendly == EDIT_DIALOG_QUERY
        assert doc_id == EDIT_DIALOG_DOC_ID
        # variables pin THROUGH the constants — a probe-adjusted
        # candidate re-pins itself here, no literal to go stale
        assert variables == {**EDIT_DIALOG_VARIABLES,
                             EDIT_DIALOG_STORY_KEY: POST_ID}

        assert state.story_id == "UzpfSTozMTM4MDE5OTY5"
        assert state.text == "the current words"
        assert state.privacy_base_state == "FRIENDS"
        # duplicated photo id collapses; the video element parses by id
        assert state.attachments == [
            {"photo": {"id": "1029384756584738"}},
            {"video": {"id": "9876543210987654"}},
        ]
        assert state.feeling == "628782513844951"
        assert state.activity is None
        assert state.place_id == "12345678901234570"

    def test_empty_payload_yields_all_absent_never_raises(self):
        stub = StubSession(responses={EDIT_DIALOG_QUERY: {}})
        state = FeedService(stub).fetch_editable(POST_ID)
        assert state == EditablePost()

    def test_hostile_shape_is_tolerated(self):
        hostile = {"errors": [{"message": "borked"}],
                   "data": {"viewer": None}, "storyID": 42,
                   "attachments": "not-a-list"}
        stub = StubSession(responses={EDIT_DIALOG_QUERY: hostile})
        state = FeedService(stub).fetch_editable(POST_ID)
        assert state.text is None
        assert state.privacy_base_state is None
        assert state.attachments == []
        assert state.place_id is None

    def test_text_node_without_text_tolerated(self):
        thin = {"data": {"story": {"message": {"text": {
            "__typename": "TextWithEntities"}}}}}
        stub = StubSession(responses={EDIT_DIALOG_QUERY: thin})
        state = FeedService(stub).fetch_editable(POST_ID)
        assert state.text is None


class TestEdit:
    """Pins edit(): the DELTA construction over the real captured
    create template — omitted kwargs keep captured values verbatim,
    provided kwargs substitute exactly like publish's pins — plus the
    story identifier at the CANDIDATE path and fresh per-call tokens."""

    def _edit(self, responses: dict[str, Any] | None = None,
              **kwargs: Any) -> tuple[str, str, dict]:
        stub = StubSession(responses={EDIT_MUTATION: {"data": {}}})
        FeedService(stub).edit(POST_ID, **kwargs)
        return _last_call(stub)

    def test_replay_identity_and_story_id(self):
        friendly, doc_id, _ = self._edit()
        assert friendly == EDIT_MUTATION
        assert doc_id == EDIT_MUTATION_DOC_ID

    def test_omitted_kwargs_keep_the_captured_values(
            self, captured_composer):
        _, _, variables = self._edit()
        captured = _captured_create_variables(captured_composer)

        # the delta contract: ONLY the story TOKEN (the live-probed form -
        # b64("S:_I<actor>:<post_id>:<post_id>"), decoded from the create
        # echo's node id - at the CANDIDATE path), the fresh tokens, and
        # the actor id differ from the capture
        story_token = base64.b64encode(
            f"S:_I{STUB_USER_ID}:{POST_ID}:{POST_ID}".encode()).decode()
        expected = copy.deepcopy(captured)
        _set_by_path(expected, EDIT_STORY_ID_PATH, story_token)
        expected["input"]["idempotence_token"] = \
            variables["input"]["idempotence_token"]
        expected["input"]["logging"]["composer_session_id"] = \
            variables["input"]["logging"]["composer_session_id"]
        expected["input"]["actor_id"] = STUB_USER_ID
        assert variables == expected

        # spot-pin the kept values publish's pins cover on create
        assert variables["input"]["message"]["text"] == \
            captured["input"]["message"]["text"]
        assert variables["input"]["message"]["ranges"] == []
        assert variables["input"]["audience"]["privacy"]["base_state"] \
            == "FRIENDS"
        assert variables["input"]["text_format_preset_id"] == "0"
        assert "target_type" not in variables["input"]
        assert "attachments" not in variables["input"]

    def test_story_id_rides_the_candidate_path(self):
        _, _, variables = self._edit()
        # pinned THROUGH the constant - the probe re-points it in ONE
        # place and this pin follows; the value is the STORY TOKEN form
        # (b64 "S:_I<actor>:<post>:<post>", live-probe finding 2026-09-20)
        story_token = base64.b64encode(
            f"S:_I{STUB_USER_ID}:{POST_ID}:{POST_ID}".encode()).decode()
        assert _get_by_path(variables, EDIT_STORY_ID_PATH) == story_token

    def test_fresh_tokens_never_the_captured_ones(self, captured_composer):
        _, _, variables = self._edit()
        captured = _captured_create_variables(captured_composer)
        token = variables["input"]["idempotence_token"]
        assert token.endswith("_FEED")
        assert token != captured["input"]["idempotence_token"]
        assert uuid.UUID(token.removesuffix("_FEED")).version == 4
        assert variables["input"]["logging"]["composer_session_id"] != \
            captured["input"]["logging"]["composer_session_id"]
        assert variables["input"]["actor_id"] == STUB_USER_ID

    def test_text_privacy_ai_background_substitute(self):
        _, _, variables = self._edit(
            text="edited body", privacy=Privacy.PUBLIC,
            ai_generated=True, text_format_preset_id="7")
        assert variables["input"]["message"]["text"] == "edited body"
        assert variables["input"]["audience"]["privacy"]["base_state"] \
            == "EVERYONE"
        assert variables["input"]["ai_generated_self_disclosure_metadata"][
            "was_self_disclosed_as_ai_generated"] is True
        assert variables["input"]["text_format_preset_id"] == "7"

    def test_tags_build_mention_ranges_against_the_new_text(self):
        _, _, variables = self._edit(
            text="hello", tags=[("12345678901234568", "Jane")])
        assert variables["input"]["message"]["text"] == "hello @Jane"
        assert variables["input"]["message"]["ranges"] == [
            {"entity": {"id": "12345678901234568"}, "length": 5,
             "offset": 6, "render_type": "mention"},
        ]

    def test_feeling_activity_place_ride_the_candidate_fields(self):
        _, _, variables = self._edit(feeling="123456", place_id="999888")
        assert variables["input"]["target_type"] == "FEELING"
        assert variables["input"]["target_id"] == "123456"
        assert variables["input"]["place_id"] == "999888"
        _, _, variables = self._edit(activity="654321")
        assert variables["input"]["target_type"] == "ACTIVITY"
        assert variables["input"]["target_id"] == "654321"

    def test_attachments_pass_through_as_photo_elements(self):
        _, _, variables = self._edit(
            attachments=["1029384756584738", "1029384756584739"])
        # the live-verified create-side attach shape (upload.py
        # post_photo/post_album), one element per id, order preserved
        assert variables["input"]["attachments"] == [
            {"photo": {"id": "1029384756584738"}},
            {"photo": {"id": "1029384756584739"}},
        ]

    def test_error_contracts_mirror_publish(self):
        svc = FeedService(StubSession(responses={}))
        with pytest.raises(ValueError):
            svc.edit(POST_ID, feeling="1", activity="2")
        with pytest.raises(ValueError):
            svc.edit(POST_ID, tags=[("615937", "Jane")])  # tags need text
        with pytest.raises(ValueError):
            svc.edit(POST_ID, text="x", tags=[("", "Jane")])
        with pytest.raises(ValueError):
            svc.edit(POST_ID, text="x", tags=[("615937", "")])


# --------------------------------------------------------------------- CLI
def _clear_env(monkeypatch) -> None:
    monkeypatch.delenv("FBK_ROOT", raising=False)
    monkeypatch.delenv("FBK_COOKIES", raising=False)
    monkeypatch.delenv("FBK_IMPERSONATE", raising=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    _clear_env(monkeypatch)


def _run(capsys, argv: list[str]) -> tuple[int, str, str, dict[str, Any]]:
    """Drive one feed edit invocation through the real app parser under
    run_command; return (rc, stdout, stderr, payload-from-JSON-line)."""
    args = build_parser().parse_args(argv)
    rc = run_command(args.fn, args)
    captured = capsys.readouterr()
    out = captured.out
    if not out.strip():
        return rc, out, captured.err, {}
    try:
        return rc, out, captured.err, json.loads(out)
    except json.JSONDecodeError:
        return rc, out, captured.err, json.loads(out.splitlines()[-1])


class _UploadRecorder:
    """upload_photo stand-in: records paths, returns canned photo ids."""

    def __init__(self, *photo_ids: str) -> None:
        self.photo_ids = list(photo_ids)
        self.paths: list[Any] = []

    def __call__(self, path: Any) -> dict[str, Any]:
        self.paths.append(path)
        return {"photo_id": self.photo_ids[len(self.paths) - 1]}


class TestEditParserWiring:
    """Parser-level pins: --help renders and the flag surface parses."""

    def test_help_renders(self, capsys):
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["feed", "edit", "--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "--post-id" in out
        assert "--show" in out
        assert "--photo" in out

    def test_flags_parse(self):
        args = build_parser().parse_args(
            ["feed", "edit", "--post-id", POST_ID, "--text", "t",
             "--privacy", "public", "--ai-label", "on", "--background", "7",
             "--tag", "615937:Jane Doe", "--feeling", "1", "--activity", "2",
             "--place", "3", "--photo", "a.png", "--photo", "b.png"])
        assert args.post_id == POST_ID
        assert args.text == "t"
        assert args.privacy == "public"
        assert args.ai_label == "on"
        assert args.background == "7"
        assert args.tags == [("615937", "Jane Doe")]
        assert args.feeling == "1" and args.activity == "2"
        assert args.place_id == "3"
        assert args.photos == ["a.png", "b.png"]
        assert args.show is False
        assert args.fn is feed_cmd.cmd_edit

    def test_show_flag_parses(self):
        args = build_parser().parse_args(
            ["feed", "edit", "--post-id", POST_ID, "--show"])
        assert args.show is True
        assert args.fn is feed_cmd.cmd_edit


class TestEditCLI:
    """Command-level pins through the real parser + run_command."""

    def _stub_session(self, responses: dict[str, Any]) -> StubSession:
        return StubSession(responses=responses)

    def test_no_edit_flags_defaults_to_preview(self, monkeypatch, capsys):
        stub = self._stub_session({EDIT_DIALOG_QUERY: EDIT_DIALOG_PAYLOAD})
        monkeypatch.setattr(feed_cmd, "new_session", lambda a: stub)

        rc, _, err, payload = _run(
            capsys, ["feed", "edit", "--post-id", POST_ID, "--json"])

        assert rc == 0
        assert err == ""
        # exactly the dialog query — no mutation was sent
        assert len(stub.graphql.calls) == 1
        assert stub.graphql.calls[0][0] == EDIT_DIALOG_QUERY
        assert payload["post_id"] == POST_ID
        assert payload["editable"]["text"] == "the current words"
        assert payload["editable"]["privacy_base_state"] == "FRIENDS"

    def test_show_previews_without_mutating(self, monkeypatch, capsys):
        stub = self._stub_session({EDIT_DIALOG_QUERY: EDIT_DIALOG_PAYLOAD})
        monkeypatch.setattr(feed_cmd, "new_session", lambda a: stub)

        rc, _, _, payload = _run(
            capsys, ["feed", "edit", "--post-id", POST_ID,
                     "--show", "--json"])

        assert rc == 0
        assert len(stub.graphql.calls) == 1
        assert stub.graphql.calls[0][0] == EDIT_DIALOG_QUERY
        assert payload["editable"]["text"] == "the current words"

    def test_show_human_renderer_prints_the_state(self, monkeypatch, capsys):
        stub = self._stub_session({EDIT_DIALOG_QUERY: EDIT_DIALOG_PAYLOAD})
        monkeypatch.setattr(feed_cmd, "new_session", lambda a: stub)

        rc, out, _, _ = _run(
            capsys, ["feed", "edit", "--post-id", POST_ID, "--show"])

        assert rc == 0
        assert f"post: {POST_ID}" in out
        assert "text: the current words" in out
        assert "privacy: FRIENDS" in out

    def test_show_with_edit_flags_is_rejected(self, monkeypatch, capsys):
        stub = self._stub_session({})
        monkeypatch.setattr(feed_cmd, "new_session", lambda a: stub)

        rc, out, err, _ = _run(
            capsys, ["feed", "edit", "--post-id", POST_ID,
                     "--show", "--text", "t", "--photo", "a.png"])

        assert rc == 1
        assert err.startswith("error:")
        assert "--text" in err and "--photo" in err
        assert out == ""
        assert stub.graphql.calls == []  # nothing was sent

    def test_edit_delta_runs_the_mutation(self, monkeypatch, capsys):
        echo = {"data": {"composer_story_edit": {"success": True}}}
        stub = self._stub_session({EDIT_MUTATION: echo})
        monkeypatch.setattr(feed_cmd, "new_session", lambda a: stub)

        rc, _, _, payload = _run(
            capsys, ["feed", "edit", "--post-id", POST_ID,
                     "--text", "new words", "--privacy", "public",
                     "--ai-label", "on", "--background", "7", "--json"])

        assert rc == 0
        assert payload == echo
        assert len(stub.graphql.calls) == 1
        friendly, doc_id, variables = stub.graphql.calls[0]
        assert friendly == EDIT_MUTATION
        assert doc_id == EDIT_MUTATION_DOC_ID
        assert variables["input"]["message"]["text"] == "new words"
        assert variables["input"]["audience"]["privacy"]["base_state"] \
            == "EVERYONE"
        assert variables["input"]["ai_generated_self_disclosure_metadata"][
            "was_self_disclosed_as_ai_generated"] is True
        assert variables["input"]["text_format_preset_id"] == "7"
        story_token = base64.b64encode(
            f"S:_I{STUB_USER_ID}:{POST_ID}:{POST_ID}".encode()).decode()
        assert _get_by_path(variables, EDIT_STORY_ID_PATH) == story_token

    def test_photo_uploads_via_upload_surface_then_attaches(
            self, monkeypatch, capsys):
        echo = {"data": {"composer_story_edit": {"success": True}}}
        stub = self._stub_session({EDIT_MUTATION: echo})
        monkeypatch.setattr(feed_cmd, "new_session", lambda a: stub)
        rec = _UploadRecorder("1001", "1002")
        monkeypatch.setattr(UploadService, "upload_photo", rec)
        gaps: list[int] = []
        monkeypatch.setattr(feed_cmd, "_sleep_upload_gap",
                            lambda: gaps.append(1))

        rc, _, _, payload = _run(
            capsys, ["feed", "edit", "--post-id", POST_ID,
                     "--photo", "a.png", "--photo", "b.png", "--json"])

        assert rc == 0
        assert payload == echo
        # uploads went through the EXISTING upload surface, in order,
        # paced between like an album (one gap for two uploads)
        assert [str(p) for p in rec.paths] == ["a.png", "b.png"]
        assert len(gaps) == 1
        # the resulting ids rode the edit mutation as attachments
        friendly, _, variables = stub.graphql.calls[0]
        assert friendly == EDIT_MUTATION
        assert variables["input"]["attachments"] == [
            {"photo": {"id": "1001"}}, {"photo": {"id": "1002"}}]
