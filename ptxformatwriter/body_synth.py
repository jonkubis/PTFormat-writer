"""Body synthesis (Pass 1): grow a session's block body to N tracks.

The body (everything except the final 0x0002 index) has no structured
absolute-pointer network — cross-block references live only in the index. So
growing the body is purely structural: insert per-track child blocks into their
parents, grow the ordinal lists, set the per-N first-block counter, and recompute
every enclosing block_size. After the body is grown, regenerate the index with
`final_index.compose_index`.

Implementation is byte-level: blocks are spliced in at exact offsets and every
enclosing parent's `block_size` field is bumped by the inserted length. This
matches the real lane-major layout that `compose_index` resolves against.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .core import Block, PTFFormat
from . import writer as W

_NAME_RE = re.compile(rb"Audio (\d+)")
_TRACK_NAME = re.compile(rb"(?:Audio|Click|MIDI) \d+")


def parse(data: bytes) -> PTFFormat:
    return W.parse_unxored(data)


def _own_track_name(data: bytes, b: Block) -> "str | None":
    """The track name in a block's OWN bytes (children blanked), or None. Used to
    locate the click's per-track blocks for reordering (the click is named
    'Click 1'); a container that merely contains a named child returns None."""
    own = bytearray(data[b.offset : b.offset + b.block_size])
    for c in b.child:
        cs, ce = (c.offset - 7) - b.offset, (c.offset + c.block_size) - b.offset
        for i in range(max(0, cs), min(ce, len(own))):
            own[i] = 0
    m = _TRACK_NAME.search(bytes(own))
    return m.group(0).decode() if m else None


def flat_blocks(ptf: PTFFormat) -> list[Block]:
    out: list[Block] = []

    def rec(b: Block) -> None:
        out.append(b)
        for c in b.child:
            rec(c)

    for b in ptf.blocks:
        rec(b)
    out.sort(key=lambda b: b.offset)
    return out


def block_bytes(data: bytes, b: Block) -> bytes:
    return data[b.offset - 7 : b.offset + b.block_size]


def _ancestor_chain(ptf: PTFFormat, parent_zmark: int) -> list[int]:
    """The block at `parent_zmark` plus every block that strictly contains it
    (its ancestors), as a list of zmark offsets. These are exactly the blocks
    whose `block_size` must grow when a child is inserted into the parent."""
    blocks = flat_blocks(ptf)
    by_zmark = {b.offset - 7: b for b in blocks}
    p = by_zmark[parent_zmark]
    pstart, pend = parent_zmark, p.offset + p.block_size
    chain = [parent_zmark]
    for b in blocks:
        bstart, bend = b.offset - 7, b.offset + b.block_size
        if bstart < pstart and pend <= bend:  # strictly contains the parent
            chain.append(bstart)
    return chain


@dataclass
class Insertion:
    offset: int        # absolute byte offset to insert at (within parent content)
    blob: bytes        # bytes to insert (a full child block, or own-bytes)
    parent_zmark: int  # zmark of the block being grown (its size + ancestors bump)


def apply_insertions(data: bytes, ptf: PTFFormat, insertions: list[Insertion]) -> bytes:
    """Insert each blob into its parent and bump the parent + all its ancestors'
    `block_size`. Disambiguating the parent explicitly (rather than inferring from
    the offset) is required because an insertion offset sits on a block boundary
    that is shared between a parent's content-end and the next sibling's start."""
    size_delta: dict[int, int] = {}  # zmark -> total added bytes
    for ins in insertions:
        for zmark in _ancestor_chain(ptf, ins.parent_zmark):
            size_delta[zmark] = size_delta.get(zmark, 0) + len(ins.blob)

    out = bytearray(data)
    for zmark, delta in size_delta.items():
        size_pos = zmark + 3
        old = int.from_bytes(out[size_pos : size_pos + 4], "little")
        out[size_pos : size_pos + 4] = (old + delta).to_bytes(4, "little")
    for ins in sorted(insertions, key=lambda x: x.offset, reverse=True):
        out[ins.offset : ins.offset] = ins.blob
    return bytes(out)


# --- stereo track unit + single-track grow ----------------------------------
from . import final_index as _FI  # noqa: E402  (avoid import cycle at top)


@dataclass
class StereoTrackUnit:
    """The block subtrees a single empty AUDIO track contributes to the body.
    Each is full block bytes (children inline). Lane order is lane-major for
    0x251a (lane0, lane1) and track-adjacent for 0x1052.

    `b1052` holds one 0x1052 audio lane PER CHANNEL: 2 for stereo, 1 for mono.
    The number of lanes is the track's channel count, so `len(b1052)` discriminates
    mono (1) from stereo (2). The class name is historical; it serves both."""

    b1014: bytes
    b1052: tuple[bytes, ...]
    b251a: tuple[bytes, bytes]
    b210b: bytes
    b261c: bytes
    b2589: bytes
    name_entry: bytes  # 34-byte entry for the 0x2519 own-bytes name table

    @property
    def channels(self) -> int:
        return len(self.b1052)


def _by_type(ptf: PTFFormat, ct: int) -> list[Block]:
    return [b for b in flat_blocks(ptf) if b.content_type == ct]


def _parent_zmarks(ptf: PTFFormat) -> dict[int, int]:
    pm: dict[int, int] = {}

    def rec(b: Block) -> None:
        for c in b.child:
            pm[c.offset - 7] = b.offset - 7
            rec(c)

    for b in ptf.blocks:
        rec(b)
    return pm


_NAME_ENTRY_SUFFIX = 23  # bytes after `<len:u32><name>` in a 0x2519 name entry


def _name_table_entries(table: bytes) -> list[bytes]:
    """Split a 0x2519 own-byte name table into per-track entries (track order).
    Each entry is `<name_len:u32> "Audio k" <23-byte suffix>`, so single-digit
    names give 34-byte entries and multi-digit ("Audio 10"...) give 35+. The final
    entry is clamped to the table end (its last 2 bytes fall past the parsed
    own-byte boundary); the wholesale transplant in `synthesize` fixes exactness."""
    first = _NAME_RE.search(table)
    if first is None:
        return []
    pos = first.start() - 4  # the u32 length prefix precedes the name
    entries: list[bytes] = []
    while pos + 4 <= len(table):
        nlen = int.from_bytes(table[pos : pos + 4], "little")
        if nlen <= 0 or nlen > 64:
            break
        step = 4 + nlen + _NAME_ENTRY_SUFFIX
        entries.append(table[pos : min(pos + step, len(table))])
        pos += step
    return entries


def _name_table_region(data: bytes, ptf: PTFFormat) -> tuple[int, int]:
    """(content_start, first_child_zmark) of the single 0x2519 block's own-byte
    name table."""
    b = _by_type(ptf, 0x2519)[0]
    return b.offset, min(c.offset - 7 for c in b.child)


def _set_name_table(body: bytes, library_data: bytes) -> bytes:
    """Replace the synthesized 0x2519 own-byte name table with the library's.
    Track-name entries are variable length (multi-digit names are longer) and the
    last entry has a boundary quirk; transplanting the target control's table
    reproduces it exactly and recomputes the 0x2519 (+ ancestor) block sizes."""
    ptf = parse(body)
    start, first_child = _name_table_region(body, ptf)
    lptf = parse(library_data)
    lstart, lfirst = _name_table_region(library_data, lptf)
    lib_tbl = library_data[lstart:lfirst]
    delta = len(lib_tbl) - (first_child - start)
    bz = _by_type(ptf, 0x2519)[0].offset - 7
    out = bytearray(body)
    if delta:
        for zmark in _ancestor_chain(ptf, bz):
            sp = zmark + 3
            old = int.from_bytes(out[sp : sp + 4], "little")
            out[sp : sp + 4] = (old + delta).to_bytes(4, "little")
    out[start:first_child] = lib_tbl
    return bytes(out)


def extract_track(data: bytes, track: int, total: int, channels: int = 2) -> StereoTrackUnit:
    """Extract audio track `track` (1-based) from a control with `total` audio
    tracks of `channels` channels each (2=stereo, 1=mono). Per-track blocks are
    deterministic per track index, so any uniform control with >= track tracks
    yields the same unit.

    0x1052 audio lanes are channel-major: an all-`channels` control lays them out
    as `channels` consecutive lanes per track, so track k's lanes are at indices
    `channels*(k-1) .. channels*(k-1)+channels-1`. 0x251a is lane-major (2 per
    track regardless of channel count): lane0 = k-1, lane1 = total + (k-1)."""
    ptf = parse(data)
    bb = lambda b: block_bytes(data, b)
    a14 = _by_type(ptf, 0x1014)
    a52 = _by_type(ptf, 0x1052)
    a51 = _by_type(ptf, 0x251a)
    a0b = _by_type(ptf, 0x210b)
    a1c = _by_type(ptf, 0x261c)
    a89 = _by_type(ptf, 0x2589)
    # 0x2519 own-byte name entry for this track. Entries are VARIABLE length:
    # `<name_len:u32> "Audio k" <23-byte suffix>`, so single-digit names are 34
    # bytes and multi-digit ("Audio 10"...) are 35+. A fixed stride misaligns the
    # table for >=10 tracks, so parse each entry by its length prefix.
    b2519 = _by_type(ptf, 0x2519)[0]
    first_child = min(c.offset - 7 for c in b2519.child)
    table = data[b2519.offset : first_child]
    name_entry = _name_table_entries(table)[track - 1]
    # 0x1052 audio lanes are track-major (each track contributes `channels` lanes).
    # The base index of track `track`'s lanes = the cumulative channel count of all
    # tracks before it. For a uniform control that's `channels*(track-1)`, but a
    # MIXED source (e.g. mono+stereo) needs the real running total, else we'd grab
    # an adjacent track's lane.
    audio = [t for t in track_types(data) if t.kind in ("mono", "stereo")]
    base = sum(t.channels for t in audio[: track - 1]) if len(audio) >= track else channels * (track - 1)
    return StereoTrackUnit(
        b1014=bb(a14[track - 1]),
        b1052=tuple(bb(a52[base + i]) for i in range(channels)),
        b251a=(bb(a51[track - 1]), bb(a51[total + (track - 1)])),
        b210b=bb(a0b[track - 1]),
        b261c=bb(a1c[track - 1]),
        b2589=bb(a89[track - 2]),  # 0x2589 count is N-1; track k's is index k-2
        name_entry=name_entry,
    )


def _patch_u32(buf: bytearray, pos: int, val: int) -> None:
    buf[pos : pos + 4] = int(val).to_bytes(4, "little")


def grow_one_track(data: bytes, base_n: int, unit: StereoTrackUnit) -> bytes:
    """Insert one empty audio track (-> base_n+1 tracks) into the body `data`
    (a body without the final index). Works for mono or stereo: the number of
    0x1052 audio lanes appended = `unit.channels`, and the 0x1054 channel count is
    set to `channels * (base_n+1)` (uniform session). Returns the new body."""
    n = base_n + 1
    ptf = parse(data)
    pm = _parent_zmarks(ptf)
    ins: list[Insertion] = []

    def append_after(block: Block, blob: bytes) -> None:
        ins.append(Insertion(block.offset + block.block_size, blob, pm[block.offset - 7]))

    a14 = _by_type(ptf, 0x1014)
    append_after(a14[-1], unit.b1014)
    a52 = _by_type(ptf, 0x1052)
    for lane in unit.b1052:                   # 1 (mono) or 2 (stereo) lanes
        append_after(a52[-1], lane)
    a51 = _by_type(ptf, 0x251a)               # lane-major: lane0 mid-run, lane1 end
    append_after(a51[base_n - 1], unit.b251a[0])
    append_after(a51[-1], unit.b251a[1])
    append_after(_by_type(ptf, 0x210b)[-1], unit.b210b)
    append_after(_by_type(ptf, 0x261c)[-1], unit.b261c)
    append_after(_by_type(ptf, 0x2589)[-1], unit.b2589)

    # 0x2519 own-bytes name entry (inserted before the first child)
    b2519 = _by_type(ptf, 0x2519)[0]
    first_child = min(c.offset - 7 for c in b2519.child)
    ins.append(Insertion(first_child, unit.name_entry, b2519.offset - 7))

    # 0x202a ordinal append (u16 before the fe ff terminator)
    for b in _by_type(ptf, 0x202a):
        s = b.offset - 7
        seg = data[s : b.offset + b.block_size]
        idx0 = seg.find(b"\xff\xff\xff\xff\x00\x00")
        feff = seg.find(b"\xfe\xff", idx0 + 8)
        ins.append(Insertion(s + feff, (n - 1).to_bytes(2, "little"), s))

    body = bytearray(apply_insertions(data, ptf, ins))
    # 0x1054 = the session's TOTAL audio channels. Reading it from the grown body
    # (sum of every track's channel count) is correct for uniform AND mixed
    # sessions; `unit.channels * n` would only hold when every track has the same
    # channel count (a too-small value -> Pro Tools "end of stream").
    _patch_counts(body, n, channel_count(bytes(body)))
    return bytes(body)


def add_click(data: bytes, clean_ref: bytes, click_ref: bytes) -> bytes:
    """Add the session's (single) click track to audio session `data` (full unxored
    bytes, body + index), placed as the LAST track, via the structural diff-replay in
    `click_clone`. Returns full unxored bytes (ready for `writer.encrypt_session_data`).

    `clean_ref` / `click_ref` are a CLEAN CONTROL PAIR sharing the same audio: an
    audio-only session and the SAME session with a Click track added (e.g.
    `lots of stereo tracks/{N stereo tracks, N stereo plus click}.ptx`). The recursive
    diff between them isolates the click's exact contribution (playlist 0x261e + the
    DigiClick plugin, the two overview 0x2589 entries, the per-track 0x200b chains, the
    "Click 1" 0x2519 name entry, the 0x1018 "Click II" registry growth, and the session
    DigiClick registration 0x2064); replaying it onto `data` adds the click.

    `clean_ref`'s audio LAYOUT must match `data`'s (same track count + types) so the
    diff's splice offsets line up with `data`'s blocks. This is the same kind of
    constraint the uniform synthesizers place on their library (a matching-N control).
    BYTE-EXACT when `data` IS `clean_ref` (validated 1 and 2 stereo + click); for a
    structurally-identical synthesized `data` the click still splices in correctly.
    PT-confirmed for 1 audio + click.

    NOTE: the click's 0x2064 carries `click_ref`'s embedded session path; if `data` is
    a different session folder than the ref, path-normalize the result (the click-only
    path chimera). Same-session (the validation case) needs no normalization."""
    from . import click_clone as _CC
    patch = _CC.derive_click_patch(clean_ref, click_ref)
    return _CC.apply_click_patch(data, patch, data)


def add_click_anyN(data: bytes, clean_ref: bytes, click_ref: bytes, at_top: bool = False) -> bytes:
    """Add a Click track to an audio session `data` of ANY track count, sourcing the click from
    a clean/click control PAIR of a DIFFERENT (smaller) layout. Unlike `add_click` (which needs
    `clean_ref`'s audio layout to match `data`), this uses the layout-independent structural
    re-key in `click_clone` (Frontier 1): it splices the click's fixed footprint by owner
    SIGNATURE (end-anchored), replicates the per-audio-track 0x261b rewrite across every track,
    rebuilds the click's 0x2519 name-table entry (incl. the `02 00` separator) + its two
    lane-major 0x251a lanes, and re-stamps the track count baked into the click's own bytes.

    The click is placed LAST; `data`'s own session name/path is preserved (the click control's
    0x2067 is NOT transplanted). `clean_ref`/`click_ref` should be a >= 2-audio pair (so the
    per-track rep is detectable), e.g. `2 stereo tracks.ptx` + `2 stereo plus click.ptx`.
    PT-CONFIRMED at N=3 and N=12 (stereo). Returns full unxored bytes (ready for
    `writer.encrypt_session_data`).

    `at_top=True` places the Click as the FIRST (top) track instead of the bottom,
    via `move_click_to_top` (the edit-window display order lives in the index's
    playlist-order list). PT-CONFIRMED at N=2 and N=12 (stereo).

    NAME-ROBUST: the structural re-key matches the target's audio tracks by their canonical
    `Audio N` names, so a click added AFTER tracks were renamed used to silently drop (no
    `Audio N` to match). This now normalizes the audio tracks to `Audio N` for the splice
    (routed through unique temporaries, so even a non-first `Audio N` name can't collide),
    then restores the original names afterward — the PT-confirmed names-last direction, and
    BYTE-IDENTICAL to building with canonical names and renaming last. So the click no longer
    depends on rename ORDER (you may rename before or after; the old 'rename last' rule is gone)."""
    from . import click_clone as _CC
    tt = track_types(data)
    aidx = [i for i, t in enumerate(tt) if t.kind in ("mono", "stereo")]
    canon = {i: f"Audio {k + 1}" for k, i in enumerate(aidx)}
    need_norm = any(tt[i].name != canon[i] for i in aidx)
    d = data
    if need_norm:                       # rename audio tracks -> Audio N for the splice
        d = set_track_names(d, {i: f"ClkNormTmp{k}" for k, i in enumerate(aidx)})  # unique temps
        d = set_track_names(d, canon)                                              # -> canonical
    spatch = _CC.derive_click_patch_structural(clean_ref, click_ref)
    out = _CC.apply_click_patch_structural(d, spatch, d)
    out = move_click_to_top(out) if at_top else out
    if need_norm:                       # restore the caller's original audio-track names
        now = track_types(out)          # click is not an audio track; audio order is preserved
        anow = [i for i, t in enumerate(now) if t.kind in ("mono", "stereo")]
        out = set_track_names(out, {i: tt[aidx[k]].name for k, i in enumerate(anow)})
    return out


# --- content transplant (clip / tempo / meter) from a matched control pair -----
#
# A whole class of session content — an audio clip, a tempo change, a meter change —
# is added the SAME way Pro Tools writes it: a handful of top-level blocks grow or
# change while the rest of the session is untouched. Given a CLEAN/MODIFIED control
# PAIR (the same session without / with the content), the contribution is exactly the
# set of top-level blocks that differ. `_transplant_top_level` copies a chosen set of
# those blocks from the modified ref onto `data`, keeps `data`'s own identity/display,
# and robustly repairs the master index with `final_index.reindex_after_resize`
# (capturing the index entries from the ref, whose index still parses, and refilling
# every offset for the assembled layout — also rewriting the first-block index
# pointer). `data` must share scaffold with the clean ref so the blocks line up — the
# same matched-pair constraint as `add_click`. BYTE-EXACT when `data` IS the clean ref.


def changed_top_level_types(clean_ref: bytes, mod_ref: bytes,
                            exclude: "tuple[int, ...]" = (0x2067, 0x0002)) -> list[int]:
    """The content_types of top-level blocks that DIFFER (in bytes) between a matched
    clean/modified control pair, excluding session-identity (0x2067 name/MRU) and the
    master index (0x0002). This is the content's structural footprint."""
    c = {b.content_type: b for b in parse(clean_ref).blocks}
    mblocks = parse(mod_ref).blocks
    # the first top-level block is the index-offset pointer (its content_type encodes the
    # index offset's low u16); it is DERIVED (set by reindex), never transplanted — and
    # its type shifts whenever a block is resized, so it must not enter the change set.
    first_off = min(b.offset for b in mblocks)
    m = {b.content_type: b for b in mblocks}
    out: list[int] = []
    for ct, mb in m.items():
        if ct in exclude or mb.offset == first_off:
            continue
        cb = c.get(ct)
        mbytes = mod_ref[mb.offset - 7 : mb.offset + mb.block_size]
        cbytes = clean_ref[cb.offset - 7 : cb.offset + cb.block_size] if cb else None
        if cbytes != mbytes:
            out.append(ct)
    return out


def _transplant_top_level(data: bytes, ref: bytes, types) -> bytes:
    """Replace each top-level block of `data` whose content_type is in `types` with
    `ref`'s block of that type, append `ref`'s master index (it carries the content's
    index entries), then repair every index offset for the assembled layout via
    `final_index.reindex_after_resize`. Returns full unxored bytes."""
    types = set(types)
    ref_by_type = {b.content_type: b for b in parse(ref).blocks}
    for ct in types:
        if ct not in ref_by_type:
            raise ValueError(f"ref missing top-level block 0x{ct:04x}")
    data_refs = W.top_level_refs(data)
    if not data_refs or data_refs[-1].block.content_type != 0x0002:
        raise ValueError("data has no trailing 0x0002 master index")

    out = bytearray(data[: data_refs[0].start])  # file header (PT version + XOR info)
    for r in data_refs[:-1]:  # body blocks only (exclude the trailing 0x0002 index)
        ct = r.block.content_type
        if ct in types:
            rb = ref_by_type[ct]
            out += ref[rb.offset - 7 : rb.offset + rb.block_size]
        else:
            out += r.data
    ref_index = _FI.final_index_ref(ref)
    out += ref[ref_index.start :]
    return _FI.reindex_after_resize(ref, bytes(out))


def _grow_blocks_from(data: bytes, ref: bytes, types) -> bytes:
    """Like `_transplant_top_level`, but for GROW-only changes that add NO index holes
    (tempo/meter/marker maps just enlarge existing session-global blocks). Swaps the
    `types` blocks from `ref` into `data`, keeps DATA's own master index, and reindexes
    against DATA — so `data` may have any track count independent of `ref` (the conductor
    is track-agnostic). `_transplant_top_level` reindexes against the ref, which corrupts
    the index when data's track count differs from ref's; this preserves data's holes."""
    types = set(types)
    ref_by_type = {b.content_type: b for b in parse(ref).blocks}
    for ct in types:
        if ct not in ref_by_type:
            raise ValueError(f"ref missing top-level block 0x{ct:04x}")
    data_refs = W.top_level_refs(data)
    if not data_refs or data_refs[-1].block.content_type != 0x0002:
        raise ValueError("data has no trailing 0x0002 master index")
    out = bytearray(data[: data_refs[0].start])
    for r in data_refs[:-1]:
        ct = r.block.content_type
        if ct in types:
            rb = ref_by_type[ct]
            out += ref[rb.offset - 7 : rb.offset + rb.block_size]
        else:
            out += r.data
    data_index = _FI.final_index_ref(data)
    out += data[data_index.start :]              # keep DATA's index (its track holes)
    return _FI.reindex_after_resize(data, bytes(out))


def _grow_blocks_into(data: bytes, blocks_by_type: "dict[int, bytes]") -> bytes:
    """Replace `data`'s top-level blocks (keyed by content_type) with the given new blocks
    (resized session-global blocks that add NO index holes — a tempo/meter/marker map), keep
    DATA's own master index, and reindex against DATA. The donor-free counterpart of
    `_grow_blocks_from`: the new blocks are supplied directly (built from inlined templates)
    instead of pulled from a reference session — so only the named blocks change and DATA
    keeps everything else (its own view-state, I/O, etc.)."""
    data_refs = W.top_level_refs(data)
    if not data_refs or data_refs[-1].block.content_type != 0x0002:
        raise ValueError("data has no trailing 0x0002 master index")
    present = {r.block.content_type for r in data_refs[:-1]}
    for ct in blocks_by_type:
        if ct not in present:
            raise ValueError(f"data missing top-level block 0x{ct:04x}")
    out = bytearray(data[: data_refs[0].start])
    for r in data_refs[:-1]:
        ct = r.block.content_type
        out += blocks_by_type[ct] if ct in blocks_by_type else r.data
    out += data[_FI.final_index_ref(data).start :]
    return _FI.reindex_after_resize(data, bytes(out))


def _grow_block_into(data: bytes, content_type: int, new_block: bytes) -> bytes:
    """Replace `data`'s single top-level block of `content_type` (see `_grow_blocks_into`)."""
    return _grow_blocks_into(data, {content_type: new_block})


# Session/hardware-config blocks (I/O routing/channels + a few session-state blocks)
# that Pro Tools may rewrite on save independently of any content. They never carry an
# audio/MIDI clip, so a content transplant must keep the TARGET's version — otherwise a
# reference saved in a different context would import its I/O routing. Excluding them is
# a no-op for a same-session control pair (they don't appear in that pair's delta).
_REF_VOLATILE_TYPES = frozenset({0x1022, 0x2603, 0x2033, 0x2058, 0x4823})


def add_audio_clip(data: bytes, clean_ref: bytes, clip_ref: bytes) -> bytes:
    """Add audio clip(s) to session `data` by transplanting the clip contribution from
    a matched control PAIR: `clean_ref` (a session WITHOUT the clip) and `clip_ref` (the
    SAME session WITH the clip(s) added). Mirrors `add_click`.

    Transplants every top-level block that differs between the pair (minus session
    identity, the index, and `_REF_VOLATILE_TYPES`): the name table 0x2519, the
    audio-file path index 0x0f3d, the file table 0x1004 (wav descriptor 0x1003 + the
    "Audio Files" 0x103a list), the region library 0x262a (the .L/.R 0x2629 regions),
    the placement table 0x1054 (per-lane 0x1050/0x104f placements — one set per clip),
    and the edit playlist 0x2624 (which carries each clip's timeline 3-point). The
    master index is repaired by `_transplant_top_level`; `data` keeps its own name.

    Because it copies whatever the pair differs by, `clip_ref` can hold ONE clip, MANY
    clips on a track (`two clips same track.ptx` -> 4 placements, reused regions), or a
    clip of a DIFFERENT wav (`clip diff wav.ptx`) — each is reproduced byte-exact (mod
    session-name) when `data` IS `clean_ref`. `data`/`clean_ref` must share audio layout
    so the blocks line up. Returns full unxored bytes (ready for `encrypt_session_data`).
    PT-CONFIRMED for 1 stereo + 1 clip."""
    types = [t for t in changed_top_level_types(clean_ref, clip_ref)
             if t not in _REF_VOLATILE_TYPES]
    return _transplant_top_level(data, clip_ref, types)


# Session-global, track-AGNOSTIC clip blocks: the file table (wav descriptor), the
# region library (the .L/.R regions) and the audio-file path index. Identical
# regardless of which track the clip lands on, so they transplant wholesale.
_CLIP_GLOBAL_TYPES = (0x1004, 0x262A, 0x0F3D)


def _clip_lane_tails(clip_ref: bytes) -> "tuple[bytes, bytes]":
    """The two clip-bearing 0x1052 lanes' bytes-after-name (channel 0 = .L, channel
    1 = .R), in lane order. These tails (`01 00 00 00` + a 0x1050/0x104f placement)
    are track-INDEPENDENT — only the lane's name differs between tracks, and that is
    kept from the target lane. Pulled from any 1-clip session's clip lanes."""
    import struct
    p = parse(clip_ref)
    lanes = [b for b in flat_blocks(p)
             if b.content_type == 0x1052 and any(c.content_type == 0x1050 for c in b.child)]
    if len(lanes) < 2:
        raise ValueError("clip_ref has no stereo clip lane to copy")

    def tail(lane: Block) -> bytes:
        lb = block_bytes(clip_ref, lane)
        namelen = struct.unpack_from("<I", lb, 9)[0]
        return lb[13 + namelen:]
    return tail(lanes[0]), tail(lanes[1])


def _insert_clip_lanes(b1054: bytes, track_index: int, tail0: bytes, tail1: bytes) -> bytes:
    """Rebuild a 0x1054 placement block, turning track `track_index`'s two empty
    0x1052 lanes into clip lanes (keep each lane's name, swap its after-name tail for
    the clip tail). Lanes are track-major (2 per stereo track); block_size is bumped."""
    import struct
    out = bytearray(b1054[:9])  # zmark + btype + size + content_type
    i, lane = 9, 0
    while i < len(b1054):
        if b1054[i] != 0x5A:
            out += b1054[i:i + 1]; i += 1; continue
        csize = struct.unpack_from("<I", b1054, i + 3)[0]
        cct = struct.unpack_from("<H", b1054, i + 7)[0]
        child = b1054[i:i + 7 + csize]
        if cct == 0x1052:
            if lane in (2 * track_index, 2 * track_index + 1):
                namelen = struct.unpack_from("<I", child, 9)[0]
                tail = tail0 if lane == 2 * track_index else tail1
                newc = bytearray(child[:13 + namelen] + tail)
                struct.pack_into("<I", newc, 3, len(newc) - 7)
                child = bytes(newc)
            lane += 1
        out += child
        i += 7 + csize
    struct.pack_into("<I", out, 3, len(out) - 7)
    return bytes(out)


def _synth_clip_index(clean_data: bytes, target_body: bytes) -> bytes:
    """Synthesize the 0x0002 index for a clip-bearing body from a CLIP-FREE session's
    records. PT-CONFIRMED that a clip adds exactly one index hole: a 0x0f3c marker
    (the path-volume pointer) appended to the single 0x0f3d path record; no existing
    indexed block changes rank. So: take `clean_data`'s records, refill every offset
    against `target_body`'s layout (resolving each hole's type by the block it points
    at, K-independent), then append the 0x0f3c marker. Reproduces the real control's
    index byte-exact."""
    import copy
    cref = _FI.final_index_ref(clean_data)
    czt, cbt = _FI.block_layout(clean_data)
    crecs = _FI.parse_records(cref.data, set(cbt), set(czt))
    _tzt, tbt = _FI.block_layout(target_body)
    recs = copy.deepcopy(crecs)

    def refill(off: int) -> int:
        t = czt.get(off)
        return tbt[t][cbt[t].index(off)] if t is not None else off

    for r in recs:
        for c in r.child_refs:
            c.offset = refill(c.offset)
        for e in r.elements:
            e.offsets = [refill(o) for o in e.offsets]
    path_records = [r for r in recs if r.content_type == 0x0F3D]
    if len(path_records) != 1:
        raise ValueError(f"expected one 0x0f3d index record, found {len(path_records)}")
    path_records[0].elements.append(_FI.Element(rel=0, tag1=0x04, offsets=[tbt[0x0F3C][0]]))
    return _FI.serialize_final_block(recs)


def add_clip_to_track(data: bytes, clip_ref: bytes, track_index: int,
                      *, position_file_samples: "int | None" = None) -> bytes:
    """Insert ONE stereo audio clip onto track `track_index` (0-based) of the clean
    (clip-free) N-stereo session `data`, generalizing clip placement to ANY track of
    ANY N. `clip_ref` is any 1-clip session (e.g. `one clip bar 2.ptx`) supplying the
    track-agnostic wav/region/path blocks and the placement lane content.

    Recipe (PT-CONFIRMED on tracks 1/2/3 of a 3-stereo): transplant the session-global
    clip blocks (`_CLIP_GLOBAL_TYPES`) from `clip_ref`; turn track K's two 0x1052 lanes
    into clip lanes (`_insert_clip_lanes`, channel 0/.L and 1/.R by lane order); and
    SYNTHESIZE the master index (`_synth_clip_index`) — the placement plus that index
    are the entire load-bearing footprint (the 0x2519 clip flag and 0x2624 position
    block are display state Pro Tools rebuilds). The clip's timeline start lives solely
    in the 0x104f field, so pass `position_file_samples` to place it (else it inherits
    `clip_ref`'s). `data` must be clip-FREE (single clip); returns full unxored bytes."""
    import struct
    ref_by = {b.content_type: b for b in parse(clip_ref).blocks}
    for ct in _CLIP_GLOBAL_TYPES:
        if ct not in ref_by:
            raise ValueError(f"clip_ref missing global clip block 0x{ct:04x}")
    tail0, tail1 = _clip_lane_tails(clip_ref)

    refs = W.top_level_refs(data)
    if not refs or refs[-1].block.content_type != 0x0002:
        raise ValueError("data has no trailing 0x0002 master index")
    body = bytearray(data[: refs[0].start])
    for r in refs[:-1]:  # body blocks (exclude the trailing index)
        ct = r.block.content_type
        if ct in _CLIP_GLOBAL_TYPES:
            rb = ref_by[ct]
            body += clip_ref[rb.offset - 7 : rb.offset + rb.block_size]
        elif ct == 0x1054:
            body += _insert_clip_lanes(r.data, track_index, tail0, tail1)
        else:
            body += r.data
    body = bytes(body)
    out = body + _synth_clip_index(data, body)
    out = _FI._set_first_block_index_pointer(out, len(body))
    if position_file_samples is not None:
        out = set_clip_position(out, position_file_samples)
    return out


def _count_block_parts(data: bytes, ct: int, child_ct: int):
    """Split a count-prefixed table block (0x1004 file table / 0x262a region lib) into
    (header9, prefix, [child_item_bytes], trailer). The block content is a u32 count at
    [0] + a prefix (list/header) + the repeated `child_ct` items + a trailer; the parser
    skips the count/inter-section u32s, so we capture them as prefix/trailer spans."""
    import struct
    blk = [b for b in parse(data).blocks if b.content_type == ct][0]
    items = [c for c in blk.child if c.content_type == child_ct]
    cstart = blk.offset + 2  # content starts after the 2-byte content_type
    first = (items[0].offset - 7) - cstart
    last = (items[-1].offset + items[-1].block_size) - cstart
    content = block_bytes(data, blk)[9:]
    return block_bytes(data, blk)[:9], content[:first], [block_bytes(data, it) for it in items], content[last:]


def _rebuild_count_block(header9: bytes, prefix: bytes, items, trailer: bytes, count: int) -> bytes:
    """Reassemble a count-prefixed table block from parts with `count` at content[0]."""
    import struct
    content = bytearray(prefix) + b"".join(items) + trailer
    struct.pack_into("<I", content, 0, count)
    out = bytearray(header9) + content
    struct.pack_into("<I", out, 3, len(out) - 7)
    return bytes(out)


def _all_clip_lane_tails(clip_ref: bytes) -> "list[bytes]":
    """Every clip-bearing 0x1052 lane's bytes-after-name, in lane order
    (clip0 .L, clip0 .R, clip1 .L, clip1 .R, ...). Each tail's 0x104f payload[2] is the
    region index it binds to (0,1,2,…) — the linkage is positional, no GUIDs."""
    import struct
    out = []
    for L in (b for b in flat_blocks(parse(clip_ref))
              if b.content_type == 0x1052 and any(c.content_type == 0x1050 for c in b.child)):
        lb = block_bytes(clip_ref, L)
        out.append(lb[13 + struct.unpack_from("<I", lb, 9)[0]:])
    return out


def _place_clip_lanes(b1054: bytes, assignments, tails) -> bytes:
    """Rebuild a 0x1054 placing clip `ci` (tails[2ci],tails[2ci+1]) on track
    `assignments[ci]`'s two lanes. Each tail keeps its region-index (payload[2])."""
    import struct
    track_tail = {t: (tails[2 * ci], tails[2 * ci + 1]) for ci, t in enumerate(assignments)}
    out = bytearray(b1054[:9])
    i, lane = 9, 0
    while i < len(b1054):
        if b1054[i] != 0x5A:
            out += b1054[i:i + 1]; i += 1; continue
        csize = struct.unpack_from("<I", b1054, i + 3)[0]
        cct = struct.unpack_from("<H", b1054, i + 7)[0]
        child = b1054[i:i + 7 + csize]
        if cct == 0x1052:
            tk = lane // 2
            if tk in track_tail:
                namelen = struct.unpack_from("<I", child, 9)[0]
                newc = bytearray(child[:13 + namelen] + track_tail[tk][lane % 2])
                struct.pack_into("<I", newc, 3, len(newc) - 7)
                child = bytes(newc)
            lane += 1
        out += child
        i += 7 + csize
    struct.pack_into("<I", out, 3, len(out) - 7)
    return bytes(out)


def add_clips_to_tracks(data: bytes, clip_ref: bytes, track_indices,
                        *, position_file_samples: "int | None" = None) -> bytes:
    """Place MULTIPLE stereo clips — one per entry of `track_indices` (0-based) — onto
    the clean N-stereo session `data`, in one shot. `clip_ref` is a session whose M
    clips supply the wav/region/path content; `len(track_indices)` must equal M, and
    clip i lands on track `track_indices[i]`.

    PT-CONFIRMED via the `3 stereo 3 different clips.ptx` byte-pair. Multi-clip is the
    single-clip recipe scaled: the file table 0x1004 (count + filename list + M wav
    descriptors), the region library 0x262a (2M regions), and the placements 0x1054
    (each clip's two lanes carry region index 2i / 2i+1 via 0x104f payload[2] — the
    region<->placement link is POSITIONAL, no GUIDs) are assembled from `clip_ref`; all
    M clips SHARE ONE 0x0f3d path-volume, so the index still gains exactly ONE 0x0f3c
    marker (`_synth_clip_index` is unchanged from the single-clip case). `data` must be
    clip-FREE. `position_file_samples` places the clips: an int moves them ALL to one
    position (the head-sync case); a list/tuple (one entry per clip, parallel to
    `track_indices`) gives each clip its OWN position (each clip's start is an
    independent 0x104f field). Returns full unxored bytes.

    Reproduces the control's load-bearing blocks (0x1004/0x262a/0x0f3d/0x1054/index)
    byte-exact; 0x2519/0x2624/0x2016/0x2587 are display state Pro Tools rebuilds."""
    cp = parse(clip_ref)
    h4, pre4, units, tr4 = _count_block_parts(clip_ref, 0x1004, 0x1003)
    h2a, pre2a, regs, tr2a = _count_block_parts(clip_ref, 0x262A, 0x2629)
    m = len(units)
    if len(track_indices) != m:
        raise ValueError(f"clip_ref has {m} clips but {len(track_indices)} tracks given")
    tails = _all_clip_lane_tails(clip_ref)
    ref_by = {b.content_type: b for b in cp.blocks}
    if 0x0F3D not in ref_by:
        raise ValueError("clip_ref missing the 0x0f3d path block")
    f3d_block = block_bytes(clip_ref, ref_by[0x0F3D])
    blk1004 = _rebuild_count_block(h4, pre4, units, tr4, m)
    blk262a = _rebuild_count_block(h2a, pre2a, regs, tr2a, 2 * m)

    refs = W.top_level_refs(data)
    if not refs or refs[-1].block.content_type != 0x0002:
        raise ValueError("data has no trailing 0x0002 master index")
    body = bytearray(data[: refs[0].start])
    for r in refs[:-1]:
        ct = r.block.content_type
        if ct == 0x1004:
            body += blk1004
        elif ct == 0x262A:
            body += blk262a
        elif ct == 0x0F3D:
            body += f3d_block
        elif ct == 0x1054:
            body += _place_clip_lanes(r.data, list(track_indices), tails)
        else:
            body += r.data
    body = bytes(body)
    out = body + _synth_clip_index(data, body)
    out = _FI._set_first_block_index_pointer(out, len(body))
    if position_file_samples is not None:
        if isinstance(position_file_samples, (list, tuple)):
            if len(position_file_samples) != m:
                raise ValueError(f"{len(position_file_samples)} positions for {m} clips")
            out = _set_clip_positions(out, {track_indices[i]: position_file_samples[i]
                                            for i in range(m)})
        else:
            out = set_clip_position(out, position_file_samples)
    return out


def _set_clip_positions(data: bytes, track_to_pos: "dict[int, int]") -> bytes:
    """Set each given track's clip to its own timeline position (FILE samples). Finds
    track K's two 0x1052 lanes by name ("Audio K+1") and writes the 0x104f position
    field (payload[7:15]) of each — so different clips can sit at different positions."""
    import struct
    out = bytearray(data)
    for lane in flat_blocks(parse(bytes(out))):
        if lane.content_type != 0x1052:
            continue
        lb = block_bytes(bytes(out), lane)
        namelen = struct.unpack_from("<I", lb, 9)[0]
        name = lb[13:13 + namelen].decode("latin1", "replace")
        if not name.startswith("Audio "):
            continue
        try:
            track = int(name.split()[1]) - 1
        except (IndexError, ValueError):
            continue
        if track not in track_to_pos:
            continue
        field = struct.pack("<Q", int(track_to_pos[track]))
        for gain in lane.child:
            for plc in gain.child:
                if plc.content_type == 0x104F:
                    out[plc.offset + 9 : plc.offset + 17] = field
    return bytes(out)


def wav_clip_identity(wav_path) -> "tuple[int, bytes, bytes]":
    """Read the Pro-Tools-relevant identity from a BWF/PT WAV:
    (sample_count, umid_material, id2). `sample_count` = data-chunk bytes / fmt block
    align. `umid_material` = the 8-byte `umid` chunk body `2a <hash4> ef <B> 80` (the
    file's content ID Pro Tools matches on). `id2` = the 2 bytes at the bext SMPTE-UMID
    marker (`06 0a 2b 34`) + 24. Raises if the WAV lacks `fmt`/`data`/`umid`/`bext`
    (a raw WAV with no BWF UMID can't be linked without first wrapping it)."""
    import struct
    data = open(wav_path, "rb").read()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError(f"not a RIFF/WAVE file: {wav_path}")
    chunks, i = {}, 12
    while i + 8 <= len(data):
        cid = data[i:i + 4]; sz = struct.unpack("<I", data[i + 4:i + 8])[0]
        chunks[cid] = (i + 8, sz); i += 8 + sz + (sz & 1)
    for need in (b"fmt ", b"data", b"umid", b"bext"):
        if need not in chunks:
            raise ValueError(f"WAV has no {need.decode().strip()!r} chunk (not a PT/BWF WAV): {wav_path}")
    fo, _ = chunks[b"fmt "]; block_align = struct.unpack("<H", data[fo + 12:fo + 14])[0]
    _do, dsz = chunks[b"data"]; sample_count = dsz // max(1, block_align)
    uo, _ = chunks[b"umid"]; umid_material = data[uo + 7:uo + 15]
    bo, _ = chunks[b"bext"]; bext = data[bo:bo + 602]
    m = bext.find(bytes.fromhex("060a2b34"))
    id2 = bext[m + 24:m + 26]
    return sample_count, umid_material, id2


def _wav_chunks(data: bytes) -> "list[tuple[bytes, int, int]]":
    out, i = [], 12
    while i + 8 <= len(data):
        import struct
        cid = data[i:i + 4]; sz = struct.unpack("<I", data[i + 4:i + 8])[0]
        out.append((cid, i + 8, sz)); i += 8 + sz + (sz & 1)
    return out


def wrap_raw_wav(raw_data: bytes, template_wav: bytes, *, seed: str = "ptformat") -> bytes:
    """Wrap a RAW WAV (one with only `fmt`/`data`, no Pro Tools BWF identity) into a
    PT-friendly container so `set_clip_wav` can link it. Grafts the raw audio into
    `template_wav`'s chunk structure (a known-good PT/BWF WAV) and writes a freshly
    generated SMPTE-UMID — deterministic from `seed` + the audio — consistently into the
    `umid`, `regn`, and `bext` chunks (and the frame count into `regn`). Returns the
    wrapped WAV bytes; stage them in the session `Audio Files` folder, then pass to
    `set_clip_wav` (which reads back the generated identity).

    The raw WAV must match the template's PCM format (the clip controls are 44.1 kHz /
    24-bit / stereo); other sample rates / bit depths / channel counts need conversion
    first. The generated UMID is content+seed-derived so it is stable and effectively
    unique. NOTE: a wrapped stereo clip currently maps to one channel pending the
    stereo-lane channel decode."""
    import struct, hashlib
    rc = {cid: (o, sz) for cid, o, sz in _wav_chunks(raw_data)}
    for need in (b"fmt ", b"data"):
        if need not in rc:
            raise ValueError(f"raw WAV has no {need.decode().strip()!r} chunk")
    raw_fmt = raw_data[rc[b"fmt "][0]:rc[b"fmt "][0] + rc[b"fmt "][1]]
    rdo, rdsz = rc[b"data"]; pcm = raw_data[rdo:rdo + rdsz]
    block_align = struct.unpack("<H", raw_fmt[12:14])[0]
    frames = rdsz // max(1, block_align)

    h = hashlib.sha256(seed.encode("utf-8", "surrogateescape") + pcm[:4096]).digest()
    hash4, b_byte, id2 = h[0:4], bytes([h[4] & 0x7F or 1]), h[5:7]
    material = b"\x2a" + hash4 + b"\xef" + b_byte + b"\x80"  # 0x1001/regn umid material

    body = bytearray()
    for cid, o, sz in _wav_chunks(template_wav):
        chunk = bytearray(template_wav[o:o + sz])
        if cid == b"data":
            chunk = bytearray(pcm)
        elif cid == b"fmt ":
            chunk = bytearray(raw_fmt)
        elif cid == b"umid":
            chunk[7:15] = material
        elif cid == b"regn":
            chunk[11:19] = material
            chunk[28:32] = int(frames).to_bytes(4, "little")
        elif cid == b"bext":
            mk = chunk.find(bytes.fromhex("060a2b34"))
            chunk[mk + 16:mk + 20] = hash4; chunk[mk + 21:mk + 22] = b_byte; chunk[mk + 24:mk + 26] = id2
        body += cid + struct.pack("<I", len(chunk)) + chunk
        if len(chunk) & 1:
            body += b"\x00"
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + bytes(body)


def set_clip_wav(clip_data: bytes, wav_path, *, filename: "str | None" = None,
                 region_name: "str | None" = None) -> bytes:
    """Re-point the single audio clip in `clip_data` (a 1-stereo session with one clip,
    e.g. `one clip bar 1.ptx`) at an arbitrary Pro-Tools/BWF WAV, so Pro Tools opens it
    and AUTO-LINKS to that WAV with no relink prompt. The WAV must be staged in the
    session's `Audio Files` folder under `filename` (default: the WAV's basename); Pro
    Tools names the region after the filename stem (override with `region_name`).

    What it patches (the full set Pro Tools resolves files by — PT-CONFIRMED):
      * 0x103a filename + 0x2628 region names (`<stem>.L` / `.R`)
      * 0x1001 length + 0x2628 region length = the WAV's frame count
      * the WAV's umid material (`wav_clip_identity`): in 0x1003 the 0x1001 copy keeps
        its `2a` marker (offset +44), but the 0x2106 copy is `00`-prefixed (offset +292,
        NOT `2a` — that stray byte is the subtle relink bug); the 2-byte id at +301
    Per-file timestamp (0x1003 +100/+172) and private file id (+202) do NOT gate linking
    (Pro Tools links the same WAV regardless), so they keep the donor's values and need
    no WAV mtime coherence. Returns full unxored bytes (ready for `encrypt_session_data`).

    NOTE: only PT/BWF WAVs (with a `umid`/`bext` UMID) can be linked this way; a raw WAV
    has no fingerprint to match and would need to be wrapped/imported first."""
    sample_count, umid_material, id2 = wav_clip_identity(wav_path)
    import os
    if filename is None:
        filename = os.path.basename(str(wav_path))
    stem = region_name if region_name is not None else filename.rsplit(".wav", 1)[0]

    parsed = parse(clip_data)
    # detect the existing clip's filename (0x103a audio entry) and region stem (0x2628)
    ft = [b for b in parsed.blocks if b.content_type == 0x1004][0]
    old_filename = None
    for ch in ft.child:
        if ch.content_type != 0x103A or ch.block_size <= 10:
            continue
        body = clip_data[ch.offset : ch.offset + ch.block_size]
        m = re.search(rb"([ -~]{4,})\.wav", body)
        if m:
            old_filename = m.group(0).decode("latin1"); break
    region = [b for b in flat_blocks(parsed) if b.content_type == 0x2628]
    rb = clip_data[region[0].offset - 7 : region[0].offset + region[0].block_size]
    rl = int.from_bytes(rb[9:13], "little")
    old_stem = rb[13:13 + rl].decode("latin1").rsplit(".", 1)[0]  # drop the ".L"
    if old_filename is None:
        raise ValueError("clip_data has no audio file entry to re-point")

    allb = flat_blocks(parsed)
    edits, size_delta = [], {}

    def _replace(old: str, new: str) -> None:
        ob, nb, i = old.encode(), new.encode(), 0
        while True:
            j = clip_data.find(ob, i)
            if j < 0:
                break
            if j >= 4 and int.from_bytes(clip_data[j - 4:j], "little") == len(ob):
                edits.append((j - 4, 4 + len(ob), len(nb).to_bytes(4, "little") + nb))
                for b in allb:
                    if b.offset - 7 <= j < b.offset + b.block_size:
                        size_delta[b.offset - 7] = size_delta.get(b.offset - 7, 0) + (len(nb) - len(ob))
            i = j + 1

    _replace(old_filename, filename)
    _replace(old_stem + ".L", stem + ".L")
    _replace(old_stem + ".R", stem + ".R")
    for z, d in size_delta.items():
        edits.append((z + 3, 4, (int.from_bytes(clip_data[z + 3:z + 7], "little") + d).to_bytes(4, "little")))
    for b in allb:
        if b.content_type == 0x1001:
            edits.append((b.offset - 7 + 15, 4, int(sample_count).to_bytes(4, "little")))
        if b.content_type == 0x2628:
            s = b.offset - 7; nl = int.from_bytes(clip_data[s + 9:s + 13], "little")
            edits.append((s + 13 + nl + 5, 2, int(sample_count).to_bytes(2, "little")))
    out = bytearray(clip_data)
    for pos, oldlen, nb in sorted(edits, key=lambda e: e[0], reverse=True):
        out[pos:pos + oldlen] = nb
    out = bytearray(_FI.reindex_after_resize(clip_data, bytes(out)))
    # umid identity (fixed-size): 0x1001 copy keeps 0x2a marker @+44, 0x2106 copy is
    # 00-prefixed @+292, 2-byte id @+301
    for b in [x for x in flat_blocks(parse(bytes(out))) if x.content_type == 0x1003]:
        z = b.offset - 7
        out[z + 44:z + 52] = umid_material
        out[z + 292:z + 300] = b"\x00" + umid_material[1:]
        out[z + 301:z + 303] = id2
    return bytes(out)


def _audio_filenames(data: bytes) -> "list[str]":
    """The session's audio filenames in 0x103a-list order (== 0x1003 wav order). Parses
    the length-prefixed entries (`02 00 00 00 00 <len:4> <name> EVAW`) so any stem
    length works (a regex with a min length would miss e.g. `01.wav`)."""
    import struct
    out = []
    for b in flat_blocks(parse(data)):
        if b.content_type != 0x103A or b.block_size <= 10:
            continue
        blk = data[b.offset:b.offset + b.block_size]
        anchor = blk.find(b"Audio Files")
        if anchor < 0:
            continue
        i = anchor + len(b"Audio Files") + 4  # skip name + the trailing u32
        while i + 9 <= len(blk):
            if blk[i:i + 4] != b"\x02\x00\x00\x00":
                break
            nlen = struct.unpack_from("<I", blk, i + 5)[0]
            name = blk[i + 9:i + 9 + nlen]
            out.append(name.decode("latin1", "replace"))
            i += 9 + nlen + 4  # name + the trailing "EVAW" marker (next entry's 02 follows)
    return out


def _repoint_clip(data: bytes, old_stem: str, wav_path, filename=None) -> bytes:
    """Re-point ONE clip (identified by its current region stem) to `wav_path`, leaving
    the session's other clips untouched. Mirrors set_clip_wav but targets only this
    clip's region names, the matching filename, that clip's two 0x2628 region lengths,
    and the single 0x1003 wav descriptor at its 0x103a-list position. Reindexes once."""
    import os, struct
    sample_count, umid_material, id2 = wav_clip_identity(wav_path)
    if filename is None:
        filename = os.path.basename(str(wav_path))
    stem = filename.rsplit(".wav", 1)[0]
    old_filename = old_stem + ".wav"
    wav_idx = _audio_filenames(data).index(old_filename)  # which 0x1003 (0x103a order)
    allb = flat_blocks(parse(data))
    edits, size_delta = [], {}

    def _replace(old: str, new: str) -> None:
        ob, nb, i = old.encode(), new.encode(), 0
        while True:
            j = data.find(ob, i)
            if j < 0:
                break
            if j >= 4 and int.from_bytes(data[j - 4:j], "little") == len(ob):
                edits.append((j - 4, 4 + len(ob), len(nb).to_bytes(4, "little") + nb))
                for b in allb:
                    if b.offset - 7 <= j < b.offset + b.block_size:
                        size_delta[b.offset - 7] = size_delta.get(b.offset - 7, 0) + (len(nb) - len(ob))
            i = j + 1

    _replace(old_filename, filename)
    _replace(old_stem + ".L", stem + ".L")
    _replace(old_stem + ".R", stem + ".R")
    for z, d in size_delta.items():
        edits.append((z + 3, 4, (int.from_bytes(data[z + 3:z + 7], "little") + d).to_bytes(4, "little")))
    for b in allb:  # this clip's two regions (by current name) get the new frame count
        if b.content_type == 0x2628:
            s = b.offset - 7
            nl = int.from_bytes(data[s + 9:s + 13], "little")
            if data[s + 13:s + 13 + nl].decode("latin1", "replace") in (old_stem + ".L", old_stem + ".R"):
                edits.append((s + 13 + nl + 5, 2, int(sample_count).to_bytes(2, "little")))
    target = [b for b in allb if b.content_type == 0x1003][wav_idx]
    for g in target.child:
        if g.content_type == 0x1001:
            edits.append((g.offset - 7 + 15, 4, int(sample_count).to_bytes(4, "little")))
    out = bytearray(data)
    for pos, oldlen, nb in sorted(edits, key=lambda e: e[0], reverse=True):
        out[pos:pos + oldlen] = nb
    out = bytearray(_FI.reindex_after_resize(data, bytes(out)))
    t1003 = [b for b in flat_blocks(parse(bytes(out))) if b.content_type == 0x1003][wav_idx]
    z = t1003.offset - 7
    out[z + 44:z + 52] = umid_material
    out[z + 292:z + 300] = b"\x00" + umid_material[1:]
    out[z + 301:z + 303] = id2
    return bytes(out)


def set_clip_wavs(data: bytes, wav_paths) -> bytes:
    """Re-point EACH clip of a multi-clip session to its own WAV, in region/track order
    (clip i, on regions 2i/2i+1, gets `wav_paths[i]`). Generalizes `set_clip_wav` to N
    clips: re-points one clip at a time (each a single-clip rename + reindex on the
    updated bytes), so the clips' file table / regions / placements all stay consistent.
    Each WAV must be a staged PT/BWF WAV. Returns full unxored bytes."""
    out = data
    for i, wav in enumerate(wav_paths):
        if wav is None:
            continue
        # the current stem of clip i = region 2i's name minus the ".L"
        regs = [b for b in flat_blocks(parse(out)) if b.content_type == 0x2628]
        import struct
        rb = out[regs[2 * i].offset - 7:regs[2 * i].offset + regs[2 * i].block_size]
        nl = struct.unpack_from("<I", rb, 9)[0]
        old_stem = rb[13:13 + nl].decode("latin1", "replace").rsplit(".", 1)[0]
        out = _repoint_clip(out, old_stem, wav)
    return out


def _region_template_name(region_2629: bytes) -> str:
    import struct
    # the 0x2628 name child sits at +9; name = <namelen:4><name> at +9+9
    nl = struct.unpack_from("<I", region_2629, 9 + 9)[0]
    return region_2629[9 + 13:9 + 13 + nl].decode("latin1", "replace")


def _build_region(template_2629: bytes, full_name: str, channel: int,
                  sample_count: int, guid16: bytes, findex: int) -> bytes:
    """Build one 0x2629 region from a `.L` template. Sets, then renames to `full_name`:
      * findex - the region->wav file index (0-based into the 0x103a file list), stored
        TWICE: once as a 4-byte value at the end of the 0x2628 name sub-block (offset
        16 + the 0x2628 size; the region-list/display copy the parser reads) and once in
        the region's fixed trailer (at len-8; the copy PT resolves for PLAYBACK). Setting
        only the first gives correct region names but plays the template's wav on every
        track (the 'all 8 tracks play bass' bug).
      * length - written as a VARIABLE-WIDTH three-point value (Ardour-style): the
        `lengthbytes` high-nibble at region offset (24 + namelen) selects a 2..4-byte
        little-endian sample count placed at (27 + namelen). A fixed 2-byte width
        truncates any clip over 65535 samples (~1.5 s) to its low 16 bits.
      * channel (@+78 in the 6-char template layout) and the region GUID (@+97).
    Only name/channel/length/GUID/findex gate linking; the rest is kept from the template.
    The fixed-offset fields are written on the template layout first, then carried through
    the length-widen and rename edits (each shifts only trailing bytes). Sizes recomputed."""
    import struct
    r = bytearray(template_2629)
    old = _region_template_name(template_2629).encode()
    L0 = len(old)
    # fixed-offset fields on the template (6-char-name) layout
    r[78] = channel
    r[97:113] = guid16
    struct.pack_into("<I", r, 16 + struct.unpack_from("<I", r, 12)[0], int(findex) & 0xFFFFFFFF)
    # widen the three-point length field to hold the full sample count
    nibble_pos, val_pos = 24 + L0, 27 + L0
    sc = max(0, int(sample_count)) & 0xFFFFFFFF
    w = max(2, (sc.bit_length() + 7) // 8) if sc else 2
    r[nibble_pos] = (w << 4) | (r[nibble_pos] & 0x0F)
    r = r[:val_pos] + bytearray(sc.to_bytes(w, "little")) + r[val_pos + 2:]
    struct.pack_into("<I", r, 12, struct.unpack_from("<I", r, 12)[0] + (w - 2))  # 0x2628 size
    # rename (shifts the three-point + trailing fields; their values are already written)
    new = full_name.encode()
    j = r.find(old)
    while j >= 0 and not (j >= 4 and int.from_bytes(r[j - 4:j], "little") == len(old)):
        j = r.find(old, j + 1)
    r = bytearray(r[:j - 4] + len(new).to_bytes(4, "little") + new + r[j + len(old):])
    struct.pack_into("<I", r, 12, struct.unpack_from("<I", r, 12)[0] + (len(new) - len(old)))  # 0x2628
    struct.pack_into("<I", r, 3, len(r) - 7)                                                    # 0x2629
    struct.pack_into("<I", r, len(r) - 8, int(findex) & 0xFFFFFFFF)  # playback findex (trailer copy)
    return bytes(r)


def _file_filetime(path) -> int:
    """A file's mtime as a Windows FILETIME (100-ns ticks since 1601-01-01 UTC) — the value
    Pro Tools stamps into the 0x1003 descriptor as the source-file timestamp."""
    import os
    return round((os.path.getmtime(path) + 11644473600) * 10_000_000)


def _build_wav_descriptor(template_1003: bytes, index: int, sample_count: int,
                          umid: bytes, id2: bytes, mtime_ft: "int | None" = None) -> bytes:
    """Build one 0x1003 wav descriptor: set the wav ordinal (@+9), the sample count (in
    the 0x1001 child @+15), and the umid identity (0x1001 copy @+44 with 2a, 0x2106 copy
    @+292 00-prefixed, 2-byte id @+301) — the same fields set_clip_wav patches.

    When `mtime_ft` (the source WAV's mtime as a Windows FILETIME) is given, also stamp the
    file-timestamp fields the way a fresh Pro Tools IMPORT does — @+100 and @+172 = the file
    mtime, @+259 = 0, and the @+182 flag = 1 (a clean clip template carries stale 2017-era
    timestamps + flag 0). PT keys waveform-overview-cache staleness on these; matching a real
    import is what lets PT build the overview on first open instead of needing a manual
    'Recalculate Waveform Overviews' (the cache itself lives in the session's WaveCache.wfm,
    keyed by UMID)."""
    import struct
    u = bytearray(template_1003)
    u[9] = index
    i = 9
    while i + 9 <= len(u):
        if u[i] == 0x5A and struct.unpack_from("<H", u, i + 7)[0] == 0x1001:
            struct.pack_into("<I", u, i + 15, sample_count)
            break
        i += 1
    u[44:52] = umid
    u[292:300] = b"\x00" + umid[1:]
    u[301:303] = id2
    if mtime_ft is not None:
        struct.pack_into("<Q", u, 100, int(mtime_ft))   # source-WAV mtime (PT overview-staleness key)
        struct.pack_into("<Q", u, 172, int(mtime_ft))   # second mtime copy
        struct.pack_into("<Q", u, 259, 0)               # PT zeros this on a fresh import
        u[182] = 1                                      # flag PT sets on import (template = 0)
    return bytes(u)


def _filename_list_trailer(clip_ref: bytes) -> bytes:
    """The bytes AFTER the filename entries in `clip_ref`'s 0x103a list: a
    `00 ffffffff` marker + the session folder's path components (Dropbox SSD / … /
    lots of stereo tracks). Folder-specific but filename/count-INDEPENDENT, so it
    transplants verbatim. Omitting it is the 'end of stream' bug."""
    import struct
    for b in flat_blocks(parse(clip_ref)):
        if b.content_type == 0x103A and b.block_size > 10:
            blk = block_bytes(clip_ref, b)
            anchor = blk.find(b"Audio Files")
            if anchor < 0:
                continue
            i = anchor + len(b"Audio Files") + 4
            while i + 9 <= len(blk) and blk[i:i + 4] == b"\x02\x00\x00\x00":
                nlen = struct.unpack_from("<I", blk, i + 5)[0]
                i += 9 + nlen + 4
            return blk[i:]
    return b""


def _renumber_path_trailer(trailer: bytes, d: int) -> bytes:
    """The folder-path trailer numbers its components in CONTINUATION of the D filename
    entries: component indices run D+1, D+2, … (the control's verbatim indices are only
    correct at its own D, else Pro Tools `out_of_range`s indexing them). Renumber for D.
    Trailer = `00 ffffffff <volname-len:4> <volname> <volID:4>` then components
    `01 <idx:4> <len:4> <name> 0000`."""
    import struct
    out = bytearray(trailer)
    if out[:5] != b"\x00\xff\xff\xff\xff":
        return trailer
    i = 5
    vnl = struct.unpack_from("<I", out, i)[0]
    i += 4 + vnl + 4  # volname-len + volname + volID
    idx = d + 1
    while i + 9 <= len(out) and out[i] == 0x01:
        struct.pack_into("<I", out, i + 1, idx)
        idx += 1
        clen = struct.unpack_from("<I", out, i + 5)[0]
        i += 1 + 4 + 4 + clen + 4
    return bytes(out)


def _build_filename_list(filenames: "list[str]", trailer: bytes) -> bytes:
    """Build the 0x103a 'Audio Files' list block for `filenames`. Header counts scale
    with D = len(filenames): `<8+D:u32> 01 <7+D:u32> <11> 'Audio Files' 0000` then each
    entry `02 00 00 00 00 <len:4> <name> EVAW`, then the folder-path `trailer` (its
    component indices renumbered to continue from D)."""
    import struct
    d = len(filenames)
    content = bytearray()
    content += (8 + d).to_bytes(4, "little") + b"\x01" + (7 + d).to_bytes(4, "little")
    content += (11).to_bytes(4, "little") + b"Audio Files" + b"\x00\x00\x00\x00"
    for fn in filenames:
        fb = fn.encode()
        content += b"\x02\x00\x00\x00\x00" + len(fb).to_bytes(4, "little") + fb + b"EVAW"
    content += _renumber_path_trailer(trailer, d)
    out = bytearray(b"\x5a\x01\x00") + (len(content) + 2).to_bytes(4, "little") + b"\x3a\x10" + content
    return bytes(out)


def _build_placement_lanes(b1054: bytes, track_clips: "dict", tail_template: bytes) -> bytes:
    """Rebuild a 0x1054 placing each track's clips. `track_clips[t]` = list of
    (region_index, position); each track lane gets that many 0x1050 placements (the lane
    count byte = K), each 0x104f's region/channel index (payload[2] = 2*region_index +
    channel) and position (payload[7:15]) patched. `tail_template` = one clip lane tail
    (`01 00 00 00` + a 0x1050{0x104f})."""
    import struct
    # tail_template = <count:4> <0x1050 block> <2-byte lane trailer>. Split the placement
    # block from the trailer: the trailer goes ONCE at the lane end, not per placement.
    plc_size = struct.unpack_from("<I", tail_template, 4 + 3)[0]
    plc_block = tail_template[4:4 + 7 + plc_size]
    lane_trailer = tail_template[4 + 7 + plc_size:]
    out = bytearray(b1054[:9])
    i, lane = 9, 0
    while i < len(b1054):
        if b1054[i] != 0x5A:
            out += b1054[i:i + 1]; i += 1; continue
        csz = struct.unpack_from("<I", b1054, i + 3)[0]
        cct = struct.unpack_from("<H", b1054, i + 7)[0]
        child = b1054[i:i + 7 + csz]
        if cct == 0x1052:
            track, chan = lane // 2, lane % 2
            if track in track_clips and track_clips[track]:
                nl = struct.unpack_from("<I", child, 9)[0]
                placements = bytearray()
                for region_index, pos in track_clips[track]:
                    plc = bytearray(plc_block)
                    fi = plc.find(b"\x4f\x10") + 2      # 0x104f payload start
                    plc[fi + 2] = 2 * region_index + chan
                    struct.pack_into("<Q", plc, fi + 7, int(pos))
                    placements += plc
                placements += lane_trailer
                newc = bytearray(child[:13 + nl]) + len(track_clips[track]).to_bytes(4, "little") + placements
                struct.pack_into("<I", newc, 3, len(newc) - 7)
                child = bytes(newc)
            lane += 1
        out += child
        i += 7 + csz
    struct.pack_into("<I", out, 3, len(out) - 7)
    return bytes(out)


_CLIP_TEMPLATE_NAMES = ("CLIP_WAV_DESCRIPTOR", "CLIP_REGION", "CLIP_PATH", "CLIP_LANE_TAIL",
                        "CLIP_1004_HEADER", "CLIP_1004_TRAILER", "CLIP_262A_HEADER",
                        "CLIP_FILENAME_TRAILER")


def _extract_clip_templates(clip_ref: bytes) -> "tuple[bytes, ...]":
    """Extract the (<=512 B) clip byte templates from a clip control session, in the order
    (wav_descriptor 0x1003, region 0x2629, path 0x0f3d, lane_tail, 0x1004 header, 0x1004
    trailer, 0x262a header, 0x103a folder-path trailer). These are exactly what `_templates`
    inlines — pass a re-authored control here (or to `donorpack.write_inline_templates`) to
    regenerate the inlined copies."""
    import struct
    cp = parse(clip_ref)
    t1003 = block_bytes(clip_ref, [b for b in flat_blocks(cp) if b.content_type == 0x1003][0])
    treg = block_bytes(clip_ref, [b for b in flat_blocks(cp) if b.content_type == 0x2629][0])
    tpath = block_bytes(clip_ref, [b for b in cp.blocks if b.content_type == 0x0F3D][0])
    lane = [b for b in flat_blocks(cp)
            if b.content_type == 0x1052 and any(c.content_type == 0x1050 for c in b.child)][0]
    lb = block_bytes(clip_ref, lane)
    ttail = lb[13 + struct.unpack_from("<I", lb, 9)[0]:]
    h4, _pre4, _units, tr4 = _count_block_parts(clip_ref, 0x1004, 0x1003)
    h2a = block_bytes(clip_ref, [b for b in cp.blocks if b.content_type == 0x262A][0])[:9]
    trailer = _filename_list_trailer(clip_ref)
    return t1003, treg, tpath, ttail, h4, tr4, h2a, trailer


def build_audio_clips(data: bytes, tracks, clip_ref: "bytes | None" = None) -> bytes:
    """Build a complete multi-track audio session: place clips on a clean N-stereo
    session `data` from a HIERARCHICAL spec `tracks` — a list (one entry per track, in
    track order) of clip lists, each clip a `(wav_path, position_file_samples)` tuple — or
    `(wav_path, position, name)` to give the clip a display name different from the WAV's
    filename. Any number of clips per track, any number of tracks, each clip its own staged
    BWF WAV, position, and optional name. The byte templates (a 0x1003 wav descriptor, a
    `.L` 0x2629 region, the 0x0f3d path, a clip lane tail, the 0x1004 header/trailer) are
    INLINED in `_templates` (extracted from `3 stereo 3 different clips.ptx`), so `clip_ref`
    defaults to None and no donor file is needed; pass a `clip_ref` only to override them
    (e.g. to regenerate the inlined templates from a re-authored control).

    The session's audio content is assembled per DISTINCT wav (clips of the same wav
    share a region): D wav descriptors in 0x1004 (each built from the WAV's identity via
    `wav_clip_identity`), D region pairs in 0x262a (unique deterministic GUIDs), one
    shared 0x0f3d path; placements reference their wav's region by index (0x104f
    payload[2]); the index gains exactly one 0x0f3c marker (`_synth_clip_index`). `data`
    must be clip-FREE with at least as many tracks as `len(tracks)`. Returns unxored bytes."""
    import os, struct, hashlib
    if clip_ref is None:
        from . import _templates as _T
        t1003, treg, tpath, ttail = _T.CLIP_WAV_DESCRIPTOR, _T.CLIP_REGION, _T.CLIP_PATH, _T.CLIP_LANE_TAIL
        h4, tr4, h2a, trailer = (_T.CLIP_1004_HEADER, _T.CLIP_1004_TRAILER,
                                 _T.CLIP_262A_HEADER, _T.CLIP_FILENAME_TRAILER)
    else:
        t1003, treg, tpath, ttail, h4, tr4, h2a, trailer = _extract_clip_templates(clip_ref)

    clips = []  # (track, wav, position, name|None)
    for t, clist in enumerate(tracks):
        for spec in clist:
            clips.append((t, spec[0], spec[1], spec[2] if len(spec) > 2 else None))
    distinct = []
    for _t, w, _p, _n in clips:
        if w not in distinct:
            distinct.append(w)
    region_of = {w: j for j, w in enumerate(distinct)}
    # region (clip) display name per distinct wav: the first clip's name, else filename stem
    name_of = {}
    for _t, w, _p, n in clips:
        if n is not None and w not in name_of:
            name_of[w] = n

    descs, regions, filenames = [], [], []
    for j, w in enumerate(distinct):
        sc, umid, id2 = wav_clip_identity(w)
        fn = os.path.basename(str(w))
        filenames.append(fn)
        stem = name_of.get(w) or fn.rsplit(".wav", 1)[0]  # clip name, decoupled from filename
        descs.append(_build_wav_descriptor(t1003, j + 1, sc, umid, id2, _file_filetime(w)))

        def _guid(chan):
            return hashlib.sha256(f"ptformat-clip-{j}-{chan}".encode()).digest()[:16]
        regions.append(_build_region(treg, stem + ".L", 0, sc, _guid(0), j))
        regions.append(_build_region(treg, stem + ".R", 1, sc, _guid(1), j))

    blk1004 = _rebuild_count_block(h4, b"\x00\x00\x00\x00" + _build_filename_list(filenames, trailer),
                                   descs, tr4, len(distinct))
    blk262a = _rebuild_count_block(h2a, b"\x00\x00\x00\x00", regions, b"", 2 * len(distinct))

    track_clips: "dict" = {}
    for t, w, pos, _n in clips:
        track_clips.setdefault(t, []).append((region_of[w], pos))

    refs = W.top_level_refs(data)
    if not refs or refs[-1].block.content_type != 0x0002:
        raise ValueError("data has no trailing 0x0002 master index")
    body = bytearray(data[: refs[0].start])
    for r in refs[:-1]:
        ct = r.block.content_type
        if ct == 0x1004:
            body += blk1004
        elif ct == 0x262A:
            body += blk262a
        elif ct == 0x0F3D:
            body += tpath
        elif ct == 0x1054:
            body += _build_placement_lanes(r.data, track_clips, ttail)
        else:
            body += r.data
    body = bytes(body)
    out = body + _synth_clip_index(data, body)
    return _FI._set_first_block_index_pointer(out, len(body))


def clip_positions(data: bytes) -> "list[int]":
    """Return the timeline start (in FILE samples) of every 0x104f placement lane,
    in body order. A single stereo clip has two (lane .L, lane .R) with equal values."""
    import struct
    out = []
    for b in flat_blocks(parse(data)):
        if b.content_type == 0x104F:
            pos = b.offset + 9  # full[16] = (offset-7)+16 = offset+9
            out.append(struct.unpack_from("<Q", data, pos)[0])
    return out


def set_clip_position(data: bytes, position_file_samples: int,
                      *, lanes: "tuple[int, ...] | None" = None) -> bytes:
    """Move audio clip placement(s) to an arbitrary timeline position. The clip's
    timeline start is governed SOLELY by the 0x104f placement position field (8-byte
    LE, in FILE samples = pos_seconds * file_rate, e.g. 44100), mirrored across the
    clip's lanes (one 0x104f per channel). PT-CONFIRMED by the bar1/bar2/bar3 controls:
    moving a clip changes ONLY this field — patching bar2's 0x104f to bar3's value
    reproduces real bar3 with zero unexpected diffs (the 0x2016/0x2056/0x2587/0x2624
    waveform-overview caches are recomputed by Pro Tools; the 0x262a region GUIDs are
    a save-nonce). The move is SIZE-NEUTRAL, so no reindex is needed.

    `position_file_samples` is in the WAV's own sample rate. A clip can only sit at a
    nonzero position if it was built from a pos>0 template (the 0->nonzero transition
    adds a 0x2038 sub-block to 0x2624 / +19 bytes); use a `one clip bar 2`-style ref.
    With `lanes` None, ALL placements move (correct for a single-clip session); pass a
    tuple of lane indices (body order) to move one clip's lane-pair. Returns unxored bytes."""
    import struct
    out = bytearray(data)
    blocks = [b for b in flat_blocks(parse(bytes(out))) if b.content_type == 0x104F]
    field = struct.pack("<Q", int(position_file_samples))
    sel = range(len(blocks)) if lanes is None else lanes
    for i in sel:
        pos = blocks[i].offset + 9
        out[pos:pos + 8] = field
    return bytes(out)


def _find_bpm_double(block: bytes) -> "tuple[int, float] | None":
    """Locate the tempo's bpm float64 in a 0x2718 / 0x2028 block: the 8-byte window
    that decodes to a plausible musical tempo (20..999 bpm). Returns (offset, bpm)."""
    import struct
    for off in range(len(block) - 8):
        (v,) = struct.unpack_from("<d", block, off)
        if 20.0 <= v <= 999.0 and abs(v - round(v, 4)) < 1e-9:
            return off, v
    return None


def set_tempo(data: bytes, clean_ref: bytes, tempo_ref: bytes, bpm: "float | None" = None) -> bytes:
    """Set a single session tempo on `data` by transplanting the tempo change from a
    matched control PAIR: `clean_ref` (a default-tempo session, e.g. `Untitled.ptx`)
    and `tempo_ref` (the SAME session with an explicit tempo, e.g. `121bpm.ptx`).

    A tempo change touches the conductor scaffolding 0x1040 (value-independent — it
    just marks the conductor track "explicit") and stores the bpm as a float64 in BOTH
    0x2718 and 0x2028. Those blocks are transplanted from `tempo_ref`; if `bpm` is
    given it overwrites the ref's bpm double in the result (so any tempo can be set
    from one non-default ref). `data` keeps its own name/display. BYTE-EXACT content
    blocks when `data` IS `clean_ref`. Returns full unxored bytes."""
    import struct
    types = changed_top_level_types(clean_ref, tempo_ref)
    out = bytearray(_grow_blocks_from(data, tempo_ref, types))
    if bpm is None:
        return bytes(out)
    for ct in (0x2718, 0x2028):
        blocks = [b for b in parse(bytes(out)).blocks if b.content_type == ct]
        if not blocks:
            continue
        b = blocks[0]
        s, e = b.offset - 7, b.offset + b.block_size
        found = _find_bpm_double(bytes(out[s:e]))
        if found is None:
            raise ValueError(f"no bpm double found in 0x{ct:04x}")
        off, _old = found
        out[s + off : s + off + 8] = struct.pack("<d", float(bpm))
    return bytes(out)


def set_meter(data: bytes, clean_ref: bytes, meter_ref: bytes,
              numerator: "int | None" = None, denominator: "int | None" = None) -> bytes:
    """Set a single session meter (time signature) on `data` by transplanting the
    meter change from a matched control PAIR: `clean_ref` (a default-4/4 session, e.g.
    `Untitled.ptx`) and `meter_ref` (the SAME session with an explicit meter, e.g.
    `3-4 meter.ptx`).

    A meter change touches the conductor scaffolding 0x1040 plus the meter map blocks
    0x2719 and 0x2029, which carry the numerator/denominator as u32s after the "Meter"
    tag. Those blocks are transplanted from `meter_ref`; if `numerator`/`denominator`
    are given they overwrite the ref's values in the result. `data` keeps its own
    name/display. BYTE-EXACT content blocks when `data` IS `clean_ref`. Returns full
    unxored bytes."""
    types = changed_top_level_types(clean_ref, meter_ref)
    out = bytearray(_grow_blocks_from(data, meter_ref, types))
    if numerator is None and denominator is None:
        return bytes(out)
    for ct in (0x2719, 0x2029):
        blocks = [b for b in parse(bytes(out)).blocks if b.content_type == ct]
        if not blocks:
            continue
        b = blocks[0]
        s, e = b.offset - 7, b.offset + b.block_size
        body = bytes(out[s:e])
        m = body.find(b"Meter")
        if m < 0:
            continue
        # after "Meter"(5) + 0x0200 sep(2) + payload_len(4) + count(4) + pos(5) +
        # pad(3) + ordinal(4) => numerator u32, then denominator u32.
        num_off = m + 5 + 2 + 4 + 4 + 5 + 3 + 4
        den_off = num_off + 4
        if den_off + 4 > len(body):
            continue
        if numerator is not None:
            out[s + num_off : s + num_off + 4] = int(numerator).to_bytes(4, "little")
        if denominator is not None:
            out[s + den_off : s + den_off + 4] = int(denominator).to_bytes(4, "little")
    return bytes(out)


# Tempo/meter event positions are PT ticks (960000 per quarter); a record's stored
# position is ZERO_TICKS + tick. A tempo record is a 61-byte "Const..TMS" block (the
# bpm float64 at +40, position at +30); meter records sit after the ") Meter" tag,
# 52 bytes each (position at +0, ordinal +8, numerator +12, denominator +16). Both a
# map block (0x2028/0x2029) AND a ruler block (0x2718/0x2719) hold the same records.
_ZERO_TICKS = 0xE8D4A51000
_TEMPO_REC_SIG = bytes.fromhex("436f6e737401002e000000544d53")  # "Const..TMS"
_METER_TAG = b") Meter\x02"
TICKS_PER_QUARTER = 960000


def _block_spans(data: bytes, ct: int) -> "list[tuple[int, int]]":
    return [(b.offset - 7, b.offset + b.block_size)
            for b in parse(data).blocks if b.content_type == ct]


def _resize_tempo_block(block: bytes, n_records: int) -> bytes:
    """Grow/shrink a 0x2028 (map) or 0x2718 (ruler) block to exactly `n_records`
    61-byte "Const..TMS" records, replicating its first record as the template (the
    caller patches each record's pos/bpm/flags). The ") Tempo" header carries count
    (u32 @ rec0-8) and a length field (u32 @ rec0-12 = 4 + 61*N); block_size is @ +3.
    The fixed trailer after the records is preserved. rec0 is found via the record
    signature (NOT the 'Tempo' tag, which also appears in the 0x2718 0x2619 preamble)."""
    import struct
    rec0 = block.find(_TEMPO_REC_SIG)
    if rec0 < 0:
        return block
    # count contiguous existing records from rec0
    R, i = 0, rec0
    while block[i:i + len(_TEMPO_REC_SIG)] == _TEMPO_REC_SIG:
        R += 1
        i += 61
    template = block[rec0:rec0 + 61]
    trailer = block[rec0 + R * 61:]
    new_block = bytearray(block[:rec0] + template * n_records + trailer)
    struct.pack_into("<I", new_block, rec0 - 8, n_records)            # count
    struct.pack_into("<I", new_block, rec0 - 12, 4 + 61 * n_records)  # length
    struct.pack_into("<I", new_block, 3, len(new_block) - 7)          # block_size
    return bytes(new_block)


def _resize_meter_block(block: bytes, n_records: int) -> bytes:
    """Grow/shrink a meter block to `n_records` events. The meter map lives in a 0x2029
    block (top-level, OR nested inside the 0x2719 ruler) as: a ") Meter\\x02" header with
    length (u32 @tag+9 = 12 + 52*N) and count (u32 @tag+13), then N 36-byte records
    (pos@0, ordinal@8, num@12, den@16), then a trailing list of N 16-byte entries
    (ordinal:u32 + 0x01000000 + 8 zero) and a fixed 8-byte tail. Each event contributes
    36 + 16 = 52 bytes. Both the 0x2029 sub-block size (@tag-4) and, when nested in a
    0x2719 wrapper, the outer block_size (@3) are bumped. Caller patches per-event fields."""
    import struct
    tag = block.find(b") Meter\x02")
    if tag < 0:
        return block
    count_off = tag + 13
    rec0 = tag + 17
    count = struct.unpack_from("<I", block, count_off)[0]
    rec_template = block[rec0:rec0 + 36]
    trailing = block[rec0 + count * 36:]
    entry_template = trailing[:16] if count else b"\x01\x00\x00\x00\x01\x00\x00\x00" + b"\x00" * 8
    fixed_tail = trailing[count * 16:]
    new_block = bytearray(block[:rec0] + rec_template * n_records
                          + entry_template * n_records + fixed_tail)
    delta = len(new_block) - len(block)
    struct.pack_into("<I", new_block, count_off, n_records)
    struct.pack_into("<I", new_block, tag + 9, 12 + 52 * n_records)        # length
    struct.pack_into("<I", new_block, tag - 4,                              # 0x2029 size
                     struct.unpack_from("<I", new_block, tag - 4)[0] + delta)
    if tag != 7:                                                           # outer 0x2719
        struct.pack_into("<I", new_block, 3,
                         struct.unpack_from("<I", new_block, 3)[0] + delta)
    return bytes(new_block)


def _resize_record_blocks(data: bytes, resize) -> bytes:
    """Rebuild `data` replacing each top-level block via `resize(content_type, bytes)`
    (returns the new block bytes, or the original to leave unchanged). Body order is
    preserved; the trailing 0x0002 index is left untouched (caller reindexes)."""
    refs = W.top_level_refs(data)
    out = bytearray(data[: refs[0].start])
    for r in refs:
        out += resize(r.block.content_type, r.data)
    return bytes(out)


def set_tempo_map(data: bytes, events: "list[tuple[float, int]]",
                  tempo_ref: "bytes | None" = None, clean_ref: "bytes | None" = None) -> bytes:
    """Write a MID-SESSION tempo map (one or more tempo changes) onto `data`.

    `events` is a list of (bpm, position_in_ticks) in time order — e.g.
    `[(120, 0), (140, 4*TICKS_PER_QUARTER)]` is 120 bpm from the start then 140 bpm at bar 2.
    ANY number of events is supported (the record slots are BUILT). The first event is the
    session-start tempo (its position is normally 0).

    The tempo map lives, identically, in BOTH the map block 0x2028 and the ruler block 0x2718
    (each: a ") Tempo" header with count/length = 4+61*N, then N 61-byte "Const..TMS" records,
    then a fixed trailer). Each record carries the first-event flags (@21 & @39 = 1 for the
    initial tempo else 0), position (ZERO_TICKS + ticks @ +30), and bpm (float64 @ +40).

    The 0x2028/0x2718 templates are INLINED (`_templates.TEMPO_*`), so no donor is needed —
    only those two blocks are grown (DATA keeps its own view-state/I/O, exactly like
    `set_markers`); the tempo map is byte-identical to the donor path either way. Pass a
    `tempo_ref` (+ matching `clean_ref`) to override — that grows the full clean/ref delta
    (view-state included), which reproduces a control session byte-for-byte. Returns unxored."""
    import struct
    n = len(events)
    if n < 1:
        raise ValueError("events must be non-empty (the first event is the start tempo)")
    if tempo_ref is None:
        from . import _templates as _T
        out = bytearray(_grow_blocks_into(data, {
            0x2028: _resize_tempo_block(_T.TEMPO_MAP_TEMPLATE, n),
            0x2718: _resize_tempo_block(_T.TEMPO_RULER_TEMPLATE, n)}))
    else:
        if clean_ref is None:
            raise ValueError("override path needs clean_ref (the no-tempo pair of tempo_ref)")
        types = changed_top_level_types(clean_ref, tempo_ref)
        grown = _resize_record_blocks(
            tempo_ref,
            lambda ct, b: _resize_tempo_block(b, n) if ct in (0x2028, 0x2718) else b)
        gref = _FI.reindex_after_resize(tempo_ref, grown)
        out = bytearray(_grow_blocks_from(data, gref, types))
    for ct in (0x2028, 0x2718):
        for s, e in _block_spans(bytes(out), ct):
            recs, i = [], s
            while True:
                j = bytes(out).find(_TEMPO_REC_SIG, i, e)
                if j < 0:
                    break
                recs.append(j)
                i = j + 61
            if len(recs) != n:
                raise ValueError(f"0x{ct:04x} built {len(recs)} records, expected {n}")
            for idx, ((bpm, pos), r) in enumerate(zip(events, recs)):
                flag = 1 if idx == 0 else 0
                out[r + 21] = flag
                out[r + 39] = flag
                out[r + 30 : r + 35] = (_ZERO_TICKS + int(pos)).to_bytes(5, "little")
                out[r + 40 : r + 48] = struct.pack("<d", float(bpm))
    return bytes(out)


def set_meter_map(data: bytes, events: "list[tuple[int, int, int]]",
                  meter_ref: "bytes | None" = None, clean_ref: "bytes | None" = None) -> bytes:
    """Write a MID-SESSION meter map (one or more time-signature changes) onto `data`.

    `events` is a list of (numerator, denominator, position_in_ticks) in time order — e.g.
    `[(4, 4, 0), (3, 4, 4*TICKS_PER_QUARTER)]` is 4/4 from the start then 3/4 at bar 2. ANY
    number of events is supported. The first event is the session-start meter (position 0).

    The meter map lives in BOTH 0x2029 and the ruler 0x2719 (each: a 36-byte record + a
    16-byte trailing-list entry per event). Each record carries position (ZERO_TICKS + ticks
    @ +0), ordinal (1-based @ +8), numerator (@ +12), denominator (@ +16); each trailing-list
    entry carries its ordinal (@ +0).

    The 0x2029/0x2719 templates are INLINED (`_templates.METER_*`), so no donor is needed —
    only those two blocks are grown (DATA keeps its own view-state/I/O, like `set_markers`).
    Pass a `meter_ref` (+ matching `clean_ref`) to override and reproduce a control byte-for-
    byte (grows the full clean/ref delta). Returns full unxored bytes."""
    n = len(events)
    if n < 1:
        raise ValueError("events must be non-empty (the first event is the start meter)")
    if meter_ref is None:
        from . import _templates as _T
        out = bytearray(_grow_blocks_into(data, {
            0x2029: _resize_meter_block(_T.METER_MAP_TEMPLATE, n),
            0x2719: _resize_meter_block(_T.METER_RULER_TEMPLATE, n)}))
    else:
        if clean_ref is None:
            raise ValueError("override path needs clean_ref (the no-meter pair of meter_ref)")
        types = changed_top_level_types(clean_ref, meter_ref)
        grown = _resize_record_blocks(
            meter_ref,
            lambda ct, b: _resize_meter_block(b, n) if ct in (0x2029, 0x2719) else b)
        gref = _FI.reindex_after_resize(meter_ref, grown)
        out = bytearray(_grow_blocks_from(data, gref, types))
    for ct in (0x2029, 0x2719):
        for s, e in _block_spans(bytes(out), ct):
            m = bytes(out).find(_METER_TAG, s, e)
            if m < 0:
                continue
            # after ") Meter\x02" (tag) comes \x00 (sep) + length(4) + count(4), then
            # record 0; the trailing list (N 16-byte ordinal entries) follows the records.
            rec0 = m + len(_METER_TAG) + 1 + 4 + 4
            for i, (num, den, pos) in enumerate(events):
                r = rec0 + i * 36
                out[r : r + 5] = (_ZERO_TICKS + int(pos)).to_bytes(5, "little")
                out[r + 8 : r + 12] = int(i + 1).to_bytes(4, "little")     # ordinal
                out[r + 12 : r + 16] = int(num).to_bytes(4, "little")
                out[r + 16 : r + 20] = int(den).to_bytes(4, "little")
            trail0 = rec0 + n * 36
            for i in range(n):
                out[trail0 + i * 16 : trail0 + i * 16 + 4] = int(i + 1).to_bytes(4, "little")
    return bytes(out)


_MARKER_CT = 0x2077  # a marker record block inside the 0x2030 marker list


def _build_marker_record(template: bytes, ordinal: int, name: str, tick: int, guid16: bytes) -> bytes:
    """Build one 0x2077 marker record from a template: ordinal @+9, name (length-prefixed:
    len @+15, name @+19), position (ZERO_TICKS + tick, 5-byte LE at name_end and
    name_end+8), and a unique GUID (16B at name_end+166). Everything else is kept from the
    template; the record size = base + namelen."""
    import struct
    t = bytearray(template)
    tnl = struct.unpack_from("<I", t, 15)[0]
    tend = 19 + tnl
    t[9] = ordinal & 0xFF
    pos = (_ZERO_TICKS + int(tick)).to_bytes(5, "little")
    t[tend:tend + 5] = pos
    t[tend + 8:tend + 13] = pos
    t[tend + 166:tend + 182] = guid16
    nb = name.encode()
    out = bytearray(t[:15]) + len(nb).to_bytes(4, "little") + nb + t[19 + tnl:]
    struct.pack_into("<I", out, 3, len(out) - 7)
    return bytes(out)


def _resize_marker_block(block: bytes, markers) -> bytes:
    """Rebuild a 0x2030 marker list: count (u32 @ content[0]) + one 0x2077 record per
    (name, tick) in `markers`, then any trailer (preserved). Records are built from the
    block's first 0x2077 template; each gets ordinal i+1 and a deterministic unique GUID."""
    import struct, hashlib
    i = 13  # 0x2030 header (9) + count (4)
    recs = []
    while i < len(block) and block[i] == 0x5A and struct.unpack_from("<H", block, i + 7)[0] == _MARKER_CT:
        sz = struct.unpack_from("<I", block, i + 3)[0]
        recs.append((i, 7 + sz))
        i += 7 + sz
    if not recs:
        return block
    template = block[recs[0][0]:recs[0][0] + recs[0][1]]
    trailer = block[i:]
    records = bytearray()
    for k, (name, tick) in enumerate(markers):
        guid = hashlib.sha256(f"ptformat-marker-{k}".encode()).digest()[:16]
        records += _build_marker_record(template, k + 1, name, tick, guid)
    content = bytearray(b"\x00\x00\x00\x00") + records + trailer
    struct.pack_into("<I", content, 0, len(markers))
    out = bytearray(block[:9]) + content
    struct.pack_into("<I", out, 3, len(out) - 7)
    return bytes(out)


def set_markers(data: bytes, markers, markers_ref: "bytes | None" = None) -> bytes:
    """Write session markers onto `data`. `markers` = list of (name, position_in_ticks) in
    time order. Markers live SOLELY in the 0x2030 marker list: a count + one 0x2077 record
    per marker (ordinal, length-prefixed name, position = ZERO_TICKS + ticks, a unique GUID).

    The 0x2030 template is INLINED (`_templates.MARKER_BLOCK_TEMPLATE`, a minimal 1-record
    block resized to N markers — byte-identical to a full donor for any marker list), so no
    donor is needed. Pass `markers_ref` (any session with a 0x2030) only to override the
    template (e.g. to regenerate the inlined copy). A marker adds no new top-level type, so
    it's the easy reindex case. Returns unxored bytes."""
    if not markers:
        return data
    if markers_ref is None:
        from . import _templates as _T
        template_2030 = _T.MARKER_BLOCK_TEMPLATE
    else:
        template_2030 = block_bytes(markers_ref,
                                    [b for b in parse(markers_ref).blocks if b.content_type == 0x2030][0])
    return _grow_block_into(data, 0x2030, _resize_marker_block(template_2030, markers))


def _patch_midi_note(block: bytearray, base: int, *, pitch=None, velocity=None,
                     length_ticks=None) -> None:
    """Patch the FIRST MIDI note in a 0x2000 events block (in place). `base` is the
    block's start within `block`. Layout after the "MdNLB" marker: +11 -> n_events
    (u32) -> the note records; record 0 begins at the n_events+4 boundary, with
    pitch at +8 (u8), length at +9 (5-byte LE ticks), velocity at +17 (u8)."""
    m = block.find(b"MdNLB", base)
    if m < 0:
        return
    rec = m + 11 + 4  # skip marker tail (11) + n_events (4); record 0 starts here
    if pitch is not None:
        block[rec + 8] = int(pitch) & 0x7F
    if length_ticks is not None:
        block[rec + 9 : rec + 14] = int(length_ticks).to_bytes(5, "little")
        # A second, length-derived field at rec+28 (signed i32): empirically
        # length//256 - 2134 for a note at clip start (fits the eighth/quarter/half/
        # whole controls byte-for-byte). Keep it consistent with the primary length.
        block[rec + 28 : rec + 32] = (int(length_ticks) // 256 - 2134).to_bytes(4, "little", signed=True)
    if velocity is not None:
        block[rec + 17] = int(velocity) & 0x7F


def add_midi_note(data: bytes, clean_ref: bytes, midi_ref: bytes, *,
                  pitch: "int | None" = None, velocity: "int | None" = None,
                  length_ticks: "int | None" = None) -> bytes:
    """Add a MIDI clip (one note) to session `data` by transplanting the clip's
    contribution from a matched control PAIR: `clean_ref` (a MIDI-track session
    WITHOUT the clip, e.g. `one midi track no clips v2.ptx`) and `midi_ref` (the SAME
    session WITH a one-note clip). Mirrors `add_audio_clip`.

    A MIDI clip touches a set of top-level blocks (the events block 0x2000, the region
    map 0x2634, the edit playlist 0x2624, the region/track maps 0x1058/0x2107, the name
    table 0x2519, plus display state) — `changed_top_level_types(clean_ref, midi_ref)`
    minus session/hardware-config blocks (`_REF_VOLATILE_TYPES`, kept from `data`).
    Those are transplanted from `midi_ref` and the master index is repaired by
    `_transplant_top_level`. `data` keeps its own name/display. The note's pitch
    (0..127), velocity (0..127) and length (in PT ticks; quarter = 960000) can be set;
    otherwise the ref's note is used.

    MIDI-EDITOR WINDOW: Pro Tools normally pops the MIDI editor window front-and-center
    when it loads a session that has one (encoded as ~10 records in 0x2587). Pass an
    EDITOR-CLOSED `midi_ref` (a one-note session resaved with the editor window closed,
    e.g. `midi note editor closed.ptx`) to suppress it — its 0x2587 lacks those records,
    so the result opens to just the Edit window. PT-CONFIRMED.

    `data` / `clean_ref` must share scaffold (same track layout) so the blocks line up.
    BYTE-EXACT to `midi_ref` (mod session-name) when `data` IS `clean_ref`; the note
    patch reproduces the pitch/velocity/length control variants byte-for-byte. Returns
    full unxored bytes (ready for `writer.encrypt_session_data`)."""
    types = [t for t in changed_top_level_types(clean_ref, midi_ref)
             if t not in _REF_VOLATILE_TYPES]
    out = bytearray(_transplant_top_level(data, midi_ref, types))
    if pitch is None and velocity is None and length_ticks is None:
        return bytes(out)
    for b in [x for x in parse(bytes(out)).blocks if x.content_type == 0x2000]:
        s = b.offset - 7
        _patch_midi_note(out, s, pitch=pitch, velocity=velocity, length_ticks=length_ticks)
    return bytes(out)


# --- click-on-top: move the (bottom) Click track to the TOP of the edit window ---
#
# Pro Tools stores the EDIT-WINDOW track display order NOT in the body block order,
# the overview, the window-config, or the per-track counters (all ruled out by PT
# loads), but as a PLAYLIST-ORDER LIST in the master index: the `0x2624` count==1
# container record's `elements[1:]` and each `0x2624` count==4 instance's
# `child_refs[1]` point at the playlists IN DISPLAY ORDER. PT sorts the edit window
# by this list. (Decoded 2026-05-31 by diffing a user drag-and-resave against a
# byte-identical synthesized session — only these 6 offsets differed at N=2.)
#
# move_click_to_top reproduces what Pro Tools writes when you drag the click to the
# top: it (1) reorders the body so the click's per-track blocks lead each run, (2)
# rank-refills the index (lanes are one type, so rank == display position), (3)
# rotates the playlist-instances click->ordinal 1 and fixes their childtypes, and
# (4) sets the playlist-order list (container + instance childref[1]) to display
# order, click first. Validated byte-exact vs a real click-on-top control at N=2;
# PT-CONFIRMED at N=2 and N=12.


def _reorder_click_to_front_body(data: bytes) -> bytes:
    """Move the click's block to the FRONT of each per-track run in the body (name-
    table entry, both lane-major 0x251a groups, 0x210b, and the 0x2624 playlist
    subtree), renumbering the embedded 1-based position ordinal (the u16 after each
    GUID) in the name/lane runs. Pure size-neutral rearrangement; returns the body."""
    ref = _FI.final_index_ref(data)
    body = data[: ref.start]
    bptf = parse(body)
    by = lambda ct: _by_type(bptf, ct)
    regions = []  # (slots[(lo,hi)] in old order, click_slot_index, renumber:bool)

    b2519 = by(0x2519)[0]
    first_child = min(c.offset - 7 for c in b2519.child)
    table = body[b2519.offset : first_child]
    name_offs = [m.start() for m in _TRACK_NAME.finditer(table)]
    estart = [b2519.offset + no - 6 for no in name_offs]  # entry = sep(2)+len(4)+name+...
    ebounds = [(estart[i], estart[i + 1] if i + 1 < len(estart) else first_child) for i in range(len(estart))]
    cidx = next(i for i, (a, b) in enumerate(ebounds) if b"Click 1" in body[a:b])
    regions.append((ebounds, cidx, True))

    lanes = sorted([c for c in b2519.child if c.content_type == 0x251A], key=lambda c: c.offset)
    half = len(lanes) // 2  # lane-major: [lane0 of all tracks][lane1 of all tracks]
    for lo, hi in [(0, half), (half, len(lanes))]:
        grp = lanes[lo:hi]
        sl = [(c.offset - 7, c.offset + c.block_size) for c in grp]
        ci = next(i for i, c in enumerate(grp) if _own_track_name(body, c) == "Click 1")
        regions.append((sl, ci, True))

    b2107 = by(0x2107)[0]
    k7 = sorted(b2107.child, key=lambda c: c.offset)
    sl7 = [(c.offset - 7, c.offset + c.block_size) for c in k7]
    regions.append((sl7, next(i for i, c in enumerate(k7) if _own_track_name(body, c) == "Click 1"), False))

    b2624 = by(0x2624)[0]
    k24 = sorted(b2624.child, key=lambda c: c.offset)
    sl24 = [(c.offset - 7, c.offset + c.block_size) for c in k24]
    regions.append((sl24, next(i for i, c in enumerate(k24) if c.content_type == 0x261E), False))

    regions = sorted(regions, key=lambda r: r[0][0][0])
    out = bytearray()
    cursor = 0
    for slots, click, renum in regions:
        out += body[cursor : slots[0][0]]
        order = [click] + [i for i in range(len(slots)) if i != click]
        for new_slot, oi in enumerate(order):
            slo, shi = slots[oi]
            chunk = bytearray(body[slo:shi])
            if renum:
                j = chunk.find(b"\x2a\x00\x00\x00")
                if 0 <= j and j + 14 <= len(chunk):
                    chunk[j + 12 : j + 14] = (new_slot + 1).to_bytes(2, "little")
            out += bytes(chunk)
        cursor = slots[-1][1]
    out += body[cursor:]
    return bytes(out)


def move_click_to_top(data: bytes) -> bytes:
    """Move a bottom-Click session's Click track to the TOP of the edit window.
    `data` is a full unxored session whose last track is a Click (e.g. the output of
    `add_click_anyN`). Returns full unxored bytes (ready for `writer.encrypt_session_data`).
    PT-CONFIRMED at N=2 and N=12 (stereo); byte-exact vs the real click-on-top control at N=2."""
    ref = _FI.final_index_ref(data)
    index = data[ref.start :]
    _r, holes = _FI.offset_holes(data)  # capture (type, rank) from the consistent input
    newbody = _reorder_click_to_front_body(data)

    # rank-refill the index for the reordered body (lanes & same-type per-track holes
    # get display order automatically; the playlist list is fixed up explicitly below).
    buf = bytearray(newbody + index)
    new_index_start = len(newbody)
    _z, by_type0 = _FI.block_layout(bytes(buf))
    for abs_pos, _val, ttype, rank, _kind in holes:
        npos = new_index_start + (abs_pos - ref.start)
        buf[npos : npos + 4] = int(by_type0[ttype][rank]).to_bytes(4, "little")

    zt, btmap = _FI.block_layout(bytes(buf))
    recs = _FI.parse_records(bytes(buf[new_index_start:]), set(btmap), set(zt))

    # The playlist-instances (0x2624 count==4) stay in stream order with ordinals
    # 1..N+1; rank-refill already pointed ordinal-1's rank-based child_refs at the
    # click's (now physically-first = rank-0) blocks. Relabel by ORDINAL: ordinal 1
    # is the click. Set the playlist-kind childtype accordingly.
    for r in recs:
        if r.content_type == 0x2624 and r.count == 4 and r.child_refs:
            r.child_refs[0].child_type = 0x261E if r.ordinal == 1 else 0x261C

    # THE display-order field: the playlist-order list, click first. The 0x2624
    # count==1 container's elements[1:] and each count==4 instance's child_refs[1]
    # point at the playlists in edit-window display order (click first).
    fb = flat_blocks(parse(newbody))
    click_pl = [b.offset - 7 for b in fb if b.content_type == 0x261E][0]
    audio_pls = sorted(b.offset - 7 for b in fb if b.content_type == 0x261C)
    display = [click_pl] + audio_pls
    for r in recs:
        if r.content_type == 0x2624 and r.count == 1 and r.flag == 1 and len(r.elements) >= 1 + len(display):
            for i, off in enumerate(display):
                r.elements[1 + i].offsets = [off]
        if r.content_type == 0x2624 and r.count == 4 and len(r.child_refs) >= 2:
            r.child_refs[1].offset = display[r.ordinal - 1]

    return _set_index_offset(newbody + _FI.serialize_final_block(recs))


# --- general track reordering (edit-window display order) ---------------------
#
# The edit-window track order is the index PLAYLIST-ORDER LIST and NOTHING ELSE —
# PT-CONFIRMED: setting just this list (body untouched) reorders the display
# (Audio 4 to top, click to middle, etc. all confirmed). So arbitrary reordering
# is a pure index edit: permute the list. (move_click_to_top additionally reorders
# the body to match what PT re-writes on save, but that is cosmetic — not required
# for the display.)


def track_playlist_order(data: bytes) -> list[tuple[int, int]]:
    """The track playlists in body/creation order as (zmark_offset, content_type):
    0x261c = audio, 0x261e = click. This is the list `reorder_tracks`'s `new_order`
    indexes (index 0 = the first-created track, etc.). MIDI/bus playlists, if any,
    are included by their playlist type."""
    fb = flat_blocks(parse(data))
    return sorted(((b.offset - 7, b.content_type) for b in fb
                   if b.content_type in (0x261C, 0x261E)), key=lambda x: x[0])


def reorder_tracks(data: bytes, new_order: list[int]) -> bytes:
    """Set the edit-window track display order to `new_order` — a permutation of
    0..N-1 over the track playlists in body/creation order (see track_playlist_order;
    e.g. [3,0,1,2] moves the 4th track to the top). Returns full unxored bytes
    (ready for `writer.encrypt_session_data`).

    INDEX-ONLY: the edit-window order lives in the master-index playlist-order list
    (the 0x2624 count==1 container's elements[1:] + each count==4 instance's
    child_refs[1], with the instance childtype = the display-position playlist's kind
    0x261c/0x261e). This sets that list to `new_order`; the body is left untouched
    (Pro Tools sorts the edit window by this list, NOT by body block order). Works
    for any track including the click at any position. PT-CONFIRMED (move-to-top,
    reverse, click-to-middle)."""
    pls = track_playlist_order(data)
    if sorted(new_order) != list(range(len(pls))):
        raise ValueError(f"new_order must be a permutation of 0..{len(pls) - 1}; got {new_order}")
    target = [pls[i] for i in new_order]
    toff = [o for o, _t in target]
    ttype = [t for _o, t in target]
    ref = _FI.final_index_ref(data)
    zt, bt = _FI.block_layout(data)
    recs = _FI.parse_records(ref.data, set(bt), set(zt))
    for r in recs:
        if r.content_type == 0x2624 and r.count == 1 and r.flag == 1 and len(r.elements) >= 1 + len(toff):
            for i, off in enumerate(toff):
                r.elements[1 + i].offsets = [off]
        if r.content_type == 0x2624 and r.count == 4 and len(r.child_refs) >= 2 and 1 <= r.ordinal <= len(toff):
            r.child_refs[1].offset = toff[r.ordinal - 1]
            r.child_refs[0].child_type = ttype[r.ordinal - 1]
    return _set_index_offset(data[: ref.start] + _FI.serialize_final_block(recs))


# --- track edit-window VIEW MODE (waveform vs volume) -------------------------
#
# A track's edit-window view is stored as a small block under TWO parents: the track's
# display chain (parent 0x2015) and its overview entry (parent 0x2589). The block is
# 0x203b (VOLUME view, 22 B) or 0x2038 (WAVEFORM view, 19 B) — a 0x2037 child plus a
# trailer (volume carries an extra `01 00 00 00`; waveform just `00`). PT renders the
# track in whichever these encode. (Real volume-AUTOMATION lanes are also 0x203b but sit
# under 0x2580 — those are left alone.) The click splice (`add_click_anyN`) can leave a
# track's view blocks as volume — PT-confirmed it mis-assigns volume to the 3rd-displayed
# track — so generated sessions run this to force every track back to waveform.
_VIEW_VOLUME = bytes.fromhex("5a01000f0000003b205a010002000000372001000000")    # 0x203b, 22 B
_VIEW_WAVEFORM = bytes.fromhex("5a01000c00000038205a010002000000372000")        # 0x2038, 19 B
_VIEW_PARENTS = (0x2015, 0x2589)   # track display chain + overview entry (NOT 0x2580 automation)


def set_waveform_view(data: bytes) -> bytes:
    """Force every track's edit-window view to WAVEFORM.

    PT stores a track's view as a 0x203b (volume, 22 B) / 0x2038 (waveform, 19 B) block under
    the track display chain (0x2015) and the overview entry (0x2589). This converts every such
    VOLUME block to WAVEFORM (leaving real 0x2580 volume-automation lanes untouched), fixes the
    containing block sizes (-3 B each), and repairs the master index. Byte-for-byte the same
    edit PT writes when you switch a track to waveform view. No-op on an already-waveform
    session. The click splice can leave the 3rd-displayed track in volume view, so the build
    runs this last. Returns full unxored bytes."""
    ptf = parse(data)
    par: dict[int, "int | None"] = {}
    ctype: dict[int, int] = {}

    def _rec(b: Block, p: "int | None") -> None:
        z = b.offset - 7
        par[z] = p
        ctype[z] = b.content_type
        for c in sorted(b.child, key=lambda x: x.offset):
            _rec(c, z)

    for b in sorted(ptf.blocks, key=lambda x: x.offset):
        _rec(b, None)
    targets = [z for z, ct in ctype.items()
               if ct == 0x203b and ctype.get(par[z]) in _VIEW_PARENTS
               and data[z : z + len(_VIEW_VOLUME)] == _VIEW_VOLUME]
    if not targets:
        return data
    delta = len(_VIEW_WAVEFORM) - len(_VIEW_VOLUME)   # -3 per converted block
    ref = _FI.final_index_ref(data)
    idx_start = ref.start
    _r, holes = _FI.offset_holes(data)
    body = bytearray(data[:idx_start])
    index = data[idx_start:]
    # shrink every ANCESTOR of each converted block by |delta| (walk the actual parent chain —
    # the view block nests deep: 0x261c->0x200b->0x200a->0x2015->0x203b, every level must shrink)
    size_delta: dict[int, int] = {}
    for z in targets:
        a = par[z]
        while a is not None:
            size_delta[a] = size_delta.get(a, 0) + delta
            a = par[a]
    for a, dz in size_delta.items():
        sp = a + 3
        sz = int.from_bytes(body[sp : sp + 4], "little")
        body[sp : sp + 4] = (sz + dz).to_bytes(4, "little")
    # splice high->low so lower offsets stay valid
    for z in sorted(targets, reverse=True):
        body[z : z + len(_VIEW_VOLUME)] = _VIEW_WAVEFORM
    out = bytearray(bytes(body) + index)
    new_index_start = len(body)
    _z2t, by_type = _FI.block_layout(bytes(out))
    for abs_pos, _value, ttype, rank, _kind in holes:
        npos = new_index_start + (abs_pos - idx_start)
        out[npos : npos + 4] = int(by_type[ttype][rank]).to_bytes(4, "little")
    return _set_index_offset(bytes(out))


# Header count fields at stable content-relative offsets (block.offset + rel),
# value = N tracks. 0x1054 (= total audio CHANNELS, 2N stereo / N mono) is patched
# separately from `n_channels`. Occurrence picks the right block instance.
_HEADER_COUNTS = [
    (0x1015, 0, 2, 1), (0x2107, 0, 11, 1), (0x2624, 0, 2, 1),
    (0x2519, 0, 16, 1), (0x202a, 0, 18, 1), (0x202a, 1, 18, 1),
    # NOTE: 0x202b's two counts are just the two 0x202a children's counts viewed
    # from 0x202b's frame (content+31 == first 0x202a count; the "second" one sits
    # AFTER the first 0x202a child, so its offset shifts with N). Both are already
    # patched per-0x202a above — patching 0x202b at a fixed offset corrupts the
    # first 0x202a once it has grown (multi-step high-N bug).
    (0x2551, 0, 1088, 1), (0x2587, 0, 1097, 1), (0x258a, 1, 316, 1), (0x258b, 1, 1009, 1),
]


def _patch_counts(body: bytearray, n: int, n_channels: int) -> None:
    """Patch the N-dependent header count fields. `n` = track count (drives most
    fields); `n_channels` = total audio channels (0x1054 = 2N stereo / N mono)."""
    ptf = parse(bytes(body))
    occ: dict[int, int] = {}
    by_occ: dict[tuple[int, int], Block] = {}
    for b in flat_blocks(ptf):
        i = occ.get(b.content_type, 0); occ[b.content_type] = i + 1
        by_occ[(b.content_type, i)] = b
    for ct, o, rel, mult in _HEADER_COUNTS:
        b = by_occ.get((ct, o))
        if b is not None:
            _patch_u32(body, b.offset + rel, n * mult)
    # 0x1054 = total audio channels (decouples from track count under mono/mix).
    b1054 = by_occ.get((0x1054, 0))
    if b1054 is not None:
        _patch_u32(body, b1054.offset + 2, n_channels)
    # 0x2519 deep second count. Anchor: 01 00 00 00 fe ff <u32 count> 5a 0a.
    # The `5a 0a` (next 0x251a ZMARK) after the u32 distinguishes the real count
    # from the end of every 0x251a block (which is `...01 00 00 00 fe ff` followed
    # IMMEDIATELY by `5a 0a`, with no u32 in between).
    b2519 = by_occ.get((0x2519, 0))
    if b2519 is not None:
        s, e = b2519.offset - 7, b2519.offset + b2519.block_size
        scan = s
        while True:
            anchor = body.find(b"\x01\x00\x00\x00\xfe\xff", scan, e)
            if anchor < 0:
                break
            if body[anchor + 10 : anchor + 12] == b"\x5a\x0a":
                _patch_u32(body, anchor + 6, n)
                break
            scan = anchor + 1

    # NOTE: the overview display-order permutation is NOT patched here. It is a
    # whole-session permutation of 0..N-1 (hash-table iteration order) that
    # reshuffles unpredictably as tracks are added, so it cannot be derived by a
    # per-step insertion. `synthesize_stereo_session` rewrites the full sequence
    # in one shot from the target control via `_set_overview_order`.


def _patch_u16(buf: bytearray, pos: int, val: int) -> None:
    buf[pos : pos + 2] = int(val).to_bytes(2, "little")


# --- overview display-order permutation -------------------------------------
# Each track contributes one entry `89 25 <order:u16> 01 00 61 00 5a 01 ...`
# inside the second 0x258a block (89 25 = the 0x2589 content_type marker, order
# is the first content word). Across an N-track session the N order values form a
# permutation of 0..N-1 whose layout is a hash-table iteration order: it shuffles
# wholesale for small N and stabilises into pure insertions for N>=8. There is no
# cheap closed form, so we copy the exact sequence from a real N-track control.
_OVW_ANCHOR = b"\x01\x00\x61\x00\x5a\x01"


def _overview_order_offsets(data: bytes) -> list[int]:
    """Byte offsets of each overview order u16 (the `<order>` word that sits two
    bytes before every `01 00 61 00 5a 01` anchor), in file order."""
    offs: list[int] = []
    pos = 0
    while True:
        j = data.find(_OVW_ANCHOR, pos)
        if j < 0:
            break
        offs.append(j - 2)
        pos = j + 1
    return offs


def overview_order(data: bytes) -> list[int]:
    """The N overview display-order values of a session, in file order."""
    return [int.from_bytes(data[o : o + 2], "little") for o in _overview_order_offsets(data)]


# --- overview scroll-extent field -------------------------------------------
# A session-level value that is 0 for N<=9 but non-zero once the tracks overflow
# the edit window (n10-12=311, n13-16=602 for the controls' window size). It is
# replicated in the fixed-size overview blocks 0x2016 (rel 34/924/1217) and
# 0x2581 (rel 6/299, per block), which otherwise match the grow exactly. The
# grow leaves it 0 -- correct for <=9 tracks but a hard load failure ("magic ID")
# for >=10. There's no closed form (it's the saved window scroll), so copy it
# from the target control like the overview order / name table.
_OVW_EXTENT_FIELDS: dict[int, tuple[int, ...]] = {0x2016: (34, 924, 1217), 0x2581: (6, 299)}


def overview_extent(data: bytes) -> int:
    """Read the overview scroll-extent value (u32 at the first 0x2581 + 6)."""
    ptf = parse(data)
    for b in flat_blocks(ptf):
        if b.content_type == 0x2581:
            return int.from_bytes(data[b.offset + 6 : b.offset + 10], "little")
    return 0


def _set_overview_extent(body: bytearray, value: int) -> None:
    """Write the overview scroll-extent value into every 0x2016/0x2581 slot."""
    ptf = parse(bytes(body))
    for b in flat_blocks(ptf):
        rels = _OVW_EXTENT_FIELDS.get(b.content_type)
        if rels:
            for rel in rels:
                _patch_u32(body, b.offset + rel, value)


# --- window state: visible-track markers + session-info size -----------------
# Pro Tools stores which tracks are in the scrolled edit-window view via a marker
# run (`ef ff df bf ...`) in each visible track's 0x261c/0x200a/0x200b/0x2015/
# 0x2104. The donor's active track carries a stray marker that, in the target
# session, lies outside the visible range -> inconsistent window state. (Tolerated
# while N<=9 because there's no scroll, but a load failure once tracks overflow.)
_WINDOW_MARK_TYPES = (0x261c, 0x200a, 0x200b, 0x2015, 0x2104)
_WMARK_SET = {0xEF, 0xFF, 0xDF, 0xBF, 0x02}
_WMARK_START = bytes.fromhex("efffdfbf")


def _clear_donor_window_markers(body: bytes, base_n: int) -> bytes:
    """Zero the stray visible-track marker on the donor's tracks (1..base_n).
    Those tracks sit below the target's visible range (which is the last tracks),
    so they must carry no marker -- matching the real control. No-op for donors
    with no marker (N<=4). Library-sourced tracks keep their correct markers."""
    out = bytearray(body)
    ptf = parse(bytes(out))
    for ct in _WINDOW_MARK_TYPES:
        blks = sorted((b for b in flat_blocks(ptf) if b.content_type == ct), key=lambda b: b.offset)
        for i, b in enumerate(blks):
            if i >= base_n:
                continue
            s, end = b.offset, b.offset + b.block_size
            while True:
                j = bytes(out[s:end]).find(_WMARK_START)
                if j < 0:
                    break
                p = s + j
                q = p
                while q < end and out[q] in _WMARK_SET:
                    q += 1
                for k in range(p, q):
                    out[k] = 0
                s = q
    return bytes(out)


def _session_info_block(ptf: PTFFormat) -> Block:
    return [b for b in flat_blocks(ptf) if b.content_type == 0x2067][0]


def _match_session_info_size(body: bytes, library_data: bytes) -> bytes:
    """Transplant the library's 0x2067 (session-info) block when its size differs
    from the donor's. The 0x2067 embeds the session name; a different name length
    makes the block a different size, which SHIFTS every following block by that
    delta. The recomposed index tolerates the shift but Pro Tools validates a
    block position from another source that does not -> "magic ID does not match"
    at N>=10 (where the control's name "10 stereo tracks" is one char longer than
    a single-digit donor's). Only transplanted on a size mismatch, so same-length
    cases (e.g. 2->3) are untouched."""
    bptf = parse(body)
    b = _session_info_block(bptf)
    lb = _session_info_block(parse(library_data))
    if b.block_size == lb.block_size:
        return body
    new = library_data[lb.offset - 7 : lb.offset + lb.block_size]
    delta = len(new) - (b.block_size + 7)
    bz = b.offset - 7
    out = bytearray(body)
    for zmark in _ancestor_chain(bptf, bz)[1:]:  # containers only; 0x2067's own size is in `new`
        sp = zmark + 3
        old = int.from_bytes(out[sp : sp + 4], "little")
        out[sp : sp + 4] = (old + delta).to_bytes(4, "little")
    out[bz : b.offset + b.block_size] = new
    return bytes(out)


# --- per-track 'selected' state ---------------------------------------------
# A track's 0x261c playlist carries two 1-byte "selected" booleans (01=selected,
# 00=not). They sit at fixed offsets from a distinctive 9-byte view-state run
# (`ef ff df bf ef ff df bf 02`): flag1 = run-40, flag2 = run+186. Anchoring on
# the run (rather than an absolute offset) is robust to BOTH the track-name
# length (which shifts the tail) and the session path length (era A vs B). NOTE
# the run itself is NOT the selection state — many unselected tracks also have it
# (e.g. n16 tracks 8-15) with the flags at 00; only the two flag bytes encode
# selection, so we touch only those.
_SEL_ANCHOR = bytes.fromhex("efffdfbfefffdfbf02")
_SEL_FLAG1_REL = -40
_SEL_FLAG2_REL = 186


def _sel_flag_positions(block_bytes: bytes) -> tuple[int, int] | None:
    p = block_bytes.find(_SEL_ANCHOR)
    if p < 0:
        return None
    f1, f2 = p + _SEL_FLAG1_REL, p + _SEL_FLAG2_REL
    if f1 < 0 or f2 >= len(block_bytes):
        return None
    return f1, f2


def selected_tracks(data: bytes) -> list[int]:
    """1-based indices of tracks whose 0x261c selection flag is set."""
    ptf = parse(data)
    a1c = sorted((b for b in flat_blocks(ptf) if b.content_type == 0x261c), key=lambda b: b.offset)
    out: list[int] = []
    for i, b in enumerate(a1c):
        blk = data[b.offset : b.offset + b.block_size]
        fp = _sel_flag_positions(blk)
        if fp and blk[fp[0]] == 0x01:
            out.append(i + 1)
    return out


def _set_track_selection(body: bytearray, selected_track_1based: int | None) -> None:
    """Set track selection. `selected_track_1based=None` deselects every track
    (the neutral default — real controls show both none-selected and
    last-selected, so it's arbitrary saved UI state). Growing from a donor leaves
    the donor's last track AND the appended last track both selected; this clears
    the stray selections by flipping only the two flag bytes (no other content)."""
    ptf = parse(bytes(body))
    a1c = sorted((b for b in flat_blocks(ptf) if b.content_type == 0x261c), key=lambda b: b.offset)
    for i, b in enumerate(a1c):
        blk = bytes(body[b.offset : b.offset + b.block_size])
        fp = _sel_flag_positions(blk)
        if fp is None:
            continue
        want = 0x01 if (i + 1) == selected_track_1based else 0x00
        body[b.offset + fp[0]] = want
        body[b.offset + fp[1]] = want


def _set_overview_order(body: bytearray, values: list[int]) -> None:
    """Overwrite every overview order u16 (in file order) with `values`. The
    count must match — both the grown body and the source control hold exactly N
    entries, so this is a positional copy of the target permutation."""
    offs = _overview_order_offsets(bytes(body))
    if len(offs) != len(values):
        raise ValueError(f"overview order count mismatch: body has {len(offs)}, want {len(values)}")
    for o, v in zip(offs, values):
        _patch_u16(body, o, v)


def first_block_counter_value(n: int) -> int:
    """The per-N value stored in the first top-level block's type field."""
    return 2530 * n + 1886


def _first_block_type(data: bytes) -> int:
    ptf = parse(data)
    first = min(ptf.blocks, key=lambda b: b.offset)
    return first.content_type


def _set_counter(body: bytes, value: int) -> bytes:
    out = bytearray(body)
    ptf = parse(body)
    first = min(ptf.blocks, key=lambda b: b.offset)
    out[first.offset : first.offset + 2] = int(value).to_bytes(2, "little")
    return bytes(out)


def _synthesize_session(
    donor_data: bytes,
    base_n: int,
    target_n: int,
    library_data: bytes,
    library_total: int,
    channels: int,
) -> bytes:
    """Grow `donor_data` (a uniform `base_n`-track session of `channels`-channel
    audio tracks) to `target_n` tracks; return full unxored session bytes (ready
    for `writer.encrypt_session_data`). `channels`=2 stereo, 1 mono. Per-track
    blocks for base_n+1..target_n come from `library_data` (a uniform control with
    >= target_n tracks; deterministic per index)."""
    body = donor_data[: _FI.final_index_ref(donor_data).start]
    for k in range(base_n + 1, target_n + 1):
        unit = extract_track(library_data, k, library_total, channels=channels)
        body = grow_one_track(body, k - 1, unit)
    body = bytearray(body)
    # The first-block "counter" is a per-session value that is only linear in N
    # for N<=4; beyond that it depends on session history. Source it from the
    # library if the library has exactly target_n tracks, else fall back to the
    # (approximate) formula.
    counter = _first_block_type(library_data) if library_total == target_n \
        else first_block_counter_value(target_n)
    body = bytearray(_set_counter(bytes(body), counter))
    # Overwrite the overview display-order permutation in one shot. Growing the
    # body track-by-track leaves the donor's original tracks holding their low-N
    # order values while appended tracks bring the library's values -> an invalid
    # sequence with duplicates. The correct N-track permutation is taken from the
    # target control (library when it has exactly target_n tracks).
    if library_total == target_n:
        _set_overview_order(body, overview_order(library_data))
        # Transplant the exact name table (variable-length multi-digit entries +
        # last-entry boundary quirk make a per-step rebuild fragile).
        body = bytearray(_set_name_table(bytes(body), library_data))
        # Copy the overview scroll-extent (0 for <=9 tracks; non-zero once tracks
        # overflow the window -- the grow leaves it 0, which fails to load at >=10).
        _set_overview_extent(body, overview_extent(library_data))
        # Deselect every track. Growing inherits the donor's last-track selection
        # AND the appended last track's selection. This MUST run before the window
        # markers are cleared, because deselection anchors on the same `ef ff df bf`
        # run that the marker clear zeros.
        _set_track_selection(body, None)
        # Clear the donor's stray visible-track window markers (its active track is
        # outside the target's visible range).
        body = bytearray(_clear_donor_window_markers(bytes(body), base_n))
        # Match the session-info (0x2067) size to the target so the body isn't
        # shifted by a session-name-length difference (the >=10 "magic ID" cause).
        body = bytearray(_match_session_info_size(bytes(body), library_data))
    else:
        raise NotImplementedError(
            "overview order requires a target_n-track library (library_total == "
            f"target_n); got library_total={library_total}, target_n={target_n}. "
            "The permutation is a hash-table iteration order with no closed form; "
            "supply the matching control as the library."
        )
    body = bytes(body)
    donor_index = donor_data[_FI.final_index_ref(donor_data).start :]
    index = _FI.compose_index(donor_data, body + donor_index, base_n, target_n, channels=channels)
    # Rewrite the first-block index-offset pointer to the real index start. For
    # N<=16 this equals what `_set_counter` produced (index < 0x20000, high bits
    # 0x0001); for >16 tracks the full 4-byte offset is required to load.
    return _set_index_offset(body + index)


def synthesize_stereo_session(
    donor_data: bytes,
    base_n: int,
    target_n: int,
    library_data: bytes,
    library_total: int,
) -> bytes:
    """Grow a `base_n`-track empty-stereo session to `target_n` tracks.
    PT-confirmed for 1..16. See `_synthesize_session`."""
    return _synthesize_session(donor_data, base_n, target_n, library_data, library_total, channels=2)


def synthesize_mono_session(
    donor_data: bytes,
    base_n: int,
    target_n: int,
    library_data: bytes,
    library_total: int,
) -> bytes:
    """Grow a `base_n`-track empty-MONO session to `target_n` tracks. Same pipeline
    as stereo with one 0x1052 audio lane per track and 0x1054 = N channels (not 2N).
    Donor + library must be uniform mono controls (e.g. `N mono tracks.ptx`)."""
    return _synthesize_session(donor_data, base_n, target_n, library_data, library_total, channels=1)


def _folder_leaf(data: bytes) -> str | None:
    """The session-folder name (last component of the embedded session path), read
    from the first 0x261c. Used to path-normalize tracks sourced from different
    control folders to one consistent leaf (else the body is a path 'chimera' that
    Pro Tools rejects).

    The session path is a run of consecutive length-prefixed ASCII components
    (`<u32 len><name>`...), e.g. `Dropbox SSD/Dropbox/.../control_files/mixed tracks`
    or `Macintosh HD/Users/.../mixed tracks` — volume-dependent, so we don't anchor
    on a fixed volume name. Pick the consecutive chain with the most total
    characters (the filesystem path beats coincidental short runs like the `0x2626`
    `&&` markers) and return its last component."""
    ptf = parse(data)
    blk = next((b for b in flat_blocks(ptf) if b.content_type == 0x261C), None)
    if blk is None:
        return None
    seg = data[blk.offset : blk.offset + blk.block_size]
    best_chain: list[str] = []
    best_chars = 0
    i = 0
    while i + 4 <= len(seg):
        chain: list[str] = []
        pos = i
        while pos + 4 <= len(seg):
            ln = int.from_bytes(seg[pos : pos + 4], "little")
            if not (1 <= ln <= 64) or pos + 4 + ln > len(seg):
                break
            comp = seg[pos + 4 : pos + 4 + ln]
            if not all(32 <= c < 127 for c in comp):
                break
            chain.append(comp.decode("latin-1"))
            pos += 4 + ln
        chars = sum(len(c) for c in chain)
        if len(chain) >= 2 and chars > best_chars:
            best_chain, best_chars = chain, chars
        i += 1
    return best_chain[-1] if best_chain else None


def synthesize_mixed_session(
    specs: list[int],
    donor_data: bytes,
    mono_lib: tuple[bytes, int],
    stereo_lib: tuple[bytes, int],
    overview_order: list[int] | None = None,
    target_leaf: str | None = None,
    click_ref: tuple[bytes, bytes] | None = None,
) -> bytes:
    """Synthesize a session with an arbitrary mix of mono (1ch) and stereo (2ch)
    audio tracks, in the given order — optionally with a Click track appended last.

    `specs`         per-track channel counts (1 or 2), in display order. len == N.
    `donor_data`    a session whose first `base_n` tracks already match `specs`
                    (base_n = its track count, must be >= 2 — the 1->2 grow has no
                    0x2589 anchor). The four 2-track donors exist as controls
                    (`2 mono`, `2 stereo`, `mono stereo`, `stereo mono`).
    `mono_lib`/`stereo_lib`  (data, total) uniform controls (>= N tracks) to source
                    each added track's unit from, by track index.
    `overview_order`  the N-track display-order permutation. The order is COSMETIC
                    (Pro Tools accepts any valid permutation of 0..N-1 — confirmed),
                    so it defaults to identity `[0..N-1]`; no matching control is
                    needed. Pass an explicit permutation only to mirror a specific
                    session's display order.
    `target_leaf`   session-folder name to normalize every track's embedded path to
                    (default: the donor's own leaf). Required-in-spirit when mixing
                    types, since mono/stereo units come from different folders.
    `click_ref`     (clean_audio_ctrl, click_audio_ctrl) — a control PAIR whose audio
                    layout matches `specs` (the 2nd = the 1st + a Click track). When
                    given, a Click track is appended LAST via `add_click` (the
                    structural diff-replay in `click_clone`). One click per session;
                    the click contributes 0 audio channels. None = no click.
                    The pair is needed for the same reason the audio synth needs a
                    library: the click's exact bytes are sourced from a real control.
                    PT-confirmed for the all-stereo case (1 audio + click); a mixed
                    (mono+stereo) click still needs a matching mixed click control to
                    validate (TODO).

    Returns full unxored bytes (ready for `writer.encrypt_session_data`). Channel
    indices are rewritten to the cumulative allocation; the index is composed with
    per-track channel counts; the path is normalized; the index-offset pointer is
    fixed. (Overview scroll-extent is set to 0 — correct for N <= 9.)"""
    n = len(specs)
    mono_data, mono_total = mono_lib
    stereo_data, stereo_total = stereo_lib
    if overview_order is None:
        overview_order = list(range(n))  # cosmetic: any valid permutation loads
    if sorted(overview_order) != list(range(n)):
        raise ValueError("overview_order must be a permutation of 0..N-1")
    base_n = len(track_types(donor_data))
    if base_n < 2:
        raise ValueError("donor must have >= 2 tracks (the 1->2 grow has no 0x2589 anchor)")

    body = donor_data[: _FI.final_index_ref(donor_data).start]
    for k in range(base_n + 1, n + 1):
        ch = specs[k - 1]
        lib, total = (mono_data, mono_total) if ch == 1 else (stereo_data, stereo_total)
        unit = extract_track(lib, k, total, channels=ch)
        body = grow_one_track(body, k - 1, unit)
        # Rewrite the just-added track's channel map to the cumulative allocation
        # (its unit carries the source control's channel numbers).
        base = sum(specs[: k - 1])
        new_1014 = _by_type(parse(body), 0x1014)[-1]
        body = set_track_channels(body, new_1014.offset, list(range(base, base + ch)))

    # Transplant the 0x2519 name table from a uniform source that has exactly N
    # tracks (names are "Audio 1..N" regardless of type). The grown table's last
    # entry has a first-child-boundary quirk (reads 2B short); transplanting a real
    # N-entry table reproduces it exactly, as the uniform synthesizer does.
    for src_data, src_total in ((stereo_data, stereo_total), (mono_data, mono_total)):
        if src_total == n:
            body = _set_name_table(body, src_data)
            break

    body = bytearray(body)
    _set_overview_order(body, overview_order)
    _set_overview_extent(body, 0)  # TODO: non-zero threshold once tracks overflow the window (N>9)
    _set_track_selection(body, None)
    body = bytearray(_clear_donor_window_markers(bytes(body), base_n))
    body = bytes(body)

    donor_index = donor_data[_FI.final_index_ref(donor_data).start :]
    index = _FI.compose_index(donor_data, body + donor_index, base_n, n, channels=specs)
    out = _set_index_offset(body + index)

    # Path-normalize the ADDED tracks' source-folder leaf -> the donor's leaf, so
    # mono+stereo tracks (sourced from different control folders) share one path and
    # don't form a chimera. Rename ONLY the leaves of the source folders actually
    # used for added tracks -- NOT every leaf in the body: the donor can carry stale
    # template-remnant path refs (e.g. in 0x2067) whose own path-wrapper length
    # fields rename_track doesn't fix, and those refs are harmless if left alone
    # (the donor loads with them). `target` defaults to the donor's own leaf; a
    # different `target_leaf` would also need the donor's tracks renamed (TODO: the
    # 0x2067 wrapper handling for arbitrary user-chosen session folders).
    target = target_leaf or _folder_leaf(donor_data)
    added = specs[base_n:]
    source_leaves: set[str | None] = set()
    if any(c == 1 for c in added):
        source_leaves.add(_folder_leaf(mono_data))
    if any(c == 2 for c in added):
        source_leaves.add(_folder_leaf(stereo_data))
    for leaf in source_leaves:
        if leaf and target and leaf != target and track_name_occurrences(out, leaf):
            out = rename_track(out, leaf, target)

    # Append the click track LAST (one per session) via the structural diff-replay.
    # Done after the audio session is fully assembled + path-normalized so the click
    # splices onto the final audio body. The click adds 0 channels (0x1015/0x1054 stay).
    if click_ref is not None:
        out = add_click(out, click_ref[0], click_ref[1])
        # Re-set the overview display-order over ALL N+1 entries (audio + click). The
        # audio overview was set to `overview_order` BEFORE the click existed; the click
        # diff (sourced from its control pair) then overwrites some audio entries and
        # appends the click's, which can leave a duplicate (an invalid permutation, e.g.
        # [0,0,2]). Overwrite the full sequence with `overview_order + [n]` (click shown
        # last) -- a valid permutation; order is COSMETIC (any valid permutation loads).
        # Positional u16 overwrite (no size change), so the index is untouched; operate
        # on the body slice only in case the anchor coincidentally appears in the index.
        cref = _FI.final_index_ref(out)
        cb, cidx = bytearray(out[: cref.start]), out[cref.start :]
        _set_overview_order(cb, list(overview_order) + [n])
        out = bytes(cb) + cidx
    return out


# --- index-offset pointer ----------------------------------------------------

def _set_index_offset(data: bytes) -> bytes:
    """The first top-level block (a 4-byte payload) stores the ABSOLUTE offset of
    the trailing 0x0002 master index. Any edit that changes the body size moves
    the index, so this pointer must be rewritten or Pro Tools seeks to the wrong
    place and reports "magic ID does not match". (The value I previously treated
    as a per-N 'counter content_type' is just the low 16 bits of this offset.)"""
    ref = _FI.final_index_ref(data)
    if ref is None:
        return data
    ptf = parse(data)
    first = min(flat_blocks(ptf), key=lambda b: b.offset)
    out = bytearray(data)
    out[first.offset : first.offset + 4] = int(ref.start).to_bytes(4, "little")
    return bytes(out)


# --- arbitrary track naming --------------------------------------------------

def track_name_occurrences(data: bytes, name: str | bytes) -> list[int]:
    """Offsets of every length-prefixed occurrence of a track name. A track name
    lives in 8 places (0x1014, 0x1052 x2, 0x251a x2, 0x210b, the 0x2519 name-table
    entry, and 0x2619), each preceded by a u32 = name length. The length-prefix
    test distinguishes a real name slot from a coincidental substring (and
    'Audio 1' from 'Audio 10')."""
    needle = name.encode() if isinstance(name, str) else bytes(name)
    out: list[int] = []
    pos = 0
    while True:
        j = data.find(needle, pos)
        if j < 0:
            break
        if j >= 4 and int.from_bytes(data[j - 4 : j], "little") == len(needle):
            out.append(j)
        pos = j + 1
    return out


def rename_track(data: bytes, old_name: str | bytes, new_name: str | bytes) -> bytes:
    """Rename a track in a full unxored session. Replaces every length-prefixed
    occurrence of `old_name` with `new_name`, updates each occurrence's u32 length
    prefix, grows/shrinks every containing block's `block_size` (and its
    ancestors, via containment), then recomputes the index offsets for the shifted
    layout. Returns the new unxored session (ready for `writer.encrypt_session_data`)."""
    old = old_name.encode() if isinstance(old_name, str) else bytes(old_name)
    new = new_name.encode() if isinstance(new_name, str) else bytes(new_name)
    if old == new:
        return data
    delta = len(new) - len(old)
    ref = _FI.final_index_ref(data)
    old_index_start = ref.start
    # Capture index holes from the ORIGINAL (consistent) layout: their positions
    # within the index are stable across the rename, and each resolves to a
    # (content_type, rank) block. We re-fill them after the shift -- the index
    # can't be re-parsed once the body moves (its stored offsets no longer match
    # the block zmarks the parser keys on).
    _r, holes = _FI.offset_holes(data)
    body = bytearray(data[:old_index_start])
    index = data[old_index_start:]
    ptf = parse(bytes(body))
    occ = track_name_occurrences(bytes(body), old)
    if not occ:
        raise ValueError(f"track name {old!r} not found (length-prefixed) in body")
    # Bump every containing block's size by (occurrences within it) * delta. A
    # block contains an occurrence iff the name offset lies within [zmark, end);
    # ancestors contain their descendants' occurrences, so this also grows them.
    for b in flat_blocks(ptf):
        s, e = b.offset - 7, b.offset + b.block_size
        cnt = sum(1 for j in occ if s <= j < e)
        if cnt:
            sp = s + 3
            sz = int.from_bytes(body[sp : sp + 4], "little")
            body[sp : sp + 4] = (sz + cnt * delta).to_bytes(4, "little")
    # Also adjust content-internal path-wrapper length fields (`5a 0a 00 <u32 size>`)
    # that span a renamed occurrence. The embedded session-folder path inside a
    # 0x261c (and 0x2067/0x200b) is wrapped by these pseudo-block length fields, but
    # the block parser does NOT descend into them (they aren't in flat_blocks /
    # block_layout), so the loop above misses them. A stale wrapper size leaves Pro
    # Tools reading past the data -> "end of stream". (No-op for track-name renames,
    # whose occurrences never fall inside one of these path wrappers.)
    parsed_zmarks = {b.offset - 7 for b in flat_blocks(ptf)}
    scan = 0
    while True:
        k = body.find(b"\x5a\x0a\x00", scan)
        if k < 0:
            break
        scan = k + 1
        if k in parsed_zmarks:
            continue  # a real (already-bumped) block, not an opaque path wrapper
        size = int.from_bytes(body[k + 3 : k + 7], "little")
        cstart, cend = k + 7, k + 7 + size
        if size <= 0 or cend > len(body):
            continue
        cnt = sum(1 for j in occ if cstart <= j < cend)
        if cnt:
            body[k + 3 : k + 7] = (size + cnt * delta).to_bytes(4, "little")
    # Apply edits high->low so lower offsets stay valid: fix length prefix, splice.
    for j in sorted(occ, reverse=True):
        body[j - 4 : j] = len(new).to_bytes(4, "little")
        body[j : j + len(old)] = new
    # Reattach the index and re-fill each hole at its (stable) index-relative
    # position with the target block's new zmark offset.
    out = bytearray(bytes(body) + index)
    new_index_start = len(body)
    _z2t, by_type = _FI.block_layout(bytes(out))
    for abs_pos, _value, ttype, rank, _kind in holes:
        npos = new_index_start + (abs_pos - old_index_start)
        out[npos : npos + 4] = int(by_type[ttype][rank]).to_bytes(4, "little")
    # The first block stores the index's absolute offset, which moved with the
    # body-size change; rewrite it (else "magic ID does not match").
    return _set_index_offset(bytes(out))


def set_track_names(data: bytes, names) -> bytes:
    """Rename the first `len(names)` tracks (in track order) via `rename_track`. `names`
    is a list (or per-track dict {index: name}); `None`/missing entries are left as-is.
    Captures the current track names first, so it works whether the tracks still have
    their default `Audio N` names or were renamed earlier (and AFTER clips are placed —
    rename_track renames the track plus its placement lanes consistently). Returns unxored
    bytes."""
    current = [t.name for t in track_types(data)]
    if isinstance(names, dict):
        items = names.items()
    else:
        items = enumerate(names)
    out = data
    for i, name in items:
        if name is None or not (0 <= i < len(current)) or current[i] == name:
            continue
        out = rename_track(out, current[i], name)
    return out


def session_name(data: bytes) -> str | None:
    """The embedded session name (the length-prefixed `…​.ptx` string in the
    0x2067 session-info block). Pro Tools titles a session by its filesystem
    filename, so this is metadata, but a synthesized session should carry a
    sensible value. Returns None if not found."""
    ptf = parse(data)
    blocks = [b for b in flat_blocks(ptf) if b.content_type == 0x2067]
    if not blocks:
        return None
    b = blocks[0]
    blk = data[b.offset : b.offset + b.block_size]
    # 0x2067 can hold several `.ptx` references (save-as/template remnants); the
    # primary session name is the FIRST length-prefixed one.
    search = 0
    while True:
        dot = blk.find(b".ptx", search)
        if dot < 0:
            return None
        end = dot + 4
        for start in range(end - 1, max(0, end - 260) - 1, -1):
            if start >= 4 and int.from_bytes(blk[start - 4 : start], "little") == end - start:
                return blk[start:end].decode("latin-1")
        search = end


# --- track-type enumeration (mono / stereo / MIDI) --------------------------
# A track declares its type via its header block:
#   audio -> 0x1014 channel map; the byte after `<type:2><namelen:4><name>` is
#            nchan-1 and the next is nchan (1=mono, 2=stereo).
#   MIDI  -> 0x1057 header (no audio channel map, no 0x1052 lanes).
# (Buses/master/click are not 0x1014/0x1057 audio/MIDI tracks and are skipped.)
_AUDIO_HDR = 0x1014
_MIDI_HDR = 0x1057


@dataclass
class TrackInfo:
    name: str
    kind: str         # "mono" | "stereo" | "midi"
    channels: int     # 2 stereo, 1 mono, 0 midi
    offset: int       # header block offset (for ordering / locating)


def _hdr_name(data: bytes, b: Block) -> tuple[str, int]:
    """(name, namelen) for a 0x1014/0x1057 header block."""
    nlen = int.from_bytes(data[b.offset + 2 : b.offset + 6], "little")
    name = data[b.offset + 6 : b.offset + 6 + nlen].decode("latin-1")
    return name, nlen


def track_types(data: bytes) -> list[TrackInfo]:
    """Enumerate audio/MIDI tracks with their type, in header-offset order.

    Mono vs stereo is read from the 0x1014 channel-count byte; MIDI is any
    0x1057 header. This is the structural basis for mix-and-match composition
    (you must know each track's type + channel footprint to place it)."""
    ptf = parse(data)
    out: list[TrackInfo] = []
    for b in flat_blocks(ptf):
        if b.content_type == _AUDIO_HDR:
            name, nlen = _hdr_name(data, b)
            nchan = data[b.offset + 6 + nlen + 1]
            out.append(TrackInfo(name, "stereo" if nchan == 2 else "mono", nchan, b.offset))
        elif b.content_type == _MIDI_HDR:
            name, _ = _hdr_name(data, b)
            out.append(TrackInfo(name, "midi", 0, b.offset))
    out.sort(key=lambda t: t.offset)
    return out


def channel_count(data: bytes) -> int:
    """Total audio channels (mono=1, stereo=2 per audio track). Mirrors the
    session field at 0x1054 + 2."""
    return sum(t.channels for t in track_types(data))


def set_track_channels(data: bytes, track_offset: int, channels: list[int]) -> bytes:
    """Rewrite the channel indices in a track's 0x1014 channel map (whose content
    starts at `track_offset`). `channels` is the absolute channel index list:
    `[c0]` for mono, `[c0, c1]` for stereo. This is what mix-and-match composition
    uses to place a track at its cumulative channel allocation: a track unit sourced
    by index from a uniform control carries that control's channel numbers, so they
    must be rewritten to the target session's allocation.

    Channel indices live ONLY in 0x1014 (the 0x261c playlist does NOT encode them).
    Positions are relative to the name length (pre-marker) and the 0x2a marker
    (post-ID), so they are correct for BOTH mono and stereo — a mono 0x1014 is 2
    bytes shorter before the marker, so its post-ID channel sits at a different
    absolute offset (marker+19 for both; the bug that mis-wrote mono as if stereo)."""
    out = bytearray(data)
    nlen = int.from_bytes(out[track_offset + 2 : track_offset + 6], "little")
    marker = out.find(b"\x2a\x00\x00\x00", track_offset)
    out[track_offset + nlen + 11] = channels[0]   # channel0 pre-marker
    out[marker + 19] = channels[0]                 # channel0 post-ID
    if len(channels) > 1:
        out[track_offset + nlen + 13] = channels[1]  # channel1 pre-marker
        out[marker + 23] = channels[1]               # channel1 post-ID
    return bytes(out)


def track_channel_indices(data: bytes, track_offset: int) -> list[int]:
    """Read a track's 0x1014 channel indices ([c0] mono / [c0, c1] stereo)."""
    nlen = int.from_bytes(data[track_offset + 2 : track_offset + 6], "little")
    nchan = data[track_offset + 6 + nlen + 1]
    base = track_offset + nlen + 11
    return [data[base]] if nchan != 2 else [data[base], data[base + 2]]


def set_session_name(data: bytes, new_name: str) -> bytes:
    """Set the embedded session name. Accepts a name with or without the `.ptx`
    suffix. Implemented via `rename_track` (the session name is a single
    length-prefixed string in 0x2067), so it handles the body-size shift + index
    + index-offset pointer the same way."""
    cur = session_name(data)
    if cur is None:
        raise ValueError("no 0x2067 session name found")
    new = new_name if new_name.endswith(".ptx") else new_name + ".ptx"
    return rename_track(data, cur, new)
