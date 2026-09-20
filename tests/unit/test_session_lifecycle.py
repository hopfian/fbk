"""Session lifecycle regression (just-landed fix, wave 2).

Session gained close() (idempotent — it delegates to the transport, whose
own close is guarded) and __enter__/__exit__. These tests pin:

  * Session.close() delegates to transport.close() and is safe to re-call;
  * the UNDERLYING http session is released exactly once no matter how
    often close is invoked (or how many paths reach it);
  * the context manager closes on exit, both on success and on exception,
    and never swallows the exception;
  * FBTransport.close() itself is idempotent at its own level too.

Fully offline: Session is built against a tmp cookies.txt + tmp state dir
with session.FBTransport swapped for a spy that counts real closes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import session as session_module
from config import Config
from session import Session
from transport.session import FBTransport

CLI_ROOT = Path(__file__).resolve().parents[2]

COOKIES_TXT = (
    "# Netscape HTTP Cookie File\n"
    ".facebook.com\tTRUE\t/\tTRUE\t2147483647\tc_user\t12345678901234\n"
    ".facebook.com\tTRUE\t/\tTRUE\t2147483647\txs\tTEST-XS\n"
    ".facebook.com\tTRUE\t/\tTRUE\t2147483647\tdatr\tTEST-DATR\n"
)


class FakeUnderlyingSession:
    """The curl_cffi session stand-in: counts close() invocations."""

    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    def get(self, url, **kw):  # pragma: no cover - never used here
        raise AssertionError("no HTTP in lifecycle tests")

    def post(self, url, **kw):  # pragma: no cover - never used here
        raise AssertionError("no HTTP in lifecycle tests")


class CloseSpyTransport(FBTransport):
    """Real FBTransport, but the pooled session is a close-counting fake."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("impersonate", "chrome136")  # pin: no probe, no net
        super().__init__(*args, **kwargs)
        self.underlying = FakeUnderlyingSession()
        self._session = self.underlying


def make_session(tmp_path: Path, monkeypatch) -> tuple[Session, CloseSpyTransport]:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(COOKIES_TXT, encoding="utf-8")
    monkeypatch.setattr(session_module, "FBTransport", CloseSpyTransport)
    cfg = Config.discover(CLI_ROOT).model_copy(update={
        "cookies_path": cookies,
        "state_dir": tmp_path / "state",
    })
    session = Session(cfg, journal_name=None)
    assert isinstance(session.transport, CloseSpyTransport)
    return session, session.transport


class TestSessionClose:
    """Pins Session.close() delegation and the exactly-once release of the
    underlying session across repeated closes and context-manager paths."""

    def test_close_delegates_to_the_transport(self, tmp_path, monkeypatch):
        session, transport = make_session(tmp_path, monkeypatch)
        session.close()
        assert transport.underlying.close_calls == 1

    def test_close_is_idempotent_the_underlying_release_happens_once(
            self, tmp_path, monkeypatch):
        session, transport = make_session(tmp_path, monkeypatch)
        session.close()
        session.close()
        session.close()
        # however often the facade is asked, the pooled session dies once
        assert transport.underlying.close_calls == 1

    def test_context_manager_closes_on_exit(self, tmp_path, monkeypatch):
        session, transport = make_session(tmp_path, monkeypatch)
        with session as entered:
            assert entered is session
        assert transport.underlying.close_calls == 1

    def test_context_manager_closes_on_exception_and_propagates_it(
            self, tmp_path, monkeypatch):
        session, transport = make_session(tmp_path, monkeypatch)
        with pytest.raises(RuntimeError, match="boom"), session:
            raise RuntimeError("boom")
        assert transport.underlying.close_calls == 1

    def test_close_after_the_context_manager_still_releases_once(
            self, tmp_path, monkeypatch):
        session, transport = make_session(tmp_path, monkeypatch)
        with session:
            pass
        session.close()  # re-close must not double-release
        assert transport.underlying.close_calls == 1


class TestFBTransportClose:
    """Pins FBTransport's own idempotent close, its context manager, and
    containment of an underlying close() failure."""

    def _transport(self) -> CloseSpyTransport:
        transport = CloseSpyTransport({"c_user": "1", "xs": "x"})
        return transport

    def test_close_is_idempotent_at_the_transport_level(self):
        transport = self._transport()
        transport.close()
        transport.close()
        assert transport.underlying.close_calls == 1
        assert transport._closed is True

    def test_transport_context_manager_closes_on_exit(self):
        transport = self._transport()
        with transport as entered:
            assert entered is transport
        assert transport.underlying.close_calls == 1

    def test_close_swallows_underlying_close_failures(self):
        """The curl session close must never take the CLI down with it."""
        transport = self._transport()

        def boom():
            raise OSError("pool already drained")

        transport.underlying.close = boom
        transport.close()  # must not raise
        assert transport._closed is True
