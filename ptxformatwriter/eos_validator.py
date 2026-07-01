"""Pre-write end-of-stream validator for Pro Tools ``.ptx`` session bodies.

Pro Tools reads a session as a tree of *size-bounded* objects. When an object's
reader consumes MORE bytes than the object's own declared ``size``, or a
count-driven inner walk runs a length field onto unrelated bytes, the reader
raises:

    "Could not complete your request because end of stream encountered."

Our own block reader is *size-driven*: it walks blocks by their declared size and
skips any leftover, so it accepts files Pro Tools rejects. This module instead
reproduces the *count-driven* read of the objects whose grammar carries an internal
element count, so we can catch that failure BEFORE writing.

``simulate(data, blocks)`` returns ``None`` when the session would read to a clean
EOF, or a dict pinpointing the object (content-type + file offset) and the field
that overruns — the object an add/remove/duplicate edit failed to keep in sync.

WIRE FORMAT (little-endian). Every object begins with a 9-byte header:

    +0  u8   marker == 0x5A
    +1  u16  format_version
    +3  u32  size          (from +7 on; object spans [zmark, zmark + 7 + size))
    +7  u16  content_type
    +9  ...  payload

NAME TABLE (content_type 0x2519) — the add/remove/duplicate-track failing path.
Its payload is a version-keyed preamble (first entry at payload +0x14 for
format_version >= 8, else +0x16), then N inline name entries, then a run of
0x5A-framed children. Each name entry is::

    u32 name_length | name[name_length] | 23-byte trailer

with a ``0x0000002A`` (u32 == 42) marker at trailer offset +6. That fixed marker
lets the walk confirm each entry is framed correctly WITHOUT interpreting the
(possibly non-ASCII) name bytes — which is what keeps it sound across sessions. An
entry whose ``name_length`` runs the trailer past the object end, or whose trailer
marker is absent, is the exact end-of-stream fault: a track's name entry was left
out of sync (a stripped-but-not-removed name, or a trailer-less inserted entry).
"""

from __future__ import annotations

import struct


MARKER = 0x5A
CT_MASTER_INDEX = 0x0002
CT_NAME_TABLE = 0x2519          # inline name-entry list (the failing path)
CT_TRACK_LIST = 0x2624         # count-prefixed track-list container

NAME_ENTRY_TRAILER = 23        # bytes after the name string
TRAILER_MARK_OFF = 6           # offset of the 0x0000002A marker inside the trailer
TRAILER_MARK = 0x2A
ENTRY_START_V8PLUS = 0x14      # first name entry, format_version >= 8
ENTRY_START_V_LOW = 0x16       # first name entry, format_version < 8
ENTRY_COUNT_OFF = 0x0E         # u16 inline-entry count in the format_version >= 8 preamble


def _u16(b: bytes, o: int) -> int:
    return struct.unpack_from("<H", b, o)[0]


def _u32(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


class EndOfStream(Exception):
    """Raised when a read would advance past the enclosing object's declared end.
    Carries the offending object's content_type, file offset, field, and detail."""

    def __init__(self, content_type: int, offset: int, field: str, detail: str):
        self.content_type = content_type
        self.offset = offset
        self.field = field
        self.detail = detail
        super().__init__(str(self))

    def __str__(self) -> str:
        return (f"end of stream: object content_type=0x{self.content_type:04x} "
                f"at file offset {self.offset} (0x{self.offset:x}); "
                f"overran on {self.field}: {self.detail}")

    def as_dict(self) -> dict:
        return {"content_type": self.content_type, "offset": self.offset,
                "field": self.field, "detail": self.detail}


def _top_level_children(blocks, z: int, e: int):
    """Direct (one-level) framed children of the object at [z, e)."""
    inner = sorted((zz, ee, cc) for (zz, ee, cc) in blocks if z < zz < e)
    top = []
    for (zz, ee, cc) in inner:
        if not any(a < zz < b for (a, b, _) in top):
            top.append((zz, ee, cc))
    return top


def check_name_table(data: bytes, blocks, z: int, e: int) -> int:
    """Walk the inline name-entry list the way the reader does. Each entry is
    ``u32 name_length | name | 23-byte trailer`` with a 0x2A marker at trailer +6.
    Raises EndOfStream on the offending entry; returns the entry count otherwise."""
    fmt_ver = _u16(data, z + 1)
    payload = z + 9
    start = ENTRY_START_V8PLUS if fmt_ver >= 8 else ENTRY_START_V_LOW

    children = _top_level_children(blocks, z, e)
    entries_end = children[0][0] if children else e   # entries end at the first framed child

    q = payload + start
    idx = 0
    while q < entries_end:
        if q + 4 > e:
            raise EndOfStream(CT_NAME_TABLE, z, f"name[{idx}].length",
                              f"length field at 0x{q:x} would read past object end 0x{e:x}")
        name_length = _u32(data, q)
        entry_end = q + 4 + name_length + NAME_ENTRY_TRAILER
        if entry_end > e:
            raise EndOfStream(
                CT_NAME_TABLE, z, f"name[{idx}].name_length={name_length}",
                f"entry at 0x{q:x} with name_length={name_length} runs to 0x{entry_end:x}, "
                f"past object end 0x{e:x} (a track's name entry was not added/removed "
                f"to match the edit)")
        mark_at = q + 4 + name_length + TRAILER_MARK_OFF
        if _u32(data, mark_at) != TRAILER_MARK:
            raise EndOfStream(
                CT_NAME_TABLE, z, f"name[{idx}].trailer",
                f"entry at 0x{q:x} (name_length={name_length}) is misframed: expected trailer "
                f"marker 0x{TRAILER_MARK:08x} at 0x{mark_at:x}, found 0x{_u32(data, mark_at):08x} "
                f"(the name-entry list is out of sync with the track edit)")
        q = entry_end
        idx += 1

    # format_version >= 8 stores an explicit inline-entry count in the preamble; the reader
    # reads exactly that many entries. A stale count reads one entry too many (past the last
    # real entry, into the framed children) or stops short — an overrun either way.
    if fmt_ver >= 8:
        stored = _u16(data, payload + ENTRY_COUNT_OFF)
        if stored != idx:
            raise EndOfStream(
                CT_NAME_TABLE, z, "preamble.entry_count",
                f"preamble entry_count={stored} but {idx} inline entries are present "
                f"(a track's name entry was added/removed without updating the count)")
    return idx


def check_track_container(data: bytes, blocks, z: int, e: int) -> int:
    """The track-list payload begins with a u32 item_count followed by that many framed
    track objects. A count exceeding the framed children is a count-driven overrun."""
    payload = z + 9
    if payload + 4 > e:
        raise EndOfStream(CT_TRACK_LIST, z, "container.item_count",
                          "item_count field runs past object end")
    item_count = _u32(data, payload)
    children = _top_level_children(blocks, z, e)
    if item_count > len(children):
        raise EndOfStream(
            CT_TRACK_LIST, z, f"container.item_count={item_count}",
            f"item_count={item_count} exceeds the {len(children)} framed child objects the "
            f"container holds (a track was removed without decrementing the list count)")
    return item_count


def simulate(data: bytes, blocks, index_start: "int | None" = None) -> "dict | None":
    """Reproduce the reader's count-driven walk over the object set. `blocks` is the
    authoritative block set as ``(start, end, content_type)`` tuples (the size-driven
    walk). Returns ``None`` for a clean EOF, else a dict ``{content_type, offset, field,
    detail}`` naming the object and field where the reader hits end of stream."""
    ordered = sorted(blocks)
    try:
        for (z, e, ct) in ordered:
            if ct == CT_NAME_TABLE:
                check_name_table(data, ordered, z, e)
            elif ct == CT_TRACK_LIST:
                check_track_container(data, ordered, z, e)
    except EndOfStream as eos:
        return eos.as_dict()
    return None
