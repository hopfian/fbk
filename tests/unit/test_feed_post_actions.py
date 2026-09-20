"""Post-centric 3-dot action tests — offline, against the REAL captured
assets.

Pins this wave's additions: the post-id-first feedback derivation (the
GROUNDED b64 codec pin — feedback:<post_id> ↔ "ZmVlZGJhY2s6…"), the
--post-id/--feedback CLI resolution (exactly-one-of-two), the share
variables build over the captured composer template (the
SHARE_ATTACHMENT_CANDIDATE element — CANDIDATE, probe pending), the
feed-save convenience delegation to the saved surface, and the notify
candidate variables (CANDIDATE, probe pending). Every candidate is
pinned through the MODULE CONSTANTS in surfaces/feed.py so a
post-probe correction stays one edit.
"""
from __future__ import annotations

import argparse
import copy
import json
import uuid
from typing import Any

import pytest
from fakes import StubSession

import commands.feed as feed_commands
from commands.feed import _resolve_feedback, register
from domain.common import FeedbackID, Privacy
from surfaces.feed import (
    NOTIFY_SUBSCRIBE_MUTATION,
    NOTIFY_UNSUBSCRIBE_MUTATION,
    NOTIFY_VARIABLES_CANDIDATE,
    SHARE_ATTACHMENT_CANDIDATE,
    FeedService,
    feedback_for_post,
)

# The live-captured wire ids (docs/15 ground truth).
COMPOSER_MUTATION = "ComposerStoryCreateMutation"
COMPOSER_DOC_ID = "28778531428503134"      # KNOWN_MUTATIONS (live-verified)
SAVE_MUTATION_DOC_ID = "9855506394526824"  # CometSaveMutation (live-verified)
# Registry-grounded (data/doc_id_registry_v3.json "carried" entries):
NOTIFY_SUBSCRIBE_DOC_ID = "9594760067273294"
NOTIFY_UNSUBSCRIBE_DOC_ID = "25718290144521573"

POST_ID = "3937024329774036"
# The GROUNDED fixture pin: b64("feedback:3937024329774036").
FB_SAMPLE = "ZmVlZGJhY2s6MzkzNzAyNDMyOTc3NDAzNg"
ACTOR_ID = "10000000000000008"
STUB_USER_ID = "12345678901234"

SAVE_RESPONSE: dict[str, Any] = {
    "data": {"node_saved_state": {
        "save_node": {"id": POST_ID, "viewer_saved_state": "SAVED"}}}}
UNSUBSCRIBE_RESPONSE: dict[str, Any] = {"data": {}}


# --------------------------------------------------------------------- harness
def _parser() -> argparse.ArgumentParser:
    """The feed family wired the way app.py wires it (direct register())."""
    parser = argparse.ArgumentParser(prog="fbk", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    register(sub)
    return parser


def _run(monkeypatch, capsys, stub: StubSession,
         argv: list[str]) -> tuple[int, str]:
    """Parse+run one feed invocation; return (rc, stdout)."""
    args = _parser().parse_args(argv)
    monkeypatch.setattr(feed_commands, "new_session", lambda a: stub)
    rc = args.fn(args)
    return rc, capsys.readouterr().out


def _last_call(stub: StubSession) -> tuple[str, str, dict]:
    assert stub.graphql.calls, "service made no client call"
    return stub.graphql.calls[-1]


# ------------------------------------------------------------ the codec pin
class TestFeedbackCodec:
    """The GROUNDED post-id → feedback-id derivation: b64
    "feedback:<post_id>", pinned against the live-captured fixture."""

    def test_feedback_for_post_matches_live_fixture(self):
        assert feedback_for_post(POST_ID).raw == FB_SAMPLE

    def test_feedback_derivation_round_trips(self):
        assert FeedbackID.from_b64(FB_SAMPLE).numeric == POST_ID
        assert str(feedback_for_post(POST_ID)) == FB_SAMPLE


# --------------------------------------------------- --post-id CLI resolution
class TestPostIdCliResolution:
    """Pins the --post-id / positional-feedback resolution: derivation
    through the codec, positional passthrough, and the both/neither
    rejections."""

    def test_react_post_id_derives_the_feedback_handle(self, monkeypatch,
                                                        capsys):
        stub = StubSession(responses={
            "CometUFIFeedbackReactMutation": {"data": {}}})
        rc, _ = _run(monkeypatch, capsys, stub,
                     ["feed", "react", "--post-id", POST_ID,
                      "--reaction", "LIKE", "--json"])
        assert rc == 0
        _, doc_id, variables = _last_call(stub)
        assert doc_id == "27646120298312844"
        # the codec-derived wire token, not the raw post id
        assert variables["input"]["feedback_id"] == FB_SAMPLE
        assert variables["input"]["feedback_reaction_id"] == "1635855486666999"

    def test_react_positional_feedback_passes_through(self, monkeypatch,
                                                      capsys):
        stub = StubSession(responses={
            "CometUFIFeedbackReactMutation": {"data": {}}})
        rc, _ = _run(monkeypatch, capsys, stub,
                     ["feed", "react", FB_SAMPLE, "--reaction", "LOVE", "--json"])
        assert rc == 0
        _, _, variables = _last_call(stub)
        assert variables["input"]["feedback_id"] == FB_SAMPLE

    def test_unreact_and_comment_accept_post_ids(self, monkeypatch, capsys):
        stub = StubSession(responses={
            "CometUFIFeedbackReactMutation": {"data": {}},
            "useCometUFICreateCommentMutation": {"data": {}}})
        rc, _ = _run(monkeypatch, capsys, stub,
                     ["feed", "unreact", "--post-id", POST_ID, "--json"])
        assert rc == 0
        _, _, variables = _last_call(stub)
        assert variables["input"]["feedback_reaction_id"] == "0"
        assert variables["input"]["feedback_id"] == FB_SAMPLE

        rc, _ = _run(monkeypatch, capsys, stub,
                     ["feed", "comment", "--post-id", POST_ID,
                      "--text", "hello", "--json"])
        assert rc == 0
        _, _, variables = _last_call(stub)
        assert variables["input"]["feedback_id"] == FB_SAMPLE
        assert variables["input"]["message"]["text"] == "hello"

    def test_both_ids_rejected(self, monkeypatch, capsys):
        stub = StubSession(responses={})
        args = _parser().parse_args(
            ["feed", "react", FB_SAMPLE, "--post-id", POST_ID,
             "--reaction", "LIKE"])
        monkeypatch.setattr(feed_commands, "new_session", lambda a: stub)
        with pytest.raises(ValueError, match="not both"):
            args.fn(args)
        assert stub.graphql.calls == []  # nothing was sent

    def test_neither_id_rejected(self, monkeypatch, capsys):
        stub = StubSession(responses={})
        args = _parser().parse_args(["feed", "react", "--reaction", "LIKE"])
        monkeypatch.setattr(feed_commands, "new_session", lambda a: stub)
        with pytest.raises(ValueError, match="a target is required"):
            args.fn(args)
        assert stub.graphql.calls == []

    def test_resolver_rejects_neither_directly(self):
        with pytest.raises(ValueError):
            _resolve_feedback(argparse.Namespace(feedback_id=None, post_id=None))
        with pytest.raises(ValueError):
            _resolve_feedback(argparse.Namespace(feedback_id=FB_SAMPLE,
                                                 post_id=POST_ID))


# ------------------------------------------------------------------- share
class TestShare:
    """Pins the share variables build: the captured composer template,
    the SHARE_ATTACHMENT_CANDIDATE element (CANDIDATE — probe pending),
    text/privacy substitution, and fresh idempotence tokens."""

    def _share(self, **kwargs: Any) -> tuple[str, str, dict]:
        stub = StubSession(responses={COMPOSER_MUTATION: {"data": {}}})
        FeedService(stub).share(POST_ID, **kwargs)
        return _last_call(stub)

    def test_share_builds_candidate_attachment_and_text(self):
        friendly, doc_id, variables = self._share(text="worth reading",
                                                  privacy=Privacy.PUBLIC)
        assert friendly == COMPOSER_MUTATION
        assert doc_id == COMPOSER_DOC_ID
        # the attachment is the module constant's shape with the post id
        # filled in — a post-probe field rename stays one edit
        expected = copy.deepcopy(SHARE_ATTACHMENT_CANDIDATE)
        expected["share"]["shareable_id"] = POST_ID
        assert variables["input"]["attachments"] == [expected]
        assert variables["input"]["message"]["text"] == "worth reading"
        assert variables["input"]["audience"]["privacy"]["base_state"] \
            == "EVERYONE"
        assert variables["input"]["actor_id"] == STUB_USER_ID
        # fresh idempotence/session tokens, never the captured ones
        token = variables["input"]["idempotence_token"]
        assert token.endswith("_FEED")
        assert uuid.UUID(token.removesuffix("_FEED")).version == 4
        assert variables["input"]["logging"]["composer_session_id"] != \
            "be80ba06-2640-452b-a5f7-1fb89e84aa7b"

    def test_share_defaults_empty_text_friends_privacy(self):
        _, _, variables = self._share()
        assert variables["input"]["message"]["text"] == ""
        assert variables["input"]["audience"]["privacy"]["base_state"] \
            == "FRIENDS"

    def test_share_candidate_constant_not_contaminated(self):
        self._share(text="one")
        self._share(text="two")
        # the module constant stays pristine across calls
        assert SHARE_ATTACHMENT_CANDIDATE["share"]["shareable_id"] is None

    def test_share_cli(self, monkeypatch, capsys):
        stub = StubSession(responses={
            COMPOSER_MUTATION: {"data": {"story_create": {}}}})
        rc, out = _run(monkeypatch, capsys, stub,
                       ["feed", "share", "--post-id", POST_ID,
                        "--text", "look at this", "--privacy", "public",
                        "--json"])
        assert rc == 0
        friendly, _, variables = _last_call(stub)
        assert friendly == COMPOSER_MUTATION
        assert variables["input"]["message"]["text"] == "look at this"
        assert variables["input"]["audience"]["privacy"]["base_state"] \
            == "EVERYONE"
        assert json.loads(out)["data"]["story_create"] == {}

    def test_share_cli_defaults(self, monkeypatch, capsys):
        stub = StubSession(responses={COMPOSER_MUTATION: {"data": {}}})
        rc, _ = _run(monkeypatch, capsys, stub,
                     ["feed", "share", "--post-id", POST_ID, "--json"])
        assert rc == 0
        _, _, variables = _last_call(stub)
        assert variables["input"]["message"]["text"] == ""
        assert variables["input"]["audience"]["privacy"]["base_state"] \
            == "FRIENDS"


# ------------------------------------------------------------- feed save
class TestFeedSaveConvenience:
    """Pins the feed-save convenience: one CometSaveMutation call that
    delegates to the saved surface verbatim (the id passes through
    unchanged — the savable-node-id finding)."""

    def test_save_delegates_to_the_saved_service(self, monkeypatch, capsys):
        stub = StubSession(responses={"CometSaveMutation": SAVE_RESPONSE})
        rc, out = _run(monkeypatch, capsys, stub,
                       ["feed", "save", "--post-id", POST_ID, "--json"])
        assert rc == 0
        friendly, doc_id, variables = _last_call(stub)
        assert friendly == "CometSaveMutation"
        assert doc_id == SAVE_MUTATION_DOC_ID
        # the pass-through convenience: the id form is untouched
        assert variables["input"]["node_id"] == POST_ID
        assert variables["input"]["save_action"] == "SAVE"
        payload = json.loads(out)
        assert payload["post_id"] == POST_ID
        assert payload["viewer_saved_state"] == "SAVED"

    def test_save_human_line(self, monkeypatch, capsys):
        stub = StubSession(responses={"CometSaveMutation": SAVE_RESPONSE})
        rc, out = _run(monkeypatch, capsys, stub,
                       ["feed", "save", "--post-id", POST_ID])
        assert rc == 0
        assert f"saved {POST_ID} -> SAVED" in out


# ------------------------------------------------------------------ notify
class TestNotify:
    """Pins the notify pair: registry-grounded doc_ids (v3 "carried"),
    the NOTIFY_VARIABLES_CANDIDATE envelope (CANDIDATE — probe pending)
    with a fresh client_mutation_id, and the state gate."""

    def test_notify_on_subscribes(self):
        stub = StubSession(
            responses={NOTIFY_SUBSCRIBE_MUTATION: {"data": {}}})
        FeedService(stub).notify(ACTOR_ID, "on")

        friendly, doc_id, variables = _last_call(stub)
        assert friendly == NOTIFY_SUBSCRIBE_MUTATION
        # registry-grounded doc_id (v3 "carried" entry)
        assert doc_id == NOTIFY_SUBSCRIBE_DOC_ID
        # the candidate envelope with actor + fresh mutation id
        expected = copy.deepcopy(NOTIFY_VARIABLES_CANDIDATE)
        expected["input"]["actor_id"] = ACTOR_ID
        expected["input"]["client_mutation_id"] = \
            variables["input"]["client_mutation_id"]
        assert variables == expected
        assert uuid.UUID(variables["input"]["client_mutation_id"]).version == 4

    def test_notify_off_unsubscribes(self):
        stub = StubSession(
            responses={NOTIFY_UNSUBSCRIBE_MUTATION: UNSUBSCRIBE_RESPONSE})
        response = FeedService(stub).notify(ACTOR_ID, "off")

        assert response == UNSUBSCRIBE_RESPONSE
        friendly, doc_id, variables = _last_call(stub)
        assert friendly == NOTIFY_UNSUBSCRIBE_MUTATION
        assert doc_id == NOTIFY_UNSUBSCRIBE_DOC_ID
        assert variables["input"]["actor_id"] == ACTOR_ID

    def test_notify_rejects_bad_state(self):
        service = FeedService(StubSession(responses={}))
        with pytest.raises(ValueError):
            service.notify(ACTOR_ID, "nonsense")  # type: ignore[arg-type]

    def test_notify_cli(self, monkeypatch, capsys):
        stub = StubSession(
            responses={NOTIFY_SUBSCRIBE_MUTATION: {"data": {}}})
        rc, out = _run(monkeypatch, capsys, stub,
                       ["feed", "notify", "--actor-id", ACTOR_ID, "on",
                        "--json"])
        assert rc == 0
        friendly, doc_id, _ = _last_call(stub)
        assert friendly == NOTIFY_SUBSCRIBE_MUTATION
        assert doc_id == NOTIFY_SUBSCRIBE_DOC_ID
        payload = json.loads(out)
        assert payload["actor_id"] == ACTOR_ID
        assert payload["state"] == "on"

    def test_notify_cli_human_line(self, monkeypatch, capsys):
        stub = StubSession(
            responses={NOTIFY_UNSUBSCRIBE_MUTATION: {"data": {}}})
        rc, out = _run(monkeypatch, capsys, stub,
                       ["feed", "notify", "--actor-id", ACTOR_ID, "off"])
        assert rc == 0
        assert f"notifications off for actor {ACTOR_ID}" in out
