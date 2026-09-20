"""auth.bootstrap coverage gaps (wave 2 polish targets).

test_bootstrap.py exercises the extractors against the REAL captured page;
what was missing is the integrated ``bootstrap_homepage()`` behavior and
the ``safe_dict()`` redaction contract:

  * malformed homepage: a 200-with-empty-body is the edge soft-block
    signature and must raise the TYPED error, never be parsed as a page;
  * happy path: a synthetic logged-in page harvested through a stub
    transport yields a fully populated Bootstrap;
  * account-warning markers are surfaced loudly in markers_seen;
  * safe_dict() redacts every token-shaped value to a fingerprint.

Unit (offline): pages are served through a local StubPageTransport
stand-in (in-memory canned HTML); no network, no real cookies.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import StubTransportResponse

from auth.bootstrap import Bootstrap, bootstrap_homepage
from auth.state import LoginState
from transport.cookies import fingerprint

COOKIES = {"c_user": "12345678901234", "xs": "x", "datr": "d"}
DTSG = "NAfTEST:1:1789723298"


def logged_in_html() -> str:
    """A synthetic logged-in homepage carrying every harvestable marker."""
    preload = {"actorID": "12345678901234",
               "preloaderID": "adp_FooQueryRelayPreloader_0123456789abcdef",
               "queryID": "12345", "variables": {"scale": 2},
               "queryName": "FooQuery"}
    return (
        '"DTSGInitData",[],{"token":"' + DTSG + '"}'
        '"LSD",[],{"token":"LSDONE"},{"LSD",[],{"token":"LSDTWO"}'
        '"CurrentUserInitialData",[],{"ACCOUNT_ID":"12345678901234",'
        '"USER_ID":"12345678901234","NAME":"Test User"'
        '"USER_ID":"12345678901234"'
        '"logout_hash":"LOGOUTHASH123"'
        '"server_revision":1047868043,'
        '//static.xx.fbcdn.net/rsrc.php/v3/yy/l/en_US/abc123.js?_nc_x=1'
        + json.dumps(preload, separators=(",", ":"))
    )


class StubPageTransport:
    """Transport stand-in: one canned homepage response, calls recorded."""

    def __init__(self, response):
        self.response = response
        self.calls: list[tuple] = []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return self.response


def page_transport(text: str = "", status: int = 200, url: str = "https://www.facebook.com/"):
    return StubPageTransport(StubTransportResponse(
        status_code=status, text=text, url=url))


# ------------------------------------------------------ malformed homepages
class TestMalformedHomepage:
    """Pins the empty-200 edge signature: the typed soft-block error, never
    a zero-byte page parsed as a session."""

    def test_empty_200_body_is_the_typed_soft_block_error(self):
        transport = page_transport(text="")  # 200 with zero bytes
        with pytest.raises(RuntimeError, match="empty body"):
            bootstrap_homepage(transport, COOKIES)

    def test_empty_200_error_names_the_soft_block_signature(self):
        transport = page_transport(text="")
        with pytest.raises(RuntimeError, match="soft-block"):
            bootstrap_homepage(transport, COOKIES)

    def test_fetches_exactly_the_homepage_with_redirects(self):
        transport = page_transport(text=logged_in_html())
        bootstrap_homepage(transport, COOKIES)
        assert len(transport.calls) == 1
        url, kw = transport.calls[0]
        assert url == "https://www.facebook.com/"
        assert kw.get("allow_redirects") is True


# ------------------------------------------------------------- happy paths
class TestBootstrapHomepageHarvest:
    """Pins the full harvest contract on a synthetic logged-in page: tokens,
    identity, bundles, preload registry, and redaction-safe secrets."""

    def test_logged_in_page_yields_a_fully_populated_bootstrap(self):
        transport = page_transport(text=logged_in_html())
        boot = bootstrap_homepage(transport, COOKIES)
        assert boot.state is LoginState.LOGGED_IN
        assert boot.dtsg() == DTSG
        assert boot.lsd_value() == "LSDONE"     # first frame wins
        assert boot.lsd_all == ["LSDONE", "LSDTWO"]
        assert boot.user_id == "12345678901234"
        assert boot.user_name == "Test User"
        assert boot.revision == "1047868043"
        assert boot.final_url == "https://www.facebook.com/"
        assert boot.html_len == len(logged_in_html())
        assert "CurrentUserInitialData" in boot.markers_seen

    def test_logout_hash_is_harvested_as_a_secret(self):
        transport = page_transport(text=logged_in_html())
        boot = bootstrap_homepage(transport, COOKIES)
        assert boot.logout_hash is not None
        assert "LOGOUTHASH123" not in str(boot.logout_hash)  # SecretStr shield
        assert "LOGOUTHASH123" not in repr(boot)

    def test_bundle_urls_are_absolutised_to_the_cdn(self):
        transport = page_transport(text=logged_in_html())
        boot = bootstrap_homepage(transport, COOKIES)
        assert boot.bundle_urls and all(u.startswith("https://")
                                        for u in boot.bundle_urls)
        assert any("rsrc.php" in u for u in boot.bundle_urls)

    def test_preload_registry_is_harvested(self):
        transport = page_transport(text=logged_in_html())
        boot = bootstrap_homepage(transport, COOKIES)
        assert len(boot.preloads) == 1
        assert boot.preloads[0].query_name == "FooQuery"
        assert boot.preloads[0].doc_id == "12345"

    def test_escaped_user_name_is_decoded(self):
        html = ('"CurrentUserInitialData",[],{"ACCOUNT_ID":"1","USER_ID":"1",'
                '"NAME":"S\\u00f8ren" DTSGInitData')
        boot = bootstrap_homepage(page_transport(text=html), COOKIES)
        assert boot.user_name == "Søren"

    def test_missing_tokens_yield_none_not_empty_strings(self):
        """A page without tokens must not produce truthy-empty secrets."""
        boot = bootstrap_homepage(
            page_transport(text='CurrentUserInitialData "USER_ID":"12345678901234"'),
            COOKIES)
        assert boot.dtsg() is None
        assert boot.lsd_value() is None
        assert boot.user_name is None


# -------------------------------------------------------- account warnings
class TestAccountWarningsSurface:
    """Pins the loud surfacing of account-warning markers in markers_seen."""

    def test_warning_marker_lands_in_markers_seen(self):
        html = logged_in_html() + "We suspect automated behaviour"
        boot = bootstrap_homepage(page_transport(text=html), COOKIES)
        assert "ACCOUNT_WARNING:We suspect automated behaviour" in boot.markers_seen

    def test_clean_page_carries_no_warning_markers(self):
        boot = bootstrap_homepage(page_transport(text=logged_in_html()), COOKIES)
        assert not [m for m in boot.markers_seen if m.startswith("ACCOUNT_WARNING:")]


# ------------------------------------------------------------ safe_dict view
class TestSafeDictRedaction:
    """Pins the safe_dict() journaling view: every token-shaped value
    becomes an HMAC fingerprint, never cleartext."""

    def _boot(self) -> Bootstrap:
        transport = page_transport(text=logged_in_html())
        return bootstrap_homepage(transport, COOKIES)

    def test_every_token_value_is_a_fingerprint_not_a_secret(self):
        safe = self._boot().safe_dict()
        assert safe["fb_dtsg"] == f"<redacted:{fingerprint(DTSG)}>"
        assert safe["lsd"] == f"<redacted:{fingerprint('LSDONE')}>"
        assert DTSG not in json.dumps(safe)
        assert "LSDONE" not in json.dumps(safe)
        assert "LOGOUTHASH123" not in json.dumps(safe)

    def test_safe_dict_reports_counts_and_state(self):
        safe = self._boot().safe_dict()
        assert safe["state"] == LoginState.LOGGED_IN.value
        assert safe["lsd_frame_count"] == 2
        assert safe["bundle_count"] == len(self._boot().bundle_urls)
        assert safe["preload_queries"] == ["FooQuery"]
        assert safe["html_len"] > 0
        assert safe["user_id"] == "12345678901234"

    def test_safe_dict_with_missing_tokens_reports_none(self):
        boot = Bootstrap(state=LoginState.LOGGED_IN)
        safe = boot.safe_dict()
        assert safe["fb_dtsg"] is None
        assert safe["lsd"] is None
        assert safe["logout_hash"] is None


# --------------------------------------------------- classification leftovers
class TestClassifyCookiesOnlyPath:
    """Pins the cookies_only classification fallback and the login-form
    markers' precedence over it."""

    def test_auth_cookies_with_no_markers_is_cookies_only(self):
        """Auth cookies + a markerless page -> LOGGED_IN via cookies_only."""
        from auth.bootstrap import classify_login_state
        state, markers = classify_login_state(
            "<html>nothing recognizable</html>",
            "https://www.facebook.com/", COOKIES)
        assert state is LoginState.LOGGED_IN
        assert markers == ["cookies_only"]

    def test_login_form_markers_beat_auth_cookies(self):
        """A served login form is LOGGED_OUT even with auth cookies present
        (the logout markers are checked before the cookie fallback)."""
        from auth.bootstrap import classify_login_state
        state, markers = classify_login_state(
            '<form id="login_form"></form>',
            "https://www.facebook.com/", COOKIES)
        assert state is LoginState.LOGGED_OUT
        assert 'id="login_form"' in markers
