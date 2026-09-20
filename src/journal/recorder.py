"""Append-only JSONL journals with mandatory secret redaction.

Rule (docs/11 §7): a journal may hold URLs, sizes, statuses and friendly
names — but every cookie/token-shaped value is replaced by a salted
fingerprint BEFORE it hits disk. Secrets only ever live in cookies.txt
(docs/12 §8: the journals are the dataset of the study — versioned,
greppable, and safe to keep).

SECURITY BOUNDARY:

  ALL_SECRETS is the single redaction vocabulary and it is deliberately a
  SUPERSET of everything secret-shaped the wire can carry:

  * cookies — xs, datr, c_user, sb, fr, presence (constants.SECRET_COOKIE_NAMES)
  * form/header tokens — fb_dtsg, lsd (docs/03 §4-§5)
  * API-plane credentials — access_token (docs/03 §7), sessionid
    (Instagram-family session cookie, docs/03 §1.2)
  * surface-scoped secrets — privacy_write_id (the settings privacy
    renderer-scope id, docs/15 §P3)
  * bootstrap pair secrets beyond the constants vocabulary — logout_hash
    (the logout.php POST pair, docs/15 §2) and async_get_token (the
    DTSGInitData frame's second secret, docs/15 §2). Bootstrap.safe_dict()
    already fingerprints these, but journal redaction must cover them
    independently (docs/11 §7 defense-in-depth: never weaker, never assumed).

  Redaction happens at write time in :meth:`JSONLJournal.record` — no
  journal entry reaches disk unredacted, and the single-argument
  :func:`redact_entry` contract makes the whole mapping the unit of
  redaction, so no caller can forget a name.

USER-DOC ANCHOR: cli/docs/09-safety-and-opsec.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import json
import sys
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, cast

import constants as C
from transport.cookies import redact

# Package-local additions beyond C.SECRET_COOKIE_NAMES / C.SECRET_TOKEN_NAMES
# (constants.py is outside this package's ownership). logout_hash pairs with
# logout.php and async_get_token is the DTSG frame's second secret (docs/15
# §2) — Bootstrap.safe_dict() already fingerprints them, but journal redaction
# must cover them too (docs/11 §7: defense-in-depth, never weaker).
_EXTRA_SECRET_NAMES: frozenset[str] = frozenset({"logout_hash", "async_get_token"})

ALL_SECRETS: frozenset[str] = (
    C.SECRET_COOKIE_NAMES | C.SECRET_TOKEN_NAMES | _EXTRA_SECRET_NAMES)


def redact_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-copy an entry with every secret-named value redacted.

    Single-argument API: takes the whole mapping, returns a redacted copy
    (the input is never mutated) — the caller cannot cherry-pick which
    names to redact, so the coverage of ALL_SECRETS applies uniformly to
    every call site. Secret names come from ALL_SECRETS.

    Args:
        entry: One journal record as a plain mapping (request/response
            metadata, session markers, ...). Values of any type may appear;
            nested structures are walked by ``transport.cookies.redact``.

    Returns:
        A new dict safe to serialize to disk: every secret-named value
        replaced by a salted sha256 fingerprint (docs/11 §7 form).
    """
    # transport.cookies.redact is annotated ``-> Any`` (transport/ is outside
    # this typing wave's scope, so its signature cannot be tightened here).
    # Its dict-in/dict-out copy rule — "a structurally identical copy" per
    # its own docstring — is asserted by this cast rather than by a bare
    # type-ignore, and can simply be dropped once transport/ is strictly
    # typed.
    return cast("dict[str, Any]", redact(dict(entry), ALL_SECRETS))


class JSONLJournal:
    """One JSONL file, one journal. Records are redacted at write time.

    Append-per-record, no fsync by design: the journal sits on the
    latency path of every governed request, and an fsync per line would
    dominate the pacing budget — the accepted trade is that a crash can
    tear the final line, which costs at most one record of forensic
    continuity, never a secret (redaction happened before the write).
    """

    def __init__(self, path: str | Path):
        """Bind the journal to a file, creating the parent directory.

        Args:
            path: The JSONL file; created lazily on first :meth:`record`,
                only its parent directory is ensured here.
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, entry: Mapping[str, Any]) -> None:
        """Append one redacted entry (a 'ts' is added when absent).

        Args:
            entry: The record to persist — redacted first via
                :func:`redact_entry`, so no caller can bypass the
                security boundary by passing a raw structure.

        Note:
            Side effect: one line appended to ``self.path`` (an explicit
            open/write/close per record — see class docstring for why no
            fsync and no held handle). A ``'ts'`` epoch-seconds timestamp
            is prepended unless the entry already carries one.
        """
        safe = redact_entry(entry)
        if "ts" not in safe:
            safe = {"ts": time.time(), **safe}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(safe, ensure_ascii=False, default=str) + "\n")

    def session_start(self, meta: Mapping[str, Any]) -> None:
        """Record a ``session_start`` event with the given metadata.

        Args:
            meta: Bootstrap/session context — pass redaction-ready
                structures (e.g. ``Bootstrap.safe_dict()``); the same
                ALL_SECRETS redaction applies as everywhere else.
        """
        self.record({"event": "session_start", **meta})

    def read_all(self, *, strict: bool = True) -> list[dict[str, Any]]:
        """Load every entry (for tests and post-run analysis).

        Args:
            strict: Loud by default. With ``strict=True`` any malformed
                line — including a crash-torn trailing line (the
                no-fsync trade-off above) — raises; analysis tooling
                must surface that rather than silently drop evidence.
                With ``strict=False`` a torn TRAILING line — the only
                line a crash mid-write can damage — is skipped with a
                stderr note. Interior corrupt lines stay loud in both
                modes: they are disk rot or tampering, never a torn
                append.

        Returns:
            All parsed records in on-disk order, one dict per non-empty
            line.

        Raises:
            json.JSONDecodeError: If a line is malformed and ``strict``
                is True, or an INTERIOR line is malformed in either
                mode.
        """
        with open(self.path, encoding="utf-8") as fh:
            lines = fh.readlines()
        out: list[dict[str, Any]] = []
        last = len(lines) - 1
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                if strict or i != last:
                    raise
                print(f"warning: journal {self.path.name} ends with a torn, "
                      f"unparseable line — skipped (read_all strict=False)",
                      file=sys.stderr)
        return out


def iter_journals(journal_dir: str | Path) -> Iterable[Path]:
    """All journal files in a directory, oldest name-first.

    Args:
        journal_dir: Directory to scan for ``*.jsonl`` files.

    Returns:
        The journal paths in lexicographic name order — journal basenames
        are datestamped, so name order is chronological. An absent or
        non-directory path yields an empty sequence, never an error.
    """
    d = Path(journal_dir)
    return sorted(d.glob("*.jsonl")) if d.is_dir() else []
