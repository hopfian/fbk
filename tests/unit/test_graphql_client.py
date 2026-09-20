"""GraphQLClient unit tests: canonical wire shape, error taxonomy, auto-refresh.

Unit (offline): every response is a canned StubTransportResponse queued on
StubTransport from ``tests.fakes`` — no network, no cookies, no governor.
Covers the doc_id form body (fb_dtsg/lsd/q/variables), the typed error-code
mapping, the DTSG-rejection auto-refresh cycle, and registry-backed
call_by_name dispatch.
"""
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
    GraphQLProtocolError,
    NotLoggedInError,
    RateLimitedError,
)


def make_bootstrap() -> Bootstrap:
    return Bootstrap(state=LoginState.LOGGED_IN,
                    fb_dtsg=SecretStr("NAfTEST:1:1789723298"),
                    lsd=SecretStr("TESTLSD"), user_id="1")


class TestWireShape:
    """Pins the canonical form body the edge expects (doc_id, fb_dtsg, lsd, q)."""

    def test_canonical_body(self):
        transport = StubTransport([StubTransportResponse(text='{"data": {}}')])
        client = GraphQLClient(transport, make_bootstrap())
        client.call("CometTestQuery", "123", {"scale": 1})
        post = transport.posts[0]
        assert post["friendly_name"] == "CometTestQuery"
        data = post["data"]
        assert data["doc_id"] == "123"
        assert data["fb_api_caller_class"] == "RelayModern"
        assert data["server_timestamps"] == "true"
        assert data["fb_dtsg"] == "NAfTEST:1:1789723298"
        assert data["lsd"] == "TESTLSD"
        assert data["q"] == "1"
        assert data["variables"] == '{"scale":1}'

    def test_q_counter_increments(self):
        transport = StubTransport([StubTransportResponse(text='{"data": {}}'),
                                   StubTransportResponse(text='{"data": {}}')])
        client = GraphQLClient(transport, make_bootstrap())
        client.call("A", "1", {})
        client.call("B", "2", {})
        assert [t["data"]["q"] for t in transport.posts] == ["1", "2"]

    def test_missing_dtsg_rejected_at_construction(self):
        boot = Bootstrap(state=LoginState.LOGGED_OUT)
        with pytest.raises(NotLoggedInError):
            GraphQLClient(StubTransport([]), boot)


class TestErrorTaxonomy:
    """Pins the typed mapping from error codes and HTTP statuses to errors."""

    def _client(self, text: str):
        return GraphQLClient(StubTransport(
            [StubTransportResponse(text=text)]), make_bootstrap())

    def test_not_logged_in_code(self):
        client = self._client('{"errors":[{"code":1357004,"message":"not logged in"}]}')
        with pytest.raises(NotLoggedInError):
            client.call("Q", "1", {})

    def test_checkpoint_code(self):
        client = self._client('{"errors":[{"code":1384000,"message":"checkpoint"}]}')
        with pytest.raises(CheckpointError):
            client.call("Q", "1", {})

    def test_rate_limit_code(self):
        client = self._client('{"errors":[{"code":1357046,"message":"slow down"}]}')
        with pytest.raises(RateLimitedError):
            client.call("Q", "1", {})

    def test_live_1675012_envelope_maps_to_protocol_error(self):
        """The exact envelope shape from docs/15 §4."""
        client = self._client('{"errors":[{"message":"A server error '
                              'noncoercible_variable_value occured.","severity":'
                              '"CRITICAL","code":1675012}]}')
        with pytest.raises(GraphQLProtocolError, match="noncoercible"):
            client.call("CometX", "1", {})

    def test_unknown_structured_error_surfaces(self):
        client = self._client('{"errors":[{"code":999999,"message":"odd"}]}')
        with pytest.raises(GraphQLProtocolError) as exc:
            client.call("Q", "1", {})
        assert exc.value.raw and exc.value.raw[0]["code"] == 999999

    def test_unparseable_200_is_soft_block(self):
        client = self._client("<html>login wall</html>")
        with pytest.raises(RateLimitedError, match="soft-block"):
            client.call("Q", "1", {})

    def test_403_is_rate_limited(self):
        transport = StubTransport([StubTransportResponse(status_code=403, text="")])
        client = GraphQLClient(transport, make_bootstrap())
        with pytest.raises(RateLimitedError):
            client.call("Q", "1", {})

    def test_redirect_to_login(self):
        transport = StubTransport([StubTransportResponse(
            status_code=302, text="", url="https://www.facebook.com/login",
            headers={"location": "https://www.facebook.com/login"})])
        client = GraphQLClient(transport, make_bootstrap())
        with pytest.raises(NotLoggedInError, match="redirected to login"):
            client.call("Q", "1", {})


class TestAutoRefresh:
    """Pins the DTSG-rejection auto-refresh cycle (code 1677047 -> re-bootstrap)."""

    def test_dtsg_rejection_refreshes_and_retries(self, monkeypatch):
        responses = [
            StubTransportResponse(text='{"errors":[{"code":1677047,"message":'
                                       '"dtsg rejected"}]}'),
            StubTransportResponse(text='{"data":{"viewer":{"ok":true}}}'),
        ]
        transport = StubTransport(responses)
        client = GraphQLClient(transport, make_bootstrap())

        def fake_bootstrap_homepage(transport, cookies):
            return make_bootstrap()  # fresh tokens, same values
        monkeypatch.setattr("auth.bootstrap.bootstrap_homepage",
                            fake_bootstrap_homepage)
        out = client.call("Q", "1", {})
        assert out["data"]["viewer"]["ok"] is True
        assert len(transport.posts) == 2  # failed attempt + retry

    def test_refresh_exhaustion_raises(self, monkeypatch):
        responses = [StubTransportResponse(
            text='{"errors":[{"code":1677047,"message":"dtsg rejected"}]}')] * 3
        transport = StubTransport(responses)
        client = GraphQLClient(transport, make_bootstrap())

        def fake_bootstrap_homepage(transport, cookies):
            return make_bootstrap()
        monkeypatch.setattr("auth.bootstrap.bootstrap_homepage",
                            fake_bootstrap_homepage)
        with pytest.raises(NotLoggedInError):
            client.call("Q", "1", {})
        assert len(transport.posts) == 2  # max_refresh = 1 retry


class TestCallByName:
    """Pins registry-backed friendly-name dispatch and its miss path."""

    def test_registry_backed(self):
        transport = StubTransport([StubTransportResponse(text='{"data":{}}')])
        from graphql.registry import DocIdRegistry
        reg = DocIdRegistry.from_pairs({"CometKnown": "42"})
        client = GraphQLClient(transport, make_bootstrap(), registry=reg)
        client.call_by_name("CometKnown", {})
        assert transport.posts[0]["data"]["doc_id"] == "42"

    def test_registry_miss_propagates(self):
        transport = StubTransport([])
        from graphql.errors import RegistryMissError
        from graphql.registry import DocIdRegistry
        client = GraphQLClient(transport, make_bootstrap(),
                               registry=DocIdRegistry.from_pairs({}))
        with pytest.raises(RegistryMissError):
            client.call_by_name("NotThere", {})
