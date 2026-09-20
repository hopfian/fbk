"""DGWClient — a Python peer on gateway.facebook.com (docs/15 §P3-6).

The DGW (data-gateway) WebSocket family: bidirectional GraphQL-over-WS
channels (rpsignaling, realtime, lightspeed, streamcontroller — docs/15
§P2-4) observed carrying live GraphQL mutation calls and Pigeon
telemetry.

ARCHITECTURE:

  Live-verified flow (docs/15 §P3-6):
    WS connect with x-dgw-* query params + cookies → server-first 0x0A
    byte → 0x0F handshake ({} or channel JSON) → {"code":200} control
    ack → 0x0D data / 0x0C ACK8 exchanges → ping/pong. All four channels
    replayed verbatim on fresh sockets, cookies as the only credential
    material, each accepted.

  Frame Format (docs/15 §P3-6):
    ``opcode | seq(2B LE) | len(2B LE) | flags(1B) | payload`` — encoded
    and decoded by ``realtime.dgw.frames.DGWFrame``; the 0x0F control
    frames carry JSON (handshakes), 0x0D carry data, 0x0C/0x0E are acks,
    0x09/0x0A the 1-byte pings.

  Handshake Failure Discipline:
    The socket is marked live before the handshake exchange so the
    connected-gate passes, and any rejection path routes through
    ``_safe_close()`` — a refused handshake never leaks a live socket.

CALIBRATION NOTES (docs/16 §2, §P10-2):
  Since Phase 10 the handshake rides curl_cffi WS with the same chrome TLS
  impersonation and session cookies as the HTTP transport, closing the
  WS split-identity gap (the Python-ssl ClientHello of the legacy
  websockets library).

NETWORK BOUNDARY:
  Outbound only: wss://gateway.facebook.com/ws/<channel> (docs/15 §P2-4).

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..ws import WSConnection
import uuid
from collections.abc import Callable, Iterator, Mapping

import constants as C
from transport.profile import sanitize_user_agent

from ..ws import coherent_ws_connect
from .frames import DGWFrame


class DGWError(RuntimeError):
    """The gateway rejected the handshake or a frame exchange."""


class DGWClient:
    """One DGW socket on one channel.

    Owns the channel's full lifecycle: URL assembly with the x-dgw-*
    parameter set, the {'code':200} handshake, sequenced frame
    exchanges, and idempotent teardown. ``LightspeedClient`` (dgw/
    requests.py) subclasses this to add the lightspeed request/response
    dialect; the base class stays channel-agnostic.
    """

    def __init__(
        self,
        cookies: Mapping[str, str],
        channel: str = "rpsignaling",
        *,
        user_agent: str | None = None,
        logging_id: str | None = None,
        open_timeout: float = 15.0,
    ):
        """Configure one channel session without touching the network.

        Args:
            cookies: The facebook.com cookie jar; the full set ships in
                the WS handshake headers (the P3-6 replays used cookies
                as the only credential material).
            channel: The gateway channel name; must be a key of
                ``constants.MESSENGER_DGW_WS`` ('rpsignaling', 'realtime',
                'lightspeed', 'streamcontroller' — docs/15 §P2-4).
            user_agent: Optional UA override, sanitized before it ships
                (docs/15 §P9-2); defaults to the chrome136 desktop shape.
            logging_id: The x-dgw-loggingid uuid; fresh uuid4 when None.
            open_timeout: Per-exchange deadline in seconds for the
                handshake and reads.

        Raises:
            DGWError: If ``channel`` is not a known gateway channel.
        """
        if channel not in C.MESSENGER_DGW_WS:
            raise DGWError(f"unknown channel {channel!r}")
        self.cookies = dict(cookies)
        self.channel = channel
        self.user_agent = user_agent
        self.logging_id = logging_id or str(uuid.uuid4())
        self.open_timeout = open_timeout
        self._ws: WSConnection | None = None  # set by connect()
        self._connected = False
        self._seq = 0

    # ------------------------------------------------------------------- URL build
    def url(self, user_id: str, device_id: str | None = None) -> str:
        """The channel URL with the full x-dgw-* parameter set (P3-6).

        Args:
            user_id: The operator's numeric c_user id — the
                ``x-dgw-uuid`` parameter (docs/15 §P2-4).
            device_id: Optional device uuid; a fresh per-socket uuid4 is
                generated when None.

        Returns:
            The full ``wss://gateway.facebook.com/ws/<channel>?<params>``
            URL with the query set replayed verbatim from the live capture
            (x-dgw-appid/version/tier/uuid/authtype/deviceid, plus
            x-dgw-loggingid on rpsignaling/lightspeed).
        """
        from urllib.parse import urlencode
        params = dict(C.DGW_QUERY_PARAMS)
        params["x-dgw-uuid"] = user_id
        params["x-dgw-deviceid"] = device_id or str(uuid.uuid4())
        # rpsignaling + lightspeed are the channels observed carrying
        # x-dgw-loggingid in the live capture (docs/15 §P2-4)
        if self.channel in ("rpsignaling", "lightspeed"):
            params["x-dgw-loggingid"] = self.logging_id
        return f"{C.MESSENGER_DGW_WS[self.channel]}?{urlencode(params)}"

    # -------------------------------------------------------------------- connect
    def connect(self, user_id: str, *, device_id: str | None = None) -> None:
        """Open the socket and complete the {'code':200} handshake.

        Phase 10: the handshake rides curl_cffi WS (chrome TLS + session
        cookies — docs/16 §P10-2), unifying the fingerprint with HTTP.

        Args:
            user_id: The operator's numeric c_user id (x-dgw-uuid).
            device_id: Optional device uuid; fresh per-socket uuid4 when
                None (each socket carries its own id, docs/15 §P2-4).

        Raises:
            DGWError: If the handshake reply is missing or its JSON body
                is not ``{"code": 200}``.
            TimeoutError: If the handshake reply never arrives within
                ``open_timeout`` — the socket is closed before the raise.
        """
        if self._connected:
            return
        self._ws = coherent_ws_connect(
            self.url(user_id, device_id),
            cookies=dict(self.cookies),
            headers={
                "Cookie": "; ".join(f"{k}={v}" for k, v in
                                    sorted(self.cookies.items())),
                "Origin": "https://www.facebook.com",
                "User-Agent": sanitize_user_agent(
                    self.user_agent or
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/136.0.0.0 Safari/537.36"),
            },
            timeout=self.open_timeout)
        assert self._ws is not None  # narrowed for the handshake exchange below
        # the socket IS connected now — mark it live so handshake()'s
        # read() passes the connected gate (a rejected handshake closes +
        # resets the flag below, so no stale state can leak out)
        self._connected = True
        try:
            # server-first byte 0x0A — consumed, non-fatal if it never arrives
            with contextlib.suppress(TimeoutError):
                self._ws.recv(timeout=3.0)
            reply = self.handshake({})
        except Exception:
            # never leak a live socket out of a half-completed handshake
            self._safe_close()
            raise
        body = reply.json() if reply else None
        if not body or body.get("code") != 200:
            self._safe_close()
            raise DGWError(f"handshake rejected: {body!r}")
        self._connected = True

    def handshake(self, payload: dict[str, Any]) -> DGWFrame | None:
        """Send a 0x0F control frame and read the control reply.

        Args:
            payload: The JSON control body; the connect() handshake uses
                ``{}`` (the captured shape — docs/15 §P3-6).

        Returns:
            The reply frame, or None if the read timed out (a keepalive
            ping fired in its place).

        Raises:
            DGWError: If not connected.
        """
        self._require_connected()
        assert self._ws is not None
        frame = DGWFrame.control(self._next_seq(), payload)
        self._ws.send(frame.encode())
        return self.read(timeout=self.open_timeout)

    # ---------------------------------------------------------------------- io
    def _next_seq(self) -> int:
        # Sequence numbers ride a 2-byte little-endian field (P3-6 frame
        # layout) — wrap at 0x10000 to stay encodable.
        self._seq = (self._seq + 1) % 0x10000
        return self._seq

    def send_data(self, payload: dict[str, Any] | list[Any] | bytes, *, flags: int = 0) -> DGWFrame:
        """Send one 0x0D data frame (JSON or pre-encoded bytes).

        Args:
            payload: A dict/list JSON-encodes with compact separators, or
                raw pre-encoded bytes passed through verbatim (the
                lightspeed dialect pre-builds its wire payload).
            flags: The frame's flags byte; 0 in all captured traffic.

        Returns:
            The sent ``DGWFrame`` (for seq/ack bookkeeping by callers).

        Raises:
            DGWError: If not connected.
        """
        self._require_connected()
        assert self._ws is not None
        if isinstance(payload, (dict, list)):
            import json
            payload = json.dumps(payload, separators=(",", ":")).encode()
        frame = DGWFrame(op=C.DGW_OP_DATA, seq=self._next_seq(), flags=flags,
                         payload=payload)
        self._ws.send(frame.encode())
        return frame

    def read(self, timeout: float = 10.0) -> DGWFrame | None:
        """One frame, or None on timeout (with a keepalive ping sent).

        Args:
            timeout: Maximum seconds to wait for a frame.

        Returns:
            The next decoded ``DGWFrame``, or None if the deadline
            elapsed — the idle tick fires a 0x09 ping keepalive.

        Raises:
            ValueError: If the received bytes are not a decodable DGW
                frame (``DGWFrame.decode`` bounds check).
            DGWError: If not connected.
        """
        self._require_connected()
        assert self._ws is not None
        try:
            data = self._ws.recv(timeout=timeout)
            assert isinstance(data, bytes), f"expected binary frame, got {type(data)!r}"
            return DGWFrame.decode(data)
        except TimeoutError:
            self.ping()
            return None

    def stream(self, idle_timeout: float = 10.0, *,
               should_continue: Callable[[], bool] | None = None) -> Iterator[DGWFrame]:
        """Infinite frame iterator with automatic keepalive; break to stop.

        Args:
            idle_timeout: Per-read deadline; every idle tick sends a
                keepalive ping.
            should_continue: Optional predicate polled between frames.

        Yields:
            Every ``DGWFrame`` that arrives while the loop runs.
        """
        while should_continue is None or should_continue():
            frame = self.read(idle_timeout)
            if frame is not None:
                yield frame

    def ping(self) -> None:
        """Send a DGW ping frame (no-op when not connected).

        The 0x09/0x0A ping/pong pair are 1-byte-payload frames in the
        captured traffic (docs/15 §P2-4); idle read ticks fire this
        automatically.
        """
        if self._connected and self._ws is not None:
            self._ws.send(DGWFrame.ping(self._seq).encode())

    # -------------------------------------------------------------------- closing
    def _require_connected(self) -> None:
        """Gate every wire-touching method on a live socket."""
        if not self._connected or self._ws is None:
            raise DGWError("not connected — call connect() first")

    def _safe_close(self) -> None:
        """Close and reset unconditionally; suppresses wire errors so a
        failed handshake or teardown can never mask the original raise."""
        if self._ws is not None:
            with contextlib.suppress(Exception):
                self._ws.close()
        self._ws = None
        self._connected = False

    def close(self) -> None:
        """Idempotent close of the WebSocket (safe without connect())."""
        self._safe_close()

    def __enter__(self) -> DGWClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
