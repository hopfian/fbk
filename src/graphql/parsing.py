"""Wire parsing (docs/04 §3): incremental NDJSON, legacy prefixes, doc merging.

The /api/graphql/ surface answers with one or more JSON documents
concatenated in a single body (docs/04 §3.2); this module turns that
byte stream into Python objects without ever raising on a partial
trailing document, then folds the frames into one payload for the error
guard. The legacy ``for (;;);`` anti-JSON-hijacking guard (docs/04 §3.3)
is stripped before parsing when present.

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import json
from typing import Any

from constants import LEGACY_JSONP_PREFIX


def strip_legacy_prefix(text: str) -> str:
    """Remove the anti-JSON-hijacking ``for (;;);`` prefix from legacy payloads.

    The guard is an era-dependent artifact of the legacy ajax transport;
    when present it precedes the first JSON document, so stripping it is
    lossless (docs/04 §3.3).

    Args:
        text: Raw response body.

    Returns:
        The body without the guard prefix; unchanged when absent.
    """
    if text.startswith(LEGACY_JSONP_PREFIX):
        return text[len(LEGACY_JSONP_PREFIX):].lstrip()
    return text


def parse_incremental(text: str) -> list[Any]:
    """Parse one-or-more concatenated JSON documents (streamed Relay frames).

    Relay streamed responses arrive as back-to-back JSON objects with
    only whitespace between them (docs/04 §3.2). A trailing partial
    object — a mid-flush read — is left for the caller: ``raw_decode``
    stops at the last complete document rather than raising.

    Args:
        text: Raw (guard-stripped) response body.

    Returns:
        Every complete JSON document in arrival order; empty list when
        the body holds no parseable document.
    """
    docs: list[Any] = []
    dec = json.JSONDecoder()
    idx, n = 0, len(text)
    while idx < n:
        while idx < n and text[idx] in " \t\r\n":
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = dec.raw_decode(text, idx)
        except json.JSONDecodeError:
            break
        docs.append(obj)
        idx = end
    return docs


def merge_docs(docs: list[Any]) -> dict[str, Any]:
    """Deep-merge streamed Relay frames into one payload (later wins).

    Later frames carry refreshed slices of keys delivered by earlier
    frames (docs/04 §3.2 incremental delivery), so last-write-wins is
    the correct fold. Non-dict frames are skipped.

    Args:
        docs: Parsed JSON documents from :func:`parse_incremental`.

    Returns:
        One merged mapping; empty dict when no frame was a dict.
    """
    merged: dict[str, Any] = {}
    for doc in docs:
        if isinstance(doc, dict):
            merged = _deepmerge(merged, doc)
    return merged


def _deepmerge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Recursive dict merge: ``b``'s leaves win; shared dict keys merge."""
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deepmerge(out[k], v)
        else:
            out[k] = v
    return out
