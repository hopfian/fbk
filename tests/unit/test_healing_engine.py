"""Unit tests for the self-healing engine (src/healing.py).

All tests execute offline — no network, no cookies, no governor. A fake
config (SimpleNamespace with tmp-path directories) satisfies the
HealingContext surface; the expensive registry re-harvest is monkeypatched
at its lazy import site (graphql.registry_refresh.refresh_registry), so
no bundle download ever runs. Covers: the HealingLog JSONL contract
(append/read/cooldown windows/fail-soft corrupt rows), the termination
guarantees (per-kind invocation caps, master switch, closed vocabulary),
and the registry-refresh heal's success/failure/record semantics.

Fixtures:
    heal_env: clears every FBK_HEAL_* variable so tests pin defaults.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from healing import (
    DEFAULT_MAX_BUNDLES,
    DEFAULT_REGISTRY_HOURS,
    KIND_DOC_ID_RETRY,
    KIND_REGISTRY_REFRESH,
    KIND_TOKEN_CACHE_REBUILD,
    HealingContext,
    HealingLog,
    healing_enabled,
    transport_retry_limit,
)

FAKE_REGISTRY_REFRESH = "graphql.registry_refresh.refresh_registry"


@pytest.fixture(autouse=True)
def heal_env(monkeypatch):
    """Pin the default healing configuration for every test."""
    for name in ("FBK_HEAL", "FBK_HEAL_REGISTRY_HOURS",
                 "FBK_HEAL_MAX_BUNDLES", "FBK_HEAL_TRANSPORT_RETRIES"):
        monkeypatch.delenv(name, raising=False)


def _ctx(tmp_path, **kw) -> HealingContext:
    """A HealingContext over a fake config rooted at tmp_path."""
    config = SimpleNamespace(assets_dir=tmp_path / "data",
                             journal_dir=tmp_path / "state")
    return HealingContext(config,
                          log_path=tmp_path / "state" / "healing.jsonl", **kw)


class TestHealingEnabled:
    """The FBK_HEAL master switch."""

    def test_default_enabled(self):
        assert healing_enabled() is True

    @pytest.mark.parametrize("value", ["off", "0", "false", "OFF"])
    def test_explicit_off(self, monkeypatch, value):
        monkeypatch.setenv("FBK_HEAL", value)
        assert healing_enabled() is False

    def test_transport_retry_limit_off_when_disabled(self, monkeypatch):
        monkeypatch.setenv("FBK_HEAL", "off")
        assert transport_retry_limit() == 0

    def test_transport_retry_limit_default(self):
        assert transport_retry_limit() == 1


class TestHealingLog:
    """The redacted JSONL healing log."""

    def test_append_writes_row_and_returns_it(self, tmp_path, capsys):
        log = HealingLog(tmp_path / "state" / "healing.jsonl")
        row = log.append(KIND_REGISTRY_REFRESH, "doc_id rejected",
                         "added=3", ts=1000.0)
        assert row == {"ts": 1000.0, "kind": KIND_REGISTRY_REFRESH,
                       "trigger": "doc_id rejected", "detail": "added=3"}
        lines = (tmp_path / "state" / "healing.jsonl").read_text(
            encoding="utf-8").splitlines()
        assert json.loads(lines[0]) == row
        # every event mirrors to stderr for real-time visibility
        assert "[heal] registry-refresh: doc_id rejected — added=3" \
            in capsys.readouterr().err

    def test_last_ts_per_kind(self, tmp_path):
        log = HealingLog(tmp_path / "healing.jsonl")
        log.append(KIND_REGISTRY_REFRESH, "a", ts=10.0)
        log.append(KIND_DOC_ID_RETRY, "b", ts=20.0)
        log.append(KIND_REGISTRY_REFRESH, "c", ts=30.0)
        assert log.last_ts(KIND_REGISTRY_REFRESH) == 30.0
        assert log.last_ts(KIND_DOC_ID_RETRY) == 20.0
        assert log.last_ts("nope") is None

    def test_count_since_window_and_kind(self, tmp_path):
        log = HealingLog(tmp_path / "healing.jsonl")
        log.append(KIND_REGISTRY_REFRESH, "old", ts=0.0)
        log.append(KIND_DOC_ID_RETRY, "new", ts=900.0)
        # a 500s window at t=1000 covers only the ts=900 row (the ts=0
        # row sits 1000s back — outside)
        assert log.count_since(500.0, now=1000.0) == 1
        assert log.count_since(500.0, kind=KIND_DOC_ID_RETRY, now=1000.0) == 1
        assert log.count_since(500.0, kind=KIND_REGISTRY_REFRESH,
                               now=1000.0) == 0
        # a fully inclusive window counts both
        assert log.count_since(1000.0, now=1000.0) == 2

    def test_corrupt_rows_are_skipped_fail_soft(self, tmp_path):
        path = tmp_path / "healing.jsonl"
        path.write_text('{"ts": 5.0, "kind": "registry-refresh", '
                        '"trigger": "ok", "detail": ""}\n{torn\n',
                        encoding="utf-8")
        log = HealingLog(path)
        assert log.last_ts(KIND_REGISTRY_REFRESH) == 5.0
        assert log.count_since(10.0, now=10.0) == 1

    def test_absent_log_reads_as_empty(self, tmp_path):
        log = HealingLog(tmp_path / "nope.jsonl")
        assert log.last_ts(KIND_REGISTRY_REFRESH) is None
        assert log.count_since(1000.0) == 0

    def test_log_self_prunes_when_oversized(self, tmp_path, monkeypatch):
        # the recursive layer: the heal log heals its own growth — once
        # the file passes PRUNE_BYTES, the next append keeps only the
        # newest KEEP_ROWS rows
        log = HealingLog(tmp_path / "healing.jsonl")
        log.PRUNE_BYTES = 1          # force the prune on every append
        log.KEEP_ROWS = 50
        for i in range(120):
            log.append(KIND_DOC_ID_RETRY, f"event-{i}", ts=float(i))
        rows = log._rows()
        assert len(rows) == 50
        # the NEWEST rows survived (cooldown windows stay correct)
        assert rows[-1]["trigger"] == "event-119"
        assert rows[0]["trigger"] == "event-70"

    def test_prune_failure_leaves_log_untouched(self, tmp_path, monkeypatch):
        log = HealingLog(tmp_path / "healing.jsonl")
        log.append(KIND_DOC_ID_RETRY, "seed", ts=1.0)
        log.PRUNE_BYTES = 1
        monkeypatch.setattr("healing.os.replace",
                            lambda *a, **kw: (_ for _ in ()).throw(
                                OSError("locked")))
        log.append(KIND_DOC_ID_RETRY, "second", ts=2.0)
        # the prune failed silently; both rows still readable (explicit
        # clock: the seeded ts values sit near epoch zero)
        assert log.count_since(10**9, now=3.0) == 2


class TestHealingContextGuards:
    """Termination guarantees: caps, cooldown, master switch, vocabulary."""

    def test_unknown_kind_is_denied_and_record_raises(self, tmp_path):
        ctx = _ctx(tmp_path)
        assert ctx.allow("made-up-kind") is False
        with pytest.raises(ValueError, match="unknown healing kind"):
            ctx.record("made-up-kind", "trigger")

    def test_invocation_cap_spent_denies(self, tmp_path):
        ctx = _ctx(tmp_path)
        assert ctx.allow(KIND_DOC_ID_RETRY) is True
        ctx.record(KIND_DOC_ID_RETRY, "first")
        assert ctx.allow(KIND_DOC_ID_RETRY) is False

    def test_master_switch_off_denies_everything(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FBK_HEAL", "off")
        ctx = _ctx(tmp_path)
        assert ctx.allow(KIND_REGISTRY_REFRESH) is False
        assert ctx.allow(KIND_TOKEN_CACHE_REBUILD) is False

    def test_registry_cooldown_blocks_inside_window(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.log.append(KIND_REGISTRY_REFRESH, "recent",
                       ts=time.time() - 60.0)
        assert ctx.allow(KIND_REGISTRY_REFRESH) is False

    def test_registry_cooldown_allows_after_window(self, tmp_path,
                                                   monkeypatch):
        monkeypatch.setenv("FBK_HEAL_REGISTRY_HOURS", "6")
        ctx = _ctx(tmp_path)
        ctx.log.append(KIND_REGISTRY_REFRESH, "old",
                       ts=time.time() - (DEFAULT_REGISTRY_HOURS * 3600) - 10)
        assert ctx.allow(KIND_REGISTRY_REFRESH) is True

    def test_cooldown_window_is_env_overridable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FBK_HEAL_REGISTRY_HOURS", "0.001")
        ctx = _ctx(tmp_path)
        ctx.log.append(KIND_REGISTRY_REFRESH, "old", ts=time.time() - 5.0)
        # 0.001h = 3.6s window: a 5s-old event is outside it
        assert ctx.allow(KIND_REGISTRY_REFRESH) is True


def _write_registry(path, names: list[str], revision: str = "rev-1") -> None:
    """Write one real-schema registry file (unique_pairs) for verification."""
    doc = {"revision": revision,
           "unique_pairs": [{"friendly_name": n, "doc_id": f"100{i}"}
                            for i, n in enumerate(names)]}
    path.write_text(json.dumps(doc), encoding="utf-8")


class TestRegistryRefreshHeal:
    """The expensive heal: governed re-harvest + verification + rollback."""

    def _patch(self, monkeypatch, behavior):
        monkeypatch.setattr(FAKE_REGISTRY_REFRESH, behavior, raising=False)

    def test_success_returns_reloaded_registry(self, tmp_path, monkeypatch):
        monkeypatch.setattr("healing.MIN_HARVEST_PAIRS", 1, raising=False)

        def fake_refresh(config, *, save, max_bundles):
            assert save is True
            assert max_bundles == DEFAULT_MAX_BUNDLES
            return SimpleNamespace(added={"Op": "999"}, changed={},
                                   bundles_fetched=12, fetch_errors=0)

        self._patch(monkeypatch, fake_refresh)
        import graphql.registry as reg_module
        reloaded = reg_module.DocIdRegistry.from_pairs({"Op": "999"})
        monkeypatch.setattr(
            reg_module.DocIdRegistry, "from_assets",
            classmethod(lambda cls, path: reloaded))
        ctx = _ctx(tmp_path)
        result = ctx.refresh_registry()
        assert result is reloaded
        rows = (tmp_path / "state" / "healing.jsonl").read_text(
            encoding="utf-8").splitlines()
        row = json.loads(rows[0])
        assert row["kind"] == KIND_REGISTRY_REFRESH
        assert "verified" in row["trigger"]

    def test_degenerate_harvest_rolls_back_to_backup(self, tmp_path,
                                                     monkeypatch):
        # a healthy v3 exists; the "harvest" overwrites it with a
        # degenerate 5-pair file; verification refuses and restores the
        # backup bit-for-bit
        assets = tmp_path / "data"
        assets.mkdir(parents=True)
        good = assets / "doc_id_registry_v3.json"
        _write_registry(good, [f"Op{i}" for i in range(300)])
        _write_registry(good.with_name("doc_id_registry_v2.json"),
                        [f"Op{i}" for i in range(300)])

        def bad_refresh(config, *, save, max_bundles):
            _write_registry(good, [f"Junk{i}" for i in range(5)],
                            revision="rev-bad")
            return SimpleNamespace(added={}, changed={}, bundles_fetched=3,
                                   fetch_errors=0)

        self._patch(monkeypatch, bad_refresh)
        ctx = _ctx(tmp_path)
        assert ctx.refresh_registry() is None
        assert good.is_file()
        assert "rev-1" in good.read_text(encoding="utf-8")  # backup restored
        row = json.loads((tmp_path / "state" / "healing.jsonl").read_text(
            encoding="utf-8").splitlines()[0])
        assert "degenerate" in row["trigger"]
        assert "5 pairs" in row["detail"]

    def test_degenerate_first_harvest_drops_new_v3(self, tmp_path,
                                                   monkeypatch):
        # no previous v3: the rollback DROPS the fresh file so the descent
        # falls back to v2 exactly as before the heal
        assets = tmp_path / "data"
        assets.mkdir(parents=True)
        _write_registry(assets / "doc_id_registry_v2.json",
                        [f"Op{i}" for i in range(300)])
        fresh_v3 = assets / "doc_id_registry_v3.json"

        def bad_refresh(config, *, save, max_bundles):
            _write_registry(fresh_v3, ["Junk"], revision="rev-bad")
            return SimpleNamespace(added={}, changed={}, bundles_fetched=3,
                                   fetch_errors=0)

        self._patch(monkeypatch, bad_refresh)
        ctx = _ctx(tmp_path)
        assert ctx.refresh_registry() is None
        assert not fresh_v3.exists()  # dropped; v2 descent is intact

    def test_unparseable_new_v3_rolls_back(self, tmp_path, monkeypatch):
        assets = tmp_path / "data"
        assets.mkdir(parents=True)
        good = assets / "doc_id_registry_v3.json"
        _write_registry(good, [f"Op{i}" for i in range(300)])

        def bad_refresh(config, *, save, max_bundles):
            good.write_text("{torn", encoding="utf-8")
            return SimpleNamespace(added={}, changed={}, bundles_fetched=3,
                                   fetch_errors=0)

        self._patch(monkeypatch, bad_refresh)
        ctx = _ctx(tmp_path)
        assert ctx.refresh_registry() is None
        assert "rev-1" in good.read_text(encoding="utf-8")

    def test_failure_records_event_and_returns_none(self, tmp_path,
                                                    monkeypatch):
        def boom(config, *, save, max_bundles):
            raise RuntimeError("network down")

        self._patch(monkeypatch, boom)
        ctx = _ctx(tmp_path)
        assert ctx.refresh_registry() is None
        row = json.loads((tmp_path / "state" / "healing.jsonl")
                         .read_text(encoding="utf-8").splitlines()[0])
        assert row["kind"] == KIND_REGISTRY_REFRESH
        assert row["trigger"] == "re-harvest failed"
        assert "RuntimeError" in row["detail"]

    def test_cap_spent_skips_the_harvest(self, tmp_path, monkeypatch):
        def must_not_run(config, *, save, max_bundles):
            raise AssertionError("harvest must not run after the cap")

        self._patch(monkeypatch, must_not_run)
        ctx = _ctx(tmp_path)
        ctx._attempts[KIND_REGISTRY_REFRESH] = 1
        assert ctx.refresh_registry() is None

    def test_cooldown_skips_the_harvest(self, tmp_path, monkeypatch):
        def must_not_run(config, *, save, max_bundles):
            raise AssertionError("harvest must not run inside cooldown")

        self._patch(monkeypatch, must_not_run)
        ctx = _ctx(tmp_path)
        ctx.log.append(KIND_REGISTRY_REFRESH, "recent", ts=time.time())
        assert ctx.refresh_registry() is None


class TestTokenCacheRebuildEvent:
    """The discard-is-the-heal cache path records its event."""

    def test_records_within_cap(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.record_token_cache_rebuild("token_cache.json corrupt")
        ctx.record_token_cache_rebuild("token_cache.json corrupt again")
        assert ctx.log.count_since(10**9, kind=KIND_TOKEN_CACHE_REBUILD) == 2

    def test_cap_two_then_denied(self, tmp_path):
        ctx = _ctx(tmp_path)
        for _ in range(2):
            ctx.record_token_cache_rebuild("corrupt")
        # third attempt: cap spent — no row, no crash
        ctx.record_token_cache_rebuild("corrupt once more")
        assert ctx.log.count_since(10**9, kind=KIND_TOKEN_CACHE_REBUILD) == 2
