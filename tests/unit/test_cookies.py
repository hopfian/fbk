"""Cookie hygiene tests: redaction, fingerprints, Netscape loading.

Unit (offline): jar parsing against the tmp_path ``netscape_file``
fixture plus the project's real cli/cookies.txt (via ``fakes.ASSETS``);
redaction checks are pure dict walking. No network, no session.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from constants import SECRET_COOKIE_NAMES
from transport.cookies import (
    CookieLoadError,
    describe,
    fingerprint,
    load_netscape,
    redact,
)


@pytest.fixture()
def netscape_file(tmp_path: Path) -> Path:
    p = tmp_path / "cookies.txt"
    p.write_text(
        "# Netscape HTTP Cookie File\n"
        "# https://curl.haxx.se/rfc/cookie_spec.html\n"
        "# This is a generated file! Do not edit.\n"
        "\n"
        ".facebook.com\tTRUE\t/\tTRUE\t1999999999\tc_user\t1234567890\n"
        ".facebook.com\tTRUE\t/\tTRUE\t1999999999\txs\t7:AbCdEfSecret\n"
        ".facebook.com\tTRUE\t/\tTRUE\t1999999999\tdatr\tDeviceTokenValue1\n"
        ".facebook.com\tTRUE\t/\tFALSE\t1999999999\tdpr\t1\n"
        ".example.com\tTRUE\t/\tTRUE\t1999999999\tintruder\tnot-facebook\n",
        encoding="utf-8")
    return p


class TestLoadNetscape:
    """Pins Netscape parsing: facebook-scoped rows only, and the typed
    CookieLoadError on a missing jar."""

    def test_loads_facebook_scoped_only(self, netscape_file):
        cookies = load_netscape(netscape_file)
        assert set(cookies) == {"c_user", "xs", "datr", "dpr"}
        assert cookies["xs"] == "7:AbCdEfSecret"

    def test_missing_file_raises_typed(self, tmp_path):
        with pytest.raises(CookieLoadError):
            load_netscape(tmp_path / "nope.txt")

    def test_auth_pair_loads(self, tmp_path):
        """A Netscape jar with the auth pair parses and carries both
        cookies (a synthetic jar — the offline suite must never depend
        on the operator's real cookies.txt)."""
        jar = tmp_path / "cookies.txt"
        jar.write_text(
            "# Netscape HTTP Cookie File\n"
            ".facebook.com\tTRUE\t/\tTRUE\t0\tc_user\t123456\n"
            ".facebook.com\tTRUE\t/\tTRUE\t0\txs\tsynthetic-xs\n",
            encoding="utf-8")
        cookies = load_netscape(jar)
        assert "c_user" in cookies and "xs" in cookies


class TestHygiene:
    """Pins the disclosure boundary: redact(), fingerprint() and
    describe() never emit cookie values."""

    def test_redact_scrubs_secret_cookie_names(self):
        entry = {"url": "https://x", "xs": "SECRETXS", "datr": "SECRETDATR",
                 "nested": [{"c_user": "1", "ok": "fine"}], "plain": "visible"}
        out = redact(entry, SECRET_COOKIE_NAMES)
        assert "SECRET" not in str(out)
        assert out["plain"] == "visible"
        assert out["nested"][0]["ok"] == "fine"
        assert out["xs"].startswith("<redacted:")

    def test_fingerprint_is_stable_and_short(self):
        assert fingerprint("abc") == fingerprint("abc")
        assert fingerprint("abc") != fingerprint("abd")
        assert len(fingerprint("abc")) == 12

    def test_describe_never_leaks_values(self, netscape_file):
        cookies = load_netscape(netscape_file)
        lines = "\n".join(describe(cookies))
        assert "7:AbCdEfSecret" not in lines
        assert "c_user: present" in lines
