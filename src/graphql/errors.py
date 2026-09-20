"""Typed GraphQL-layer errors with operator guidance baked in.

Every class documents its wire trigger and the caller's mandated
response, so exception handling encodes containment policy instead of
guesswork: docs/04 §3.4 (error-envelope catalogue), docs/10 (soft-block
symptom catalogue), and docs/11 §5 (containment principle —
enforcement is met with disengagement, never escalating retries).

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class FBGraphError(Exception):
    """Base class for every structured failure in this package.

    Attributes:
        code: The numeric GraphQL error code from the envelope, when one
            was present (docs/04 §3.4 catalogue; ``None`` for
            transport-level triggers like redirects).
        raw: The raw structured payload (the single error object or the
            full envelope) preserved for journaling and post-hoc
            analysis; never secret-bearing.
    """

    def __init__(self, message: str, *, code: int | None = None,
                 raw: Any | None = None):
        """Initialize with the operator-facing message plus envelope evidence.

        Args:
            message: Human-readable summary for logs and the operator.
            code: Wire error code, when the failure came from a
                structured envelope.
            raw: Raw envelope (or error object) for downstream inspection.
        """
        super().__init__(message)
        self.code = code
        self.raw = raw


class NotLoggedInError(FBGraphError):
    """Session invalid / logged out / dtsg rejected — re-auth or re-bootstrap.

    Wire triggers (docs/04 §3.4): hard ``1357004`` ("Not logged in." —
    the ``xs`` binding is dead, docs/03 §3.3) and the DTSG-rejection
    pair ``1357051``/``1677047``. ``GraphQLClient.call`` cures the
    DTSG-rejection pair automatically with one re-bootstrap + retry;
    a ``NotLoggedInError`` that escapes the client means the refresh
    failed and the operator must re-auth.
    """


class CheckpointError(FBGraphError):
    """Account or device is in an integrity challenge (docs/07 §5). Halt.

    Wire triggers: CHECKPOINT_REQUIRED-labeled envelopes
    (``1384000``-family, docs/04 §3.4) and 302 redirects landing on
    ``/checkpoint``. Mandated response: full stop — the governor engages
    a long disengagement cooldown (docs/15 §P9-1); pushing through a
    checkpoint escalates account-level enforcement (docs/11 §5).
    """


class RateLimitedError(FBGraphError):
    """Soft block / rate limit — back off per docs/10 §7.

    Wire triggers: HTTP 403/429 at the edge,
    RATE_LIMITED_SUSPECTED-labeled envelopes, and the
    200-but-unparseable empty-body soft-block signature (docs/10 §3).
    Mandated response: disengage into the governor cooldown; the command
    layer retries with jittered backoff only (docs/15 §P8-3
    RetryPolicy) — never an immediate re-fire.
    """


class DocIdStaleError(FBGraphError):
    """doc_id rejected — the deploy rolled; re-harvest bundles (docs/13 §2).

    Wire trigger: DOC_ID_UNKNOWN / ``1570245``-family "Query with id ...
    not found" envelopes (docs/04 §3.4). Staleness is per-doc_id and
    batch-correlated — ids harvested together usually die together on a
    single build push (docs/04 §10). Mandated response: run the registry
    refresh (``graphql.registry_refresh``), not ad-hoc retries.
    """


class GraphQLProtocolError(FBGraphError):
    """Request-level protocol rejection (e.g. 1675012 variable coercion).

    Wire trigger: a structured error envelope that maps to no
    session/rate/staleness class — live-confirmed as code ``1675012``
    with messages like ``missing_required_variable_value`` /
    ``noncoercible_variable_value`` (docs/15 §4). ``raw`` carries the
    full envelope; the fix is in the caller's variables shape — the
    same body will fail identically on retry, so do not retry.
    """


class RegistryMissError(FBGraphError):
    """friendly_name not present in any harvested registry.

    Trigger: a strict ``DocIdRegistry.doc_id`` lookup, or no registry
    file present in assets at all. Mandated response: re-harvest bundles
    per docs/13 §2 and extend coverage to the surface's lazy-loaded
    routes — action surfaces ship bundles the homepage harvest never
    sees (docs/15 §P2-1).
    """


class RegistryLoadError(RegistryMissError):
    """A registry file exists but is corrupt (unparseable JSON / wrong schema).

    Subclasses :class:`RegistryMissError` so callers that already treat
    a registry miss as "re-harvest" keep working; the message names the
    offending file and the recovery action (delete or re-harvest,
    docs/13 §2). A corrupt file aborts the priority descent instead of
    silently falling back to staler data.
    """


class DryRunComplete(Exception):  # noqa: N818 (success sentinel, not an error)
    """Success sentinel: --dry-run planned one request and touched nothing.

    Raised by ``GraphQLClient.call``/``call_raw`` when the client runs
    in dry-run mode: the complete request plan (friendly_name, doc_id,
    endpoint, mutation classification, redacted variables) is already
    on stdout and NOTHING was sent — no transport call, no governor
    tick, no journal entry, no ``q`` advance (docs/11 §8 volume
    discipline: the budgets count wire volume, and a plan reaches no
    edge).

    Deliberately NOT an FBGraphError subclass: surface degradation
    handlers catch the family broadly (surfaces/measurement.py treats
    FBGraphError as a failed sample, surfaces/video_upload.py falls
    back to default config) — a SUCCESS sentinel must never be
    swallowed into a degraded path, it propagates untouched to
    ``commands.common.run_command``, which converts it to exit 0.
    That is also why it is absent from ``_EXIT_CODES``: it is a
    success, not a typed failure.

    Attributes:
        plan: The printed request plan — friendly_name, doc_id,
            endpoint, is_mutation, would_debit, and the variables
            already redacted via ``journal.recorder.redact_entry``
            (the ALL_SECRETS vocabulary, docs/11 §7).
    """

    def __init__(self, plan: Mapping[str, Any]):
        """Attach the printed plan to the success sentinel.

        Args:
            plan: The request plan exactly as printed (secret values
                already fingerprinted); carried as an attribute so the
                exit-0 path and tests can assert on it without
                re-parsing stdout.
        """
        self.plan = dict(plan)
        super().__init__(
            f"dry-run plan for {plan.get('friendly_name')!r} — nothing sent")


class DryRunRawSeamError(Exception):
    """A raw-seam HTTP call was attempted under ``--dry-run``.

    The upload/video surfaces bypass GraphQLClient through
    ``FBTransport.raw_post``/``raw_get`` (hand-built multipart and
    rupload dialects the form-only governed API cannot express,
    docs/15 §P5-1/P6-1) — a seam no request plan can describe.
    ``Session`` therefore installs a refusal guard on both methods
    when constructed with ``dry_run=True``, and this error is what the
    guard raises.

    Like :class:`DryRunComplete`, deliberately outside the FBGraphError
    family (degradation handlers must not swallow it) and absent from
    ``_EXIT_CODES``: run_command maps it to exit 1 — a failed
    precondition, the same contract slot a handler uses for "the
    command itself decided it cannot proceed".
    """
