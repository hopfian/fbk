"""GraphQLClient error-guard wiring: the remaining typed-error mappings
and the escalation hooks that feed the governor (docs/11 §5 containment
— a checkpoint or rate-limit envelope must DISengage, not just raise).

Complements test_graphql_client.py (taxonomy basics, auto-refresh).

Unit (offline): canned StubTransport responses; the governor is a local
RecordingGovernor spy — no network, no real governor state.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import StubTransport, StubTransportResponse
from pydantic import SecretStr

from auth.bootstrap import Bootstrap
from auth.state import LoginState
from graphql.client import GraphQLClient
from graphql.errors import (
    CheckpointError,
    DocIdStaleError,
    GraphQLProtocolError,
    NotLoggedInError,
    RateLimitedError,
)


class RecordingGovernor:
    def __init__(self):
        self.calls: list[str] = []

    def observe_checkpoint(self):
        self.calls.append("checkpoint")

    def observe_soft_block(self):
        self.calls.append("soft_block")


def make_bootstrap() -> Bootstrap:
    return Bootstrap(state=LoginState.LOGGED_IN,
                     fb_dtsg=SecretStr("NAfTEST:1:1789723298"),
                     lsd=SecretStr("TESTLSD"), user_id="1")


def client_with(text: str, governor=None) -> GraphQLClient:
    transport = StubTransport([StubTransportResponse(text=text)])
    if governor is not None:
        transport.governor = governor
    return GraphQLClient(transport, make_bootstrap())


class TestErrorGuardWiring:
    """Pins the remaining typed-error mappings and the governor escalation
    hooks — checkpoint/rate-limit envelopes must DISengage (docs/11 §5)."""

    def test_doc_id_stale_code_maps_to_typed_error(self):
        client = client_with(
            '{"errors":[{"code":1570245,"message":"unknown doc"}]}')
        with pytest.raises(DocIdStaleError, match="re-harvest") as exc:
            client.call("CometX", "999", {})
        assert exc.value.code == 1570245
        assert exc.value.raw["message"] == "unknown doc"

    def test_checkpoint_envelope_notifies_the_governor(self):
        gov = RecordingGovernor()
        client = client_with(
            '{"errors":[{"code":1384000,"message":"checkpoint required"}]}',
            governor=gov)
        with pytest.raises(CheckpointError):
            client.call("Q", "1", {})
        assert gov.calls == ["checkpoint"]

    def test_rate_limit_envelope_notifies_the_governor(self):
        gov = RecordingGovernor()
        client = client_with(
            '{"errors":[{"code":1357046,"message":"slow down"}]}',
            governor=gov)
        with pytest.raises(RateLimitedError):
            client.call("Q", "1", {})
        assert gov.calls == ["soft_block"]

    def test_no_governor_attached_still_classifies(self):
        client = client_with(
            '{"errors":[{"code":1384000,"message":"checkpoint required"}]}')
        with pytest.raises(CheckpointError):
            client.call("Q", "1", {})

    def test_empty_errors_list_is_a_success(self):
        client = client_with(
            '{"data":{"viewer":{"ok":true}},"errors":[]}')
        assert client.call("Q", "1", {})["data"]["viewer"]["ok"] is True

    def test_first_error_in_the_array_wins(self):
        client = client_with(
            '{"errors":[{"code":111111,"message":"odd"},'
            '{"code":1384000,"message":"checkpoint"}]}')
        with pytest.raises(GraphQLProtocolError):
            client.call("Q", "1", {})


class TestStatusGuard:
    """Pins 301 Location routing: a checkpoint redirect raises
    CheckpointError, a login redirect raises NotLoggedInError."""

    def test_301_to_checkpoint_is_a_checkpoint_error(self):
        transport = StubTransport([StubTransportResponse(
            status_code=301, text="",
            headers={"location": "https://www.facebook.com/checkpoint/"})])
        client = GraphQLClient(transport, make_bootstrap())
        with pytest.raises(CheckpointError, match="redirected to checkpoint"):
            client.call("Q", "1", {})

    def test_301_to_login_is_not_logged_in(self):
        transport = StubTransport([StubTransportResponse(
            status_code=301, text="",
            headers={"location": "https://www.facebook.com/login/"})])
        client = GraphQLClient(transport, make_bootstrap())
        with pytest.raises(NotLoggedInError, match="redirected to login"):
            client.call("Q", "1", {})
