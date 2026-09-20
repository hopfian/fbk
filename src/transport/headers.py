"""Canonical header sets (docs/09 §1-2).

curl_cffi impersonation supplies the TLS/h2 fingerprint and the browser's
own header order, so only the FB-specific informational headers are layered
on top — duplicating sec-ch-* or accept-encoding would break the impersonated
shape and is forbidden.

ARCHITECTURE:

  Layering Discipline (docs/09 §8):
    The impersonation preset emits the browser-default header block
    (sec-ch-ua family, user-agent, accept, sec-fetch-*, accept-encoding,
    priority, ...) in Chrome's order; these builders add only what the
    preset cannot know — the profile's accept-language and the FB-specific
    GraphQL headers (x-fb-friendly-name, x-fb-lsd). Overriding the preset
    with a complete-but-wrong list would degrade realism, so the explicit
    sets stay minimal (docs/09 §8 header-order note).

  Provenance of each header (docs/09 §1.1-1.2):
    * ``content-type``/``origin``/``referer``/``x-fb-lsd`` — validated or
      semi-validated on state-changing GraphQL calls.
    * ``x-fb-friendly-name`` — informational telemetry that mirrors the
      body's ``fb_api_req_friendly_name``; the web client always sends it,
      so omitting it is itself an anomaly.

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .profile import ClientProfile


def page_headers(profile: ClientProfile) -> dict[str, str]:
    """Headers for a plain authenticated page GET (docs/09 §1.3).

    The minimal overlay on the impersonation preset's default block:
    navigation GETs need only the profile's Accept-Language. Everything
    else (sec-ch-*, user-agent, accept, sec-fetch-*, cookie jar) is
    supplied by curl_cffi's impersonated default-header emission.

    Args:
        profile: The ``ClientProfile`` whose accept_language is layered on.

    Returns:
        A small header dict suitable for ``FBTransport.get``'s default.
    """
    return {"accept-language": profile.accept_language}


def graphql_headers(profile: ClientProfile, friendly_name: str, lsd: str | None) -> dict[str, str]:
    """Headers for a /api/graphql/ persisted-query POST (docs/04 §2, docs/09 §1.1).

    Args:
        profile: The ``ClientProfile`` supplying accept-language.
        friendly_name: The operation's registry name (doc 04 key), emitted
            as ``x-fb-friendly-name`` — the web client always pairs this
            header with the body's ``fb_api_req_friendly_name``.
        lsd: The login-session-data CSRF token; None omits ``x-fb-lsd``
            (semi-validated: some surfaces reject a stale or missing lsd,
            logged-out flows carry none — docs/09 §1.2).

    Returns:
        The FB-specific overlay headers for the impersonated POST. The
        TLS/h2 layer and browser-default headers come from curl_cffi's
        preset and must not be duplicated here (docs/09 §8).
    """
    headers = {
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://www.facebook.com",
        "referer": "https://www.facebook.com/",
        "accept-language": profile.accept_language,
        "x-fb-friendly-name": friendly_name,
    }
    if lsd:
        headers["x-fb-lsd"] = lsd
    return headers


def cookies_header(cookies: Mapping[str, str]) -> str:
    """Render the Cookie header in a stable order (jar order is normalised).

    Args:
        cookies: The cookie dict; NAME-ONLY content is reportable, values
            render into the wire header only (docs/03 §9.1).

    Returns:
        A single ``name=value; name=value`` header string with names in
        sorted order, so hand-built headers (e.g. WS handshakes) are
        byte-stable across runs and comparable in captures.
    """
    return "; ".join(f"{k}={v}" for k, v in sorted(cookies.items()))
