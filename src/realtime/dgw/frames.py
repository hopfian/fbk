"""DGW frame codec.

Live-confirmed format (docs/15 §P3-6):
    byte 0    opcode (0x0F control / 0x0D data / 0x0C ack8 / 0x0E ack3 /
                       0x09 ping / 0x0A pong)
    bytes 1-2 sequence number (little-endian)
    bytes 3-4 payload length (little-endian)
    byte 5    flags
    payload   JSON for 0x0F/0x0D; empty for acks/pings

Every encoder here round-trips the exact bytes captured from the production
client (tests/test_dgw_frames.py replays them from assets/messenger_ws_full.json).

ARCHITECTURE:

  One dataclass, two directions:
    ``encode`` serializes and ``decode`` parses the fixed 6-byte header
    plus payload; all opcode knowledge lives in ``constants`` (DGW_OP_*),
    this module only moves bytes. Bounds are validated on decode: short
    headers and payload-length mismatches raise the typed ValueError so a
    hostile frame never decodes optimistically.

  Acks and pings carry empty payloads in the fixed-header layout — the
  3-byte ACK3 exception (op + seq, no header) is handled by the frame
  *splitter* in dgw/requests.py, not here, because ``decode`` only ever
   sees full-header frames on the wire.

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

import constants as C


class DGWDecodeError(ValueError):
    """A frame payload violates the documented DGW JSON contract.

    Control/data frames carry JSON OBJECTS on the wire (docs/15 §P3-6);
    a valid-JSON non-object payload (a list, a bare int, ...) is a
    hostile/off-spec frame, never data to interpret optimistically.
    Frames-level twin of realtime.mqtt.frames.MQTTDecodeError — DGWError
    (dgw/client.py) describes the gateway REJECTING an exchange, while
    this error describes bytes that are not a spec-conformant frame.
    """


@dataclass
class DGWFrame:
    """One decoded/encodable DGW frame.

    The data contract mirrors the wire header exactly (docs/15 §P3-6):
    ``encode`` always emits ``op | seq(2B LE) | len(2B LE) | flags | payload``
    and ``decode`` enforces the inverse, so frame bytes round-trip
    byte-identically with the captured production traffic.
    """

    op: int            # opcode byte 0: DGW_OP_* (control/data/ack/ping)
    seq: int           # bytes 1-2: little-endian per-socket sequence
    flags: int         # byte 5: 0 in all captured traffic
    payload: bytes = b""  # JSON for 0x0F/0x0D; empty for acks/pings

    # ------------------------------------------------------------------ encode
    def encode(self) -> bytes:
        """Serialize to the fixed-header wire layout.

        Returns:
            The frame bytes: ``op | seq(2B LE) | len(2B LE) | flags | payload``.
        """
        return (bytes([self.op])
                + struct.pack("<H", self.seq)
                + struct.pack("<H", len(self.payload))
                + bytes([self.flags])
                + self.payload)

    # ------------------------------------------------------------------ decode
    @classmethod
    def decode(cls, data: bytes) -> DGWFrame:
        """Parse one full-header DGW frame.

        Args:
            data: The raw frame bytes (one packet, already split out of
                any concatenated WS message by dgw/requests.py).

        Returns:
            The decoded frame.

        Raises:
            ValueError: If the header is shorter than 6 bytes or the
                payload runs past the end of the buffer — both indicate
                a truncated or non-DGW message, never a follow-up read.
        """
        if len(data) < 6:
            raise ValueError(f"DGW frame too short: {len(data)} bytes")
        op = data[0]
        seq = struct.unpack_from("<H", data, 1)[0]
        length = struct.unpack_from("<H", data, 3)[0]
        flags = data[5]
        payload = data[6:6 + length]
        if len(payload) != length:
            raise ValueError("DGW frame truncated payload")
        return cls(op=op, seq=seq, flags=flags, payload=payload)

    # --------------------------------------------------------------- conveniences
    @property
    def kind(self) -> str:
        """The frame's human name ('CONTROL', 'DATA', 'ACK8', ...)."""
        return {C.DGW_OP_CONTROL: "CONTROL", C.DGW_OP_DATA: "DATA",
                C.DGW_OP_ACK8: "ACK8", C.DGW_OP_ACK3: "ACK3",
                C.DGW_OP_PING: "PING", C.DGW_OP_PONG: "PONG"}.get(self.op, f"0x{self.op:02x}")

    def json(self) -> dict[str, Any] | None:
        """Decode JSON payloads (control/data frames); None for binary/opaque.

        Returns:
            The parsed JSON dict, or None when the payload is empty or
            not valid UTF-8 JSON (binary/opaque payloads are a normal
            case — the caller distinguishes, the codec does not guess).

        Raises:
            DGWDecodeError: If the payload IS valid JSON but not an
                object (docs/15 §P3-6: control/data frames carry JSON
                objects) — a hostile/off-spec frame fails loudly typed
                instead of leaking a mis-typed value through the
                declared dict return.
        """
        if not self.payload:
            return None
        try:
            import json
            obj = json.loads(self.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(obj, dict):
            raise DGWDecodeError(
                f"DGW {self.kind} frame JSON payload is "
                f"{type(obj).__name__}, not an object (docs/15 §P3-6)")
        return obj

    @classmethod
    def control(cls, seq: int, obj: object) -> DGWFrame:
        """A 0x0F control frame carrying JSON (the handshake body is '{}').

        Args:
            seq: The frame's sequence number (the connect() handshake
                body is ``{}`` — docs/15 §P3-6).
            obj: The JSON-encodable control body.

        Returns:
            The encoded control frame.
        """
        import json
        return cls(op=C.DGW_OP_CONTROL, seq=seq, flags=0,
                   payload=json.dumps(obj, separators=(",", ":")).encode())

    @classmethod
    def ping(cls, seq: int = 0) -> DGWFrame:
        """A 0x09 keepalive ping frame with an empty payload.

        Args:
            seq: The frame's sequence number (0 in captured ping traffic).

        Returns:
            The encoded ping frame.
        """
        return cls(op=C.DGW_OP_PING, seq=seq, flags=0)

    def ack(self) -> DGWFrame:
        """The matching ACK8 for a received data frame.

        The 0x0C ACK8 echoes the received frame's sequence number and
        carries the constant 2-byte ``\\x00\\x00`` payload in all
        captured traffic (docs/15 §P6-2).

        Returns:
            The ACK8 frame to send back.
        """
        return DGWFrame(op=C.DGW_OP_ACK8, seq=self.seq, flags=0)
