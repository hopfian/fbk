"""Frame PUBLISH demux: the FB-dialect skip byte (just-landed fix).

The Facebook MQTT PUBLISH body carries ONE extra unexplained byte between
the remaining-length varint and the u16-BE topic length. Parsers that
skip it split topic/payload correctly; parsers that don't land one byte
early and garble every /t_ms delta.

Complements test_mqtt_frames.py (codec shapes; its PUBLISH round trip
exercises the outbound encoder, not the inbound dialect split). Unit
(offline): hand-built dialect packets verified through Frame; no
network, no capture dependency.
"""
from __future__ import annotations

import struct

import pytest

from realtime.mqtt import frames
from realtime.mqtt.client import Frame


def build_dialect_publish(topic: str, payload: bytes, *,
                          extra: bytes = b"\x00", op: int = 0x30) -> bytes:
    """Hand-build one FB-dialect PUBLISH: op byte, varint remaining length,
    ONE extra byte, then u16-BE topic length | topic | payload."""
    t = topic.encode()
    body = extra + struct.pack(">H", len(t)) + t + payload
    return bytes([op]) + frames.encode_remaining_length(len(body)) + body


class TestPublishSkipByte:
    """Pins the inbound dialect split: the extra byte is skipped, so the
    topic/payload boundary lands exactly where the wire says it does."""

    def test_splits_topic_and_payload(self):
        packet = build_dialect_publish("/t_ms", b"\x01\x02\x03delta")
        frame = Frame(frames.parse_packet(packet))
        assert frame.kind == "PUBLISH"
        assert frame.topic == "/t_ms"
        assert frame.payload == b"\x01\x02\x03delta"

    @pytest.mark.parametrize("extra", [b"\x00", b"\x42", b"\xff"])
    def test_extra_byte_value_is_ignored(self, extra):
        """The byte is arbitrary on the wire — skipped, never interpreted."""
        packet = build_dialect_publish("/t_rtc_multi", b"payload", extra=extra)
        frame = Frame(frames.parse_packet(packet))
        assert frame.topic == "/t_rtc_multi"
        assert frame.payload == b"payload"

    def test_empty_payload_splits_cleanly(self):
        packet = build_dialect_publish("/t_ms", b"")
        frame = Frame(frames.parse_packet(packet))
        assert frame.topic == "/t_ms"
        assert frame.payload == b""

    def test_topic_like_bytes_in_payload_never_leak(self):
        """A payload starting with its own length-looking bytes must stay
        payload — the boundary is the topic length, nothing else."""
        payload = struct.pack(">H", 4) + b"/t_x" + b"rest"
        packet = build_dialect_publish("/t_ms", payload)
        frame = Frame(frames.parse_packet(packet))
        assert frame.topic == "/t_ms"
        assert frame.payload == payload

    def test_qos_flag_nibble_still_demuxes(self):
        """Any op 0x30-0x3F is a PUBLISH; the split applies to each."""
        packet = build_dialect_publish("/t_ms", b"p", op=0x32)
        frame = Frame(frames.parse_packet(packet))
        assert frame.kind == "PUBLISH"
        assert frame.topic == "/t_ms"
        assert frame.payload == b"p"

    def test_multibyte_varint_body_splits_the_same(self):
        """A >127-byte body exercises the 2-byte varint; the split rides
        the already-decoded packet.body, never the raw bytes."""
        payload = bytes(range(256)) * 2
        packet = build_dialect_publish("/t_ms", payload)
        frame = Frame(frames.parse_packet(packet))
        assert frame.topic == "/t_ms"
        assert frame.payload == payload

    def test_non_publish_packet_carries_no_topic(self):
        """Control packets keep the raw-body contract: no topic split."""
        frame = Frame(frames.parse_packet(bytes([0xD0, 0x00])))  # PINGRESP
        assert frame.kind == "PINGRESP"
        assert frame.topic is None
        assert frame.payload == b""
