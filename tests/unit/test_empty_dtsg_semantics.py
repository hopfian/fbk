"""Empty-DTSG semantics regression (just-landed fix, wave 2).

The trap: ``SecretStr("")`` is TRUTHY as an instance — an emptiness check
on the wrapper turns a *missing* token into an empty-string one. The fix:
both ``Bootstrap`` (auth) and ``CachedBootstrap`` (token cache) unwrap
BEFORE deciding, and a ``TokenCache.save()`` of a dtsg-less bootstrap must
leave a cache entry that ``load()`` REJECTS — the next call regenerates
rather than trusting an empty token.

Complements test_token_cache.py / test_token_cache_boundaries.py (which
cover populated entries and corrupt shapes, not the empty-string seam).

Unit (offline): every cache file lives in tmp_path; token construction
is pure pydantic. No session, no network.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auth.bootstrap import Bootstrap
from auth.state import LoginState
from token_cache import CachedBootstrap, TokenCache

T0 = 1_789_723_000.0


def make_empty_dtsg_bootstrap() -> Bootstrap:
    """A logged-in bootstrap whose page carried NO dtsg token."""
    return Bootstrap(state=LoginState.LOGGED_IN, fb_dtsg=SecretStr(""))


class TestTheSecretStrTrap:
    """Pins the trap itself: SecretStr('') is a present wrapper holding
    an empty payload — only the unwrapped value may decide emptiness."""

    def test_the_wrapper_holds_an_empty_value_without_being_none(self):
        """Pins WHY the unwrapping matters: the wrapper is a live object
        either way — only the UNWRAPPED value decides emptiness, never the
        wrapper's own truthiness (which varies across pydantic versions)."""
        empty = SecretStr("")
        assert empty is not None                    # a present wrapper...
        assert empty.get_secret_value() == ""       # ...with nothing inside
        assert make_empty_dtsg_bootstrap().fb_dtsg is not None
        assert make_empty_dtsg_bootstrap().dtsg() is None  # the fix holds


# ------------------------------------------------- CachedBootstrap accessors
class TestCachedBootstrapEmptySemantics:
    """Pins CachedBootstrap accessor semantics: empty/absent tokens yield
    None, never a truthy-empty secret."""

    def test_dtsg_none_when_secret_wraps_empty_string(self):
        entry = CachedBootstrap(fb_dtsg=SecretStr(""), state="logged_in")
        assert entry.dtsg() is None

    def test_dtsg_field_is_required(self):
        with pytest.raises(ValidationError):
            CachedBootstrap(state="logged_in")

    def test_lsd_none_when_secret_wraps_empty_string(self):
        entry = CachedBootstrap(fb_dtsg="NAfOK", lsd=SecretStr(""),
                                state="logged_in")
        assert entry.lsd_value() is None

    def test_lsd_none_when_secret_absent(self):
        entry = CachedBootstrap(fb_dtsg="NAfOK", lsd=None, state="logged_in")
        assert entry.lsd_value() is None

    def test_populated_tokens_round_trip(self):
        entry = CachedBootstrap(fb_dtsg=SecretStr("NAfX:1:1"), lsd=SecretStr("L"),
                                state="logged_in")
        assert entry.dtsg() == "NAfX:1:1"
        assert entry.lsd_value() == "L"


# ---------------------------------------------------- Bootstrap accessors
class TestBootstrapEmptySemantics:
    """Pins Bootstrap accessor semantics: empty/absent tokens yield None,
    never a truthy-empty secret."""

    def test_dtsg_none_when_secret_wraps_empty_string(self):
        assert make_empty_dtsg_bootstrap().dtsg() is None

    def test_dtsg_none_when_secret_absent(self):
        assert Bootstrap(state=LoginState.LOGGED_IN).dtsg() is None

    def test_lsd_none_when_secret_wraps_empty_string(self):
        boot = Bootstrap(state=LoginState.LOGGED_IN, lsd=SecretStr(""))
        assert boot.lsd_value() is None

    def test_lsd_none_when_secret_absent(self):
        assert Bootstrap(state=LoginState.LOGGED_IN).lsd_value() is None

    def test_populated_tokens_round_trip(self):
        boot = Bootstrap(state=LoginState.LOGGED_IN, fb_dtsg=SecretStr("NAfY:1:2"),
                         lsd=SecretStr("LSD"))
        assert boot.dtsg() == "NAfY:1:2"
        assert boot.lsd_value() == "LSD"


# ------------------------------------- save of a dtsg-less bootstrap is a miss
class TestTokenCacheRejectsEmptyDtsg:
    """Pins the cache-miss contract: a dtsg-less save leaves an entry that
    load() REJECTS, so the next call regenerates instead of trusting it."""

    def test_saved_dtsgless_bootstrap_is_rejected_on_load(self, tmp_path):
        cache = TokenCache(tmp_path / "tokens.json")
        cache.save(make_empty_dtsg_bootstrap())
        # the file exists and holds an EMPTY dtsg — never a usable token
        raw = json.loads((tmp_path / "tokens.json").read_text(encoding="utf-8"))
        assert raw["fb_dtsg"] == ""
        # ...so load() must report a cache miss (regeneration path)
        assert cache.load(now=T0) is None

    def test_handwritten_empty_dtsg_entry_is_rejected(self, tmp_path):
        (tmp_path / "tokens.json").write_text(json.dumps({
            "fb_dtsg": "", "state": "logged_in",
            "cached_at": T0, "expires_at": T0 + 3600}), encoding="utf-8")
        assert TokenCache(tmp_path / "tokens.json").load(now=T0) is None

    def test_rejection_is_stable_across_repeated_loads(self, tmp_path):
        """The miss must persist: no code path may 'heal' an empty token."""
        cache = TokenCache(tmp_path / "tokens.json")
        cache.save(make_empty_dtsg_bootstrap())
        assert cache.load(now=T0) is None
        assert cache.load(now=T0) is None
        assert cache.status()["cached"] is False

    def test_populated_bootstrap_still_saves_and_loads(self, tmp_path):
        """Guard against over-tightening: a REAL token must still round-trip."""
        cache = TokenCache(tmp_path / "tokens.json")
        cache.save(Bootstrap(state=LoginState.LOGGED_IN,
                             fb_dtsg=SecretStr("NAfZ:1:3"),
                             lsd=SecretStr("LSD")))
        entry = cache.load(now=T0)
        assert entry is not None
        assert entry.dtsg() == "NAfZ:1:3"
        assert entry.lsd_value() == "LSD"
