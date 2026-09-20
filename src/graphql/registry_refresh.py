"""Registry refresh: re-harvest the doc_id registry from the live deploy.

Methodology (docs/13 §2 — harvest pipeline): bootstrap the logged-in
homepage, collect the current deploy's rsrc.php bundle URLs, download
them (threaded, one curl_cffi session per worker via threading.local,
polite jitter — the pattern established by scripts/02_harvest_bundles.py),
and extract persisted-query registrations with the two live-calibrated
shapes (docs/15 §3):

    __d("<Friendly>_facebookRelayOperation",[],(function(...){a.exports="<doc_id>"}),null);
    params:{id:"<doc_id>",metadata:{},name:"<Friendly>",operationKind:"query|mutation"}

The harvest is diffed against the persisted registry
(assets/doc_id_registry_v2.json); with save=True the fresh pairs are
written to assets/doc_id_registry_v3.json (v2 is NEVER overwritten) in
the established schema, and v3 becomes the preferred registry source via
the _PRIORITY list in graphql/registry.py.

Every bundle fetch runs under the RequestGovernor (docs/15 §P9-1), so
a mass-harvest respects the same pacing and daily caps as any other
request traffic — deliberate mass operations raise FBK_GOVERNOR_DAILY
for the run.

USER-DOC ANCHOR: cli/docs/07-reference-tooling.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from auth.bootstrap import bootstrap_homepage
from config import Config

from .errors import RegistryMissError
from .registry import DocIdRegistry

if TYPE_CHECKING:  # pragma: no cover - typing only
    from transport.session import FBTransport

# Live-calibrated extraction shape (a) (docs/15 §3) — module-per-operation:
# __d("<Friendly>_facebookRelayOperation",[],(function(...){a.exports="<doc_id>"}),null);
# The doc_id IS the module export; the old docID:"…"/queryID:"…" literals are
# absent from current bundles.
RE_RELAY_OP = re.compile(
    r'__d\("([A-Za-z0-9_]+)_facebookRelayOperation",\[\],'
    r'\(function\([^)]*\)\{[a-z]\.exports="(\d{5,})"\}\),null\)'
)
# Live-calibrated extraction shape (b) (docs/15 §3) — Relay Request node:
# params:{id:"<doc_id>",metadata:{},name:"<Friendly>",operationKind:"query|mutation"}
RE_PARAMS = re.compile(
    r'params:\{id:"(\d{5,})",metadata:\{\},name:"([A-Za-z0-9_]+)",operationKind:"(\w+)"'
)

# rsrc.php is CDN-served and cookieless (docs/13 §7) — a bare fetch header set
# suffices and never touches the authenticated session's reputation.
_BUNDLE_HEADERS = {"accept": "*/*", "referer": "https://www.facebook.com/"}
_FETCH_TIMEOUT = 60.0       # generous single-bundle timeout; bundles are large minified JS
_V3_NAME = "doc_id_registry_v3.json"   # refresh output; v2 is never overwritten (docs/15 §P5-3)
# rollback copy written before every v3 overwrite (self-healing primitive)
_V3_PREV_NAME = "doc_id_registry_v3.prev.json"

# Per-worker curl sessions: the transport's own curl_cffi session is NOT
# thread-safe, so workers get thread-local sessions instead.
_tl = threading.local()


@dataclass
class RegistryDiff:
    """Outcome of one refresh: old (persisted) registry vs fresh harvest.

    Encodes the docs/13 §3 maintenance loop's diff vocabulary: new
    doc_ids classify, vanished ids flag STALE, and id churn on a stable
    name is the deploy-rotation signal.
    """

    added: dict[str, str]                     # friendly_name -> new doc_id
    removed: list[str]                        # friendly names gone from the fresh harvest
    changed: dict[str, tuple[str, str]]       # friendly -> (old doc_id, new doc_id)
    harvest_revision: str | None              # server_revision of the bootstrapped deploy
    bundles_fetched: int                      # bundles successfully downloaded
    fetch_errors: int                         # bundles that failed to download


@dataclass
class HarvestStats:
    """Side-channel metrics a caller may pass into harvest_pairs.

    * collisions: friendly -> every distinct doc_id seen for that name,
      winning id first (the harvest keeps only the winner).
    * sources: friendly -> source tags ("params:<kind>" / "relayOperation")
      contributing to the winning pair, scripts/02 style.
    """

    bundles_fetched: int = 0
    fetch_errors: int = 0
    collisions: dict[str, list[str]] = field(default_factory=dict)
    sources: dict[str, set[str]] = field(default_factory=dict)


class RegistryRefreshError(RuntimeError):
    """Refresh could not run (no bundles in bootstrap, unusable fetcher, ...).

    Distinct from :class:`RegistryMissError`: this failure means the
    refresh pipeline itself was blocked — nothing was harvested and
    nothing changed on disk. The message carries the remediation (page
    shape change vs edge soft-block, docs/10 §3).
    """


def _absorb(pairs: dict[str, str], collisions: dict[str, list[str]],
            sources: dict[str, set[str]], friendly: str, doc_id: str,
            tag: str) -> None:
    """Merge one (friendly, doc_id, source-tag) observation; first id wins.

    Because merged results are folded in ``bundle_urls`` order regardless
    of thread completion order, first-wins is deterministic across runs;
    a later, different id for an already-known name is recorded in
    ``collisions`` (winning id first) instead of silently overwriting.
    """
    known = pairs.get(friendly)
    if known is None:
        pairs[friendly] = doc_id
        sources[friendly] = {tag}
        return
    sources[friendly].add(tag)
    if doc_id != known and doc_id not in collisions.get(friendly, ()):
        collisions.setdefault(friendly, [known]).append(doc_id)


def _resolve_fetcher(transport: Any) -> Callable[[str], str]:
    """Adapt `transport` into a fetch(url) -> bundle-text callable.

    Three accepted shapes:
      * a plain callable fetch(url) -> text (or an object with .text);
      * a real FBTransport — its curl session is NOT thread-safe, so we
        spin up one curl_cffi session per worker thread instead
        (threading.local, the scripts/02 pattern) and never touch it;
      * any duck-typed stub exposing .get(url, headers=...) (offline tests).
    """
    if callable(transport):
        def call_fetch(url: str) -> str:
            out = transport(url)
            return out if isinstance(out, str) else out.text
        return call_fetch
    if hasattr(transport, "impersonate"):
        impersonate = str(transport.impersonate)

        def session_fetch(url: str) -> str:
            from curl_cffi import requests as cr
            if not hasattr(_tl, "s"):
                _tl.s = cr.Session()
            # typed-by-contract (§5.5): curl_cffi 0.16+ ships py.typed, so
            # pin the response to the real cr.Response — the thread-local
            # session itself stays Any (threading.local), and the receiver
            # being Any keeps curl_cffi's strict impersonate literal types
            # out of this seam.
            resp: cr.Response = _tl.s.get(
                url, impersonate=impersonate, timeout=_FETCH_TIMEOUT,
                headers=dict(_BUNDLE_HEADERS))
            if resp.status_code >= 400:
                raise RegistryRefreshError(
                    f"bundle fetch HTTP {resp.status_code}: {url}")
            return resp.text
        return session_fetch
    if hasattr(transport, "get"):
        def stub_fetch(url: str) -> str:
            resp = transport.get(url, headers=dict(_BUNDLE_HEADERS))
            return resp if isinstance(resp, str) else resp.text
        return stub_fetch
    raise RegistryRefreshError(
        "transport must be a fetch callable, an FBTransport (thread-local "
        "curl sessions), or a stub with .get(url, headers=...)")


def harvest_pairs(
    transport: Any,
    bundle_urls: Sequence[str],
    *,
    workers: int = 6,
    delay_ceiling: float = 0.2,
    max_bundles: int | None = None,
    stats: HarvestStats | None = None,
    governor: Any | None = None,
) -> dict[str, str]:
    """Fetch bundles and extract friendly_name -> doc_id (docs/13 §2).

    Downloads run on a thread pool with polite per-request jitter
    (uniform 0.05..delay_ceiling seconds). Results are merged in
    bundle_urls order regardless of completion order, so dedupe is
    deterministic: first-wins per friendly name, with params: entries
    absorbing relayOperation entries by insertion precedence
    (docs/15 §3 shapes). Competing ids for one name are noted in
    `stats.collisions` (winning id first). Pass a HarvestStats object
    to receive bundles_fetched / fetch_errors / collisions / sources.

    When ``governor`` is provided (a RequestGovernor), every bundle fetch
    is gated through it — a full harvest then respects the same pacing
    and daily caps as every other request (docs/10 §7). Deliberate
    mass-harvests can raise FBK_GOVERNOR_DAILY for the run.
    """
    urls = list(bundle_urls)
    if max_bundles is not None:
        urls = urls[: max_bundles]
    fetch = _resolve_fetcher(transport)
    ceiling = max(0.0, delay_ceiling)

    texts: list[str | None] = [None] * len(urls)
    errors = 0

    def worker(url: str) -> str:
        if ceiling:
            time.sleep(random.uniform(0.05, ceiling))
        if governor is not None:
            governor.before_request(quiet=True)
        return fetch(url)

    if urls:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futs = {pool.submit(worker, u): i for i, u in enumerate(urls)}
            for fut in as_completed(futs):
                i = futs[fut]
                try:
                    texts[i] = fut.result()
                except Exception:
                    errors += 1

    pairs: dict[str, str] = {}
    collisions: dict[str, list[str]] = {}
    sources: dict[str, set[str]] = {}
    for text in texts:
        if text is None:
            continue
        for doc_id, friendly, kind in RE_PARAMS.findall(text):
            _absorb(pairs, collisions, sources, friendly, doc_id, f"params:{kind}")
        for friendly, doc_id in RE_RELAY_OP.findall(text):
            _absorb(pairs, collisions, sources, friendly, doc_id, "relayOperation")

    if stats is not None:
        stats.bundles_fetched = len(urls) - errors
        stats.fetch_errors = errors
        stats.collisions = collisions
        stats.sources = sources
    return pairs


def _build_transport(config: Config) -> FBTransport:
    """Cookie-authenticated transport for the bootstrap GET (docs/09)."""
    from transport.cookies import load_netscape
    from transport.session import FBTransport
    cookies = load_netscape(config.cookies_path)
    return FBTransport(cookies, timeout=config.timeout,
                       impersonate=config.impersonate)


def _write_v3(config: Config, fresh: dict[str, str], diff: RegistryDiff,
              stats: HarvestStats, baseline_old: dict[str, str] | None = None) -> Path:
    """Write the MERGED registry as assets/doc_id_registry_v3.json.

    Merge semantics (docs/15 §P5-3 — a homepage-only harvest loses pairs
    that live in lazy action-surface bundles, so replacement would regress
    coverage):

      * names present in the fresh harvest -> the FRESH doc_id wins
        (this is how deploy-rolled ids get applied);
      * names absent from the fresh harvest (surface deltas from
        /groups/create, /settings, permalink surfaces, ...) -> the
        baseline doc_id is CARRIED, source-tagged "carried:<n>".

    v2 is never overwritten; v3 sits at the front of the registry
    _PRIORITY list, so it is preferred from the next load on.
    """
    merged: dict[str, str] = dict(baseline_old or {})
    merged.update(fresh)
    carried = sorted(n for n in merged if n not in fresh)

    assets = Path(config.assets_dir)
    assets.mkdir(parents=True, exist_ok=True)
    path = assets / _V3_NAME
    # uniform backup-on-write (the self-healing rollback primitive, src/
    # healing.py): both the operator's manual `registry refresh --save`
    # and the coordinator's auto-heal overwrite v3 — a bit-for-bit .prev
    # copy first means a bad harvest is always reversible, whichever
    # path produced it. Best-effort: a failed copy must not block the write.
    if path.is_file():
        try:
            import shutil
            shutil.copy2(path, path.with_name(_V3_PREV_NAME))
        except OSError:
            pass
    payload = {
        "revision": diff.harvest_revision,
        "harvested_at": datetime.now().replace(microsecond=0).isoformat(),
        "sources": ["refresh", "carried"],
        "unique_pairs": [
            {"friendly_name": n, "doc_id": d,
             "sources": sorted(stats.sources.get(n) or
                               (["carried"] if n in carried else ["refresh"]))}
            for n, d in sorted(merged.items())
        ],
        "stats": {
            "unique_pairs": len(merged),
            "fresh_pairs": len(fresh),
            "carried_pairs": len(carried),
            "added": len(diff.added),
            "changed": len(diff.changed),
            # 'removed' is informational: names absent from the homepage
            # deploy this pass — they are CARRIED, not dropped (see above).
            "removed_from_homepage_deploy": len(diff.removed),
            "mutations": sum(1 for n in merged if n.endswith("Mutation")),
            "bundles_fetched": diff.bundles_fetched,
            "fetch_errors": diff.fetch_errors,
            "collisions": len(stats.collisions),
        },
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"registry v3 -> {path}")
    return path


def refresh_registry(
    config: Config,
    *,
    transport: FBTransport | None = None,
    save: bool = False,
    workers: int = 6,
    max_bundles: int | None = None,
    baseline: DocIdRegistry | None = None,
) -> RegistryDiff:
    """Bootstrap the live homepage, re-harvest the current deploy's bundles,
    and diff them against the persisted registry (docs/13 §2, docs/15 §P2-1).

    The bootstrap enumerates this deploy's rsrc.php bundles and its
    server_revision; the harvest downloads them (thread-local curl
    sessions — the transport's own session is not thread-safe) and
    extracts friendly_name -> doc_id pairs. When save=True the fresh
    harvest is written to assets/doc_id_registry_v3.json (v2 is NEVER
    overwritten) in the established schema and its path is printed;
    from then on DocIdRegistry.from_assets prefers v3 (registry.py
    _PRIORITY). `baseline` overrides the assets registry for diffing
    (tests / dry comparisons). Returns the diff.
    """
    if transport is None:
        transport = _build_transport(config)
    cookies = getattr(transport, "cookies", None)
    if not isinstance(cookies, Mapping) or not cookies:
        from transport.cookies import load_netscape
        cookies = load_netscape(config.cookies_path)
    boot = bootstrap_homepage(transport, cookies)
    if not boot.bundle_urls:
        raise RegistryRefreshError(
            "bootstrap found no rsrc.php bundle URLs — page shape changed "
            "or edge soft-block (docs/10 §3)")
    stats = HarvestStats()
    from governor import default_governor
    fresh = harvest_pairs(transport, boot.bundle_urls, workers=workers,
                          max_bundles=max_bundles, stats=stats,
                          governor=default_governor())

    if baseline is None:
        try:
            baseline = DocIdRegistry.from_assets(config.assets_dir)
        except RegistryMissError:
            # also covers RegistryLoadError (corrupt persisted file): the
            # refresh then re-harvests from scratch and _write_v3 heals it
            baseline = DocIdRegistry.from_pairs({})
    old: dict[str, str] = {}
    for name in baseline:
        doc_id = baseline.get(name)
        if doc_id is not None:
            old[name] = doc_id

    added = {n: d for n, d in fresh.items() if n not in old}
    removed = sorted(n for n in old if n not in fresh)
    changed = {n: (old[n], d) for n, d in fresh.items()
                if n in old and old[n] != d}
    diff = RegistryDiff(added=added, removed=removed, changed=changed,
                        harvest_revision=boot.revision,
                        bundles_fetched=stats.bundles_fetched,
                        fetch_errors=stats.fetch_errors)
    if save:
        _write_v3(config, fresh, diff, stats, baseline_old=old)
    return diff
