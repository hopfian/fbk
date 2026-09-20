"""Exit-code family resolution (just-landed fix, wave 2).

The trap: an exact-class ``_EXIT_CODES.get(type(exc))`` lookup misses
SUBCLASSES — ``RegistryLoadError`` (a RegistryMissError raised by a
corrupt-but-present registry file) resolved to the generic 2 instead of
its family's 6. The fix walks the table with isinstance: every error in
a family exits with the family's code.

Complements test_retry_overview.py (retry wiring, the 5/3/130 codes) —
this file pins the whole family table and subclass resolution.

Unit (offline): stub args namespaces + handler fns raising typed errors
through run_command; no session, no network.
"""
from __future__ import annotations

import argparse

import pytest

from commands.common import _exit_code, run_command
from graphql.errors import (
    CheckpointError,
    DocIdStaleError,
    FBGraphError,
    GraphQLProtocolError,
    NotLoggedInError,
    RateLimitedError,
    RegistryLoadError,
    RegistryMissError,
)
from transport.cookies import CookieLoadError
from transport.session import FingerprintRejectedError


def _ns(**kw) -> argparse.Namespace:
    """A minimal command namespace (retry default mirrors the parsers)."""
    base = {"retry": 0}
    base.update(kw)
    return argparse.Namespace(**base)


def _raising(exc: Exception):
    """A command handler that raises ``exc`` and nothing else."""
    def fn(args):
        raise exc
    return fn


class TestSubclassResolution:
    """Pins the subclass fix itself: a RegistryLoadError command exits 6."""

    def test_registry_load_error_is_a_registry_miss_subclass(self):
        """The family relationship the old exact-class lookup missed."""
        assert issubclass(RegistryLoadError, RegistryMissError)
        assert issubclass(RegistryLoadError, FBGraphError)

    def test_registry_load_error_command_exits_6(self, capsys):
        """The wave-2 fix: 6 (re-harvest guidance), never the generic 2."""
        rc = run_command(_raising(RegistryLoadError("corrupt registry")),
                         _ns())
        assert rc == 6

    def test_registry_miss_command_exits_6(self, capsys):
        rc = run_command(_raising(RegistryMissError("no such name")), _ns())
        assert rc == 6

    def test_subclass_resolution_is_never_retried(self):
        """Only RateLimitedError clears on its own — a registry failure
        with --retry N burns exactly one call and still exits 6."""
        fn = _raising(RegistryLoadError("corrupt registry"))
        rc = run_command(fn, _ns(retry=3))
        assert rc == 6


class TestSecondBranchSubclassResolution:
    """Contract hygiene (second branch): the cookie/fingerprint families
    route through the same isinstance walk — a future subclass exits
    with its family's code, never the generic 2."""

    def test_cookie_load_error_subclass_command_exits_7(self, capsys):
        class ExpiredJarError(CookieLoadError):
            pass

        assert run_command(_raising(ExpiredJarError("subclass")), _ns()) == 7

    def test_fingerprint_rejected_error_subclass_command_exits_8(self, capsys):
        class EdgeBlockedError(FingerprintRejectedError):
            pass

        assert run_command(_raising(EdgeBlockedError("subclass")), _ns()) == 8


class TestFamilyCodesViaRunCommand:
    """Pins the full typed-error -> exit-code table through run_command."""

    @pytest.mark.parametrize("exc, code", [
        (FBGraphError("unclassified wire failure"), 2),
        (GraphQLProtocolError("variable coercion"), 2),
        (DocIdStaleError("deploy rolled the doc_id"), 2),
        (NotLoggedInError("session expired"), 3),
        (CheckpointError("integrity challenge"), 4),
        (RateLimitedError("soft block"), 5),
        (CookieLoadError("jar unreadable"), 7),
        (FingerprintRejectedError("edge rejected TLS identity"), 8),
    ])
    def test_family_exit_codes(self, exc, code, capsys):
        assert run_command(_raising(exc), _ns()) == code

    def test_plain_graph_error_exits_2(self, capsys):
        """The base-class fallback stays the generic 2, never KeyError."""
        assert run_command(_raising(FBGraphError("anything")), _ns()) == 2


class TestExitCodeLookup:
    """Pins _exit_code's isinstance-ordered walk over the family table."""

    def test_every_family_and_subclass_resolves(self):
        for exc, code in [
            (NotLoggedInError("x"), 3),
            (CheckpointError("x"), 4),
            (RateLimitedError("x"), 5),
            (RegistryMissError("x"), 6),
            (RegistryLoadError("x"), 6),   # the subclass the fix restored
            (FBGraphError("x"), 2),
            (GraphQLProtocolError("x"), 2),
            (DocIdStaleError("x"), 2),
            (CookieLoadError("x"), 7),     # the typed errors of the second
            (FingerprintRejectedError("x"), 8),  # branch walk the same table
        ]:
            assert _exit_code(exc) == code, f"{type(exc).__name__} -> {code}"
