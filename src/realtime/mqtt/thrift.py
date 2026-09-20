"""Minimal Thrift Compact Protocol reader (docs/06 §5).

The /t_ms PUBLISH bodies (message deltas) are Thrift-compact-encoded. This
module decodes them generically: it walks the wire format (field headers,
varint zigzag ints, length-prefixed strings/binary, list headers) and yields
values, without needing the Messenger .thrift IDL — structure recovery is a
Phase-4 task and the reader is deliberately forgiving with unknown fields.

ARCHITECTURE:

  IDL-Free Decoding (docs/06 §5.3):
    A generic compact-protocol field dumper is the right first tool
    because it makes schema drift observable even before fields can be
    named: field ids, wire types, and raw values are recovered with no
    .thrift schema. ``dump`` keys results by field id; the caller maps ids
    to names when ground truth exists (the Iris-era ``syncApiVersion``
    bump invalidates any frozen mapping — docs/06 §5.2).

  Bounds Discipline:
    Every primitive validates against the buffer before reading — reads
    past the end, strings/lists/doubles that run off the buffer, and
    over-long varints all raise the typed ``ThriftError`` instead of
    producing garbage (a hostile or truncated PUBLISH must fail loudly,
    never decode optimistically).

CALIBRATION NOTES:
  Type ids follow the Thrift Compact spec (docs/06 §5.3 crash course):
  field header = (delta << 4) | type, zigzag varints for signed ints,
  strings = varint length + raw UTF-8, list headers inline the size in
  the high nibble with 15 as the escape to a follow-up varint.

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import struct
from collections.abc import Iterator
from typing import Any


class ThriftError(ValueError):
    """Malformed thrift-compact payload."""


class CompactReader:
    """Cursor over a thrift-compact byte buffer.

    All reads advance ``pos`` monotonically and validate the buffer bound
    first; a reader instance can be passed around to resume decoding at
    any offset (``dump`` uses this to hand nested structs to child
    readers while keeping the parent cursor in sync).
    """

    def __init__(self, buf: bytes, offset: int = 0):
        """Bind the reader to a buffer.

        Args:
            buf: The thrift-compact payload (a /t_ms PUBLISH body).
            offset: Starting cursor position; non-zero for child readers
                decoding a nested struct in place.
        """
        self.buf = buf
        self.pos = offset

    # ------------------------------------------------------------------ low-level
    def read_byte(self) -> int:
        """Read one byte and advance.

        Returns:
            The byte value at the cursor.

        Raises:
            ThriftError: If the cursor is at or past the end of the buffer.
        """
        if self.pos >= len(self.buf):
            raise ThriftError("read past end of buffer")
        b = self.buf[self.pos]
        self.pos += 1
        return b

    def read_varint(self) -> int:
        """Read a base-128 varint (7 payload bits, 0x80 continuation).

        Returns:
            The decoded unsigned value.

        Raises:
            ThriftError: On buffer exhaustion mid-varint, or a varint
                longer than 63 bits (not a valid thrift-compact value).
        """
        result, shift = 0, 0
        while True:
            b = self.read_byte()
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                return result
            shift += 7
            if shift > 63:
                raise ThriftError("varint too long")

    def read_zigzag(self) -> int:
        """ZigZag-decoded signed integer.

        Thrift-compact zigzag: the wire value is ``n << 1` xor-sign;
        decoding is ``(v >> 1) ^ -(v & 1)`` (docs/06 §5.3), so small
        magnitudes stay small on the wire regardless of sign.

        Returns:
            The signed integer value.
        """
        v = self.read_varint()
        return (v >> 1) ^ -(v & 1)

    def read_string(self) -> bytes:
        """Read a varint-length-prefixed binary/string payload.

        Returns:
            The raw payload bytes (callers apply UTF-8 decoding).

        Raises:
            ThriftError: If the announced length runs past the buffer end
                — a truncated payload must never yield a short slice.
        """
        length = self.read_varint()
        if self.pos + length > len(self.buf):
            raise ThriftError("string runs past end of buffer")
        out = self.buf[self.pos:self.pos + length]
        self.pos += length
        return out

    # ------------------------------------------------------------------- schemaful
    def read_field_header(self, last_id: int) -> tuple[int, int]:
        """Compact field header: (field_id, type). Stop fields return type 0.

        Header byte = (delta << 4) | type where delta is the offset from
        the previous field id; delta 0 means the full id follows as a
        zigzag varint (docs/06 §5.3). Type 0 (STOP) ends every struct.

        Args:
            last_id: The previously seen field id in this struct, for
                delta arithmetic.

        Returns:
            The (field_id, type_id) pair; (0, 0) for the stop field.
        """
        b = self.read_byte()
        if b == 0:
            return 0, 0
        delta = (b & 0xF0) >> 4
        ftype = b & 0x0F
        fid = last_id + delta if delta else self.read_zigzag()
        return fid, ftype

    # thrift-compact wire type ids (docs/06 §5.3; NOT contiguous with the
    # binary protocol's ids — booleans are split into two literal types)
    TYPE_BOOL_TRUE = 1
    TYPE_BOOL_FALSE = 2
    TYPE_BYTE = 3
    TYPE_I16 = 4
    TYPE_I32 = 5
    TYPE_I64 = 6
    TYPE_DOUBLE = 7
    TYPE_BINARY = 8
    TYPE_LIST = 9
    TYPE_SET = 10
    TYPE_MAP = 11
    TYPE_STRUCT = 12

    def read_value(self, ftype: int) -> Any:
        """Read one value of the given compact type.

        Args:
            ftype: One of the ``TYPE_*`` wire ids; booleans read as no
                bytes (the type id IS the value), sets decode as lists,
                and maps carry their key/value types in one header byte.

        Returns:
            The decoded Python value: bool, int, float, bytes, list, or
            list of (field_id, type_id, value) tuples for nested structs.

        Raises:
            ThriftError: On an unsupported type id or any bounds violation.
        """
        if ftype in (self.TYPE_BOOL_TRUE, self.TYPE_BOOL_FALSE):
            # compact quirk: the boolean's value IS its field type id
            return ftype == self.TYPE_BOOL_TRUE
        if ftype == self.TYPE_BYTE:
            return self.read_byte()
        if ftype in (self.TYPE_I16, self.TYPE_I32, self.TYPE_I64):
            return self.read_zigzag()
        if ftype == self.TYPE_DOUBLE:
            # 8-byte little-endian IEEE 754 (thrift-compact endianness)
            raw = self.buf[self.pos:self.pos + 8]
            if len(raw) < 8:
                raise ThriftError("double runs past end of buffer")
            self.pos += 8
            return struct.unpack("<d", raw)[0]
        if ftype == self.TYPE_BINARY:
            return self.read_string()
        if ftype in (self.TYPE_LIST, self.TYPE_SET):
            header = self.read_byte()
            size = (header & 0xF0) >> 4
            elem_type = header & 0x0F
            if size == 15:
                # size-15 nibble is the escape to a follow-up varint
                size = self.read_varint()
            return [self.read_value(elem_type) for _ in range(size)]
        if ftype == self.TYPE_STRUCT:
            return list(self.read_struct())
        if ftype == self.TYPE_MAP:
            size = self.read_varint()
            if size == 0:
                return {}
            # empty maps have NO type header byte (compact spec quirk)
            kv_header = self.read_byte()
            ktype, vtype = (kv_header & 0xF0) >> 4, kv_header & 0x0F
            return {self.read_value(ktype): self.read_value(vtype) for _ in range(size)}
        raise ThriftError(f"unsupported thrift type {ftype}")

    def read_struct(self) -> Iterator[tuple[int, int, Any]]:
        """Yield (field_id, type_id, value) for every field of one struct.

        Yields:
            Each field until the stop header; nested structs yield as a
            list of child tuples.
        """
        fid = 0
        while True:
            fid, ftype = self.read_field_header(fid)
            if ftype == 0:
                return
            yield fid, ftype, self.read_value(ftype)

    # ------------------------------------------------------------------ high-level
    def dump(self, max_depth: int = 6) -> dict[str, Any]:
        """Decode a top-level struct into a dict keyed by field id.

        Nested structs are read recursively by read_value (TYPE_STRUCT),
        bounded by max_depth to survive pathological payloads.

        Args:
            max_depth: Remaining recursion budget; deeper nested structs
                are skipped and marked ``"<struct:depth-limit>"`` instead
                of risking runaway recursion on hostile payloads.

        Returns:
            The field-id-keyed dict for the struct at the cursor.
        """
        if max_depth <= 0:
            return {}
        result: dict[str, Any] = {}
        fid = 0
        while True:
            fid, ftype = self.read_field_header(fid)
            if ftype == 0:
                return result
            if ftype == self.TYPE_STRUCT:
                if max_depth <= 1:
                    self._skip_struct()
                    result[str(fid)] = "<struct:depth-limit>"
                else:
                    nested = CompactReader(self.buf, self.pos)
                    result[str(fid)] = nested.dump(max_depth - 1)
                    self.pos = nested.pos  # advance the parent cursor past the child
            else:
                result[str(fid)] = self.read_value(ftype)

    def _skip_struct(self) -> None:
        """Fast-forward over one nested struct (depth-limited dump)."""
        fid = 0
        while True:
            fid, ftype = self.read_field_header(fid)
            if ftype == 0:
                return
            if ftype == self.TYPE_STRUCT:
                self._skip_struct()
            else:
                self.read_value(ftype)
