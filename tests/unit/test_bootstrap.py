"""Bootstrap extraction tests against the REAL captured logged-in page HTML
(assets/search_page_sample.html — a live 2026-09 search results page).

Unit (offline): the module-scoped ``logged_in_html`` fixture skips when the
capture is absent; classification edges use synthetic strings and a
hand-canned COOKIES dict. No network, no session.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import ASSETS

from auth.bootstrap import (
    classify_login_state,
    extract_bundles,
    extract_fb_dtsg,
    extract_lsd_all,
    extract_preload_registry,
    extract_revision,
    extract_user_id,
)
from auth.state import LoginState


@pytest.fixture(scope="module")
def logged_in_html() -> str:
    path = ASSETS / "search_page_sample.html"
    if not path.is_file():
        pytest.skip("search_page_sample.html fixture not captured yet")
    return path.read_text(encoding="utf-8", errors="replace")


COOKIES = {"c_user": "12345678901234", "xs": "x", "datr": "d"}


class TestExtraction:
    """Pins every extractor against the REAL captured search page: NAf dtsg
    shape, lsd frames, uid, revision, preloads, bundles."""

    def test_dtsg_shape(self, logged_in_html):
        dtsg = extract_fb_dtsg(logged_in_html)
        assert dtsg is not None
        assert dtsg.startswith("NAf")        # live 2026-09 prefix (doc 15 §2)
        assert ":1:" in dtsg                  # version + expiry suffix

    def test_lsd_multiple_frames(self, logged_in_html):
        tokens = extract_lsd_all(logged_in_html)
        assert len(tokens) >= 1               # one per SSR frame (P2-2)

    def test_user_id_matches_cuser(self, logged_in_html):
        assert extract_user_id(logged_in_html) == COOKIES["c_user"]

    def test_revision_present(self, logged_in_html):
        assert extract_revision(logged_in_html).isdigit()

    def test_preload_registry(self, logged_in_html):
        preloads = extract_preload_registry(logged_in_html)
        assert preloads, "logged-in pages always embed preloader registrations"
        entry = preloads[0]
        assert entry.doc_id.isdigit()
        assert entry.variables != {}

    def test_bundles(self, logged_in_html):
        urls = extract_bundles(logged_in_html)
        assert urls and all("rsrc.php" in u for u in urls)

    def test_classify_logged_in(self, logged_in_html):
        state, markers = classify_login_state(logged_in_html,
                                             "https://www.facebook.com/search/top", COOKIES)
        assert state is LoginState.LOGGED_IN
        assert "CurrentUserInitialData" in markers


class TestClassificationEdgeCases:
    """Pins state-machine precedence: logout markers, the authoritative
    is_checkpointed flag, uid mismatch, and the no-auth-cookie path."""

    def test_logout_by_markers(self):
        html = '<form id="login_form" name="login"></form>'
        state, _ = classify_login_state(html, "https://www.facebook.com/", COOKIES)
        assert state is LoginState.LOGGED_OUT

    def test_checkpoint_flag_beats_markers(self):
        html = "CurrentUserInitialData DTSGInitData" + '"is_checkpointed":true'
        state, _ = classify_login_state(html, "https://www.facebook.com/", COOKIES)
        assert state is LoginState.CHECKPOINT

    def test_bare_checkpoint_string_is_not_a_checkpoint(self):
        """'/checkpoint' appears benignly in URL routing tables (P2-2)."""
        html = 'CurrentUserInitialData DTSGInitData "/checkpoint/":1'
        state, _ = classify_login_state(html, "https://www.facebook.com/", COOKIES)
        assert state is LoginState.LOGGED_IN

    def test_uid_mismatch_is_logout(self):
        html = '"USER_ID":"99999999" CurrentUserInitialData DTSGInitData'
        state, markers = classify_login_state(html, "https://www.facebook.com/", COOKIES)
        assert state is LoginState.LOGGED_OUT
        assert "uid_mismatch" in markers

    def test_no_auth_cookies(self):
        state, _ = classify_login_state("whatever", "https://www.facebook.com/", {})
        assert state is LoginState.LOGGED_OUT
