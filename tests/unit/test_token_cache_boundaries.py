"""TokenCache boundary tests: exact-TTL expiry, deterministic save stamps,
corrupt-shape rejection, status reporting and secret redaction.

Complements test_token_cache.py (roundtrip, session wiring). Time is
frozen via monkeypatch where save() stamps the file; load() is driven
through its injected ``now`` so expiry boundaries are exact.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

from pydantic import SecretStr

import token_cache as token_cache_module
from auth.state import LoginState
from token_cache import CachedBootstrap, TokenCache

T0 = 1_789_723_000.0


def make_boot(state=LoginState.LOGGED_IN) -> SimpleNamespace:
    return SimpleNamespace(
        fb_dtsg=SecretStr("NAfTEST:1:1789723298"),
        lsd=SecretStr("TESTLSD"),
        user_id="12345678901234",
        user_name="Test User",
        revision="1047868043",
        state=state,
        dtsg=lambda: "NAfTEST:1:1789723298",
        lsd_value=lambda: "TESTLSD",
    )


class TestTTLBoundaries:
    """Pins exact-TTL expiry semantics (strict <: the window is closed)
    and deterministic save stamps under frozen time."""

    def test_is_fresh_expires_exactly_at_ttl(self):
        entry = CachedBootstrap(fb_dtsg="NAfTEST", state="logged_in",
                                cached_at=0.0, expires_at=1000.0)
        assert entry.is_fresh(999.999) is True
        assert entry.is_fresh(1000.0) is False  # strict <: the window is closed

    def test_load_boundary_at_exact_expiry(self, tmp_path: Path):
        path = tmp_path / "tokens.json"
        cache = TokenCache(path)
        cache.save(make_boot())
        expires_at = json.loads(path.read_text(encoding="utf-8"))["expires_at"]
        assert cache.load(now=expires_at - 1e-6) is not None
        assert cache.load(now=expires_at) is None

    def test_save_stamps_are_deterministic(self, tmp_path, monkeypatch):
        monkeypatch.setattr(token_cache_module.time, "time", lambda: T0)
        path = tmp_path / "tokens.json"
        TokenCache(path).save(make_boot())
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["cached_at"] == T0
        assert raw["expires_at"] == T0 + TokenCache.DEFAULT_TTL_S

    def test_custom_ttl_shifts_expiry(self, tmp_path, monkeypatch):
        monkeypatch.setattr(token_cache_module.time, "time", lambda: T0)
        path = tmp_path / "tokens.json"
        TokenCache(path, ttl_s=60).save(make_boot())
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["expires_at"] == T0 + 60

    def test_default_ttl_is_fifteen_minutes(self):
        assert TokenCache.DEFAULT_TTL_S == 15 * 60.0


class TestLoadRejection:
    """Pins corrupt-shape rejection on load: missing dtsg, logged-out
    state, non-numeric fields — while tolerant of unknown extra keys."""

    def _write(self, tmp_path: Path, payload) -> None:
        (tmp_path / "tokens.json").write_text(json.dumps(payload),
                                               encoding="utf-8")

    def test_entry_without_dtsg_rejected(self, tmp_path):
        self._write(tmp_path, {"state": "logged_in", "expires_at": T0 + 600})
        assert TokenCache(tmp_path / "tokens.json").load(now=T0) is None

    def test_logged_out_state_rejected(self, tmp_path):
        self._write(tmp_path, {
            "fb_dtsg": "NAfTEST", "lsd": "L", "state": "logged_out",
            "cached_at": T0, "expires_at": T0 + 600})
        assert TokenCache(tmp_path / "tokens.json").load(now=T0) is None

    def test_non_numeric_expiry_rejected(self, tmp_path):
        self._write(tmp_path, {
            "fb_dtsg": "NAfTEST", "state": "logged_in",
            "cached_at": 0, "expires_at": "soon"})
        assert TokenCache(tmp_path / "tokens.json").load(now=T0) is None

    def test_non_string_dtsg_rejected(self, tmp_path):
        self._write(tmp_path, {
            "fb_dtsg": 12345, "state": "logged_in",
            "cached_at": 0, "expires_at": T0 + 600})
        assert TokenCache(tmp_path / "tokens.json").load(now=T0) is None

    def test_fresh_entry_in_a_dict_with_extra_keys_survives(self, tmp_path):
        self._write(tmp_path, {
            "fb_dtsg": "NAfTEST", "lsd": "L", "state": "logged_in",
            "cached_at": T0, "expires_at": T0 + 600, "junk": "ignored"})
        entry = TokenCache(tmp_path / "tokens.json").load(now=T0)
        assert entry is not None
        assert entry.dtsg() == "NAfTEST"


class TestStatusReports:
    """Pins the status dict for the hit, miss, and post-invalidate states."""

    def test_status_after_save_reports_every_field(self, tmp_path):
        path = tmp_path / "tokens.json"
        cache = TokenCache(path, ttl_s=600)
        cache.save(make_boot())
        status = cache.status()
        assert status["cached"] is True
        assert status["user_id"] == "12345678901234"
        assert status["revision"] == "1047868043"
        assert status["expires_at"] == status["cached_at"] + 600
        assert status["ttl_s"] == 600
        assert status["path"] == str(path)

    def test_status_miss_reports_the_minimal_shape(self, tmp_path):
        path = tmp_path / "tokens.json"
        status = TokenCache(path, ttl_s=60).status()
        assert status == {"cached": False, "path": str(path), "ttl_s": 60}

    def test_status_after_invalidate_reports_miss(self, tmp_path):
        cache = TokenCache(tmp_path / "tokens.json")
        cache.save(make_boot())
        assert cache.status()["cached"] is True
        cache.invalidate()
        assert cache.status()["cached"] is False


class TestSecretHandling:
    """Pins SecretStr shielding through repr/dumps and the
    known-keys-only disk shape."""

    def test_secretstr_never_exposes_the_token(self, tmp_path):
        entry = CachedBootstrap(fb_dtsg="NAfxxxxxxxxxxx", state="logged_in")
        assert "NAfxxxxxxxxxxx" not in repr(entry)
        assert str(entry.fb_dtsg) == "**********"
        assert entry.model_dump()["fb_dtsg"] == "**********"
        assert "NAfxxxxxxxxxxx" not in json.dumps(entry.model_dump())

    def test_lsd_is_optional(self, tmp_path):
        cache = TokenCache(tmp_path / "tokens.json")
        cache.save(make_boot())
        loaded = cache.load()
        assert loaded is not None
        assert loaded.lsd_value() == "TESTLSD"

        cache2 = TokenCache(tmp_path / "tokens2.json")
        boot = make_boot()
        boot.lsd = None
        boot.lsd_value = lambda: None
        cache2.save(boot)
        loaded2 = cache2.load()
        assert loaded2 is not None
        assert loaded2.lsd_value() is None
        assert loaded2.lsd is None

    def test_plain_string_state_saves_and_loads(self, tmp_path):
        cache = TokenCache(tmp_path / "tokens.json")
        cache.save(make_boot(state="logged_in"))
        loaded = cache.load()
        assert loaded is not None
        assert loaded.state == "logged_in"
        assert loaded.is_fresh() is True

    def test_disk_file_holds_only_known_keys(self, tmp_path):
        path = tmp_path / "tokens.json"
        TokenCache(path).save(make_boot())
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert set(raw) == {"fb_dtsg", "lsd", "user_id", "user_name",
                            "revision", "state", "cached_at", "expires_at"}


class TestInvalidate:
    """Pins invalidate's no-op on a missing file and that load() falls
    back to the wall clock when no ``now`` is injected."""

    def test_invalidate_missing_file_is_a_noop(self, tmp_path):
        cache = TokenCache(tmp_path / "absent.json")
        cache.invalidate()  # must not raise
        assert cache.load() is None

    def test_load_uses_wall_clock_without_injected_now(self, tmp_path, monkeypatch):
        monkeypatch.setattr(token_cache_module.time, "time", lambda: T0)
        cache = TokenCache(tmp_path / "tokens.json", ttl_s=600)
        cache.save(make_boot())
        monkeypatch.setattr(token_cache_module.time, "time", lambda: T0 + 601)
        assert cache.load() is None  # no now= -> wall clock drove the check
        assert time.time() == T0 + 601  # monkeypatch sanity
