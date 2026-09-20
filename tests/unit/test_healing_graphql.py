"""Unit tests for the GraphQL self-healing paths (src/graphql/client.py).

All tests execute offline against StubTransport/StubTransportResponse from
tests.fakes. The registry re-harvest is monkeypatched at its lazy import
site (graphql.registry_refresh.refresh_registry) so no bundle download
ever runs. Covers: the RegistryMissError heal in call_by_name (capped
re-harvest + fresh-registry adoption + re-resolution), the
DocIdStaleError heal in call (one retry on a genuinely DIFFERENT fresh
id, never a same-id re-fire), the dry-run refusal, the cooldown path,
and the healer-less back-compat behavior.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from fakes import StubTransport, StubTransportResponse

from graphql.client import GraphQLClient
from graphql.errors import DocIdStaleError, RegistryMissError
from graphql.registry import DocIdRegistry
from healing import (
    KIND_DOC_ID_RETRY,
    KIND_REGISTRY_REFRESH,
    HealingContext,
)

FAKE_REFRESH = "graphql.registry_refresh.refresh_registry"
OK = '{"data": {}}'
STALE = json.dumps({"errors": [{"code": 1570245,
                                "message": "Query with id not found"}]})


@pytest.fixture(autouse=True)
def heal_env(monkeypatch):
    """Pin the default healing configuration for every test."""
    for name in ("FBK_HEAL", "FBK_HEAL_REGISTRY_HOURS",
                 "FBK_HEAL_MAX_BUNDLES", "FBK_HEAL_TRANSPORT_RETRIES"):
        monkeypatch.delenv(name, raising=False)


def make_bootstrap():
    from pydantic import SecretStr

    from auth.bootstrap import Bootstrap
    from auth.state import LoginState
    return Bootstrap(state=LoginState.LOGGED_IN,
                     fb_dtsg=SecretStr("NAfTEST:1:1789723298"),
                     lsd=SecretStr("TESTLSD"), user_id="1")


def make_healer(tmp_path) -> HealingContext:
    config = SimpleNamespace(assets_dir=tmp_path / "data",
                             journal_dir=tmp_path / "state")
    return HealingContext(config,
                          log_path=tmp_path / "state" / "healing.jsonl")


def stub_refresh(monkeypatch, fresh_pairs: dict[str, str]) -> SimpleNamespace:
    """Patch the lazy re-harvest import to return a fresh registry.

    Also drops the verification floor to 1 pair: the fixtures carry a
    single operation, far below the real floor of 200 — the floor's
    degenerate-harvest rollback is covered by its own dedicated test.
    """
    fresh = SimpleNamespace(added=dict(fresh_pairs), changed={},
                            bundles_fetched=5, fetch_errors=0)
    registry = DocIdRegistry.from_pairs(fresh_pairs)

    def fake_refresh(config, *, save, max_bundles):
        assert save is True
        return fresh

    monkeypatch.setattr(FAKE_REFRESH, fake_refresh, raising=False)
    monkeypatch.setattr(
        DocIdRegistry, "from_assets",
        classmethod(lambda cls, path: registry))
    monkeypatch.setattr("healing.MIN_HARVEST_PAIRS", 1, raising=False)
    return fresh


class TestCallByNameRegistryMissHeal:
    """The RegistryMissError heal: re-harvest, adopt, re-resolve."""

    def test_miss_heals_and_calls_with_fresh_id(self, tmp_path, monkeypatch,
                                                capsys):
        stub_refresh(monkeypatch, {"CometModernHomeFeedQuery": "999"})
        transport = StubTransport([StubTransportResponse(text=OK)])
        client = GraphQLClient(transport, make_bootstrap(),
                               registry=DocIdRegistry.from_pairs({}),
                               healer=make_healer(tmp_path))
        merged = client.call_by_name("CometModernHomeFeedQuery", {})
        assert merged == {"data": {}}
        # the wire carried the FRESH doc_id, not the empty-registry miss
        assert transport.posts[0]["data"]["doc_id"] == "999"
        # the client adopted the reloaded registry for subsequent calls
        assert client.registry.get("CometModernHomeFeedQuery") == "999"
        err = capsys.readouterr().err
        assert "[heal] registry-refresh" in err

    def test_failed_harvest_propagates_the_miss(self, tmp_path, monkeypatch):
        def boom(config, *, save, max_bundles):
            raise RuntimeError("edge down")

        monkeypatch.setattr(FAKE_REFRESH, boom, raising=False)
        transport = StubTransport([])
        client = GraphQLClient(transport, make_bootstrap(),
                               registry=DocIdRegistry.from_pairs({}),
                               healer=make_healer(tmp_path))
        with pytest.raises(RegistryMissError):
            client.call_by_name("CometModernHomeFeedQuery", {})
        rows = (tmp_path / "state" / "healing.jsonl").read_text(
            encoding="utf-8").splitlines()
        assert json.loads(rows[0])["trigger"] == "re-harvest failed"

    def test_cooldown_blocks_the_heal(self, tmp_path, monkeypatch):
        def must_not_run(config, *, save, max_bundles):
            raise AssertionError("harvest inside cooldown")

        monkeypatch.setattr(FAKE_REFRESH, must_not_run, raising=False)
        healer = make_healer(tmp_path)
        healer.log.append(KIND_REGISTRY_REFRESH, "recent", ts=time.time())
        transport = StubTransport([])
        client = GraphQLClient(transport, make_bootstrap(),
                               registry=DocIdRegistry.from_pairs({}),
                               healer=healer)
        with pytest.raises(RegistryMissError):
            client.call_by_name("CometModernHomeFeedQuery", {})

    def test_master_off_blocks_the_heal(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FBK_HEAL", "off")

        def must_not_run(config, *, save, max_bundles):
            raise AssertionError("harvest must not run when healing is off")

        monkeypatch.setattr(FAKE_REFRESH, must_not_run, raising=False)
        transport = StubTransport([])
        client = GraphQLClient(transport, make_bootstrap(),
                               registry=DocIdRegistry.from_pairs({}),
                               healer=make_healer(tmp_path))
        with pytest.raises(RegistryMissError):
            client.call_by_name("CometModernHomeFeedQuery", {})

    def test_dry_run_never_heals(self, tmp_path, monkeypatch):
        def must_not_run(config, *, save, max_bundles):
            raise AssertionError("dry-run must not trigger a harvest")

        monkeypatch.setattr(FAKE_REFRESH, must_not_run, raising=False)
        transport = StubTransport([])
        client = GraphQLClient(transport, make_bootstrap(),
                               registry=DocIdRegistry.from_pairs({}),
                               dry_run=True, healer=make_healer(tmp_path))
        with pytest.raises(RegistryMissError):
            client.call_by_name("CometModernHomeFeedQuery", {})
        assert transport.posts == []

    def test_healer_less_client_unchanged(self):
        transport = StubTransport([])
        client = GraphQLClient(transport, make_bootstrap(),
                               registry=DocIdRegistry.from_pairs({}))
        with pytest.raises(RegistryMissError):
            client.call_by_name("CometModernHomeFeedQuery", {})
        assert transport.posts == []

    def test_degenerate_harvest_is_refused(self, tmp_path, monkeypatch):
        # a harvest whose fresh registry holds fewer than MIN_HARVEST_PAIRS
        # pairs is treated as a degenerate parse and rolled back — the
        # typed miss propagates, no retry happens
        stub_refresh(monkeypatch, {"CometModernHomeFeedQuery": "999"})
        monkeypatch.setattr("healing.MIN_HARVEST_PAIRS", 200, raising=False)
        transport = StubTransport([])
        client = GraphQLClient(transport, make_bootstrap(),
                               registry=DocIdRegistry.from_pairs({}),
                               healer=make_healer(tmp_path))
        with pytest.raises(RegistryMissError):
            client.call_by_name("CometModernHomeFeedQuery", {})
        rows = [json.loads(line) for line in
                (tmp_path / "state" / "healing.jsonl").read_text(
                    encoding="utf-8").splitlines()]
        assert "rolled back" in rows[-1]["trigger"]


class TestCallDocIdStaleHeal:
    """The DocIdStaleError heal: one retry, only on a genuinely fresh id."""

    def test_stale_id_heals_and_retries_with_fresh_id(self, tmp_path,
                                                      monkeypatch):
        stub_refresh(monkeypatch, {"CometModernHomeFeedQuery": "222"})
        transport = StubTransport([
            StubTransportResponse(text=STALE),
            StubTransportResponse(text=OK),
        ])
        client = GraphQLClient(transport, make_bootstrap(),
                               registry=DocIdRegistry.from_pairs(
                                   {"CometModernHomeFeedQuery": "111"}),
                               healer=make_healer(tmp_path))
        merged = client.call("CometModernHomeFeedQuery", "111", {})
        assert merged == {"data": {}}
        assert [p["data"]["doc_id"] for p in transport.posts] == ["111", "222"]
        rows = [json.loads(line) for line in
                (tmp_path / "state" / "healing.jsonl").read_text(
                    encoding="utf-8").splitlines()]
        kinds = [row["kind"] for row in rows]
        assert kinds == [KIND_REGISTRY_REFRESH, KIND_DOC_ID_RETRY]
        assert "222" in rows[1]["detail"]

    def test_same_fresh_id_never_refires(self, tmp_path, monkeypatch):
        # the harvest resolved the SAME id: the failure is shape drift, a
        # caller bug — re-firing the identical body would fail identically
        stub_refresh(monkeypatch, {"CometModernHomeFeedQuery": "111"})
        transport = StubTransport([StubTransportResponse(text=STALE)])
        client = GraphQLClient(transport, make_bootstrap(),
                               registry=DocIdRegistry.from_pairs(
                                   {"CometModernHomeFeedQuery": "111"}),
                               healer=make_healer(tmp_path))
        with pytest.raises(DocIdStaleError):
            client.call("CometModernHomeFeedQuery", "111", {})
        rows = [json.loads(line) for line in
                (tmp_path / "state" / "healing.jsonl").read_text(
                    encoding="utf-8").splitlines()]
        assert [row["kind"] for row in rows] == [KIND_REGISTRY_REFRESH]
        assert len(transport.posts) == 1  # no second send

    def test_stale_heal_not_recurring_within_one_call(self, tmp_path,
                                                      monkeypatch):
        # after one heal, a SECOND stale rejection propagates: the caps
        # make the recursion terminate (docs: healing.py module contract)
        stub_refresh(monkeypatch, {"CometModernHomeFeedQuery": "222"})
        transport = StubTransport([
            StubTransportResponse(text=STALE),
            StubTransportResponse(text=STALE),
        ])
        client = GraphQLClient(transport, make_bootstrap(),
                               registry=DocIdRegistry.from_pairs(
                                   {"CometModernHomeFeedQuery": "111"}),
                               healer=make_healer(tmp_path))
        with pytest.raises(DocIdStaleError):
            client.call("CometModernHomeFeedQuery", "111", {})
        assert len(transport.posts) == 2

    def test_stale_without_healer_propagates(self):
        transport = StubTransport([StubTransportResponse(text=STALE)])
        client = GraphQLClient(transport, make_bootstrap(),
                               registry=DocIdRegistry.from_pairs(
                                   {"CometModernHomeFeedQuery": "111"}))
        with pytest.raises(DocIdStaleError):
            client.call("CometModernHomeFeedQuery", "111", {})

    def test_dry_run_stale_never_heals(self, tmp_path, monkeypatch):
        def must_not_run(config, *, save, max_bundles):
            raise AssertionError("dry-run must not trigger a harvest")

        monkeypatch.setattr(FAKE_REFRESH, must_not_run, raising=False)
        transport = StubTransport([StubTransportResponse(text=STALE)])
        client = GraphQLClient(transport, make_bootstrap(),
                               registry=DocIdRegistry.from_pairs(
                                   {"CometModernHomeFeedQuery": "111"}),
                               dry_run=True, healer=make_healer(tmp_path))
        # dry-run stops at the plan BEFORE any error classification — the
        # stale envelope is never even parsed (the guard in call() is the
        # defense-in-depth backstop; the plan sentinel is what fires)
        from graphql.errors import DryRunComplete
        with pytest.raises(DryRunComplete):
            client.call("CometModernHomeFeedQuery", "111", {})
        # the plan was printed and raised BEFORE any send: the stub never
        # recorded a post, and no harvest ran (the monkeypatch would have
        # raised AssertionError otherwise)
        assert transport.posts == []
