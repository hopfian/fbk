"""Cookie loading and secret hygiene (docs/03 §1, docs/11 §7, docs/12 §4).

This module owns the transition of credential material from disk (the
Netscape ``cookies.txt`` jar) into the transport layer, plus the redaction
primitives every journal-bearing surface uses.

ARCHITECTURE:

  Cookie Ingestion:
    ``load_netscape`` parses the jar with the stdlib ``MozillaCookieJar``,
    keeps only facebook.com-scoped rows (the session's domain, docs/03 §1),
    and flattens them to ``{name: value}`` for injection into the
    curl_cffi session jar. Expiry and attribute enforcement are delegated
    to the session jar, which replicates the browser's own policy.

  Secret Hygiene:
    The discipline enforced everywhere in fbk: cookie and token VALUES never
    enter logs, journals, exceptions, stdout, or typed errors. Only names,
    domains, and salted hash fingerprints are reportable — ``redact``
    converts secret-keyed payloads to fingerprinted placeholders before
    any journal emission, and ``describe`` renders presence/absence lines
    only (docs/12 §4).

SECURITY BOUNDARY:
  ``fingerprint`` uses a fixed application-salt SHA-256 prefix: stable and
  comparable across runs within the project, but never reversible to the
  underlying secret. Cookie values live in ``cookies.txt`` and the process
  memory only (docs/03 §9.1).

USER-DOC ANCHOR: cli/docs/03-session-and-auth.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import os
from collections.abc import Iterable, Mapping
from typing import Any

from .headers import cookies_header  # noqa: F401 (canonical home: headers.py)


class CookieLoadError(Exception):
    """The cookies.txt file is missing or unparseable."""


def load_netscape(path: str | os.PathLike[str]) -> dict[str, str]:
    """Parse a Netscape-format cookie jar into a flat ``{name: value}`` dict.

    The Netscape format (HTTP State Management Mechanism) uses tab-separated
    rows with fields ``domain``, ``flag``, ``path``, ``secure``, ``expiry``,
    ``name``, ``value``; ``#``-prefixed lines are comments and the
    stdlib ``MozillaCookieJar`` (which also understands the
    ``#HttpOnly_``-prefixed variant) performs the parsing. Only rows scoped
    to ``facebook.com`` are kept — the session's domain per docs/03 §1 —
    and only the ``name``/``value`` columns are extracted: expiry
    enforcement and ``HttpOnly``/``Secure`` semantics are delegated to
    curl_cffi's session cookie jar, which replicates the browser's own
    policy (docs/12 §4.1).

    Args:
        path: Path to the cookie file, typically ``cli/cookies.txt``.

    Returns:
        A flat string-to-string dict of facebook.com-scoped cookies, ready
        to be fed directly into ``curl_cffi.requests.Session.cookies.update()``.

    Raises:
        CookieLoadError: If the file is absent, unparseable by
            ``MozillaCookieJar``, or contains zero facebook.com-scoped rows —
            an empty jar must never silently produce an anonymous session.
    """
    if not os.path.isfile(path):
        raise CookieLoadError(f"cookie file not found: {path}")
    jar = http.cookiejar.MozillaCookieJar(os.fspath(path))
    try:
        # ignore_discard/ignore_expires: session cookies (no expiry column
        # value) and expired rows are still loaded — the session jar and the
        # operator own expiry policy, not the loader.
        jar.load(ignore_discard=True, ignore_expires=True)
    except (http.cookiejar.LoadError, OSError) as exc:
        raise CookieLoadError(f"failed to parse cookies.txt: {exc}") from exc
    cookies: dict[str, str] = {}
    for ck in jar:
        if "facebook.com" not in (ck.domain or ""):
            continue
        cookies[ck.name] = ck.value or ""
    if not cookies:
        raise CookieLoadError("no facebook.com cookies found in file")
    return cookies


def describe(cookies: Mapping[str, str]) -> list[str]:
    """Render a safe 'name: present/absent' report — never values.

    Args:
        cookies: The cookie dict whose keys are to be reported.

    Returns:
        Sorted report lines, one per cookie name, stating only whether a
        non-empty value exists for that name (docs/03 §9.1 hygiene: cookie
        names are reportable, values never are).
    """
    return [f"{name}: {'present' if cookies.get(name) else 'absent'}"
            for name in sorted(cookies)]


def fingerprint(value: str) -> str:
    """A short salted hash prefix for referencing a secret in logs.

    Args:
        value: The secret string (cookie value, token, ...) being
            referenced.

    Returns:
        The first 12 hex chars of SHA-256 over the fixed application salt
        ``"fbk-salt::"`` plus the value: stable across runs within the
        project for correlation, but never reversible to the secret and
        never collision-useful for recovery (docs/12 §4).
    """
    return hashlib.sha256(("fbk-salt::" + value).encode()).hexdigest()[:12]


def redact(obj: Any, secret_names: Iterable[str]) -> Any:
    """Deep-redact every dict key whose name is secret (journal payloads).

    Args:
        obj: An arbitrarily nested dict/list structure (a journal payload).
        secret_names: Key names to redact wherever they appear at any
            depth (e.g. ``xs``, ``fb_dtsg``, ``password``).

    Returns:
        A structurally identical copy in which every value under a secret
        key is replaced by ``"<redacted:<fingerprint>>"``; non-secret
        content passes through untouched.
    """
    secrets = set(secret_names)
    if isinstance(obj, dict):
        return {
            k: (f"<redacted:{fingerprint(str(v))}>" if k in secrets else redact(v, secrets))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v, secrets) for v in obj]
    return obj
