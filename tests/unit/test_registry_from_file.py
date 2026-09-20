"""graphql.registry.DocIdRegistry.from_file — the version-addressed
disk loader (docs/13 §2; the ``registry diff`` pre-flight seam).

test_registry.py pins from_assets discovery and lookups on the REAL
data/ registry; test_registry_robustness.py pins from_assets' fail-soft
descent and typed errors. What was missing is the one-NAMED-file
loader's own contract — previously covered only by the verification run:

  * the REAL data/ registries load by name: v3 agrees with from_assets'
    v3 pick (pair count, source, revision) and v2 loads independently
    with its own, older provenance;
  * a corrupt-but-present named file raises the TYPED RegistryLoadError
    naming the file — raised immediately, no fail-soft descent (there
    is no fallback candidate) — never a deep KeyError from the pair
    loop; the empty-mutations shape (no ``unique_pairs`` key) is one
    such wrong-schema corruption;
  * a missing named file raises the TYPED RegistryMissError (a miss,
    never a corrupt-file error) with re-harvest/refresh guidance;
  * ``revision`` provenance rides the file's deploy tag and is ``None``
    for a file without one.

Unit (offline): pure in-memory file reads — the real-data tests read
the shipped cli/data/ registry files (no network, no cookies), the
error-contract tests use synthetic tmp_path trees. Fixtures: the two
real registries load once at module scope; every synthetic case builds
its own tmp_path file.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fakes import ASSETS

from graphql.errors import FBGraphError, RegistryLoadError, RegistryMissError
from graphql.registry import DocIdRegistry

V3 = "doc_id_registry_v3.json"
V2 = "doc_id_registry_v2.json"

# The real shipped registries (loader-level pins; the pair-dict sizes —
# unique_pairs entries collapse to distinct friendly names on load).
_V3_PAIRS = 1031
_V2_PAIRS = 1028
_V3_REVISION = "1047963790"
_V2_REVISION = "1047871790"


@pytest.fixture(scope="module")
def v3() -> DocIdRegistry:
    return DocIdRegistry.from_file(ASSETS, V3)


@pytest.fixture(scope="module")
def v2() -> DocIdRegistry:
    return DocIdRegistry.from_file(ASSETS, V2)


def _write(tmp_path: Path, text: str) -> None:
    (tmp_path / V3).write_text(text, encoding="utf-8")


def _registry_file(pairs: dict[str, str],
                   revision: str | None = "r") -> dict[str, object]:
    """A synthetic registry file body (the unique_pairs schema)."""
    doc: dict[str, object] = {
        "unique_pairs": [{"friendly_name": n, "doc_id": d}
                         for n, d in sorted(pairs.items())]}
    if revision is not None:
        doc["revision"] = revision
    return doc


# --------------------------------------------------------- real registries
class TestRealRegistries:
    """The REAL shipped data/ registries, loaded by name — the loader
    must address one specific version that from_assets' priority walk
    cannot (v2 loads even though from_assets always prefers v3)."""

    def test_v3_agrees_with_the_from_assets_pick(self, v3: DocIdRegistry) -> None:
        """from_assets prefers v3; from_file(v3) is the same table —
        same pair count, same source tag, same revision provenance."""
        pick = DocIdRegistry.from_assets(ASSETS)
        assert v3.source == pick.source == V3
        assert len(v3) == len(pick) == _V3_PAIRS
        assert v3.revision == pick.revision == _V3_REVISION

    def test_v2_loads_with_its_own_provenance(self, v2: DocIdRegistry,
                                              v3: DocIdRegistry) -> None:
        """v2 loads independently: its own (older) deploy revision, its
        own source tag, never confused with the v3 pick."""
        assert v2.source == V2
        assert len(v2) == _V2_PAIRS
        assert v2.revision == _V2_REVISION
        assert v2.revision != v3.revision

    def test_v3_carries_the_v2_baseline(self, v2: DocIdRegistry,
                                        v3: DocIdRegistry) -> None:
        """Merge semantics of the refresh output: every v2 friendly name
        resolves in v3 to the same doc_id (v3 supersedes by carrying)."""
        assert set(v2) <= set(v3)
        assert v2.doc_id("CometUFIFeedbackReactMutation") \
            == v3.doc_id("CometUFIFeedbackReactMutation")


# ------------------------------------------------------------- typed errors
class TestFromFileMiss:
    """Missing named file -> the TYPED RegistryMissError (a miss, never
    a corrupt-file error), naming the file, the directory, and the
    recovery action (harvest or `registry refresh --save`)."""

    def test_missing_file_is_a_typed_miss(self, tmp_path: Path) -> None:
        with pytest.raises(RegistryMissError) as ei:
            DocIdRegistry.from_file(tmp_path, V3)
        msg = str(ei.value)
        assert V3 in msg
        assert str(tmp_path) in msg
        assert "registry refresh --save" in msg
        assert isinstance(ei.value, FBGraphError)  # exit-code mapping relies on this
        assert not isinstance(ei.value, RegistryLoadError)  # a miss, not corruption

    def test_missing_file_in_missing_dir_is_a_typed_miss(
            self, tmp_path: Path) -> None:
        with pytest.raises(RegistryMissError):
            DocIdRegistry.from_file(tmp_path / "absent", V3)


class TestFromFileCorrupt:
    """Corrupt-but-present named file -> the TYPED RegistryLoadError
    naming the file and directory, raised immediately (no fail-soft
    descent — there is no fallback candidate) — never a deep
    KeyError/IndexError from the pair loop escaping unwrapped."""

    @pytest.mark.parametrize("text", [
        "{oops not json",
        '["not", "a", "registry"]',
        json.dumps({"mutations": []}),
        json.dumps({"revision": "1", "sources": []}),
        json.dumps({"unique_pairs": [{"friendly_name": "OnlyFriendly"}]}),
    ], ids=["unparseable-json", "json-array", "empty-mutations-shape",
            "wrong-shape-object", "pair-rows-missing-keys"])
    def test_corrupt_file_raises_the_typed_load_error(
            self, tmp_path: Path, text: str) -> None:
        _write(tmp_path, text)
        with pytest.raises(RegistryLoadError) as ei:
            DocIdRegistry.from_file(tmp_path, V3)
        assert V3 in str(ei.value)
        assert str(tmp_path) in str(ei.value)
        # a deep KeyError/IndexError never escapes unwrapped
        assert not isinstance(ei.value, KeyError)
        assert not isinstance(ei.value, IndexError)

    def test_empty_unique_pairs_list_is_not_corruption(
            self, tmp_path: Path) -> None:
        """Contract boundary: only a missing/malformed ``unique_pairs``
        is corruption — a present but EMPTY list loads as a zero-pair
        registry (the revision tag still rides along)."""
        _write(tmp_path, json.dumps({"revision": "r", "unique_pairs": []}))
        reg = DocIdRegistry.from_file(tmp_path, V3)
        assert reg.source == V3
        assert len(reg) == 0
        assert reg.revision == "r"


# -------------------------------------------------------------- provenance
class TestRevisionProvenance:
    """``revision`` rides the file's deploy tag; a file without one
    loads with ``revision is None`` (provenance absent, not invented)."""

    def test_revision_is_carried_from_the_deploy_tag(
            self, tmp_path: Path) -> None:
        _write(tmp_path, json.dumps(_registry_file({"A": "1"},
                                                   revision="deploy-42")))
        reg = DocIdRegistry.from_file(tmp_path, V3)
        assert reg.revision == "deploy-42"
        assert reg.source == V3
        assert reg.doc_id("A") == "1"

    def test_absent_revision_is_none(self, tmp_path: Path) -> None:
        _write(tmp_path, json.dumps(_registry_file({"A": "1"},
                                                   revision=None)))
        reg = DocIdRegistry.from_file(tmp_path, V3)
        assert reg.revision is None
        assert reg.doc_id("A") == "1"
