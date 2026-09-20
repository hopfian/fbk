"""Unit tests for the headless login state machine (auth/login.py).

All tests execute offline against a duck-typed transport stub that
queues canned responses (no network, no cookies, no governor). The
login/checkpoint pages are SYNTHETIC minimal HTML shapes built to the
decoded wire markers — the live flow is the operator's call (the
surface module's CALIBRATION NOTES say the same).

Covers: the lsd harvest + jazoest derivation, the credential POST body,
bad-credential rejection, the three 2FA variants (authenticator code,
SMS code, phone-notification approval with polling), the generic
continue-form replay, unrecognized-checkpoint conservatism, the step
cap, the auth-pair success verification, and the jar hand-off
(cookie_map → save_netscape → load_netscape round trip).

The stub models the LIVE jar transition: ``jar_frames`` lists a dict
applied after each wire call (the Set-Cookie the server "would" send),
so the auth pair appears exactly when the flow's checkpoint succeeds —
never one step early.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from auth.login import (
    LOGIN_POST_URL,
    BadCredentialsError,
    LoginError,
    LoginFlow,
    LoginTimeoutError,
    LoginUnrecognizedCheckpointError,
    derive_jazoest,
    parse_checkpoint,
)
from transport.cookies import load_netscape, save_netscape

LOGIN_PAGE = (
    '<!doctype html><html><head><script>require("LSD",[],'
    '{"token":"NAfLSDTEST:1:1789723298","async_get_token":"x"},1)'
    "</script></head><body><form action=\"/login.php\" method=\"post\">"
    "<input type=\"hidden\" name=\"jazoest\" value=\"2873\"></form></body></html>"
)

AUTHED = {"c_user": "12345678901234", "xs": "1:TEST"}


def checkpoint_page(kind: str) -> str:
    """Minimal synthetic checkpoint HTML for each variant."""
    if kind == "totp":
        return ("<form action=\"/checkpoint/block/\" method=\"post\">"
                "<input type=\"hidden\" name=\"lsd\" value=\"NAfLSD\">"
                "<input type=\"text\" name=\"approvals_code\" value=\"\">"
                "</form>Enter the code from your authentication app</p>")
    if kind == "sms":
        return ("<form action=\"/checkpoint/block/\" method=\"post\">"
                "<input type=\"hidden\" name=\"lsd\" value=\"NAfLSD\">"
                "<input type=\"text\" name=\"approvals_code\" value=\"\">"
                "</form>We sent a code via text message (SMS)</p>")
    if kind == "continue":
        return ("<form action=\"/checkpoint/continue/\" method=\"post\">"
                "<input type=\"hidden\" name=\"lsd\" value=\"NAfLSD\">"
                "<input type=\"hidden\" name=\"submit\" value=\"Continue\">"
                "</form>")
    if kind == "approval":
        return ("<p>Did you just log in? We sent a notification to your "
                "device. Approve the login from your phone.</p>")
    return "<p>Unrecognized integrity challenge page</p>"


def bad_credentials_page() -> str:
    return ("<p>The password that you entered is incorrect. "
            "Please try again.</p>")


def response(text: str, url: str = "https://www.facebook.com/") -> SimpleNamespace:
    return SimpleNamespace(status_code=200, text=text, url=url,
                           headers={}, content=text.encode())


class StubTransport:
    """Duck-typed transport for the login flow: queued canned responses.

    ``get``/``post`` pop one queued response (an Exception raises at pop
    time); ``calls`` records every (verb, url, data) so tests can pin the
    exact wire bodies — the credential POST is asserted on field NAMES
    and the lsd/jazoest derivation, never on a real secret.
    ``jar_frames`` models the live jar: after call N the Nth frame's
    key/values merge in (the Set-Cookie the server "would" set), so the
    auth pair lands exactly when a checkpoint succeeds.
    """

    def __init__(self, queue: list, jar_frames: list[dict] | None = None):
        self.queue = list(queue)
        self.jar: dict[str, str] = {}
        self.jar_frames = list(jar_frames or [])
        self.calls: list[tuple[str, str, dict[str, str] | None]] = []
        self._last: Any = None

    def _advance_jar(self) -> None:
        if self.jar_frames:
            self.jar.update(self.jar_frames.pop(0))

    def _next(self) -> Any:
        """Pop the queued response; past the queue, re-serve the last one."""
        item = self.queue.pop(0) if self.queue else self._last
        self._last = item
        return item

    def get(self, url: str, **kw):
        self.calls.append(("GET", url, None))
        item = self._next()
        self._advance_jar()
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url: str, *, data: dict[str, str], **kw):
        self.calls.append(("POST", url, dict(data)))
        item = self._next()
        self._advance_jar()
        if isinstance(item, Exception):
            raise item
        return item

    def cookie_value(self, name: str) -> str | None:
        return self.jar.get(name)

    def cookie_map(self) -> dict[str, str]:
        return dict(self.jar)


@pytest.fixture
def flow() -> tuple[LoginFlow, StubTransport]:
    """A flow over a fresh stub queued with: login page → credential POST."""
    transport = StubTransport([
        response(LOGIN_PAGE),                       # login page GET
        response(checkpoint_page("approval"),       # credentials POST lands
                 url="https://www.facebook.com/checkpoint/block/"),
    ])
    flow = LoginFlow(transport, identifier="user@example.com",
                     password="not-a-real-password",
                     log=lambda line: None)
    return flow, transport


class TestDerivations:
    """The CSRF pair harvest from the login page."""

    def test_jazoest_derivation(self):
        # docs/05 §4: '2' + the byte-sum complement of the protected token
        assert derive_jazoest("NAfLSD") == "2" + str(
            sum(ord(c) - 48 for c in "NAfLSD"))

    def test_lsd_extracted_from_the_require_frame(self, flow):
        _flow, transport = flow
        page = transport.get("x")
        lsd = LoginFlow._extract_lsd(page.text)
        assert lsd == "NAfLSDTEST:1:1789723298"

    def test_lsd_missing_raises_typed(self):
        with pytest.raises(LoginError, match="no lsd token"):
            LoginFlow._extract_lsd("<html></html>")


class TestCheckpointParsing:
    """The server-given-shape walker."""

    def test_totp_variant(self):
        page = parse_checkpoint(checkpoint_page("totp"))
        assert page.kind == "code-totp"
        assert page.action == "https://www.facebook.com/checkpoint/block/"
        assert page.fields["approvals_code"] == ""

    def test_sms_variant(self):
        page = parse_checkpoint(checkpoint_page("sms"))
        assert page.kind == "code-sms"
        assert "approvals_code" in page.fields

    def test_continue_variant(self):
        page = parse_checkpoint(checkpoint_page("continue"))
        assert page.kind == "continue"
        assert page.fields["submit"] == "Continue"

    def test_approval_variant_has_no_form(self):
        page = parse_checkpoint(checkpoint_page("approval"))
        assert page.kind == "approval"
        assert page.action == ""

    def test_unknown_variant(self):
        assert parse_checkpoint("<p>???</p>").kind == "unknown"


class TestCredentialRejection:
    """The wrong-password path never enters the checkpoint loop."""

    def test_bad_credentials_raise_typed(self):
        transport = StubTransport([
            response(LOGIN_PAGE),
            response(bad_credentials_page()),
        ])
        flow = LoginFlow(transport, identifier="user@example.com",
                         password="wrong", log=lambda line: None)
        with pytest.raises(BadCredentialsError, match="rejected"):
            flow.run(code_provider=lambda variant: "123456")
        # the credential POST carried the derived CSRF pair + the fields
        verb, url, data = transport.calls[1]
        assert verb == "POST" and LOGIN_POST_URL in url
        assert set(data) == {"email", "pass", "lsd", "jazoest",
                             "login_source", "persistent", "default"}
        assert data["lsd"] == "NAfLSDTEST:1:1789723298"
        assert data["jazoest"] == derive_jazoest("NAfLSDTEST:1:1789723298")
        assert data["login_source"] == "Comet_Dialog"


class TestTwoFactorVariants:
    """Each 2FA variant through its full resolution."""

    def test_totp_code_submits_and_logs_in(self):
        transport = StubTransport([
            response(LOGIN_PAGE),
            response(checkpoint_page("totp"),
                     url="https://www.facebook.com/checkpoint/block/"),
            response(checkpoint_page("totp")),  # code POST re-serves (stub)
        ], jar_frames=[{}, {}, AUTHED])
        flow = LoginFlow(transport, identifier="u", password="p",
                         log=lambda line: None)
        result = flow.run(code_provider=lambda variant: "123456")
        assert result.state == "logged_in"
        assert result.checkpoint == "code-totp"
        assert result.user_id == "12345678901234"
        verb, _url, data = transport.calls[2]
        assert verb == "POST"
        assert data["approvals_code"] == "123456"
        assert data["lsd"] == "NAfLSD"  # the server-given form replayed

    def test_sms_code_submits_and_logs_in(self):
        transport = StubTransport([
            response(LOGIN_PAGE),
            response(checkpoint_page("sms"),
                     url="https://www.facebook.com/checkpoint/block/"),
            response(checkpoint_page("sms")),
        ], jar_frames=[{}, {}, AUTHED])
        flow = LoginFlow(transport, identifier="u", password="p",
                         log=lambda line: None)
        result = flow.run(code_provider=lambda variant: "00000")
        assert result.state == "logged_in"
        assert result.checkpoint == "code-sms"

    def test_approval_flow_polls_until_accepted(self, monkeypatch):
        # poll pacing collapsed: no real sleeps in tests
        monkeypatch.setattr("auth.login.time.sleep", lambda _s: None)
        approval = response(checkpoint_page("approval"),
                            url="https://www.facebook.com/checkpoint/block/")
        transport = StubTransport([
            response(LOGIN_PAGE),
            approval,                                   # credentials POST
            approval,                                   # poll round 1
            response(checkpoint_page("continue")),      # poll round 2: approved
            response(checkpoint_page("continue")),      # continue POST
        ], jar_frames=[{}, {}, {}, {}, AUTHED])
        flow = LoginFlow(transport, identifier="u", password="p",
                         log=lambda line: None)
        result = flow.run(code_provider=lambda variant: "",
                          approval_wait_s=60.0, poll_interval_s=0.0)
        assert result.state == "logged_in"
        assert result.checkpoint == "approval"
        assert result.polls == 2
        # the polls GET the checkpoint page (never re-POST credentials)
        poll_verbs = [c[0] for c in transport.calls[2:4]]
        assert poll_verbs == ["GET", "GET"]

    def test_approval_timeout_raises_typed(self, monkeypatch):
        monkeypatch.setattr("auth.login.time.sleep", lambda _s: None)
        approval = response(checkpoint_page("approval"))
        transport = StubTransport([
            response(LOGIN_PAGE),
            approval,
            * [approval for _ in range(20)],  # the poll never transitions
        ])
        flow = LoginFlow(transport, identifier="u", password="p",
                         log=lambda line: None)
        with pytest.raises(LoginTimeoutError, match="not approved"):
            flow.run(code_provider=lambda v: "",
                     approval_wait_s=0.05, poll_interval_s=0.0)

    def test_continue_form_replays_verbatim(self):
        transport = StubTransport([
            response(LOGIN_PAGE),
            response(checkpoint_page("continue")),
            response(checkpoint_page("continue")),
        ], jar_frames=[{}, {}, AUTHED])
        flow = LoginFlow(transport, identifier="u", password="p",
                         log=lambda line: None)
        flow.run(code_provider=lambda v: "")
        verb, _url, data = transport.calls[2]
        assert verb == "POST"
        assert data == {"lsd": "NAfLSD", "submit": "Continue"}

    def test_unrecognized_checkpoint_is_conservative(self):
        transport = StubTransport([
            response(LOGIN_PAGE),
            response(checkpoint_page("unknown")),
        ])
        flow = LoginFlow(transport, identifier="u", password="p",
                         log=lambda line: None)
        with pytest.raises(LoginUnrecognizedCheckpointError,
                           match="unrecognized"):
            flow.run(code_provider=lambda v: "")

    def test_step_cap_terminates_the_loop(self):
        transport = StubTransport([
            response(LOGIN_PAGE),
            * [response(checkpoint_page("continue")) for _ in range(12)],
        ])
        flow = LoginFlow(transport, identifier="u", password="p",
                         log=lambda line: None)
        with pytest.raises(LoginError, match="did not resolve"):
            flow.run(code_provider=lambda v: "")
        # 1 login page + 1 credentials POST + 8 checkpoint steps = 10 calls
        assert len(transport.calls) == 10


class TestJarHandoff:
    """cookie_map → save_netscape → load_netscape round trip."""

    def test_save_and_load_round_trip(self, tmp_path):
        jar = {"datr": "DATRTOKEN", "c_user": "12345678901234",
               "xs": "1:TESTXS"}
        path = save_netscape(tmp_path / "cookies.txt", jar)
        loaded = load_netscape(path)
        assert loaded == jar

    def test_save_is_atomic_and_netscape_shaped(self, tmp_path):
        path = save_netscape(tmp_path / "cookies.txt",
                             {"c_user": "12345678901234"})
        text = path.read_text(encoding="utf-8")
        assert text.startswith("# Netscape HTTP Cookie File\n")
        assert ".facebook.com\tTRUE\t/\tTRUE\t0\tc_user\t" in text
        assert not (tmp_path / "cookies.txt.tmp").exists()
