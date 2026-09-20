"""Page bootstrap: fetch, classify, and harvest the session tokens.

Implements the page side of the session-state machine (docs/03 §3,
docs/12 §3): the homepage HTML is fetched once, parsed for embedded SSR
``require``-frame data, and classified into one of the
:class:`auth.state.LoginState` states (``LOGGED_IN``, ``LOGGED_OUT``,
``CHECKPOINT``, ``UNKNOWN``). Everything the GraphQL layer needs for a
call — CSRF pair, viewer identity, deploy revision, bundle-URL census,
and the SSR preload registry — is harvested from that single load.

ARCHITECTURE:

  Extraction is tolerant-regex-over-HTML by design: tokens ship inside
  ``<script>`` blobs as ``require``-frame JSON escaped with ``\\/``, so
  each extractor runs a pattern ladder (live-confirmed shape first,
  historical fallbacks after) that degrades to ``None`` instead of
  raising on a truncated or marker-less page.
  :func:`bootstrap_homepage` orchestrates exactly one GET through
  ``FBTransport`` — which enforces governor pacing, journaling, and
  fingerprint coherence (docs/15 §P9-1) — so no call site can bypass
  the request discipline at the bootstrap level.

CALIBRATION NOTES (docs/15 §2 — every regex below matched live 2026-09):

  * ``fb_dtsg`` CSRF token (docs/03 §4):
    Shipped in require-frames as:
      ``["DTSGInitData",[],{"token":"NAf…:1:<unix-expiry>",``
      ``"async_get_token":"…"},N]``
    Tokens now begin with the ``NAf`` prefix; prior public references to
    ``AQ``-prefix tokens are stale (pre-2024 format). Three frames
    appear per page (one per SSR frame); they share a single
    ``async_get_token`` URL.
  * ``lsd`` anti-CSRF token (docs/03 §5):
    Shipped as ``["LSD",[],{"token":"…"},N]`` — three tokens per page,
    one per SSR frame. The first token is the one the browser sends;
    all are retained in ``lsd_all``.
  * Checkpoint detection:
    The authoritative signal is ``"is_checkpointed":(true|false)`` in the
    config frames. String-matching ``/checkpoint`` in URLs FALSE-POSITIVES
    on healthy pages (the string appears in URL routing tables), so it is
    trusted only on the post-redirect ``final_url``.
  * Deploy revision lives in
    ``["SiteData",[],{"server_revision":N,…},N]`` — the legacy ``__rev``
    key is gone; the value tracks deploy boundaries for registry
    staleness (docs/13 §3).
  * The viewer config carries ``"logout_hash"`` — the secret that
    authorizes the ``logout.php`` POST pair.
  * SSR preloader registry (docs/15 §P2-2):
    Pages embed ``{"actorID","preloaderID","queryID","variables",
    "queryName"}`` registrations carrying the VERBATIM variables the
    server itself used during SSR — replaying them is the single most
    faithful and bypass-resistant read strategy in the surface.

SECURITY BOUNDARY:
  ``fb_dtsg``, ``lsd``, and ``logout_hash`` are wrapped in
  ``pydantic.SecretStr`` throughout, so ``repr()``, ``str()``, and
  logging formatters never leak the raw values.
  :meth:`Bootstrap.safe_dict` produces the journal view: token values
  are replaced by HMAC fingerprints, never emitted in cleartext
  (docs/11 §7 non-disclosure policy).

USER-DOC ANCHOR: cli/docs/03-session-and-auth.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, SecretStr

import constants as C

from .state import LoginState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from transport.session import FBTransport

# ---------------------------------------------------------------------- extractors
# DTSG require-frame pattern ladder, in descending order of live confidence:
#   primary (docs/15 §2):   ["DTSGInitData",[],{"token":"NAf…:1:<unix>"},N]
#   DTSGInitialData:        historical frame spelling retained for older deploys.
#   inline object:          non-frame config-object spelling on degraded pages.
_RE_DTSG = (
    re.compile(r'"DTSGInitData",\[\],\{"token":"([^"]+)"'),
    re.compile(r'"DTSGInitialData",\[\],\{"token":"([^"]+)"'),
    re.compile(r'"DTSGInitData",\{"token":"([^"]+)"'),
)
# Legacy hidden-form spelling (docs/03 §4.2): <input name="fb_dtsg" value="…">.
_RE_DTSG_INPUT = re.compile(r'name="fb_dtsg"\s+value="([^"]+)"')
# Last-resort generic: any AQ- or NAf-prefixed "token" literal — the only
# pattern that still recognizes the stale AQ format (docs/15 §2 calibration).
_RE_DTSG_GENERIC = re.compile(r'"token":"((?:AQ|NAf)[A-Za-z0-9_\-:]+)"')
# LSD require-frame (docs/15 §2): ["LSD",[],{"token":"…"},N] — 3 per page live.
_RE_LSD_ALL = re.compile(r'"LSD",\[\],\{"token":"([^"]+)"')
# Fallback: plain "lsd":"…" JSON spelling on non-frame surfaces (docs/03 §5).
_RE_LSD_FALLBACK = re.compile(r'"lsd"\s*:\s*"([A-Za-z0-9_-]+)"')
# CurrentUserInitialData identity frame: USER_ID is the uid-coherence check
# against c_user (docs/15 §2; the server-side actor is the xs binding, docs/03 §6.4).
_RE_USER_ID = re.compile(r'"USER_ID"\s*:\s*"?(\d+)"?')
# viewer-config secret pairing logout.php (docs/15 §2).
_RE_LOGOUT_HASH = re.compile(r'"logout_hash":"([^"]+)"')
# rsrc.php script chunks; protocol-relative URLs are absolutised to the CDN
# (the path hash is the content identity — docs/13 §3).
_RE_BUNDLE = re.compile(r'(?:https?:)?//[\w.\-]+/rsrc\.php/[\w./\-]+\.js(?:\?[\w=\-]*)?')
# SiteData deploy revision (docs/15 §2): __rev is gone from live pages.
_RE_REV = re.compile(r'"server_revision"\s*:\s*"?(\d+)"?')
# SSR preloader registration head (docs/15 §P2-2); the variables object that
# follows the match offset is decoded with raw_decode so nested braces parse exactly.
_RE_PRELOAD_FULL = re.compile(
    r'\{"actorID":"(\d+)","preloaderID":"(adp_[A-Za-z0-9_]+RelayPreloader_[0-9a-f]+)",'
    r'"queryID":"(\d{5,})","variables":'
)


class BootstrapPageError(RuntimeError):
    """The served page is unusable — the canonical trigger is HTTP 200 with
    an empty body, the edge soft-block signature (docs/10 §3). Subclasses
    ``RuntimeError`` so existing handlers keep catching it. Callers must
    treat this as an enforcement signal: disengage per the docs/11 §5
    containment principle, never retry-escalate."""


def _first(html: str, patterns: Iterable[re.Pattern[str]]) -> str | None:
    """First capture group of the first pattern that matches.

    Ladder semantics: extractors try live-confirmed shapes before
    historical fallbacks, so a truncated or degraded page yields
    ``None`` instead of raising.
    """
    for pat in patterns:
        m = pat.search(html)
        if m:
            return m.group(1)
    return None


def extract_fb_dtsg(html: str) -> str | None:
    """Harvest the CSRF token used by every mutating call (docs/03 §4).

    Args:
        html: Raw page HTML containing SSR ``require``-frame JSON.

    Returns:
        The live ``NAf…:1:<unix-expiry>`` token, or ``None`` on
        logged-out/checkpoint pages that carry no DTSG frame.
    """
    return (_first(html, _RE_DTSG) or _first(html, (_RE_DTSG_INPUT,))
            or _first(html, (_RE_DTSG_GENERIC,)))


def extract_lsd_all(html: str) -> list[str]:
    """Harvest every LSD token from the page (docs/15 §2).

    One ``["LSD",[],{"token":"…"},N]`` frame ships per SSR frame (three
    observed live); all are preserved in page order because downstream
    replay may reference any frame. Falls back to the plain
    ``"lsd":"…"`` JSON spelling when no frames are present.

    Args:
        html: Raw page HTML.

    Returns:
        All tokens in page order; empty list when the page carries none.
    """
    hits = _RE_LSD_ALL.findall(html)
    if hits:
        return hits
    fb = _RE_LSD_FALLBACK.search(html)
    return [fb.group(1)] if fb else []


def extract_lsd(html: str) -> str | None:
    """First LSD token — the CSRF pair part 2 (header/body, docs/03 §5).

    The first frame's token is the one the browser itself sends
    (docs/15 §2 calibration).

    Args:
        html: Raw page HTML.

    Returns:
        The first token in page order, or ``None`` when no LSD frame is
        present (logged-out bootstrap).
    """
    tokens = extract_lsd_all(html)
    return tokens[0] if tokens else None


def extract_user_id(html: str) -> str | None:
    """Viewer uid from the CurrentUserInitialData frame (docs/15 §2).

    The server-side actor is the ``xs`` binding (docs/03 §6.4); this
    value serves as the coherence check — it must equal the ``c_user``
    cookie, else the session classifies LOGGED_OUT (uid mismatch).

    Args:
        html: Raw page HTML.

    Returns:
        The numeric uid, or ``None`` when the identity frame is absent.
    """
    return _first(html, (_RE_USER_ID,))


def extract_logout_hash(html: str) -> str | None:
    """Harvest the secret that authorizes ``logout.php`` (docs/03 §3).

    Args:
        html: Raw page HTML.

    Returns:
        The ``logout_hash`` from the viewer config, or ``None`` on pages
        that do not carry it (logged-out/checkpoint bootstraps).
    """
    return _first(html, (_RE_LOGOUT_HASH,))


def extract_revision(html: str) -> str | None:
    """Harvest the deploy revision from the SiteData frame (docs/15 §2).

    The revision identifies the current build for registry-staleness
    tracking (docs/13 §3 deploy-boundary detection).

    Args:
        html: Raw page HTML.

    Returns:
        The ``server_revision`` as a string, or ``None`` when absent.
    """
    return _first(html, (_RE_REV,))


def extract_bundles(html: str) -> list[str]:
    """Collect the rsrc.php JS bundle URLs referenced by a page (docs/13 §2.1).

    A live homepage load references ~680 bundles (docs/15 §1). Escaped
    ``\\/`` path separators are normalised first; protocol-relative URLs
    are absolutised against ``static.xx.fbcdn.net`` — the path hash is
    the content identity, so two URLs are the same bundle iff equal
    (docs/13 §3).

    Args:
        html: Raw page HTML.

    Returns:
        De-duplicated absolute bundle URLs in first-appearance order —
        the harvest input for the doc_id registry refresh.
    """
    norm = html.replace("\\/", "/")
    found: list[str] = []
    seen: set[str] = set()
    for m in _RE_BUNDLE.finditer(norm):
        url = m.group(0)
        if not url.startswith("http"):
            url = "https://static.xx.fbcdn.net" + url
        if url not in seen:
            seen.add(url)
            found.append(url)
    return found


# ------------------------------------------------------------------- preload registry
class PreloadEntry(BaseModel):
    """One SSR preloader registration (docs/15 §P2-2).

    Live shape:

      ``{"actorID":"<uid>","preloaderID":"adp_<Friendly>RelayPreloader_<guid>",``
      ``"queryID":"<doc_id>","variables":{…},"queryName":"<Friendly>"}``

    The ``variables`` are the exact values the server itself used during
    SSR — replaying them verbatim against ``queryID`` is the most
    faithful, bypass-resistant read available (a 923 KB live feed was
    replayed this way, docs/15 §P2-2).
    """

    actor_id: str        # uid the preload was rendered for
    preloader_id: str    # adp_<Friendly>RelayPreloader_<guid> — Relay island handle
    doc_id: str         # persisted-query id (numeric string)
    query_name: str | None = None    # Relay friendly name; None when not adjacent
    variables: dict[str, Any] = Field(default_factory=dict)  # verbatim SSR variables


def extract_preload_registry(html: str) -> list[PreloadEntry]:
    """Harvest preloaded (queryName, doc_id, variables) tuples from a page.

    The ``variables`` object is decoded with ``raw_decode`` from the
    match offset so nested braces are handled exactly. The
    ``queryName`` key is searched only in the 4 KB window following the
    match — the live wire places it after the variables object. Entries
    are deduplicated on ``(doc_id, query_name)``: one page load yields
    11-12 distinct preloaders (newsfeed, composer, contacts, megaphone,
    right-rail, stories tray … docs/15 §P2-2).

    Args:
        html: Raw page HTML.

    Returns:
        Preload entries in first-appearance order.
    """
    dec = json.JSONDecoder()
    out: dict[tuple[str, str | None], PreloadEntry] = {}
    for m in _RE_PRELOAD_FULL.finditer(html):
        actor_id, preloader_id, doc_id = m.group(1), m.group(2), m.group(3)
        try:
            variables, _end = dec.raw_decode(html, m.end())
        except json.JSONDecodeError:
            continue
        qn = re.search(r'"queryName":"([A-Za-z0-9_]+)"', html[m.end(): m.end() + 4000])
        entry = PreloadEntry(actor_id=actor_id, preloader_id=preloader_id,
                             doc_id=doc_id, query_name=qn.group(1) if qn else None,
                             variables=variables if isinstance(variables, dict) else {})
        out.setdefault((doc_id, entry.query_name), entry)
    return list(out.values())


# ------------------------------------------------------------------------ bootstrap
class Bootstrap(BaseModel):
    """Complete session metadata extracted from one page bootstrap (docs/03 §3).

    Populated by :func:`bootstrap_homepage` from a single homepage load.
    Secret fields (``fb_dtsg``, ``lsd``, ``logout_hash``) are wrapped in
    ``pydantic.SecretStr`` so ``repr()``, ``str()``, and logging
    formatters cannot leak them; :meth:`safe_dict` produces the
    redacted journal view (HMAC fingerprints, never cleartext —
    docs/11 §7).
    """

    state: LoginState = LoginState.UNKNOWN
    fb_dtsg: SecretStr | None = None          # CSRF pair part 1 (form/body)
    lsd: SecretStr | None = None              # CSRF pair part 2 (header/body)
    lsd_all: list[str] = Field(default_factory=list)   # every SSR frame's token
    user_id: str | None = None
    user_name: str | None = None
    logout_hash: SecretStr | None = None
    revision: str | None = None                # SiteData server_revision (deploy id)
    final_url: str = ""
    html_len: int = 0
    bundle_urls: list[str] = Field(default_factory=list)
    preloads: list[PreloadEntry] = Field(default_factory=list)
    markers_seen: list[str] = Field(default_factory=list)
    fetched_at: float = Field(default_factory=time.time)

    def dtsg(self) -> str | None:
        """Unwrapped ``fb_dtsg`` value, or ``None`` when absent/empty.

        Unwraps the ``SecretStr`` BEFORE the emptiness check: testing the
        wrapper itself makes emptiness truthiness pydantic-version-
        dependent, and an empty token must surface as ``None`` (a cache
        miss) — never as a fresh empty-token entry downstream
        (docs/16 §P11-1 token-cache contract).

        Returns:
            The plaintext CSRF token for call bodies, or ``None``.
        """
        value = self.fb_dtsg.get_secret_value() if self.fb_dtsg else ""
        return value or None

    def lsd_value(self) -> str | None:
        """Unwrapped ``lsd`` value, or ``None`` when absent/empty.

        Applies the same unwrap-then-check discipline as :meth:`dtsg`:
        an empty ``SecretStr`` must surface as ``None`` rather than as a
        falsy-but-present token downstream.

        Returns:
            The plaintext LSD token for call bodies, or ``None``.
        """
        value = self.lsd.get_secret_value() if self.lsd else ""
        return value or None

    def safe_dict(self) -> dict[str, Any]:
        """Journal/report view with every secret redacted.

        Token values are replaced by ``<redacted:<hmac-fingerprint>>``
        via :func:`transport.cookies.fingerprint` — sufficient to
        correlate entries across journal lines without disclosing the
        tokens themselves (docs/11 §7 non-disclosure policy).

        Returns:
            A JSON-serialisable dict of the bootstrap's non-secret state
            plus fingerprinted stand-ins for each token.
        """
        from transport.cookies import fingerprint

        def red(v: str | None) -> str | None:
            return f"<redacted:{fingerprint(v)}>" if v else None

        return {
            "state": self.state.value,
            "fb_dtsg": red(self.dtsg()),
            "lsd": red(self.lsd_value()),
            "lsd_frame_count": len(self.lsd_all),
            "user_id": self.user_id,
            "user_name": self.user_name,
            "logout_hash": red(self.logout_hash.get_secret_value() if self.logout_hash else None),
            "revision": self.revision,
            "final_url": self.final_url,
            "html_len": self.html_len,
            "bundle_count": len(self.bundle_urls),
            "preload_queries": [p.query_name or "?" for p in self.preloads][:48],
            "markers_seen": self.markers_seen,
        }


def classify_login_state(html: str, final_url: str,
                         cookies: Mapping[str, str]) -> tuple[LoginState, list[str]]:
    """Classify a served page into the session state machine (docs/03 §3).

    Calibrated live 2026-09 (docs/15 §2):
      * ``"is_checkpointed":(true|false)`` is authoritative for CHECKPOINT;
        ``/checkpoint`` string-matching false-positives on healthy pages,
        so the URL is trusted only as the post-redirect ``final_url``.
      * ``CurrentUserInitialData``'s ``USER_ID`` must equal the ``c_user``
        cookie — a uid mismatch means the ``xs`` binding no longer points
        at this viewer, classifying LOGGED_OUT (docs/03 §6.4).
      * Cookie-authenticated pages with no login markers classify
        LOGGED_IN on ``cookies_only``; the marker sets vary by deploy.

    Args:
        html: Raw response body of the served page.
        final_url: URL after redirects; a ``/checkpoint`` landing here is
            an authoritative CHECKPOINT.
        cookies: Live cookie-jar snapshot; only ``c_user``/``xs``
            presence and the ``c_user`` value are consulted.

    Returns:
        ``(state, markers)`` — ``markers`` lists the evidence strings that
        produced the decision (surfaced to the operator and the journal
        via ``Bootstrap.markers_seen``).
    """
    has_auth = bool(cookies.get("c_user") and cookies.get("xs"))

    if "/checkpoint" in final_url:
        return LoginState.CHECKPOINT, ["final_url"]
    if C.CHECKPOINT_FLAG in html:
        return LoginState.CHECKPOINT, ["is_checkpointed:true"]

    logged_out = [mk for mk in C.LOGGED_OUT_MARKERS if mk in html]
    logged_in = [mk for mk in C.LOGGED_IN_MARKERS if mk in html]

    if logged_out and not logged_in:
        return LoginState.LOGGED_OUT, logged_out
    if has_auth and logged_in:
        page_uid = extract_user_id(html)
        if page_uid and cookies.get("c_user") and page_uid != cookies["c_user"]:
            return LoginState.LOGGED_OUT, ["uid_mismatch", *logged_in]
        return LoginState.LOGGED_IN, logged_in
    if has_auth and not logged_out:
        return LoginState.LOGGED_IN, ["cookies_only"]
    if not has_auth:
        return LoginState.LOGGED_OUT, ["no_auth_cookies"]
    return LoginState.UNKNOWN, []


def extract_account_warnings(html: str) -> list[str]:
    """Scan a served page for account-level restriction banners (P8-1, P9-2).

    The Phase-8 enforcement ladder ended in an "automated behaviour"
    warning (docs/15 §P8-1). Any hit here means the operator must STOP
    automated activity entirely — disengage per the docs/11 §5
    containment principle rather than push further; ``constants``
    ships the marker list so this check stays in one place.

    Args:
        html: Raw response body.

    Returns:
        Every ``ACCOUNT_WARNING_MARKERS`` substring found, in marker-list
        order; empty when the account is unflagged.
    """
    return [m for m in C.ACCOUNT_WARNING_MARKERS if m in html]


def bootstrap_homepage(transport: FBTransport, cookies: Mapping[str, str]) -> Bootstrap:
    """GET www.facebook.com/ and harvest everything needed for GraphQL calls.

    One page load yields the CSRF pair, viewer identity, deploy revision,
    bundle-URL census, and SSR preload registry. A truncated or
    marker-less page fails soft: extractors return ``None`` and
    classification lands in UNKNOWN/LOGGED_OUT; the only hard failure is
    the empty-body 200 soft-block, which raises
    :class:`BootstrapPageError` (docs/10 §3).

    Args:
        transport: Cookie-authenticated ``FBTransport``; the GET passes
            through the governor, so pacing discipline cannot be bypassed.
        cookies: Cookie map used for the classification coherence check.

    Returns:
        The populated :class:`Bootstrap` for this page load, including
        ``ACCOUNT_WARNING:…`` markers when restriction banners are present.

    Raises:
        BootstrapPageError: HTTP 200 with an empty body — the edge
            soft-block signature; the caller must disengage, not retry.
    """
    resp = transport.get("https://www.facebook.com/", allow_redirects=True)
    if resp.status_code == 200 and not resp.content:
        raise BootstrapPageError(
            "200 with empty body — classic edge soft-block signature (docs/10 §3)")
    html = resp.text
    state, markers = classify_login_state(html, str(resp.url), cookies)
    warnings = extract_account_warnings(html)
    if warnings:
        # a flagged account must not be pushed further: report it loudly
        markers = [*markers, *[f"ACCOUNT_WARNING:{w}" for w in warnings]]
    lsd_all = extract_lsd_all(html)
    name = None
    # CurrentUserInitialData NAME capture (docs/15 §2): the live frame orders
    # ACCOUNT_ID, USER_ID, then NAME; display names ship unicode-escaped.
    nm = re.search(r'"CurrentUserInitialData",\[\],\{"ACCOUNT_ID":"\d+","USER_ID":"\d+",'
                   r'"NAME":"([^"]*)"', html)
    if nm:
        try:
            name = nm.group(1).encode().decode("unicode_escape")
        except UnicodeDecodeError:
            # truncated/mojibake escapes on a malformed page: keep the raw
            # capture rather than crashing the whole bootstrap
            name = nm.group(1)
    return Bootstrap(
        state=state,
        fb_dtsg=SecretStr(dtsg) if (dtsg := extract_fb_dtsg(html)) else None,
        lsd=SecretStr(lsd_all[0]) if lsd_all else None,
        lsd_all=lsd_all,
        user_id=extract_user_id(html),
        user_name=name,
        logout_hash=SecretStr(h) if (h := extract_logout_hash(html)) else None,
        revision=extract_revision(html),
        final_url=str(resp.url),
        html_len=len(html),
        bundle_urls=extract_bundles(html),
        preloads=extract_preload_registry(html),
        markers_seen=markers,
    )
