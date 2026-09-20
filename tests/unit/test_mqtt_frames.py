"""MQTT frame codec tests — including a full round-trip against the REAL
captured CONNECT from the production client (assets/messenger_ws_full.json).

Unit (offline): synthetic packet encode/decode plus round-trips against
the ``captured_connect`` fixture (the 452B MQIsdp CONNECT sliced from
the ``ws_capture`` asset, cli/data/messenger_ws_full.json). No network.
"""
import json
import struct

import pytest

from constants import (
    MQTT_CLIENT_ID_PREFIX,
    MQTT_CONNECT_FLAGS,
    MQTT_KEEPALIVE_S,
    MQTT_OP_PUBLISH,
)
from realtime.mqtt import frames
from realtime.mqtt.connect import build_fb_connect, rewrite_connect_identity
from realtime.mqtt.frames import MQTTDecodeError


class TestVarint:
    """Pins MQTT remaining-length varint encode/decode across the
    multi-byte boundary values."""

    def test_roundtrip(self):
        for n in (0, 1, 127, 128, 16383, 449, 65535, 268435455):
            enc = frames.encode_remaining_length(n)
            val, _ = frames.remaining_length(b"\x00" + enc, 1)
            assert val == n


class TestConnectCodec:
    """Pins the MQIsdp CONNECT codec against both a synthetic identity and
    the REAL 452B captured CONNECT (P3-1), including actor rewriting."""

    def test_build_and_decode_roundtrip(self):
        identity = json.dumps({"u": "123", "aid": 2220391788200892}).encode()
        packet = frames.build_connect(identity)
        anatomy = frames.decode_connect(packet)
        assert anatomy["protocol"] == "MQIsdp"
        assert anatomy["level"] == 3
        assert anatomy["flags"] == f"0x{MQTT_CONNECT_FLAGS:02x}"
        assert anatomy["keepalive"] == MQTT_KEEPALIVE_S
        assert anatomy["client_id"] == MQTT_CLIENT_ID_PREFIX
        assert json.loads(anatomy["username"])["u"] == "123"

    def test_real_captured_connect_decodes(self, captured_connect):
        """The production client's 452-byte CONNECT (doc 15 §P3-1)."""
        anatomy = frames.decode_connect(captured_connect)
        assert anatomy["protocol"] == "MQIsdp"
        assert anatomy["level"] == 3
        assert anatomy["keepalive"] == 15
        assert anatomy["client_id"] == "mqttwsclient"
        identity = json.loads(anatomy["username"])
        assert identity["aid"] == 2220391788200892
        assert identity["ct"] == "websocket"
        assert identity["u"] == "12345678901234"  # the live capture's actor
        assert identity["d"].count("-") == 4      # a uuid4

    def test_fresh_connect_has_same_shape_as_capture(self, captured_connect):
        live = frames.decode_connect(captured_connect)
        mine = frames.decode_connect(build_fb_connect("12345678901234"))
        assert mine["protocol"] == live["protocol"]
        assert mine["level"] == live["level"]
        assert mine["flags"] == live["flags"]
        assert mine["keepalive"] == live["keepalive"]
        assert mine["client_id"] == live["client_id"]

    def test_rewrite_identity_preserves_or_rebuilds(self, captured_connect):
        rewritten = rewrite_connect_identity(captured_connect, "12345678901234")
        anatomy = frames.decode_connect(rewritten)
        identity = json.loads(anatomy["username"])
        assert identity["u"] == "12345678901234"
        assert identity["d"] != json.loads(frames.decode_connect(
            captured_connect)["username"])["d"]

    def test_rewrite_rejects_wrong_actor(self, captured_connect):
        with pytest.raises(ValueError):
            rewrite_connect_identity(captured_connect, "99999999999")


class TestPackets:
    """Pins SUBSCRIBE/PUBLISH/CONNACK/SUBACK/PINGREQ codec shapes,
    including the live-captured SUBACK bytes and truncation rejection."""

    def test_subscribe_encode_decode(self):
        sub = frames.encode_subscribe("/t_ms", packet_id=7)
        assert sub[0] == 0x82
        packet = frames.parse_packet(sub)
        assert packet.kind == "SUBSCRIBE"
        body = packet.body
        pid = struct.unpack(">H", body[:2])[0]
        tlen = struct.unpack(">H", body[2:4])[0]
        assert pid == 7
        assert body[4:4 + tlen].decode() == "/t_ms"
        assert body[4 + tlen] == 0

    def test_publish_roundtrip(self):
        pub = frames.encode_publish("/t_ms", b"payload-bytes")
        packet = frames.parse_packet(pub)
        assert packet.op & 0xF0 == MQTT_OP_PUBLISH
        tlen = struct.unpack(">H", packet.body[:2])[0]
        assert packet.body[2 + tlen:] == b"payload-bytes"

    def test_connack_decode_accepted(self):
        sp, rc = frames.decode_connack(bytes([0x20, 0x02, 0x00, 0x00]))
        assert (sp, rc) == (0, 0)

    def test_connack_decode_rejected(self):
        _, rc = frames.decode_connack(bytes([0x20, 0x02, 0x00, 0x05]))
        assert rc == 5

    def test_suback_grants(self):
        pid, granted = frames.decode_suback(bytes([0x90, 0x03, 0x00, 0x01, 0x00]))
        assert (pid, granted) == (1, [0])

    def test_real_captured_suback(self):
        """Live SUBACK from P3-1: 90 03 00 01 00."""
        assert frames.decode_suback(bytes.fromhex("9003000100")) == (1, [0])

    def test_truncated_packet_raises(self):
        with pytest.raises(MQTTDecodeError):
            frames.parse_packet(bytes([0x30, 0x20, 0x01]))  # claims 32, has 1

    def test_pingreq(self):
        assert frames.encode_pingreq() == b"\xc0\x00"
