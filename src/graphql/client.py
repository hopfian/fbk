"""GraphQLClient — /api/graphql/ persisted queries (docs/04).

NETWORK BOUNDARY (docs/04 §1; transport live-confirmed docs/15 §1):
  POST https://www.facebook.com/api/graphql/
  Content-Type: application/x-www-form-urlencoded
  body: fb_api_req_friendly_name=<name>&fb_api_caller_class=RelayModern
        &variables=<json>&doc_id=<persisted id>&fb_dtsg=<token>&lsd=<token>
        &server_timestamps=true&q=<seq>

ARCHITECTURE:
  The wire carries no query text at all — persisted queries resolve
  doc_id → compiled AST server-side, so arbitrary queries are
  inexpressible and the registry (docs/04 §4) is the only id source.
  Responses are concatenated NDJSON Relay frames (docs/04 §3.2),
  deep-merged into one payload by ``parsing.merge_docs``; a merged
  payload with an empty ``errors`` list IS success. A non-empty list
  classifies first-error-wins into the typed taxonomy of
  ``graphql/errors.py``; CHECKPOINT_REQUIRED and RATE_LIMITED_SUSPECTED
  additionally feed the transport's RequestGovernor (docs/15 §P9-1) so
  the containment cooldown engages before the exception propagates.

  DTSG rejection auto-refresh (docs/04 §3.4): codes 1357051/1677047 are
  the only class where a silent re-bootstrap + retry is legitimate.
  ``call`` invalidates the persistent token cache (docs/16 §P11-1),
  re-bootstraps, and retries exactly once; a hard NOT_LOGGED_IN
  (1357004) propagates immediately.

  Dry-run mode (``--dry-run``, docs/04 §1-§2 request shape): when the
  client is constructed with ``dry_run=True``, call()/call_raw() build
  the complete request plan, print it (variables redacted via
  journal.recorder.redact_entry — the ALL_SECRETS vocabulary,
  docs/11 §7), and raise :class:`DryRunComplete` — the transport edge,
  the governor (docs/11 §8: budgets count wire volume, and a plan
  reaches no edge), the journal, and the ``q`` counter are all left
  untouched.

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, NoReturn

import constants as C
from auth.bootstrap import Bootstrap
from healing import KIND_DOC_ID_RETRY
from journal.recorder import redact_entry
from transport.session import FBTransport

from .errors import (
    CheckpointError,
    DocIdStaleError,
    DryRunComplete,
    GraphQLProtocolError,
    NotLoggedInError,
    RateLimitedError,
    RegistryMissError,
)
from .parsing import merge_docs, parse_incremental, strip_legacy_prefix
from .registry import DocIdRegistry

# Error codes meaning "the DTSG was rejected" — the only class where a
# silent re-bootstrap + retry is legitimate (docs/04 §3.4: 1357051
# session/CSRF epoch expired, 1677047 invalid/expired fb_dtsg variant).
_DTSG_REJECTION_CODES = frozenset({1357051, 1677047})


class GraphQLClient:
    """Executes persisted queries against /api/graphql/ with typed errors.

    Holds the live :class:`Bootstrap` (CSRF pair) and an optional
    ``DocIdRegistry`` enabling name-based dispatch. Every call passes
    through ``FBTransport.post_graphql`` — governor pacing, journaling,
    and fingerprint coherence are structural, not per-call-site
    (docs/15 §P9-1). Mutations are detected by the ``Mutation``
    friendly-name suffix and count against the separate governor
    mutation budget.

    With ``dry_run=True`` the same construction and dispatch run, but
    every call stops at the plan: full request printed (variables
    redacted, docs/11 §7), :class:`DryRunComplete` raised, and nothing
    — transport, governor, journal, ``q`` — consumed (docs/11 §8).
    """

    def __init__(
        self,
        transport: FBTransport,
        bootstrap: Bootstrap,
        registry: DocIdRegistry | None = None,
        endpoint: str = C.GRAPHQL_ENDPOINT,
        max_refresh: int = 1,
        dry_run: bool = False,
        healer: Any | None = None,
    ):
        """Construct the client around one harvested bootstrap.

        Args:
            transport: Cookie-authenticated transport; supplies the ``q``
                sequence counter and the governor escalation hooks.
            bootstrap: Fresh page bootstrap supplying fb_dtsg/lsd.
            registry: Optional registry enabling ``call_by_name``.
            endpoint: Persisted-query endpoint (docs/04 §1); the mobile
                surface variant uses the same protocol shape.
            max_refresh: Automatic re-bootstrap retries allowed on DTSG
                rejection; the live contract is exactly one.
            dry_run: Plan-and-abort mode — every call()/call_raw() (and
                call_by_name transitively) prints the request plan and
                raises :class:`DryRunComplete` instead of sending. The
                flag is inert by default; nothing about the live wire
                path changes when it is absent.
            healer: Optional self-healing coordinator
                (:class:`healing.HealingContext`, wired by Session). When
                attached, a :class:`RegistryMissError` on by-name
                dispatch and a :class:`DocIdStaleError` mid-call trigger
                the capped, cooled-down registry re-harvest + one retry
                (src/healing.py); inert when absent.

        Raises:
            NotLoggedInError: The bootstrap carries no usable fb_dtsg —
                the session is unusable for GraphQL; re-auth.
        """
        # Unwrap-before-check (Bootstrap.dtsg): an empty SecretStr("")
        # wrapper passes ``is not None`` but carries no token — the
        # constructor must reject it here, not on the wire (docs/03 §3).
        if bootstrap.dtsg() is None:
            raise NotLoggedInError("bootstrap produced no fb_dtsg — not logged in")
        self.transport = transport
        self.bootstrap = bootstrap
        self.registry = registry
        self.endpoint = endpoint
        self._max_refresh = max_refresh
        self.dry_run = dry_run
        self._healer = healer
        # set by Session to enable the persistent token cache (docs/16 §P11-1)
        self._token_cache: Any | None = None

    # ------------------------------------------------------------------- plumbing
    def _status_guard(self, resp: Any) -> None:
        """Map transport-level statuses to typed errors (redirects, 403/429).

        ``resp`` is a curl_cffi Response (or the structural
        StubTransportResponse in tests): status_code / headers / url are
        duck-typed.

        Raises:
            CheckpointError: Redirect landing on ``/checkpoint``.
            NotLoggedInError: Redirect landing on ``/login``.
            RateLimitedError: 403 (IP/edge-level refusal) or 429
                (explicit rate limit) — both are soft-block signals the
                governor cooldown is designed for (docs/10 §3).
        """
        if resp.status_code in (301, 302):
            loc = resp.headers.get("location", "")
            if "/checkpoint" in loc:
                raise CheckpointError(f"redirected to checkpoint: {loc}")
            if "/login" in loc:
                raise NotLoggedInError(f"redirected to login: {loc}")
        if resp.status_code == 403:
            raise RateLimitedError("403 — IP/edge-level refusal")
        if resp.status_code == 429:
            raise RateLimitedError("429 — explicit rate limit")

    def _error_guard(self, payload: Mapping[str, Any], doc_id: str, friendly: str) -> None:
        """Classify a structured ``errors`` array — first error wins.

        An empty or absent ``errors`` list returns silently: that IS the
        success path. The first entry classifies the whole response via
        ``constants.GRAPHQL_ERROR_CODES`` (docs/04 §3.4 catalogue);
        CHECKPOINT_REQUIRED and RATE_LIMITED_SUSPECTED additionally
        escalate into the transport's governor so the containment
        cooldown engages (docs/15 §P9-1, docs/11 §5 — disengage, don't
        push).

        Raises:
            NotLoggedInError: NOT_LOGGED_IN, or the DTSG-rejection pair
                SESSION_EXPIRED_DTSG / INVALID_DTSG.
            DocIdStaleError: DOC_ID_UNKNOWN — the deploy rolled this
                doc_id; re-harvest the registry.
            CheckpointError: CHECKPOINT_REQUIRED, after governor escalation.
            RateLimitedError: RATE_LIMITED_SUSPECTED, after escalation.
            GraphQLProtocolError: Any unmapped structured error, with the
                full envelope attached (e.g. 1675012 variable coercion,
                docs/15 §4).
        """
        errs = payload.get("errors") or []
        for e in errs:
            code = e.get("code")
            label = C.GRAPHQL_ERROR_CODES.get(code, "UNKNOWN")
            msg = str(e.get("message", ""))
            if label == "NOT_LOGGED_IN":
                raise NotLoggedInError(f"GraphQL {code}: {msg}", code=code, raw=e)
            if label in ("SESSION_EXPIRED_DTSG", "INVALID_DTSG"):
                raise NotLoggedInError(f"GraphQL {code}: dtsg rejected — {msg}",
                                       code=code, raw=e)
            if label == "DOC_ID_UNKNOWN":
                raise DocIdStaleError(
                    f"doc_id {doc_id} for {friendly!r} rejected — re-harvest",
                    code=code, raw=e)
            if label == "CHECKPOINT_REQUIRED":
                # escalation: a checkpoint flips the governor into a long
                # cooldown (docs/11 §5 containment — disengage, don't push)
                gov = getattr(self.transport, "governor", None)
                if gov is not None:
                    gov.observe_checkpoint()
                raise CheckpointError(f"GraphQL {code}: {msg}", code=code, raw=e)
            if label == "RATE_LIMITED_SUSPECTED":
                gov = getattr(self.transport, "governor", None)
                if gov is not None:
                    gov.observe_soft_block()
                raise RateLimitedError(f"GraphQL {code}: {msg}", code=code, raw=e)
            # unknown structured error — surface it with the full envelope
            raise GraphQLProtocolError(
                f"{friendly}: {msg} (code={code})", code=code, raw=errs)

    # ------------------------------------------------------------- public surface
    def _dry_run_stop(self, friendly_name: str, doc_id: str,
                      variables: Mapping[str, Any],
                      is_mutation: bool) -> NoReturn:
        """Print the full request plan and abort the call (dry-run mode).

        The plan mirrors the canonical /api/graphql/ request shape
        (docs/04 §1-§2) minus everything secret-bearing: fb_dtsg and
        lsd are never printed, and the variables pass through
        ``journal.recorder.redact_entry`` — the ALL_SECRETS vocabulary
        (docs/11 §7) — so token-named fields (idempotence tokens, the
        xs-bound credentials, ...) surface as salted fingerprints,
        never values. Everything else in the variables is protocol
        data, safe to print exactly as ``templates show`` does.

        Nothing here touches the transport: no send, no governor tick
        (docs/11 §8 volume discipline — the pacing and budget gates
        count wire volume, and a plan reaches no edge), no journal
        entry, and the ``q`` counter is NOT consumed (``next_q`` is a
        side effect on the transport, called only when the real body
        is assembled; the estimate lines below say what the call WOULD
        debit, without debiting anything).

        Args:
            friendly_name: Relay operation name for the plan header.
            doc_id: Persisted-query id resolved for the operation.
            variables: The already-substituted GraphQL variables.
            is_mutation: The friendly-name suffix classification
                (docs/15 §P9-1: mutations debit the separate, lower
                governor mutation budget).

        Raises:
            DryRunComplete: Always — the success sentinel run_command
                converts to exit 0.
        """
        plan: dict[str, Any] = {
            "friendly_name": friendly_name,
            "doc_id": doc_id,
            "endpoint": self.endpoint,
            "is_mutation": is_mutation,
            "would_debit": "1 mutation" if is_mutation else "1 read",
            "variables": redact_entry(dict(variables)),
        }
        print("--- dry-run request plan ---")
        print(f"friendly_name: {friendly_name}")
        print(f"doc_id: {doc_id}")
        print(f"endpoint: {self.endpoint}")
        print(f"classification: {'mutation' if is_mutation else 'read'} "
              f"(would debit: {plan['would_debit']})")
        print("variables: " + json.dumps(plan["variables"],
                                          ensure_ascii=False, default=str,
                                          sort_keys=True))
        raise DryRunComplete(plan)

    def call_raw(self, friendly_name: str, doc_id: str, variables: Mapping[str, Any],
                 *, caller_class: str = "RelayModern",
                 extra_body: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
        """Execute one persisted query; return ALL streamed docs.

        The body replicates the real client's canonical parameter set
        (docs/04 §2); ``q`` is a page-session monotonic counter the
        server tolerates rather than a strict nonce. In dry-run mode
        the call stops BEFORE anything is assembled that consumes wire
        state: the plan is printed and :class:`DryRunComplete` raised.

        Args:
            friendly_name: Relay operation name; the ``Mutation`` suffix
                routes the call onto the governor's separate mutation
                budget (docs/15 §P9-1).
            doc_id: Persisted-query id resolved from the registry.
            variables: GraphQL variables object, JSON-encoded compactly.
            caller_class: Client layer identifier (docs/04 §2).
            extra_body: Surface-specific form params merged last.

        Returns:
            Every complete JSON document from the streamed NDJSON body,
            in arrival order (docs/04 §3.2).

        Raises:
            DryRunComplete: Dry-run mode — the plan is on stdout and
                nothing was sent or debited.
            CheckpointError: Redirect to ``/checkpoint``.
            NotLoggedInError: Redirect to ``/login``.
            RateLimitedError: 403/429, or a 200-but-unparseable body —
                the soft-block signature (docs/10 §3).
        """
        is_mutation = friendly_name.endswith("Mutation")
        if self.dry_run:
            self._dry_run_stop(friendly_name, str(doc_id), variables, is_mutation)
        body: dict[str, str] = {
            "fb_api_req_friendly_name": friendly_name,
            "fb_api_caller_class": caller_class,
            "variables": json.dumps(variables, separators=(",", ":")),
            "doc_id": str(doc_id),
            "server_timestamps": "true",
            "q": self.transport.next_q(),
        }
        if self.bootstrap.fb_dtsg is not None:
            body["fb_dtsg"] = self.bootstrap.dtsg() or ""
        lsd = self.bootstrap.lsd_value()
        if lsd:
            body["lsd"] = lsd
        if extra_body:
            body.update(extra_body)

        resp = self.transport.post_graphql(
            self.endpoint, data=body, friendly_name=friendly_name, lsd=lsd,
            is_mutation=is_mutation)
        self._status_guard(resp)
        text = strip_legacy_prefix(resp.text)
        docs = parse_incremental(text)
        if not docs:
            raise RateLimitedError(
                f"200-but-unparseable GraphQL response ({len(text)} chars) — "
                "soft-block signature (docs/10 §3)")
        return docs

    def call(self, friendly_name: str, doc_id: str, variables: Mapping[str, Any],
             *, caller_class: str = "RelayModern",
             extra_body: Mapping[str, str] | None = None) -> dict[str, Any]:
        """Execute one persisted query; return the deep-merged payload.

        On DTSG rejection (codes 1357051/1677047, docs/04 §3.4), the
        client re-bootstraps once and retries — automatic token refresh.
        Only the DTSG-rejection class is curable that way; a hard
        NOT_LOGGED_IN (1357004) propagates immediately.

        Args:
            friendly_name: Relay operation name (see :meth:`call_raw`).
            doc_id: Persisted-query id from the registry.
            variables: GraphQL variables object.
            caller_class: Client layer identifier (docs/04 §2).
            extra_body: Surface-specific form params merged last.

        Returns:
            The deep-merged Relay payload; an empty ``errors`` list IS
            the success contract.

        Raises:
            DryRunComplete: Dry-run mode — the plan was printed and
                nothing was sent (raised from :meth:`call_raw` before
                any wire state is consumed).
            NotLoggedInError: Session invalid and refresh failed or was
                not applicable.
            CheckpointError: CHECKPOINT_REQUIRED envelope or redirect.
            RateLimitedError: RATE_LIMITED_SUSPECTED envelope or soft-block.
            DocIdStaleError: The deploy rolled this doc_id and the
                self-healing re-harvest could not produce a different
                fresh id (healing disabled, cooldown active, harvest
                failed, or the fresh registry carries the same id).
            GraphQLProtocolError: Unmapped structured error (docs/15 §4).
        """
        doc_id_healed = False
        for attempt in range(self._max_refresh + 1):
            docs = self.call_raw(friendly_name, doc_id, variables,
                                 caller_class=caller_class, extra_body=extra_body)
            merged = merge_docs(docs)
            try:
                self._error_guard(merged, str(doc_id), friendly_name)
            except NotLoggedInError as exc:
                # only genuine DTSG rejections can be cured by re-bootstrapping;
                # a hard NOT_LOGGED_IN (1357004) propagates immediately
                if (attempt < self._max_refresh
                        and exc.code in _DTSG_REJECTION_CODES
                        and self._refresh_tokens()):
                    continue
                raise
            except DocIdStaleError as exc:
                # Self-healing (src/healing.py): a 1570245-family rejection
                # means the deploy rolled this registration. One capped,
                # cooled-down re-harvest, then exactly one retry — and only
                # when the fresh registry actually carries a DIFFERENT id
                # (re-firing the same body against the same dead id fails
                # identically; that shape drift is a caller bug, not
                # staleness — docs/04 §10). Dry-run never heals: a plan
                # touches no edge, so a harvest would violate docs/11 §8.
                if (self.dry_run or self._healer is None or doc_id_healed
                        or attempt >= self._max_refresh):
                    raise
                doc_id_healed = True
                fresh = self._healer.refresh_registry()
                if fresh is None:
                    raise
                self.registry = fresh
                new_id = fresh.get(friendly_name)
                if new_id is None or new_id == str(doc_id):
                    raise
                self._healer.record(
                    KIND_DOC_ID_RETRY,
                    f"doc_id {doc_id} rejected for {friendly_name!r} "
                    f"({exc.code})",
                    f"retrying once with the fresh id {new_id}")
                doc_id = new_id
                continue
            return merged
        return merged  # pragma: no cover - unreachable

    def call_by_name(self, friendly_name: str, variables: Mapping[str, Any],
                     **kw: Any) -> dict[str, Any]:
        """Registry-backed convenience: resolve doc_id, then call.

        Args:
            friendly_name: Operation name to look up in the attached
                registry.
            variables: GraphQL variables object.
            **kw: Forwarded to :meth:`call` (caller_class, extra_body).

        Returns:
            The deep-merged Relay payload.

        Raises:
            RegistryMissError: No registry attached to this client (the
                operator ran without the data/ registry), or the name is
                absent — including after the self-healing re-harvest
                (the operation is lazy-loaded and outside the homepage
                harvest's reach; extend the harvest per docs/13 §2).
        """
        if self.registry is None:
            raise RegistryMissError("no registry attached to this client")
        try:
            doc_id = self.registry.doc_id(friendly_name)
        except RegistryMissError:
            # Self-healing (src/healing.py): one capped, cooled-down
            # re-harvest, then re-resolve against the fresh registry.
            # Dry-run never heals — a plan touches no edge (docs/11 §8),
            # so triggering a bundle harvest from plan mode would debit
            # exactly what dry-run exists to avoid.
            if self.dry_run or self._healer is None:
                raise
            fresh = self._healer.refresh_registry()
            if fresh is None:
                raise
            self.registry = fresh
            doc_id = fresh.doc_id(friendly_name)
        return self.call(friendly_name, doc_id, variables, **kw)

    # ---------------------------------------------------------- token refresh
    def _refresh_tokens(self) -> bool:
        """Re-bootstrap the session and pick up fresh DTSG/lsd tokens.

        Also invalidates the persistent token cache (docs/16 §P11-1) so
        stale tokens are never reused after a rejection. The transport's
        cookie jar absorbs any Set-Cookie rotation during the re-bootstrap
        (e.g. datr/lifetime updates) automatically.
        """
        from auth.bootstrap import bootstrap_homepage
        fresh = bootstrap_homepage(self.transport, self.transport.cookies)
        if fresh.state.value != "logged_in" or fresh.fb_dtsg is None:
            return False
        self.bootstrap = fresh
        # invalidate the persistent cache so the next Session picks up
        # the fresh tokens (or re-bootstraps on its own if this process dies)
        cache = self._token_cache
        if cache is not None:
            cache.invalidate()
            cache.save(fresh)
        return True
