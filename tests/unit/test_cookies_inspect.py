"""``fbk cookies inspect`` — offline jar hygiene audit.

Unit (offline): a synthetic Netscape jar in tmp_path (never the
operator's real cookies.txt) exercises every taxonomy group, the
never-values disclosure boundary, and the exit-code contract: 0 on a
complete auth pair, 1 on a missing pair (failed precondition, stderr),
7 on a missing jar (CookieLoadError through run_command).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app import build_parser
from commands.common import run_command
from constants import AUTH_COOKIES, DEVICE_COOKIES, INFO_COOKIES
from transport.cookies import fingerprint

# Every raw value is a unique synthetic marker; the hygiene assertions
# pin that NONE of them ever reaches stdout (docs/12 §4 boundary).
JAR_FULL = (
    "# Netscape HTTP Cookie File\n"
    ".facebook.com\tTRUE\t/\tTRUE\t1999999999\tc_user\t1234567890\n"
    ".facebook.com\tTRUE\t/\tTRUE\t1999999999\txs\t7:AbCdEfSecret\n"
    ".facebook.com\tTRUE\t/\tTRUE\t1999999999\tdatr\tDeviceTokenValue1\n"
    ".facebook.com\tTRUE\t/\tTRUE\t1999999999\tsb\tSubBrowserValue99\n"
    ".facebook.com\tTRUE\t/\tFALSE\t1999999999\twd\t1900x1000\n"
    ".facebook.com\tTRUE\t/\tFALSE\t1999999999\tdpr\t1\n"
    ".facebook.com\tTRUE\t/\tFALSE\t1999999999\tpresence\tPR-presence-val\n"
    ".facebook.com\tTRUE\t/\tFALSE\t1999999999\tfr\tfr-value-xyz\n"
    ".facebook.com\tTRUE\t/\tFALSE\t1999999999\tps_l\t1\n"
    ".facebook.com\tTRUE\t/\tFALSE\t1999999999\tps_n\t2\n"
    ".facebook.com\tTRUE\t/\tFALSE\t1999999999\tlocale\ten_US\n"
    ".facebook.com\tTRUE\t/\tFALSE\t1999999999\tmessengerPageId\textra-xyz\n"
    ".example.com\tTRUE\t/\tTRUE\t1999999999\tintruder\tnot-facebook\n"
)

RAW_VALUES = ["1234567890", "7:AbCdEfSecret", "DeviceTokenValue1",
              "SubBrowserValue99", "PR-presence-val", "fr-value-xyz",
              "extra-xyz", "not-facebook"]


def _clean_env(monkeypatch) -> None:
    monkeypatch.delenv("FBK_ROOT", raising=False)
    monkeypatch.delenv("FBK_COOKIES", raising=False)


class TestCookiesInspect:
    """Pins taxonomy coverage, the disclosure boundary, and exit codes
    on the real parser + real run_command barrier."""

    @pytest.fixture()
    def full_jar_root(self, tmp_path: Path, monkeypatch) -> Path:
        _clean_env(monkeypatch)
        (tmp_path / "cookies.txt").write_text(JAR_FULL, encoding="utf-8")
        return tmp_path

    def _run(self, capsys, root: Path, argv_extra=()):
        args = build_parser().parse_args(
            ["cookies", "inspect", "--root", str(root), *argv_extra])
        rc = run_command(args.fn, args)
        captured = capsys.readouterr()
        return rc, captured.out, captured.err

    def test_full_taxonomy_coverage_exits_zero(self, full_jar_root, capsys):
        rc, out, _ = self._run(capsys, full_jar_root)
        assert rc == 0
        payload = json.loads(out.splitlines()[-1])
        for name in AUTH_COOKIES:
            assert payload["auth"][name]["present"] is True
        for name in DEVICE_COOKIES:
            assert payload["device"][name]["present"] is True
        for name in INFO_COOKIES:
            assert payload["info"][name]["present"] is True
        # non-facebook.com rows never load; unclassified names go to "other"
        assert list(payload["other"]) == ["messengerPageId"]
        assert payload["auth_pair_complete"] is True

    def test_presence_only_rows_carry_fingerprints_not_values(self, full_jar_root,
                                                              capsys):
        """The disclosure boundary (docs/12 §4): every present cookie is
        reported by fingerprint; NO raw value ever reaches stdout."""
        rc, out, _ = self._run(capsys, full_jar_root)
        assert rc == 0
        for value in RAW_VALUES:
            assert value not in out
        payload = json.loads(out.splitlines()[-1])
        assert payload["auth"]["xs"]["fingerprint"] == fingerprint("7:AbCdEfSecret")
        for group in ("auth", "device", "info"):
            for row in payload[group].values():
                if row["present"]:
                    assert re.fullmatch(r"[0-9a-f]{12}", row["fingerprint"])

    def test_missing_auth_pair_is_a_failed_precondition(self, tmp_path, capsys,
                                                         monkeypatch):
        _clean_env(monkeypatch)
        jar = JAR_FULL.replace(
            ".facebook.com\tTRUE\t/\tTRUE\t1999999999\txs\t7:AbCdEfSecret\n", "")
        (tmp_path / "cookies.txt").write_text(jar, encoding="utf-8")
        rc, out, err = self._run(capsys, tmp_path)
        assert rc == 1
        assert "auth pair incomplete" in err and "xs" in err
        payload = json.loads(out.splitlines()[-1])
        assert payload["auth"]["xs"]["present"] is False
        assert payload["auth_pair_complete"] is False

    def test_missing_jar_maps_to_cookie_load_error_exit_7(self, tmp_path, capsys,
                                                          monkeypatch):
        _clean_env(monkeypatch)
        rc, _, err = self._run(capsys, tmp_path)
        assert rc == 7
        assert "cookie file not found" in err

    def test_flat_jar_expiry_note_is_reported(self, full_jar_root, capsys):
        """load_netscape returns {name: value} — the report must state
        expiry is not tracked instead of widening the loader contract."""
        rc, out, _ = self._run(capsys, full_jar_root)
        assert rc == 0
        payload = json.loads(out.splitlines()[-1])
        assert "expiry not tracked" in payload["note"]
        assert "expiry not tracked" in out

    def test_fbk_cookies_env_override_selects_the_jar(self, tmp_path, monkeypatch,
                                                      capsys):
        alt = tmp_path / "alt-jar.txt"
        alt.write_text(
            "# Netscape HTTP Cookie File\n"
            ".facebook.com\tTRUE\t/\tTRUE\t0\tc_user\t123456\n"
            ".facebook.com\tTRUE\t/\tTRUE\t0\txs\tsynthetic-xs\n",
            encoding="utf-8")
        monkeypatch.delenv("FBK_ROOT", raising=False)
        monkeypatch.setenv("FBK_COOKIES", str(alt))
        args = build_parser().parse_args(["cookies", "inspect",
                                          "--root", str(tmp_path)])
        rc = run_command(args.fn, args)
        out = capsys.readouterr().out
        assert rc == 0
        payload = json.loads(out.splitlines()[-1])
        assert payload["cookies_path"] == str(alt)
