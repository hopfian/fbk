"""DGW frame codec tests — round-trip against real captured frames (P3-6).

Unit (offline): synthetic encode/decode plus the REAL full-byte capture
via the ``ws_capture`` fixture (cli/data/messenger_ws_full.json — the
rpsignaling/realtime/lightspeed sockets). No network.
"""
import base64
import json

import pytest

from constants import DGW_OP_ACK8, DGW_OP_CONTROL, DGW_OP_DATA
from realtime.dgw.frames import DGWDecodeError, DGWFrame


def _sent_frames(ws_capture, channel: str, count: int = 3):
    sock = next(s for s in ws_capture
                if s["url"].split("?")[0].endswith(f"/{channel}"))
    out = []
    for f in sock["frames"]:
        if f["dir"] == "sent":
            out.append(base64.b64decode(f["b64"]))
        if len(out) >= count:
            break
    return out


class TestEncodeDecode:
    """Pins the fixed-header codec round-trip and the live handshake byte
    shapes (rpsignaling opener, ping, {"code": 200} ack)."""

    def test_roundtrip(self):
        frame = DGWFrame(op=DGW_OP_CONTROL, seq=0x1234, flags=0, payload=b"{}")
        data = frame.encode()
        assert DGWFrame.decode(data) == frame

    def test_control_constructor(self):
        data = DGWFrame.control(0, {}).encode()
        # live rpsignaling handshake: 0f 0000 0200 00 7b7d
        assert data == bytes.fromhex("0f00000200007b7d")

    def test_ping_frame(self):
        assert DGWFrame.ping(0).encode() == b"\x09\x00\x00\x00\x00\x00"

    def test_json_payload(self):
        frame = DGWFrame.decode(bytes.fromhex("0f00000c00007b22636f6465223a3230307d"))
        assert frame.json() == {"code": 200}  # the live handshake ack


class TestJSONContract:
    """Pins the json() object contract (docs/15 §P3-6): control/data
    payloads are JSON OBJECTS. Valid-JSON non-objects are hostile
    off-spec frames and raise the typed DGWDecodeError naming the
    payload type; object payloads return the dict; binary/non-JSON and
    empty payloads stay None (opaque frames are a normal case the
    caller distinguishes — that contract is unchanged)."""

    def test_object_payload_returns_dict(self):
        frame = DGWFrame(op=DGW_OP_CONTROL, seq=0, flags=0, payload=b'{"code":200}')
        assert frame.json() == {"code": 200}

    def test_list_payload_raises_typed_error(self):
        frame = DGWFrame(op=DGW_OP_CONTROL, seq=0, flags=0, payload=b"[1,2]")
        with pytest.raises(DGWDecodeError, match="list"):
            frame.json()

    def test_scalar_payload_raises_typed_error(self):
        frame = DGWFrame(op=DGW_OP_DATA, seq=0, flags=0, payload=b"42")
        with pytest.raises(DGWDecodeError, match="int"):
            frame.json()

    def test_non_object_json_error_is_a_valueerror(self):
        # DGWDecodeError subclasses ValueError: the frames-level typed
        # guard matches the MQTTDecodeError discipline (mqtt/frames.py)
        frame = DGWFrame(op=DGW_OP_CONTROL, seq=0, flags=0, payload=b"[1,2]")
        with pytest.raises(ValueError):
            frame.json()

    def test_binary_and_empty_payloads_stay_none(self):
        opaque = DGWFrame(op=DGW_OP_DATA, seq=0, flags=0, payload=b"\x00\x80\x01")
        assert opaque.json() is None
        assert DGWFrame.ping(0).json() is None

    def test_error_names_the_frame_kind_and_type(self):
        frame = DGWFrame(op=DGW_OP_CONTROL, seq=0, flags=0, payload=b"[1,2]")
        with pytest.raises(DGWDecodeError, match="CONTROL frame JSON payload is list"):
            frame.json()


class TestCapturedFrames:
    """Pins decode() against the REAL captured rpsignaling / realtime /
    lightspeed frames from the full WS capture."""

    def test_rpsignaling_handshake_matches(self, ws_capture):
        first = _sent_frames(ws_capture, "rpsignaling", 1)[0]
        frame = DGWFrame.decode(first)
        assert frame.op == DGW_OP_CONTROL
        assert frame.seq == 0
        assert frame.json() == {}

    def test_realtime_first_control_frame(self, ws_capture):
        first = _sent_frames(ws_capture, "realtime", 1)[0]
        frame = DGWFrame.decode(first)
        assert frame.op == DGW_OP_CONTROL
        body = frame.json()
        assert "x-dgw-app-XRSS-method" in json.dumps(body)

    def test_lightspeed_data_frame_decodes(self, ws_capture):
        sock = next(s for s in ws_capture
                     if s["url"].split("?")[0].endswith("/lightspeed"))
        data = None
        for f in sock["frames"]:
            if f["dir"] == "sent" and f["n"] > 500:
                data = base64.b64decode(f["b64"])
                break
        assert data is not None
        frame = DGWFrame.decode(data[:6 + 2])  # header slice of the control part
        assert frame.op in (DGW_OP_CONTROL, 0x0D)

    def test_ack_shape(self, ws_capture):
        sock = next(s for s in ws_capture
                    if s["url"].split("?")[0].endswith("/realtime"))
        acks = [f for f in sock["frames"]
                if f["dir"] == "recv" and f["n"] == 8
                and base64.b64decode(f["b64"])[0] == DGW_OP_ACK8]
        assert acks, "expected at least one captured ACK8"
        frame = DGWFrame.decode(base64.b64decode(acks[0]["b64"]))
        assert frame.op == DGW_OP_ACK8


class TestErrors:
    """Pins truncation rejection: short headers and truncated payloads
    raise ValueError, never a mis-decoded frame."""

    def test_too_short_rejected(self):
        with pytest.raises(ValueError):
            DGWFrame.decode(b"\x0f\x00")

    def test_truncated_payload_rejected(self):
        frame = DGWFrame(op=DGW_OP_CONTROL, seq=0, flags=0, payload=b"{}")
        data = frame.encode()
        with pytest.raises(ValueError):
            DGWFrame.decode(data[:-1])
