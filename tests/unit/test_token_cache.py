"""TokenCache tests: the Phase 11 persistent bootstrap cache (docs/16 §P11-1).

The cache is the single biggest volume-reduction lever: without it, every
CLI invocation bootstrapped the full homepage (~2-5MB, 1 request) just
to harvest tokens. With it, subsequent commands reuse cached tokens.

Unit (offline): every cache file lives in tmp_path; the session-wiring
test drives the real Session against a seeded temp cache with the HTTP
bootstrap monkeypatched to explode. No network.
"""
from __future__ import annotations

import time
from pathlib import Path

from auth.state import LoginState
from token_cache import CachedBootstrap, TokenCache


class TestCachedBootstrap:
    """Pins CachedBootstrap freshness semantics: TTL window, logged-out
    rejection, and the dtsg accessor."""

    def test_is_fresh_within_ttl(self):
        now = time.time()
        entry = CachedBootstrap(
            fb_dtsg="NAfTEST:1:1789723298",
            cached_at=now - 60,
            expires_at=now + 120,
            state="logged_in")
        assert entry.is_fresh(now) is True

    def test_is_stale_past_ttl(self):
        now = time.time()
        entry = CachedBootstrap(
            fb_dtsg="NAfTEST:1:1789723298",
            cached_at=now - 301,
            expires_at=now - 1,
            state="logged_in")
        assert entry.is_fresh(now) is False

    def test_stale_when_logged_out(self):
        now = time.time()
        entry = CachedBootstrap(
            fb_dtsg="NAfTEST:1:1789723298",
            cached_at=now,
            expires_at=now + 600,
            state="logged_out")
        assert entry.is_fresh(now) is False

    def test_dtsg_accessor(self):
        entry = CachedBootstrap(fb_dtsg="NAfTEST:1:1789723298")
        assert entry.dtsg() == "NAfTEST:1:1789723298"


class TestTokenCache:
    """Pins the cache round-trip, expiry, invalidation, corrupt-file
    tolerance, and the no-cookie-values disk contract."""

    def test_miss_on_missing_file(self, tmp_path):
        cache = TokenCache(tmp_path / "tokens.json")
        assert cache.load() is None

    def test_roundtrip_save_load(self, tmp_path):
        cache = TokenCache(tmp_path / "tokens.json")
        from types import SimpleNamespace

        from pydantic import SecretStr
        boot = SimpleNamespace(
            fb_dtsg=SecretStr("NAfTEST:1:1789723298"),
            lsd=SecretStr("TESTLSD"),
            user_id="123",
            user_name="Test",
            revision="999",
            state=LoginState.LOGGED_IN,
            dtsg=lambda: "NAfTEST:1:1789723298",
            lsd_value=lambda: "TESTLSD",
        )
        cache.save(boot)
        loaded = cache.load()
        assert loaded is not None
        assert loaded.dtsg() == "NAfTEST:1:1789723298"
        assert loaded.lsd_value() == "TESTLSD"
        assert loaded.user_id == "123"
        assert loaded.revision == "999"
        assert loaded.is_fresh()

    def test_expired_entry_rejected(self, tmp_path):
        cache = TokenCache(tmp_path / "tokens.json", ttl_s=0)
        from types import SimpleNamespace

        from pydantic import SecretStr
        boot = SimpleNamespace(
            fb_dtsg=SecretStr("NAfTEST"),
            lsd=None,
            user_id="123",
            user_name=None,
            revision=None,
            state=LoginState.LOGGED_IN,
            dtsg=lambda: "NAfTEST",
            lsd_value=lambda: None,
        )
        cache.save(boot)
        time.sleep(0.1)  # let the TTL elapse
        assert cache.load() is None

    def test_invalidate_clears(self, tmp_path):
        cache = TokenCache(tmp_path / "tokens.json")
        from types import SimpleNamespace

        from pydantic import SecretStr
        boot = SimpleNamespace(
            fb_dtsg=SecretStr("NAfTEST"),
            lsd=None,
            user_id="123",
            user_name=None,
            revision=None,
            state=LoginState.LOGGED_IN,
            dtsg=lambda: "NAfTEST",
            lsd_value=lambda: None,
        )
        cache.save(boot)
        assert cache.load() is not None
        cache.invalidate()
        assert cache.load() is None
        assert not (tmp_path / "tokens.json").is_file()

    def test_corrupt_file_returns_none(self, tmp_path):
        path = tmp_path / "tokens.json"
        path.write_text("{not valid json", encoding="utf-8")
        cache = TokenCache(path)
        assert cache.load() is None

    def test_status_reports_cached(self, tmp_path):
        cache = TokenCache(tmp_path / "tokens.json")
        assert cache.status()["cached"] is False

    def test_cookie_values_never_persisted(self, tmp_path):
        """The disk file must NEVER contain cookie values (docs/11 §7)."""
        import json
        cache = TokenCache(tmp_path / "tokens.json")
        from types import SimpleNamespace

        from pydantic import SecretStr
        boot = SimpleNamespace(
            fb_dtsg=SecretStr("NAfTEST"),
            lsd=None,
            user_id="123",
            user_name=None,
            revision=None,
            state=LoginState.LOGGED_IN,
            dtsg=lambda: "NAfTEST",
            lsd_value=lambda: None,
        )
        cache.save(boot)
        disk = (tmp_path / "tokens.json").read_text(encoding="utf-8")
        # the file holds the DTSG token (by design), but never cookies
        assert "c_user" not in disk
        assert "xs" not in disk.lower().replace('"', "")
        # verify it is valid JSON (round-trip through SecretStr worked)
        json.loads(disk)


class TestSessionCacheWiring:
    """The Session uses the cache-first bootstrap path (docs/16 §P11-1)."""

    def test_session_bootstrap_hits_cache(self, tmp_path, monkeypatch):
        """When a fresh cache entry exists, Session.bootstrap() makes
        ZERO HTTP requests (the biggest possible volume reduction)."""
        from config import Config
        from session import Session
        from token_cache import TokenCache

        cli_root = Path(__file__).resolve().parents[2]  # cli/

        # pre-seed the cache
        cache = TokenCache(tmp_path / "state" / "token_cache.json")
        from types import SimpleNamespace

        from pydantic import SecretStr
        boot = SimpleNamespace(
            fb_dtsg=SecretStr("NAfTEST:1:1789723298"),
            lsd=SecretStr("TESTLSD"),
            user_id="123",
            user_name="Cached",
            revision="cached",
            state=LoginState.LOGGED_IN,
            dtsg=lambda: "NAfTEST:1:1789723298",
            lsd_value=lambda: "TESTLSD",
        )
        cache.save(boot)

        # create a session pointing at the seeded cache (real cli/data for the
        # registry; temp state dir for the token cache)
        # Synthetic cookie jar in tmp_path: the offline suite must never
        # depend on a real cookies.txt (scrubbed personal data never
        # returns as a test dependency).
        jar = tmp_path / "cookies.txt"
        jar.write_text(
            "# Netscape HTTP Cookie File\n"
            ".facebook.com\tTRUE\t/\tTRUE\t0\tc_user\t123\n"
            ".facebook.com\tTRUE\t/\tTRUE\t0\txs\tsynthetic-xs\n",
            encoding="utf-8")
        cfg = Config.discover(cli_root)
        cfg = cfg.model_copy(update={"state_dir": tmp_path / "state",
                                     "cookies_path": jar})
        monkeypatch.setattr(
            "session.bootstrap_homepage",
            lambda t, c: (_ for _ in ()).throw(
                AssertionError("HTTP bootstrap should not run when cache is fresh")))
        session = Session(cfg, journal_name=None)
        result = session.bootstrap()
        assert result.dtsg() == "NAfTEST:1:1789723298"
        assert result.user_id == "123"
        assert "token_cache" in result.markers_seen
