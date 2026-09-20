"""Headless login state machine: identifier + password → 2FA → cookie jar.

Implements the full device-based web login flow the browser performs:
harvest the login page's CSRF pair (``lsd`` + derived ``jazoest``,
docs/05 §4), POST the credentials to ``login.php``, then drive whatever
checkpoint the edge serves back — the three 2FA variants (phone-
notification approval, authenticator-app TOTP, SMS OTP) plus generic
continue-style checkpoint forms — until the session jar carries the
``c_user``/``xs`` auth pair, which the caller persists to
``cookies.txt`` (``transport.cookies.save_netscape``).

CALIBRATION NOTES (2026-09-20):

  * The POST endpoint and form fields follow the classic device-based
    ``login.php`` wire shape (``email``, ``pass``, ``lsd``, ``jazoest``,
    ``login_source``, ``persistent``); the login page's LSD rides the
    standard require-frame shape (docs/03 §4's ``["LSD",[],{"token":…}]``).
  * Checkpoint variants VARY per deploy and per account risk state.
    Rather than hardcoding one shape, :func:`parse_checkpoint` walks the
    form the server actually served (action URL + hidden fields) and the
    flow replays it verbatim with the operator's code filled in — the
    same server-given-shape principle the SSR preload replay uses
    (docs/15 §P2-2). The variant labels (``code-totp`` / ``code-sms`` /
    ``approval`` / ``continue``) are heuristic classifications for the
    operator prompt, not wire enums.
  * NOT live-fired: like the events family before its first execution,
    the state machine is schema-decoded and unit-tested offline against
    synthetic checkpoint pages; the live login is the operator's call.

SECURITY BOUNDARY:
  The password and 2FA codes exist only as flow arguments in process
  memory; they are never logged, journalled, or printed. The transport's
  journal records body FIELD NAMES only (docs/12 §4); the emitted
  payloads carry identity metadata and checkpoint labels, never
  credentials.

USER-DOC ANCHOR: cli/docs/03-session-and-auth.md — ship a matching edit
to that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import html as _html
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# ── Wire endpoints (docs/03 §3 login shape; live-confirmed endpoint host) ────
LOGIN_PAGE_URL = "https://www.facebook.com/login/"
LOGIN_POST_URL = ("https://www.facebook.com/login/device-based/regular/"
                  "login.php?login_attempt=1&lwv=100")

#: The login page's LSD require-frame (docs/03 §4 shape).
_RE_LSD_FRAME = re.compile(r'"LSD",\[\],\{"token":"([^"]+)"')
#: Fallback: a form-hidden lsd input (shape varies on some logged-out pages).
_RE_LSD_HIDDEN = re.compile(
    r'<input[^>]*\bname="lsd"[^>]*\bvalue="([^"]*)"', re.I)

#: Checkpoint form walking: action + input tags (server-given shapes only).
_RE_FORM = re.compile(r'<form[^>]*\baction="([^"]*)"[^>]*>(.*?)</form>',
                      re.S | re.I)
_RE_INPUT = re.compile(r'<input\b[^>]*>', re.I)
_RE_ATTR = re.compile(r'\b(name|value|type)="([^"]*)"')

#: Approval-variant markers: the "did you log in?" phone-notification page.
_RE_APPROVAL = re.compile(
    r"check your (?:Facebook )?notifications|approve.{0,20}device|"
    r"did you (?:just )?log in|we sent a notification", re.I)
#: SMS-variant marker inside a code checkpoint.
_RE_SMS = re.compile(r"text(?:ed| message)|SMS", re.I)
#: TOTP-variant marker inside a code checkpoint.
_RE_TOTP = re.compile(r"authentication app|authenticator", re.I)

#: Hard step cap for the checkpoint loop (each step is one server-given
#: form replay or one approval poll round; the edge never chains deeper).
MAX_CHECKPOINT_STEPS = 8


def derive_jazoest(seed: str) -> str:
    """Derive ``jazoest`` — the CSRF byte-sum complement (docs/05 §4).

    jazoest = "2" + str(sum(ord(c) - 48 for c in seed)) where the seed is
    the token the form protects (``lsd`` on the logged-out login page, the
    same derivation ``fb_dtsg`` gets in-session). The leading "2" is the
    version prefix, constant across observed sessions.
    """
    return "2" + str(sum(ord(c) - 48 for c in seed))


class LoginError(Exception):
    """Base typed failure for the login flow (the command maps to exit 1).

    Attributes:
        detail: The redacted human summary (never credential-bearing).
    """


class BadCredentialsError(LoginError):
    """The edge rejected the identifier/password pair."""


class LoginUnrecognizedCheckpointError(LoginError):
    """The checkpoint served a shape this flow does not handle.

    Deliberately conservative: an unrecognized checkpoint is an integrity
    flow the operator should complete in a browser, not something to
    guess at headlessly.
    """


class LoginTimeoutError(LoginError):
    """The approval wait elapsed without the notification being accepted."""


@dataclass(frozen=True)
class CheckpointPage:
    """One parsed checkpoint page (the server-given shape).

    Attributes:
        kind: ``code-totp`` / ``code-sms`` / ``approval`` / ``continue`` /
            ``unknown`` — the operator-facing classification.
        action: The absolute form action URL to replay.
        fields: The form's fields as parsed (hidden inputs; unescaped).
    """

    kind: str
    action: str
    fields: dict[str, str] = field(default_factory=dict)


def _absolute(url: str, base: str = "https://www.facebook.com") -> str:
    """Normalize a form action to an absolute facebook.com URL."""
    if url.startswith("http"):
        return url
    if url.startswith("/"):
        return base + url
    return base + "/" + url


def parse_checkpoint(page_html: str) -> CheckpointPage:
    """Classify and walk the checkpoint form the server actually served.

    Args:
        page_html: The checkpoint page's HTML (the final response of the
            redirect chain that landed there).

    Returns:
        The parsed page: the first form carrying an ``approvals_code``
        input wins (the code variants); otherwise the first form with any
        fields is the generic continue-style replay target; a page with
        approval markers but no form is the phone-notification poll
        variant; anything else is ``unknown``.
    """
    forms = _RE_FORM.findall(page_html)
    code_form: tuple[str, dict[str, str]] | None = None
    generic_form: tuple[str, dict[str, str]] | None = None
    for action, body in forms:
        fields: dict[str, str] = {}
        for tag in _RE_INPUT.findall(body):
            attrs = dict(_RE_ATTR.findall(tag))
            name = attrs.get("name")
            if not name:
                continue
            fields[name] = _html.unescape(attrs.get("value", ""))
        if "approvals_code" in fields and code_form is None:
            code_form = (action, fields)
        if fields and generic_form is None:
            generic_form = (action, fields)
    if code_form is not None:
        action, fields = code_form
        variant = "code-sms" if _RE_SMS.search(page_html) else "code-totp"
        if _RE_TOTP.search(page_html) and not _RE_SMS.search(page_html):
            variant = "code-totp"
        return CheckpointPage(kind=variant, action=_absolute(action),
                              fields=fields)
    if _RE_APPROVAL.search(page_html):
        return CheckpointPage(kind="approval", action="")
    if generic_form is not None:
        action, fields = generic_form
        return CheckpointPage(kind="continue", action=_absolute(action),
                              fields=fields)
    return CheckpointPage(kind="unknown", action="")


@dataclass
class LoginResult:
    """The flow outcome (safe to emit: never credential-bearing)."""

    state: str                    # "logged_in" | "checkpoint_exhausted"
    user_id: str | None           # c_user once the jar carries it
    checkpoint: str | None        # the 2FA variant that resolved (or None)
    steps: int = 0                # checkpoint steps consumed
    polls: int = 0                # approval poll rounds performed


class LoginFlow:
    """The login state machine over one fingerprint-coherent transport.

    Drives: login-page token harvest → credential POST → checkpoint loop
    (code entry, approval polling, or continue replay — whatever the
    server serves) → auth-pair verification. Every wire call rides the
    transport's governed ``get``/``post`` (the request governor paces the
    whole flow; a login is ~3-8 paced requests, not a burst).
    """

    def __init__(self, transport: Any, *, identifier: str, password: str,
                 log: Callable[[str], None] | None = None):
        """Bind the flow to one transport and one credential pair.

        Args:
            transport: Cookie-authenticated transport (an empty jar is
                correct pre-login — the login-page GET absorbs ``datr``);
                supplies the governed ``get``/``post`` and the live
                session jar (``cookie_value``/``cookie_map``).
            identifier: Email, phone, or username.
            password: The account password (process memory only).
            log: Optional progress printer (stderr for the CLI); lines
                never carry credentials.
        """
        self.transport = transport
        self.identifier = identifier
        self.password = password
        self._log = log or (lambda _line: None)

    # ------------------------------------------------------------------ wire
    def _get(self, url: str, describe: str) -> Any:
        """One governed GET; the response object is returned as-is."""
        self._log(f"GET {describe}")
        return self.transport.get(url, allow_redirects=True)

    def _post(self, url: str, data: dict[str, str], describe: str) -> Any:
        """One governed POST (form-encoded); redirects followed."""
        self._log(f"POST {describe}")
        return self.transport.post(url, data=data, allow_redirects=True)

    def _authed(self) -> bool:
        """Whether the live session jar now carries the auth pair."""
        return bool(self.transport.cookie_value("c_user")
                    and self.transport.cookie_value("xs"))

    # ------------------------------------------------------------------ flow
    def run(self, *, code_provider: Callable[[str], str],
            approval_wait_s: float = 180.0,
            poll_interval_s: float = 5.0) -> LoginResult:
        """Execute the full login; returns the typed result.

        Args:
            code_provider: Called with the checkpoint variant label
                (``code-totp`` / ``code-sms``) when the edge asks for a
                2FA code; returns the operator's code (empty aborts).
            approval_wait_s: Total wall-clock budget for the phone-
                notification approval variant before
                :class:`LoginTimeoutError`.
            poll_interval_s: Base pause between approval polls (the
                governor's own inter-arrival gate stacks on top; polls
                are never tighter than human-paced).

        Returns:
            The :class:`LoginResult` (the caller verifies ``state`` and
            persists the jar).

        Raises:
            BadCredentialsError: The edge rejected the credential pair.
            LoginUnrecognizedCheckpointError: The checkpoint served an
                unrecognized shape.
            LoginTimeoutError: The approval wait elapsed.
            LoginError: Any other flow failure (step cap exhausted).
        """
        page = self._get(LOGIN_PAGE_URL, "login page")
        lsd = self._extract_lsd(page.text)
        jazoest = derive_jazoest(lsd)
        resp = self._post(LOGIN_POST_URL, {
            "email": self.identifier,
            "pass": self.password,
            "lsd": lsd,
            "jazoest": jazoest,
            "login_source": "Comet_Dialog",
            "persistent": "1",
            "default": "0",
        }, "credentials")
        if self._authed():
            return LoginResult(state="logged_in",
                               user_id=self.transport.cookie_value("c_user"),
                               checkpoint=None)
        self._reject_bad_credentials(resp.text)
        page_html, page_url = resp.text, str(getattr(resp, "url", ""))
        steps = 0
        polls = 0
        first_kind: str | None = None
        for step in range(MAX_CHECKPOINT_STEPS):
            steps = step + 1
            checkpoint = parse_checkpoint(page_html)
            # the reported variant is the FIRST 2FA flavor the edge asked
            # for — the operator-facing answer to "which method resolved
            # this login", not the final continue-style form
            first_kind = first_kind or checkpoint.kind
            if checkpoint.kind in ("code-totp", "code-sms", "code"):
                code = code_provider(checkpoint.kind)
                if not code:
                    raise LoginError("no 2FA code provided — aborted")
                resp = self._submit(checkpoint, {"approvals_code": code})
            elif checkpoint.kind == "continue":
                resp = self._submit(checkpoint, {})
            elif checkpoint.kind == "approval":
                resp, rounds = self._poll_approval(
                    page_url, checkpoint, approval_wait_s, poll_interval_s)
                polls += rounds
            else:
                raise LoginUnrecognizedCheckpointError(
                    "checkpoint served an unrecognized shape — complete "
                    "this login in a browser, then export the jar")
            if self._authed():
                return LoginResult(
                    state="logged_in",
                    user_id=self.transport.cookie_value("c_user"),
                    checkpoint=first_kind, steps=steps, polls=polls)
            page_html, page_url = resp.text, str(getattr(resp, "url", page_url))
            self._reject_bad_credentials(page_html)
        raise LoginError(
            f"checkpoint did not resolve within {MAX_CHECKPOINT_STEPS} steps")

    # ------------------------------------------------------------- internals
    @staticmethod
    def _extract_lsd(page_html: str) -> str:
        """The login page's lsd token (require-frame first, form fallback)."""
        match = _RE_LSD_FRAME.search(page_html) or _RE_LSD_HIDDEN.search(
            page_html)
        if not match:
            raise LoginError("login page carried no lsd token — page shape "
                             "changed; calibration needed (docs/03 §4)")
        return _html.unescape(match.group(1))

    @staticmethod
    def _reject_bad_credentials(page_html: str) -> None:
        """Raise :class:`BadCredentialsError` on the rejection markers."""
        lowered = page_html.lower()
        if ("incorrect" in lowered and "password" in lowered) or \
                "wrong credentials" in lowered or \
                "you entered an incorrect password" in lowered:
            raise BadCredentialsError(
                "the identifier/password pair was rejected — check the "
                "credentials and retry")

    def _submit(self, checkpoint: CheckpointPage,
                extra: dict[str, str]) -> Any:
        """Replay the server-given checkpoint form with the operator's input."""
        data = dict(checkpoint.fields)
        data.update(extra)
        return self._post(checkpoint.action, data,
                          f"checkpoint ({checkpoint.kind})")

    def _poll_approval(self, page_url: str, checkpoint: CheckpointPage,
                       approval_wait_s: float, poll_interval_s: float
                       ) -> tuple[Any, int]:
        """Poll the approval checkpoint until the notification is accepted.

        The phone-notification variant has NO form to fill: the page asks
        the operator to approve/deny on a signed-in device, and the same
        GET transitions once the decision lands. Polling is paced by the
        caller's interval PLUS the governor's own gate (never tighter than
        human pacing, docs/10 §7).

        Returns:
            ``(response, poll_rounds)`` — the transitioned response for
            the main loop to re-classify.

        Raises:
            LoginTimeoutError: The budget elapsed without approval.
        """
        self._log(f"approval required — accept the notification on a "
                  f"signed-in device (waiting up to {approval_wait_s:.0f}s)")
        deadline = time.monotonic() + approval_wait_s
        rounds = 0
        while time.monotonic() < deadline:
            time.sleep(poll_interval_s)
            rounds += 1
            resp = self._get(page_url or LOGIN_PAGE_URL,
                             "approval poll")
            if self._authed():
                return resp, rounds
            next_checkpoint = parse_checkpoint(resp.text)
            if next_checkpoint.kind != "approval":
                return resp, rounds  # the page transitioned — re-classify
            page_url = str(getattr(resp, "url", page_url))
        raise LoginTimeoutError(
            f"the notification was not approved within {approval_wait_s:.0f}s "
            "— approve it on a signed-in device and retry")
