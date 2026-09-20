"""with_session(args) — the command-layer session lifecycle seam.

Contract (landed in commands/common.py): ``with_session`` scopes one
Session to a command body, closing it on scope exit whatever the exit
reason — exactly once, with close errors suppressed. Two accepted forms:
the build form (a parsed args namespace, resolved through this module's
``new_session`` at call time) and the adopt form (an already-built
session handed in directly, the form the command modules use).

Unit (offline): monkeypatched ``new_session`` + stub sessions counting
close() calls; no transport, no network.
"""
from __future__ import annotations

import argparse

import pytest

import commands.common as common


def _ns(**kw) -> argparse.Namespace:
    base = {"retry": 0, "command": "test"}
    base.update(kw)
    return argparse.Namespace(**base)


class _StubSession:
    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


def _require_contract():
    if not hasattr(common, "with_session"):
        pytest.skip("[CONTRACT] with_session has not landed in "
                    "commands/common yet")


class TestWithSessionContract:
    """Closes exactly once, on every exit path, without masking."""

    def test_build_form_yields_the_session_and_closes_once(self, monkeypatch):
        _require_contract()
        stub = _StubSession()
        monkeypatch.setattr(common, "new_session", lambda args, **kw: stub)
        with common.with_session(_ns()) as session:
            assert session is stub
        assert stub.close_calls == 1

    def test_build_form_forwards_journal_name(self, monkeypatch):
        _require_contract()
        seen: list = []

        def fake_new_session(args, *, journal_name=None):
            seen.append(journal_name)
            return _StubSession()

        monkeypatch.setattr(common, "new_session", fake_new_session)
        with common.with_session(_ns(), journal_name="custom"):
            pass
        assert seen == ["custom"]

    def test_adopt_form_closes_an_already_built_session(self):
        _require_contract()
        stub = _StubSession()
        with common.with_session(stub) as session:
            assert session is stub
        assert stub.close_calls == 1

    def test_closes_when_the_body_raises(self, monkeypatch):
        _require_contract()
        stub = _StubSession()
        monkeypatch.setattr(common, "new_session", lambda args, **kw: stub)
        with (pytest.raises(RuntimeError, match="body failed"),
              common.with_session(_ns())):
            raise RuntimeError("body failed")
        assert stub.close_calls == 1

    def test_suppresses_close_errors(self, monkeypatch):
        _require_contract()

        class _ExplodingSession(_StubSession):
            def close(self):
                super().close()
                raise RuntimeError("close blew up")

        stub = _ExplodingSession()
        monkeypatch.setattr(common, "new_session", lambda args, **kw: stub)
        with common.with_session(_ns()):
            pass  # a clean body must not see the close error
        assert stub.close_calls == 1

    def test_body_exception_wins_over_close_error(self, monkeypatch):
        _require_contract()

        class _ExplodingSession(_StubSession):
            def close(self):
                super().close()
                raise RuntimeError("close blew up")

        stub = _ExplodingSession()
        monkeypatch.setattr(common, "new_session", lambda args, **kw: stub)
        with (pytest.raises(RuntimeError, match="body failed"),
              common.with_session(_ns())):
            raise RuntimeError("body failed")
        assert stub.close_calls == 1
