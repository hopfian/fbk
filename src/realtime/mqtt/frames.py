"""MQTT frame codec — the Facebook variant.

Encodes and decodes the MQIsdp wire shapes byte-for-byte as captured from
the production client (docs/15 §P3-1, assets/messenger_ws_full.json); the
unit tests replay the captured frames through these codecs.

ARCHITECTURE:

  Dialect (docs/06 §2, docs/15 §P2-4 — live-confirmed 2026-09):
    Differences from the OASIS MQTT 3.1.1 spec:
      * protocol name is "MQIsdp", level 3 (not "MQTT"/4)
      * the CONNECT payload carries the identity blob as the MQTT *username*
        (connect flags 0x82 = username + clean session): a 2-byte length
        followed by plain-UTF-8 JSON (UA, app id, device uuid, session int,
        uid — decoded by ``decode_connect``/``realtime.mqtt.connect``)
      * vanilla MQTT 3.1.1 CONNECTs are rejected with a clean WS 1000 close

  Framing Rules (docs/06 §9.1):
    Byte 1 = (packet type << 4) | flags; remaining length is a base-128
    varint (7 payload bits + 0x80 continuation, max 4 bytes); strings are
    2-byte big-endian length + bytes. The one-packet-per-WS-message
    observation (docs/06 §1.2) makes ``parse_packet`` take the first
    packet of each frame, tolerating stream-style concatenation.

CALIBRATION NOTES (docs/15 §P3-1):
  The captured 452B CONNECT decodes as ``10 <varint> | 0006 "MQIsdp" |
  03 | 82 | 000f (keepalive 15s) | 000c "mqttwsclient" | 01 a5 <JSON
  identity>``; the captured CONNACK is ``20 02 00 00`` (rc=0) and the
  SUBACK ``90 03 00 01 00`` (granted).

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

import constants as C


class MQTTDecodeError(ValueError):
    """A malformed MQTT packet."""


# ------------------------------------------------------------------ remaining length
def encode_remaining_length(n: int) -> bytes:
    """MQTT varint remaining-length encoding (docs/06 §9.1).

    Args:
        n: The body length to encode.

    Returns:
        The base-128 varint: 7 payload bits per byte, high bit 0x80 set on
        every byte but the last, at most 4 bytes per the spec's limit.
    """
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n:
            out.append(b | 0x80)  # 0x80 = continuation flag: more bytes follow
        else:
            out.append(b)
            return bytes(out)


def remaining_length(packet: bytes, offset: int = 1) -> tuple[int, int]:
    """Decode a varint at `offset`; return (value, next_offset).

    Args:
        packet: The raw packet bytes.
        offset: Byte index the varint starts at (1 — after the opcode).

    Returns:
        The decoded length and the offset of the first body byte.

    Raises:
        MQTTDecodeError: If the packet ends mid-varint (truncated wire
            data) or the varint exceeds the spec's 4-byte maximum — both
            indicate a malformed or hostile frame, never a follow-up read.
    """
    value, multiplier = 0, 1
    while True:
        if offset >= len(packet):
            raise MQTTDecodeError("truncated remaining-length varint")
        b = packet[offset]
        offset += 1
        value += (b & 0x7F) * multiplier
        if not b & 0x80:
            return value, offset
        multiplier *= 128
        # 4-byte varint cap per the MQTT spec (docs/06 §9.1): a longer
        # varint is not a valid packet and must not be consumed optimistically
        if multiplier > 128 ** 4:
            raise MQTTDecodeError("remaining-length varint too long")


# ------------------------------------------------------------------------ packets
def build_connect(username: bytes, *, client_id: bytes | None = None,
                  keepalive: int = C.MQTT_KEEPALIVE_S) -> bytes:
    """Build the fb MQIsdp CONNECT: username is the JSON identity blob.

    Byte layout (docs/15 §P3-1, matches the captured 452B frame):
      ``10 <varint> | u16 protocol-name length | "MQIsdp" | level 3 |
      flags 0x82 | u16-BE keepalive | u16 client-id length + client_id |
      u16 username length + identity JSON``.

    Args:
        username: The encoded identity blob riding the MQTT username
            field (connect flags 0x82 = username + clean session).
        client_id: Client identifier; defaults to the captured
            ``mqttwsclient`` prefix (docs/15 §P2-4).
        keepalive: Keepalive seconds; defaults to the captured 15s
            (``constants.MQTT_KEEPALIVE_S``).

    Returns:
        The complete CONNECT packet bytes, ready for the socket.
    """
    client_id = client_id or C.MQTT_CLIENT_ID_PREFIX.encode()
    vh = (b"\x00" + bytes([len(C.MQTT_PROTOCOL_NAME)]) + C.MQTT_PROTOCOL_NAME.encode()
          + bytes([C.MQTT_PROTOCOL_LEVEL, C.MQTT_CONNECT_FLAGS])
          + struct.pack(">H", keepalive))
    payload = struct.pack(">H", len(client_id)) + client_id
    payload += struct.pack(">H", len(username)) + username
    body = vh + payload
    return bytes([C.MQTT_OP_CONNECT]) + encode_remaining_length(len(body)) + body


def decode_connect(packet: bytes) -> dict[str, Any]:
    """Annotate a fb CONNECT's fields (tests + protocol verification).

    Args:
        packet: Raw CONNECT packet bytes (e.g. from the live capture).

    Returns:
        A dict with keys ``remaining_length``, ``protocol``, ``level``,
        ``flags`` (hex string), ``keepalive``, ``client_id``, and
        ``username`` — the identity JSON string when the username flag is
        set, else None. Used by ``connect.rewrite_connect_identity`` to
        locate the identity blob for splicing.

    Raises:
        MQTTDecodeError: If the packet is empty, not a CONNECT opcode,
        or truncated mid-field.
    """
    if len(packet) < 2:
        raise MQTTDecodeError(f"CONNECT packet too short: {len(packet)}")
    if packet[0] != C.MQTT_OP_CONNECT:
        raise MQTTDecodeError("not a CONNECT")
    rl, off = remaining_length(packet, 1)
    plen = struct.unpack(">H", packet[off:off + 2])[0]
    proto = packet[off + 2: off + 2 + plen].decode()
    i = off + 2 + plen
    level, flags = packet[i], packet[i + 1]
    keepalive = struct.unpack(">H", packet[i + 2:i + 4])[0]
    j = i + 4
    cid_len = struct.unpack(">H", packet[j:j + 2])[0]
    client_id = packet[j + 2: j + 2 + cid_len].decode()
    j += 2 + cid_len
    username = None
    if flags & 0x80:  # 0x80 = username-present bit in the connect flags
        ulen = struct.unpack(">H", packet[j:j + 2])[0]
        username = packet[j + 2: j + 2 + ulen].decode()
    return {"remaining_length": rl, "protocol": proto, "level": level,
            "flags": f"0x{flags:02x}", "keepalive": keepalive,
            "client_id": client_id, "username": username}


def encode_subscribe(topic: str, packet_id: int = 1, qos: int = 0) -> bytes:
    """Encode a standalone SUBSCRIBE packet (docs/06 §2 type 8).

    Body layout: ``u16-BE packet_id | u16-BE topic length | topic | qos``.

    Args:
        topic: The topic string to subscribe ('/t_ms', '/t_rtc_multi', ...).
        packet_id: The packet identifier echoed in the SUBACK.
        qos: The requested QoS byte (0 in all captured traffic).

    Returns:
        The complete SUBSCRIBE packet bytes.
    """
    t = topic.encode()
    body = struct.pack(">H", packet_id) + struct.pack(">H", len(t)) + t + bytes([qos])
    return bytes([C.MQTT_OP_SUBSCRIBE]) + encode_remaining_length(len(body)) + body


def encode_publish(topic: str, payload: bytes, qos: int = 0) -> bytes:
    """PUBLISH with a short-payload assumption (varint handles longer bodies).

    Body layout: ``u16-BE topic length | topic | payload``; the QoS rides
    the opcode's low nibble (docs/06 §2 — QoS>0 packets add a 2-byte
    packet id, which the legacy send path never needs).

    Args:
        topic: The target topic string.
        payload: Raw payload bytes (thrift-compact on the /t_ms send leg).
        qos: QoS value placed in the low nibble of the opcode.

    Returns:
        The complete PUBLISH packet bytes.
    """
    t = topic.encode()
    body = struct.pack(">H", len(t)) + t + payload
    return bytes([C.MQTT_OP_PUBLISH | qos]) + encode_remaining_length(len(body)) + body


def encode_pingreq() -> bytes:
    """Encode the PINGREQ keepalive probe (docs/06 §2 type 12).

    The fixed two-byte header ``C0 00`` with a zero remaining length —
    confirmed live in the P2-4 capture (docs/15 §P2-4); the server answers
    PINGRESP (``D0 00``).

    Returns:
        The two PINGREQ bytes.
    """
    return bytes([C.MQTT_OP_PINGREQ, 0x00])


@dataclass
class Packet:
    """One parsed MQTT packet.

    The data contract: ``op`` is byte 0 including the flag nibble, ``body``
    is everything after the remaining-length varint, and ``raw`` is the
    exact captured bytes — parsers never re-serialize, they slice ``raw``.
    """

    op: int       # byte 0: packet type << 4 | flags (docs/06 §2)
    body: bytes   # post-varint payload (headers/topic/payload per type)
    raw: bytes    # the full original packet bytes (decode/verify source)

    @property
    def kind(self) -> str:
        """The packet's human name ('PUBLISH', 'PINGREQ', ...)."""
        exact = {C.MQTT_OP_CONNECT: "CONNECT", C.MQTT_OP_CONNACK: "CONNACK",
                 C.MQTT_OP_SUBSCRIBE: "SUBSCRIBE", C.MQTT_OP_SUBACK: "SUBACK",
                 C.MQTT_OP_PINGREQ: "PINGREQ", C.MQTT_OP_PINGRESP: "PINGRESP",
                 C.MQTT_OP_DISCONNECT: "DISCONNECT", C.MQTT_OP_PUBACK: "PUBACK"}
        if self.op in exact:
            return exact[self.op]
        # 0xF0 masks the flag nibble: any op 0x30-0x3F is a PUBLISH
        if self.op & 0xF6 in (0x30, 0x32, 0x34) or (self.op & 0xF0) == 0x30:
            return "PUBLISH"
        return f"0x{self.op:02x}"


def parse_packet(data: bytes) -> Packet:
    """Parse the first MQTT packet in `data` (WS frames carry one each).

    The live capture (docs/15 §P2-4) shows one MQTT packet per WS binary
    message, but the parser follows the spec's stream semantics anyway:
    it validates exactly the leading packet and ignores trailing bytes
    (docs/06 §1.2).

    Args:
        data: The raw WS message bytes.

    Returns:
        The parsed ``Packet``.

    Raises:
        MQTTDecodeError: On empty input, a truncated remaining-length
            varint, or a body shorter than the announced length.
    """
    if not data:
        raise MQTTDecodeError("empty packet")
    op = data[0]
    rl, off = remaining_length(data, 1)
    end = off + rl
    if end > len(data):
        raise MQTTDecodeError(f"truncated packet: need {end}, have {len(data)}")
    return Packet(op=op, body=data[off:end], raw=data[:end])


def decode_connack(packet: bytes) -> tuple[int, int]:
    """(session_present, return_code) — rc==0 means accepted.

    The captured handshake ack is ``20 02 00 00`` (docs/15 §P3-1): body
    byte 0 is the session-present flag, byte 1 the return code; any
    non-zero rc is fatal for the session (docs/06 §2).

    Args:
        packet: The CONNACK packet bytes.

    Returns:
        The (session_present, return_code) pair.

    Raises:
        MQTTDecodeError: If the packet is not a CONNACK or its body is
            shorter than the two mandatory bytes.
    """
    p = parse_packet(packet)
    if p.op != C.MQTT_OP_CONNACK:
        raise MQTTDecodeError("not a CONNACK")
    if len(p.body) < 2:
        raise MQTTDecodeError(f"CONNACK body too short: {len(p.body)}")
    return p.body[0], p.body[1]


def decode_suback(packet: bytes) -> tuple[int, list[int]]:
    """(packet_id, granted_qos list) — granted 0x80 means rejected.

    The captured SUBACK is ``90 03 00 01 00`` (docs/15 §P3-1): u16-BE
    packet id followed by one granted-QoS byte per subscribed topic;
    0x80 in that list is the spec's subscription-failure code.

    Args:
        packet: The SUBACK packet bytes.

    Returns:
        The echoed packet id and the granted-QoS byte list.

    Raises:
        MQTTDecodeError: If the packet is not a SUBACK or its body lacks
            even the packet-id bytes.
    """
    p = parse_packet(packet)
    if p.op != C.MQTT_OP_SUBACK:
        raise MQTTDecodeError("not a SUBACK")
    if len(p.body) < 2:
        raise MQTTDecodeError(f"SUBACK body too short: {len(p.body)}")
    pid = struct.unpack(">H", p.body[:2])[0]
    return pid, list(p.body[2:])
