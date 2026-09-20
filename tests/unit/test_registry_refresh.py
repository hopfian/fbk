"""Registry-refresh tests: harvest extraction, diff computation, v3 save.

Offline throughout: bundle texts below use the exact live registration
shapes calibrated in docs/15 §P2-1 (relayOperation __d frame + params
frame), so the harvest regexes are validated against real wire text.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import graphql.registry_refresh as rr
from config import Config
from graphql.registry import DocIdRegistry
from graphql.registry_refresh import (
    HarvestStats,
    RegistryRefreshError,
    harvest_pairs,
    refresh_registry,
)

U1 = "https://static.xx.fbcdn.net/rsrc.php/aa/b1.js"
U2 = "https://static.xx.fbcdn.net/rsrc.php/aa/b2.js"
U3 = "https://static.xx.fbcdn.net/rsrc.php/aa/b3.js"

# Real wire shapes (docs/15 §P2-1) — the regexes in registry_refresh.py
# must match these byte-for-byte semantics.
RELAY = ('__d("FooQuery_facebookRelayOperation",[],'
         '(function(t,n,r,o,a,i){a.exports="12345"}),null);')
PARAMS = 'params:{id:"67890",metadata:{},name:"BarQuery",operationKind:"query"'
PARAMS_MUT = ('params:{id:"777777",metadata:{},name:"SendThingMutation",'
              'operationKind:"mutation"')


class StubBundleTransport:
    """Offline transport: .get(url, headers=...) -> canned bundle text."""

    def __init__(self, texts: dict[str, str]):
        self.texts = dict(texts)
        self.cookies = {"c_user": "1"}
        self.fetched: list[str] = []

    def get(self, url: str, headers: dict | None = None, **_kw):
        self.fetched.append(url)
        if url not in self.texts:
            raise RuntimeError(f"no canned bundle for {url}")
        return SimpleNamespace(status_code=200, text=self.texts[url])


class TestHarvestPairs:
    """Pins bundle harvest against the exact live registration shapes
    (docs/15 §P2-1): both frames, collision bookkeeping, worker-order
    determinism, and fetch-error accounting."""

    def test_extracts_both_registration_shapes(self):
        t = StubBundleTransport({U1: RELAY, U2: "noise;" + PARAMS + ";noise"})
        pairs = harvest_pairs(t, [U1, U2], workers=2, delay_ceiling=0.0)
        assert pairs == {"FooQuery": "12345", "BarQuery": "67890"}

    def test_params_preferred_over_relay_and_collision_noted(self):
        both = RELAY.replace("FooQuery", "DupQuery").replace("12345", "11111") \
            + PARAMS.replace("BarQuery", "DupQuery").replace("67890", "22222")
        stats = HarvestStats()
        t = StubBundleTransport({U1: both})
        pairs = harvest_pairs(t, [U1], stats=stats)
        assert pairs == {"DupQuery": "22222"}  # params wins
        assert stats.collisions["DupQuery"] == ["22222", "11111"]
        assert stats.sources["DupQuery"] == {"params:query", "relayOperation"}

    def test_first_bundle_wins_regardless_of_thread_completion(self):
        t = StubBundleTransport({
            U1: PARAMS,                                        # BarQuery 67890
            U2: PARAMS.replace("67890", "99999"),              # BarQuery 99999
        })
        stats = HarvestStats()
        pairs = harvest_pairs(t, [U1, U2], workers=4, delay_ceiling=0.0,
                              stats=stats)
        assert pairs == {"BarQuery": "67890"}  # URL order, not completion order
        assert stats.collisions["BarQuery"] == ["67890", "99999"]
        assert stats.bundles_fetched == 2
        assert stats.fetch_errors == 0

    def test_fetch_errors_counted(self):
        t = StubBundleTransport({U1: RELAY})  # U2 raises inside .get
        stats = HarvestStats()
        pairs = harvest_pairs(t, [U1, U2], workers=1, stats=stats)
        assert pairs == {"FooQuery": "12345"}
        assert stats.bundles_fetched == 1
        assert stats.fetch_errors == 1

    def test_max_bundles_caps_the_fetch_list(self):
        t = StubBundleTransport({U1: RELAY, U2: PARAMS, U3: PARAMS_MUT})
        pairs = harvest_pairs(t, [U1, U2, U3], max_bundles=2,
                              delay_ceiling=0.0)
        assert pairs == {"FooQuery": "12345", "BarQuery": "67890"}
        assert set(t.fetched) == {U1, U2}

    def test_accepts_plain_fetch_callable(self):
        texts = {U1: RELAY, U2: PARAMS}
        pairs = harvest_pairs(lambda url: texts[url], [U1, U2],
                              delay_ceiling=0.0)
        assert pairs == {"FooQuery": "12345", "BarQuery": "67890"}

    def test_operation_kind_lands_in_source_tags(self):
        stats = HarvestStats()
        harvest_pairs(StubBundleTransport({U1: PARAMS_MUT}), [U1], stats=stats)
        assert stats.sources["SendThingMutation"] == {"params:mutation"}

    def test_unusable_transport_raises(self):
        with pytest.raises(RegistryRefreshError, match="transport must be"):
            harvest_pairs(object(), [U1])


FRESH = {"Keep": "1", "Move": "9", "New": "7"}
OLD = {"Keep": "1", "Move": "2", "Gone": "3"}


def _fake_harvest(transport, urls, *, workers=6, delay_ceiling=0.2,
                  max_bundles=None, stats=None, governor=None):
    if stats is not None:
        stats.bundles_fetched = len(urls)
        stats.fetch_errors = 0
    return dict(FRESH)


def _patch_refresh(monkeypatch, *, revision="1002003",
                   bundles: tuple[str, ...] = (U1, U2)) -> None:
    boot = SimpleNamespace(bundle_urls=list(bundles), revision=revision)
    monkeypatch.setattr(rr, "bootstrap_homepage",
                        lambda transport, cookies: boot)
    monkeypatch.setattr(rr, "harvest_pairs", _fake_harvest)


def _write_v2(root: Path, pairs: dict[str, str]) -> bytes:
    """A baseline registry file in the established v2 schema."""
    assets = root / "data"
    assets.mkdir(parents=True, exist_ok=True)
    payload = {
        "revision": "1001001",
        "harvested_at": "2026-09-01T00:00:00",
        "sources": ["scripts/02"],
        "unique_pairs": [{"friendly_name": n, "doc_id": d,
                          "sources": ["params:query"]}
                         for n, d in sorted(pairs.items())],
        "stats": {"unique_pairs": len(pairs)},
    }
    path = assets / "doc_id_registry_v2.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path.read_bytes()


class TestRefreshRegistry:
    """Pins the refresh pipeline: diff against the assets baseline, and
    the v3 save with MERGE/carry semantics (docs/15 §P5-2) that never
    touches the v2 file."""

    def test_diff_computed_against_assets_registry(self, tmp_path, monkeypatch):
        _patch_refresh(monkeypatch)
        _write_v2(tmp_path, OLD)
        cfg = Config.discover(tmp_path)
        diff = refresh_registry(cfg, transport=SimpleNamespace(cookies={"c_user": "1"}))
        assert diff.added == {"New": "7"}
        assert diff.changed == {"Move": ("2", "9")}
        assert diff.removed == ["Gone"]
        assert diff.harvest_revision == "1002003"
        assert diff.bundles_fetched == 2
        assert diff.fetch_errors == 0

    def test_diff_against_inline_baseline(self, tmp_path, monkeypatch):
        _patch_refresh(monkeypatch)
        cfg = Config.discover(tmp_path)
        diff = refresh_registry(cfg, baseline=DocIdRegistry.from_pairs(OLD),
                               transport=SimpleNamespace(cookies={"c_user": "1"}))
        assert diff.added == {"New": "7"}
        assert diff.changed == {"Move": ("2", "9")}
        assert diff.removed == ["Gone"]

    def test_missing_baseline_means_everything_added(self, tmp_path, monkeypatch):
        _patch_refresh(monkeypatch)
        cfg = Config.discover(tmp_path)
        diff = refresh_registry(cfg, transport=SimpleNamespace(cookies={"c_user": "1"}))
        assert diff.added == FRESH
        assert diff.removed == []
        assert diff.changed == {}

    def test_no_bundles_in_bootstrap_raises(self, tmp_path, monkeypatch):
        _patch_refresh(monkeypatch, bundles=())
        cfg = Config.discover(tmp_path)
        with pytest.raises(RegistryRefreshError, match=r"no rsrc\.php bundle"):
            refresh_registry(cfg, transport=SimpleNamespace(cookies={"c_user": "1"}))

    def test_save_writes_v3_and_never_touches_v2(self, tmp_path, monkeypatch, capsys):
        _patch_refresh(monkeypatch)
        v2_bytes = _write_v2(tmp_path, OLD)
        cfg = Config.discover(tmp_path)
        diff = refresh_registry(cfg, save=True,
                                transport=SimpleNamespace(cookies={"c_user": "1"}))

        v3 = tmp_path / "data" / "doc_id_registry_v3.json"
        assert v3.is_file()
        data = json.loads(v3.read_text(encoding="utf-8"))
        assert data["revision"] == "1002003"
        assert data["sources"] == ["refresh", "carried"]
        assert data["harvested_at"]
        # MERGE semantics (docs/15 §P5-2): fresh wins for harvested names;
        # baseline names absent from the homepage deploy are CARRIED, never dropped.
        merged = {p["friendly_name"]: p["doc_id"] for p in data["unique_pairs"]}
        expected = dict(OLD)
        expected.update(FRESH)
        assert merged == expected
        carried = sorted(n for n in OLD if n not in FRESH)
        assert all(p["sources"] for p in data["unique_pairs"])
        carried_rows = {p["friendly_name"] for p in data["unique_pairs"]
                        if p["sources"] == ["carried"]}
        assert carried_rows == set(carried)
        assert data["stats"]["unique_pairs"] == len(expected)
        assert data["stats"]["carried_pairs"] == len(carried)
        assert data["stats"]["fresh_pairs"] == len(FRESH)
        assert data["stats"]["added"] == len(diff.added)
        assert data["stats"]["bundles_fetched"] == 2
        assert f"registry v3 -> {v3}" in capsys.readouterr().out

        # v2 untouched byte-for-byte, and discovery now prefers v3
        assert (tmp_path / "data" / "doc_id_registry_v2.json").read_bytes() == v2_bytes
        assert DocIdRegistry.from_assets(tmp_path / "data").source \
            == "doc_id_registry_v3.json"
        assert DocIdRegistry.from_assets(tmp_path / "data").doc_id("New") == "7"


class TestCommandHumanOutput:
    """`registry refresh` human output: counts + at most 5 example names per
    category (live symptom: pages of 'removed' names). --json keeps the full
    diff payload byte-complete."""

    DIFF = SimpleNamespace(
        added={f"New{i}": str(i) for i in range(8)},
        changed={f"Moved{i}": (str(i), f"1{i}") for i in range(8)},
        removed=[f"Gone{i}" for i in range(200)],
        harvest_revision="1002003",
        bundles_fetched=3,
        fetch_errors=0,
    )

    def _run(self, tmp_path, monkeypatch, *, as_json: bool) -> int:
        from commands import registry as reg_cmd
        monkeypatch.setattr(rr, "refresh_registry", lambda cfg, **kw: self.DIFF)
        args = SimpleNamespace(root=str(tmp_path), cookies=None, save=False,
                               workers=6, max_bundles=None, as_json=as_json)
        return reg_cmd.cmd_registry_refresh(args)

    def test_human_output_is_counts_plus_five_examples(self, tmp_path, monkeypatch,
                                                       capsys):
        assert self._run(tmp_path, monkeypatch, as_json=False) == 0
        out = capsys.readouterr().out
        assert "added 8, changed 8, removed 200" in out
        assert "New4" in out and "Gone4" in out        # first 5 examples shown
        assert "New5" not in out and "Gone5" not in out  # the rest trimmed
        assert "Gone199" not in out                     # never pages the diff

    def test_json_output_keeps_full_payload(self, tmp_path, monkeypatch, capsys):
        assert self._run(tmp_path, monkeypatch, as_json=True) == 0
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["added"]) == 8
        assert len(payload["changed"]) == 8
        assert len(payload["removed"]) == 200
        assert payload["counts"] == {"added": 8, "changed": 8, "removed": 200}
