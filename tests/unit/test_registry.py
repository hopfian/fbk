"""Registry tests — against the live-harvested v2 registry (1,032 pairs).

Unit (offline): the module-scoped ``registry`` fixture loads the REAL
assets/doc_id_registry_v2.json via ``fakes.ASSETS``; lookups and regex
matching are pure in-memory operations. No network.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import ASSETS

from graphql.errors import RegistryMissError
from graphql.registry import DocIdRegistry


@pytest.fixture(scope="module")
def registry() -> DocIdRegistry:
    return DocIdRegistry.from_assets(ASSETS)


class TestRegistry:
    """Pins the real harvested registry: discovery priority, the
    live-verified mutation doc_ids, and miss guidance."""

    def test_loads_freshest_priority(self, registry):
        """Priority order: v3 (refresh output) > v2 > merged > full (docs/15 §P5-2)."""
        assert registry.source in ("doc_id_registry_v3.json", "doc_id_registry_v2.json")
        assert len(registry) > 1000
        if registry.source == "doc_id_registry_v3.json":
            # merge semantics: surface-delta pairs must have been CARRIED
            assert "useGroupsCometCreateMutation" in registry
            assert "AdditionalProfilePlusCreationMutation" in registry

    def test_live_verified_doc_ids_present(self, registry):
        """Every mutation live-verified in docs/15 must be in the registry."""
        assert registry.doc_id("CometUFIFeedbackReactMutation") == "27646120298312844"
        assert registry.doc_id("useCometUFICreateCommentMutation") == "39607465588840384"
        assert registry.doc_id("ComposerStoryCreateMutation") == "28778531428503134"
        assert registry.doc_id("useCometAIHTSSendMessageMutation") == "28137996599166900"
        assert registry.doc_id("useGroupsCometCreateMutation") == "37309644325300927"

    def test_miss_raises_with_guidance(self, registry):
        with pytest.raises(RegistryMissError, match="re-harvest"):
            registry.doc_id("NoSuchQueryAnywhere")

    def test_get_returns_none(self, registry):
        assert registry.get("NoSuchQueryAnywhere") is None

    def test_match_regex(self, registry):
        hits = registry.match(r"^CometNotifications.*Query$")
        assert "CometNotificationsBadgeCountQuery" in hits
        assert all(k.endswith("Query") for k in hits)

    def test_contains_and_len(self, registry):
        assert "CometModernHomeFeedQuery" in registry
        assert isinstance(len(registry), int)
