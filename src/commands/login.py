"""Login command: identifier + password + 2FA → a usable cookies.txt.

The only command that CREATES a session rather than consuming one:
drives ``auth.login.LoginFlow`` over a fresh governed transport (no jar
required — the login page GET absorbs ``datr``), prompts for the 2FA
step the edge serves (phone-notification approval with polling,
authenticator TOTP code, or SMS OTP), and persists the resulting jar to
``Config.cookies_path`` so every other command works immediately.

Exit-code contract (run_command): 0 success (or already logged in),
1 typed login failure (bad credentials, aborted/unrecognized checkpoint,
approval timeout), 2 unexpected errors. The password and 2FA codes are
read via getpass/stdin — never argv, never journaled, never emitted.

USER-DOC ANCHOR: cli/docs/03-session-and-auth.md — ship a matching edit
to that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from typing import Any

from auth.login import LoginError, LoginFlow
from commands.common import add_common_args, build_config, emit
from governor import default_governor
from journal.recorder import JSONLJournal
from token_cache import TokenCache
from transport.cookies import load_netscape, save_netscape
from transport.profile import load_or_default
from transport.session import FBTransport


def _already_authed(cfg: Any) -> tuple[bool, str | None]:
    """Whether the on-disk jar already carries the auth pair."""
    try:
        cookies = load_netscape(cfg.cookies_path)
    except Exception:
        return False, None
    if cookies.get("c_user") and cookies.get("xs"):
        return True, cookies.get("c_user")
    return False, None


def cmd_login(args: argparse.Namespace) -> int:
    """Drive the login flow; persist the jar; emit the outcome."""
    cfg = build_config(args)

    authed, existing_user = _already_authed(cfg)
    if authed and not getattr(args, "force", False):
        payload: dict[str, Any] = {
            "state": "already_logged_in",
            "user_id": existing_user,
            "jar": str(cfg.cookies_path),
        }

        def human_already() -> None:
            print(f"already logged in (user {existing_user or '?'}) — "
                  f"{cfg.cookies_path} carries the auth pair; "
                  "use --force to re-login")

        emit(args, payload, human=human_already)
        return 0

    identifier = getattr(args, "identifier", None) or input(
        "identifier (email / phone / username): ").strip()
    if not identifier:
        print("error: an identifier is required", file=sys.stderr)
        return 1
    if getattr(args, "password_stdin", False):
        print("password (stdin): ", file=sys.stderr, end="", flush=True)
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("password: ")
    if not password:
        print("error: a password is required", file=sys.stderr)
        return 1

    journal = (JSONLJournal(cfg.journal_file("login"))
               if not getattr(args, "no_journal", False) else None)
    transport = FBTransport(
        cookies={},  # fresh jar pre-login: the login-page GET absorbs datr
        profile=load_or_default(str(cfg.profile_path)
                                if cfg.profile_path else None),
        journal=journal,
        timeout=cfg.timeout,
        impersonate=cfg.impersonate,
        governor=default_governor())
    flow = LoginFlow(transport, identifier=identifier, password=password,
                     log=lambda line: print(f"[login] {line}", file=sys.stderr))

    upfront_code: str = getattr(args, "code", None) or ""

    def code_provider(variant: str) -> str:
        """The 2FA code: CLI-provided for scripting, else prompted."""
        if upfront_code:
            return upfront_code
        labels = {"code-totp": "authenticator app code",
                  "code-sms": "SMS code", "code": "verification code"}
        return getpass.getpass(f"2FA {labels.get(variant, 'code')}: ")

    try:
        result = flow.run(code_provider=code_provider,
                          approval_wait_s=getattr(args, "approval_wait", 180.0))
    except LoginError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    # capture the jar BEFORE closing (the live session jar is the source)
    jar = transport.cookie_map()
    transport.close()

    jar_path = save_netscape(cfg.cookies_path, jar)
    # a NEW jar invalidates any persisted token cache from the old session
    TokenCache(cfg.journal_dir / "token_cache.json").invalidate()

    payload = {
        "state": result.state,
        "user_id": result.user_id,
        "checkpoint": result.checkpoint,
        "steps": result.steps,
        "polls": result.polls,
        "saved_to": str(jar_path),
        "next": "fbk whoami",
    }

    def human() -> None:
        print(f"logged in — user {result.user_id or '?'} "
              f"(checkpoint: {result.checkpoint or 'none'}, "
              f"{result.steps} step(s), {result.polls} poll(s))")
        print(f"jar saved -> {jar_path}")
        print("verify with: fbk whoami")

    emit(args, payload, human=human)
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the login command onto the root parser (leaf — no children)."""
    p = sub.add_parser(
        "login",
        help="interactive login: identifier + password + 2FA (approval "
             "notification, authenticator code, or SMS OTP) -> saves "
             "cookies.txt")
    add_common_args(p)
    p.add_argument("identifier", nargs="?",
                   help="email, phone, or username (prompted when omitted)")
    p.add_argument("--password-stdin", action="store_true",
                   help="read the password from stdin (first line) instead "
                        "of the hidden prompt (for scripting)")
    p.add_argument("--code",
                   help="2FA code up front (TOTP/SMS) — skips the "
                        "interactive code prompt when the edge asks")
    p.add_argument("--approval-wait", type=float, default=180.0,
                   help="seconds to wait for the phone-notification "
                        "approval (default 180)")
    p.add_argument("--force", action="store_true",
                   help="re-login even when the jar already carries an "
                        "auth pair")
    p.set_defaults(fn=cmd_login)
