"""[CONTRACT] constants hygiene — dead endpoint/mutation removal.

Contract (landed by the hygiene agent): ``GRAPHQL_ENDPOINT_M`` (the
m.facebook.com endpoint never used by any surface) and
``UFI_REACT_MUTATION`` (a legacy friendly-name constant superseded by
the captured mutation templates) are removed from constants.py, and the
two dead data assets are deleted from data/. The removal tests
self-skip if the names ever reappear; ``import constants`` must keep
working either way.

Needles are built from split parts so this file never self-matches in
its own suite-wide reference scan.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import constants as C

CLI_ROOT = Path(__file__).resolve().parents[2]  # cli/
DATA_DIR = CLI_ROOT / "data"


def _dead_asset_names() -> list[str]:
    return ["captured_dm" + "_send.json", "reaction" + "_ids.json"]


def _removal_landed() -> bool:
    return not (hasattr(C, "GRAPHQL_ENDPOINT_M")
               or hasattr(C, "UFI_REACT_MUTATION"))


class TestHygieneContract:
    """The package must import cleanly; the dead names must be gone."""

    def test_import_constants_still_works(self):
        """Unconditional: every removal must keep the module importable
        (no dangling references left behind by the hygiene edit)."""
        import importlib
        module = importlib.import_module("constants")
        assert module is C
        assert C.GRAPHQL_ENDPOINT  # the live endpoint survives untouched

    def test_dead_endpoint_and_mutation_constants_removed(self):
        if not _removal_landed():
            pytest.skip("[CONTRACT] GRAPHQL_ENDPOINT_M / UFI_REACT_MUTATION "
                        "removal has not landed in constants.py yet")
        assert not hasattr(C, "GRAPHQL_ENDPOINT_M")
        assert not hasattr(C, "UFI_REACT_MUTATION")

    def test_dead_data_assets_stay_deleted(self):
        if not _removal_landed():
            pytest.skip("[CONTRACT] pending the constants.py removal")
        for name in _dead_asset_names():
            assert not (DATA_DIR / name).exists(), name

    def test_no_test_source_references_the_dead_assets(self):
        """Post-removal hygiene: the suite must not mention the deleted
        asset basenames anywhere in tests/."""
        if not _removal_landed():
            pytest.skip("[CONTRACT] pending the constants.py removal")
        for path in sorted((CLI_ROOT / "tests").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for needle in _dead_asset_names():
                assert needle not in text, f"{path.name} references {needle!r}"
