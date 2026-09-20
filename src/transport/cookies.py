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
from pathlib import Path
from typing import Any

from .headers import cookies_header  # noqa: F401 (canonical home: headers.py)


class CookieLoadError(Exception):
    """The cookies.txt file is missing or unparseable."""


def _tolerant_facebook_rows(text: str) -> tuple[dict[str, str], int]:
    """Line-level reparse of a Netscape jar the strict loader rejected.

    The self-healing path for a torn jar (one mangled line among good
    rows — a hand-edited file, a truncated export): rows that carry the
    canonical seven tab-separated fields (including the ``#HttpOnly_``
    prefix variant) are parsed directly, mangled lines are dropped and
    counted. Only the ``name``/``value`` columns are extracted — exactly
    what the strict loader's output feeds — so the healed result is
    byte-equivalent to a clean jar's parse.

    Returns:
        ``(rows, mangled)`` — the facebook.com-scoped ``{name: value}``
        map (possibly empty) and the count of NON-COMMENT, non-blank
        lines that failed the seven-field shape (the genuinely damaged
        rows; comment and blank lines are not damage).
    """
    rows: dict[str, str] = {}
    mangled = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#HttpOnly_"):
            stripped = stripped[len("#HttpOnly_"):]
        elif stripped.startswith("#"):
            continue  # comment row
        fields = stripped.split("\t")
        if len(fields) != 7:
            mangled += 1  # mangled row: dropped, counted for the heal event
            continue
        domain, _flag, _path, _secure, _expiry, name, value = fields
        if "facebook.com" not in (domain or ""):
            continue
        rows[name] = value
    return rows, mangled


def load_netscape(path: str | os.PathLike[str], *,
                  on_heal: Any = None) -> dict[str, str]:
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

    Self-healing (src/healing.py): when the strict parse fails on a
    NON-EMPTY file (one mangled line among good rows — a torn export,
    a hand-edited jar), a tolerant line-level reparse runs; if it still
    yields facebook.com rows, the healed map is returned and ``on_heal``
    (when given) is invoked with the dropped-row count so the coordinator
    records a ``cookie-jar-heal`` event. A file the tolerant pass cannot
    rescue (all rows mangled, or zero facebook.com rows) raises exactly
    as the strict path did — a jar without session cookies must never
    silently produce an anonymous session.

    Args:
        path: Path to the cookie file, typically ``cli/cookies.txt``.
        on_heal: Optional callback invoked as ``on_heal(detail)`` when the
            tolerant reparse healed the load; ``detail`` counts the
            dropped rows (never carries values).

    Returns:
        A flat string-to-string dict of facebook.com-scoped cookies, ready
        to be fed directly into ``curl_cffi.requests.Session.cookies.update()``.

    Raises:
        CookieLoadError: If the file is absent, unparseable by
            ``MozillaCookieJar`` AND unrescuable by the tolerant reparse,
            or contains zero facebook.com-scoped rows — an empty jar must
            never silently produce an anonymous session.
    """
    if not os.path.isfile(path):
        raise CookieLoadError(f"cookie file not found: {path}")
    jar = http.cookiejar.MozillaCookieJar(os.fspath(path))
    try:
        # ignore_discard/ignore_expires: session cookies (no expiry column
        # value) and expired rows are still loaded — the session jar and the
        # operator own expiry policy, not the loader.
        #
        # The stdlib's _really_load emits a "http.cookiejar bug!" UserWarning
        # on the unpack failure a mangled row produces (right before raising
        # LoadError). The tolerant reparse below OWNS that failure path — the
        # warning is noise on an already-healed input, so it is silenced for
        # the strict attempt only, and only that exact message.
        import warnings
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="http.cookiejar bug!",
                                    category=UserWarning)
            jar.load(ignore_discard=True, ignore_expires=True)
    except (http.cookiejar.LoadError, OSError) as exc:
        # self-healing: one torn row must not kill the session when the
        # auth-bearing rows survive — reparse line-level, drop the damage
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            raise CookieLoadError(f"failed to parse cookies.txt: {exc}") from exc
        healed, mangled = _tolerant_facebook_rows(text)
        if not healed:
            raise CookieLoadError(f"failed to parse cookies.txt: {exc}") from exc
        if on_heal is not None:
            on_heal(f"strict parse failed; tolerant reparse dropped "
                    f"{mangled} row(s)")
        return healed
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
