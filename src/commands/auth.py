"""Session commands: whoami, state, logout — the auth state machine
(docs/03 §3) plus its one state-changing transition.

``whoami`` doubles as the canary (docs/12 §9): one harmless bootstrap +
identity read that proves jar → transport → bootstrap → token harvest
before any real run fires. Both read commands classify the homepage
bootstrap into the LoginState machine and exit 1 when the session is
anything but LOGGED_IN — a failed precondition reported to the
operator, not an error condition.

``logout`` fires the documented ``logout.php`` POST pair (docs/02
§2.12, docs/05 §8): session teardown is the one auth transition that
never migrated to GraphQL, so it rides the legacy form surface — fresh
bootstrap (token cache never round-trips the ``logout_hash``), one
governed POST, token-cache invalidation on confirmation (docs/16
§P11-1).

USER-DOC ANCHOR: cli/docs/03-session-and-auth.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse

from auth.logout import LogoutService
from auth.state import LoginState

from .common import add_common_args, emit, new_session, with_session


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the whoami/state/logout (sub)commands onto the root parser."""
    who = sub.add_parser("whoami", help="validate the session, print identity + tokens")
    add_common_args(who)
    who.set_defaults(fn=cmd_whoami)

    state = sub.add_parser("state", help="login-state machine classification only")
    add_common_args(state)
    state.set_defaults(fn=cmd_state)

    logout = sub.add_parser("logout",
                            help="server-side session teardown (logout.php)")
    add_common_args(logout)
    logout.set_defaults(fn=cmd_logout)


def cmd_whoami(args: argparse.Namespace) -> int:
    """Bootstrap the session and print identity + token availability.

    The canary read (docs/12 §9): one pass proves the whole session
    pipeline. Token values never print — ``safe_dict()`` emits only
    HMAC fingerprints per the journal redaction policy (docs/11 §7).

    Returns:
        0 when LoginState.LOGGED_IN; 1 otherwise (the jar needs
        re-auth — a failed precondition, not an exception).
    """
    with with_session(new_session(args)) as session:
        boot = session.bootstrap()
        payload = {"whoami": boot.safe_dict(), "cookies": sorted(session.cookies)}

        def human() -> None:
            print(f"state    : {boot.state.value}")
            print(f"user     : {boot.user_name} ({boot.user_id})")
            print(f"revision : {boot.revision}")
            print(f"dtsg     : {'harvested' if boot.fb_dtsg else 'MISSING'}")
            print(f"preloads : {len(boot.preloads)} queries")

        emit(args, payload, human=human)
        return 0 if boot.state is LoginState.LOGGED_IN else 1


def cmd_state(args: argparse.Namespace) -> int:
    """Print only the login-state classification and the markers seen.

    The cheap variant of whoami: same single bootstrap, no identity
    decoding — for scripts that gate on session health alone.

    Returns:
        0 when LoginState.LOGGED_IN; 1 otherwise.
    """
    with with_session(new_session(args)) as session:
        boot = session.bootstrap()
        emit(args, {"state": boot.state.value, "markers": boot.markers_seen})
        return 0 if boot.state is LoginState.LOGGED_IN else 1


def cmd_logout(args: argparse.Namespace) -> int:
    """Fire the ``logout.php`` teardown POST and report the outcome.

    The one state-changing auth verb: a fresh bootstrap (the token
    cache never round-trips ``logout_hash``), then the documented form
    POST — ``fb_dtsg`` + ``h`` + ``jazoest``, exactly the docs/02 §2.12
    triple — through the governed transport. Confirmation is the 3xx
    redirect class (docs/14 §4: ``302 → /login`` is the clean
    logout/expiry signal); anything else is an unconfirmed teardown.

    Returns:
        0 when the server confirmed the teardown (3xx) and the token
        cache was invalidated; 1 otherwise — an unconfirmed mutation is
        a failed precondition, same contract as every other write
        command (docs/12 §7).

    Raises:
        NotLoggedInError: The bootstrap carries no logout authorization
            (maps to exit 3 via run_command — re-auth or ignore; the
            session was already unusable).
    """
    with with_session(new_session(args)) as session:
        result = LogoutService(session).logout()

        def human() -> None:
            tokens = ("cache invalidated" if result["token_cache_invalidated"]
                      else "cache kept")
            print(f"endpoint : {result['endpoint']}")
            print(f"status   : {result['status']}"
                  + (f" -> {result['location']}" if result["location"] else ""))
            print(f"state    : "
                  f"{'logged out' if result['logged_out'] else 'UNCONFIRMED'}")
            print(f"tokens   : {tokens}")

        emit(args, result, human=human)
        return 0 if result["logged_out"] else 1
