"""MQTTClient — a Python peer on the Messenger MQTT bus (docs/06, docs/15 §P3-1).

Implements the Facebook-dialect MQTT 3 protocol over a fingerprint-coherent
WebSocket connection established via ``realtime.ws.coherent_ws_connect``.

PROTOCOL DIALECT (docs/06 §2, docs/15 §P2-4, live-confirmed 2026-09):

  Facebook does NOT speak standard MQTT v3.1.1:
    * ``ProtocolName`` = ``"MQIsdp"``   (not the spec's ``"MQTT"``)
    * ``ProtocolLevel`` = 3              (not 4)
    * The CONNECT ``username`` field carries a plain-UTF-8 JSON identity
      blob (UA, app id, device uuid, session int, uid — docs/15 §P3-1
      anatomy) rather than a plaintext credential.
    * The broker address is ``wss://edge-chat.facebook.com/chat``;
      ``edge.facebook.com`` is NXDOMAIN-dead (docs/15 §P2-4), and a
      vanilla MQTT 3.1.1 CONNECT (``"MQTT"``/level 4) is rejected with a
      clean WS 1000 close.

VERIFIED HANDSHAKE FLOW (docs/15 §P3-1):
  1. WS connect (chrome136 TLS + session cookies in handshake headers).
  2. Server-first ``0x0A`` byte received and discarded.
  3. Client sends MQIsdp CONNECT frame (identity payload as username).
  4. Server replies CONNACK ``rc=0`` — any non-zero return code is fatal.
  5. Client subscribes to ``/t_ms`` and ``/t_rtc_multi`` (SUBACK required;
     ``/t_rtc_multi`` is the only topic the production client was observed
     subscribing live — docs/15 §P3-1).
  6. PINGREQ/PINGRESP keepalive loop at 15-second CONNECT keepalive.
  7. PUBLISH deltas arrive on ``/t_ms`` with Thrift-compact binary bodies,
     decoded by ``realtime.mqtt.thrift.CompactReader``.

FINGERPRINT COHERENCE (docs/16 §P10-2):
  Prior to Phase 10, HTTP transport used chrome136 TLS while the MQTT WS
  used the Python ``websockets`` library's default ClientHello — a
  split-identity signal observable at the edge. This class now routes the
  WS handshake through ``curl_cffi`` to unify the fingerprint across all
  transport paths.

CALIBRATION NOTES (docs/15 §P7-1, §P5-2):
  The classic /t_ms bus is DEAD for chat delivery — message sync rides the
  DGW lightspeed state-sync channel; the captured production client only
  ever subscribes ``/t_rtc_multi`` (call signaling) on this socket. The
  client is retained for protocol completeness and /t_ms delta capture;
  no PUBLISH delta has yet been captured (docs/15 §P5-2 — the first real
  delta capture is an operator-gated event).

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import contextlib
import functools
import time
from collections.abc import Callable, Iterator, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..ws import WSConnection

import constants as C
from transport.profile import sanitize_user_agent

from ..ws import coherent_ws_connect
from . import frames
from .connect import build_fb_connect
from .thrift import CompactReader


@functools.lru_cache(maxsize=4)
def _capture_connect_bytes(capture: str) -> bytes:
    """Extract the captured CONNECT frame from one capture file, once per path.

    Cache design (startup/reconnect audit 2026-09): keyed by the RESOLVED
    capture path — the ``FBK_WS_CAPTURE`` override or the shipped default —
    so a changed env value resolves to a new key and is re-read, while the
    same path (the overwhelmingly common case) parses the 235KB JSON only
    once per process. ``maxsize=4`` bounds memory if an operator flips
    between a handful of captures; the LRU eviction re-reads on demand.
    """
    import base64
    import json as _json
    from pathlib import Path
    data = _json.loads(Path(capture).read_text(encoding="utf-8"))
    # the capture holds both sockets (edge-chat MQTT + DGW channels) —
    # pick the MQTT one; frame n>400 is the 452B CONNECT (docs/15 §P3-1)
    chat = next(s for s in data if s["url"].startswith("wss://edge-chat.facebook.com"))
    return base64.b64decode(next(f["b64"] for f in chat["frames"]
                                 if f["dir"] == "sent" and f["n"] > 400))


class MQTTClientError(RuntimeError):
    """The broker refused us at a protocol level."""


class Frame:
    """One received MQTT frame (PUBLISH topics are split out for you).

    For PUBLISH packets (opcode nibble 0x30), the body is demultiplexed:
    a 2-byte big-endian topic length, the UTF-8 topic, then the payload.
    All other packet kinds carry their raw post-varint body.
    """

    def __init__(self, packet: frames.Packet):
        """Wrap one parsed packet, splitting PUBLISH topic/payload apart.

        Args:
            packet: The decoded MQTT packet from ``frames.parse_packet``.
        """
        self.packet = packet
        self.topic: str | None = None
        self.payload: bytes = b""
        if packet.op & 0xF0 == C.MQTT_OP_PUBLISH:
            # FB-dialect PUBLISH body layout (docs/06 §1 packet table):
            # ONE EXTRA UNEXPLAINED BYTE between the remaining-length
            # varint and the topic string, then u16-BE topic length |
            # topic | payload. All known third-party parsers skip the
            # byte; without the skip, the topic/payload split lands one
            # byte early and every /t_ms delta mis-parses.
            tlen = int.from_bytes(packet.body[1:3], "big")
            self.topic = packet.body[3:3 + tlen].decode("utf-8", "replace")
            self.payload = packet.body[3 + tlen:]

    @property
    def kind(self) -> str:
        """The packet's name ('PUBLISH', 'CONNACK', ...)."""
        return self.packet.kind

    def thrift_summary(self, max_depth: int = 5) -> dict[str, Any]:
        """Best-effort thrift-compact decode of a /t_ms-style payload.

        Args:
            max_depth: Recursion bound for nested structs; deeper levels
                are elided to survive pathological payloads.

        Returns:
            A field-id-keyed dict from ``CompactReader.dump``; a
            ``{"decode_error": ...}`` dict when the payload is not
            thrift-compact — never raises, so stream consumers can log and
            continue.
        """
        try:
            return CompactReader(self.payload).dump(max_depth)
        except Exception as exc:
            return {"decode_error": f"{type(exc).__name__}: {exc}"}


class MQTTClient:
    """One MQTT session over one WebSocket. Context-manager capable.

    Owns the full MQIsdp lifecycle for one socket: CONNECT (fresh identity
    or verbatim capture replay), SUBSCRIBE with SUBACK enforcement,
    deadline-aware reads with keepalive pings, and idempotent teardown.
    The handshake failure discipline: any exception before CONNACK rc=0
    closes the socket before re-raising, so a half-completed handshake
    never leaves a live socket behind.
    """

    # PINGREQ cadence while connected: the CONNECT negotiates a 15s
    # keepalive window (docs/15 §P2-4); 10s keeps the client inside it
    # with margin and matches the web client's shorter-than-negotiated
    # practice (docs/06 §8).
    PING_CADENCE_S: float = 10.0

    def __init__(
        self,
        cookies: Mapping[str, str],
        *,
        url: str = C.MESSENGER_MQTT_WS,
        user_agent: str | None = None,
        device_id: str | None = None,
        session_id: int | None = None,
        connect_timeout: float = 15.0,
    ):
        """Configure the session without touching the network.

        Args:
            cookies: The facebook.com cookie jar; the full set ships in
                the WS handshake headers (docs/06 §1.2/§10 — c_user/xs is
                hard-validated at the upgrade).
            url: The broker URL; defaults to
                ``constants.MESSENGER_MQTT_WS`` (wss://edge-chat.facebook
                .com/chat — edge.facebook.com is dead, docs/15 §P2-4).
            user_agent: Optional UA override; sanitized via
                ``sanitize_user_agent`` before it ships (docs/15 §P9-2).
            device_id: Optional device uuid for the CONNECT identity blob;
                a fresh uuid4 is generated when None.
            session_id: Optional session int for the identity blob; a
                fresh random int is generated when None.
            connect_timeout: Per-exchange deadline in seconds for the
                handshake, subscribes, and reads.
        """
        self.cookies = dict(cookies)
        self.url = url
        self.user_agent = user_agent
        self.device_id = device_id
        self.session_id = session_id
        self.connect_timeout = connect_timeout
        self._ws: WSConnection | None = None  # set by connect()
        self._connected = False
        self._subscriptions: list[str] = []
        self._last_activity = 0.0
        # Last PINGREQ send time: the CONNECT negotiates a 15s keepalive
        # (docs/15 §P2-4) and MQTT semantics require the CLIENT to send
        # a control packet inside the window even while deltas stream in
        # — receiving traffic does not satisfy the keepalive (docs/06 §8).
        # 10s cadence keeps us inside the window with margin.
        self._last_ping = 0.0

    # ------------------------------------------------------------------ connect
    def connect(self, user_id: str, *, fresh_identity: bool = True) -> None:
        """Open the WS and complete the MQIsdp handshake (CONNACK required).

        Since Phase 10 the socket is fingerprint-coherent: the handshake
        rides curl_cffi with the same chrome TLS/h2 impersonation and the
        session cookies as the HTTP transport (docs/16 §P10-2) — the old
        `websockets`-library ClientHello was a split-identity signal.

        Args:
            user_id: The operator's numeric c_user id; rides the identity
                blob's ``u`` field (must match the cookie jar's c_user per
                docs/06 §10 or the connection is dead on arrival).
            fresh_identity: When True (default), build a fresh CONNECT
                identity blob; when False, replay the captured CONNECT
                bytes verbatim (the docs/15 §P3-1 path-A proof).

        Raises:
            MQTTClientError: If CONNACK returns a non-zero return code —
                the broker refused the session at a protocol level.
            TimeoutError: If the CONNACK does not arrive within
                ``connect_timeout`` — the socket is closed before the
                raise, so retrying calls connect() from a clean state.
        """
        if self._connected:
            return
        headers = {
            "Cookie": "; ".join(f"{k}={v}" for k, v in sorted(self.cookies.items())),
            "Origin": "https://www.facebook.com",
            "User-Agent": sanitize_user_agent(self.user_agent or (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")),
        }
        self._ws = coherent_ws_connect(self.url, cookies=dict(self.cookies),
                                      headers=headers,
                                      timeout=self.connect_timeout)
        assert self._ws is not None
        try:
            # server-first byte (observed 0x0A in the live capture,
            # docs/15 §P2-4) — consumed, non-fatal if absent
            with contextlib.suppress(TimeoutError):
                self._ws.recv(timeout=3.0)
            connect_packet = build_fb_connect(
                user_id, user_agent=self.user_agent, device_id=self.device_id,
                session_id=self.session_id) if fresh_identity else self._verbatim_connect()
            self._ws.send(connect_packet)
            reply = self._ws.recv(timeout=self.connect_timeout)
            assert isinstance(reply, bytes), f"expected binary frame, got {type(reply)!r}"
            session_present, rc = frames.decode_connack(reply)
        except Exception:
            # a half-completed handshake must not leave the socket open —
            # close() is idempotent, so re-raising after it is safe.
            self._safe_close()
            raise
        if rc != 0:
            self._safe_close()
            raise MQTTClientError(f"CONNACK rejected: rc={rc} (session_present={session_present})")
        self._connected = True
        self._last_activity = time.time()
        # Handshake counts as client-sent traffic: the ping cadence clock
        # starts here so the first PINGREQ lands ~PING_CADENCE_S after.
        self._last_ping = time.time()

    def _verbatim_connect(self) -> bytes:
        """CONNECT bytes replayed verbatim from the live capture (P3-1 path A).

        Resolves the capture file (``data/messenger_ws_full.json``,
        overridable via ``FBK_WS_CAPTURE``) and returns the first large
        client-sent frame from the edge-chat socket — the production
        client's own CONNECT, byte-for-byte. The P3-1 proof: this replay
        alone yields CONNACK rc=0 on a fresh socket.

        The 235KB capture is parsed ONCE per resolved path per process
        (``_capture_connect_bytes`` lru_cache, keyed by the path string):
        reconnect loops re-send cached bytes instead of re-parsing, while
        a changed ``FBK_WS_CAPTURE`` value still resolves to a fresh key
        and re-reads.

        Raises:
            FileNotFoundError: If the capture file is absent.
            StopIteration: If the capture holds no edge-chat CONNECT frame.
        """
        import os
        # Route through Config.discover() (wheel packaging, v3): a flat
        # wheel install puts data/ beside the modules in site-packages,
        # and the config layer knows both install shapes. FBK_WS_CAPTURE
        # still short-circuits the default resolution entirely.
        capture = os.environ.get("FBK_WS_CAPTURE")
        if not capture:
            from config import Config
            capture = str(Config.discover().data_dir / "messenger_ws_full.json")
        return _capture_connect_bytes(capture)

    # ------------------------------------------------------------- subscription
    def subscribe(self, topic: str, *, packet_id: int = 1) -> None:
        """SUBSCRIBE a topic; raises unless every requested QoS was granted.

        Args:
            topic: The topic string ('/t_ms', '/t_rtc_multi', ... — the
                docs/06 §4 map; the live client's own subscribe target is
                '/t_rtc_multi', docs/15 §P3-1).
            packet_id: The SUBSCRIBE packet identifier; echoed in SUBACK.

        Raises:
            MQTTClientError: If the SUBACK grants 0x80 (rejection) for the
                topic, or the client is not connected.
        """
        self._require_connected()
        assert self._ws is not None
        self._ws.send(frames.encode_subscribe(topic, packet_id))
        reply = self._ws.recv(timeout=self.connect_timeout)
        assert isinstance(reply, bytes), f"expected binary frame, got {type(reply)!r}"
        _pid, granted = frames.decode_suback(reply)
        # 0x80 in the granted-QoS list is the MQTT-spec subscription
        # failure code (docs/06 §2) — treat it as a hard error, not a
        # degraded-but-continue state.
        if granted and any(q == 0x80 for q in granted):
            raise MQTTClientError(f"subscription to {topic!r} rejected: {granted}")
        self._subscriptions.append(topic)

    @property
    def subscriptions(self) -> list[str]:
        """A copy of the topics subscribed on this session so far."""
        return list(self._subscriptions)

    # ------------------------------------------------------------------- reading
    def read(self, timeout: float = 10.0) -> Frame | None:
        """One frame or None on timeout (sends keepalive pings when idle).

        Args:
            timeout: Maximum seconds to wait for a frame.

        Returns:
            The next ``Frame``, or None if the deadline elapsed — an idle
            tick also fires a PINGREQ keepalive, keeping the 15s CONNECT
            keepalive window satisfied (docs/15 §P2-4).
        """
        self._require_connected()
        assert self._ws is not None
        try:
            data = self._ws.recv(timeout=timeout)
            assert isinstance(data, bytes), f"expected binary frame, got {type(data)!r}"
            self._last_activity = time.time()
            frame = Frame(frames.parse_packet(data))
            # Keepalive cadence runs on SEND time, not idle time: MQTT
            # requires the client to emit a control packet inside the
            # 15s window even during continuous inbound traffic.
            if time.time() - self._last_ping >= self.PING_CADENCE_S:
                self.ping()
            return frame
        except TimeoutError:
            self.ping()
            return None

    def stream(self, idle_timeout: float = 10.0,
               should_continue: Callable[[], bool] | None = None) -> Iterator[Frame]:
        """Infinite frame iterator with automatic keepalive; break to stop.

        Args:
            idle_timeout: Per-read deadline; every idle tick sends a
                PINGREQ keepalive.
            should_continue: Optional predicate polled between frames;
                returning False (or the caller breaking) ends the stream.

        Yields:
            Every ``Frame`` that arrives while the loop runs.
        """
        while should_continue is None or should_continue():
            frame = self.read(idle_timeout)
            if frame is not None:
                yield frame

    def publish(self, topic: str, payload: bytes) -> None:
        """PUBLISH one payload to a topic (the legacy /t_ms send path).

        Args:
            topic: The target topic string.
            payload: Raw payload bytes — thrift-compact for the classic
                send shape (docs/06 §6.1; the modern web send path is
                GraphQL, §6.2 — this is the legacy/mobile leg).
        """
        self._require_connected()
        assert self._ws is not None
        self._ws.send(frames.encode_publish(topic, payload))
        self._last_activity = time.time()

    def ping(self) -> None:
        """Send a PINGREQ keepalive (no-op when not connected).

        The broker drops sockets that idle without pings (docs/06 §8);
        idle read ticks and the 10s send-time cadence fire this
        automatically — the stamp below is what the cadence reads.
        """
        if self._connected and self._ws is not None:
            self._ws.send(frames.encode_pingreq())
            self._last_ping = time.time()

    # ------------------------------------------------------------------- closing
    def _require_connected(self) -> None:
        if not self._connected or self._ws is None:
            raise MQTTClientError("not connected — call connect() first")
        assert self._ws is not None

    def _safe_close(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                self._ws.close()
        self._ws = None
        self._connected = False

    def close(self) -> None:
        """Idempotent close of the WebSocket (safe without connect())."""
        self._safe_close()

    def __enter__(self) -> MQTTClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
