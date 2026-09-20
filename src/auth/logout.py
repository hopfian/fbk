"""Server-side session teardown: the ``logout.php`` POST pair (docs/02 §2.12).

Session lifecycle never migrated to GraphQL (docs/05 §8): logout stays
a classic form POST to ``/logout.php`` signed with the harvested
``logout_hash``, the ``fb_dtsg`` CSRF token, and the derivable ``jazoest``
form checksum — exactly the field set the research record documents and
nothing beyond it (the full browser body was never captured: docs/13
§8.1 row 8 lists the logout-flow capture as a pending Day-0 probe, so no
field is invented here).

ARCHITECTURE:

  The service lives in the auth plane because logout IS a session-state
  transition (docs/03 §3): it needs no GraphQL registry and no surfaces
  dependency, only the Session facade. Flow:

  1. ``session.refresh()`` — a FULL fresh bootstrap. The persistent
     token cache (docs/16 §P11-1) does not round-trip ``logout_hash``
     (its disk shape is deliberately unchanged), so a cached entry can
     never authorize a teardown; logout always pays one homepage GET.
  2. :func:`build_logout_request` — the documented wire shape:
     ``POST constants.LOGOUT_ENDPOINT`` with body fields ``fb_dtsg``,
     ``h`` (the harvested ``logout_hash``), and ``jazoest``.
  3. Governed transport ``post()`` — pacing cannot be bypassed at the
     call-site level (docs/15 §P9-1). ``FBTransport.post`` carries no
     ``is_mutation`` flag (only ``post_graphql`` debits the mutation
     budget), so this single POST debits the read budget; a
     once-per-session teardown is volume-equivalent to one read, and
     the transport API is frozen scope — documented here as the
     deliberate choice rather than a private-API bypass.
  4. Success = a 3xx redirect: ``302 → /login`` is the canonical clean
     logout/expiry signal (docs/14 §4). On success the token cache is
     invalidated — the cached session tokens are dead post-logout; on
     any other response class the teardown is UNCONFIRMED and the cache
     is kept (the session may still be alive).

CALIBRATION NOTES — ground truth (wire shape per docs/, never live-fired):

  * ``/logout.php`` — POST with ``fb_dtsg`` + ``h``, the per-session
    hash param (docs/02 §2.12, CONFIRMED-PUBLIC). The ``h`` vs ``lh``
    spelling and the ``jazoest`` requirement carry the doc's own
    "(verify)" tags (docs/02 open-questions item 6); this module sends
    the documented name ``h`` and includes ``jazoest`` because docs/05
    §4 lists the logout form among the classic form posts that require
    it — a conformance marker classic endpoints reject when missing
    even with a valid ``fb_dtsg`` (docs/09 §4).
  * ``logout_hash`` harvest: ``"viewer":{"logout_hash":…}`` in the
    bootstrap page require-frames (docs/15 §2) — already wired through
    :class:`auth.bootstrap.Bootstrap`.
  * ``jazoest`` formula (docs/05 §4, identical in docs/09 §4):
    ``"2" + str(sum(ord(c) - 48 for c in fb_dtsg))`` — the docs' own
    worked example pins ``jazoest("AQHRN") == "2120"``.

SECURITY BOUNDARY:
  The form body carries ``logout_hash`` and ``fb_dtsg``; the journal
  sees only body field NAMES via ``FBTransport._note`` (docs/11 §7).
  The service result dict is secret-free by construction.

USER-DOC ANCHOR: cli/docs/03-session-and-auth.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import constants as C

from .bootstrap import Bootstrap

if TYPE_CHECKING:  # pragma: no cover - typing only
    from session import Session


def jazoest(fb_dtsg: str) -> str:
    """The classic-form checksum over ``fb_dtsg`` (docs/05 §4, docs/09 §4).

    Base-10 checksum where each character contributes ``ord(c) - 48``,
    prefixed with the constant ``"2"`` version digit. Not a security
    boundary — a conformance marker classic endpoints validate per
    request (docs/09 §4: bodies with a missing/inconsistent ``jazoest``
    are rejected even when ``fb_dtsg`` is valid).

    Args:
        fb_dtsg: The page-bootstrap CSRF token (the same string the
            form body carries in its ``fb_dtsg`` field).

    Returns:
        The checksum string, e.g. ``"2120"`` for ``"AQHRN"`` (the
        docs/05 §4 worked example).
    """
    return "2" + str(sum(ord(c) - 48 for c in fb_dtsg))


def build_logout_request(bootstrap: Bootstrap) -> tuple[str, dict[str, str]]:
    """Assemble the documented ``logout.php`` form POST (docs/02 §2.12).

    Body fields — exactly the documented triple, nothing invented
    (docs/13 §8.1: the full browser body was never captured):

      * ``fb_dtsg`` — the page CSRF token (docs/03 §4)
      * ``h`` — the harvested per-session ``logout_hash``
        (docs/15 §2: "needed for the logout.php POST pair")
      * ``jazoest`` — the derivable form checksum (docs/05 §4)

    Args:
        bootstrap: A FRESH page bootstrap (``Session.refresh()``) —
            the token cache does not round-trip ``logout_hash``, so a
            cache-served Bootstrap can never authorize a teardown.

    Returns:
        ``(url, form_fields)`` where ``url`` is
        :data:`constants.LOGOUT_ENDPOINT` and ``form_fields`` maps the
        three documented body names to their live values.

    Raises:
        NotLoggedInError: The bootstrap carries no ``fb_dtsg`` or no
            ``logout_hash`` — a page that does not authorize a logout
            is a page that is not logged in (docs/03 §3.1).
    """
    dtsg = bootstrap.dtsg()
    h = (bootstrap.logout_hash.get_secret_value()
         if bootstrap.logout_hash else "")
    if not dtsg or not h:
        # lazy import: graphql/__init__ drags the client/registry chain,
        # and every non-logout auth command must not pay for it
        from graphql.errors import NotLoggedInError
        raise NotLoggedInError(
            "bootstrap carries no logout authorization (fb_dtsg/logout_hash "
            "absent) — the session is not logged in (docs/03 §3.1)")
    return C.LOGOUT_ENDPOINT, {"fb_dtsg": dtsg, "h": h, "jazoest": jazoest(dtsg)}


class LogoutService:
    """Session-teardown service: one governed ``logout.php`` POST (docs/02 §2.12).

    The auth-plane twin of the surface services: state transition, not
    data retrieval, so it rides the transport's form POST directly —
    governor-paced, soft-block-observed, journaled with field names
    only (docs/15 §P9-1, docs/11 §7).

    Args:
        session: The authenticated operator session; supplies
            ``refresh()`` (the fresh bootstrap that carries
            ``logout_hash``), ``transport``, and ``token_cache``.
    """

    def __init__(self, session: Session):
        self.session = session

    def logout(self) -> dict[str, Any]:
        """Fire the documented logout POST and report the outcome.

        One fresh bootstrap (the token cache never round-trips
        ``logout_hash`` — docs/16 §P11-1 disk shape is unchanged), one
        governed POST with redirects disabled so the 3xx acknowledgement
        is observable (docs/14 §4: ``302 → /login`` is the clean
        logout/expiry signal), and — on confirmation — one token-cache
        invalidation, because every cached session token is dead the
        moment the teardown lands.

        Returns:
            A secret-free result dict: ``endpoint``, ``status``,
            ``location`` (the redirect target when present),
            ``logged_out`` (True iff the response was a 3xx),
            ``token_cache_invalidated``, and ``user_id``.

        Raises:
            NotLoggedInError: The fresh bootstrap carries no logout
                authorization (not logged in), or the transport raised
                it — both propagate unchanged per the typed-error
                contract (exit 3 via commands.common.run_command).
        """
        boot = self.session.refresh()
        url, data = build_logout_request(boot)
        # allow_redirects=False: the 3xx IS the documented confirmation
        # signal (docs/14 §4) — following it would spend a second
        # request to learn what the status line already said.
        resp = self.session.transport.post(url, data=data,
                                            allow_redirects=False)
        status = int(resp.status_code)
        logged_out = 300 <= status < 400
        invalidated = False
        if logged_out:
            self.session.token_cache.invalidate()
            invalidated = True
        return {
            "endpoint": url,
            "status": status,
            "location": (resp.headers.get("location")
                         if getattr(resp, "headers", None) else None),
            "logged_out": logged_out,
            "token_cache_invalidated": invalidated,
            "user_id": boot.user_id,
        }
