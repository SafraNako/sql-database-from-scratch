"""B-trees on top of the pager, in SQLite's layout.

A *table tree* is a B+tree keyed by the 64-bit rowid: the rows are in the leaves, the interior pages only hold (child, largest rowid of that child) pairs. An *index tree*
is a B-tree whose keys are records (the indexed columns followed by the rowid): every key lives in exactly one place, a leaf or an interior page, and the interior
keys separate the subtrees. Both keep the root page number fixed for the life of the tree (the schema table remembers it), so a root that has to split pushes its content
down into a new child first.

Insertion splits a page that no longer fits, into as many pages as it takes, and the parent gets the separators (which may make the parent split in turn). Deletion
removes the cell; a page that becomes empty is taken out of the tree and given to the free list. Pages are not re-balanced when they are merely half empty: the file
format does not ask for it.
"""
from __future__ import annotations

import struct

from . import node as N
from .errors import CorruptDatabase
from .format import decode_record
from .node import Cell


class _Tree:
    def __init__(self, pager, root, index):
        self.pager = pager
        self.root = root
        self.index = index
        self.leaf_kind = N.INDEX_LEAF if index else N.TABLE_LEAF
        self.interior_kind = N.INDEX_INTERIOR if index else N.TABLE_INTERIOR

    # --- records that may not fit in the page: overflow chains -------------------------------------------------------------------------------
    def _store_payload(self, record):
        """``(local part, first overflow page, total size)``; allocates the overflow pages."""
        total = len(record)
        usable = self.pager.usable
        n = N.local_payload_size(total, usable, self.leaf_kind)
        if total <= n:
            return bytes(record), 0, total
        rest = record[n:]
        chunk = usable - 4
        first, prev = 0, None
        for i in range(0, len(rest), chunk):
            pgno, raw = self.pager.new_raw()
            part = rest[i:i + chunk]
            raw[4:4 + len(part)] = part
            if prev is None:
                first = pgno
            else:
                struct.pack_into(">I", prev, 0, pgno)
            prev = raw
        return bytes(record[:n]), first, total

    def payload(self, cell):
        """The whole record of a cell, following its overflow chain."""
        if not cell.ovfl:
            return cell.payload
        parts = [cell.payload]
        remaining = cell.total - len(cell.payload)
        pgno = cell.ovfl
        seen = 0
        while remaining > 0:
            if pgno == 0 or seen > self.pager.db_size:
                raise CorruptDatabase("an overflow chain ends too early or loops")
            raw = self.pager.get_raw(pgno)
            take = min(remaining, self.pager.usable - 4)
            parts.append(bytes(raw[4:4 + take]))
            remaining -= take
            pgno = struct.unpack_from(">I", raw, 0)[0]
            seen += 1
        return b"".join(parts)

    def _free_overflow(self, cell):
        pgno = cell.ovfl
        remaining = cell.total - len(cell.payload)
        while pgno and remaining > 0:
            raw = self.pager.get_raw(pgno)
            nxt = struct.unpack_from(">I", raw, 0)[0]
            remaining -= self.pager.usable - 4
            self.pager.free_page(pgno)
            pgno = nxt

    def vals(self, cell):
        """The decoded record of an index cell (remembered in the cell)."""
        v = cell.vals
        if v is None:
            v = cell.vals = decode_record(self.payload(cell))
        return v

    # --- pages ---------------------------------------------------------------------------------------------------------------------------------
    def node(self, pgno):
        return self.pager.get_node(pgno)

    def _fits(self, node):
        return N.used_bytes(node, 100 if node.pgno == 1 else 0) <= self.pager.usable

    def _fits_cells(self, kind, cells):
        return N.header_size(kind) + 2 * len(cells) + sum(N.cell_size(kind, c) for c in cells) <= self.pager.usable

    # --- splitting a page that does not fit ----------------------------------------------------------------------------------------------------
    def _split_point(self, kind, cells):
        """The index at which to cut ``cells`` so that the two halves are about the same size in bytes."""
        sizes = [N.cell_size(kind, c) + 2 for c in cells]
        half = sum(sizes) / 2
        acc = 0
        for i, s in enumerate(sizes):
            acc += s
            if acc >= half:
                return i
        return len(cells) - 1

    def _partition_leaf_table(self, cells, append_hint):
        kind = N.TABLE_LEAF
        if append_hint and len(cells) > 1 and self._fits_cells(kind, cells[:-1]):
            return [cells[:-1], cells[-1:]]                    # rows come in order: leave the full page alone, start a new one
        groups = []

        def rec(cs):
            if self._fits_cells(kind, cs):
                groups.append(cs)
                return
            m = min(max(self._split_point(kind, cs) + 1, 1), len(cs) - 1)
            rec(cs[:m])
            rec(cs[m:])
        rec(cells)
        return groups

    def _partition_promoting(self, kind, cells):
        """Cut ``cells`` into groups that fit, taking one cell out between two groups (it goes up into the parent). ``(groups, promoted cells)``."""
        if self._fits_cells(kind, cells):
            return [cells], []
        if len(cells) < 3:
            raise CorruptDatabase("internal error: a page with %d cells does not fit" % len(cells))
        m = min(max(self._split_point(kind, cells), 1), len(cells) - 2)
        left, promoted, right = cells[:m], cells[m], cells[m + 1:]
        gl, sl = self._partition_promoting(kind, left)
        gr, sr = self._partition_promoting(kind, right)
        return gl + gr, sl + [promoted] + sr

    def _split(self, node, path, append_hint=False):
        pager = self.pager
        if not path:                                           # the root: move its content into a new child and split that
            child = pager.new_node(node.kind)
            child.cells, child.right = node.cells, node.right
            node.kind = self.interior_kind
            node.cells, node.right = [], child.pgno
            pager.touch_node(node)
            path = [(node, 0)]
            node = child
        kind = node.kind
        old_right = node.right
        if kind == N.TABLE_LEAF:
            groups, promoted = self._partition_leaf_table(node.cells, append_hint), None
        else:
            groups, promoted = self._partition_promoting(kind, node.cells)
        pages = [node] + [pager.new_node(kind) for _ in groups[1:]]
        separators = []
        for j, (g, page) in enumerate(zip(groups, pages)):
            page.cells = g
            if kind in (N.TABLE_INTERIOR, N.INDEX_INTERIOR):
                page.right = promoted[j].left if j < len(promoted) else old_right
            pager.touch_node(page)
            if j < len(groups) - 1:
                if kind == N.TABLE_LEAF:
                    sep = Cell(left=page.pgno, key=g[-1].key)
                else:
                    sep = promoted[j].with_left(page.pgno)
                separators.append(sep)
        parent, idx = path[-1]
        if idx < len(parent.cells):
            parent.cells[idx] = parent.cells[idx].with_left(pages[-1].pgno)
            parent.cells[idx:idx] = separators
        else:
            parent.cells.extend(separators)
            parent.right = pages[-1].pgno
        pager.touch_node(parent)
        if not self._fits(parent):
            self._split(parent, path[:-1])

    # --- taking out pages that became empty -------------------------------------------------------------------------------------------------------
    def _after_child_removed(self, parent, ppath):
        """``parent`` lost a child. An interior page with no cells and no child is itself empty; with no cells and one child it is replaced by that child."""
        pager = self.pager
        if parent.cells:
            return
        if parent.right == 0:
            if not ppath:
                parent.kind = self.leaf_kind
                parent.cells = []
                pager.touch_node(parent)
            else:
                self._remove_node(parent, ppath)
            return
        child = self.node(parent.right)
        if not ppath:                                          # the root takes over the content of its only child
            if N.used_bytes(child, 100 if parent.pgno == 1 else 0) > pager.usable:
                return                                         # (the schema table's root has 100 bytes less room; leave the chain as it is)
            parent.kind, parent.cells, parent.right = child.kind, child.cells, child.right
            pager.free_page(child.pgno)
            pager.touch_node(parent)
        else:
            # a page that is not the root cannot be replaced by its only child (the leaves below it would end up one level higher than the others): it is merged with
            # a neighbour instead, the separator between the two moving down into the neighbour
            gp, gidx = ppath[-1]
            if gidx < len(gp.cells):
                sep = gp.cells[gidx]
                sib = self.node(gp.cells[gidx + 1].left if gidx + 1 < len(gp.cells) else gp.right)
                sib.cells.insert(0, sep.with_left(child.pgno))
                del gp.cells[gidx]
                sib_path = ppath[:-1] + [(gp, gidx)]
            elif gp.cells:
                sep = gp.cells.pop()
                sib = self.node(sep.left)
                sib.cells.append(sep.with_left(sib.right))
                sib.right = child.pgno
                gp.right = sib.pgno
                sib_path = ppath[:-1] + [(gp, len(gp.cells))]
            else:                                              # (an only child: nothing to merge with)
                return
            pager.free_page(parent.pgno)
            pager.touch_node(gp)
            pager.touch_node(sib)
            if not self._fits(sib):
                self._split(sib, sib_path)
            self._after_child_removed(gp, ppath[:-1])

    def _remove_node(self, node, path):
        """Take an empty non-root page out of the tree (table trees: its separator goes with it)."""
        parent, idx = path[-1]
        self.pager.free_page(node.pgno)
        if idx < len(parent.cells):
            del parent.cells[idx]
        elif parent.cells:
            parent.right = parent.cells.pop().left
        else:
            parent.right = 0
        self.pager.touch_node(parent)
        self._after_child_removed(parent, path[:-1])

    # --- walking the whole tree ------------------------------------------------------------------------------------------------------------------
    def pages(self):
        """Every page number used by the tree: B-tree pages and overflow pages (for the integrity check and for dropping)."""
        out = []
        stack = [self.root]
        while stack:
            pgno = stack.pop()
            n = self.node(pgno)
            out.append(pgno)
            for c in n.cells:
                if c.ovfl:
                    out.extend(self._overflow_pages(c))
                if not n.leaf:
                    stack.append(c.left)
            if not n.leaf:
                stack.append(n.right)
        return out

    def _overflow_pages(self, cell):
        out = []
        pgno = cell.ovfl
        remaining = cell.total - len(cell.payload)
        while pgno and remaining > 0:
            out.append(pgno)
            remaining -= self.pager.usable - 4
            pgno = struct.unpack_from(">I", self.pager.get_raw(pgno), 0)[0]
        return out

    def drop(self):
        """Give every page of the tree, the root too, back to the free list."""
        stack = [self.root]
        while stack:
            n = self.node(stack.pop())
            for c in n.cells:
                self._free_overflow(c)
                if not n.leaf:
                    stack.append(c.left)
            if not n.leaf:
                stack.append(n.right)
            self.pager.free_page(n.pgno)

    def clear(self):
        """Delete everything but keep the (now empty) root page."""
        root = self.node(self.root)
        stack = []
        for c in root.cells:
            self._free_overflow(c)
            if not root.leaf:
                stack.append(c.left)
        if not root.leaf:
            stack.append(root.right)
        while stack:
            n = self.node(stack.pop())
            for c in n.cells:
                self._free_overflow(c)
                if not n.leaf:
                    stack.append(c.left)
            if not n.leaf:
                stack.append(n.right)
            self.pager.free_page(n.pgno)
        root.kind, root.cells, root.right = self.leaf_kind, [], 0
        self.pager.touch_node(root)


class TableTree(_Tree):
    def __init__(self, pager, root):
        super().__init__(pager, root, False)

    @staticmethod
    def create(pager):
        """A new empty table tree; its root page number."""
        return pager.new_node(N.TABLE_LEAF).pgno

    def _find_leaf(self, rowid):
        path = []
        n = self.node(self.root)
        while n.kind == N.TABLE_INTERIOR:
            cells = n.cells
            lo, hi = 0, len(cells)
            while lo < hi:
                mid = (lo + hi) >> 1
                if cells[mid].key < rowid:
                    lo = mid + 1
                else:
                    hi = mid
            path.append((n, lo))
            n = self.node(cells[lo].left if lo < len(cells) else n.right)
        return n, path

    @staticmethod
    def _bisect(cells, rowid):
        lo, hi = 0, len(cells)
        while lo < hi:
            mid = (lo + hi) >> 1
            if cells[mid].key < rowid:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def get(self, rowid):
        leaf, _ = self._find_leaf(rowid)
        i = self._bisect(leaf.cells, rowid)
        if i < len(leaf.cells) and leaf.cells[i].key == rowid:
            return self.payload(leaf.cells[i])
        return None

    def insert(self, rowid, record):
        """Add a row, or replace the row with that rowid."""
        leaf, path = self._find_leaf(rowid)
        cells = leaf.cells
        i = self._bisect(cells, rowid)
        local, ovfl, total = self._store_payload(record)
        cell = Cell(key=rowid, payload=local, ovfl=ovfl, total=total)
        if i < len(cells) and cells[i].key == rowid:
            self._free_overflow(cells[i])
            cells[i] = cell
        else:
            cells.insert(i, cell)
        self.pager.touch_node(leaf)
        if not self._fits(leaf):
            self._split(leaf, path, append_hint=(i == len(cells) - 1 and all(idx >= len(p.cells) for p, idx in path)))

    def delete(self, rowid):
        leaf, path = self._find_leaf(rowid)
        i = self._bisect(leaf.cells, rowid)
        if i >= len(leaf.cells) or leaf.cells[i].key != rowid:
            return False
        self._free_overflow(leaf.cells[i])
        del leaf.cells[i]
        self.pager.touch_node(leaf)
        if not leaf.cells and path:
            self._remove_node(leaf, path)
        return True

    def max_rowid(self):
        n = self.node(self.root)
        while n.kind == N.TABLE_INTERIOR:
            n = self.node(n.right)
        return n.cells[-1].key if n.cells else 0

    def count(self):
        total = 0
        stack = [self.root]
        while stack:
            n = self.node(stack.pop())
            if n.leaf:
                total += len(n.cells)
            else:
                stack.extend(c.left for c in n.cells)
                stack.append(n.right)
        return total

    def scan(self, lo=None, hi=None):
        """``(rowid, record)`` in rowid order, for ``lo <= rowid <= hi`` (either bound may be left out)."""
        done = [False]
        yield from self._walk(self.node(self.root), lo, hi, done)

    def _walk(self, n, lo, hi, done):
        cells = n.cells
        if n.leaf:
            for i in range(self._bisect(cells, lo) if lo is not None else 0, len(cells)):
                c = cells[i]
                if hi is not None and c.key > hi:
                    done[0] = True
                    return
                yield c.key, self.payload(c)
            return
        start = self._bisect(cells, lo) if lo is not None else 0
        for i in range(start, len(cells)):
            yield from self._walk(self.node(cells[i].left), lo, hi, done)
            if done[0]:
                return
            if hi is not None and cells[i].key >= hi:
                done[0] = True
                return
        yield from self._walk(self.node(n.right), lo, hi, done)

    def scan_reverse(self, lo=None, hi=None):
        """Like ``scan`` but from the largest rowid down."""
        done = [False]
        yield from self._walk_reverse(self.node(self.root), lo, hi, done)

    def _walk_reverse(self, n, lo, hi, done):
        cells = n.cells
        if n.leaf:
            end = len(cells) if hi is None else self._bisect(cells, hi + 1)
            for i in range(end - 1, -1, -1):
                c = cells[i]
                if lo is not None and c.key < lo:
                    done[0] = True
                    return
                yield c.key, self.payload(c)
            return
        # children from the right-most to the left-most; a child under cell i holds keys <= cells[i].key and > cells[i-1].key
        end = len(cells) if hi is None else self._bisect(cells, hi)
        if end >= len(cells):
            yield from self._walk_reverse(self.node(n.right), lo, hi, done)
            if done[0]:
                return
            end = len(cells) - 1
        for i in range(min(end, len(cells) - 1), -1, -1):
            if lo is not None and cells[i].key < lo:
                done[0] = True
                return
            yield from self._walk_reverse(self.node(cells[i].left), lo, hi, done)
            if done[0]:
                return


class IndexTree(_Tree):
    """``compare(key, probe)`` gives -1, 0 or 1 for the decoded record ``key`` against a probe of the same or shorter length (only the probe's columns count)."""

    def __init__(self, pager, root, compare):
        super().__init__(pager, root, True)
        self.compare = compare

    @staticmethod
    def create(pager):
        return pager.new_node(N.INDEX_LEAF).pgno

    def _lower_bound(self, n, probe):
        """The first index whose key is not smaller than the probe."""
        cells = n.cells
        lo, hi = 0, len(cells)
        cmp = self.compare
        while lo < hi:
            mid = (lo + hi) >> 1
            if cmp(self.vals(cells[mid]), probe) < 0:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def _find(self, key):
        """``(node, index, path)`` of the exact key, or ``None``."""
        path = []
        n = self.node(self.root)
        while True:
            i = self._lower_bound(n, key)
            if i < len(n.cells) and self.compare(self.vals(n.cells[i]), key) == 0:
                return n, i, path
            if n.leaf:
                return None
            path.append((n, i))
            n = self.node(n.cells[i].left if i < len(n.cells) else n.right)

    def contains(self, probe):
        """Is there a key that starts with the probe?"""
        n = self.node(self.root)
        while True:
            i = self._lower_bound(n, probe)
            if i < len(n.cells) and self.compare(self.vals(n.cells[i]), probe) == 0:
                return True
            if n.leaf:
                return False
            n = self.node(n.cells[i].left if i < len(n.cells) else n.right)

    def insert(self, key, record):
        """Add the key (the decoded ``key`` list, and its encoded ``record``). A key that is already there is left alone."""
        path = []
        n = self.node(self.root)
        while not n.leaf:
            i = self._lower_bound(n, key)
            if i < len(n.cells) and self.compare(self.vals(n.cells[i]), key) == 0:
                return False
            path.append((n, i))
            n = self.node(n.cells[i].left if i < len(n.cells) else n.right)
        i = self._lower_bound(n, key)
        if i < len(n.cells) and self.compare(self.vals(n.cells[i]), key) == 0:
            return False
        local, ovfl, total = self._store_payload(record)
        cell = Cell(payload=local, ovfl=ovfl, total=total)
        cell.vals = list(key)
        n.cells.insert(i, cell)
        self.pager.touch_node(n)
        if not self._fits(n):
            self._split(n, path)
        return True

    def delete(self, key):
        found = self._find(key)
        if found is None:
            return False
        n, i, path = found
        cell = n.cells[i]
        if n.leaf:
            self._free_overflow(cell)
            del n.cells[i]
            self.pager.touch_node(n)
            if not n.cells and path:
                self._empty_leaf(n, path)
            return True
        # an interior key is replaced by its predecessor, the largest key of the subtree on its left
        lpath = path + [(n, i)]
        leaf = self.node(cell.left)
        while not leaf.leaf:
            lpath.append((leaf, len(leaf.cells)))
            leaf = self.node(leaf.right)
        pred = leaf.cells.pop()
        self._free_overflow(cell)
        n.cells[i] = Cell(left=cell.left, payload=pred.payload, ovfl=pred.ovfl, total=pred.total)
        n.cells[i].vals = pred.vals
        self.pager.touch_node(n)
        self.pager.touch_node(leaf)
        if not leaf.cells:
            self._empty_leaf(leaf, lpath)
        return True

    def _empty_leaf(self, leaf, path):
        """A leaf ran empty. Its parent's separator key cannot just go away (it is a real key), so it moves down into a neighbouring leaf, and the empty page is freed."""
        pager = self.pager
        parent, idx = path[-1]
        if idx < len(parent.cells):
            sep = parent.cells[idx]
            sib = self.node(parent.cells[idx + 1].left if idx + 1 < len(parent.cells) else parent.right)
            down = Cell(payload=sep.payload, ovfl=sep.ovfl, total=sep.total)
            down.vals = sep.vals
            sib.cells.insert(0, down)
            del parent.cells[idx]
            sib_path = path[:-1] + [(parent, idx)]
        else:
            if not parent.cells:                               # the leaf is the only child of its parent: nothing to merge with
                pager.free_page(leaf.pgno)
                parent.right = 0
                pager.touch_node(parent)
                self._after_child_removed(parent, path[:-1])
                return
            sep = parent.cells.pop()
            sib = self.node(sep.left)
            down = Cell(payload=sep.payload, ovfl=sep.ovfl, total=sep.total)
            down.vals = sep.vals
            sib.cells.append(down)
            parent.right = sib.pgno
            sib_path = path[:-1] + [(parent, len(parent.cells))]
        pager.free_page(leaf.pgno)
        pager.touch_node(parent)
        pager.touch_node(sib)
        if not self._fits(sib):
            self._split(sib, sib_path)
        self._after_child_removed(parent, path[:-1])

    def scan(self, lo=None, hi=None, lo_inclusive=True, hi_inclusive=True):
        """The decoded keys in order, from the first one that is ``>= lo`` (or ``> lo``) to the last one that is ``<= hi`` (or ``< hi``); lo and hi are probes (prefixes)."""
        done = [False]
        yield from self._walk(self.node(self.root), lo, hi, lo_inclusive, hi_inclusive, done)

    def _walk(self, n, lo, hi, lo_inc, hi_inc, done):
        cells = n.cells
        cmp = self.compare
        start = 0
        if lo is not None:
            lo_, hi_ = 0, len(cells)
            while lo_ < hi_:
                mid = (lo_ + hi_) >> 1
                r = cmp(self.vals(cells[mid]), lo)
                if r < 0 or (r == 0 and not lo_inc):
                    lo_ = mid + 1
                else:
                    hi_ = mid
            start = lo_
        leaf = n.leaf
        for i in range(start, len(cells)):
            if not leaf:
                yield from self._walk(self.node(cells[i].left), lo, hi, lo_inc, hi_inc, done)
                if done[0]:
                    return
            v = self.vals(cells[i])
            if hi is not None:
                r = cmp(v, hi)
                if r > 0 or (r == 0 and not hi_inc):
                    done[0] = True
                    return
            yield v
        if not leaf:
            yield from self._walk(self.node(n.right), lo, hi, lo_inc, hi_inc, done)

    def count(self):
        return sum(1 for _ in self.scan())
