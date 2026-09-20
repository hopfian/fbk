"""FBTransport: the cookie-authenticated, fingerprint-coherent HTTP client.

Every request fbk issues to the Facebook edge passes through this class
so that TLS/h2 identity (docs/09), cookie hygiene (docs/03 §1), request
journaling (docs/11 §7), and request governor integration (docs/10 §7,
docs/15 §P9-1) remain uniform and impossible to bypass at the call-site
level.

ARCHITECTURE:

  Transport Identity (docs/09 §2, docs/16 §1):
    curl_cffi impersonates ``chrome136`` by default — the version confirmed
    live 2026-09 to pass Facebook's JA3/JA4 fingerprint gate with zero
    interstitials (docs/15 §1). The ``resolve_impersonate()`` helper probes
    each candidate by opening a connection to 127.0.0.1:9 (a port
    guaranteed to refuse) to distinguish unsupported targets
    (``ValueError`` before any I/O) from valid ones (``ConnectionRefused``
    after the TLS handshake succeeds — docs/12 §3 probe methodology).
    Fallback targets descend ``chrome136 → chrome131 → chrome124 →
    chrome120`` (``constants.IMPERSONATE_FALLBACKS``).

  Session Cookie Jar:
    Cookies are loaded from ``cookies.txt`` (Netscape format, via
    ``transport.cookies.load_netscape``) and injected into the underlying
    ``curl_cffi.requests.Session`` cookie jar. Set-Cookie rotation on 302
    re-bootstrap responses is absorbed automatically, keeping the jar
    consistent with any lifetime updates Facebook issues for ``datr``,
    ``sb``, or ``fr`` (docs/03 §1).

  Governor Integration (docs/10 §7, docs/15 §P9-1):
    Every ``get()`` and ``post()`` call invokes ``_govern()`` BEFORE the
    network I/O begins, enforcing the lognormal inter-arrival gap and
    budget caps. Soft-block signals (empty-200, HTTP 403/429) are fed back
    via ``_observe_governor_response()`` to trigger a containment cooldown
    — enforcement is answered with disengagement, not escalating retries
    (docs/11 §5 containment principle, docs/15 §P8-1 lesson).

  Rupload Seam (docs/15 §P6-1):
    ``raw_post()``/``raw_get()`` are the deliberate ungoverned seam for
    rupload media transfers, whose hand-built multipart bodies and custom
    headers the governed API cannot express. Chunk transfer is data
    plumbing, not a user-visible mutation — the GraphQL publish that
    concludes each upload goes through ``post_graphql()`` and carries the
    mutation budget. Raw calls remain soft-block-observed and are journaled
    centrally with a surface tag, so every bypass of the governed API is
    still auditable.

  Journaling (docs/11 §7):
    Requests and response metadata are appended through the optional
    ``JournalLike`` dependency as JSONL records: method, URL, status,
    content length, and context keys (body field NAMES only). Token and
    cookie values never enter journal entries in cleartext (docs/12 §4
    secret-hygiene discipline).

USER-DOC ANCHOR: cli/docs/03-session-and-auth.md and cli/docs/09-safety-and-opsec.md —
ship a matching edit to the guides in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import contextlib
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol, cast

import constants as C

from .cookies import CookieLoadError  # noqa: F401 (re-export for compat)
from .headers import graphql_headers, page_headers
from .profile import ClientProfile

if TYPE_CHECKING:
    # curl_cffi costs ~200ms to import and is only needed once a transport
    # is actually constructed — never at module import (startup audit
    # 2026-09: ~93% of CLI invocation cost is import overhead). The
    # runtime imports live in resolve_impersonate / FBTransport.__init__;
    # annotations below resolve lazily via `from __future__ import
    # annotations`.
    from curl_cffi import requests as creq
    from curl_cffi.requests import BrowserTypeLiteral


class JournalLike(Protocol):
    """The journal contract FBTransport depends on.

    Deliberately minimal (a single ``record(entry)``) so the JSONL journal,
    test fakes, and any future sink are all interchangeable without the
    transport importing the journal package (docs/12 §2 layering rule:
    the journal is injected as a callback, never imported upward).
    """

    def record(self, entry: dict[str, Any]) -> None: ...


class FingerprintRejectedError(Exception):
    """The edge refused the client fingerprint (interstitial / empty body)."""


def resolve_impersonate(preferred: str = C.IMPERSONATE_DEFAULT) -> BrowserTypeLiteral:
    """Pick the newest curl_cffi impersonation target this build supports.

    Probes candidates in fallback order (``chrome136 → chrome131 →
    chrome124 → chrome120``) and returns the first one this curl_cffi build
    can express. The pinned-version policy is deliberate: the unversioned
    ``impersonate="chrome"`` target would silently change the fingerprint
    on library upgrade, breaking day-to-day coherence (docs/16 §6).

    Validation trick (docs/12 §3): an unsupported target raises while
    setting curl options (before any network I/O), so probing a closed
    loopback port distinguishes 'unknown target' (ValueError) from 'valid
    target' (connection refused).

    Args:
        preferred: Preferred impersonation target name; defaults to
            ``constants.IMPERSONATE_DEFAULT`` ("chrome136").

    Returns:
        The first candidate the installed curl_cffi supports, typed as a
        ``BrowserTypeLiteral`` for downstream casts.

    Raises:
        FingerprintRejectedError: If no candidate in the fallback chain is
            supported — the transport cannot present a coherent edge
            identity and must not be constructed.
    """
    from curl_cffi import requests as creq  # lazy: only paid on construction
    for candidate in (preferred, *C.IMPERSONATE_FALLBACKS):
        try:
            # the probe below validates `candidate` before it is trusted
            creq.get("http://127.0.0.1:9/",
                     impersonate=cast("BrowserTypeLiteral", candidate), timeout=3)
        except ValueError:
            # unsupported target: curl_cffi raises while setting options,
            # before any network I/O — try the next fallback (docs/12 §3).
            continue
        except Exception:
            # any other failure means the TLS handshake itself succeeded
            # (connection refused on the probe port) — target is valid.
            return cast("BrowserTypeLiteral", candidate)
    raise FingerprintRejectedError("no supported curl_cffi impersonation target found")


class FBTransport:
    """HTTP transport bound to one cookie session and client profile.

    One instance = one coherent identity tuple (docs/08 §5): the cookie
    jar, the ``ClientProfile``, and the resolved curl_cffi impersonation
    target are frozen together at construction and every request method
    replays that tuple. The class enforces the invariant the edge scores
    (docs/16 §1): same account, same fingerprint, on every wire contact.

    Governed methods (``get``/``post``/``post_graphql``) cannot be paced
    around, and the ungoverned rupload seam (``raw_post``/``raw_get``)
    remains soft-block-observed and journaled.
    """

    def __init__(
        self,
        cookies: Mapping[str, str],
        profile: ClientProfile | None = None,
        journal: JournalLike | None = None,
        timeout: float = 30.0,
        impersonate: str | None = None,
        governor: Any | None = None,
    ):
        """Construct the transport and freeze its identity tuple.

        Args:
            cookies: The facebook.com cookie jar (from
                ``transport.cookies.load_netscape``), injected into the
                underlying curl_cffi session jar.
            profile: Coherent client profile; defaults to the safe
                ``ClientProfile()`` default (chrome136 / Windows / en-US).
            journal: Optional JSONL sink satisfying ``JournalLike``;
                when present, every request's metadata is recorded.
            timeout: Per-request timeout in seconds for all wire calls.
            impersonate: Override the impersonation target; when None,
                resolved from the profile via ``resolve_impersonate``.
            governor: Optional ``RequestGovernor`` (docs/15 §P9-1) pacing
                the governed request methods; None disables pacing.

        Raises:
            FingerprintRejectedError: If the profile fails coherence
                validation or no impersonation target can be resolved.
        """
        from curl_cffi import requests as creq  # lazy: construction-time only
        self.profile = profile or ClientProfile()
        problems = self.profile.validate_coherence()
        if problems:
            raise FingerprintRejectedError("incoherent client profile: " + "; ".join(problems))
        self.cookies = dict(cookies)
        self.journal = journal
        self.timeout = timeout
        self.impersonate = impersonate or resolve_impersonate(self.profile.impersonate)
        self.governor = governor  # RequestGovernor | None (docs/10/11 discipline)
        # typed-by-contract (§5.5): curl_cffi 0.16+ ships py.typed with
        # Session generic over the response class; the default response
        # type is pinned explicitly because the strict gate requires the
        # type argument (mypy runs at python_version 3.11, below the
        # TypeVar-default syntax upstream).
        self._session: creq.Session[creq.Response] = creq.Session()
        self._session.cookies.update(self.cookies)
        self._closed = False
        self._q = 0  # Relay request sequence (docs/04 §2)

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        """Idempotent close of the underlying curl session.

        Pooled connections are released; further requests fail fast. Safe
        to call multiple times and from ``__exit__`` after an exception.
        """
        if not self._closed:
            self._closed = True
            with contextlib.suppress(Exception):
                self._session.close()

    def __enter__(self) -> FBTransport:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ primitives
    def next_q(self) -> str:
        """The next Relay request sequence number (docs/04 §2).

        Returns:
            The monotonically increasing per-transport counter as a string,
            suitable for the ``__req`` form field.
        """
        self._q += 1
        return str(self._q)

    def _govern(self, *, is_mutation: bool = False) -> None:
        """Apply the pacing/cap gate before any request (docs/10 §7).

        The gate runs BEFORE the network I/O begins so a blocked request
        never reaches the edge at all — budget exhaustion aborts the call
        client-side rather than after the socket is open.
        """
        if self.governor is not None:
            self.governor.before_request(is_mutation=is_mutation)

    def _observe_governor_response(self, resp: Any) -> None:
        """Feed soft-block signals back to the governor (empty-200, 403/429).

        Args:
            resp: The curl_cffi response (or response-like object) from the
                just-completed request; only ``status_code`` and ``content``
                are read.
        """
        if self.governor is None:
            return
        status = getattr(resp, "status_code", 0)
        # empty-200: the edge's silent soft-block shape — a 200 whose body
        # never arrives is the classic degraded-payload signal (docs/10 §3).
        empty_200 = status == 200 and not getattr(resp, "content", b"")
        if empty_200 or status in (403, 429):
            self.governor.observe_soft_block()

    def get(self, url: str, *, headers: Mapping[str, str] | None = None,
            **kw: Any) -> creq.Response:
        """Governed, impersonated GET; returns the curl_cffi response.

        Args:
            url: Absolute https URL on a facebook.com surface.
            headers: Optional header overrides; when None, the canonical
                page-GET set from ``transport.headers.page_headers`` is
                applied (docs/09 §1.3 — curl_cffi's impersonation preset
                supplies the browser-order default block).
            **kw: Extra kwargs forwarded to ``curl_cffi.Session.get``
                verbatim.

        Returns:
            The ``curl_cffi`` response object.

        Note:
            Governor-gated (``_govern()`` runs before I/O) and
            soft-block-observed; journal entry recorded when a journal
            is attached.
        """
        # governor gate BEFORE I/O: a blocked call must never open a socket
        self._govern()
        resp = self._session.get(
            url,
            headers=headers or page_headers(self.profile),
            impersonate=cast("BrowserTypeLiteral", self.impersonate),
            timeout=self.timeout,
            **kw,
        )
        self._note("GET", resp, kw)
        self._observe_governor_response(resp)
        return resp

    def post(self, url: str, *, data: Mapping[str, str] | None = None,
             headers: Mapping[str, str] | None = None,
             **kw: Any) -> creq.Response:
        """Governed, impersonated form POST; returns the curl_cffi response.

        Args:
            url: Absolute https URL on a facebook.com surface.
            data: Form fields; only their NAMES reach the journal entry,
                never their values (docs/12 §4 secret hygiene).
            headers: Optional header overrides; when None, the canonical
                page set from ``transport.headers.page_headers`` is applied.
            **kw: Extra kwargs forwarded to ``curl_cffi.Session.post``
                verbatim.

        Returns:
            The ``curl_cffi`` response object.

        Note:
            Governor-gated (``_govern()`` runs before I/O) and
            soft-block-observed; journal entry recorded when a journal
            is attached.
        """
        # governor gate BEFORE I/O: a blocked call must never open a socket
        self._govern()
        resp = self._session.post(
            url,
            data=dict(data) if data else None,
            headers=headers or page_headers(self.profile),
            impersonate=cast("BrowserTypeLiteral", self.impersonate),
            timeout=self.timeout,
            **kw,
        )
        self._note("POST", resp, {"body_fields": sorted((data or {}).keys())})
        self._observe_governor_response(resp)
        return resp

    def post_graphql(self, url: str, *, data: Mapping[str, str], friendly_name: str,
                     lsd: str | None, is_mutation: bool = False,
                     **kw: Any) -> creq.Response:
        """Governed GraphQL POST with the canonical header set (docs/04 §2).

        The single wire shape every persisted-query call takes: the
        ``graphql_headers`` set (content-type/origin/referer/x-fb-friendly-
        name/x-fb-lsd, docs/09 §1.1) layered over the impersonated TLS/h2
        identity. Mutations are paced against the separate, lower mutation
        budget (docs/10 §2 action ceilings, docs/15 §P9-1).

        Args:
            url: The /api/graphql/ endpoint URL.
            data: The full form body (friendly_name, doc_id, variables,
                fb_dtsg, lsd, ...); field NAMES only reach the journal.
            friendly_name: The operation's registry name; mirrored into
                the ``x-fb-friendly-name`` header as the real client does.
            lsd: The login-session-data CSRF token, or None to omit the
                ``x-fb-lsd`` header (logged-out pages carry no lsd).
            is_mutation: When True, the governor debits the separate
                state-mutating daily budget instead of the read budget.
            **kw: Extra kwargs forwarded to ``curl_cffi.Session.post``
                verbatim.

        Returns:
            The ``curl_cffi`` response object.
        """
        # governor gate BEFORE I/O — mutation calls debit their own budget
        self._govern(is_mutation=is_mutation)
        resp = self._session.post(
            url, data=dict(data),
            headers=graphql_headers(self.profile, friendly_name, lsd),
            impersonate=cast("BrowserTypeLiteral", self.impersonate), timeout=self.timeout, **kw)
        self._note("POST", resp, {"body_fields": sorted(data.keys())})
        self._observe_governor_response(resp)
        return resp

    # ------------------------------------------------------------- raw rupload seam
    def raw_post(self, url: str, *, data: bytes | dict[str, Any],
                 headers: Mapping[str, str], surface: str = "raw") -> creq.Response:
        """One raw POST through the curl_cffi session — for endpoints whose
        hand-built multipart bodies / custom headers the governed post() API
        cannot express (rupload transfers — docs/15 §P6-1).

        Deliberately NOT governor-gated: rupload chunk transfers are data
        plumbing, not user-visible mutations — the GraphQL publish that
        concludes each upload goes through post_graphql and carries the
        mutation budget. Journaled here centrally, so every raw call is
        auditable with its surface tag.

        Args:
            url: The rupload-family endpoint URL (rupload-ccu2-1.up.
                facebook.com and kin, docs/15 §P6-1).
            data: Raw body bytes or a field mapping, passed verbatim —
                byte-level upload framing is the caller's contract.
            headers: Caller-provided headers applied VERBATIM (X-Entity-*,
                Offset, X-FB-Region, ...); never merged with the canonical
                sets, because the rupload wire shape is header-complete by
                construction.
            surface: Journal tag identifying the calling surface, so raw
                traffic remains attributable in audit review.

        Returns:
            The ``curl_cffi`` response object.

        Note:
            Still soft-block-observed (empty-200/403/429 feed the governor)
            even though the pacing gate is skipped by design.
        """
        resp = self._session.post(
            url,
            data=data,
            headers=headers,
            impersonate=cast("BrowserTypeLiteral", self.impersonate),
            timeout=self.timeout,
        )
        self._note("POST", resp, {"surface": surface})
        self._observe_governor_response(resp)
        return resp

    def raw_get(self, url: str, *, headers: Mapping[str, str],
                surface: str = "raw") -> creq.Response:
        """One raw GET through the curl_cffi session (rupload resume probe).

        Same discipline as raw_post: no governor gate, journaled centrally
        with the surface tag. The rupload GET probes the transfer offset
        (``GET .../fb_video/<session-key> -> {"offset"}``, docs/15 §P6-1
        stage 2), which is chunk plumbing rather than a user-visible read.

        Args:
            url: The rupload-family endpoint URL.
            headers: Caller-provided headers applied VERBATIM.
            surface: Journal tag identifying the calling surface.

        Returns:
            The ``curl_cffi`` response object.

        Note:
            Still soft-block-observed (empty-200/403/429 feed the governor).
        """
        resp = self._session.get(
            url,
            headers=headers,
            impersonate=cast("BrowserTypeLiteral", self.impersonate),
            timeout=self.timeout,
        )
        self._note("GET", resp, {"surface": surface})
        self._observe_governor_response(resp)
        return resp

    # ------------------------------------------------------------------- journaling
    def _note(self, method: str, resp: Any, ctx: dict[str, Any]) -> None:
        """Record one journal entry: metadata only, no secret values.

        Args:
            method: Wire verb ("GET"/"POST").
            resp: The response object; URL, status, and content length are
                read, nothing else.
            ctx: Context keys (body field names or surface tag) — callers
                are responsible for passing names, never values.
        """
        if self.journal is None:
            return
        self.journal.record({
            "ts": time.time(),
            "method": method,
            "url": str(resp.url),
            "status": resp.status_code,
            "content_length": len(resp.content or b""),
            **({"ctx": ctx} if ctx else {}),
        })
