"""Surface services: the command-facing API per Facebook surface.

Each service encapsulates one surface family from docs/02-endpoint-surface-map.md
(friendly names, variable assembly, response typing) and never touches HTTP
directly — every wire interaction delegates to ``Session``/``GraphQLClient``,
which keeps the services fully testable against recorded fixtures
(tests/fakes.py).

ARCHITECTURE:

  Governor-transparent delegation (docs/10 §7, docs/15 §P9-1):
    Surfaces hold no pacing logic of their own. Every request they issue
    funnels through the session's GraphQL client and transport, where
    ``RequestGovernor`` enforces the lognormal inter-arrival discipline and
    the hourly/daily/mutation budgets. A surface therefore cannot burst by
    accident — the P8-1 lesson is structural here, not behavioural.

  Template semantics (docs/15 §P2-3/P4 wire shapes):
    The ``*_TEMPLATE`` / ``DEFAULT_*_VARIABLES`` dicts declared at module
    scope across the surface package are read-only wire blueprints captured
    from live bundles. Callers deep-copy them before substituting per-call
    values; the module-level originals are never mutated, so the captured
    shapes stay byte-comparable against future harvests.

  Config-driven data dir:
    ``_data_dir`` resolves ``cli/data`` through ``Config.discover()`` so
    fixture/asset paths remain portable across machines; no surface hardcodes
    a filesystem location.

  Shared-helper consolidation:
    This module also hosts the private helpers shared verbatim by 2+ surface
    modules (hoisted here so each piece of wire plumbing lives in exactly one
    place): the data-dir resolver, the payload walker, the captured-template
    reaction predicate, the captured-mutation template loader (the canonical
    ``load_template`` every surface write path delegates to), the placeholder
    template filler, the minified-product attribution/mutation-id minters,
    and the page-fetch/preload-harvest seam every HTML-harvesting service
    monkeypatches in offline tests.

CALIBRATION NOTES:

  * The ``_fetch``/``_preloads`` seam implements the docs/15 §P2-2 discovery
    as the package-wide read strategy: server-side-rendered pages embed
    preloader registrations carrying the exact variables the server itself
    used, and replaying them verbatim is the most faithful, bypass-resistant
    read available.
  * ``_mutation_doc_id`` prefers the live-verified ``KNOWN_MUTATIONS`` ids
    (docs/15 §P2-3 et seq. — each was verified end-to-end) over the larger
    harvested registry, whose entries are bundle-decoded but not all fired.

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import copy
import json
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import constants as C
from auth.bootstrap import PreloadEntry, extract_preload_registry

if TYPE_CHECKING:  # pragma: no cover
    from curl_cffi.requests import Response as CurlResponse

from graphql.client import GraphQLClient
from graphql.errors import RegistryMissError
from graphql.registry import DocIdRegistry
from session import Session


def _data_dir() -> Path:
    """Resolve the package data directory (``cli/data/``) via ``Config``.

    Delegates to :meth:`config.Config.discover` so the path follows the
    project root wherever it is checked out (FBK_ROOT override honored)
    rather than this module's import location.
    """
    from config import Config
    return Config.discover().data_dir


def _walk_preorder(root: Any) -> Iterator[dict[str, Any]]:
    """Yield every dict in the tree, document order (DFS pre-order).

    Relay payloads are arbitrarily nested (docs/04 §3.2 streamed responses
    are deep-merged into one tree), so field extraction is "first matching
    node in document order" — this walker guarantees that order without
    recursion-depth risk on ~1MB payloads.
    """
    stack: list[Any] = [root]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            children: list[Any] = list(cur.values())
        elif isinstance(cur, list):
            children = list(cur)
        else:
            continue
        stack.extend(reversed(children))


def _is_reaction(variables: dict[str, Any], reaction_id: str) -> bool:
    """Template predicate: match the capture whose feedback_reaction_id fits.

    Used to locate a specific reaction capture inside a recorded mutation
    template set; ``feedback_reaction_id`` is the UFI reaction discriminator
    (docs/15 §P2-3: "1635855486666999" = LIKE, "0" = remove).
    """
    inp = variables.get("input") or {}
    return inp.get("feedback_reaction_id") == reaction_id


# Captured-mutation template cache (docs/15 §P2-3): one parsed asset doc per
# module lifetime, keyed by basename. Every load_template hit deep-copies out
# of it, so the captured wire shapes stay byte-pristine for re-comparison
# against future harvests.
_TEMPLATE_CACHE: dict[str, Any] = {}


def load_template(asset_name: str, friendly: str,
                  *, where: Callable[[dict[str, Any]], bool] | None = None,
                  check_asset: bool = True) -> dict[str, Any]:
    """Deep-copied captured mutation variables for ``friendly``.

    The canonical captured-template loader every surface write path
    delegates to (docs/15 §P2-3/P3-3 verbatim-replay invariant): loads
    ``data/<asset_name>`` (cached at module level), scans its ``mutations``
    list for the entry with ``friendly_name == friendly``, and returns a
    deep copy of that entry's ``variables`` so callers can freely
    substitute fields without contaminating the cached capture. ``where``
    disambiguates same-named captures (e.g. like vs remove reactions —
    see ``_is_reaction``).

    Args:
        asset_name: Basename of the capture asset under ``data/``, e.g.
            ``"captured_mutations.json"``.
        friendly: The ``fb_api_req_friendly_name`` to match, e.g.
            ``"CometUFIFeedbackReactMutation"``.
        where: Optional predicate over each candidate entry's variables;
            the first friendly match that satisfies it wins.
        check_asset: Pre-verify the asset exists and raise a RuntimeError
            naming the path when absent (the comments/upload behavior);
            ``False`` lets the filesystem read raise its native
            FileNotFoundError instead (feed's historical behavior).

    Returns:
        A deep copy of the matched entry's ``variables`` dict (``{}`` when
        the capture carries none).

    Raises:
        RuntimeError: When ``check_asset`` is set and the asset file is
            missing, or when no ``mutations`` entry matches ``friendly``
            (and ``where``) — the asset is incomplete; re-capture per
            docs/15 §P2-3.
    """
    doc = _TEMPLATE_CACHE.get(asset_name)
    if doc is None:
        path = _data_dir() / asset_name
        if check_asset and not path.is_file():
            raise RuntimeError(f"captured template asset missing: {path}")
        doc = json.loads(path.read_text(encoding="utf-8"))
        _TEMPLATE_CACHE[asset_name] = doc
    for entry in doc.get("mutations", []):
        if entry.get("friendly_name") != friendly:
            continue
        variables = entry.get("variables") or {}
        if where is not None and not where(variables):
            continue
        return copy.deepcopy(variables)
    raise RuntimeError(
        f"no captured mutation {friendly!r} in {asset_name} — the asset is "
        "incomplete; re-capture per docs/15 §P2-3")


def _fill(template: Any, values: dict[str, Any]) -> Any:
    """Deep-copy a template, substituting ``"{key}"`` placeholder strings.

    Walks dicts/lists recursively and replaces any string of the exact form
    ``"{key}"`` with ``values[key]``; placeholders with no matching key pass
    through untouched, as do all non-string leaves. The template is never
    mutated — every level is rebuilt — so module-level wire captures stay
    pristine across calls.
    """
    if isinstance(template, str):
        if template.startswith("{") and template.endswith("}"):
            return values.get(template[1:-1], template)
        return template
    if isinstance(template, dict):
        return {key: _fill(value, values) for key, value in template.items()}
    if isinstance(template, list):
        return [_fill(item, values) for item in template]
    return template


def _attribution(surface: str) -> str:
    """Mint a minified-product attribution string (docs/15 §P2-3 wire shape).

    The trailing fields are (surface, product, entry mode, client ms
    timestamp, then the zero/empty tail) — the comma-joined form the web
    client ships as ``attribution_id_v2`` on UFI mutations.
    """
    return f"{surface},comet,via_cold_start,{int(time.time() * 1000)},0,0,,"


def _mutation_id() -> str:
    """Mint a fresh client dedup nonce per mutation call (uuid4-shaped).

    The relay ``client_mutation_id``: distinct per call so the server can
    deduplicate optimistic retries; deliberately NOT reused across surfaces.
    """
    return str(uuid.uuid4())


class Surface:
    """Base class for every surface service.

    Holds the shared ``Session`` dependency and exposes the three accessors
    every service reads through — the GraphQL client, the doc_id registry,
    and the registry lookup helper — so surface subclasses contain wire
    knowledge only, never transport wiring (docs/12 §3 layering).

    Args:
        session: The authenticated operator session; supplies ``transport``,
            ``graphql``, ``registry``, ``bootstrap()`` and ``user_id()`` to
            every subclass call.
    """

    def __init__(self, session: Session):
        self.session = session

    @property
    def client(self) -> GraphQLClient:
        """The session's GraphQL client (governor-paced, DTSG-managed)."""
        return self.session.graphql

    @property
    def registry(self) -> DocIdRegistry:
        """The harvested persisted-query registry (docs/13 harvest)."""
        return self.session.registry

    def doc_id(self, friendly_name: str) -> str:
        """Resolve a friendly name to its persisted doc_id.

        Self-healing (src/healing.py): a strict lookup miss triggers the
        coordinator's capped, cooled-down re-harvest exactly once, then
        re-resolves against the verified fresh registry (which the
        session adopts for every later lookup). The original
        :class:`RegistryMissError` propagates when healing is off, the
        caps/cooldown are spent, the harvest fails, or the operation is
        lazy-loaded outside the homepage harvest's reach. Dry-run mode
        never heals — a plan touches no edge (docs/11 §8).

        Args:
            friendly_name: The registry key, e.g.
                ``"CometNotificationsBadgeCountQuery"`` — the same string the
                web client ships as ``fb_api_req_friendly_name``.

        Returns:
            The numeric doc_id string for the current deploy's registry.

        Raises:
            RegistryMissError: When the name is absent — including after
                the self-healing re-harvest — or no healer is available
                and recovery cannot run.
        """
        try:
            return self.session.registry.doc_id(friendly_name)
        except RegistryMissError:
            healer = getattr(self.session, "healer", None)
            if healer is None or getattr(self.session, "dry_run", False):
                raise
            healed = healer.refresh_registry()
            if healed is None:
                raise
            fresh = cast(DocIdRegistry, healed)
            self.session.adopt_registry(fresh)
            return fresh.doc_id(friendly_name)

    # -------------------------------------------------- shared service plumbing
    def _fetch(self, url: str) -> str:
        """Fetch one HTML page through the session transport.

        The single network seam for HTML-harvesting services: offline tests
        monkeypatch this method to serve fixture HTML, so no preload-replay
        service ever performs real I/O in the test suite. Redirects are
        followed to match browser navigation semantics (e.g. ``/me`` → the
        canonical profile URL).

        Args:
            url: Absolute ``https://www.facebook.com/...`` page URL.

        Returns:
            The final response body as text (Comet HTML).

        Raises:
            Whatever the transport raises (RateLimitedError, NotLoggedInError,
                ...); callers that treat fetch failure as data catch broadly
                (see ``_preloads``).
        """
        # ``FBTransport.get`` returns the curl_cffi response object (its
        # documented seam contract); the annotation below pins ``.text``
        # to ``str`` without importing curl_cffi at runtime (docs/12 §2
        # lazy-import discipline — TYPE_CHECKING only).
        resp: CurlResponse = self.session.transport.get(url, allow_redirects=True)
        return resp.text

    def _preloads(self, url: str) -> list[PreloadEntry]:
        """Harvest the SSR preload registry from one page URL.

        Applies the docs/15 §P2-2 read strategy at the helper level: fetch
        the page and extract its ``{actorID, preloaderID, queryID, variables,
        queryName}`` registrations. A fetch or harvest failure — a
        logged-out shell carries no registry — degrades to ``[]`` so callers
        can fall back to baked defaults instead of crashing.

        Args:
            url: The page whose embedded preloads to harvest.

        Returns:
            The parsed preload entries, in document order; ``[]`` on any
            fetch/parse failure.
        """
        try:
            return extract_preload_registry(self._fetch(url))
        except Exception:
            return []

    @staticmethod
    def _find(entries: list[PreloadEntry],
              query_name: str) -> PreloadEntry | None:
        """Locate the first preload entry matching ``query_name``.

        Args:
            entries: Harvested preload entries (document order preserved).
            query_name: The friendly name to look up, e.g.
                ``"StoriesTrayRectangularRootQuery"``.

        Returns:
            The first matching entry, or ``None`` when the page did not
            preload that query — the caller applies its baked fallback.
        """
        for entry in entries:
            if entry.query_name == query_name:
                return entry
        return None

    def _mutation_doc_id(self, friendly: str) -> str:
        """Resolve a mutation's doc_id with live-verification preference.

        ``KNOWN_MUTATIONS`` ids first (docs/15 §P2-3/P3/P4 — each was fired
        and verified end-to-end against the live edge), harvested registry
        as fallback (bundle-decoded, not necessarily fired).

        Args:
            friendly: The mutation's friendly name, e.g.
                ``"CometUFIFeedbackReactMutation"``.

        Returns:
            The doc_id string from the live-verified table when present,
            else the registry's entry.

        Raises:
            RegistryMissError: When the name is in neither the verified table
                nor the registry.
        """
        known = C.KNOWN_MUTATIONS.get(friendly)
        if known:
            return known
        return self.doc_id(friendly)
