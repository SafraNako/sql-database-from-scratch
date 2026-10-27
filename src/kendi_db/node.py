"""A B-tree page in memory, and how it is laid out in the 4096 (or other) bytes of the file.

Page types: 13 = leaf of a table tree (cells: payload size, rowid, record), 5 = interior of a table tree (cells: left child, rowid), 10 = leaf of an index tree (cells:
payload size, record), 2 = interior of an index tree (cells: left child, payload size, record). An interior page also has a "right-most pointer". After the page header
(8 bytes for a leaf, 12 for an interior page) comes the array of 2-byte cell offsets, in key order; the cell contents are packed at the end of the page, growing towards
the front. A record too big for its cell continues in a chain of overflow pages (the cell keeps only the first bytes and the number of the first overflow page).

In memory a page is a list of ``Cell`` objects, so that the tree code works on lists and the page is only laid out again when it is written.
"""
from __future__ import annotations

import struct

from .errors import CorruptDatabase
from .format import decode_signed_varint, decode_varint, encode_varint, varint_len

TABLE_LEAF, TABLE_INTERIOR, INDEX_LEAF, INDEX_INTERIOR = 13, 5, 10, 2
LEAF_KINDS = (TABLE_LEAF, INDEX_LEAF)
KINDS = (TABLE_LEAF, TABLE_INTERIOR, INDEX_LEAF, INDEX_INTERIOR)
MIN_CELL = 4


class Cell:
    """One cell. ``left``: the child page (interior pages), ``key``: the rowid (table pages), ``payload``: the part of the record kept in the page, ``total``: the size of the
    whole record, ``ovfl``: the first overflow page or 0, ``vals``: the decoded record, filled in when a comparison needs it."""
    __slots__ = ("left", "key", "payload", "ovfl", "total", "vals")

    def __init__(self, left=0, key=None, payload=b"", ovfl=0, total=0):
        self.left = left
        self.key = key
        self.payload = payload
        self.ovfl = ovfl
        self.total = total
        self.vals = None

    def with_left(self, left):
        """A copy pointing at another child. Cells are never changed once they are in a page, only replaced, so that a statement can be undone by restoring the page's list."""
        c = Cell(left, self.key, self.payload, self.ovfl, self.total)
        c.vals = self.vals
        return c

    def __repr__(self):
        return "Cell(left=%r, key=%r, total=%r, ovfl=%r)" % (self.left, self.key, self.total, self.ovfl)


class Node:
    __slots__ = ("pgno", "kind", "cells", "right")

    def __init__(self, pgno, kind, cells=None, right=0):
        self.pgno = pgno
        self.kind = kind
        self.cells = cells if cells is not None else []
        self.right = right                   # the right-most child (interior pages)

    @property
    def leaf(self):
        return self.kind == TABLE_LEAF or self.kind == INDEX_LEAF

    @property
    def is_table(self):
        return self.kind == TABLE_LEAF or self.kind == TABLE_INTERIOR

    def __repr__(self):
        return "Node(%d, kind=%d, %d cells, right=%d)" % (self.pgno, self.kind, len(self.cells), self.right)


def max_local(usable, kind):
    if kind == TABLE_LEAF:
        return usable - 35
    return (usable - 12) * 64 // 255 - 23


def min_local(usable):
    return (usable - 12) * 32 // 255 - 23


def local_payload_size(total, usable, kind):
    """How many bytes of a record of ``total`` bytes stay in the cell (the rest goes to overflow pages): the rule of the file format."""
    mx = max_local(usable, kind)
    if total <= mx:
        return total
    mn = min_local(usable)
    k = mn + (total - mn) % (usable - 4)
    return k if k <= mx else mn


def cell_size(kind, c):
    if kind == TABLE_INTERIOR:
        n = 4 + varint_len(c.key)
    elif kind == TABLE_LEAF:
        n = varint_len(c.total) + varint_len(c.key) + len(c.payload) + (4 if c.ovfl else 0)
    elif kind == INDEX_LEAF:
        n = varint_len(c.total) + len(c.payload) + (4 if c.ovfl else 0)
    else:
        n = 4 + varint_len(c.total) + len(c.payload) + (4 if c.ovfl else 0)
    return n if n >= MIN_CELL else MIN_CELL


def cell_bytes(kind, c):
    if kind == TABLE_INTERIOR:
        out = struct.pack(">I", c.left) + encode_varint(c.key)
    elif kind == TABLE_LEAF:
        out = encode_varint(c.total) + encode_varint(c.key) + c.payload + (struct.pack(">I", c.ovfl) if c.ovfl else b"")
    elif kind == INDEX_LEAF:
        out = encode_varint(c.total) + c.payload + (struct.pack(">I", c.ovfl) if c.ovfl else b"")
    else:
        out = struct.pack(">I", c.left) + encode_varint(c.total) + c.payload + (struct.pack(">I", c.ovfl) if c.ovfl else b"")
    if len(out) < MIN_CELL:
        out += b"\x00" * (MIN_CELL - len(out))
    return out


def header_size(kind):
    return 8 if kind in LEAF_KINDS else 12


def used_bytes(node, base=0):
    """Bytes the page needs: header, cell offsets and cells (``base`` is 100 for page 1, which starts with the file header)."""
    kind = node.kind
    return base + header_size(kind) + 2 * len(node.cells) + sum(cell_size(kind, c) for c in node.cells)


def parse_node(pgno, raw, usable, base=0):
    kind = raw[base]
    if kind not in KINDS:
        raise CorruptDatabase("page %d is not a B-tree page (type byte %d)" % (pgno, kind))
    ncells = (raw[base + 3] << 8) | raw[base + 4]
    right = struct.unpack_from(">I", raw, base + 8)[0] if kind in (TABLE_INTERIOR, INDEX_INTERIOR) else 0
    pointers = base + header_size(kind)
    cells = []
    for i in range(ncells):
        off = (raw[pointers + 2 * i] << 8) | raw[pointers + 2 * i + 1]
        if off < pointers + 2 * ncells or off >= usable:
            raise CorruptDatabase("page %d: a cell pointer (%d) is outside the page" % (pgno, off))
        if kind == TABLE_INTERIOR:
            left = struct.unpack_from(">I", raw, off)[0]
            key, _ = decode_signed_varint(raw, off + 4)
            cells.append(Cell(left=left, key=key))
        elif kind == TABLE_LEAF:
            total, p = decode_varint(raw, off)
            key, p = decode_signed_varint(raw, p)
            local = local_payload_size(total, usable, kind)
            ovfl = struct.unpack_from(">I", raw, p + local)[0] if total > local else 0
            cells.append(Cell(key=key, payload=bytes(raw[p:p + local]), ovfl=ovfl, total=total))
        else:
            p = off
            left = 0
            if kind == INDEX_INTERIOR:
                left = struct.unpack_from(">I", raw, p)[0]
                p += 4
            total, p = decode_varint(raw, p)
            local = local_payload_size(total, usable, kind)
            ovfl = struct.unpack_from(">I", raw, p + local)[0] if total > local else 0
            cells.append(Cell(left=left, payload=bytes(raw[p:p + local]), ovfl=ovfl, total=total))
    return Node(pgno, kind, cells, right)


def serialize_node(node, page_size, usable, base=0):
    """The page as bytes: a fresh layout with the cells packed at the end and no free blocks."""
    kind = node.kind
    out = bytearray(page_size)
    n = len(node.cells)
    out[base] = kind
    out[base + 3] = n >> 8
    out[base + 4] = n & 0xFF
    if kind in (TABLE_INTERIOR, INDEX_INTERIOR):
        struct.pack_into(">I", out, base + 8, node.right)
    pointers = base + header_size(kind)
    pos = usable
    for i, c in enumerate(node.cells):
        data = cell_bytes(kind, c)
        pos -= len(data)
        if pos < pointers + 2 * n:
            raise CorruptDatabase("internal error: page %d overflows (%d cells)" % (node.pgno, n))
        out[pos:pos + len(data)] = data
        out[pointers + 2 * i] = pos >> 8
        out[pointers + 2 * i + 1] = pos & 0xFF
    start = pos if n else usable
    if start == 65536:
        start = 0
    out[base + 5] = start >> 8
    out[base + 6] = start & 0xFF
    return out
