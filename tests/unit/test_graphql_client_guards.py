"""GraphQLClient constructor + registry guards (just-landed fixes).

Two seams: (1) the empty-DTSG guard — ``SecretStr("")`` is a PRESENT
wrapper with nothing inside, so the constructor unwraps BEFORE deciding
the session is unusable (NotLoggedInError at construction, not on the
wire); (2) ``call_by_name`` with no registry attached raises
``RegistryMissError`` — the operator's remediation is a registry
re-harvest, NOT a re-auth.

Complements test_graphql_client.py (wire shape, error taxonomy,
auto-refresh) and test_empty_dtsg_semantics.py (the Bootstrap /
CachedBootstrap accessors). Unit (offline): StubTransport; no network.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import StubTransport, StubTransportResponse

import constants as C
from auth.bootstrap import Bootstrap
from auth.state import LoginState
from graphql.client import GraphQLClient
from graphql.errors import NotLoggedInError, RegistryMissError


def make_bootstrap(**overrides) -> Bootstrap:
    """A logged-in bootstrap with the standard test CSRF pair."""
    base = {"state": LoginState.LOGGED_IN,
            "fb_dtsg": SecretStr("NAfTEST:1:1789723298"),
            "lsd": SecretStr("TESTLSD"), "user_id": "1"}
    base.update(overrides)
    return Bootstrap(**base)


class TestEmptyDtsgGuard:
    """Pins the unwrap-before-check constructor guard: a SecretStr('')
    wrapper passes an ``is not None`` check but carries no token — the
    client must refuse it here, not discover it on the wire."""

    def test_empty_dtsg_bootstrap_rejected_at_construction(self):
        boot = make_bootstrap(fb_dtsg=SecretStr(""))
        with pytest.raises(NotLoggedInError, match="fb_dtsg"):
            GraphQLClient(StubTransport([]), boot)

    def test_the_wrapper_is_present_but_valueless(self):
        """Documents the trap: fb_dtsg is NOT None — only dtsg() can tell."""
        boot = make_bootstrap(fb_dtsg=SecretStr(""))
        assert boot.fb_dtsg is not None
        assert boot.dtsg() is None

    def test_empty_dtsg_with_populated_lsd_still_rejected(self):
        boot = make_bootstrap(fb_dtsg=SecretStr(""), lsd=SecretStr("LSD"))
        with pytest.raises(NotLoggedInError):
            GraphQLClient(StubTransport([]), boot)

    def test_normal_token_constructs_cleanly(self):
        transport = StubTransport([])
        client = GraphQLClient(transport, make_bootstrap())
        assert client.transport is transport
        assert client.registry is None
        assert client.endpoint == C.GRAPHQL_ENDPOINT


class TestCallByNameWithoutRegistry:
    """Pins the no-registry miss: RegistryMissError (re-harvest guidance),
    never NotLoggedInError — and no wire call in flight."""

    def test_no_registry_raises_registry_miss(self):
        client = GraphQLClient(StubTransport([]), make_bootstrap())
        with pytest.raises(RegistryMissError, match="no registry"):
            client.call_by_name("CometTestQuery", {})

    def test_the_error_is_not_mistyped_as_a_login_failure(self):
        client = GraphQLClient(StubTransport([]), make_bootstrap())
        with pytest.raises(RegistryMissError) as exc:
            client.call_by_name("CometTestQuery", {})
        assert type(exc.value) is RegistryMissError
        assert not isinstance(exc.value, NotLoggedInError)

    def test_no_registry_never_touches_the_wire(self):
        transport = StubTransport([])
        client = GraphQLClient(transport, make_bootstrap())
        with pytest.raises(RegistryMissError):
            client.call_by_name("CometTestQuery", {})
        assert transport.posts == []

    def test_with_registry_a_real_call_still_flows(self):
        """Guard against over-tightening: an attached registry dispatches."""
        from graphql.registry import DocIdRegistry
        transport = StubTransport([StubTransportResponse(text='{"data":{}}')])
        client = GraphQLClient(transport, make_bootstrap(),
                               registry=DocIdRegistry.from_pairs({"K": "9"}))
        client.call_by_name("K", {})
        assert transport.posts[0]["data"]["doc_id"] == "9"
