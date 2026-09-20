"""Transport package: curl_cffi HTTP, cookie hygiene, client profiles.

ARCHITECTURE:
    The lowest layer of the stack (docs/12 §2 layering rule): everything
    above — auth bootstrap, GraphQL, surfaces, commands — may import from
    here, and nothing here imports upward. The package assembles the
    coherence-critical trio the edge scores (docs/16 §1): fingerprint
    (``FBTransport`` + ``resolve_impersonate``), credential hygiene
    (``load_netscape`` + redaction primitives), and the identity tuple
    (``ClientProfile`` + ``sanitize_user_agent``).

Public API (docs/09):
  * FBTransport: the chrome136-impersonated HTTP client
  * resolve_impersonate: pick the newest supported target
  * load_netscape: cookies.txt loader (facebook-scoped)
  * ClientProfile: coherent UA/sec-ch-ua/locale/tz identity
  * sanitize_user_agent: strip HeadlessChrome markers
  * Secret hygiene: redact, fingerprint, describe

USER-DOC ANCHOR: cli/docs/03-session-and-auth.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from .cookies import (
    CookieLoadError,
    describe,
    fingerprint,
    load_netscape,
    redact,
)
from .headers import cookies_header
from .profile import ClientProfile, load_or_default, sanitize_user_agent
from .session import FBTransport, FingerprintRejectedError, resolve_impersonate

__all__ = [
    "ClientProfile",
    "CookieLoadError",
    "FBTransport",
    "FingerprintRejectedError",
    "cookies_header",
    "describe",
    "fingerprint",
    "load_netscape",
    "load_or_default",
    "redact",
    "resolve_impersonate",
    "sanitize_user_agent",
]
