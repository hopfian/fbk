"""Persisted-query registry: friendly_name -> doc_id (docs/13 §2).

Sources, in priority order (all live-harvested from rsrc.php bundles;
scale figures from docs/15 §P2-1 and §P5-3):
  * doc_id_registry_v3.json    refresh output — freshest ids plus carried
                                baseline pairs (written by registry_refresh.py)
  * doc_id_registry_v2.json     1,032 pairs — homepage + action-surface deltas
  * doc_id_registry_merged.json              flat + permalink delta
  * doc_id_registry_full.json                flat homepage harvest (840)

The first file that loads wins — per-file fail-soft: a corrupt-but-
present file is skipped with a stderr warning naming it (never a raw
``JSONDecodeError``), and the typed :class:`RegistryLoadError` is raised
only when NO priority candidate loads, so the operator learns exactly
which files to delete or re-harvest while every healthy fallback keeps
serving reads.

USER-DOC ANCHOR: cli/docs/07-reference-tooling.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path

from .errors import RegistryLoadError, RegistryMissError

# v3 (refresh output) first — it supersedes v2 by merge (docs/15 §P5-3).
_PRIORITY = ("doc_id_registry_v3.json", "doc_id_registry_v2.json",
             "doc_id_registry_merged.json", "doc_id_registry_full.json")


class DocIdRegistry:
    """Lookup table for persisted-query ids, keyed by friendly name.

    The friendly_name → doc_id mapping lives only in the shipped JS
    bundles (docs/04 §2.3) and rotates with Facebook's build pipeline;
    this class is the harvested snapshot of that mapping. ``source``
    records which registry file (or ``inline``) supplied the pairs and
    ``revision`` the deploy tag the file was harvested from, so
    staleness audits can trace provenance.
    """

    def __init__(self, pairs: dict[str, str], source: str = "",
                 *, revision: str | None = None):
        """Args:
            pairs: friendly_name → doc_id mapping; copied defensively so
                later mutation of the caller's dict cannot corrupt the table.
            source: Provenance tag for audits (registry file name or ``inline``).
            revision: Harvest revision of the deploy the file was cut
                from, when a disk loader read it (``None`` for inline).
        """
        self._pairs = dict(pairs)
        self.source = source
        self.revision = revision

    # ------------------------------------------------------------- construction
    @classmethod
    def from_assets(cls, assets_dir: Path | str) -> DocIdRegistry:
        """Load the freshest registry file available in assets/.

        Tries the ``_PRIORITY`` order (v3 first — the refresh output,
        docs/15 §P5-3) with per-file fail-soft descent: a candidate
        that exists but is corrupt (truncated write, wrong schema) is
        skipped with a stderr warning naming the file — never a raw
        ``JSONDecodeError`` or ``KeyError`` from the parsing below —
        and the next healthy candidate wins, so every read command keeps
        working. All present candidates are probed so a corrupt
        fallback is reported even when a fresher file wins. The typed
        errors surface only when nothing loads. (docs/13 §2 covers the
        harvest/re-harvest recovery procedure; it does not constrain
        the descent order — this fail-soft semantics is this module's
        own contract.)

        Args:
            assets_dir: Directory holding the ``doc_id_registry_*.json``
                files (``Config.assets_dir``).

        Raises:
            RegistryLoadError: Every present priority file is corrupt;
                the message names each offending file and the recovery
                action.
            RegistryMissError: No registry file at all — run the harvest
                first (``scripts/02_harvest_bundles.py``).

        Example::

            registry = DocIdRegistry.from_assets(config.assets_dir)
            doc_id = registry.doc_id("CometModernHomeFeedQuery")
        """
        base = Path(assets_dir)
        registry: DocIdRegistry | None = None
        corrupt: list[str] = []
        for name in _PRIORITY:
            path = base / name
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                pairs = {p["friendly_name"]: p["doc_id"] for p in data["unique_pairs"]}
            except (json.JSONDecodeError, KeyError, TypeError, IndexError) as exc:
                print(f"warning: registry file {name} in {base} is corrupt "
                      f"({exc!r}) — skipped; delete it or re-harvest with "
                      f"scripts/02_harvest_bundles.py / "
                      f"21_surface_delta_harvest.py", file=sys.stderr)
                corrupt.append(name)
                continue
            if registry is None:
                registry = cls(pairs, source=name, revision=data.get("revision"))
        if registry is not None:
            return registry
        if corrupt:
            raise RegistryLoadError(
                f"registry file(s) corrupt in {base}: {', '.join(corrupt)} "
                f"— delete them or re-harvest with "
                f"scripts/02_harvest_bundles.py / 21_surface_delta_harvest.py"
            )
        raise RegistryMissError(
            f"no registry file in {base} — run scripts/02_harvest_bundles.py first")

    @classmethod
    def from_file(cls, assets_dir: Path | str, name: str) -> DocIdRegistry:
        """Load one specific registry file by name (offline, typed).

        :meth:`from_assets` walks the ``_PRIORITY`` list and stops at the
        first healthy file — it cannot address one specific version, but
        version-aware operations need exactly that (``registry diff``
        compares v2 vs v3 side by side, the docs/13 §2 maintenance loop's
        pre-flight). Same ``unique_pairs`` schema and the same typed
        corrupt/miss errors as :meth:`from_assets`, raised immediately
        for the one named file (no fail-soft descent — there is no
        fallback candidate to descend to), with ``source=name`` for
        provenance and ``revision`` carried from the file's deploy tag.

        Args:
            assets_dir: Directory holding the ``doc_id_registry_*.json``
                files (``Config.assets_dir``).
            name: Registry file name (e.g. ``doc_id_registry_v3.json``).

        Raises:
            RegistryLoadError: The named file exists but is corrupt
                (truncated write, wrong schema) — the message names the
                file and the recovery action.
            RegistryMissError: The named file is not on disk — for v3
                that means no refresh has been saved yet.
        """
        base = Path(assets_dir)
        path = base / name
        if not path.is_file():
            raise RegistryMissError(
                f"registry file {name} not present in {base} — run the "
                f"harvest (scripts/02_harvest_bundles.py) or write it via "
                f"`fbk registry refresh --save` (v2 is never overwritten)")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pairs = {p["friendly_name"]: p["doc_id"] for p in data["unique_pairs"]}
        except (json.JSONDecodeError, KeyError, TypeError, IndexError) as exc:
            raise RegistryLoadError(
                f"registry file {name} in {base} is corrupt ({exc!r}) — "
                f"delete it or re-harvest with scripts/02_harvest_bundles.py / "
                f"21_surface_delta_harvest.py"
            ) from None
        return cls(pairs, source=name, revision=data.get("revision"))

    @classmethod
    def from_pairs(cls, pairs: dict[str, str]) -> DocIdRegistry:
        """Inline construction (tests, refresh baselines)."""
        return cls(dict(pairs), source="inline")

    # ------------------------------------------------------------------ lookups
    def get(self, friendly_name: str) -> str | None:
        """doc_id for a friendly name, or ``None`` — the non-raising probe."""
        return self._pairs.get(friendly_name)

    def doc_id(self, friendly_name: str) -> str:
        """Strict lookup — the form commands use.

        Args:
            friendly_name: Relay operation name.

        Returns:
            The persisted-query id.

        Raises:
            RegistryMissError: The name is absent from every harvested
                source; the message carries the re-harvest guidance
                (docs/13 §2).
        """
        try:
            return self._pairs[friendly_name]
        except KeyError:
            raise RegistryMissError(
                f"doc_id for {friendly_name!r} not in registry — re-harvest "
                f"bundles (scripts/02_harvest_bundles.py / 21_surface_delta_harvest.py)"
            ) from None

    def match(self, pattern: str) -> dict[str, str]:
        """All (friendly_name, doc_id) pairs matching a regex.

        Args:
            pattern: Python regex applied via ``re.search`` to each
                friendly name — used to explore the prefix/suffix
                taxonomy of the harvest (docs/13 §2.4).

        Returns:
            Matching pairs sorted by friendly name.
        """
        rx = re.compile(pattern)
        return {k: v for k, v in sorted(self._pairs.items()) if rx.search(k)}

    def __contains__(self, friendly_name: str) -> bool:
        """Membership by friendly name."""
        return friendly_name in self._pairs

    def __len__(self) -> int:
        """Number of harvested pairs."""
        return len(self._pairs)

    def __iter__(self) -> Iterator[str]:
        """Iterate friendly names in insertion (harvest) order."""
        return iter(self._pairs)
