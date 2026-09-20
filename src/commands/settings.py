"""Settings commands: show, set-default-privacy, verify-password,
send-reset-link (docs/02 §2.12 account/settings surface; docs/15 §P3
mutation ground truth).

The settings surface is deliberately sensitive (docs/11 §4): password
reauth and reset-link mutations carry stronger challenges than content
surfaces, and grazing it incidentally is itself a risk signal — touch
it only when the operation demands it.

Password handling discipline: the current password is prompted via getpass
(never echoed), never journalled (the journal records field names only),
and never emitted — verify-password output is scrubbed against the
secret before emit (see _scrub_secret).

USER-DOC ANCHOR: cli/docs/06-reference-people.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse
import getpass
from typing import Any

from domain.common import Privacy
from surfaces.settings import (
    PASSWORD_REAUTH_MUTATION,
    PRIVACY_SAVE_MUTATION,
    SEND_RESET_LINK_MUTATION,
    SettingsService,
)

from .common import add_common_args, emit, new_session, with_session


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the settings family onto the root parser: show,
    set-default-privacy, verify-password, send-reset-link."""
    settings = sub.add_parser("settings", help="account-settings surface")
    ssub = settings.add_subparsers(dest="settings_cmd", required=True)

    show = ssub.add_parser("show", help="replay the settings queries, "
                           "print the trimmed payloads")
    add_common_args(show)
    show.set_defaults(fn=cmd_settings_show)

    priv = ssub.add_parser("set-default-privacy",
                           help="save the default post privacy")
    add_common_args(priv)
    priv.add_argument("--privacy", required=True,
                      choices=("public", "friends", "private"),
                      help="audience for new posts")
    priv.set_defaults(fn=cmd_set_default_privacy)

    verify = ssub.add_parser("verify-password",
                             help="verify the current password "
                             "(reauthentication mutation)")
    add_common_args(verify)
    verify.add_argument("--password", default=None,
                        help="current password (prompted securely if absent)")
    verify.set_defaults(fn=cmd_verify_password)

    reset = ssub.add_parser("send-reset-link",
                            help="email a password-reset link to the viewer")
    add_common_args(reset)
    reset.set_defaults(fn=cmd_send_reset_link)


def _scrub_secret(node: Any, secret: str) -> Any:
    """Recursively replace any string equal to `secret` with "<redacted>".

    Defense in depth against the reauthentication response echoing the
    submitted password back: getpass keeps it off the terminal, the
    journal keeps it off disk (field names only), and this scrub keeps
    it off stdout even if the server reflects it in the payload.
    """
    if isinstance(node, dict):
        return {k: _scrub_secret(v, secret) for k, v in node.items()}
    if isinstance(node, list):
        return [_scrub_secret(v, secret) for v in node]
    if isinstance(node, str) and secret and node == secret:
        return "<redacted>"
    return node


def cmd_settings_show(args: argparse.Namespace) -> int:
    """Replay the settings queries and print trimmed payloads (docs/02
    §2.12 — settings HTML's GraphQL underlay).

    Only the top-level key sets print in human mode; the full decoded
    payloads ride in --json for template inspection.

    Returns:
        0 — settings shape is never a failure condition.
    """
    with with_session(new_session(args)) as session:
        shown = SettingsService(session).show()
        emit(args, {"settings": shown},
             human=lambda: print(f"root keys      : {sorted(shown['root'])}\n"
                                 f"categories keys: {sorted(shown['categories'])}"))
        return 0


def cmd_set_default_privacy(args: argparse.Namespace) -> int:
    """Save the default post-privacy audience (the privacy-save mutation,
    docs/15 §P3).

    Returns:
        0 — in-band echo; the next composer publish will pick up the
        new base_state server-side.
    """
    with with_session(new_session(args)) as session:
        service = SettingsService(session)
        privacy = Privacy.from_name(args.privacy)
        response = service.set_default_privacy(privacy)
        emit(args, {
            "mutation": PRIVACY_SAVE_MUTATION,
            "base_state": privacy.value,
            "response": response,
        }, human=lambda: print(f"default privacy -> {privacy.value}"))
        return 0


def cmd_verify_password(args: argparse.Namespace) -> int:
    """Verify the current password via the reauthentication mutation
    (comet_password_reauthentication, docs/02 §2.12).

    ``--password`` skips the getpass prompt for scripting; the secret
    is scrubbed from the emitted payload either way
    (_scrub_secret — the journal already records field names only).

    Returns:
        0 when reauth_is_successful; 1 otherwise — a failed
        verification is a failed precondition, not an exception.
    """
    with with_session(new_session(args)) as session:
        service = SettingsService(session)
        password = args.password
        if password is None:
            password = getpass.getpass("Current password: ")
        response = service.verify_password(password)
        node = ((response.get("data") or {})
                .get("comet_password_reauthentication") or {})
        verified = bool(node.get("reauth_is_successful"))
        payload = _scrub_secret({
            "mutation": PASSWORD_REAUTH_MUTATION,
            "verified": verified,
            "response": response,
        }, password)
        emit(args, payload,
             human=lambda: print(f"password verified: {verified}"))
        return 0 if verified else 1


def cmd_send_reset_link(args: argparse.Namespace) -> int:
    """Email a password-reset link to the viewer
    (xfb_send_password_reset_link_for_viewer, docs/02 §2.12).

    A high-impact mutation: the reset email lands in the account's
    inbox immediately — never wire this into automation loops.

    Returns:
        0 when the response carries success; 1 otherwise — an
        unconfirmed send is a failed precondition.
    """
    with with_session(new_session(args)) as session:
        service = SettingsService(session)
        response = service.send_reset_link()
        node = ((response.get("data") or {})
                .get("xfb_send_password_reset_link_for_viewer") or {})
        sent = bool(node.get("success"))
        payload = {
            "mutation": SEND_RESET_LINK_MUTATION,
            "sent": sent,
        }
        emit(args, payload,
             human=lambda: print(f"reset link sent: {sent}"))
        return 0 if sent else 1
