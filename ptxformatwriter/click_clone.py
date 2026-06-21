"""Structural click-track synthesis by diff-and-replay (the robust path).

The click is the most entangled track type: besides its own playlist (0x261e + a
click-plugin block) and lanes, it adds a "Click 1" name-table entry (0x2519), grows
the 0x1018 plugin registry, adds TWO overview entries (0x2589 in 0x258a), and even
attaches a nested chain (0x200b->0x200a->0x2015->0x2104->0x2103) INSIDE the audio
track's 0x261c. A hand-enumerated block transplant is fragile.

Instead, derive the click's contribution as a DIFF between a clean control pair
that shares the same audio — `1 stereo tracks.ptx` (donor) and `1 stereo plus
click.ptx` (donor + a real click) — and replay that contribution onto a target
audio session. Validation: replaying onto the donor must reproduce the control
byte-for-byte (modulo the session-name string in 0x2067).

The diff is a RECURSIVE STRUCTURAL DIFF: for every block common to donor and
control (matched by signature), align the children by signature and replace the
divergent suffix (the bytes after the last common child) wholesale. This single
mechanism captures every kind of change uniformly — new child subtrees, grown
blocks (0x1018/0x1017), the 0x2519 "Click 1" name entry, the 0x258a overview's
own-content reframing (a 270B tail -> two real 0x2589 entries), and the 0x261c
chain — without per-type special cases or insert/grow/duplicate ambiguity. An
earlier per-subtree-insert model double-applied the 0x261c chain and could not
express the overview's own-content replacement; the structural diff fixes both.

Status 2026-05-30: see docs/click-apply-findings-2026-05-30b.md.
The index machinery (final_index) is already byte-exact for a real click body.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import body_synth as BS
from . import final_index as _FI

_NAME = re.compile(rb"(?:Audio|Click|MIDI) (\d+)")


@dataclass
class _Node:
    t: int          # content_type
    z: int          # zmark
    off: int        # content offset (z+7)
    end: int        # z + 7 + block_size
    par: "int | None"
    name: "str | None"
    kids: list      # child zmarks, offset-sorted


def _nodes(data: bytes) -> dict:
    ptf = BS.parse(data)
    out: dict = {}

    def rec(b, par):
        z = b.offset - 7
        seg = data[b.offset : b.offset + b.block_size]
        m = _NAME.search(seg)
        kids = sorted(b.child, key=lambda x: x.offset)
        out[z] = _Node(b.content_type, z, b.offset, b.offset + b.block_size, par,
                       (m.group(0).decode() if m else None), [c.offset - 7 for c in kids])
        for c in kids:
            rec(c, z)

    for b in sorted(ptf.blocks, key=lambda x: x.offset):
        rec(b, None)
    return out


def _sigs(nd: dict) -> dict:
    """zmark -> signature: path of (type, occ-among-same-(type,name), name).

    Occurrence is counted among siblings of the SAME (type, name) so it stays stable
    when the click interleaves a differently-named sibling (e.g. a 'Click 1' 0x251a
    lane between the 'Audio 1' lanes must NOT renumber the audio lanes). The single
    lowest-offset top-level block (the index-offset pointer, whose 'type' is the
    pointer's low-16 bits and so differs per file) gets a fixed ('FIRST',) sig."""
    sig: dict = {}
    roots = [z for z in nd if nd[z].par is None]
    first = min(roots, key=lambda z: nd[z].off) if roots else None

    def occ(z):
        n = nd[z]
        if n.par is None:
            sibs = [x for x in nd if nd[x].par is None and nd[x].t == n.t and nd[x].name == n.name]
        else:
            sibs = [x for x in nd[n.par].kids if nd[x].t == n.t and nd[x].name == n.name]
        return sorted(sibs).index(z)

    def build(z):
        n = nd[z]
        if z == first:
            return (("FIRST",),)
        s = (n.t, occ(z), n.name)
        return (s,) if n.par is None else build(n.par) + (s,)

    for z in nd:
        sig[z] = build(z)
    return sig


@dataclass
class ClickPatch:
    """The click's contribution as byte-region replacements against the donor body.

    `replacements`: list of (donor_start, donor_end, ctrl_bytes, owner_zmark). Replace
    the donor body's `[donor_start, donor_end)` with `ctrl_bytes`; `owner_zmark` is the
    donor block whose content contains that region (its `block_size` + every ancestor
    bump by the net length delta). Regions are non-overlapping and offset-sorted.

    Produced by `derive_click_patch`'s recursive structural diff; consumed by
    `apply_click_patch`. (Coordinates are in the DONOR body, which for the validation
    case IS the apply target.)"""

    replacements: list = field(default_factory=list)


_FIRST_SIG = (("FIRST",),)


def derive_click_patch(donor: bytes, ctrl: bytes) -> ClickPatch:
    """Diff a clean (donor, donor+click) pair into a replayable ClickPatch.

    Recursive structural diff (BODY-ONLY; the trailing 0x0002 index is recomposed,
    not cloned). For each block common to both (matched by signature), align the
    children by signature prefix, recurse into common children, and replace the
    divergent suffix — the bytes after the last common child, through the block's
    content end — with the control's corresponding suffix. The index-pointer first
    block is skipped (restored by `_set_index_offset`)."""
    dbody = donor[: _FI.final_index_ref(donor).start]
    cbody = ctrl[: _FI.final_index_ref(ctrl).start]
    dn, cn = _nodes(dbody), _nodes(cbody)
    ds, cs = _sigs(dn), _sigs(cn)
    patch = ClickPatch()
    reps = patch.replacements

    def diff_block(dz: int, cz: int) -> None:
        if dbody[dz : dn[dz].end] == cbody[cz : cn[cz].end]:
            return  # identical subtree
        d_kids = sorted(dn[dz].kids, key=lambda z: dn[z].off)
        c_kids = sorted(cn[cz].kids, key=lambda z: cn[z].off)
        # match the common-prefix children by signature
        m = 0
        while m < len(d_kids) and m < len(c_kids) and ds[d_kids[m]] == cs[c_kids[m]]:
            m += 1
        # Walk the matched children, replacing the own-content GAP before each (the gap
        # before the FIRST child is the block's own-content prefix — e.g. the 0x2519
        # name table that holds the "Click 1" entry — which a suffix-only diff misses),
        # recursing into each matched child, then replacing the divergent suffix (own
        # tail + any unmatched/new trailing children).
        d_prev, c_prev = dn[dz].off, cn[cz].off
        for k in range(m):
            dk, ck = d_kids[k], c_kids[k]
            if dbody[d_prev : dn[dk].z] != cbody[c_prev : cn[ck].z]:
                reps.append((d_prev, dn[dk].z, cbody[c_prev : cn[ck].z], dz))
            diff_block(dk, ck)
            d_prev, c_prev = dn[dk].end, cn[ck].end
        if dbody[d_prev : dn[dz].end] != cbody[c_prev : cn[cz].end]:
            reps.append((d_prev, dn[dz].end, cbody[c_prev : cn[cz].end], dz))

    d_roots = sorted((z for z in dn if dn[z].par is None), key=lambda z: dn[z].off)
    c_roots = sorted((z for z in cn if cn[z].par is None), key=lambda z: cn[z].off)
    i = 0
    while i < len(d_roots) and i < len(c_roots) and ds[d_roots[i]] == cs[c_roots[i]]:
        if ds[d_roots[i]] != _FIRST_SIG:
            diff_block(d_roots[i], c_roots[i])
        i += 1
    # Trailing top-level blocks: the body suffix after the last common root (the click
    # appends a trailing top-level 0x0 null block before the index). owner=None -> no
    # container size bump; the index recompose + index-offset pointer absorb the shift.
    split_d = dn[d_roots[i - 1]].end if i > 0 else 0
    split_c = cn[c_roots[i - 1]].end if i > 0 else 0
    if dbody[split_d:] != cbody[split_c:]:
        reps.append((split_d, len(dbody), cbody[split_c:], None))
    reps.sort(key=lambda r: r[0])
    return patch


def _chain(nd: dict, z: "int | None") -> list:
    """`z` and every ancestor of it (walking `.par`), as a zmark list. These are
    exactly the blocks whose `block_size` must grow when bytes inside the block at
    `z` change size."""
    out: list = []
    while z is not None:
        out.append(z)
        z = nd[z].par
    return out


def apply_click_patch(target: bytes, patch: ClickPatch, donor_index_src: bytes) -> bytes:
    """Replay a ClickPatch onto a target audio session (full unxored bytes).

    The patch's replacement coordinates are in the donor body, which for validation
    is `target` itself. For each replacement, bump the owner block's `block_size`
    (and every ancestor's) by the net length delta, then rebuild the body in one
    forward merge (offset-sorted, non-overlapping splices). Finally patch the track
    counts, compose the click-aware index, and fix the index-offset pointer.

    `donor_index_src` supplies the index donor for `compose_index` (same track layout
    as `target` before the click)."""
    return _apply_resolved(target, list(patch.replacements), donor_index_src)


def _merge_reps(target: bytes, reps: list) -> bytes:
    """Bump each owner+ancestor `block_size` by its net length delta, then forward-merge the
    splices (offset-sorted, non-overlapping). `reps` = [(start, end, new_bytes, owner_zmark)]
    in TARGET-body coordinates. Returns the grown body (no index)."""
    ref = _FI.final_index_ref(target)
    body0 = target[: ref.start]
    tn = _nodes(body0)
    reps = sorted(reps, key=lambda r: r[0])
    size_delta: dict = {}
    for start, end, new_bytes, owner in reps:
        d = len(new_bytes) - (end - start)
        if d == 0:
            continue
        for z in _chain(tn, owner):
            size_delta[z] = size_delta.get(z, 0) + d
    out = bytearray(body0)
    for z, d in size_delta.items():
        sp = z + 3
        sz = int.from_bytes(out[sp : sp + 4], "little")
        out[sp : sp + 4] = (sz + d).to_bytes(4, "little")
    res = bytearray()
    cursor = 0
    for start, end, new_bytes, _owner in reps:
        res += out[cursor:start]
        res += new_bytes
        cursor = end
    res += out[cursor:]
    return bytes(res)


def _finish_click(body: bytes, donor_index_src: bytes) -> bytes:
    """Patch the click-added track counts (0x2107/0x2624/0x202a +1; 0x1015/0x1054 stay),
    compose the click-aware 0x0002 index, and fix the index-offset pointer."""
    # ALL tracks (audio + MIDI) in INDEX/lane order — NOT track_types' type-grouped order, which
    # misaligns the per-track names/channels with the index positions once MIDI is present. (For an
    # audio-only session lane order == track_types order, so this is byte-identical there.)
    channels = BS._internal_channel_order(donor_index_src)   # 1/2 = audio, 0 = MIDI, lane order
    base_n = len(channels)                                   # total tracks before the click
    names = BS._lane_order_track_names(donor_index_src, base_n)
    n_audio = sum(1 for c in channels if c > 0)
    midi_tracks = {i + 1 for i, c in enumerate(channels) if c == 0}
    bodyb = bytearray(body)
    BS._patch_counts(bodyb, base_n + 1, BS.channel_count(body))
    b1015 = BS._by_type(BS.parse(bytes(bodyb)), 0x1015)[0]
    BS._patch_u32(bodyb, b1015.offset + 2, n_audio)          # 0x1015 = AUDIO track count (MIDI adds 0)
    body = bytes(bodyb)
    donor_index = donor_index_src[_FI.final_index_ref(donor_index_src).start :]
    # The MIDI playlists (0x2620) already live in the donor index, so only the click's 0x261e is new
    # — no 0x261E/0x2620 _fill_offsets collision (that only bites when click AND MIDI are both grown).
    index = _FI.compose_index(donor_index_src, body + donor_index, base_n, base_n + 1,
                              channels=channels + [0], track_names=names + ["Click 1"],
                              click_tracks={base_n + 1}, midi_tracks=midi_tracks)
    return BS._set_index_offset(body + index)


def _apply_resolved(target: bytes, reps: list, donor_index_src: bytes) -> bytes:
    """Core replay (matching-layout path): merge the resolved byte-region replacements into the
    body, then finish (counts + index). `reps` = [(start, end, new_bytes, owner_zmark)] in
    TARGET-body coordinates. Used by `apply_click_patch`; the cross-N path
    (`apply_click_patch_structural`) inserts a 0x2519 rebuild between merge and finish."""
    return _finish_click(_merge_reps(target, reps), donor_index_src)


# --- layout-independent re-key (Frontier 1: synthesize a click onto ANY-N session) ----
#
# `ClickPatch`/`apply_click_patch` use absolute donor-body offsets, so they only work when the
# target IS the donor (byte-identical layout). To splice a click onto a DIFFERENT-size session,
# re-key the patch to OWNER-SIGNATURE + offset and apply with cross-N handling. PT-CONFIRMED
# 2026-05-30 at N=3 and N=12 (stereo). See docs/click-on-top-findings-2026-05-30.md.
#
# The click's block footprint is FIXED (same blocks for any N); cross-N is a PLACEMENT problem:
#   1. END-ANCHORING: a rep ending at its owner's content end resolves vs the TARGET owner's
#      (grown) end — containers like 0x2624 grow with N, so offset-from-start lands mid-block.
#   2. Per-track templates (the 0x261b rewrite) are replicated ONCE per target audio track.
#   3. The 0x2519 rep (name table + lane-major 0x251a) is SKIPPED and rebuilt via grow: insert
#      the click's 2 lanes lane-major (lane-0 after the last audio lane-0, lane-1 at the end) +
#      its name-table addition. (The patch's 0x2519 rep wholesale-replaced a tail holding
#      per-track audio lane data, which clobbered a lane.)
#   4. The 0x2067 rep (session name/path) is SKIPPED — metadata, not a click change.
# Two subtleties that caused Pro Tools "end of stream" until fixed:
#   - the name-table addition MUST include the `02 00` inter-entry separator before the
#     "Click 1" entry (a single-entry extract drops it -> PT reads a wrong entry length -> EOS).
#     We append the exact addition = click_name_region[len(clean_name_region):].
#   - the click's own bytes (its 2 lanes + name entry) embed the SOURCE control's track count
#     (src_audio+1) as a u16 right after each `2a 00 00 00 <8B GUID>`; re-stamp it to the
#     TARGET's track count (target_audio+1).

_AUDIO_NAME = re.compile(r"Audio \d+$")


@dataclass
class StructuralClickPatch:
    """A `ClickPatch` re-keyed for replay onto a session of a DIFFERENT audio-track count.
    `sreps` are owner-signature-anchored byte-region replacements (kind 'start'/'end'/'suffix').
    `lane_bytes`/`name_addition` are the click's 0x2519 contribution (rebuilt via grow, not
    spliced). `src_audio_n` is the source control's audio-track count (to re-stamp the track
    count baked into the click's own bytes)."""

    sreps: list = field(default_factory=list)
    lane_bytes: list = field(default_factory=list)
    name_addition: bytes = b""
    src_audio_n: int = 0


def _name_table_region(data: bytes) -> bytes:
    """The 0x2519 name-table own-bytes (content start .. first child)."""
    b = [x for x in BS.flat_blocks(BS.parse(data)) if x.content_type == 0x2519][0]
    first_child = min(c.offset - 7 for c in b.child)
    return data[b.offset : first_child]


def _extract_click_2519(click_ctrl: bytes, clean_ctrl: bytes) -> tuple:
    """The click's 2 0x251a lane blocks (offset order) + its name-table addition. The addition
    = click_region[len(clean_region):] (the trailing bytes the click appends), which captures
    the `02 00` separator + the full 'Click 1' entry that a single-entry extract would drop."""
    ptf = BS.parse(click_ctrl)
    lanes = sorted((b for b in BS.flat_blocks(ptf) if b.content_type == 0x251a
                    and b"Click 1" in click_ctrl[b.offset : b.offset + b.block_size]),
                   key=lambda b: b.offset)
    lane_bytes = [click_ctrl[b.offset - 7 : b.offset + b.block_size] for b in lanes]
    clean_region, click_region = _name_table_region(clean_ctrl), _name_table_region(click_ctrl)
    return lane_bytes, click_region[len(clean_region):]


def _restamp_track_count(b: bytes, src_n: int, tgt_n: int) -> bytes:
    """Re-stamp the session track count embedded in the click's own bytes: a u16 == src_n+1
    right after each `2a 00 00 00 <8B GUID>` becomes tgt_n+1."""
    out = bytearray(b)
    src_val, tgt_val = src_n + 1, tgt_n + 1
    i = 0
    while True:
        j = out.find(b"\x2a\x00\x00\x00", i)
        if j < 0 or j + 14 > len(out):
            break
        if int.from_bytes(out[j + 12 : j + 14], "little") == src_val:
            out[j + 12 : j + 14] = tgt_val.to_bytes(2, "little")
        i = j + 4
    return bytes(out)


def _insert_click_2519(body: bytes, lane_bytes: list, name_addition: bytes,
                       audio_n: int, src_n: int) -> bytes:
    """Insert the click's two 0x251a lanes lane-major (lane-0 after the LAST lane-0 of any kind,
    lane-1 at the end) + its name-table addition into the body's 0x2519, re-stamping the track
    count from the source control's to the target's.

    The boundary + the re-stamped count are the TOTAL track count (audio + MIDI + ...), not just
    audio: 0x251a is lane-major `[N lane-0][N lane-1]` over ALL tracks (every track — audio or
    MIDI — has 2 lanes), and the click is the (N+1)-th track. Using audio_n would land the click's
    lane-0 mid-group when MIDI tracks have lane-0s after the audio ones, leaving the index's
    0x2519 lane refs unresolved (PT 'end of stream'). N = len(0x251a)//2."""
    ptf = BS.parse(body)
    pm = BS._parent_zmarks(ptf)
    a51 = BS._by_type(ptf, 0x251a)
    n_tracks = len(a51) // 2  # ALL tracks (audio + MIDI), 2 lanes each — not audio_n
    lanes = [_restamp_track_count(l, src_n, n_tracks) for l in lane_bytes]
    name_addition = _restamp_track_count(name_addition, src_n, n_tracks)
    lane0_after, last = a51[n_tracks - 1], a51[-1]
    ins = [
        BS.Insertion(lane0_after.offset + lane0_after.block_size, lanes[0], pm[lane0_after.offset - 7]),
        BS.Insertion(last.offset + last.block_size, lanes[1], pm[last.offset - 7]),
    ]
    b2519 = BS._by_type(ptf, 0x2519)[0]
    first_child = min(c.offset - 7 for c in b2519.child)
    ins.append(BS.Insertion(first_child, name_addition, b2519.offset - 7))
    return BS.apply_insertions(body, ptf, ins)


def _per_track_templates(sreps: list) -> set:
    """Reps that recur on >=2 distinct audio-named owners (the 0x261b per-track rewrite): these
    are replicated once per target audio track."""
    groups: dict = {}
    for r in sreps:
        if r["kind"] in ("start", "end") and r.get("owner_name") and _AUDIO_NAME.match(r["owner_name"]):
            key = (r["owner_t"], r["kind"], r.get("start_rel"), r.get("start_from_end"))
            groups.setdefault(key, []).append(r)
    return {k for k, v in groups.items() if len({r["owner_name"] for r in v}) >= 2}


def derive_click_patch_structural(donor: bytes, ctrl: bytes) -> StructuralClickPatch:
    """Diff a clean/click control PAIR into a layout-independent `StructuralClickPatch`.
    `donor` (clean) should have >= 2 audio tracks so per-track reps are detectable."""
    patch = derive_click_patch(donor, ctrl)
    dbody = donor[: _FI.final_index_ref(donor).start]
    nd = _nodes(dbody)
    sg = _sigs(nd)
    roots = sorted((z for z in nd if nd[z].par is None), key=lambda z: nd[z].off)
    sreps: list = []
    for (start, end, new_bytes, owner) in patch.replacements:
        if owner is None:
            pred = max((z for z in roots if nd[z].end <= start), key=lambda z: nd[z].end, default=None)
            sreps.append(dict(kind="suffix", pred_sig=(sg[pred] if pred is not None else None),
                              start_from_predend=start - (nd[pred].end if pred is not None else 0),
                              new=new_bytes))
        else:
            n = nd[owner]
            if end == n.end:   # END-anchored (tracks a grown owner's end)
                sreps.append(dict(kind="end", owner_sig=sg[owner], owner_t=n.t, owner_name=n.name,
                                  start_from_end=start - n.end, new=new_bytes))
            else:              # START-anchored (prefix/interior)
                sreps.append(dict(kind="start", owner_sig=sg[owner], owner_t=n.t, owner_name=n.name,
                                  start_rel=start - n.off, end_rel=end - n.off, new=new_bytes))
    lane_bytes, name_addition = _extract_click_2519(ctrl, donor)
    src_audio_n = len([t for t in BS.track_types(donor) if t.kind in ("mono", "stereo")])
    return StructuralClickPatch(sreps=sreps, lane_bytes=lane_bytes,
                                name_addition=name_addition, src_audio_n=src_audio_n)


def apply_click_patch_structural(target: bytes, spatch: StructuralClickPatch,
                                 donor_index_src: bytes) -> bytes:
    """Replay a `StructuralClickPatch` onto `target` (any audio-track count). Resolve each rep
    against the target's block signatures (end-anchored where appropriate), replicate per-track
    templates across the target's audio tracks, SKIP the 0x2519/0x2067 reps (0x2519 is rebuilt
    via grow; 0x2067 session name stays the target's), then finish (counts + index).
    `donor_index_src` is the target audio session before the click (usually `target`)."""
    body0 = target[: _FI.final_index_ref(target).start]
    tn = _nodes(body0)
    tsg = _sigs(tn)
    sig2z: dict = {}
    for z in tn:
        sig2z.setdefault(tsg[z], z)
    by_type_name: dict = {}
    for z in tn:
        if tn[z].name and _AUDIO_NAME.match(tn[z].name):
            by_type_name[(tn[z].t, tn[z].name)] = z
    target_audio = [t.name for t in BS.track_types(target) if t.kind in ("mono", "stereo")]
    templates = _per_track_templates(spatch.sreps)

    def place(z, r):
        o_off, o_end = tn[z].off, tn[z].end
        if r["kind"] == "end":
            return (o_end + r["start_from_end"], o_end, r["new"], z)
        return (o_off + r["start_rel"], o_off + r["end_rel"], r["new"], z)

    tmpl: dict = {}
    for r in spatch.sreps:
        if r["kind"] in ("start", "end") and r.get("owner_name") and _AUDIO_NAME.match(r["owner_name"]):
            key = (r["owner_t"], r["kind"], r.get("start_rel"), r.get("start_from_end"))
            if key in templates:
                tmpl.setdefault(key, []).append(r)
    handled = {id(r) for v in tmpl.values() for r in v}
    resolved: list = []
    for r in spatch.sreps:
        if id(r) in handled:
            continue
        if r.get("owner_t") in (0x2519, 0x2067):   # 0x2519 rebuilt via grow; 0x2067 = session name
            continue
        if r["kind"] == "suffix":
            pz = sig2z.get(r["pred_sig"]) if r["pred_sig"] is not None else None
            base = tn[pz].end if pz is not None else 0
            resolved.append((base + r["start_from_predend"], len(body0), r["new"], None))
            continue
        z = sig2z.get(r["owner_sig"])
        if z is not None:
            resolved.append(place(z, r))
    for key, reps in tmpl.items():
        by_name = {r["owner_name"]: r for r in reps}
        for nm in target_audio:                          # one application per target audio track
            z = by_type_name.get((key[0], nm))
            if z is not None:
                resolved.append(place(z, by_name.get(nm, reps[0])))

    body = _merge_reps(target, resolved)
    body = _insert_click_2519(body, spatch.lane_bytes, spatch.name_addition,
                              len(target_audio), spatch.src_audio_n)
    return _finish_click(body, donor_index_src)
