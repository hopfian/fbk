"""Captured-mutation census: ``fbk templates list/show/verify`` — the
offline inventory of the replay assets under ``data/captured_*.json``.

The mutation engine replays captured variables VERBATIM (docs/15
§P2-3/P3 replay invariant: substitute only target ids and payload
text, re-send the encrypted tracking blobs untouched) — but nothing
let the operator see WHAT is captured: which assets exist, which
friendly names each carries, which doc_ids they map to. This family
is that census. Fully OFFLINE: no session, no network, no cookie jar;
a registry cross-reference miss is DATA (an orphan is a registry
refresh candidate), never an error.

Asset shapes are reported honestly, never forced into one frame:

  * mutations assets (``captured_mutations.json``,
    ``captured_composer.json``, ``captured_comment_mutations.json``)
    carry a ``mutations`` list of ``{friendly_name, doc_id,
    variables, q}`` entries — the exact shape
    surfaces.base.load_template reads;
  * ``captured_real_deltas.json`` is NOT a mutations file: it holds a
    DGW frame capture (``thread_id``, ``sent``, ``mqtt_frames``,
    ``dgw_frames``) — listed and verified as what it is ("delta
    capture: N entries"), with no mutation checks invented for it.

SECURITY BOUNDARY:

  Display redaction rides ENTIRELY on journal/recorder.redact_entry
  with the ALL_SECRETS vocabulary (docs/11 §7) — this module adds no
  redaction logic of its own and must never need any: secret-named
  values (xs, fb_dtsg, privacy_write_id, ...) never print, only the
  ``<redacted:fingerprint>`` markers do. Tracking blobs are NOT
  secret-named — they are opaque ciphertext by design (replayed
  verbatim per the docs/15 §P2-3 invariant), so they print with only
  their head kept (>``_TRACKING_MAX`` chars clipped with a length
  note) — enough to identify the capture without dumping kilobytes.

USER-DOC ANCHOR: cli/docs/07-reference-tooling.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from journal.recorder import redact_entry

from .common import add_common_args, build_config, emit

if TYPE_CHECKING:  # pragma: no cover
    from graphql.registry import DocIdRegistry

# The census's asset universe: every captured_* JSON under data/.
_ASSET_GLOB = "captured_*.json"

# Tracking ciphertext display cap (docs/15 §P2-3: the blobs replay
# verbatim; the head suffices to identify which capture is loaded).
_TRACKING_MAX = 200


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the templates family onto the root parser: list/show/verify."""
    p = sub.add_parser(
        "templates",
        help="captured-mutation census over data/captured_*.json (offline)")
    csub = p.add_subparsers(dest="templates_command", required=True)

    listing = csub.add_parser(
        "list", help="enumerate captured assets: mutations, doc_ids, "
                     "registry cross-refs")
    add_common_args(listing)
    listing.set_defaults(fn=cmd_list)

    show = csub.add_parser(
        "show", help="one captured mutation's variables — secrets redacted, "
                     "tracking truncated")
    add_common_args(show)
    show.add_argument("--asset", required=True, metavar="FILE",
                      help="captured asset name under data/ "
                           "(e.g. captured_mutations.json)")
    show.add_argument("--mutation", required=True, metavar="NAME",
                      help="the captured entry's friendly_name "
                           "(first match wins)")
    show.set_defaults(fn=cmd_show)

    verify = csub.add_parser(
        "verify", help="structural sanity: parse + fields + doc_id registry "
                       "resolution (orphans warn, parse failures fail)")
    add_common_args(verify)
    verify.set_defaults(fn=cmd_verify)


def _assets(data_dir: Path) -> list[Path]:
    """Every captured_* asset under data/, name-sorted (stable census order)."""
    return sorted(data_dir.glob(_ASSET_GLOB))


def _parse_asset(path: Path) -> tuple[dict[str, Any] | None, str]:
    """Parse one asset file into (doc, "") or (None, failure reason).

    A census cannot trust its inputs: any read or JSON error, and any
    non-object top level, is folded into the reason string instead of
    raising — the callers decide whether it is reportable data
    (``list``) or the CRITICAL gate condition (``verify``).
    """
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(doc, dict):
        return None, f"top level is {type(doc).__name__}, expected a JSON object"
    return doc, ""


def _is_mutations_asset(doc: dict[str, Any]) -> bool:
    """True when the asset carries the ``mutations`` list load_template reads."""
    return isinstance(doc.get("mutations"), list)


def _kind_of(doc: dict[str, Any]) -> str:
    """Honest kind label for a non-mutations capture (never forced)."""
    if isinstance(doc.get("dgw_frames"), list) or isinstance(doc.get("mqtt_frames"), list):
        return "delta capture"
    return "other capture"


def _shape_of(doc: dict[str, Any]) -> dict[str, str]:
    """Shape summary for a non-mutations capture: scalars by JSON type,
    lists by entry count (e.g. ``{"dgw_frames": "list[17]"}``)."""
    return {key: (f"list[{len(value)}]" if isinstance(value, list)
                  else type(value).__name__)
            for key, value in doc.items()}


def _registry_ids(reg: DocIdRegistry) -> set[str]:
    """Every doc_id the registry maps — the orphan membership test."""
    return {reg.doc_id(name) for name in reg}


def _registry_state(friendly: Any, doc_id: Any,
                    reg: DocIdRegistry, ids: set[str]) -> str:
    """Human registry cross-ref for one captured mutation.

    Three states, all data: the doc_id is in the registry (checked by
    value — the orphan test), the registry maps the friendly name to a
    DIFFERENT doc_id (a deploy roll — the capture is stale relative to
    the harvest), or the doc_id is an orphan (registry refresh
    candidate).
    """
    if not (isinstance(doc_id, str) and doc_id in ids):
        return "NOT in registry (orphan)"
    known = reg.get(friendly) if isinstance(friendly, str) else None
    if known != doc_id:
        return f"in registry, but {friendly} maps to {known} (deploy roll?)"
    return "in registry"


def _truncate_tracking(obj: Any) -> Any:
    """Display-copy transform: clip long values under ``tracking`` keys.

    docs/15 §P2-3/P3: tracking blobs are encrypted and replayed
    VERBATIM by the mutation engine — display keeps only the head
    (``_TRACKING_MAX`` chars) plus a length note, enough to identify
    the capture. Applied AFTER redaction, on the display copy only;
    every other value passes through untouched.
    """
    if isinstance(obj, dict):
        return {key: (_clip_tracking(value) if key == "tracking"
                      else _truncate_tracking(value))
                for key, value in obj.items()}
    if isinstance(obj, list):
        return [_truncate_tracking(item) for item in obj]
    return obj


def _clip_tracking(value: Any) -> Any:
    """Clip one tracking value (a string, or a list of them) to the cap."""
    if isinstance(value, list):
        return [_clip_tracking(item) for item in value]
    if isinstance(value, str) and len(value) > _TRACKING_MAX:
        over = len(value) - _TRACKING_MAX
        return f"{value[:_TRACKING_MAX]}…[+{over} chars truncated]"
    return value


def cmd_list(args: argparse.Namespace) -> int:
    """Census every captured_* asset in the resolved data dir.

    Per mutations asset: mutation count, per-mutation friendly_name +
    doc_id + the registry cross-reference (via ``DocIdRegistry`` — a
    miss is data, not an error). Non-mutations captures report their
    actual shape; an unparseable asset is census data here too (the
    ``verify`` subcommand is the failing gate for that).

    Returns:
        0 — always: this is the inventory; verdicts belong to verify.
        Registry absence/corruption raises the typed RegistryMissError/
        RegistryLoadError (exit 6, refresh guidance) before output.
    """
    from graphql.registry import DocIdRegistry
    cfg = build_config(args)
    reg = DocIdRegistry.from_assets(cfg.assets_dir)
    ids = _registry_ids(reg)
    assets: list[dict[str, Any]] = []
    for path in _assets(cfg.data_dir):
        doc, err = _parse_asset(path)
        if doc is None:
            assets.append({"asset": path.name, "kind": "unparseable",
                           "error": err})
            continue
        if _is_mutations_asset(doc):
            mutations: list[dict[str, Any]] = []
            for entry in doc["mutations"]:
                if not isinstance(entry, dict):
                    mutations.append({"friendly_name": None, "doc_id": None,
                                      "registry_doc_id": None,
                                      "in_registry": False})
                    continue
                friendly = entry.get("friendly_name")
                doc_id = entry.get("doc_id")
                mutations.append({
                    "friendly_name": friendly,
                    "doc_id": doc_id,
                    "registry_doc_id": (reg.get(friendly)
                                        if isinstance(friendly, str) else None),
                    "in_registry": isinstance(doc_id, str) and doc_id in ids,
                })
            assets.append({"asset": path.name, "kind": "mutations",
                           "count": len(mutations), "mutations": mutations})
        else:
            assets.append({"asset": path.name, "kind": _kind_of(doc),
                           "shape": _shape_of(doc)})
    payload = {"data_dir": str(cfg.data_dir), "registry": reg.source,
               "assets": assets}

    def human() -> None:
        print(f"data dir : {cfg.data_dir}")
        print(f"registry : {reg.source} ({len(reg)} pairs)")
        for asset in assets:
            if asset["kind"] == "mutations":
                print(f"{asset['asset']} — {asset['count']} mutation(s)")
                for m in asset["mutations"]:
                    state = _registry_state(m["friendly_name"], m["doc_id"],
                                            reg, ids)
                    print(f"  {m['friendly_name']!s:<52} "
                          f"{m['doc_id']!s:>21}  {state}")
            elif asset["kind"] == "unparseable":
                print(f"{asset['asset']} — UNPARSEABLE ({asset['error']})")
            else:
                shape = ", ".join(f"{k}={v}" for k, v in asset["shape"].items())
                print(f"{asset['asset']} — {asset['kind']} ({shape})")

    emit(args, payload, human=human)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """Print one captured mutation's full variables — the census drill-down.

    The matched entry's variables JSON-dump with ALL secret-named
    fields REDACTED via journal/recorder.redact_entry (the ALL_SECRETS
    vocabulary — the values never print, only ``<redacted:>`` markers),
    and tracking ciphertext clipped to its head. ``--json`` emits the
    same redacted structure as machine output; no mode ever bypasses
    redaction. The first friendly_name match wins (load_template
    semantics — same-named captures like like-vs-remove reactions are
    disambiguated at replay time, not here).

    Returns:
        0 on success; 1 when the asset is absent, unparseable, or not
        a mutations asset, or no entry matches ``--mutation`` (failed
        precondition, the house convention).
    """
    cfg = build_config(args)
    path = cfg.data_dir / args.asset
    if not path.is_file():
        print(f"error: no captured asset {args.asset!r} under {cfg.data_dir}",
              file=sys.stderr)
        return 1
    doc, err = _parse_asset(path)
    if doc is None:
        print(f"error: asset {args.asset!r} does not parse ({err})",
              file=sys.stderr)
        return 1
    if not _is_mutations_asset(doc):
        print(f"error: asset {args.asset!r} carries no 'mutations' list — "
              f"not a captured-mutation asset", file=sys.stderr)
        return 1
    entries = doc["mutations"]
    entry = next((e for e in entries
                  if isinstance(e, dict)
                  and e.get("friendly_name") == args.mutation), None)
    if entry is None:
        names = sorted({str(e.get("friendly_name")) for e in entries
                        if isinstance(e, dict)})
        print(f"error: no mutation {args.mutation!r} in {args.asset} — "
              f"captured names: {', '.join(names)}", file=sys.stderr)
        return 1
    # SECURITY BOUNDARY: redaction rides ENTIRELY on recorder.redact_entry
    # (ALL_SECRETS, docs/11 §7) — no custom redaction exists below this
    # line; _truncate_tracking only clips the already-safe display copy.
    safe = _truncate_tracking(redact_entry(entry))
    variables = safe.get("variables") or {}
    payload = {"asset": args.asset, "mutation": args.mutation,
               "doc_id": entry.get("doc_id"), "variables": variables}

    def human() -> None:
        print(f"asset    : {args.asset}")
        print(f"mutation : {args.mutation} (doc_id {entry.get('doc_id')})")
        print("variables (secret-named fields redacted via journal "
              "redaction; tracking ciphertext truncated):")
        print(json.dumps(variables, ensure_ascii=False, indent=2, default=str))

    emit(args, payload, human=human)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Structural sanity over every captured_* asset.

    Per mutations asset: every entry must have a friendly_name,
    non-empty variables, and a doc_id that resolves in the registry.
    Entry-shape gaps and orphans (doc_id absent from the registry —
    refresh candidates) are WARN lines, data not failures. A non-
    mutations capture only has to parse. An asset that fails to PARSE
    (unreadable, invalid JSON, or a broken ``mutations`` schema) is
    CRITICAL — the one condition that fails the gate.

    Returns:
        0 when every asset parses (warnings allowed); 1 on any CRITICAL.
        Registry absence/corruption raises the typed RegistryMissError/
        RegistryLoadError (exit 6) before any check runs.
    """
    from graphql.registry import DocIdRegistry
    cfg = build_config(args)
    reg = DocIdRegistry.from_assets(cfg.assets_dir)
    ids = _registry_ids(reg)
    assets: list[dict[str, Any]] = []
    critical = 0
    warnings = 0
    for path in _assets(cfg.data_dir):
        doc, err = _parse_asset(path)
        if doc is None:
            critical += 1
            assets.append({"asset": path.name, "status": "critical",
                           "issues": [f"unparseable: {err}"]})
            continue
        if _is_mutations_asset(doc):
            issues: list[str] = []
            checked = 0
            for i, entry in enumerate(doc["mutations"]):
                if not isinstance(entry, dict):
                    issues.append(f"entry {i} is {type(entry).__name__}, "
                                  f"not an object")
                    warnings += 1
                    continue
                friendly = entry.get("friendly_name")
                if not isinstance(friendly, str) or not friendly:
                    issues.append(f"entry {i}: missing friendly_name")
                    warnings += 1
                variables = entry.get("variables")
                if not isinstance(variables, dict) or not variables:
                    issues.append(f"entry {i} ({friendly}): empty variables")
                    warnings += 1
                doc_id = entry.get("doc_id")
                if not (isinstance(doc_id, str) and doc_id in ids):
                    issues.append(f"entry {i} ({friendly}): doc_id {doc_id!r} "
                                  f"is an orphan — absent from the registry "
                                  f"(refresh candidate)")
                    warnings += 1
                checked += 1
            assets.append({"asset": path.name,
                           "status": "warn" if issues else "ok",
                           "mutations": checked, "issues": issues})
        elif "mutations" in doc:
            critical += 1
            assets.append({"asset": path.name, "status": "critical",
                           "issues": ["'mutations' present but not a list"]})
        else:
            entries = sum(len(v) for v in doc.values() if isinstance(v, list))
            assets.append({"asset": path.name, "status": "ok",
                           "kind": _kind_of(doc), "entries": entries,
                           "issues": []})
    payload = {"data_dir": str(cfg.data_dir), "registry": reg.source,
               "critical": critical, "warnings": warnings,
               "status": "critical" if critical else "ok", "assets": assets}

    def human() -> None:
        print(f"data dir : {cfg.data_dir}")
        print(f"registry : {reg.source} ({len(reg)} pairs)")
        for asset in assets:
            if asset["status"] == "critical":
                print(f"CRITICAL {asset['asset']} — {asset['issues'][0]}")
            elif asset["status"] == "warn":
                print(f"WARN     {asset['asset']}")
                for issue in asset["issues"]:
                    print(f"  - {issue}")
            elif "mutations" in asset:
                print(f"ok       {asset['asset']} — "
                      f"{asset['mutations']} mutation(s)")
            else:
                print(f"ok       {asset['asset']} — {asset['kind']}: "
                      f"{asset['entries']} entries, no mutation checks")
        verdict = "FAIL" if critical else "PASS"
        print(f"summary: {len(assets)} asset(s) — {critical} critical, "
              f"{warnings} warning(s) — verify {verdict}")

    emit(args, payload, human=human)
    return 1 if critical else 0
