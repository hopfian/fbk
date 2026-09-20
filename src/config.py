"""Runtime configuration — fully self-contained inside cli/ (docs/12 §8).

The cli/ directory is the package root. Every runtime path resolves
inside it, making the package portable: copy cli/ anywhere, put a
cookies.txt inside it, and everything works. This is the self-containment
contract — nothing outside cli/ is ever referenced at runtime, so the
directory can be cloned, zipped, and vendored without environment fixes.

    cli/
        cookies.txt     ← the session cookie jar (gitignored)
        data/           ← immutable runtime data (registries, templates)
        state/          ← mutable runtime state (governor, token cache)
        src/            ← the Python source
        tests/          ← the test suite

ARCHITECTURE:

  Path resolution walks up exactly one level from this module:
  ``Path(__file__).resolve().parents[1]`` is the directory containing
  ``pyproject.toml`` — i.e. cli/ itself. Anchoring on the package root
  (not on ``cwd``) keeps every derived path stable no matter where the
  interpreter is invoked from. Environment overrides, applied in this
  order:

  * ``FBK_ROOT``: an explicit package root (for tests, CI, alternate installs)
  * ``FBK_COOKIES``: an explicit cookies.txt path
  * ``FBK_IMPERSONATE``: the curl_cffi TLS/h2 target, overriding the pinned
    ``constants.IMPERSONATE_DEFAULT`` (docs/16 §6 pinning policy)

USER-DOC ANCHOR: cli/docs/02-configuration.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field

from constants import GRAPHQL_ENDPOINT, IMPERSONATE_DEFAULT

_ENV_ROOT = "FBK_ROOT"
_ENV_COOKIES = "FBK_COOKIES"


class Config(BaseModel):
    """Resolved runtime configuration. Path fields are absolute.

    All paths live inside the package root (cli/) — nothing outside the
    cli/ directory is ever referenced at runtime (the docs/12 §8
    self-containment contract). Construct via :meth:`discover`; direct
    construction is for tests that need synthetic trees.
    """

    root: Path = Field(..., description="Package root (cli/) — the pyproject.toml parent.")
    cookies_path: Path = Field(
        ...,
        description="Netscape-format cookie jar (docs/03 §9.1); the only secret-bearing input.")
    data_dir: Path = Field(
        ...,
        description="Immutable runtime data: doc_id registries, capture templates (docs/13 §2).")
    state_dir: Path = Field(
        ...,
        description="Mutable runtime state: journals, governor state, token cache (gitignored).")
    profile_path: Path | None = Field(
        default=None,
        description="Coherent ClientProfile JSON (docs/09); None when absent — the "
                    "transport then falls back to its built-in default profile.")
    impersonate: str = Field(
        default=IMPERSONATE_DEFAULT,
        description="curl_cffi TLS/h2 impersonation target (docs/16 §6); pinned by default, "
                    "overridable via FBK_IMPERSONATE.")
    timeout: float = Field(
        default=30.0, gt=0,
        description="Per-request HTTP timeout in seconds; strictly positive.")
    graphql_endpoint: str = Field(
        default=GRAPHQL_ENDPOINT,
        description="The persisted-query POST endpoint (docs/04 §1); the "
                    "default IS constants.GRAPHQL_ENDPOINT — single source "
                    "of truth, no literal drift.")

    @property
    def assets_dir(self) -> Path:
        """Backward-compat alias for ``data_dir`` (the pre-v3 name).

        Retained because ``DocIdRegistry.from_assets`` and several surfaces
        still name the parameter ``assets_dir``; the two words describe the
        same directory.
        """
        return self.data_dir

    @property
    def journal_dir(self) -> Path:
        """Backward-compat alias for ``state_dir`` (the pre-v3 name)."""
        return self.state_dir

    @classmethod
    def discover(cls, root: Path | str | None = None) -> Config:
        """Resolve the runtime config from the first available root.

        Resolution order (first wins): the explicit ``root`` argument,
        ``FBK_ROOT``, then the package root derived from this module's
        location (``src/config.py`` → ``src`` → ``cli/`` — the directory
        containing pyproject.toml).

        Args:
            root: Explicit package root; overrides ``FBK_ROOT`` when given.
                Tests pass synthetic trees here.

        Returns:
            A fully resolved Config with absolute paths for root, data/,
                state/, cookies.txt (``FBK_COOKIES`` override applied), and
                profile.json (``profile_path`` stays None until the file
                exists on disk).
        """
        if root is None:
            root = os.environ.get(_ENV_ROOT)
        if root is None:
            # Two install shapes share one rule (wheel packaging, v3):
            #   * editable: src/config.py -> parents[1] = cli/ (package root)
            #   * wheel: config.py sits FLAT in site-packages next to the
            #     shipped data/ dir -> parents[0] = site-packages root.
            # The layout probe is the src/ directory name under editable.
            here = Path(__file__).resolve()
            root = here.parents[1] if here.parent.name == "src" else here.parent
        root = Path(root).resolve()

        data = root / "data"
        state = root / "state"
        cookies = Path(os.environ.get(_ENV_COOKIES, root / "cookies.txt"))
        profile = data / "profile.json"
        return cls(
            root=root,
            cookies_path=cookies,
            data_dir=data,
            state_dir=state,
            profile_path=profile if profile.is_file() else None,
            impersonate=os.environ.get("FBK_IMPERSONATE", IMPERSONATE_DEFAULT),
        )

    def journal_file(self, name: str) -> Path:
        """Resolve a journal path under ``state/``.

        Args:
            name: Journal basename without extension; ``"<name>.jsonl"`` is
                appended (e.g. ``"session"`` → ``state/session.jsonl``).

        Returns:
            The absolute JSONL path — the file itself is created lazily by
            the journal recorder, never here.
        """
        return self.state_dir / f"{name}.jsonl"
