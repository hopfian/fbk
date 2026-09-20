"""Unit tests for the session-input self-healing paths (round three).

All tests execute offline — no network. The homepage bootstrap is
monkeypatched at its session-module import site so no page fetch runs.
Covers: the cookie jar's tolerant reparse (one torn row healed, a
hopeless jar still typed-error), the token-cache identity-mismatch
discard (a swapped jar never burns a doomed request), the degenerate
bootstrap retry (UNKNOWN state / logged-in-without-token page), the
adopt_registry push reaching the live GraphQL client, and the uniform
v3 backup-on-write in the manual refresh path.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

import session as session_module
from auth.bootstrap import Bootstrap
from auth.state import LoginState
from config import Config
from graphql.registry import DocIdRegistry
from healing import (
    KIND_BOOTSTRAP_RETRY,
    KIND_COOKIE_JAR_HEAL,
    KIND_TOKEN_CACHE_REBUILD,
)
from session import Session, _bootstrap_degenerate
from transport.cookies import CookieLoadError, load_netscape

FAKE_REFRESH = "graphql.registry_refresh.refresh_registry"

GOOD_JAR = (
    "# Netscape HTTP Cookie File\n"
    ".facebook.com\tTRUE\t/\tTRUE\t2147483647\tc_user\t12345678901234\n"
    ".facebook.com\tTRUE\t/\tTRUE\t2147483647\txs\tTEST-XS\n"
    ".facebook.com\tTRUE\t/\tTRUE\t2147483647\tdatr\tTEST-DATR\n"
)


@pytest.fixture(autouse=True)
def heal_env(monkeypatch):
    """Pin the default healing configuration for every test."""
    for name in ("FBK_HEAL", "FBK_HEAL_REGISTRY_HOURS",
                 "FBK_HEAL_MAX_BUNDLES", "FBK_HEAL_TRANSPORT_RETRIES",
                 "FBK_HEAL_REALTIME_RECONNECTS"):
        monkeypatch.delenv(name, raising=False)


def make_bootstrap(*, dtsg: str | None = "NAfTEST:1:1789723298",
                   state: LoginState = LoginState.LOGGED_IN,
                   user_id: str = "12345678901234") -> Bootstrap:
    return Bootstrap(state=state,
                     fb_dtsg=SecretStr(dtsg) if dtsg else None,
                     lsd=SecretStr("TESTLSD"), user_id=user_id)


class TestCookieJarHeal:
    """One torn row must not kill the session when the auth rows survive."""

    def test_torn_jar_is_healed(self, tmp_path):
        jar = tmp_path / "cookies.txt"
        # the middle row is mangled (6 fields, not 7) — the strict
        # MozillaCookieJar load dies; the tolerant reparse drops it
        jar.write_text(GOOD_JAR.replace(
            ".facebook.com\tTRUE\t/\tTRUE\t2147483647\tdatr\tTEST-DATR\n",
            "garbage line not a cookie\n"), encoding="utf-8")
        events: list[str] = []
        cookies = load_netscape(jar, on_heal=events.append)
        assert cookies["c_user"] == "12345678901234"
        assert cookies["xs"] == "TEST-XS"
        assert len(events) == 1
        assert "dropped 1 row(s)" in events[0]

    def test_heal_event_reaches_the_healing_log(self, tmp_path, monkeypatch):
        jar = tmp_path / "cookies.txt"
        jar.write_text(GOOD_JAR.replace(
            ".facebook.com\tTRUE\t/\tTRUE\t2147483647\tdatr\tTEST-DATR\n",
            "garbage line not a cookie\n"), encoding="utf-8")
        monkeypatch.setattr(session_module, "FBTransport",
                            _AnyTransport, raising=False)
        cfg = Config.discover().model_copy(update={
            "cookies_path": jar,
            "state_dir": tmp_path / "state",
            "data_dir": tmp_path / "data",
        })
        Session(cfg, journal_name=None)
        rows = [json.loads(line) for line in
                (tmp_path / "state" / "healing.jsonl").read_text(
                    encoding="utf-8").splitlines()]
        assert rows[0]["kind"] == KIND_COOKIE_JAR_HEAL
        assert "dropped 1 row(s)" in rows[0]["detail"]

    def test_hopeless_jar_still_raises_typed(self, tmp_path):
        jar = tmp_path / "cookies.txt"
        jar.write_text("total garbage\nno rows at all\n", encoding="utf-8")
        with pytest.raises(CookieLoadError):
            load_netscape(jar)

    def test_clean_jar_never_heals(self, tmp_path):
        jar = tmp_path / "cookies.txt"
        jar.write_text(GOOD_JAR, encoding="utf-8")
        events: list[str] = []
        cookies = load_netscape(jar, on_heal=events.append)
        assert cookies["c_user"] == "12345678901234"
        assert events == []


class _AnyTransport:
    """Session constructor stub: accepts every kwarg, does nothing."""

    def __init__(self, **kwargs):
        self.healing_log = None

    def close(self) -> None:
        pass


def make_session(tmp_path, monkeypatch, *, user_id: str = "12345678901234"
                 ) -> Session:
    """A real Session with the network seam monkeypatched away."""
    jar = tmp_path / "cookies.txt"
    jar.write_text(GOOD_JAR.replace("12345678901234", user_id),
                   encoding="utf-8")
    monkeypatch.setattr(session_module, "FBTransport", _AnyTransport,
                        raising=False)
    cfg = Config.discover().model_copy(update={
        "cookies_path": jar,
        "state_dir": tmp_path / "state",
        "data_dir": tmp_path / "data",
    })
    return Session(cfg, journal_name=None)


class TestTokenCacheIdentityHeal:
    """A swapped jar never burns a doomed request on stale tokens."""

    def _seed_cache(self, session: Session, user_id: str) -> None:
        session.token_cache.save(make_bootstrap(user_id=user_id))

    def test_identity_mismatch_discards_cache(self, tmp_path, monkeypatch):
        session = make_session(tmp_path, monkeypatch)
        self._seed_cache(session, user_id="99999999999999")
        calls: list[str] = []

        def fake_homepage(transport, cookies):
            calls.append("boot")
            return make_bootstrap()

        monkeypatch.setattr(session_module, "bootstrap_homepage",
                            fake_homepage)
        session.bootstrap()
        assert calls == ["boot"]  # the cache was NOT trusted
        rows = [json.loads(line) for line in
                (tmp_path / "state" / "healing.jsonl").read_text(
                    encoding="utf-8").splitlines()]
        assert rows[0]["kind"] == KIND_TOKEN_CACHE_REBUILD
        assert "identity mismatch" in rows[0]["trigger"]

    def test_matching_identity_still_hits_the_cache(self, tmp_path,
                                                    monkeypatch):
        session = make_session(tmp_path, monkeypatch)
        self._seed_cache(session, user_id="12345678901234")
        calls: list[str] = []

        def fake_homepage(transport, cookies):
            calls.append("boot")
            return make_bootstrap()

        monkeypatch.setattr(session_module, "bootstrap_homepage",
                            fake_homepage)
        session.bootstrap()
        assert calls == []  # zero-request cache hit preserved


class TestDegenerateBootstrapRetry:
    """A poisoned homepage gets exactly one governed retry."""

    def test_unknown_state_retries_and_recovers(self, tmp_path, monkeypatch):
        session = make_session(tmp_path, monkeypatch)
        pages = [make_bootstrap(state=LoginState.UNKNOWN, dtsg=None),
                 make_bootstrap()]

        def fake_homepage(transport, cookies):
            return pages.pop(0)

        monkeypatch.setattr(session_module, "bootstrap_homepage",
                            fake_homepage)
        boot = session.bootstrap()
        assert boot.fb_dtsg is not None  # the healthy retry won
        rows = [json.loads(line) for line in
                (tmp_path / "state" / "healing.jsonl").read_text(
                    encoding="utf-8").splitlines()]
        assert rows[0]["kind"] == KIND_BOOTSTRAP_RETRY

    def test_logged_in_without_dtsg_retries(self, tmp_path, monkeypatch):
        session = make_session(tmp_path, monkeypatch)
        pages = [make_bootstrap(dtsg=None), make_bootstrap()]

        def fake_homepage(transport, cookies):
            return pages.pop(0)

        monkeypatch.setattr(session_module, "bootstrap_homepage",
                            fake_homepage)
        boot = session.bootstrap()
        assert boot.fb_dtsg is not None

    def test_persistent_degenerate_propagates_after_one_retry(
            self, tmp_path, monkeypatch):
        session = make_session(tmp_path, monkeypatch)
        pages = [make_bootstrap(state=LoginState.UNKNOWN, dtsg=None),
                 make_bootstrap(state=LoginState.UNKNOWN, dtsg=None)]
        calls: list[Bootstrap] = []

        def fake_homepage(transport, cookies):
            boot = pages.pop(0)
            calls.append(boot)
            return boot

        monkeypatch.setattr(session_module, "bootstrap_homepage",
                            fake_homepage)
        boot = session.bootstrap()
        assert boot.state == LoginState.UNKNOWN  # the honest outcome
        assert len(calls) == 2  # exactly one retry, never a loop
        rows = [json.loads(line) for line in
                (tmp_path / "state" / "healing.jsonl").read_text(
                    encoding="utf-8").splitlines()]
        assert len(rows) == 1  # one retry event, no recovery event

    def test_healthy_page_never_retries(self, tmp_path, monkeypatch):
        session = make_session(tmp_path, monkeypatch)
        calls: list[Bootstrap] = []

        def fake_homepage(transport, cookies):
            boot = make_bootstrap()
            calls.append(boot)
            return boot

        monkeypatch.setattr(session_module, "bootstrap_homepage",
                            fake_homepage)
        session.bootstrap()
        assert len(calls) == 1
        assert not (tmp_path / "state" / "healing.jsonl").exists()

    def test_checkpoint_state_is_never_retried(self, tmp_path, monkeypatch):
        session = make_session(tmp_path, monkeypatch)
        calls: list[Bootstrap] = []

        def fake_homepage(transport, cookies):
            boot = make_bootstrap(state=LoginState.CHECKPOINT, dtsg=None)
            calls.append(boot)
            return boot

        monkeypatch.setattr(session_module, "bootstrap_homepage",
                            fake_homepage)
        session.bootstrap()
        assert len(calls) == 1  # enforcement is met with disengagement

    def test_degenerate_classifier(self):
        assert _bootstrap_degenerate(
            make_bootstrap(state=LoginState.UNKNOWN, dtsg=None)) is True
        assert _bootstrap_degenerate(make_bootstrap(dtsg=None)) is True
        assert _bootstrap_degenerate(make_bootstrap()) is False
        assert _bootstrap_degenerate(
            make_bootstrap(state=LoginState.CHECKPOINT, dtsg=None)) is False


class TestAdoptRegistryReachesClient:
    """The adopt hook updates the LIVE client, not just the session cache."""

    def test_adopt_pushes_to_the_graphql_client(self, tmp_path, monkeypatch):
        session = make_session(tmp_path, monkeypatch)
        fake_client = SimpleNamespace(registry=DocIdRegistry.from_pairs({}))
        session._graphql = fake_client
        fresh = DocIdRegistry.from_pairs({"Op": "999"})
        session.adopt_registry(fresh)
        assert session.registry is fresh
        assert fake_client.registry is fresh


class TestWriteV3Backup:
    """The manual refresh path carries the same rollback primitive."""

    def test_write_v3_backs_up_the_previous_registry(self, tmp_path):
        from graphql.registry_refresh import _V3_PREV_NAME, _write_v3
        cfg = Config.discover().model_copy(update={
            "data_dir": tmp_path / "data",
            "state_dir": tmp_path / "state",
        })
        (tmp_path / "data").mkdir(parents=True)
        v3 = tmp_path / "data" / "doc_id_registry_v3.json"
        v3.write_text(json.dumps({"revision": "rev-old",
                                  "unique_pairs": []}), encoding="utf-8")
        diff = SimpleNamespace(harvest_revision="rev-new", added={},
                               changed={}, removed=[],
                               removed_from_homepage_deploy=[],
                               fetch_errors=0, bundles_fetched=5)
        stats = SimpleNamespace(sources={}, collisions=[], bundles_fetched=5, fetch_errors=0, mutations=1)
        _write_v3(cfg, {"Op": "999"}, diff, stats, baseline_old={})
        prev = tmp_path / "data" / _V3_PREV_NAME
        assert prev.is_file()
        assert json.loads(prev.read_text(encoding="utf-8"))["revision"] \
            == "rev-old"
        assert json.loads(v3.read_text(encoding="utf-8"))["revision"] \
            == "rev-new"

    def test_first_write_writes_no_backup(self, tmp_path):
        from graphql.registry_refresh import _V3_PREV_NAME, _write_v3
        cfg = Config.discover().model_copy(update={
            "data_dir": tmp_path / "data",
            "state_dir": tmp_path / "state",
        })
        (tmp_path / "data").mkdir(parents=True)
        diff = SimpleNamespace(harvest_revision="rev-new", added={},
                               changed={}, removed=[],
                               removed_from_homepage_deploy=[],
                               fetch_errors=0, bundles_fetched=5)
        stats = SimpleNamespace(sources={}, collisions=[], bundles_fetched=5, fetch_errors=0, mutations=1)
        _write_v3(cfg, {"Op": "999"}, diff, stats, baseline_old={})
        assert not (tmp_path / "data" / _V3_PREV_NAME).exists()
