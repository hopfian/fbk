"""Fingerprint-coherent WebSocket transport (docs/16 §2, §P10-2).

The single WebSocket factory every realtime transport (MQTT, all four DGW
channels) connects through, so the account presents one TLS identity on
every wire contact.

ARCHITECTURE:

  Phase-10 Unification (docs/16 §2, live-calibrated):
    Until Phase 10, the HTTP transport spoke with a chrome136 TLS/h2
    fingerprint (curl_cffi impersonation) while the MQTT and DGW realtime
    connections spoke with the *websockets* Python library's default
    ClientHello — a split-identity signal on the same account (the https
    edge sees chrome136, edge-chat/gateway see Python-ssl). curl_cffi
    >= 0.15 ships a WebSocket client whose handshake inherits the
    session's impersonation and cookies, which unifies the fingerprint
    across ALL transports: one account, one fingerprint.

  Blocking-recv Bridge:
    curl_cffi's SYNC WebSocket recv() blocks indefinitely (the timeout
    parameter is async-only), so ``CurlCffiWSAdapter`` runs a daemon
    reader thread feeding a queue; our recv(timeout) pops from the queue
    with a deadline (§5.5 fallback annotation below). The underlying
    connection is never leaked: the reader thread exits when the ws
    closes, and close() joins it before releasing the session.

  Legacy Fallback (retained by design):
    When curl_cffi's WS support is unavailable in the installed build,
    ``_legacy_ws_connect`` reproduces the pre-Phase-10 ``websockets``
    library path: same send/recv/close shape and native timeouts, weaker
    fingerprint. docs/16 §2 documents this as the deliberate degradation
    order, not an oversight.

NETWORK BOUNDAY:
  Outbound only: wss://edge-chat.facebook.com/chat (MQTT — docs/15 §P2-4)
  and wss://gateway.facebook.com/ws/<channel> (DGW — docs/15 §P3-6).

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import contextlib
import queue
import threading
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    # curl_cffi is only needed once a socket is actually opened; the
    # runtime imports live in ws_connect (startup audit 2026-09). The
    # WS exception/flag re-exports below resolve lazily via __getattr__.
    from curl_cffi import CurlWsFlag, WebSocketTimeout
    from curl_cffi import requests as creq

_LAZY_CURL_EXPORTS = ("CurlWsFlag", "WebSocketTimeout")


def __getattr__(name: str) -> Any:
    """Lazily re-export the curl_cffi WS types named in ``__all__`` (PEP 562).

    Importing curl_cffi at module level would tax every import of this
    module (directly or via realtime/__init__) with ~200ms of library
    load; the names stay importable from here, just not eagerly.
    """
    if name == "CurlWsFlag":
        from curl_cffi import CurlWsFlag
        return CurlWsFlag
    if name == "WebSocketTimeout":
        from curl_cffi import WebSocketTimeout
        return WebSocketTimeout
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class WSConnection(Protocol):
    """The minimal wire surface MQTT/DGW clients depend on.

    Structural typing: any object with these three methods is an acceptable
    transport, which is what lets the curl_cffi adapter and the legacy
    websockets adapter be swapped without the protocol clients knowing
    which one is live.
    """

    def send(self, data: bytes) -> None: ...
    def recv(self, timeout: float) -> bytes: ...
    def close(self) -> None: ...


class CurlCffiWSAdapter:
    """Adapts a curl_cffi sync WebSocket to the WSConnection protocol.

    The underlying session holds the cookie jar and the impersonation
    profile; both ride the WS handshake automatically. A daemon thread
    bridges curl's blocking recv() into our timeout-aware interface.
    """

    def __init__(self, session: creq.Session[creq.Response], ws: Any, impersonate: str):
        """Bind the adapter to a live curl_cffi WS handle.

        Args:
            session: The curl_cffi session that owns the WS — it carries
                the impersonation profile and cookie jar that rode the
                handshake; it is held for lifetime management.
            ws: The live WebSocket handle from ``session.ws_connect``.
            impersonate: The impersonation target the handshake used
                (retained for diagnostics/repr only).
        """
        self._session = session  # keep alive: closing it closes the ws
        self._ws = ws
        self._impersonate = impersonate
        self._queue: queue.Queue[bytes] = queue.Queue()
        self._error: BaseException | None = None
        self._closed = threading.Event()
        # Daemon thread: exits automatically when the WS closes without
        # requiring explicit join() — prevents a blocking hang on process
        # shutdown (docs/16 §2 adapter design).
        self._reader = threading.Thread(
            target=self._read_loop, name="ws-reader", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        """Bridge curl's blocking recv() into the queue until close."""
        while not self._closed.is_set():
            try:
                payload, _flags = self._ws.recv()
            except BaseException as exc:
                self._error = exc
                return
            if isinstance(payload, str):
                payload = payload.encode()
            if isinstance(payload, memoryview):
                payload = payload.tobytes()
            self._queue.put(bytes(payload))

    def send(self, data: bytes) -> None:
        """Send one binary frame.

        Args:
            data: The raw MQTT/DGW packet bytes.

        Raises:
            Exception: Whatever curl_cffi raises on a broken or closed
                socket — the caller's handshake/read loops convert this
                into a typed transport error.
        """
        self._ws.send_bytes(data)

    def recv(self, timeout: float) -> bytes:
        """Pop one frame from the reader queue with a deadline.

        Args:
            timeout: Maximum seconds to wait for a frame.

        Returns:
            The next complete binary frame from the socket.

        Raises:
            TimeoutError: No frame arrived within the deadline (the
                keepalive loops treat this as an idle tick, not an error).
            ConnectionError: The reader thread has died — the socket
                closed or the underlying recv() failed — and the queue is
                drained.
        """
        # curl_cffi sync WS recv() blocks indefinitely (timeout param is
        # async-only); the queue bridges blocking recv() into deadline-
        # aware pop() calls (docs/16 §2 adapter design).
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty as exc:
            if self._error is not None and self._queue.empty():
                raise ConnectionError(f"ws reader terminated: {self._error}") \
                    from self._error
            raise TimeoutError("ws recv timed out") from exc

    def close(self) -> None:
        """Idempotent close: stop the reader, join it, then release the
        underlying ws + session (closing the session also closes the ws)."""
        if self._closed.is_set():
            return
        self._closed.set()
        with contextlib.suppress(Exception):
            self._ws.close()
        # closing the ws unblocks the reader's recv(); join (bounded, so a
        # stuck curl recv can never hang close() itself) before dropping
        # the session so the thread never touches a freed handle.
        self._reader.join(timeout=5.0)
        with contextlib.suppress(Exception):
            self._session.close()

    def __enter__(self) -> CurlCffiWSAdapter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def ws_connect(url: str, *, cookies: dict[str, str],
               impersonate: str = "chrome136",
               headers: dict[str, str] | None = None,
               timeout: float = 15.0) -> WSConnection:
    """Open a fingerprint-coherent WebSocket.

    Primary path: curl_cffi (chrome TLS + session cookies in the
    handshake). Fallback: the legacy `websockets` library (used when
    curl_cffi's WS support is missing) - same shape, weaker fingerprint.

    Args:
        url: The wss:// endpoint (MQTT broker or DGW channel URL).
        cookies: The facebook.com cookie jar, injected into the curl_cffi
            session so the WS upgrade carries the authenticated jar.
        impersonate: The curl_cffi impersonation target — chrome136 by
            default, matching the HTTP transport (docs/16 §2).
        headers: Handshake headers layered over the jar (Cookie/Origin/
            User-Agent per docs/06 §1.2 — belt and braces with the session
            jar, as the real client ships the full set).
        timeout: Handshake/open timeout in seconds.

    Returns:
        A live ``WSConnection`` — the curl_cffi adapter on the primary
        path, the legacy adapter on fallback.
    """
    from curl_cffi import requests as creq  # lazy: paid when a socket opens
    try:
        # typed-by-contract (§5.5): Session is generic over the response
        # class since curl_cffi 0.16 shipped py.typed; the argument is
        # pinned because the strict gate demands it (mypy runs at
        # python_version 3.11, below the TypeVar-default syntax upstream).
        session: creq.Session[creq.Response] = creq.Session()
        try:
            session.cookies.update(cookies)
            ws = session.ws_connect(
                url,
                impersonate=impersonate,
                headers=headers or {},
                timeout=timeout,
            )
        except Exception:
            # curl_cffi WS unavailable or failed before the handshake -
            # release the half-built session (its curl handle holds the
            # cookie jar), then re-raise for the fallback gate below.
            with contextlib.suppress(Exception):
                session.close()
            raise
        return CurlCffiWSAdapter(session, ws, impersonate)
    except (AttributeError, NotImplementedError):
        # Fallback is reserved for ONE condition: this curl_cffi build
        # has no sync WS API at all. A genuine handshake/network failure
        # must NOT fall back — retrying the same endpoint with the legacy
        # library's Python-ssl ClientHello presents two different TLS
        # identities to the edge in quick succession, exactly the
        # split-identity signal Phase 10 eliminated (docs/16 §P10-2).
        return _legacy_ws_connect(url, cookies=cookies,
                                  headers=headers, timeout=timeout)


def _legacy_ws_connect(url: str, *, cookies: dict[str, str],
                       headers: dict[str, str] | None,
                       timeout: float) -> WSConnection:
    """The pre-Phase-10 transport (websockets lib) as a fallback.

    Retained by design (docs/16 §2): for curl_cffi builds without WS
    support, this path keeps the realtime clients functional with the same
    send/recv/close shape and native timeouts — at the cost of the
    Python-ssl ClientHello that Phase 10 eliminated. The fixed header set
    below must therefore ship the full browser shape explicitly, since
    the websockets library's default handshake carries no browser realism
    of its own.

    Args:
        url: The wss:// endpoint.
        cookies: Accepted for signature parity with the primary path; the
            legacy library relies on the Cookie header inside ``headers``
            for authentication.
        headers: Handshake headers (Cookie/Origin/User-Agent).
        timeout: Handshake open timeout in seconds.

    Returns:
        A ``LegacyAdapter`` satisfying ``WSConnection``.
    """
    from websockets.sync.client import connect as ws_connect_lib

    class LegacyAdapter:
        """websockets-lib connection with the same close/timeout shape."""

        def __init__(self) -> None:
            # typed Any: close() nulls this attribute out (idempotent close)
            self._ws: Any = ws_connect_lib(url, additional_headers=headers or {},
                                           open_timeout=timeout)

        def __enter__(self) -> LegacyAdapter:
            return self

        def __exit__(self, *exc: object) -> None:
            self.close()

        def send(self, data: bytes) -> None:
            assert self._ws is not None
            self._ws.send(data)

        def recv(self, timeout: float) -> bytes:
            assert self._ws is not None
            data = self._ws.recv(timeout=timeout)
            assert isinstance(data, bytes), \
                f"expected binary frame, got {type(data)!r}"
            return data

        def close(self) -> None:
            """Idempotent close of the underlying connection."""
            ws, self._ws = self._ws, None
            if ws is not None:
                ws.close()

    return LegacyAdapter()


# alias: the clients import this name (clearer at call sites than ws_connect)
coherent_ws_connect = ws_connect

__all__ = [
    "CurlCffiWSAdapter",
    "CurlWsFlag",
    "WSConnection",
    "WebSocketTimeout",
    "coherent_ws_connect",
    "ws_connect",
]
