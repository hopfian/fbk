"""ws_connect fallback-gate narrowing (just-landed fix).

Fallback to the legacy websockets adapter is reserved for ONE condition:
this curl_cffi build has no sync WS API at all (AttributeError /
NotImplementedError). A genuine handshake/network failure must PROPAGATE
— retrying the same endpoint with the legacy library's Python-ssl
ClientHello presents two different TLS identities to the edge in quick
succession, exactly the split-identity signal Phase 10 eliminated.

Unit (offline): curl_cffi's Session is swapped for raising stubs at the
resolved binding, ``_legacy_ws_connect`` records calls; no network.

Note: the patch targets ``curl_cffi.requests.Session`` itself — the one
object every binding style (module-top import or in-function lazy
import) resolves through.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import realtime.ws as ws_module


class _StubSession:
    """A curl_cffi Session look-alike whose ws_connect fails on demand."""

    def __init__(self, *, ws_connect=None, close_error=None):
        # the jar update must succeed — only ws_connect is under test
        self.cookies = SimpleNamespace(update=lambda *a, **k: None)
        self.closed = 0
        self._close_error = close_error
        if ws_connect is not None:
            self.ws_connect = ws_connect

    def close(self):
        self.closed += 1
        if self._close_error:
            raise self._close_error


def _install(monkeypatch, stub) -> None:
    """Point ws_connect's Session resolution at one pre-built stub."""
    import curl_cffi.requests as creq
    monkeypatch.setattr(creq, "Session", lambda: stub)


def _install_legacy(monkeypatch, calls):
    """Replace the legacy adapter with a call recorder returning a sentinel."""
    sentinel = object()

    def fake_legacy(url, *, cookies, headers, timeout):
        calls.append({"url": url, "cookies": cookies,
                      "headers": headers, "timeout": timeout})
        return sentinel

    monkeypatch.setattr(ws_module, "_legacy_ws_connect", fake_legacy)
    return sentinel


COOKIES = {"c_user": "1", "xs": "x"}
HEADERS = {"Origin": "https://www.facebook.com"}


class TestFallbackGate:
    """Pins the two-way gate: missing-API falls back, network errors
    propagate — never the reverse."""

    def test_missing_ws_api_falls_back(self, monkeypatch):
        """A Session with no ws_connect at all is the one fallback case."""
        calls: list[dict] = []
        sentinel = _install_legacy(monkeypatch, calls)
        stub = _StubSession()  # no ws_connect attribute
        _install(monkeypatch, stub)

        result = ws_module.ws_connect("wss://edge-chat.facebook.com/chat",
                                      cookies=COOKIES, headers=HEADERS,
                                      timeout=3.0)
        assert result is sentinel
        assert len(calls) == 1

    def test_not_implemented_falls_back(self, monkeypatch):
        """curl_cffi ships the name but raises NotImplementedError."""
        calls: list[dict] = []
        sentinel = _install_legacy(monkeypatch, calls)
        stub = _StubSession(
            ws_connect=lambda *a, **k: (_ for _ in ()).throw(
                NotImplementedError("no sync WS in this build")))
        _install(monkeypatch, stub)

        assert ws_module.ws_connect("wss://gw", cookies=COOKIES) is sentinel
        assert len(calls) == 1

    @pytest.mark.parametrize("exc", [
        ConnectionError("connection refused"),
        TimeoutError("handshake timed out"),
        RuntimeError("curl handle exploded"),
        OSError("network unreachable"),
    ])
    def test_real_failures_propagate(self, monkeypatch, exc):
        """A genuine failure must NEVER be retried with a second TLS
        identity — the exception escapes ws_connect untouched."""
        calls: list[dict] = []
        _install_legacy(monkeypatch, calls)
        stub = _StubSession(ws_connect=lambda *a, **k: (_ for _ in ()).throw(exc))
        _install(monkeypatch, stub)

        with pytest.raises(type(exc), match=str(exc)):
            ws_module.ws_connect("wss://gw", cookies=COOKIES)
        assert calls == []  # legacy never engaged

    def test_fallback_receives_the_same_arguments(self, monkeypatch):
        """url/cookies/headers/timeout pass through to the legacy path."""
        calls: list[dict] = []
        _install_legacy(monkeypatch, calls)
        _install(monkeypatch, _StubSession())

        ws_module.ws_connect("wss://gw/facebook.com/ws",
                             cookies=COOKIES, headers=HEADERS, timeout=7.5)
        assert calls == [{"url": "wss://gw/facebook.com/ws",
                          "cookies": COOKIES, "headers": HEADERS,
                          "timeout": 7.5}]

    def test_half_built_session_released_before_fallback(self, monkeypatch):
        """The failed primary session's curl handle (which holds the cookie
        jar) is closed before the fallback re-opens with the same jar."""
        calls: list[dict] = []
        _install_legacy(monkeypatch, calls)
        stub = _StubSession()
        _install(monkeypatch, stub)

        ws_module.ws_connect("wss://gw", cookies=COOKIES)
        assert stub.closed == 1
