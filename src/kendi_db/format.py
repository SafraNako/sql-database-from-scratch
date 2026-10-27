"""The byte-level formats of the SQLite 3 database file that this engine reads and writes: variable-length integers and the record format of a row.

A record is a header (its own length as a varint, then one varint "serial type" per column) followed by the column values; the serial type says what the value is and
how many bytes it takes: 0 NULL, 1-6 integers of 1, 2, 3, 4, 6, 8 bytes, 7 an 8-byte float, 8 and 9 the constants 0 and 1, and for N >= 12 a blob (even N, (N-12)/2
bytes) or a string (odd N, (N-13)/2 bytes).
"""
from __future__ import annotations

import struct

from .errors import CorruptDatabase

MASK64 = 0xFFFFFFFFFFFFFFFF
INT_MIN, INT_MAX = -(1 << 63), (1 << 63) - 1


def varint_len(v):
    v &= MASK64
    if v <= 0x7F:
        return 1
    if v > 0x00FFFFFFFFFFFFFF:
        return 9
    n = 1
    while v > 0x7F:
        v >>= 7
        n += 1
    return n


def encode_varint(v):
    """The 1 to 9 bytes of ``v`` (taken as an unsigned 64-bit number): big-endian groups of seven bits, the high bit of a byte saying that another byte follows; the
    ninth byte, when there is one, carries a full eight bits."""
    v &= MASK64
    if v <= 0x7F:
        return bytes((v,))
    if v > 0x00FFFFFFFFFFFFFF:
        buf = bytearray(9)
        buf[8] = v & 0xFF
        v >>= 8
        for i in range(7, -1, -1):
            buf[i] = (v & 0x7F) | 0x80
            v >>= 7
        return bytes(buf)
    out = []
    while True:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
        if v == 0:
            break
    out[0] &= 0x7F
    out.reverse()
    return bytes(out)


def decode_varint(buf, pos):
    """``(value, position after it)``."""
    v = 0
    for i in range(8):
        b = buf[pos + i]
        v = (v << 7) | (b & 0x7F)
        if not b & 0x80:
            return v, pos + i + 1
    return (v << 8) | buf[pos + 8], pos + 9


def signed64(v):
    return v - (1 << 64) if v >= (1 << 63) else v


def decode_signed_varint(buf, pos):
    v, pos = decode_varint(buf, pos)
    return signed64(v), pos


# --- records -----------------------------------------------------------------------------------------------------------------------------------
_INT_SIZES = ((1, 1, 0x7F), (2, 2, 0x7FFF), (3, 3, 0x7FFFFF), (4, 4, 0x7FFFFFFF), (5, 6, 0x7FFFFFFFFFFF), (6, 8, INT_MAX))


def serial_type(v):
    """``(serial type, number of body bytes)`` of a value."""
    if v is None:
        return 0, 0
    t = type(v)
    if t is int:
        if v == 0:
            return 8, 0
        if v == 1:
            return 9, 0
        mag = v if v >= 0 else ~v
        for code, size, limit in _INT_SIZES:
            if mag <= limit:
                return code, size
        raise OverflowError("integer out of range for a 64-bit column")
    if t is float:
        return 7, 8
    if t is str:
        n = len(v.encode("utf-8", "surrogateescape"))
        return 13 + 2 * n, n
    if t is bytes or t is bytearray or t is memoryview:
        n = len(v)
        return 12 + 2 * n, n
    raise TypeError("cannot store %r" % (v,))


def encode_record(values):
    """The record for a row (a sequence of None, int, float, str and bytes values)."""
    types = bytearray()
    body = []
    for v in values:
        code, size = serial_type(v)
        types += encode_varint(code)
        if size == 0:
            continue
        if code == 7:
            body.append(struct.pack(">d", v))
        elif code >= 12:
            body.append(v.encode("utf-8", "surrogateescape") if code & 1 else bytes(v))
        elif size == 3:
            body.append(struct.pack(">i", v)[1:])
        elif size == 6:
            body.append(struct.pack(">q", v)[2:])
        else:
            body.append(v.to_bytes(size, "big", signed=True))
    # the header's length includes the bytes of its own length
    n = 1
    while varint_len(len(types) + n) != n:
        n += 1
    header_len = len(types) + n
    return encode_varint(header_len) + bytes(types) + b"".join(body)


def decode_record(buf, columns=None):
    """The list of values of a record. ``columns`` (a number) limits how many are decoded from the front."""
    header_len, pos = decode_varint(buf, 0)
    types = []
    while pos < header_len:
        t, pos = decode_varint(buf, pos)
        types.append(t)
        if columns is not None and len(types) >= columns:
            break
    body = header_len
    out = []
    for t in types:
        if t == 0:
            out.append(None)
        elif t == 8:
            out.append(0)
        elif t == 9:
            out.append(1)
        elif t <= 6:
            size = (0, 1, 2, 3, 4, 6, 8)[t]
            out.append(int.from_bytes(buf[body:body + size], "big", signed=True))
            body += size
        elif t == 7:
            out.append(struct.unpack(">d", buf[body:body + 8])[0])
            body += 8
        elif t >= 12:
            size = (t - 12) // 2 if t % 2 == 0 else (t - 13) // 2
            chunk = bytes(buf[body:body + size])
            out.append(chunk if t % 2 == 0 else chunk.decode("utf-8", "surrogateescape"))
            body += size
        else:
            raise CorruptDatabase("reserved serial type %d in a record" % t)
    return out
