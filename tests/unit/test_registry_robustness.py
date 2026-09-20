"""graphql.registry robustness gaps (wave 2 polish targets).

test_registry.py validates the real harvested registry pairs; what was
missing is what happens when the registry FILE itself is wrong:

  * an assets dir with no registry file -> the TYPED RegistryMissError
    with re-harvest guidance (already the from_assets contract);
  * malformed / corrupt / wrong-shape registry JSON -> per-file fail-soft
    descent: the corrupt file is skipped with a stderr warning and the
    next healthy priority candidate wins; only when NOTHING loads does
    the TYPED RegistryLoadError name the corrupt files — never a deep
    KeyError raised from inside the pair loop;
  * discovery priority is offline-checkable: a synthetic v3 beats v2.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from graphql.errors import (
    FBGraphError,
    RegistryLoadError,
    RegistryMissError,
)
from graphql.registry import DocIdRegistry


# ------------------------------------------------------------- construction
class TestFromAssetsMiss:
    """Pins the typed RegistryMissError (with re-harvest guidance) for an
    absent registry file or directory."""

    def test_empty_assets_dir_is_a_typed_registry_miss(self, tmp_path):
        with pytest.raises(RegistryMissError, match="run scripts/02_harvest") as ei:
            DocIdRegistry.from_assets(tmp_path)
        assert isinstance(ei.value, FBGraphError)  # exit-code mapping relies on this
        assert not isinstance(ei.value, RegistryLoadError)  # a miss, not a corrupt file

    def test_missing_dir_is_a_typed_registry_miss(self, tmp_path):
        with pytest.raises(RegistryMissError):
            DocIdRegistry.from_assets(tmp_path / "absent")


class TestCorruptRegistryFiles:
    """Corrupt registry JSON raises the TYPED RegistryLoadError naming the
    file — never a deep KeyError from the pair loop below."""

    DEEP_ERRORS = (KeyError, IndexError)

    def _write_v2(self, tmp_path: Path, text: str) -> None:
        (tmp_path / "doc_id_registry_v2.json").write_text(text, encoding="utf-8")

    @pytest.mark.parametrize("text", [
        "{oops not json",
        '["not", "a", "registry"]',
        json.dumps({"revision": "1", "sources": []}),
        json.dumps({"unique_pairs": [{"friendly_name": "OnlyFriendly"}]}),
    ], ids=["unparseable-json", "json-array", "wrong-shape-object",
            "pair-rows-missing-keys"])
    def test_corrupt_files_raise_the_typed_load_error(self, tmp_path, text):
        self._write_v2(tmp_path, text)
        with pytest.raises(RegistryLoadError) as ei:
            DocIdRegistry.from_assets(tmp_path)
        # a clear error names the offending file and its directory…
        assert "doc_id_registry_v2.json" in str(ei.value)
        assert str(tmp_path) in str(ei.value)
        # …and never lets a deep KeyError/IndexError escape unwrapped
        assert not isinstance(ei.value, self.DEEP_ERRORS)

    def test_corrupt_preferred_file_with_no_fallback_still_raises_typed(
            self, tmp_path):
        """A corrupt v3 (e.g. a truncated refresh write) with no v2 on
        disk raises the typed load error, not a raw JSONDecodeError."""
        (tmp_path / "doc_id_registry_v3.json").write_text("{oops", encoding="utf-8")
        with pytest.raises(RegistryLoadError, match=r"doc_id_registry_v3\.json"):
            DocIdRegistry.from_assets(tmp_path)

    def test_pair_rows_missing_keys_are_not_a_deep_keyerror(self, tmp_path):
        self._write_v2(tmp_path, json.dumps(
            {"unique_pairs": [{"friendly_name": "OnlyFriendly"}]}))
        with pytest.raises(Exception) as ei:
            DocIdRegistry.from_assets(tmp_path)
        assert not isinstance(ei.value, self.DEEP_ERRORS)


# ------------------------------------------------------- fail-soft descent
class TestFailSoftDescent:
    """Pins the per-file fail-soft descent: a corrupt-but-present file
    is skipped with a stderr warning naming it, the next healthy
    priority candidate serves reads, and only an all-corrupt assets dir
    raises the typed RegistryLoadError naming what was tried."""

    def test_corrupt_v3_descends_to_healthy_v2(self, tmp_path, capsys):
        (tmp_path / "doc_id_registry_v3.json").write_text("{oops", encoding="utf-8")
        (tmp_path / "doc_id_registry_v2.json").write_text(
            json.dumps(_registry_file({"A": "1"})), encoding="utf-8")
        reg = DocIdRegistry.from_assets(tmp_path)
        assert reg.source == "doc_id_registry_v2.json"
        assert reg.doc_id("A") == "1"
        err = capsys.readouterr().err
        assert "warning" in err
        assert "doc_id_registry_v3.json" in err

    def test_corrupt_everything_raises_load_error_naming_all(self, tmp_path):
        (tmp_path / "doc_id_registry_v3.json").write_text("{oops", encoding="utf-8")
        (tmp_path / "doc_id_registry_v2.json").write_text('["not"]', encoding="utf-8")
        with pytest.raises(RegistryLoadError) as ei:
            DocIdRegistry.from_assets(tmp_path)
        assert "doc_id_registry_v3.json" in str(ei.value)
        assert "doc_id_registry_v2.json" in str(ei.value)
        assert str(tmp_path) in str(ei.value)

    def test_corrupt_fallback_warns_when_healthy_v3_wins(self, tmp_path, capsys):
        (tmp_path / "doc_id_registry_v3.json").write_text(
            json.dumps(_registry_file({"A": "3"})), encoding="utf-8")
        (tmp_path / "doc_id_registry_v2.json").write_text("{oops", encoding="utf-8")
        reg = DocIdRegistry.from_assets(tmp_path)
        assert reg.source == "doc_id_registry_v3.json"
        assert reg.doc_id("A") == "3"
        assert "doc_id_registry_v2.json" in capsys.readouterr().err


# ------------------------------------------------------------------ priority
def _registry_file(pairs: dict[str, str]) -> dict:
    return {"revision": "r", "sources": ["synthetic"],
            "unique_pairs": [{"friendly_name": n, "doc_id": d}
                             for n, d in sorted(pairs.items())]}


class TestDiscoveryPriority:
    """Pins the v3 > v2 > merged > full discovery order with synthetic
    registry files."""

    def test_v3_is_preferred_over_v2(self, tmp_path):
        (tmp_path / "doc_id_registry_v3.json").write_text(
            json.dumps(_registry_file({"Shared": "3", "OnlyV3": "33"})),
            encoding="utf-8")
        (tmp_path / "doc_id_registry_v2.json").write_text(
            json.dumps(_registry_file({"Shared": "2", "OnlyV2": "22"})),
            encoding="utf-8")
        reg = DocIdRegistry.from_assets(tmp_path)
        assert reg.source == "doc_id_registry_v3.json"
        assert reg.doc_id("Shared") == "3"      # v3 wins shared names
        assert reg.doc_id("OnlyV3") == "33"
        assert reg.get("OnlyV2") is None        # v2 is not even consulted

    def test_v2_used_when_v3_absent(self, tmp_path):
        (tmp_path / "doc_id_registry_v2.json").write_text(
            json.dumps(_registry_file({"A": "1"})), encoding="utf-8")
        reg = DocIdRegistry.from_assets(tmp_path)
        assert reg.source == "doc_id_registry_v2.json"
        assert reg.doc_id("A") == "1"

    def test_merged_file_beats_full_when_v2_absent(self, tmp_path):
        (tmp_path / "doc_id_registry_merged.json").write_text(
            json.dumps(_registry_file({"M": "1"})), encoding="utf-8")
        (tmp_path / "doc_id_registry_full.json").write_text(
            json.dumps(_registry_file({"F": "1"})), encoding="utf-8")
        reg = DocIdRegistry.from_assets(tmp_path)
        assert reg.source == "doc_id_registry_merged.json"


# ------------------------------------------------------------- inline source
class TestFromPairsSource:
    """Pins the inline-pairs source label of DocIdRegistry.from_pairs()."""

    def test_inline_pairs_report_their_source(self):
        reg = DocIdRegistry.from_pairs({"X": "1"})
        assert reg.source == "inline"
        assert "X" in reg
        assert len(reg) == 1
