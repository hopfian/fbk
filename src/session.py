"""Session facade: one object wiring cookies + transport + auth + GraphQL.

This is the single dependency every surface service takes — which makes the
services trivially testable against a stub with the same shape
(tests/fakes.py). Nothing else in the package constructs an FBTransport,
GraphQLClient, or TokenCache directly: token lifecycle, disk caching, and
governor wiring stay in exactly one place (docs/12 §3 session contract).

ARCHITECTURE:

  Composition order (constructor): cookie jar → profile → journal →
  governed transport → token cache. Every expensive step after the
  constructor is LAZY:

  * ``registry`` parses the ~180KB registry JSON on FIRST access only —
    commands that never run GraphQL skip the parse entirely.
  * ``bootstrap`` is cache-first: a fresh persistent token-cache entry
    satisfies it with ZERO HTTP requests (docs/16 §P11-1); only on cache
    miss does the full homepage bootstrap run.

  The persistent token cache (docs/16 §P11-1) is the single biggest
  volume-reduction lever: without it, every CLI invocation bootstrapped
  the full homepage (~2-5MB, 1 request) just to harvest tokens. With it,
  one command = one API call — the bootstrap download happens only when
  the 15-minute TTL expires or a token is rejected.

USER-DOC ANCHOR: cli/docs/03-session-and-auth.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from auth.bootstrap import Bootstrap, bootstrap_homepage
from auth.state import LoginState
from config import Config
from governor import default_governor
from graphql.errors import DryRunRawSeamError
from graphql.registry import DocIdRegistry
from healing import HealingContext
from journal.recorder import JSONLJournal
from token_cache import TokenCache
from transport.cookies import load_netscape
from transport.profile import load_or_default

if TYPE_CHECKING:
    # Heavy/transitive imports stay OUT of the eager import graph (startup
    # audit 2026-09: ~93% of per-invocation CLI cost is import overhead).
    # FBTransport and GraphQLClient are resolved lazily below; the
    # annotations resolve under `from __future__ import annotations`.
    from graphql.client import GraphQLClient
    from transport.session import FBTransport as _FBTransportClass

# The transport class, resolved on FIRST Session construction. Kept as a
# module attribute (not a local import in __init__) so tests can monkey-
# patch ``session.FBTransport`` with a transport stub while keeping the
# module import free of the curl_cffi import chain entirely.
FBTransport: type[_FBTransportClass] | None = None


def _resolve_transport() -> type[_FBTransportClass]:
    """Return the live ``FBTransport`` class, importing it exactly once.

    Reads the module attribute rather than re-importing so a monkeypatched
    ``session.FBTransport`` (tests/unit/test_session_lifecycle.py) wins
    over the freshly imported class on every call.
    """
    global FBTransport
    if FBTransport is None:
        from transport import session as _transport_session
        FBTransport = _transport_session.FBTransport
    return FBTransport


class Session:
    """An authenticated Facebook web session bound to one cookie jar.

    Lazily bootstraps on first use, caches tokens (in-memory AND on disk —
    the Phase 11 persistent token cache), and exposes:
      * session.bootstrap -> Bootstrap (DTSG/lsd/preloads/identity)
      * session.graphql   -> GraphQLClient (registry-backed)
      * session.registry  -> DocIdRegistry

    The persistent token cache (docs/16 §P11-1) is the single biggest
    volume-reduction lever: without it, every CLI invocation bootstrapped
    the full homepage (~2-5MB, 1 request) just to harvest tokens. With it,
    one command = one API call — the bootstrap download happens only when
    the 15-minute TTL expires or a token is rejected.

    ``dry_run=True`` (``--dry-run``) forwards the mode to the GraphQL
    client (plan-and-abort, docs/11 §8) and installs the raw-seam
    refusal guard on the transport; the default False construction is
    exactly the pre-flag Session.
    """

    def __init__(self, config: Config | None = None, *,
                 journal_name: str | None = "session",
                 dry_run: bool = False):
        """Wire the full session stack around one cookie jar.

        Args:
            config: Resolved runtime config; defaults to
                :meth:`config.Config.discover` (FBK_ROOT / FBK_COOKIES
                overrides apply).
            journal_name: Basename of the JSONL journal under ``state/``;
                ``None`` disables journaling entirely (used by
                ``--no-journal`` runs and lightweight probes).
            dry_run: ``--dry-run`` mode (forwarded to the GraphQL
                client and the raw-seam guard below); inert by
                default — a False session is byte-identical to the
                pre-flag constructor.

        Raises:
            CookieLoadError: If the cookie jar is absent or carries zero
                parseable ``c_user``/``xs`` rows — no session can be built
                around an empty jar.
        """
        self.config = config or Config.discover()
        self.cookies = load_netscape(self.config.cookies_path)
        profile = load_or_default(str(self.config.profile_path)
                                  if self.config.profile_path else None)
        self.journal: JSONLJournal | None = (
            JSONLJournal(self.config.journal_file(journal_name))
            if journal_name else None)
        # lazy: importing transport.session here (never at module level)
        # keeps `import session` off the curl_cffi import chain entirely;
        # the resolver honors a monkeypatched session.FBTransport.
        self.transport = _resolve_transport()(
            cookies=self.cookies, profile=profile, journal=self.journal,
            timeout=self.config.timeout, impersonate=self.config.impersonate,
            governor=default_governor())
        self.dry_run = dry_run
        if dry_run:
            self._install_raw_seam_guard()
        self._registry: DocIdRegistry | None = None
        self.token_cache = TokenCache(
            self.config.journal_dir / "token_cache.json")
        # the self-healing coordinator (src/healing.py): capped, cooled
        # down, log-backed recovery for registry rotation, corrupt state
        # files, and transport blips. Wired into the GraphQL client below.
        self.healer = HealingContext(
            self.config, log_path=self.config.journal_dir / "healing.jsonl")
        self.transport.healing_log = self.healer.log
        self._bootstrap: Bootstrap | None = None
        self._graphql: GraphQLClient | None = None

    def _install_raw_seam_guard(self) -> None:
        """Refuse raw-seam HTTP for this Session (dry-run mode).

        The upload/video surfaces bypass GraphQLClient through
        ``FBTransport.raw_post``/``raw_get`` (hand-built multipart and
        rupload dialects, docs/15 §P5-1/P6-1) — a seam no request plan
        can describe. Session, the single place a transport is ever
        constructed, swaps both methods for a refusal raising
        :class:`DryRunRawSeamError` (run_command maps it to exit 1).
        Instance-attribute shadowing keeps the transport's declared
        type intact for GraphQLClient and every other consumer; only
        the two raw-seam entry points are covered, by design — the
        GraphQL calls remain the dry-run coverage target, and governed
        transport reads (bootstrap) still work so plan construction
        gets real tokens and preloads.

        The refusing callables mirror the exact keyword signatures of
        the methods they replace (``data`` is required on raw_post,
        absent on raw_get) so the shadow is never the reason a call
        fails: it fails with the typed sentinel or not at all.
        """
        def refuse_post(url: str, *, data: bytes | dict[str, Any],
                        headers: Mapping[str, str],
                        surface: str = "raw") -> Any:
            raise DryRunRawSeamError(
                "dry-run covers GraphQL calls only — this command uses "
                f"the raw HTTP seam ({url}), which has no request plan")

        def refuse_get(url: str, *, headers: Mapping[str, str],
                       surface: str = "raw") -> Any:
            raise DryRunRawSeamError(
                "dry-run covers GraphQL calls only — this command uses "
                f"the raw HTTP seam ({url}), which has no request plan")

        # method-assign is deliberate here — the instance-level shadow IS
        # the guard. No cast can express it (the declared transport type
        # stays FBTransport for GraphQLClient and every other consumer);
        # the signatures above mirror the replaced methods exactly so the
        # shadow is behavior-identical up to the refusal.
        self.transport.raw_post = refuse_post  # type: ignore[method-assign]
        self.transport.raw_get = refuse_get  # type: ignore[method-assign]

    @property
    def registry(self) -> DocIdRegistry:
        """The doc-id registry, parsed on FIRST access and cached.

        Deliberately lazy (audit 2026-09): from_assets parses the
        180KB v3 registry JSON; commands that never run GraphQL
        (governor status, auth whoami, registry doc-ids...) shouldn't
        pay ~12ms of parse + 180KB of I/O per invocation.
        """
        if self._registry is None:
            self._registry = DocIdRegistry.from_assets(self.config.assets_dir)
        return self._registry

    def reload_registry(self) -> DocIdRegistry:
        """Drop the memoized registry and re-parse from assets.

        The self-healing registry path uses this after a re-harvest rewrites
        ``doc_id_registry_v3.json``: the next access re-reads the directory
        and picks up the rotated ids. Also the hook a test uses to swap a
        fixture registry in between calls.
        """
        self._registry = None
        return self.registry

    # ---------------------------------------------------------------- bootstrap
    def bootstrap(self, *, force: bool = False) -> Bootstrap:
        """Return the page bootstrap, going to the network only on cache miss.

        Check the persistent token cache first: when a fresh entry exists
        (within the 15-minute TTL), build the Bootstrap from the cached
        values without making any HTTP request. Only on cache miss (or
        explicit ``force=True``) does the full homepage bootstrap run.

        Args:
            force: Invalidate the cache and re-run the full bootstrap —
                the token-refresh path (a rejected DTSG triggers this via
                :meth:`refresh`).

        Returns:
            The session's ``Bootstrap`` (state, fb_dtsg, lsd, identity,
            revision, preloads); memoized per Session after the first call.

        Note:
            A cached hit constructs the Bootstrap with state
            ``LOGGED_IN`` and a ``token_cache`` marker — the on-disk entry
            only ever exists for a previously verified login, and the
            GraphQL client's auto-refresh still catches revoked tokens.
            A corrupt cache file fails soft to the full bootstrap (the
            discard IS the heal) and the coordinator records a
            ``token-cache-rebuild`` event so the cost is auditable.
        """
        if force:
            self.token_cache.invalidate()
        if self._bootstrap is not None:
            return self._bootstrap

        # cache-first: a fresh entry means ZERO bootstrap requests
        cached, reason = self.token_cache.load_diagnosed()
        if cached is not None:
            self._bootstrap = Bootstrap(
                state=LoginState.LOGGED_IN,
                fb_dtsg=cached.fb_dtsg,
                lsd=cached.lsd,
                user_id=cached.user_id,
                user_name=cached.user_name,
                revision=cached.revision,
                markers_seen=["token_cache"],
            )
            return self._bootstrap
        if reason in ("corrupt", "invalid-shape"):
            # a damaged cache file was discarded: the full bootstrap below
            # regenerates it — record the heal so the extra request is
            # auditable (self-healing visibility, src/healing.py)
            self.healer.record_token_cache_rebuild(
                f"token_cache.json {reason} — discarded")

        # cache miss: full bootstrap (the expensive path)
        self._bootstrap = bootstrap_homepage(self.transport, self.cookies)
        self.token_cache.save(self._bootstrap)
        if self.journal:
            self.journal.session_start(
                {"bootstrap": self._bootstrap.safe_dict()})
        return self._bootstrap

    @property
    def graphql(self) -> GraphQLClient:
        """The persisted-query client, rebuilt whenever tokens refresh.

        The client's private ``_token_cache`` handle is wired to this
        Session's cache so the DTSG-rejection auto-refresh cycle (docs/04
        §3.4, docs/16 §P11-1) invalidates and re-saves the SAME disk entry
        the next CLI invocation will read — without this wiring each
        refresh would silently fork its own cache file. In dry-run mode
        the client is built with the flag, so every call plans and
        stops instead of sending.
        """
        if self._graphql is None:
            # lazy: graphql.client imports transport.session (and its full
            # parse/parsing helpers) — only sessions that actually speak
            # GraphQL should pay for it.
            from graphql.client import GraphQLClient
            boot = self.bootstrap()
            self._graphql = GraphQLClient(
                self.transport, boot, registry=self.registry,
                endpoint=self.config.graphql_endpoint,
                dry_run=self.dry_run, healer=self.healer)
            self._graphql._token_cache = self.token_cache
        return self._graphql

    # ------------------------------------------------------------- convenience
    def user_id(self) -> str:
        """The viewer's user id (bootstrap first, c_user cookie fallback)."""
        return self.bootstrap().user_id or self.cookies.get("c_user", "")

    def refresh(self) -> Bootstrap:
        """Explicit token refresh (docs/12 §3 state machine).

        Drops the memoized Bootstrap and the persistent cache entry, then
        re-runs the full homepage bootstrap. Returns the fresh Bootstrap.
        """
        return self.bootstrap(force=True)

    # ------------------------------------------------------------- lifecycle
    def close(self) -> None:
        """Release the underlying transport (idempotent, safe to re-call).

        The token cache and governor persist to disk on their own; the
        journal appends per-line so nothing needs flushing. Closing the
        Session only closes the transport's pooled connections.
        """
        self.transport.close()

    def __enter__(self) -> Session:
        """Context-manager entry: the Session itself."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Context-manager exit: always closes the transport."""
        self.close()
