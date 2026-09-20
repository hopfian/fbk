"""Persistent bootstrap token cache — the single biggest volume-reduction
lever in the package (docs/16 §P11-1).

Before this module: every CLI invocation created a Session, bootstrapped
the full homepage (1 request, ~2-5MB body) to harvest fb_dtsg/lsd, then
made the actual API call. A typical feed read → react → comment workflow
cost 6+ requests and 3 full homepage downloads.

After: the bootstrap result is cached to disk (state/token_cache.json)
with a TTL. Subsequent invocations reuse the cached tokens for their full
TTL window and re-bootstrap only on expiry (or on a dtsg-rejection error,
which the GraphQL client's auto-refresh already handles). One command =
one request.

TTL SEMANTICS:

  Default TTL: 15 minutes. Live DTSG tokens carry their own expiry in the
  ``:1:<unix>`` suffix (docs/15 §2, docs/03 §4), observed as a multi-day
  validity window — 15 minutes sits far inside it, so cache-driven token
  reuse is always server-legal, and the GraphQL client's auto-refresh
  (docs/04 §3.4) catches any edge case where a cached token was revoked
  early.

SECURITY BOUNDARY:

  The cache file holds only the TOKEN VALUES (fb_dtsg, lsd) and the
  non-secret bootstrap metadata (user_id, revision, bundle URL count).
  It NEVER holds cookie values — those stay in cookies.txt. SecretStr
  fields redact in any ``repr()``/``str()``/``model_dump()`` path; the
  ``field_serializer`` below pins that redaction for serialization
  (docs/12 §4 secret-hygiene discipline).

USER-DOC ANCHOR: cli/docs/03-session-and-auth.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, SecretStr, field_serializer

from auth.state import LoginState


class CachedBootstrap(BaseModel):
    """The disk-persisted token cache entry (state/token_cache.json shape).

    Represents one successful page bootstrap distilled to its reusable
    core: the CSRF token pair plus identity metadata. The two token
    fields are wrapped in ``SecretStr`` so no ``repr()``/``str()`` path
    can leak them into logs or journals (docs/12 §4).
    """

    # session CSRF token; live format ``NAf…:1:<unix>`` (docs/15 §2, docs/03 §4)
    fb_dtsg: SecretStr
    lsd: SecretStr | None = None   # coarse browser-epoch CSRF token (docs/03 §5); rides x-fb-lsd
    user_id: str | None = None    # viewer uid (== c_user)
    user_name: str | None = None  # display name from CurrentUserInitialData
    revision: str | None = None   # server_revision deploy fingerprint (docs/15 §2 SiteData frame)
    state: str = "logged_in"     # harvest state; is_fresh() trusts only 'logged_in' entries
    cached_at: float = 0.0       # epoch seconds when the entry was written
    expires_at: float = 0.0      # epoch seconds TTL deadline; is_fresh() rejects past-due entries

    @field_serializer("fb_dtsg", "lsd")
    @classmethod
    def _redact_secrets(cls, value: SecretStr | None) -> str | None:
        """model_dump() emits the redacted string, never the raw token.

        ``str()`` on a SecretStr yields the ``'**********'`` redaction, so
        any serialized view of this model — JSON dumps, journal emission,
        debug prints — is safe by construction.
        """
        return str(value) if value is not None else None

    def is_fresh(self, now: float | None = None) -> bool:
        """Whether the cache entry is within its TTL window.

        Args:
            now: Explicit epoch-seconds clock; defaults to ``time.time()``.
                Tests inject deterministic values.

        Returns:
            True only when ``now`` precedes ``expires_at`` AND the entry was
            harvested in the ``logged_in`` state — a checkpoint or
            logged-out bootstrap must never round-trip as reusable tokens.
        """
        check = now if now is not None else time.time()
        return check < self.expires_at and self.state == LoginState.LOGGED_IN.value

    def dtsg(self) -> str | None:
        """The raw DTSG token; None when absent or empty.

        Unwrap BEFORE the emptiness check: testing the SecretStr
        wrapper makes emptiness truthiness version-dependent (newer
        pydantic bools SecretStr("") False; older versions True), and
        a miss here must never round-trip as a fresh empty-token
        entry — it has to surface as a cache miss instead.
        """
        value = self.fb_dtsg.get_secret_value() if self.fb_dtsg else ""
        return value or None

    def lsd_value(self) -> str | None:
        """The raw lsd token; None when absent or empty.

        Applies the same unwrap-before-check discipline as
        :meth:`dtsg`: the SecretStr wrapper must never decide
        emptiness, only the unwrapped value can.
        """
        value = self.lsd.get_secret_value() if self.lsd else ""
        return value or None


class _BootstrapSource(Protocol):
    """The structural surface :meth:`TokenCache.save` consumes from a
    bootstrap: the raw CSRF token pair plus the identity metadata.

    This types ``save``'s documented contract — "an
    fbk.auth.bootstrap.Bootstrap (or anything with the same attribute
    surface)" — as a checked protocol instead of prose.
    ``auth.bootstrap.Bootstrap`` satisfies it structurally, so no import
    of auth.bootstrap (and no import cycle) is created. ``state`` is
    typed LoginState (the Bootstrap surface); save()'s runtime hasattr
    fallback keeps tolerating plain strings regardless.
    """

    user_id: str | None
    user_name: str | None
    revision: str | None
    state: LoginState

    def dtsg(self) -> str | None: ...
    def lsd_value(self) -> str | None: ...


class TokenCache:
    """Disk-backed fb_dtsg/lsd cache with TTL expiry.

    The cache file lives at ``state/token_cache.json`` (the state
    directory is already gitignored — docs/12 §4 repo security). A
    corrupt or hand-edited cache file always fails SOFT: ``load``
    returns None and the next bootstrap regenerates it.

    TTL default: 15 minutes. The live-captured DTSG tokens carry their own
    expiry in the ``:1:<unix>`` suffix (docs/03 §4); 15 minutes is well
    inside the observed multi-day validity window, so cache-driven token
    reuse is always server-legal. The auto-refresh in GraphQLClient still
    catches any edge case where a cached token was revoked early.
    """

    DEFAULT_TTL_S: float = 15 * 60.0

    def __init__(self, cache_path: Path | str, *, ttl_s: float | None = None):
        """Bind the cache to a file with a TTL.

        Args:
            cache_path: The JSON cache file (conventionally
                ``state/token_cache.json``); created lazily by :meth:`save`,
                never here.
            ttl_s: Entry lifetime in seconds; defaults to
                :attr:`DEFAULT_TTL_S` (15 minutes — far inside the observed
                multi-day DTSG validity window, docs/03 §4).
        """
        self.path = Path(cache_path)
        self.ttl_s = ttl_s if ttl_s is not None else self.DEFAULT_TTL_S

    def load_diagnosed(self, *, now: float | None = None
                       ) -> tuple[CachedBootstrap | None, str]:
        """Load with a machine-readable miss reason (the healing hook).

        Behaviorally identical to :meth:`load` — same fail-soft contract,
        same ``None`` on every miss — but pairs the result with a reason
        token so the self-healing coordinator (src/healing.py) can
        distinguish the benign misses (``absent``, ``expired``,
        ``stale-state``) from the heal-worthy ones (``corrupt``,
        ``invalid-shape``) without re-reading the file.

        Args:
            now: Explicit epoch-seconds clock for TTL evaluation; defaults
                to ``time.time()``.

        Returns:
            ``(entry, "ok")`` on a fresh valid hit; ``(None, reason)``
            otherwise, with reason one of ``absent`` / ``expired`` /
            ``stale-state`` / ``corrupt: <exception repr>`` /
            ``invalid-shape``.
        """
        check = now if now is not None else time.time()
        if not self.path.is_file():
            return None, "absent"
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return None, f"corrupt: {exc!r}"
        if not isinstance(raw, dict):
            return None, "invalid-shape"
        dtsg = raw.get("fb_dtsg")
        lsd = raw.get("lsd")
        if not isinstance(dtsg, str) or not dtsg:
            return None, "invalid-shape"  # missing/empty/non-string dtsg
        if lsd is not None and not isinstance(lsd, str):
            return None, "invalid-shape"
        try:
            entry = CachedBootstrap(
                fb_dtsg=SecretStr(dtsg),
                lsd=SecretStr(lsd) if lsd else None,
                user_id=raw.get("user_id"),
                user_name=raw.get("user_name"),
                revision=raw.get("revision"),
                state=raw.get("state", ""),
                cached_at=raw.get("cached_at", 0),
                expires_at=raw.get("expires_at", 0),
            )
        except (AttributeError, TypeError, ValueError, KeyError) as exc:
            return None, f"corrupt: {exc!r}"
        if not entry.is_fresh(check):
            state = raw.get("state", "")
            return None, ("stale-state" if state != LoginState.LOGGED_IN.value
                          else "expired")
        return entry, "ok"

    def load(self, *, now: float | None = None) -> CachedBootstrap | None:
        """Load a fresh cache entry, or None when absent/expired/invalid.

        Fail-soft by design (§5.5): a corrupt cache file (bad JSON,
        non-dict root, missing/empty/non-string dtsg, wrong-typed fields)
        yields None — never an exception — so the caller regenerates the
        tokens with a full bootstrap. A cache that could crash a command
        would be strictly worse than no cache.

        Args:
            now: Explicit epoch-seconds clock for TTL evaluation; defaults
                to ``time.time()``.

        Returns:
            The wrapped entry, or None when the file is absent, stale,
            structurally invalid, or was harvested in a non-logged-in
            state.
        """
        entry, _reason = self.load_diagnosed(now=now)
        return entry

    def save(self, bootstrap: _BootstrapSource) -> None:
        """Persist the token pair + identity metadata to disk.

        Accepts an fbk.auth.bootstrap.Bootstrap (or anything with the
        same attribute surface). Cookie values are NEVER written — only
        the CSRF token pair and identity metadata. The cache file lives
        in the gitignored state/ directory.

        SecretStr values are extracted explicitly (model_dump_json would
        redact them to '**********' — the cache needs the real tokens).

        Note:
            ATOMIC replace (temp + ``os.replace``): a torn cache file
            still fails soft to a cache miss on the next :meth:`load`
            (one extra bootstrap, never a wrong token), but a cache
            write racing a process kill mid-``save`` should not force
            that cost — same discipline as the governor's state file.
        """
        now = time.time()
        entry = {
            "fb_dtsg": bootstrap.dtsg() or "",
            "lsd": bootstrap.lsd_value(),
            "user_id": bootstrap.user_id,
            "user_name": bootstrap.user_name,
            "revision": bootstrap.revision,
            "state": (bootstrap.state.value
                      if hasattr(bootstrap.state, "value")
                      else str(bootstrap.state)),
            "cached_at": now,
            "expires_at": now + self.ttl_s,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(entry, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def invalidate(self) -> None:
        """Drop the cache (after a dtsg rejection or on operator demand).

        Absent file is not an error; an unremovable file is suppressed —
        the in-memory Bootstrap is already gone and the TTL makes any
        surviving file self-expiring within minutes.
        """
        if self.path.is_file():
            with contextlib.suppress(OSError):
                self.path.unlink()

    def status(self) -> dict[str, Any]:
        """A safe report for the governor status command.

        Returns:
            Cache health and metadata: ``cached`` (bool), ``path``, the
            configured ``ttl_s``, and — on a hit — the non-secret
            ``cached_at``/``expires_at``/``revision``/``user_id`` fields.
            Token VALUES never appear: the report is printed and
            journal-safe by construction.
        """
        entry = self.load()
        if entry is None:
            return {"cached": False, "path": str(self.path),
                    "ttl_s": self.ttl_s}
        return {
            "cached": True,
            "path": str(self.path),
            "ttl_s": self.ttl_s,
            "cached_at": entry.cached_at,
            "expires_at": entry.expires_at,
            "revision": entry.revision,
            "user_id": entry.user_id,
        }
