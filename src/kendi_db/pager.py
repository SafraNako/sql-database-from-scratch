"""The pager: the file as an array of pages, a cache of them, the free-page list, and atomic transactions with a rollback journal in SQLite's own format.

Changes of a transaction stay in memory until the commit (nothing is written to the database file before it, so a rollback is just forgetting them). The commit then
does what SQLite's rollback-journal mode does:

1. write the *original* content of every page that is about to change to ``<file>-journal`` (with a checksum for each) and make it durable (fsync);
2. write the new pages into the database file and make that durable;
3. delete the journal. This is the moment the transaction exists: a crash before it leaves a "hot" journal that the next open uses to put the old pages back.

So at every instant the database is either entirely the old state or entirely the new one. The journal is the real SQLite format, so SQLite can roll back our interrupted
commits and we can roll back its.
"""
from __future__ import annotations

import os
import struct

from . import node as N
from .errors import CorruptDatabase, OperationalError, SimulatedCrash

try:
    import fcntl
except ImportError:                                                       # pragma: no cover  (not POSIX)
    fcntl = None

MAGIC = b"SQLite format 3\x00"
JOURNAL_MAGIC = bytes.fromhex("d9d505f920a163d7")
SECTOR = 512
PENDING_BYTE = 0x40000000
VERSION_NUMBER = 3045000                                                  # what the header says about the "library" that last wrote the file

# offsets in the 100-byte file header
OFF_PAGE_SIZE, OFF_CHANGE_COUNTER, OFF_DB_SIZE, OFF_FREELIST_TRUNK, OFF_FREELIST_COUNT, OFF_SCHEMA_COOKIE, OFF_SCHEMA_FORMAT = 16, 24, 28, 32, 36, 40, 44
OFF_ENCODING, OFF_USER_VERSION, OFF_APPLICATION_ID, OFF_VALID_FOR, OFF_VERSION = 56, 60, 68, 92, 96


def new_header(page_size):
    h = bytearray(100)
    h[0:16] = MAGIC
    struct.pack_into(">H", h, OFF_PAGE_SIZE, 1 if page_size == 65536 else page_size)
    h[18] = 1                                                             # file format versions: legacy (rollback journal)
    h[19] = 1
    h[20] = 0                                                             # reserved bytes at the end of each page
    h[21], h[22], h[23] = 64, 32, 32                                      # payload fractions: fixed by the format
    struct.pack_into(">I", h, OFF_DB_SIZE, 1)
    struct.pack_into(">I", h, OFF_SCHEMA_FORMAT, 4)
    struct.pack_into(">I", h, OFF_ENCODING, 1)                            # UTF-8
    struct.pack_into(">I", h, OFF_VERSION, VERSION_NUMBER)
    return h


def checksum(nonce, data, page_size):
    """The journal's checksum of a page: the nonce plus every 200th byte, counting back from the end."""
    total = nonce
    i = page_size - 200
    while i > 0:
        total += data[i]
        i -= 200
    return total & 0xFFFFFFFF


class Pager:
    def __init__(self, path, page_size=4096):
        if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
            raise ValueError("the page size must be a power of two between 512 and 65536")
        self.path = path
        self.journal_path = path + "-journal"
        self.crash_at = None                                              # tests: stop the commit at a named point
        existing = os.path.exists(path) and os.path.getsize(path) > 0
        try:
            self.file = open(path, "r+b" if os.path.exists(path) else "w+b")
        except OSError as exc:
            raise OperationalError("unable to open database file: %s" % exc.strerror) from None
        self.nodes = {}                                                   # pgno -> Node (the working copy of a B-tree page)
        self.raw = {}                                                     # pgno -> bytearray (overflow and free-list pages)
        self.dirty = set()
        self.in_txn = False
        self.read_depth = 0
        self.orig_db_size = 0
        self.locked = False
        self._sp = None                                                   # the open statement savepoint, if any
        if existing:
            self._acquire(True)
            try:
                self._recover_journal_if_hot()
                self._read_header()
            finally:
                self._release()
        else:
            self.page_size = page_size
            self.usable = page_size
            self.header = new_header(page_size)
            self.db_size = 0

    # --- the file ------------------------------------------------------------------------------------------------------------------------------
    # --- locks: shared while reading, exclusive while writing; none between statements, so that other processes (SQLite included) can use the file ---------------
    def _acquire(self, exclusive):
        if fcntl is None:
            return
        try:
            fcntl.flock(self.file, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except OSError:
            raise OperationalError("database is locked") from None
        self.locked = True

    def _release(self):
        if fcntl is not None and self.locked:
            fcntl.flock(self.file, fcntl.LOCK_UN)
        self.locked = False

    def _refresh(self):
        """With a lock held: finish what an interrupted writer left, and drop the cache if somebody else changed the file since we last looked."""
        if os.path.exists(self.journal_path) and os.path.getsize(self.journal_path) > 0:
            exclusive = self.in_txn
            if not exclusive:
                self._release()
                self._acquire(True)
            self._recover_journal_if_hot()
            self.nodes.clear()
            self.raw.clear()
            if not exclusive:
                self._release()
                self._acquire(False)
        size = os.path.getsize(self.path)
        if size == 0:
            if self.db_size != 0 and not self.dirty:
                self.nodes.clear()
                self.raw.clear()
                self.page_size = self.page_size
                self.header = new_header(self.page_size)
                self.db_size = 0
            return
        self.file.seek(0)
        h = self.file.read(100)
        if self.db_size == 0 or len(h) < 100 or struct.unpack_from(">I", h, OFF_CHANGE_COUNTER)[0] != struct.unpack_from(">I", self.header, OFF_CHANGE_COUNTER)[0] \
                or h[:100] != bytes(self.header[:100]):
            self.nodes.clear()
            self.raw.clear()
            self._read_header()

    def begin_read(self):
        """Start looking at the database: a shared lock (several readers are fine) and a fresh cache if the file changed."""
        if self.read_depth == 0 and not self.in_txn:
            self._acquire(False)
            self._refresh()
        self.read_depth += 1

    def end_read(self):
        self.read_depth -= 1
        if self.read_depth == 0 and not self.in_txn:
            self._release()

    def close(self):
        if self.in_txn:
            self.rollback()
        if not self.file.closed:
            self.file.close()

    def _read_header(self):
        self.file.seek(0)
        h = self.file.read(100)
        if len(h) < 100 or h[:16] != MAGIC:
            raise CorruptDatabase("file is not a database")
        ps = struct.unpack_from(">H", h, OFF_PAGE_SIZE)[0]
        self.page_size = 65536 if ps == 1 else ps
        if self.page_size < 512 or self.page_size & (self.page_size - 1):
            raise CorruptDatabase("bad page size %d" % self.page_size)
        self.usable = self.page_size - h[20]
        if h[56:60] != b"\x00\x00\x00\x01":
            raise CorruptDatabase("only UTF-8 databases are supported")
        if h[18] > 2 or h[19] > 2:
            raise CorruptDatabase("unknown file format version")
        if h[18] == 2 or h[19] == 2:
            raise CorruptDatabase("databases in WAL mode are not supported")
        self.header = bytearray(h)
        size = os.path.getsize(self.path)
        in_header = struct.unpack_from(">I", h, OFF_DB_SIZE)[0]
        valid_for = struct.unpack_from(">I", h, OFF_VALID_FOR)[0]
        counter = struct.unpack_from(">I", h, OFF_CHANGE_COUNTER)[0]
        self.db_size = in_header if (in_header and valid_for == counter) else size // self.page_size

    def _read_page_from_file(self, pgno):
        if pgno < 1 or pgno > self.db_size:
            raise CorruptDatabase("page %d is outside the database (%d pages)" % (pgno, self.db_size))
        self.file.seek((pgno - 1) * self.page_size)
        data = self.file.read(self.page_size)
        if len(data) < self.page_size:
            raise CorruptDatabase("page %d is cut short" % pgno)
        return bytearray(data)

    # --- header fields -------------------------------------------------------------------------------------------------------------------------
    def get_u32(self, off):
        return struct.unpack_from(">I", self.header, off)[0]

    def set_u32(self, off, value):
        self._touch_header()
        struct.pack_into(">I", self.header, off, value & 0xFFFFFFFF)

    def _touch_header(self):
        if not self.in_txn:
            raise OperationalError("internal error: change outside a transaction")
        self.dirty.add(1)

    # --- reading and changing pages ------------------------------------------------------------------------------------------------------------
    def get_node(self, pgno):
        n = self.nodes.get(pgno)
        if n is None:
            raw = self.raw.get(pgno) or self._read_page_from_file(pgno)
            n = N.parse_node(pgno, raw, self.usable, 100 if pgno == 1 else 0)
            self.nodes[pgno] = n
        sp = self._sp
        if sp is not None and pgno not in sp["nodes"] and pgno not in sp["new"]:
            sp["nodes"][pgno] = (n, n.kind, list(n.cells), n.right)          # everything that changes a page gets it from here first, so this is its state before
        return n

    def get_raw(self, pgno):
        """A page that is not a B-tree page (overflow, free list). Call ``touch(pgno)`` before changing it."""
        r = self.raw.get(pgno)
        if r is None:
            r = self.raw[pgno] = self._read_page_from_file(pgno)
        sp = self._sp
        if sp is not None and pgno not in sp["raw"] and pgno not in sp["new"]:
            sp["raw"][pgno] = bytes(r)
        return r

    # --- undoing one statement inside a transaction ----------------------------------------------------------------------------------------------
    def savepoint(self):
        """Start remembering what the pages looked like, so that one statement can be undone without ending the transaction."""
        self._sp = {"nodes": {}, "raw": {}, "new": set(), "header": bytes(self.header), "dirty": set(self.dirty), "db_size": self.db_size}

    def release_savepoint(self):
        self._sp = None

    def rollback_savepoint(self):
        sp, self._sp = self._sp, None
        for pgno in sp["new"]:                                             # (a page given out again may also have a saved state: put that back afterwards)
            self.nodes.pop(pgno, None)
            self.raw.pop(pgno, None)
        for pgno, (n, kind, cells, right) in sp["nodes"].items():
            n.kind, n.cells, n.right = kind, cells, right
            self.nodes[pgno] = n
            self.raw.pop(pgno, None)
        for pgno, data in sp["raw"].items():
            self.raw[pgno] = bytearray(data)
            self.nodes.pop(pgno, None)
        self.header[:] = sp["header"]
        self.dirty = set(sp["dirty"])
        self.db_size = sp["db_size"]

    def touch(self, pgno):
        self._touch_header()
        self.dirty.add(pgno)

    def touch_node(self, node):
        self.touch(node.pgno)

    def new_node(self, kind):
        pgno = self.allocate()
        node = N.Node(pgno, kind)
        self.nodes[pgno] = node
        self.raw.pop(pgno, None)
        self.dirty.add(pgno)
        if self._sp is not None:
            self._sp["new"].add(pgno)
        return node

    def new_raw(self):
        pgno = self.allocate()
        self.nodes.pop(pgno, None)
        r = self.raw[pgno] = bytearray(self.page_size)
        self.dirty.add(pgno)
        if self._sp is not None:
            self._sp["new"].add(pgno)
        return pgno, r

    # --- allocation and the free list -----------------------------------------------------------------------------------------------------------
    def allocate(self):
        """A page number for a new page: a page from the free list if there is one, else a new page at the end of the file."""
        self._touch_header()
        trunk = self.get_u32(OFF_FREELIST_TRUNK)
        if trunk:
            t = self.get_raw(trunk)
            leaves = struct.unpack_from(">I", t, 4)[0]
            self.touch(trunk)
            self.set_u32(OFF_FREELIST_COUNT, self.get_u32(OFF_FREELIST_COUNT) - 1)
            if leaves:
                pgno = struct.unpack_from(">I", t, 8 + 4 * (leaves - 1))[0]
                struct.pack_into(">I", t, 4, leaves - 1)
                self.nodes.pop(pgno, None)
                self.raw.pop(pgno, None)
                return pgno
            self.set_u32(OFF_FREELIST_TRUNK, struct.unpack_from(">I", t, 0)[0])    # the trunk itself is the page to hand out
            self.raw.pop(trunk, None)
            self.nodes.pop(trunk, None)
            return trunk
        self.db_size += 1
        if self.db_size * self.page_size - self.page_size == PENDING_BYTE:
            self.db_size += 1                                             # the page that holds the lock byte is never used
        return self.db_size

    def free_page(self, pgno):
        self._touch_header()
        self.nodes.pop(pgno, None)
        self.raw.pop(pgno, None)
        self.dirty.discard(pgno)
        trunk = self.get_u32(OFF_FREELIST_TRUNK)
        capacity = (self.usable // 4) - 2
        if trunk:
            t = self.get_raw(trunk)
            leaves = struct.unpack_from(">I", t, 4)[0]
            if leaves < capacity:
                self.touch(trunk)
                struct.pack_into(">I", t, 8 + 4 * leaves, pgno)
                struct.pack_into(">I", t, 4, leaves + 1)
                self.set_u32(OFF_FREELIST_COUNT, self.get_u32(OFF_FREELIST_COUNT) + 1)
                return
        # the freed page becomes the new first trunk of the free list
        r = self.raw[pgno] = bytearray(self.page_size)
        struct.pack_into(">I", r, 0, trunk)
        self.dirty.add(pgno)
        self.set_u32(OFF_FREELIST_TRUNK, pgno)
        self.set_u32(OFF_FREELIST_COUNT, self.get_u32(OFF_FREELIST_COUNT) + 1)

    def freelist_pages(self):
        """Every page of the free list (trunks and leaves), for the integrity check."""
        out = []
        trunk = self.get_u32(OFF_FREELIST_TRUNK)
        seen = set()
        while trunk:
            if trunk in seen:
                raise CorruptDatabase("the free list has a loop")
            seen.add(trunk)
            out.append(trunk)
            t = self.get_raw(trunk)
            n = struct.unpack_from(">I", t, 4)[0]
            out.extend(struct.unpack_from(">I", t, 8 + 4 * i)[0] for i in range(n))
            trunk = struct.unpack_from(">I", t, 0)[0]
        return out

    # --- transactions ---------------------------------------------------------------------------------------------------------------------------
    def begin(self):
        if self.in_txn:
            raise OperationalError("cannot start a transaction within a transaction")
        if self.read_depth:
            self._release()
        self._acquire(True)
        try:
            self._refresh()
        except BaseException:
            self._release()
            raise
        self.in_txn = True
        self.orig_db_size = self.db_size
        if self.db_size == 0:                                             # a new database gets its first page, the root of the schema table
            self.db_size = 1
            self.nodes[1] = N.Node(1, N.TABLE_LEAF)
            self.dirty.add(1)

    def rollback(self):
        if not self.in_txn:
            return
        self.nodes.clear()
        self.raw.clear()
        self.dirty.clear()
        self.in_txn = False
        self.db_size = self.orig_db_size
        if self.orig_db_size == 0:
            self.header = new_header(self.page_size)
        else:
            self._read_header()
        self._release()
        if self.read_depth:
            self._acquire(False)

    def _crash(self, point):
        if self.crash_at == point:
            raise SimulatedCrash(point)

    def commit(self):
        if not self.in_txn:
            return
        if not self.dirty:
            self.in_txn = False
            self._release()
            if self.read_depth:
                self._acquire(False)
            return
        # the header: a new change counter, the size, and the same number to say that the counter is valid
        counter = (self.get_u32(OFF_CHANGE_COUNTER) + 1) & 0xFFFFFFFF
        struct.pack_into(">I", self.header, OFF_CHANGE_COUNTER, counter)
        struct.pack_into(">I", self.header, OFF_DB_SIZE, self.db_size)
        struct.pack_into(">I", self.header, OFF_VALID_FOR, counter)
        struct.pack_into(">I", self.header, OFF_VERSION, VERSION_NUMBER)
        self.dirty.add(1)
        images = {}
        for pgno in self.dirty:
            if pgno in self.nodes:
                data = N.serialize_node(self.nodes[pgno], self.page_size, self.usable, 100 if pgno == 1 else 0)
            elif pgno in self.raw:
                data = self.raw[pgno]
            elif pgno == 1:                                                  # only the file header changed: the rest of the page is as it was
                data = bytearray(self._read_page_from_file(1))
            else:
                continue
            if pgno == 1:
                data[0:100] = self.header
            images[pgno] = bytes(data)
        if 1 not in images:                                               # (cannot happen: the header is always dirty here)
            raise CorruptDatabase("internal error: no page 1 at commit")
        originals = sorted(p for p in images if p <= self.orig_db_size)
        if originals:
            self._write_journal(originals)
        self._crash("journal_written")
        written = 0
        for pgno in sorted(images):
            self.file.seek((pgno - 1) * self.page_size)
            self.file.write(images[pgno])
            written += 1
            self._crash("after_page_%d" % written)
        if os.path.getsize(self.path) < self.db_size * self.page_size and self.file.seek(0, 2) < self.db_size * self.page_size:
            self.file.truncate(self.db_size * self.page_size)             # pages that were allocated and given back again are never written: they are zeros
        self.file.flush()
        os.fsync(self.file.fileno())
        self._crash("database_synced")
        if os.path.exists(self.journal_path):
            os.unlink(self.journal_path)                                  # the commit point
            self._sync_directory()
        self.dirty.clear()
        self.in_txn = False
        self._release()
        if self.read_depth:
            self._acquire(False)

    def _sync_directory(self):
        try:
            fd = os.open(os.path.dirname(os.path.abspath(self.path)), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    # --- the journal ------------------------------------------------------------------------------------------------------------------------------
    def _write_journal(self, pgnos):
        nonce = struct.unpack(">I", os.urandom(4))[0]
        header = bytearray(SECTOR)
        header[0:8] = JOURNAL_MAGIC
        struct.pack_into(">IIIII", header, 8, len(pgnos), nonce, self.orig_db_size, SECTOR, self.page_size)
        with open(self.journal_path, "wb") as j:
            j.write(header)
            for pgno in pgnos:
                if pgno == 1:
                    data = bytearray(self._read_page_from_file(1))        # the original page, as it is on disk
                else:
                    data = self._read_page_from_file(pgno)
                j.write(struct.pack(">I", pgno))
                j.write(data)
                j.write(struct.pack(">I", checksum(nonce, data, self.page_size)))
            j.flush()
            os.fsync(j.fileno())
        self._sync_directory()

    def _recover_journal_if_hot(self):
        """If a commit was interrupted, put the old pages back: that is what makes the commit atomic."""
        if not os.path.exists(self.journal_path):
            return
        with open(self.journal_path, "rb") as j:
            data = j.read()
        if len(data) < 28 or data[:8] != JOURNAL_MAGIC:                   # empty or zeroed header: the commit had finished
            os.unlink(self.journal_path)
            return
        nrec, nonce, orig_size, sector, page_size = struct.unpack_from(">IIIII", data, 8)
        if page_size < 512 or sector < 28:
            os.unlink(self.journal_path)
            return
        record = 4 + page_size + 4
        if nrec == 0xFFFFFFFF or nrec == 0:
            nrec = (len(data) - sector) // record
        pos = sector
        for _ in range(nrec):
            if pos + record > len(data):
                break
            pgno = struct.unpack_from(">I", data, pos)[0]
            page = data[pos + 4:pos + 4 + page_size]
            if struct.unpack_from(">I", data, pos + 4 + page_size)[0] != checksum(nonce, page, page_size) or pgno == 0:
                break                                                     # a torn record: it and what follows were never needed
            self.file.seek((pgno - 1) * page_size)
            self.file.write(page)
            pos += record
        self.file.truncate(orig_size * page_size)
        self.file.flush()
        os.fsync(self.file.fileno())
        os.unlink(self.journal_path)
        self._sync_directory()
