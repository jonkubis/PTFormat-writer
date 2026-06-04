"""Deterministic (re)builder for the 0x0002 master index — "Pass 2".

The final block (content_type 0x0002) is the session's master pointer table: it
references every indexed block by absolute file offset. Editing the body shifts
those offsets. The historical approach (`writer._update_final_index`) tries to
*guess* which 4-byte values are offsets and rewrite them, which is undecidable
(a scalar can equal a block offset) and corrupts files.

This module rebuilds offsets *deterministically* from the block layout. Every
offset hole's target is identified by logical identity — the content_type of the
block it points at, and that block's rank among blocks of the same type in
file-offset order — never by guessing from the stored value. See
`docs/final-index-0x0002-schema.md`.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

from . import writer as W


@dataclass
class ChildRef:
    """A container record's back-reference to a child block.

    Layout (11 bytes): <u16 child_type><u32 offset><2 flag bytes><3 zero>.
    `offset` is an offset hole at record-relative position `rel + 2`.
    """

    rel: int
    child_type: int
    offset: int
    flags: bytes


@dataclass
class Element:
    """A marker/offset-table element.

    Layout: <0x01><4*k><0x00><u16 k><k * u32 offset><6 zero>, total 4*k + 11.
    The familiar marker `01 04 00 01 00 <off> ...` is just k == 1. Each of the
    `k` offsets is a hole at record-relative position `rel + 5 + 4*i`.
    """

    rel: int
    tag1: int
    offsets: list[int] = field(default_factory=list)


@dataclass
class IndexRecord:
    """One record in the 0x0002 stream, fully framed by the grammar parser."""

    start: int          # offset within the final block (ZMARK-relative)
    end: int
    count: int          # the +0 field (per-type constant)
    content_type: int
    flag: int
    ordinal: int
    child_refs: list[ChildRef] = field(default_factory=list)
    elements: list[Element] = field(default_factory=list)


def _u16(data: bytes, pos: int) -> int:
    return int.from_bytes(data[pos : pos + 2], "little")


def _u32(data: bytes, pos: int) -> int:
    return int.from_bytes(data[pos : pos + 4], "little")


def parse_records(
    final_block: bytes,
    known_types: set[int],
    zmarks: set[int],
) -> list[IndexRecord]:
    """Forward-parse the record stream by grammar (see module docstring).

    This frames every record exactly — it tiles the record region [13, end) with
    zero leftover and yields exactly `record_count` records (verified across all
    stereo controls, where that count == 2N+159). Unlike the legacy
    `writer._final_index_records`, it does not pattern-scan for record starts and
    so never misframes container/child-ref records.
    """
    if (
        len(final_block) < 13
        or final_block[0] != 0x5A
        or _u16(final_block, 1) != 0x0002
        or _u16(final_block, 7) != 0x0002
    ):
        return []

    def is_child_ref(p: int) -> bool:
        # The offset-is-a-ZMARK test is the discriminator vs. entry_count, whose
        # trailing bytes form a huge non-offset u32. The 3-zero tail guards it.
        return (
            _u16(final_block, p) in known_types
            and _u32(final_block, p + 2) in zmarks
            and final_block[p + 8 : p + 11] == b"\x00\x00\x00"
        )

    end = len(final_block)
    records: list[IndexRecord] = []
    pos = 13
    while pos < end:
        if pos + 15 > end or final_block[pos + 6 : pos + 10] != b"\xff\xff\xff\xff":
            break  # trailing padding / not a record start
        start = pos
        count = _u32(final_block, pos)
        content_type = _u16(final_block, pos + 4)
        flag = final_block[pos + 10]
        ordinal = _u32(final_block, pos + 11)
        p = pos + 15
        child_refs: list[ChildRef] = []
        while is_child_ref(p):
            child_refs.append(
                ChildRef(
                    rel=p - start,
                    child_type=_u16(final_block, p),
                    offset=_u32(final_block, p + 2),
                    flags=bytes(final_block[p + 6 : p + 8]),
                )
            )
            p += 11
        entry_count = _u32(final_block, p)
        p += 4
        elements: list[Element] = []
        for _ in range(entry_count):
            if final_block[p] != 0x01 or final_block[p + 2] != 0x00:
                raise ValueError(
                    f"bad element tag at {p}: {final_block[p:p+5].hex()} "
                    f"(record type {content_type:#x} start {start})"
                )
            k = _u16(final_block, p + 3)
            offsets = [_u32(final_block, p + 5 + 4 * i) for i in range(k)]
            elements.append(Element(rel=p - start, tag1=final_block[p + 1], offsets=offsets))
            p += 4 * k + 11
        records.append(
            IndexRecord(
                start=start,
                end=p,
                count=count,
                content_type=content_type,
                flag=flag,
                ordinal=ordinal,
                child_refs=child_refs,
                elements=elements,
            )
        )
        pos = p
    return records


def serialize_record(record: IndexRecord) -> bytes:
    """Emit one record's bytes from the dataclass (inverse of the parser)."""
    out = bytearray()
    out += record.count.to_bytes(4, "little")
    out += record.content_type.to_bytes(2, "little")
    out += b"\xff\xff\xff\xff"
    out += bytes([record.flag])
    out += record.ordinal.to_bytes(4, "little")
    for child in record.child_refs:
        out += child.child_type.to_bytes(2, "little")
        out += child.offset.to_bytes(4, "little")
        out += child.flags
        out += b"\x00\x00\x00"
    out += len(record.elements).to_bytes(4, "little")
    for element in record.elements:
        out += bytes([0x01, element.tag1, 0x00])
        out += len(element.offsets).to_bytes(2, "little")
        for offset in element.offsets:
            out += offset.to_bytes(4, "little")
        out += b"\x00\x00\x00\x00\x00\x00"
    return bytes(out)


def serialize_final_block(records: list[IndexRecord]) -> bytes:
    """Emit a complete 0x0002 block (ZMARK header + records) from records."""
    stream = b"".join(serialize_record(record) for record in records)
    content = (
        (0x0002).to_bytes(2, "little")      # content_type
        + len(records).to_bytes(4, "little")  # record_count
        + stream
    )
    return (
        bytes([0x5A])
        + (0x0002).to_bytes(2, "little")    # block_type
        + len(content).to_bytes(4, "little")  # block_size
        + content
    )


def replace_final_index(data: bytes, records: list[IndexRecord]) -> bytes:
    """Return `data` with its trailing 0x0002 block replaced by `records`."""
    ref = final_index_ref(data)
    if ref is None:
        return data
    return data[: ref.start] + serialize_final_block(records)


# --- record synthesis (Pass 2, step 1: emit the per-track records) -----------
#
# Adding one empty stereo track to the index is a fixed edit, derived from the
# n -> n+1 deltas across the control set. All offsets in newly-added/grown holes
# are left 0 (placeholders); fill them from the final layout afterwards.

def _only(records: list[IndexRecord], predicate) -> IndexRecord:
    matches = [r for r in records if predicate(r)]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one matching record, found {len(matches)}")
    return matches[0]


def _is_lane_instance(record: IndexRecord) -> bool:
    # per-track 0x251a sidecar instance
    return (
        record.content_type == 0x2519
        and record.count == 2
        and record.flag == 1
        and len(record.child_refs) == 1
        and record.child_refs[0].child_type == 0x251A
    )


def _is_playlist_instance(record: IndexRecord) -> bool:
    return record.content_type == 0x2624 and record.count == 4


def _blank_clone(template: IndexRecord, ordinal: int) -> IndexRecord:
    clone = copy.deepcopy(template)
    clone.ordinal = ordinal
    for child in clone.child_refs:
        child.offset = 0
    for element in clone.elements:
        element.offsets = [0] * len(element.offsets)
    return clone


def add_track(records: list[IndexRecord], new_count: int, channels: int = 2) -> list[IndexRecord]:
    """Transform the record list to add one audio track (resulting in `new_count`
    tracks). `channels` = audio channels for the new track (2=stereo, 1=mono);
    it drives how many markers the 0x1054 channel-container record gains (the
    0x1054 markers reference the per-track 0x1052 audio lanes, one per channel).
    Returns a new list; `records` is not mutated."""
    out = copy.deepcopy(records)

    def append_marker(record: IndexRecord, times: int = 1) -> None:
        for _ in range(times):
            record.elements.append(Element(rel=0, tag1=0x04, offsets=[0]))

    append_marker(_only(out, lambda r: r.content_type == 0x1015 and r.count == 1 and not r.child_refs))
    append_marker(_only(out, lambda r: r.content_type == 0x1054 and r.count == 1 and not r.child_refs), channels)
    append_marker(_only(out, lambda r: r.content_type == 0x2519 and r.count == 1 and not r.child_refs))
    append_marker(_only(out, lambda r: r.content_type == 0x2624 and r.count == 1 and not r.child_refs))

    # packed-offset table: its single element gains one offset (tag1 == 4*k)
    table = _only(out, lambda r: r.content_type == 0x2519 and r.count == 2 and r.flag == 0)
    table.elements[0].offsets.append(0)
    table.elements[0].tag1 = 4 * len(table.elements[0].offsets)

    # insert the new per-track 0x251a instance after the existing ones
    lane_positions = [i for i, r in enumerate(out) if _is_lane_instance(r)]
    out.insert(lane_positions[-1] + 1, _blank_clone(out[lane_positions[-1]], new_count))

    # the fixed 0x251b/0x251c/0x2716 records carry ordinal == track count
    for record in out:
        if record.content_type == 0x2519 and record.child_refs and record.child_refs[0].child_type in (0x251B, 0x2716):
            record.ordinal = new_count

    # insert the new per-track 0x2624 instance after the existing ones
    playlist_positions = [i for i, r in enumerate(out) if _is_playlist_instance(r)]
    out.insert(playlist_positions[-1] + 1, _blank_clone(out[playlist_positions[-1]], new_count))

    return out


def add_stereo_track(records: list[IndexRecord], new_count: int) -> list[IndexRecord]:
    """Back-compat wrapper: add one 2-channel (stereo) track."""
    return add_track(records, new_count, channels=2)


def add_click_track(records: list[IndexRecord], new_count: int) -> list[IndexRecord]:
    """Transform the record list to add the session's (single) click track, bringing
    the total lane-track count to `new_count`. A click is a 2-lane "track" whose
    playlist is `0x261e` (not audio's `0x261c`) and which has NO channel map, so vs.
    `add_track` it: SKIPS the 0x1015 (audio-count) and 0x1054 (channel) container
    markers; appends one marker each to the 0x2519 and 0x2624 count==1 containers;
    grows the 0x2519 packed-offset table by one; inserts a new 0x251a lane instance
    and a new 0x2624 playlist instance whose childref child_type is switched from
    `0x261C` to `0x261E`. Returns a new list; `records` is not mutated."""
    out = copy.deepcopy(records)

    def append_marker(record: IndexRecord, times: int = 1) -> None:
        for _ in range(times):
            record.elements.append(Element(rel=0, tag1=0x04, offsets=[0]))

    # NO 0x1015 (audio count unchanged) and NO 0x1054 (the click has 0 channels).
    append_marker(_only(out, lambda r: r.content_type == 0x2519 and r.count == 1 and not r.child_refs))
    append_marker(_only(out, lambda r: r.content_type == 0x2624 and r.count == 1 and not r.child_refs))

    table = _only(out, lambda r: r.content_type == 0x2519 and r.count == 2 and r.flag == 0)
    table.elements[0].offsets.append(0)
    table.elements[0].tag1 = 4 * len(table.elements[0].offsets)

    lane_positions = [i for i, r in enumerate(out) if _is_lane_instance(r)]
    out.insert(lane_positions[-1] + 1, _blank_clone(out[lane_positions[-1]], new_count))

    for record in out:
        if record.content_type == 0x2519 and record.child_refs and record.child_refs[0].child_type in (0x251B, 0x2716):
            record.ordinal = new_count

    playlist_positions = [i for i, r in enumerate(out) if _is_playlist_instance(r)]
    clone = _blank_clone(out[playlist_positions[-1]], new_count)
    for child in clone.child_refs:
        if child.child_type == 0x261C:      # the click's playlist is 0x261e
            child.child_type = 0x261E
    out.insert(playlist_positions[-1] + 1, clone)

    return out


def synthesize_index_records(
    base_records: list[IndexRecord],
    base_count: int,
    target_count: int,
    channels: "int | list[int]" = 2,
    click_tracks: "set[int] | tuple[int, ...]" = (),
) -> list[IndexRecord]:
    """Grow a `base_count`-track index record list to `target_count` tracks.
    `channels` is the per-added-track channel count: an int (uniform — every added
    track has that many channels) OR a list indexed by track number (1-based), so a
    MIXED session can be built track-by-track (e.g. `[2,1,2]` for stereo,mono,stereo)
    from a 1-track donor. `click_tracks` is the set of 1-based track positions that
    are click tracks (added via `add_click_track` instead of `add_track`). Offsets in
    new/grown holes are 0; fill them with `rebuild_index_offsets` (or `_fill_offsets`)
    against the final layout."""
    records = base_records
    clicks = set(click_tracks)
    for count in range(base_count + 1, target_count + 1):
        if count in clicks:
            records = add_click_track(records, count)
        else:
            ch = channels[count - 1] if isinstance(channels, (list, tuple)) else channels
            records = add_track(records, count, channels=ch)
    return records


def block_layout(data: bytes) -> tuple[dict[int, int], dict[int, list[int]]]:
    """Return (zmark_offset -> content_type, content_type -> [zmark offsets]).

    The per-type offset lists are sorted ascending so a block can be addressed by
    (content_type, rank).
    """
    zmark_to_type: dict[int, int] = {}
    by_type: dict[int, list[int]] = {}
    for zmark, _end, content_type in W._flatten_block_bounds(data):
        zmark_to_type[zmark] = content_type
        by_type.setdefault(content_type, []).append(zmark)
    for offsets in by_type.values():
        offsets.sort()
    return zmark_to_type, by_type


def final_index_ref(data: bytes) -> W.BlockRef | None:
    """The trailing 0x0002 block, or None if the file has no master index."""
    refs = W.top_level_refs(data)
    if not refs or refs[-1].block.content_type != 0x0002:
        return None
    return refs[-1]


def offset_holes(data: bytes) -> tuple["W.BlockRef | None", list[tuple[int, int, int, int, str]]]:
    """Locate every offset hole in the master index by walking the parsed records.

    Holes come from the grammar, never from scanning for values that happen to
    equal a block offset (that misclassifies coincidental scalars — e.g. a count
    field equal to a 0x2037 block offset at N=7). Two kinds:
      * childref — the u32 in a container record's child-ref.
      * marker   — each of the `k` u32 offsets in a marker/table element
        (`k == 1` is the familiar `01 04 00 01 00 <off>`; `k > 1` is a table).

    Returns (final_ref, holes) where each hole is
    (abs_pos, value, target_type, target_rank, kind):
      abs_pos      absolute byte position of the u32 in `data`
      value        current stored offset
      target_type  content_type of the block it points at
      target_rank  index of that block among blocks of target_type (offset order)
      kind         "marker" or "childref"
    """
    ref = final_index_ref(data)
    if ref is None:
        return None, []
    zmark_to_type, by_type = block_layout(data)
    known = set(by_type)
    zmarks = set(zmark_to_type)
    records = parse_records(ref.data, known, zmarks)
    holes: list[tuple[int, int, int, int, str]] = []

    def add(abs_pos: int, value: int, kind: str) -> None:
        target_type = zmark_to_type.get(value)
        if target_type is None:
            return
        rank = by_type[target_type].index(value)
        holes.append((abs_pos, value, target_type, rank, kind))

    for record in records:
        for child in record.child_refs:
            add(ref.start + record.start + child.rel + 2, child.offset, "childref")
        for element in record.elements:
            for index, value in enumerate(element.offsets):
                add(ref.start + record.start + element.rel + 5 + 4 * index, value, "marker")
    return ref, holes


def rebuild_index_offsets(data: bytes) -> bytes:
    """Rewrite every master-index offset hole from the current block layout.

    For each hole, the target block is resolved by (content_type, rank) and the
    hole is filled with that block's current ZMARK offset. On an unmodified file
    this round-trips byte-for-byte; on a file whose body has moved it produces
    correct offsets with no value-guessing and no per-type special cases.
    """
    ref, holes = offset_holes(data)
    if ref is None:
        return data
    _zmark_to_type, by_type = block_layout(data)
    out = bytearray(data)
    for abs_pos, _value, target_type, target_rank, _kind in holes:
        new_value = by_type[target_type][target_rank]
        out[abs_pos : abs_pos + 4] = int(new_value).to_bytes(4, "little")
    return bytes(out)


def _set_first_block_index_pointer(data: bytes, index_start: int) -> bytes:
    """Write the master-index absolute offset into the first top-level block (the
    4-byte pointer Pro Tools seeks with; wrong value -> 'magic ID does not match')."""
    z = _counter_zmark(data)  # first top-level block's ZMARK
    out = bytearray(data)
    out[z + 7 : z + 11] = int(index_start).to_bytes(4, "little")
    return bytes(out)


def reindex_after_resize(consistent_data: bytes, resized_data: bytes) -> bytes:
    """Robustly repair `resized_data`'s master index after a body-size change that
    grew/shrank blocks but did NOT add or remove indexed records (e.g. inserting
    audio/MIDI/tempo content into an already-indexed scaffold).

    The body shift makes `resized_data`'s stored offsets stale — so stale that
    `parse_records` can no longer even frame the index (a child-ref offset must be
    a live ZMARK to be recognized). So we capture the offset holes from
    `consistent_data` (whose index still parses), then refill each at its stable
    index-relative position with the target block's NEW offset, resolved by
    (content_type, rank) in the resized layout. No value-guessing, no re-parsing the
    stale index. Mirrors `rename_track`'s tail; this is the robust replacement for
    the legacy `writer._update_final_index` offset guesser (which corrupts records
    like 0x2587 at some configs -> Pro Tools EOS / magic-ID).

    Requires the indexed (content_type, rank) mapping to be preserved across the
    resize (true for block-growth content insertion; NOT for track-count growth,
    which needs `compose_index`/`add_track` to add records first)."""
    ref0 = final_index_ref(consistent_data)
    ref1 = final_index_ref(resized_data)
    if ref0 is None or ref1 is None:
        return resized_data
    _r, holes = offset_holes(consistent_data)
    old_start, new_start = ref0.start, ref1.start
    _zmark_to_type, by_type = block_layout(resized_data)
    out = bytearray(resized_data)
    for abs_pos, _value, target_type, target_rank, _kind in holes:
        npos = new_start + (abs_pos - old_start)
        targets = by_type.get(target_type, ())
        if 0 <= npos and npos + 4 <= len(out) and target_rank < len(targets):
            out[npos : npos + 4] = int(targets[target_rank]).to_bytes(4, "little")
    return _set_first_block_index_pointer(bytes(out), new_start)


def index_target_type_sequence(data: bytes) -> list[tuple[int, str]]:
    """The ordered sequence of marker/child-ref target *content_types*.

    Layout-independent: depends only on the track layout, not on absolute
    offsets. Two sessions with the same track mix produce the same sequence —
    this is the structural invariant Pass 2 reproduces. (Concrete instance ranks
    are deliberately excluded; those are set by the block Pass 1 places.)
    """
    _ref, holes = offset_holes(data)
    return [(target_type, kind) for _pos, _val, target_type, _rank, kind in holes]


# --- offset composition: fill a synthesized index against a target body --------
#
# Resolving each hole to a target block survives the body's per-track reordering
# (lane-major blocks) by matching on the track NAME embedded in each block
# ("Audio N"), grouped as (content_type, name, occurrence). The per-N "counter"
# block (first top-level, type = 2530*N + 1886) is given a unique label so its
# type collision with a real content_type doesn't pollute occurrences.

_NAME_RE = re.compile(rb"(?:Audio|Click|MIDI) (\d+)")
_CONTAINER_CHILD = {0x1015: 0x1014, 0x1054: 0x1052, 0x2519: 0x251A, 0x2624: 0x261C}
_COUNTER_LABEL = (-1, None)


def _counter_zmark(data: bytes) -> int:
    return min(b.offset - 7 for b in W.parse_unxored(data).blocks)


def _flat_blocks(data: bytes):
    ptf = W.parse_unxored(data)
    out: list = []

    def rec(block):
        out.append(block)
        for child in block.child:
            rec(child)

    for block in ptf.blocks:
        rec(block)
    out.sort(key=lambda b: b.offset)
    return out


def _block_label(data: bytes, block, counter_z: int) -> tuple[int, str | None]:
    if block.offset - 7 == counter_z:
        return _COUNTER_LABEL
    body = data[block.offset - 7 : block.offset + block.block_size]
    m = _NAME_RE.search(body)
    return (block.content_type, m.group(0).decode() if m else None)


def _label_maps(data: bytes):
    """Return (zmark -> (label, occ), label -> [zmarks in doc order])."""
    counter_z = _counter_zmark(data)
    z2lo: dict[int, tuple] = {}
    seen: dict[tuple, int] = {}
    by_label: dict[tuple, list[int]] = {}
    for block in _flat_blocks(data):
        z = block.offset - 7
        label = _block_label(data, block, counter_z)
        z2lo[z] = (label, seen.get(label, 0))
        seen[label] = seen.get(label, 0) + 1
        by_label.setdefault(label, []).append(z)
    return z2lo, by_label


def compose_index(
    donor_data: bytes,
    target_body_data: bytes,
    base_count: int,
    target_count: int,
    channels: int = 2,
    track_names: "list[str] | None" = None,
    click_tracks: "set[int] | tuple[int, ...]" = (),
) -> bytes:
    """Build the 0x0002 index for a `target_count`-track body from a
    `base_count`-track donor. `channels` = the audio channels per added track
    (2=stereo, 1=mono); it sets how many 0x1052-lane markers each new track's
    0x1054 record gains.

    Synthesizes the record structure from the donor index, then fills every
    offset against `target_body_data`'s block layout (the donor index supplies
    each existing hole's logical target; new holes target the new tracks' blocks).
    Returns the full index block bytes. Single-step (target == base + 1) is
    byte-exact-validated; multi-step grows iteratively.
    """
    donor_ref = final_index_ref(donor_data)
    donor_zt, donor_bt = block_layout(donor_data)
    donor_records = parse_records(donor_ref.data, set(donor_bt), set(donor_zt))
    synth = synthesize_index_records(donor_records, base_count, target_count,
                                     channels=channels, click_tracks=click_tracks)
    records = _fill_offsets(donor_records, synth, donor_data, target_body_data, base_count,
                            track_names=track_names)
    return serialize_final_block(records)


def _fill_offsets(donor_records, synth, donor_data, target_data, base_count, track_names=None):
    """Fill synth holes against target_data; returns synth with offsets set.

    New per-track instances are assigned tracks in document order (the k-th
    instance of its type -> track k), so a single fill handles any number of
    added tracks (multi-step / from-scratch). `track_names` (1-based, optional)
    overrides the default "Audio {k}" name used to resolve a track's blocks — pass
    e.g. [..., "Click 1"] so a click track's blocks (named "Click 1", not "Audio N")
    resolve correctly."""
    def tname(k):
        if track_names and 0 <= k - 1 < len(track_names):
            return track_names[k - 1]
        return f"Audio {k}"

    d_z2lo, _ = _label_maps(donor_data)
    t_z2lo, t_by_label = _label_maps(target_data)
    donor_first = _counter_zmark(donor_data)
    target_first = _counter_zmark(target_data)

    donor_names: dict[int, set] = {}
    for (ct, nm), _zs in _label_maps(donor_data)[1].items():
        donor_names.setdefault(ct, set()).add(nm)
    donor_count: dict[int, int] = {}
    d_lastocc: dict[int, int] = {}
    for b in _flat_blocks(donor_data):
        lbl = _block_label(donor_data, b, donor_first)
        donor_count[lbl[0]] = donor_count.get(lbl[0], 0) + 1
        d_lastocc[b.content_type] = b.offset - 7

    new_by_type: dict[int, list[int]] = {}
    seen_ct: dict[int, int] = {}
    tcz = _counter_zmark(target_data)
    for b in _flat_blocks(target_data):
        ct, nm = _block_label(target_data, b, tcz)
        if ct == -1:
            continue
        occ = seen_ct.get(ct, 0)
        seen_ct[ct] = occ + 1
        is_new = (nm is not None and nm not in donor_names.get(ct, set())) if nm is not None \
            else (occ >= donor_count.get(ct, 0))
        if is_new:
            new_by_type.setdefault(ct, []).append(b.offset - 7)

    cursor = {ct: 0 for ct in new_by_type}

    def pop_new(ct):
        lst = new_by_type.get(ct, [])
        i = cursor.get(ct, 0)
        cursor[ct] = i + 1
        return lst[i] if i < len(lst) else 0

    def by_name_lane(ct, track, lane):
        """The `lane`-th block of type `ct` named like track `track` (or 0)."""
        lst = t_by_label.get((ct, tname(track)), [])
        return lst[lane] if lane < len(lst) else 0

    def match(v):
        if v == donor_first:
            return target_first
        label, occ = d_z2lo[v]
        lst = t_by_label.get(label, [])
        return lst[occ] if occ < len(lst) else 0

    def remap_newtrack(v, new_track):
        (ct, nm), occ = d_z2lo[v]
        if nm is not None:
            lst = t_by_label.get((ct, tname(new_track)), [])
            if not lst and ct == 0x261C:
                # A click track's playlist-instance childref chain references the click
                # playlist (0x261e) where the audio template references its 0x261c. Only
                # fires when the 0x261c lookup is empty (i.e. a click), so audio is
                # untouched.
                lst = t_by_label.get((0x261E, tname(new_track)), [])
            return lst[occ] if occ < len(lst) else match(v)
        if ct in new_by_type and v == d_lastocc.get(ct):
            return pop_new(ct)
        return match(v)

    # donor templates for the freshly-added instances (holes are zeroed in synth)
    def last_instance(pred):
        cand = [r for r in donor_records if pred(r)]
        return cand[-1] if cand else None

    d_2519 = last_instance(lambda r: r.content_type == 0x2519 and r.count == 2 and r.flag == 1
                           and r.child_refs and r.child_refs[0].child_type == 0x251A)
    d_2624 = last_instance(lambda r: r.content_type == 0x2624 and r.count == 4)

    def is_lane_inst(r):
        return r.content_type == 0x2519 and r.count == 2 and r.flag == 1 \
            and r.child_refs and r.child_refs[0].child_type == 0x251A

    def is_playlist_inst(r):
        return r.content_type == 0x2624 and r.count == 4

    lane_track = 0
    playlist_track = 0
    for record in synth:
        zeroed = all(c.offset == 0 for c in record.child_refs) and \
            all(o == 0 for e in record.elements for o in e.offsets)
        new_track = None
        template = None
        if is_lane_inst(record):
            lane_track += 1
            if zeroed:
                template, new_track = d_2519, lane_track
        elif is_playlist_inst(record):
            playlist_track += 1
            if zeroed:
                template, new_track = d_2624, playlist_track
        tmpl_holes = []
        if template:
            tmpl_holes = [c.offset for c in template.child_refs] + \
                [o for e in template.elements for o in e.offsets]

        # 0x2519 parent (markers -> 0x251a lane 2) and table (offsets -> lane 1)
        # reference lane-major 0x251a by name; assign new ones positionally.
        is_parent = record.content_type == 0x2519 and record.count == 1 and not record.child_refs
        is_table = record.content_type == 0x2519 and record.count == 2 and record.flag == 0

        hi = 0

        def resolve(value, lane_track=None, lane=None):
            nonlocal hi
            if value != 0:
                out = match(value)
            elif lane_track is not None:
                out = by_name_lane(0x251A, lane_track, lane)
            elif template:
                out = remap_newtrack(tmpl_holes[hi], new_track)
            else:
                child = _CONTAINER_CHILD.get(record.content_type,
                                             0x251A if record.content_type == 0x2519 else None)
                out = pop_new(child) if child is not None else 0
                if out == 0 and record.content_type == 0x2624:
                    # the click track's playlist-container marker targets 0x261e
                    out = pop_new(0x261E)
            hi += 1
            return out

        for child in record.child_refs:
            child.offset = resolve(child.offset)
        if is_table:
            element = record.elements[0]
            element.offsets = [resolve(o, lane_track=j + 1, lane=0) for j, o in enumerate(element.offsets)]
            for element in record.elements[1:]:
                element.offsets = [resolve(o) for o in element.offsets]
        elif is_parent:
            for ei, element in enumerate(record.elements):
                # element 0 is the self-marker (-> 0x2519); 1..N -> 0x251a track ei
                lt = None if ei == 0 else ei
                element.offsets = [resolve(o, lane_track=lt, lane=1) for o in element.offsets]
        else:
            for element in record.elements:
                element.offsets = [resolve(o) for o in element.offsets]

    return synth
