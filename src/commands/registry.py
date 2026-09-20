"""Registry commands: doc_id inspection + registry refresh re-harvest
(docs/13 §2 registry lifecycle).

``doc-ids`` reads the on-disk registry offline — no session, no cookies,
no network — for inspection and shell scripting. ``registry refresh`` is
the recovery action for DocIdStaleError (exit 2) and RegistryMissError
(exit 6): a controlled re-harvest of friendly_name → doc_id pairs from
the current deploy bundles, diffed against the stored registry.
``registry diff`` is its OFFLINE pre-flight: the same diff vocabulary
(:class:`graphql.registry_refresh.RegistryDiff`) computed between the
registry versions already on disk (v2 vs v3) — what a refresh would
change, without a single wire call. ``registry audit`` closes the
catalog loop, offline: it cross-references constants.KNOWN_MUTATIONS
against the loaded registry so a stale or absent catalog entry is
reported before it can become a live-wire surprise.

USER-DOC ANCHOR: cli/docs/07-reference-tooling.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse
from typing import Any

from constants import KNOWN_MUTATIONS

from .common import add_common_args, build_config, emit


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the doc-ids (offline lookup) and registry maintenance
    subcommands (refresh/diff/audit) onto the root parser."""
    p = sub.add_parser("doc-ids", help="search/list the persisted-query registry")
    add_common_args(p)
    p.add_argument("--search", default=None,
                   help="regex over friendly names (e.g. 'SendMessage.*Mutation')")
    p.add_argument("--limit", type=int, default=50, metavar="N",
                   help="maximum pairs to show (default 50)")
    p.set_defaults(fn=cmd_doc_ids)

    reg = sub.add_parser("registry", help="registry maintenance (doc_id re-harvest)")
    rsub = reg.add_subparsers(dest="registry_command", required=True)
    rp = rsub.add_parser(
        "refresh",
        help="re-harvest doc_ids from the current deploy (docs/13 §2) "
             "and diff vs the stored registry")
    add_common_args(rp)
    rp.add_argument("--save", action="store_true",
                    help="write assets/doc_id_registry_v3.json (v2 is never overwritten)")
    rp.add_argument("--workers", type=int, default=6,
                    help="concurrent bundle downloads")
    rp.add_argument("--max-bundles", type=int, default=None,
                    help="cap on bundles fetched (default all)")
    rp.set_defaults(fn=cmd_registry_refresh)

    rdp = rsub.add_parser(
        "diff",
        help="offline diff of the on-disk registry versions (v2 vs v3) — "
             "the pre-flight for refresh")
    add_common_args(rdp)
    rdp.add_argument("--limit", type=int, default=_EXAMPLES, metavar="N",
                     help=f"maximum names shown per category in human output "
                          f"(default {_EXAMPLES})")
    rdp.set_defaults(fn=cmd_registry_diff)

    rau = rsub.add_parser(
        "audit",
        help="cross-reference the KNOWN_MUTATIONS catalog against the "
             "registry (offline)")
    add_common_args(rau)
    rau.set_defaults(fn=cmd_audit)


def cmd_doc_ids(args: argparse.Namespace) -> int:
    """List (a slice of) the persisted-query registry, optionally filtered.

    Fully offline: parses the registry JSON shipped under data/
    (docs/13 §1; the v3 registry carries 1,032 pairs, docs/12).
    ``--search`` is a regex over friendly names; ``--limit`` caps the
    emitted pairs. Direct ``reg._pairs`` access is registry-internal
    but stable — the public API is the regex match path.

    Returns:
        0 — an empty match set is a valid query result, not a failure.
    """
    from graphql.registry import DocIdRegistry
    cfg = build_config(args)
    reg = DocIdRegistry.from_assets(cfg.assets_dir)
    pairs = (reg.match(args.search) if args.search
             else dict(sorted(reg._pairs.items()))  # registry-internal but stable
             )
    limited = dict(list(pairs.items())[: args.limit])
    emit(args, {"registry": reg.source, "total_pairs": len(reg),
                "shown": limited},
         human=lambda: print(f"registry: {reg.source} ({len(reg)} pairs; "
                             f"showing {len(limited)})"))
    return 0


# Human-mode cap: a live refresh diff can list hundreds of removed names —
# counts + this many examples per category is the readable form; the full
# diff is only a --json concern.
_EXAMPLES = 5


def cmd_registry_refresh(args: argparse.Namespace) -> int:
    """Re-harvest doc_ids from the live deploy bundles and diff (docs/13 §2).

    Downloads the current JS bundles (``--workers`` concurrent,
    optionally capped by ``--max-bundles``), extracts the
    doc_id/friendly_name pairs, and reports the added/changed/removed
    diff against the stored registry — the fix for a stale deploy roll
    (DocIdStaleError). ``--save`` writes data/doc_id_registry_v3.json;
    the previous file is never overwritten in place.

    Human output is capped at _EXAMPLES per category; ``--json``
    carries the complete diff for machine consumption.

    Returns:
        0 — the verdict is the diff itself. Fetch errors are reported
        in-band (``fetch_errors``) rather than as a failed exit.
    """
    from graphql.registry_refresh import refresh_registry
    cfg = build_config(args)
    diff = refresh_registry(cfg, save=args.save, workers=args.workers,
                            max_bundles=args.max_bundles)
    counts = {"added": len(diff.added), "changed": len(diff.changed),
              "removed": len(diff.removed)}

    def human() -> None:
        print(f"harvest revision {diff.harvest_revision or '?'} — "
              f"bundles fetched {diff.bundles_fetched}, errors {diff.fetch_errors}")
        print(f"added {counts['added']}, changed {counts['changed']}, "
              f"removed {counts['removed']}")
        if diff.added:
            print(f"  + {', '.join(list(diff.added)[:_EXAMPLES])}"
                  + (" …" if counts["added"] > _EXAMPLES else ""))
        if diff.changed:
            shown = ", ".join(
                f"{n} {o}->{v}" for n, (o, v) in list(diff.changed.items())[:_EXAMPLES])
            print(f"  ~ {shown}" + (" …" if counts["changed"] > _EXAMPLES else ""))
        if diff.removed:
            print(f"  - {', '.join(diff.removed[:_EXAMPLES])}"
                  + (" …" if counts["removed"] > _EXAMPLES else ""))
        if args.save:
            print(f"saved: {cfg.assets_dir / 'doc_id_registry_v3.json'}")

    if getattr(args, "as_json", False):
        # Full payload — machine consumers get every added/changed/removed name.
        emit(args, {
            "added": diff.added,
            "removed": diff.removed,
            "changed": {n: [o, v] for n, (o, v) in sorted(diff.changed.items())},
            "harvest_revision": diff.harvest_revision,
            "bundles_fetched": diff.bundles_fetched,
            "fetch_errors": diff.fetch_errors,
            "counts": counts,
            "saved_to": (str(cfg.assets_dir / "doc_id_registry_v3.json")
                         if args.save else None),
        })
        return 0

    # Human mode: counts + examples only — never page through the whole diff.
    emit(args, {
        "harvest_revision": diff.harvest_revision,
        "bundles_fetched": diff.bundles_fetched,
        "fetch_errors": diff.fetch_errors,
        "counts": counts,
        "examples": {
            "added": list(diff.added)[:_EXAMPLES],
            "changed": {n: [o, v] for n, (o, v) in
                        list(diff.changed.items())[:_EXAMPLES]},
            "removed": diff.removed[:_EXAMPLES],
        },
        "saved_to": (str(cfg.assets_dir / "doc_id_registry_v3.json")
                     if args.save else None),
    }, human=human)
    return 0


# The on-disk registry versions `registry diff` compares (docs/15 §P5-3:
# v3 is the refresh output and supersedes v2 by merge; v2 is never
# overwritten).
_V2_NAME = "doc_id_registry_v2.json"
_V3_NAME_ON_DISK = "doc_id_registry_v3.json"


def cmd_registry_diff(args: argparse.Namespace) -> int:
    """Diff the persisted registry versions on disk (v2 vs v3), fully
    offline — the pre-flight for ``registry refresh`` (docs/13 §2).

    Computes the same diff vocabulary the live refresh reports —
    added/removed/changed over friendly names, carried by the existing
    :class:`RegistryDiff` — but between the two registry FILES already
    in data/ (docs/15 §P5-3: v3 supersedes v2 by merge), so the operator
    can see what a registry version bump means with zero wire calls.
    Each version loads through :meth:`DocIdRegistry.from_file` — the
    one-file loader sharing from_assets' shape validation — which
    carries the file name (``source``) and deploy tag (``revision``)
    the report surfaces.

    Human output matches refresh's shape (counts + a ``--limit`` head
    per category); ``--json`` carries the complete name sets for
    machine consumption.

    Returns:
        0 — the verdict is the diff itself, as with refresh. A missing
        version file raises the typed RegistryMissError (exit 6) before
        any output; a corrupt one raises RegistryLoadError — the same
        exit-6 family (commands.common._exit_code).
    """
    from graphql.registry import DocIdRegistry
    from graphql.registry_refresh import RegistryDiff
    cfg = build_config(args)
    old = DocIdRegistry.from_file(cfg.assets_dir, _V2_NAME)
    new = DocIdRegistry.from_file(cfg.assets_dir, _V3_NAME_ON_DISK)
    old_rev, new_rev = old.revision, new.revision
    old_map = {name: old.doc_id(name) for name in old}
    new_map = {name: new.doc_id(name) for name in new}

    diff = RegistryDiff(
        added={n: d for n, d in new_map.items() if n not in old_map},
        removed=sorted(n for n in old_map if n not in new_map),
        changed={n: (old_map[n], d) for n, d in new_map.items()
                 if n in old_map and old_map[n] != d},
        harvest_revision=new_rev,
        bundles_fetched=0,
        fetch_errors=0,
    )
    counts = {"added": len(diff.added), "changed": len(diff.changed),
              "removed": len(diff.removed)}

    def human() -> None:
        print(f"old: {old.source} ({len(old)} pairs, revision {old_rev or '?'})")
        print(f"new: {new.source} ({len(new)} pairs, revision {new_rev or '?'})")
        print(f"added {counts['added']}, changed {counts['changed']}, "
              f"removed {counts['removed']}")
        limit = max(0, args.limit)
        if diff.added:
            print(f"  + {', '.join(list(diff.added)[:limit])}"
                  + (" …" if counts["added"] > limit else ""))
        if diff.changed:
            shown = ", ".join(
                f"{n} {o}->{v}" for n, (o, v) in list(diff.changed.items())[:limit])
            print(f"  ~ {shown}" + (" …" if counts["changed"] > limit else ""))
        if diff.removed:
            print(f"  - {', '.join(diff.removed[:limit])}"
                  + (" …" if counts["removed"] > limit else ""))

    if getattr(args, "as_json", False):
        # Full payload — machine consumers get every added/changed/removed
        # name, exactly like refresh's --json.
        emit(args, {
            "old": {"source": old.source, "pairs": len(old),
                    "revision": old_rev},
            "new": {"source": new.source, "pairs": len(new),
                    "revision": new_rev},
            "added": diff.added,
            "removed": diff.removed,
            "changed": {n: [o, v] for n, (o, v) in sorted(diff.changed.items())},
            "counts": counts,
        })
        return 0

    # Human mode: counts + the --limit head per category, refresh-style.
    emit(args, {
        "old": {"source": old.source, "pairs": len(old), "revision": old_rev},
        "new": {"source": new.source, "pairs": len(new), "revision": new_rev},
        "counts": counts,
        "head": {
            "added": list(diff.added)[: max(0, args.limit)],
            "changed": {n: [o, v] for n, (o, v) in
                        list(diff.changed.items())[: max(0, args.limit)]},
            "removed": diff.removed[: max(0, args.limit)],
        },
    }, human=human)
    return 0


# The audit's per-entry status vocabulary — ok / doc-id-mismatch / missing.
_AUDIT_STATUSES = ("ok", "doc-id-mismatch", "missing")


def cmd_audit(args: argparse.Namespace) -> int:
    """Cross-reference the KNOWN_MUTATIONS catalog against the loaded
    registry — fully OFFLINE, the catalog's closed loop.

    Every catalog name must resolve in the registry (loaded through
    :meth:`DocIdRegistry.from_assets`, the priority pick) to the SAME
    doc_id the catalog pins. A doc-id-mismatch is a REPLAY RISK: the
    surfaces resolve mutations KNOWN_MUTATIONS-first (surfaces.base.
    Surface._mutation_doc_id), so a stale catalog id means we would
    send the wrong persisted-doc hash at mutation time; a name the
    registry does not know at all would surface live as a
    RegistryMissError (exit 6) — audit finds both before the wire.

    Query-side, audited honestly: constants declares NO separate query
    catalog — queries resolve through per-surface module literals and
    the registry — so the canary Query entry inside KNOWN_MUTATIONS
    (``CometNotificationsBadgeCountQuery``, the live-verified cheapest
    read) is the only query-side declaration, and it is audited here
    with the rest of the catalog.

    Mismatches and misses are DATA (exit 0 — the verdict is the audit
    itself, as with refresh/diff); only a registry that cannot LOAD is
    an error, and it fails through the existing typed path
    (RegistryMissError family → exit 6, commands.common._exit_code).
    Human output: one status line per catalog entry, summary counts,
    and — when anything is off — the docs/13 §2 recovery pointer.

    Returns:
        0 once the registry loaded; the per-entry statuses are payload.
    """
    from graphql.registry import DocIdRegistry
    cfg = build_config(args)
    reg = DocIdRegistry.from_assets(cfg.assets_dir)
    entries: list[dict[str, Any]] = []
    for name, catalog_id in KNOWN_MUTATIONS.items():
        registry_id = reg.get(name)
        status = ("missing" if registry_id is None
                  else "doc-id-mismatch" if registry_id != catalog_id
                  else "ok")
        entries.append({"name": name, "status": status,
                        "catalog_doc_id": catalog_id,
                        "registry_doc_id": registry_id})
    counts: dict[str, int] = {
        s: sum(1 for e in entries if e["status"] == s) for s in _AUDIT_STATUSES}
    problems = counts["doc-id-mismatch"] + counts["missing"]

    def human() -> None:
        print(f"catalog: {len(entries)} KNOWN_MUTATIONS entries vs registry "
              f"{reg.source} ({len(reg)} pairs, revision {reg.revision or '?'})")
        for e in entries:
            line = f"{e['status']:<15} {e['name']}"
            if e["status"] == "doc-id-mismatch":
                line += (f" (catalog {e['catalog_doc_id']} vs "
                         f"registry {e['registry_doc_id']})")
            print(line)
        print(f"summary: ok {counts['ok']}, doc-id-mismatch "
              f"{counts['doc-id-mismatch']}, missing {counts['missing']}")
        print("query catalog: none in constants — the canary Query entry in "
              "KNOWN_MUTATIONS is the sole query-side declaration, audited "
              "above")
        if problems:
            print("recovery: re-harvest with `fbk registry refresh --save` "
                  "(docs/13 §2)")

    emit(args, {
        "registry": reg.source,
        "revision": reg.revision,
        "registry_pairs": len(reg),
        "catalog_entries": len(entries),
        "counts": counts,
        "entries": entries,
        "query_catalog": "none — the canary Query entry in KNOWN_MUTATIONS "
                         "is the only query-side declaration",
    }, human=human)
    return 0
