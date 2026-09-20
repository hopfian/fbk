"""Thrift-compact reader tests: crafted payloads + truncation behavior.

Unit (offline): every payload is assembled byte-by-byte with the local
varint/zigzag helpers — no captured fixtures, no session, no network.
Pins the CompactReader dialect the MQTT /t_ms delta decoder relies on.
"""

import pytest

from realtime.mqtt.thrift import CompactReader, ThriftError


def write_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def zigzag(v: int) -> int:
    return (v << 1) ^ (v >> 63) if v < 0 else v << 1


def field_header(fid: int, ftype: int, last_id: int = 0) -> bytes:
    delta = fid - last_id
    if 0 < delta <= 15:
        return bytes([(delta << 4) | ftype])
    return bytes([ftype]) + write_varint(zigzag(fid))


class TestCompactReader:
    """Pins the compact-protocol decode: varints, zigzag integers, lists,
    nested structs, the depth cap, and truncation rejection."""

    def test_i32_field(self):
        payload = field_header(1, CompactReader.TYPE_I32) + write_varint(zigzag(-42)) + b"\x00"
        out = CompactReader(payload).dump()
        assert out == {"1": -42}

    def test_string_field(self):
        payload = (field_header(1, CompactReader.TYPE_BINARY)
                   + write_varint(5) + b"hello" + b"\x00")
        assert CompactReader(payload).dump() == {"1": b"hello"}

    def test_list_of_i64(self):
        body = field_header(2, CompactReader.TYPE_LIST)
        body += bytes([(2 << 4) | CompactReader.TYPE_I64])  # size 2, i64 elements
        body += write_varint(zigzag(111)) + write_varint(zigzag(222))
        out = CompactReader(body + b"\x00").dump()
        assert out == {"2": [111, 222]}

    def test_nested_struct(self):
        inner = (field_header(1, CompactReader.TYPE_I32) + write_varint(zigzag(7))
                 + b"\x00")  # struct stop
        outer = (field_header(3, CompactReader.TYPE_STRUCT) + inner + b"\x00")
        out = CompactReader(outer).dump()
        assert out == {"3": {"1": 7}}

    def test_depth_limit(self):
        inner = field_header(1, CompactReader.TYPE_I32) + write_varint(zigzag(1)) + b"\x00"
        outer = field_header(9, CompactReader.TYPE_STRUCT) + inner + b"\x00"
        out = CompactReader(outer).dump(max_depth=1)
        assert out == {"9": "<struct:depth-limit>"}

    def test_bool_values(self):
        payload = (field_header(1, CompactReader.TYPE_BOOL_TRUE)
                   + field_header(2, CompactReader.TYPE_BOOL_FALSE, last_id=1) + b"\x00")
        assert CompactReader(payload).dump() == {"1": True, "2": False}

    def test_long_field_id(self):
        payload = bytes([CompactReader.TYPE_I32]) + write_varint(zigzag(1000)) \
            + write_varint(zigzag(5)) + b"\x00"
        assert CompactReader(payload).dump() == {"1000": 5}

    def test_truncated_raises(self):
        payload = field_header(1, CompactReader.TYPE_BINARY) + write_varint(50)
        with pytest.raises(ThriftError):
            CompactReader(payload).dump()

    def test_varint_past_end_raises(self):
        with pytest.raises(ThriftError):
            CompactReader(b"\x80\x80").read_varint()
