"""Unit tests for the session/surface/governor self-healing wiring.

All tests execute offline — no network, no cookies beyond a synthetic
tmp jar. Covers the three wiring points the healing wave round two
added: Session.registry adopting a healed registry when every on-disk
registry tier is corrupt (RegistryLoadError), SurfaceService.doc_id
healing a registry miss through the coordinator (with the dry-run
refusal), and the governor's corrupt-state rebuild event.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fakes import StubTransport

import session as session_module
from config import Config
from governor import RequestGovernor
from graphql.errors import RegistryLoadError, RegistryMissError
from graphql.registry import DocIdRegistry
from healing import KIND_GOVERNOR_STATE_REBUILD, HealingContext, HealingLog
from session import Session
from surfaces.base import Surface

FAKE_REFRESH = "graphql.registry_refresh.refresh_registry"

COOKIES_TXT = (
    "# Netscape HTTP Cookie File\n"
    ".facebook.com\tTRUE\t/\tTRUE\t2147483647\tc_user\t12345678901234\n"
    ".facebook.com\tTRUE\t/\tTRUE\t2147483647\txs\tTEST-XS\n"
    ".facebook.com\tTRUE\t/\tTRUE\t2147483647\tdatr\tTEST-DATR\n"
)


@pytest.fixture(autouse=True)
def heal_env(monkeypatch):
    """Pin the default healing configuration for every test."""
    for name in ("FBK_HEAL", "FBK_HEAL_REGISTRY_HOURS",
                 "FBK_HEAL_MAX_BUNDLES", "FBK_HEAL_TRANSPORT_RETRIES"):
        monkeypatch.delenv(name, raising=False)


def _corrupt_registry_env(tmp_path):
    """An assets dir where EVERY registry tier is corrupt."""
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    for name in ("doc_id_registry_v3.json", "doc_id_registry_v2.json"):
        (data / name).write_text("{torn", encoding="utf-8")
    return data


class _SessionTransport(StubTransport):
    """StubTransport accepting the Session constructor's full kwarg set."""

    def __init__(self, **kwargs):
        super().__init__([])


def make_session(tmp_path, monkeypatch) -> Session:
    """A real Session over a stub transport and tmp state/cookies.

    Both mutable dirs are redirected into tmp_path: state/ for the
    healing log + governor, data/ for the registry tiers under test.
    """
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(COOKIES_TXT, encoding="utf-8")
    monkeypatch.setattr(session_module, "FBTransport", _SessionTransport,
                        raising=False)
    cfg = Config.discover().model_copy(update={
        "cookies_path": cookies,
        "state_dir": tmp_path / "state",
        "data_dir": tmp_path / "data",
    })
    return Session(cfg, journal_name=None)


class TestSessionRegistryCorruptionHeal:
    """Every registry tier corrupt -> the coordinator re-harvests."""

    def test_all_tiers_corrupt_heals_and_adopts_fresh(
            self, tmp_path, monkeypatch):
        data = _corrupt_registry_env(tmp_path)

        def fake_refresh(config, *, save, max_bundles):
            # simulate the verified harvest: a healthy v3 lands on disk
            doc = {"revision": "rev-healed",
                   "unique_pairs": [{"friendly_name":
                                     "CometModernHomeFeedQuery",
                                     "doc_id": "999"}]}
            (data / "doc_id_registry_v3.json").write_text(
                json.dumps(doc), encoding="utf-8")
            return SimpleNamespace(added={}, changed={}, bundles_fetched=5,
                                   fetch_errors=0)

        monkeypatch.setattr(FAKE_REFRESH, fake_refresh, raising=False)
        session = make_session(tmp_path, monkeypatch)
        session.healer.log_path = None  # unused; the fake does the writing
        # the REAL floor: a 1-pair fixture would be refused — drop it for
        # this fixture (the floor's rollback is covered in the engine tests)
        monkeypatch.setattr("healing.MIN_HARVEST_PAIRS", 1, raising=False)
        monkeypatch.setattr(
            DocIdRegistry, "from_assets",
            classmethod(lambda cls, path: DocIdRegistry.from_pairs(
                {"CometModernHomeFeedQuery": "999"})))
        registry = session.registry
        assert registry.get("CometModernHomeFeedQuery") == "999"
        # the fresh registry was ADOPTED: subsequent accesses reuse it
        assert session.registry is registry

    def test_heal_failure_repropagates_the_load_error(
            self, tmp_path, monkeypatch):
        _corrupt_registry_env(tmp_path)

        def refuse(config, *, save, max_bundles):
            return None

        monkeypatch.setattr(FAKE_REFRESH, refuse, raising=False)
        session = make_session(tmp_path, monkeypatch)
        with pytest.raises(RegistryLoadError):
            _ = session.registry


class TestSurfaceDocIdHeal:
    """The surface-layer chokepoint heals registry misses."""

    def _service(self, tmp_path):
        config = SimpleNamespace(assets_dir=tmp_path / "data",
                                 journal_dir=tmp_path / "state")
        healer = HealingContext(config,
                                log_path=tmp_path / "state" / "healing.jsonl")
        session = SimpleNamespace(
            registry=DocIdRegistry.from_pairs({}),
            healer=healer, dry_run=False,
            registry_obj=None)
        session.adopt_registry = (
            lambda fresh: setattr(session, "registry", fresh))
        return Surface(session), session

    def test_miss_heals_and_resolves_fresh_id(self, tmp_path, monkeypatch):
        fresh_registry = DocIdRegistry.from_pairs(
            {"CometNotificationsBadgeCountQuery": "777"})

        def fake_refresh(config, *, save, max_bundles):
            return SimpleNamespace(added={}, changed={}, bundles_fetched=5,
                                   fetch_errors=0)

        monkeypatch.setattr(FAKE_REFRESH, fake_refresh, raising=False)
        monkeypatch.setattr(
            DocIdRegistry, "from_assets",
            classmethod(lambda cls, path: fresh_registry))
        monkeypatch.setattr("healing.MIN_HARVEST_PAIRS", 1, raising=False)
        service, session = self._service(tmp_path)
        assert service.doc_id("CometNotificationsBadgeCountQuery") == "777"
        # the session adopted the fresh registry
        assert session.registry.get(
            "CometNotificationsBadgeCountQuery") == "777"

    def test_dry_run_never_heals(self, tmp_path, monkeypatch):
        def must_not_run(config, *, save, max_bundles):
            raise AssertionError("dry-run must not harvest")

        monkeypatch.setattr(FAKE_REFRESH, must_not_run, raising=False)
        service, _session = self._service(tmp_path)
        service.session.dry_run = True
        with pytest.raises(RegistryMissError):
            service.doc_id("CometNotificationsBadgeCountQuery")

    def test_failed_heal_propagates_the_miss(self, tmp_path, monkeypatch):
        def refuse(config, *, save, max_bundles):
            return None

        monkeypatch.setattr(FAKE_REFRESH, refuse, raising=False)
        service, _session = self._service(tmp_path)
        with pytest.raises(RegistryMissError):
            service.doc_id("CometNotificationsBadgeCountQuery")


class TestGovernorCorruptStateEvent:
    """The governor's corrupt-state rebuild records its event."""

    def test_corrupt_state_resets_and_logs(self, tmp_path):
        log_path = tmp_path / "healing.jsonl"
        log = HealingLog(log_path)
        corrupt = tmp_path / "governor_state.json"
        corrupt.write_text("{torn", encoding="utf-8")
        gov = RequestGovernor(state_path=corrupt, healing_log=log)
        assert gov.state.day_count == 0  # fresh counters
        rows = [json.loads(line) for line in
                log_path.read_text(encoding="utf-8").splitlines()]
        assert rows[0]["kind"] == KIND_GOVERNOR_STATE_REBUILD
        assert "counters reset" in rows[0]["detail"]

    def test_healthy_state_logs_nothing(self, tmp_path):
        log_path = tmp_path / "healing.jsonl"
        log = HealingLog(log_path)
        healthy = tmp_path / "governor_state.json"
        healthy.write_text(json.dumps({"day_count": 5}), encoding="utf-8")
        RequestGovernor(state_path=healthy, healing_log=log)
        assert not log_path.exists()  # no event, no file

    def test_no_log_handle_stays_silent(self, tmp_path):
        corrupt = tmp_path / "governor_state.json"
        corrupt.write_text("{torn", encoding="utf-8")
        gov = RequestGovernor(state_path=corrupt)  # no healing log
        assert gov.state.day_count == 0
        assert not (tmp_path / "healing.jsonl").exists()
