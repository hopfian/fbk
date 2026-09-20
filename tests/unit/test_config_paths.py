"""Config discovery deep-dive: path aliases (assets_dir/journal_dir),
profile.json detection, env overrides and field validation.

Complements test_config.py (root discovery, cookies override). Unit
(offline): every discovery runs against a tmp_path root with the env
vars monkeypatched — no network, no live config state.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import constants as C
from config import Config


class TestPathAliases:
    """Pins the assets_dir/journal_dir aliases over data_dir/state_dir."""

    def test_assets_dir_aliases_data_dir(self, tmp_path):
        cfg = Config.discover(tmp_path)
        assert cfg.assets_dir == cfg.data_dir == tmp_path.resolve() / "data"

    def test_journal_dir_aliases_state_dir(self, tmp_path):
        cfg = Config.discover(tmp_path)
        assert cfg.journal_dir == cfg.state_dir == tmp_path.resolve() / "state"


class TestProfileDetection:
    """Pins profile.json detection: present -> profile_path set, absent -> None."""

    def test_profile_path_set_when_profile_json_exists(self, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        (data / "profile.json").write_text("{}", encoding="utf-8")
        cfg = Config.discover(tmp_path)
        assert cfg.profile_path == data / "profile.json"

    def test_profile_path_none_when_absent(self, tmp_path):
        cfg = Config.discover(tmp_path)
        assert cfg.profile_path is None

    def test_real_package_root_has_no_profile_json(self, monkeypatch):
        monkeypatch.delenv("FBK_ROOT", raising=False)
        monkeypatch.delenv("FBK_COOKIES", raising=False)
        assert Config.discover().profile_path is None


class TestDefaultsAndValidation:
    """Pins field defaults, the impersonate env override, and the
    timeout > 0 validation bound."""

    def _minimal(self, tmp_path: Path, **overrides) -> dict:
        base = {
            "root": tmp_path,
            "cookies_path": tmp_path / "cookies.txt",
            "data_dir": tmp_path / "data",
            "state_dir": tmp_path / "state",
        }
        base.update(overrides)
        return base

    def test_default_timeout_and_endpoint(self, tmp_path):
        cfg = Config(**self._minimal(tmp_path))
        assert cfg.timeout == 30.0
        assert cfg.graphql_endpoint == C.GRAPHQL_ENDPOINT

    def test_default_impersonate_matches_constants(self, tmp_path):
        assert Config(**self._minimal(tmp_path)).impersonate == C.IMPERSONATE_DEFAULT

    def test_impersonate_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FBK_IMPERSONATE", "chrome131")
        assert Config.discover(tmp_path).impersonate == "chrome131"

    def test_zero_timeout_rejected_by_validation(self, tmp_path):
        with pytest.raises(ValidationError):
            Config(**self._minimal(tmp_path, timeout=0))

    def test_negative_timeout_rejected_by_validation(self, tmp_path):
        with pytest.raises(ValidationError):
            Config(**self._minimal(tmp_path, timeout=-1))

    def test_cookies_default_under_root(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FBK_COOKIES", raising=False)
        cfg = Config.discover(tmp_path)
        assert cfg.cookies_path == tmp_path.resolve() / "cookies.txt"

    def test_journal_file_lands_in_state_dir(self, tmp_path):
        cfg = Config.discover(tmp_path)
        run_file = cfg.journal_file("runs")
        assert run_file.parent == cfg.state_dir
        assert run_file.name == "runs.jsonl"


class TestGraphqlEndpointSingleSourcing:
    """Pins the endpoint's single source of truth (just-landed fix):
    ``Config.discover`` resolves the endpoint from
    ``constants.GRAPHQL_ENDPOINT`` — never a drifting literal — and no
    discovery-time input (env overrides, profile detection) moves it."""

    def test_discover_uses_the_constants_endpoint(self, tmp_path):
        assert Config.discover(tmp_path).graphql_endpoint == C.GRAPHQL_ENDPOINT

    def test_endpoint_immune_to_env_overrides(self, tmp_path, monkeypatch):
        """FBK_* overrides steer paths and impersonation only — the
        endpoint must stay pinned to the constant."""
        monkeypatch.setenv("FBK_IMPERSONATE", "chrome131")
        monkeypatch.setenv("FBK_COOKIES", str(tmp_path / "alt-cookies.txt"))
        cfg = Config.discover(tmp_path)
        assert cfg.graphql_endpoint == C.GRAPHQL_ENDPOINT
        assert cfg.cookies_path == tmp_path / "alt-cookies.txt"
        assert cfg.impersonate == "chrome131"  # the overrides DID apply

    def test_endpoint_identical_constructed_vs_discovered(self, tmp_path):
        """Direct construction and discover() agree on the endpoint —
        both flow through the same field default."""
        direct = Config(**{
            "root": tmp_path, "cookies_path": tmp_path / "cookies.txt",
            "data_dir": tmp_path / "data", "state_dir": tmp_path / "state",
        })
        assert direct.graphql_endpoint == Config.discover(
            tmp_path).graphql_endpoint == C.GRAPHQL_ENDPOINT
