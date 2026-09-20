"""CLI-local draft store: composed post SPECS saved under state/drafts/.

RECON GAP (native drafts): the harvested mutation registry carries no
ComposerDraft mutations — only the ads/lead-gen draft families — so
Facebook's own server-side draft surface is out of reach until a live
capture of it lands in data/. This module is the honest substitute: a
"draft" here is a CLI-LOCAL composed-post spec (the exact inputs
``fbk draft save`` accepted), persisted as one JSON file under
``state/drafts/<name>.json`` and publishable later through the same
FeedService.publish path ``fbk feed publish`` rides. Nothing is ever
sent to the edge by this module — it is pure offline state.

SECURITY BOUNDARY:

  Draft names are BARE basenames — the ``_journal_path`` discipline
  (commands/journal.py): anything empty, dot-segmented, or carrying a
  path separator is rejected BEFORE the ``state/drafts/`` path is ever
  resolved, so no invocation can read or write outside the drafts
  directory (tests pin the exact traversal matrix).

Writes are ATOMIC (temp file + ``os.replace`` — the governor/token-
cache pattern): a crash mid-save can never leave a half-written draft
behind, and the only .tmp sibling ever visible is the one os.replace
is about to consume. Corrupt existing files stay LOUD on load/list
(disk rot or tampering, never a torn append — this store writes whole
  files, not append-per-record lines).

USER-DOC ANCHOR: cli/docs/05-reference-content.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class DraftSpec(BaseModel):
    """One composed post: the exact publish inputs, persisted as JSON.

    Field semantics mirror ``fbk feed publish`` plus the composer extras
    ``FeedService.publish`` already accepts (``ai_label`` → the
    AI-disclosure toggle, ``background`` → the text-format preset id).
    The fields no captured mutation can carry yet (media, tags, feeling,
    activity, place) round-trip faithfully in the spec — the native-gap
    note in the module docstring — so a later capture can publish them
    without changing the store's schema.
    """

    name: str
    text: str
    privacy: str = "friends"
    ai_label: bool | None = None
    background: str | None = None
    media: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    feeling: str | None = None
    activity: str | None = None
    place: str | None = None
    created_at: str = ""


class DraftExistsError(RuntimeError):
    """A draft with this name already exists; --force is the opt-in.

    The journal-export convention (commands/journal.py cmd_export): an
    existing file is never silently overwritten — the caller reports and
    exits 1, the operator re-runs with the explicit overwrite flag.
    """


def is_valid_draft_name(name: str) -> bool:
    """True when ``name`` is a bare basename safe to resolve under drafts/.

    The ``_journal_path`` matrix (commands/journal.py): empty strings,
    dot-segments ("." / ".."), and anything carrying a path separator
    ("/" or "\\") are rejected before resolution, so the derived path can
    never escape ``state/drafts/``.

    Args:
        name: The operator-supplied ``--name`` value.

    Returns:
        True when the name may resolve inside the drafts directory.
    """
    return bool(name) and name not in {".", ".."} and "/" not in name and "\\" not in name


def _head(text: str, limit: int = 60) -> str:
    """A one-line summary head of the post text (first line, truncated)."""
    first = text.splitlines()[0] if text.splitlines() else text
    return first if len(first) <= limit else first[: limit - 1] + "…"


class DraftStore:
    """The state/drafts/ directory as a typed save/list/load/delete API.

    One JSON file per draft (``<name>.json``), atomic writes, name
    validation at the boundary — pure offline state, no session and no
    network anywhere in this class.
    """

    def __init__(self, state_dir: Path | str):
        """Bind the store to a state directory.

        Args:
            state_dir: The resolved ``Config.state_dir``; the store owns
                the ``drafts/`` child beneath it. The directory itself is
                created lazily on the first :meth:`save`, never here.
        """
        self.state_dir = Path(state_dir)
        self.dir = self.state_dir / "drafts"

    def _resolve(self, name: str) -> Path | None:
        """Resolve one draft NAME to its path, or None to refuse it.

        Args:
            name: The operator-supplied ``--name`` value.

        Returns:
            The intended ``<state>/drafts/<name>.json`` path, or None when
            the name could escape the drafts directory (invalid — the
            caller reports and exits 1, the house convention).
        """
        if not is_valid_draft_name(name):
            return None
        return self.dir / f"{name}.json"

    def exists(self, name: str) -> bool:
        """True when a draft with this (valid) name is on disk.

        Args:
            name: The draft name.

        Returns:
            False for invalid names and absent drafts alike — an invalid
            name can never exist, by the boundary rule.
        """
        path = self._resolve(name)
        return path is not None and path.is_file()

    def save(self, spec: DraftSpec, *, force: bool = False) -> Path:
        """Persist one draft spec atomically.

        Args:
            spec: The composed-post spec; its ``name`` selects the file.
            force: Overwrite an existing draft with the same name — the
                explicit opt-in (the journal-export convention; without
                it an existing draft is never silently clobbered).

        Returns:
            The written ``<state>/drafts/<name>.json`` path.

        Raises:
            ValueError: When the spec's name is path-shaped (the boundary
                matrix — the caller is expected to pre-validate and report
                exit 1, this is defense in depth).
            DraftExistsError: When the draft exists and ``force`` is not
                set; the caller reports and exits 1.
        """
        path = self._resolve(spec.name)
        if path is None:
            raise ValueError(
                f"invalid draft name {spec.name!r} — a bare name, no path "
                f"separators or dot-segments")
        if path.exists() and not force:
            raise DraftExistsError(
                f"draft {spec.name!r} already exists at {path}")
        self.dir.mkdir(parents=True, exist_ok=True)
        # ATOMIC replace (temp + os.replace — the governor/token-cache
        # pattern): a crash mid-save can never leave a half-written draft.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(spec.model_dump_json(indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return path

    def load(self, name: str) -> DraftSpec | None:
        """Read one draft back as a typed spec.

        Args:
            name: The draft name.

        Returns:
            The spec, or None when the name is path-shaped or no such
            draft exists (the caller reports and exits 1, the house
            precondition convention).

        Raises:
            pydantic.ValidationError: When the file exists but does not
                parse — corrupt files stay LOUD, never silently absent.
        """
        path = self._resolve(name)
        if path is None or not path.is_file():
            return None
        return DraftSpec.model_validate_json(path.read_text(encoding="utf-8"))

    def delete(self, name: str) -> bool:
        """Remove one draft.

        Args:
            name: The draft name.

        Returns:
            True when a draft was removed; False when the name is
            path-shaped or no such draft exists (the caller reports and
            exits 1 — the journal show/delete precondition).
        """
        path = self._resolve(name)
        if path is None or not path.is_file():
            return False
        path.unlink()
        return True

    def list(self) -> list[dict[str, Any]]:
        """Summarize every draft, name order.

        Returns:
            One summary dict per draft — name, created_at, the text
            head, and the media count — in lexicographic name order.
            An absent or empty drafts directory yields an empty list,
            never an error ("no drafts" is a reported fact).

        Raises:
            pydantic.ValidationError: When any draft file fails to parse
                (loud, same as :meth:`load`).
        """
        if not self.dir.is_dir():
            return []
        entries: list[dict[str, Any]] = []
        for path in sorted(self.dir.glob("*.json")):
            spec = DraftSpec.model_validate_json(
                path.read_text(encoding="utf-8"))
            entries.append({
                "name": spec.name,
                "created_at": spec.created_at,
                "text_head": _head(spec.text),
                "media_count": len(spec.media),
            })
        return entries
