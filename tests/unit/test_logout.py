"""Offline unit tests for the logout.php teardown (docs/02 §2.12, docs/05
§4/§8) — request construction, service flow, token-cache invalidation,
typed-error propagation, and the ``fbk logout`` command wiring.

Everything runs against purpose-built stubs (no network, no cookies):

  * ``jazoest`` is pinned against the docs/05 §4 worked example
    (``"AQHRN" -> "2120"``) plus an independent inline recomputation.
  * the request builder asserts the EXACT documented field triple
    (``fb_dtsg`` + ``h`` + ``jazoest``) — the full browser body was
    never captured (docs/13 §8.1 row 8), so no other field may appear.
  * the service drives a recording transport stub; the token cache is
    the REAL TokenCache on tmp_path, so invalidation is observable as
    file deletion.
  * the command test drives the REAL app.build_parser() with
    ``commands.auth.new_session`` monkeypatched — the house seam.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fakes import StubTransportResponse

import constants as C
from app import build_parser
from auth.bootstrap import Bootstrap
from auth.logout import LogoutService, build_logout_request, jazoest
from auth.state import LoginState
from graphql.errors import NotLoggedInError, RateLimitedError
from token_cache import TokenCache

UID = "12345678901234"
DTSG = "NAfTEST:1:1789723298"
LOGOUT_HASH = "AQHRTESTHASH0123456789"
LOGIN_URL = "https://www.facebook.com/login/"
# The docs/05 §4 worked example evaluates its own sum expression to
# (65-48)+(81-48)+(72-48)+(82-48)+(78-48) = 138 — the doc comment's
# "2120" is an arithmetic slip; the formula (docs/05 §4, docs/09 §4,
# identical in both) is the ground truth this pins.
DOCS_EXAMPLE = ("AQHRN", "2138")


def craft_logout_bootstrap(*, logout_hash: str | None = LOGOUT_HASH,
                           fb_dtsg: str | None = DTSG) -> Bootstrap:
    """A logged-in bootstrap carrying the logout POST pair (docs/15 §2)."""
    return Bootstrap(
        state=LoginState.LOGGED_IN,
        fb_dtsg=fb_dtsg,
        lsd="TESTLSD00000000000",
        user_id=UID,
        user_name="Test User",
        revision="1047868043",
        logout_hash=logout_hash,
        preloads=[],
    )


def seeded_token_cache(tmp_path: Path) -> tuple[TokenCache, Path]:
    """A real TokenCache with a live on-disk entry; returns (cache, path)."""
    cache = TokenCache(tmp_path / "token_cache.json")
    cache.save(SimpleNamespace(
        fb_dtsg=SecretStr(DTSG),
        lsd=SecretStr("TESTLSD"),
        user_id=UID,
        user_name="Test User",
        revision="1047868043",
        state=LoginState.LOGGED_IN,
        dtsg=lambda: DTSG,
        lsd_value=lambda: "TESTLSD",
    ))
    assert cache.path.is_file()
    return cache, cache.path


class StubLogoutTransport:
    """Recording form-POST stub: captures the wire request, serves canned
    responses, or raises a queued exception at call time."""

    def __init__(self, response: StubTransportResponse | None = None,
                 error: Exception | None = None):
        self.response = response or StubTransportResponse(
            status_code=302, headers={"location": LOGIN_URL})
        self.error = error
        self.posts: list[dict] = []

    def post(self, url: str, *, data=None, headers=None, **kw):
        self.posts.append({"url": url, "data": dict(data or {}), "kw": kw})
        if self.error is not None:
            raise self.error
        return self.response


class StubLogoutSession:
    """Session look-alike for the auth-plane logout service: refresh() yields
    the crafted bootstrap; transport and token_cache are injectable."""

    def __init__(self, boot: Bootstrap, transport: StubLogoutTransport,
                 token_cache: TokenCache):
        self._boot = boot
        self.transport = transport
        self.token_cache = token_cache

    def refresh(self) -> Bootstrap:
        return self._boot

    def close(self) -> None:
        pass


# ------------------------------------------------------------------- jazoest
class TestJazoest:
    """Pins the docs/05 §4 / docs/09 §4 checksum formula exactly."""

    def test_docs_worked_example(self):
        assert jazoest(DOCS_EXAMPLE[0]) == DOCS_EXAMPLE[1]

    def test_synthetic_dtsg_against_independent_recomputation(self):
        # independent inline recomputation — not a call into the SUT
        expected = "2" + str(sum(ord(c) - 48 for c in DTSG))
        assert jazoest(DTSG) == expected

    def test_empty_token_yields_version_digit_plus_zero_sum(self):
        # sum() over nothing is 0, so the checksum is "2" + "0"
        assert jazoest("") == "20"

    def test_char_offset_is_minus_48(self):
        # "0" contributes 0 — the ASCII-digit anchor of the formula
        assert jazoest("0") == "20"


# -------------------------------------------------------- request construction
class TestBuildLogoutRequest:
    """Pins the documented wire shape: constants.LOGOUT_ENDPOINT + exactly
    the fb_dtsg / h / jazoest triple (docs/02 §2.12, docs/05 §4/§8)."""

    def test_url_comes_from_constants(self):
        url, _ = build_logout_request(craft_logout_bootstrap())
        assert url == C.LOGOUT_ENDPOINT
        assert url == "https://www.facebook.com/logout.php"

    def test_body_is_exactly_the_documented_triple(self):
        _, data = build_logout_request(craft_logout_bootstrap())
        assert set(data) == {"fb_dtsg", "h", "jazoest"}
        assert data["fb_dtsg"] == DTSG
        assert data["h"] == LOGOUT_HASH
        assert data["jazoest"] == "2" + str(sum(ord(c) - 48 for c in DTSG))

    def test_h_carries_the_harvested_logout_hash(self):
        _, data = build_logout_request(craft_logout_bootstrap(logout_hash="OTHERHASH"))
        assert data["h"] == "OTHERHASH"

    def test_missing_logout_hash_is_not_logged_in(self):
        with pytest.raises(NotLoggedInError):
            build_logout_request(craft_logout_bootstrap(logout_hash=None))

    def test_missing_dtsg_is_not_logged_in(self):
        with pytest.raises(NotLoggedInError):
            build_logout_request(craft_logout_bootstrap(fb_dtsg=None))

    def test_empty_string_tokens_fail_closed(self):
        # unwrap-then-check discipline: empty must surface as a refusal,
        # never as a live empty-token entry on the wire
        with pytest.raises(NotLoggedInError):
            build_logout_request(craft_logout_bootstrap(logout_hash=""))


# ------------------------------------------------------------- service flow
class TestLogoutService:
    """Service behavior against the recording transport stub: one governed
    POST, confirmation semantics (docs/14 §4), cache lifecycle."""

    def _service(self, tmp_path, transport):
        cache, path = seeded_token_cache(tmp_path)
        session = StubLogoutSession(craft_logout_bootstrap(), transport, cache)
        return LogoutService(session), transport, path

    def test_success_posts_the_documented_shape_once(self, tmp_path):
        service, transport, _ = self._service(tmp_path, StubLogoutTransport())
        result = service.logout()
        assert len(transport.posts) == 1
        post = transport.posts[0]
        assert post["url"] == C.LOGOUT_ENDPOINT
        assert set(post["data"]) == {"fb_dtsg", "h", "jazoest"}
        assert post["data"]["h"] == LOGOUT_HASH
        # the 3xx must be observed, not followed — following it would
        # spend a second request to learn what the status line said
        assert post["kw"].get("allow_redirects") is False
        assert result["logged_out"] is True

    def test_success_is_the_3xx_class_and_invalidates_cache(self, tmp_path):
        service, _, cache_path = self._service(
            tmp_path, StubLogoutTransport(
                StubTransportResponse(status_code=302,
                                      headers={"location": LOGIN_URL})))
        result = service.logout()
        assert result["logged_out"] is True
        assert result["status"] == 302
        assert result["location"] == LOGIN_URL
        assert result["token_cache_invalidated"] is True
        assert result["user_id"] == UID
        assert result["endpoint"] == C.LOGOUT_ENDPOINT
        assert not cache_path.is_file()  # tokens are dead post-logout

    def test_non_redirect_response_is_unconfirmed_cache_kept(self, tmp_path):
        # docs/14 §4: the 200-with-HTML trap — a non-3xx body can be
        # anything; without a captured response shape the teardown is
        # UNCONFIRMED and the cache must survive
        service, _, cache_path = self._service(
            tmp_path, StubLogoutTransport(StubTransportResponse(status_code=200)))
        result = service.logout()
        assert result["logged_out"] is False
        assert result["token_cache_invalidated"] is False
        assert cache_path.is_file()

    def test_typed_errors_propagate_unchanged(self, tmp_path):
        for error in (RateLimitedError("soft block"), NotLoggedInError("dead xs")):
            service, _, _ = self._service(
                tmp_path, StubLogoutTransport(error=error))
            with pytest.raises(type(error)):
                service.logout()


# ----------------------------------------------------------- command wiring
class TestLogoutCommand:
    """Drives the REAL app.build_parser(): ``logout`` registers, --help
    renders, the handler runs against a stubbed session seam, and the
    state sibling stays intact."""

    def test_logout_subcommand_parses_with_callable_handler(self):
        args = build_parser().parse_args(["logout"])
        assert args.command == "logout"
        assert callable(args.fn)

    def test_logout_help_renders(self, capsys):
        with pytest.raises(SystemExit) as ei:
            build_parser().parse_args(["logout", "--help"])
        assert ei.value.code == 0
        assert "logout" in capsys.readouterr().out

    def test_state_help_still_renders(self, capsys):
        with pytest.raises(SystemExit) as ei:
            build_parser().parse_args(["state", "--help"])
        assert ei.value.code == 0
        assert "state" in capsys.readouterr().out

    def _run(self, monkeypatch, capsys, tmp_path, transport):
        import commands.auth as auth_cmd
        cache, _ = seeded_token_cache(tmp_path)
        stub = StubLogoutSession(craft_logout_bootstrap(), transport, cache)
        monkeypatch.setattr(auth_cmd, "new_session", lambda _a: stub)
        args = build_parser().parse_args(["logout", "--json"])
        rc = args.fn(args)
        out = capsys.readouterr().out
        return rc, out

    def test_run_success_exits_zero_and_invalidates_cache(
            self, monkeypatch, capsys, tmp_path):
        import json

        transport = StubLogoutTransport(
            StubTransportResponse(status_code=302,
                                  headers={"location": LOGIN_URL}))
        rc, out = self._run(monkeypatch, capsys, tmp_path, transport)
        assert rc == 0
        payload = json.loads(out)  # --json mode: machine output only
        assert payload["logged_out"] is True
        assert payload["token_cache_invalidated"] is True
        assert payload["status"] == 302

    def test_run_unconfirmed_exits_one_keeps_cache(
            self, monkeypatch, capsys, tmp_path):
        transport = StubLogoutTransport(StubTransportResponse(status_code=200))
        rc, _ = self._run(monkeypatch, capsys, tmp_path, transport)
        assert rc == 1

    def test_run_not_logged_in_propagates_to_exit_three(
            self, monkeypatch, capsys, tmp_path):
        # a bootstrap without logout authorization raises the typed
        # error THROUGH the handler — run_command maps it to exit 3
        import commands.auth as auth_cmd
        from commands.common import run_command

        cache, _ = seeded_token_cache(tmp_path)
        stub = StubLogoutSession(craft_logout_bootstrap(logout_hash=None),
                                 StubLogoutTransport(), cache)
        monkeypatch.setattr(auth_cmd, "new_session", lambda _a: stub)
        args = build_parser().parse_args(["logout", "--json"])
        rc = run_command(args.fn, args)
        assert rc == 3
        assert "error:" in capsys.readouterr().err
