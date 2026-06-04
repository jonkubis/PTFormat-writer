# Dead code — analysis + removal plan (2026-05-30)

**STATUS: EXECUTED 2026-05-30 — 8 dead symbols moved here from `ptxformatwriter/body_synth.py`.**
The verbatim source of every removed symbol is preserved under "## MOVED CODE" below, so
any removal can be reversed by pasting it back (these files are untracked in git, so this
doc IS the recovery path). Verification run after the cut is recorded at the bottom.
NOTE: `ptxformatwriter/body_synth.py` has NO git history to diff against (untracked) — trust the
"## MOVED CODE" verbatim copies + the test suite, not git.

**RESUME HERE:**
1. `python3 -c "import ptxformatwriter.body_synth"` then `python3 -m unittest discover -s tests`
   — confirm baseline 69 OK / 1 skip before touching anything.
2. Re-run the census (recipe in "EXECUTION RECIPE" below) to re-confirm 0 callers.
3. For each of the 8 symbols: Read its exact current text, append verbatim under a
   `## <name>` heading in "## MOVED CODE", then remove it from body_synth.py with an
   EXACT-TEXT `Edit` (match on text, NOT line numbers — see the † note).
4. After each removal: `python3 -c "import ptxformatwriter.body_synth"` (catches a missed ref).
5. Final: `python3 validate_click.py` (byte_exact=True) + full suite (still 69 OK / 1 skip).
Per the project rule + the user's explicit ask: this is a MOVE (not delete) so any
mistake is reversible by pasting the source back from "## MOVED CODE".

## How "dead" was determined
Caller census across `ptxformatwriter/`, `tests/`, and `validate_click.py` (grep for each
symbol, excluding its own def/class line). Note: NONE of these are exported in
`ptxformatwriter/__init__.py`, so there are no external API consumers to worry about — the
only callers are within the repo.

## CONFIRMED DEAD (0 reachable callers) — safe to move to this file
All in `ptxformatwriter/body_synth.py`. These are the OLD hand-enumerated click path
(superseded by the `click_clone` recursive-diff replay that `add_click` now uses) plus
two stereo back-compat wrappers that nothing calls anymore.

| symbol | kind | def line† | why dead |
|---|---|---|---|
| `extract_stereo_track` | func | 231 | 0 callers; back-compat alias for `extract_track(...,channels=2)` |
| `grow_one_stereo_track` | func | 287 | 0 callers; back-compat alias — AND BUGGY: its body calls **itself** recursively (`return grow_one_stereo_track(...)`), would infinite-loop if ever called; extra proof it's dead |
| `ClickTrackUnit` | dataclass | 296 (`@dataclass` at 295) | only referenced by `extract_click_unit` + `grow_one_click` (both dead) |
| `extract_click_unit` | func | 312 | 0 callers; returns a `ClickTrackUnit` |
| `grow_one_click` | func | 334 | 0 callers; `add_click` no longer uses it |
| `_path_chain_span` | func | 425 | only caller is `_transplant_click_path` (dead) |
| `_splice_region` | func | 453 | only caller is `_transplant_click_path` (dead) |
| `_transplant_click_path` | func | 499 | 0 callers; old `add_click` used it, new one doesn't |

†Def line per grep + Python `ast` (they agree). **Do NOT trust line numbers when cutting —
use EXACT-TEXT `Edit` matching** (it fails safe if the text doesn't match). One Read this
session reported these one line early (an off-by-one from channel garble), which is the
whole reason to match on text, not lines. `add_click` (LIVE, def ~397) sits BETWEEN the
click trio (ends ~394) and the transplant trio (starts ~425) — they are NOT one contiguous
block; remove each function separately, leaving `add_click` and `_patch_u32` (~236) intact.

### Dependency order matters
`_transplant_click_path` is the SOLE caller of `_path_chain_span` and `_splice_region`.
So those two only become dead once `_transplant_click_path` is removed — remove the
trio together. Likewise `ClickTrackUnit` only after `extract_click_unit` + `grow_one_click`.

## CONFIRMED LIVE — DO NOT REMOVE
`add_click` (rewritten to call `click_clone`), `extract_track`, `grow_one_track`
(the channel-aware versions the dead wrappers delegated to), `_set_overview_order`,
`_set_overview_extent`, `_set_track_selection`, `_clear_donor_window_markers`,
`_match_session_info_size`, `_session_info_block`, `_overview_order_offsets`,
`_patch_counts`, `overview_order`, `overview_extent`, `selected_tracks`. All have
≥1 live caller (see /tmp/census.txt while it lasts, or re-run the census).

## EXECUTION RECIPE (on a healthy channel)
1. Re-run the census to re-confirm 0 callers (paste the loop from this session, or):
   `for s in ClickTrackUnit extract_click_unit grow_one_click _path_chain_span _splice_region _transplant_click_path extract_stereo_track grow_one_stereo_track; do echo "## $s"; grep -rn "\b$s\b" ptxformatwriter tests validate_click.py | grep -vE "def $s|class $s"; done`
2. For EACH dead symbol: Read its exact current span, append the verbatim source under a
   `## <name>` heading in the "## MOVED CODE" section below, then delete it from
   `ptxformatwriter/body_synth.py`.
3. `python3 -c "import ptxformatwriter.body_synth"` — must import cleanly (catches a missed ref).
4. `python3 validate_click.py` — must stay `byte_exact=True`.
5. `python3 -m unittest discover -s tests` — must stay **69 OK (skipped=1)**.
6. If all green, commit/leave; if anything red, the verbatim source here lets you paste
   the function straight back.

## MOVED CODE
Verbatim source removed from `ptxformatwriter/body_synth.py` (2026-05-30). To restore any one,
paste it back at the indicated neighborhood with the standard 2 blank lines between defs.

### extract_stereo_track  (was after `extract_track`, before `_patch_u32`)
```python
def extract_stereo_track(data: bytes, track: int, total: int) -> StereoTrackUnit:
    """Back-compat wrapper: extract a 2-channel (stereo) track unit."""
    return extract_track(data, track, total, channels=2)
```

### grow_one_stereo_track  (was after `grow_one_track`, before the click section)
```python
def grow_one_stereo_track(data: bytes, base_n: int, unit: StereoTrackUnit) -> bytes:
    """Back-compat wrapper for the stereo grow (unit carries 2 lanes)."""
    return grow_one_track(data, base_n, unit)
```

### ClickTrackUnit  (dataclass; headed the "click track unit + single-click grow" section)
```python
@dataclass
class ClickTrackUnit:
    """The body blocks a single click track contributes. A click is structurally a
    2-lane "track" whose playlist is `0x261e` (which carries the embedded DigiClick
    plugin instance subtree `0x2627->0x2616->0x2615->0x2613->0x1038`) instead of
    audio's `0x261c`. It has NO `0x1014` channel map, NO `0x1052` audio lanes, and NO
    `0x2519` own-byte name entry — its name "Click 1" lives inside `0x261e`. It also
    registers the DigiClick plugin session-wide via a `0x2064->0x1000` subtree placed
    under the top-level `0x2027` block. (Pro Tools allows ONE click track per session.)"""

    b261e: bytes                # click playlist subtree (brings the plugin instance)
    b2064: bytes                # session-level DigiClick registration (0x2064->0x1000)
    b251a: tuple[bytes, bytes]  # the click's two lanes (lane-major: lane0, lane1)
    b210b: bytes
    b2589: bytes                # overview-order entry (inside the 2nd 0x258a)
```

### extract_click_unit
```python
def extract_click_unit(data: bytes, audio_n: int = 1) -> ClickTrackUnit:
    """Extract the click track's body blocks from a control that has `audio_n` audio
    tracks + 1 click, where the click is the LAST lane-track (e.g. `stereo click`,
    `audio_n=1`). The click is lane-track index `audio_n` (0-based) of `audio_n+1`
    total lane-tracks, so its lane-major `0x251a` lanes are at indices `audio_n`
    (lane0) and `(audio_n+1)+audio_n` (lane1); its `0x210b` is index `audio_n`; its
    `0x2589` is index `audio_n-1` (the count is lane-tracks-1)."""
    ptf = parse(data)
    bb = lambda b: block_bytes(data, b)
    total = audio_n + 1
    a51 = _by_type(ptf, 0x251A)
    a0b = _by_type(ptf, 0x210B)
    a89 = _by_type(ptf, 0x2589)
    return ClickTrackUnit(
        b261e=bb(_by_type(ptf, 0x261E)[0]),
        b2064=bb(_by_type(ptf, 0x2064)[0]),
        b251a=(bb(a51[audio_n]), bb(a51[total + audio_n])),
        b210b=bb(a0b[audio_n]),
        b2589=bb(a89[audio_n - 1]),
    )
```

### grow_one_click
```python
def grow_one_click(data: bytes, base_n: int, unit: ClickTrackUnit) -> bytes:
    """Insert the session's (single) click track into the all-audio body `data` (a
    body without the final index), placed as the LAST track. `base_n` = the current
    audio-track count. Returns the new body.

    The click mirrors a 2-lane audio track's lane/playlist/ordinal insertions
    (`0x251a` lane-major, `0x210b`, `0x2589`, `0x202a` ordinal, `0x261e` under the
    same `0x2624` container) but SKIPS the audio-only blocks (`0x1014`/`0x1052`/the
    `0x2519` name entry) and ADDS the session-level DigiClick registration (`0x2064`
    under the top-level `0x2027`). Count fields all advance to `base_n+1` EXCEPT
    `0x1015` (audio count) and `0x1054` (channels), which stay put — the click is not
    an audio track and contributes 0 channels."""
    n = base_n + 1  # total lane-tracks including the click
    ptf = parse(data)
    pm = _parent_zmarks(ptf)
    ins: list[Insertion] = []

    def append_after(block: Block, blob: bytes) -> None:
        ins.append(Insertion(block.offset + block.block_size, blob, pm[block.offset - 7]))

    # 0x261e click playlist -> after the last 0x261c (same 0x2624 container as audio)
    append_after(_by_type(ptf, 0x261C)[-1], unit.b261e)
    # 0x251a lanes, lane-major (identical placement to grow_one_track's appended track)
    a51 = _by_type(ptf, 0x251A)
    append_after(a51[base_n - 1], unit.b251a[0])
    append_after(a51[-1], unit.b251a[1])
    # 0x210b
    append_after(_by_type(ptf, 0x210B)[-1], unit.b210b)
    # 0x2589 overview entry. For base_n>=2 there are existing 0x2589 to anchor after;
    # for base_n==1 (none yet) insert after the 0x2581 child of the 2nd 0x258a.
    a89 = _by_type(ptf, 0x2589)
    if a89:
        append_after(a89[-1], unit.b2589)
    else:
        a8a = _by_type(ptf, 0x258A)[1]
        child2581 = [c for c in a8a.child if c.content_type == 0x2581][-1]
        ins.append(Insertion(child2581.offset + child2581.block_size, unit.b2589, a8a.offset - 7))
    # session-level DigiClick registration: 0x2064 subtree into the top-level 0x2027,
    # right after its 6-byte header (`27 20 <count:u32>`) -> it becomes 0x2027's child.
    b2027 = _by_type(ptf, 0x2027)[0]
    ins.append(Insertion(b2027.offset + 6, unit.b2064, b2027.offset - 7))
    # 0x202a ordinal append (u16 before the fe ff terminator), like grow_one_track
    for b in _by_type(ptf, 0x202A):
        s = b.offset - 7
        seg = data[s : b.offset + b.block_size]
        idx0 = seg.find(b"\xff\xff\xff\xff\x00\x00")
        feff = seg.find(b"\xfe\xff", idx0 + 8)
        ins.append(Insertion(s + feff, (n - 1).to_bytes(2, "little"), s))

    body = bytearray(apply_insertions(data, ptf, ins))
    # Flip the 0x2027 child count 0 -> 1 (it now contains the 0x2064). Re-find 0x2027:
    # earlier low-offset 0x251a insertions shifted its absolute offset.
    b2027b = _by_type(parse(bytes(body)), 0x2027)[0]
    _patch_u32(body, b2027b.offset + 2, 1)
    # Counts: advance every N-dependent field to n=base_n+1, then restore the audio
    # count (0x1015) to base_n. 0x1054 (channels) stays correct via channel_count
    # (the click adds 0 channels).
    _patch_counts(body, n, channel_count(bytes(body)))
    b1015 = _by_type(parse(bytes(body)), 0x1015)[0]
    _patch_u32(body, b1015.offset + 2, base_n)
    return bytes(body)
```

### _path_chain_span  (sole caller was `_transplant_click_path`)
```python
def _path_chain_span(seg: bytes) -> tuple[int, int]:
    """(start, end) of the longest run of consecutive length-prefixed ASCII
    components (`<u32 len><name>`...) in `seg` -- the embedded filesystem path. Same
    chain `_folder_leaf` walks, but returns the byte span so it can be transplanted."""
    best = (0, 0)
    best_chars = -1
    i = 0
    while i + 4 <= len(seg):
        pos = i
        chars = 0
        comps = 0
        while pos + 4 <= len(seg):
            ln = int.from_bytes(seg[pos : pos + 4], "little")
            if not (1 <= ln <= 64) or pos + 4 + ln > len(seg):
                break
            comp = seg[pos + 4 : pos + 4 + ln]
            if not all(32 <= c < 127 for c in comp):
                break
            chars += ln
            comps += 1
            pos += 4 + ln
        if comps >= 2 and chars > best_chars:
            best = (i, pos)
            best_chars = chars
        i += 1
    return best
```

### _splice_region  (sole caller was `_transplant_click_path`)
```python
def _splice_region(data: bytes, abs_start: int, old_len: int, new_bytes: bytes) -> bytes:
    """Replace `data[abs_start:abs_start+old_len]` with `new_bytes`, bumping every
    enclosing block's `block_size` (and any content-internal `5a 0a 00` path-wrapper)
    that spans the region, then re-filling the master-index offsets for the shift and
    fixing the index-offset pointer. (The byte-splice analog of `rename_track`, for a
    whole path chain rather than one length-prefixed string.)"""
    delta = len(new_bytes) - old_len
    if delta == 0 and data[abs_start : abs_start + old_len] == new_bytes:
        return data
    ref = _FI.final_index_ref(data)
    old_index_start = ref.start
    _r, holes = _FI.offset_holes(data)
    body = bytearray(data[:old_index_start])
    index = data[old_index_start:]
    ptf = parse(bytes(body))
    for b in flat_blocks(ptf):
        s, e = b.offset - 7, b.offset + b.block_size
        if s <= abs_start < e:  # an enclosing block (self + every ancestor)
            sp = s + 3
            sz = int.from_bytes(body[sp : sp + 4], "little")
            body[sp : sp + 4] = (sz + delta).to_bytes(4, "little")
    parsed_zmarks = {b.offset - 7 for b in flat_blocks(ptf)}
    scan = 0
    while True:
        k = body.find(b"\x5a\x0a\x00", scan)
        if k < 0:
            break
        scan = k + 1
        if k in parsed_zmarks:
            continue
        size = int.from_bytes(body[k + 3 : k + 7], "little")
        cstart, cend = k + 7, k + 7 + size
        if size <= 0 or cend > len(body):
            continue
        if cstart <= abs_start < cend:
            body[k + 3 : k + 7] = (size + delta).to_bytes(4, "little")
    body[abs_start : abs_start + old_len] = new_bytes
    out = bytearray(bytes(body) + index)
    new_index_start = len(body)
    _z2t, by_type = _FI.block_layout(bytes(out))
    for abs_pos, _value, ttype, rank, _kind in holes:
        npos = new_index_start + (abs_pos - old_index_start)
        out[npos : npos + 4] = int(by_type[ttype][rank]).to_bytes(4, "little")
    return _set_index_offset(bytes(out))
```

### _transplant_click_path
```python
def _transplant_click_path(data: bytes, click_lib: bytes) -> bytes:
    """Replace the click_lib session path embedded in the cloned 0x2064 (DigiClick
    registration) with the audio session's path, eliminating the cross-folder chimera.

    The 0x2064 ALSO holds the DigiClick plugin-settings path
    (`Macintosh HD/.../DigiclikCkRTFact.tfx`) -- a SYSTEM path that must NOT be
    touched. That path is actually longer than the session path, so "replace the
    longest chain" is wrong; instead match the EXACT click_lib session-path byte
    chain (taken from click_lib's own first 0x261c, which the cloned 0x2064 carries
    verbatim) and swap only that."""
    ptf = parse(data)
    a1c = sorted((b for b in flat_blocks(ptf) if b.content_type == 0x261C), key=lambda b: b.offset)
    g64 = [b for b in flat_blocks(ptf) if b.content_type == 0x2064]
    if not a1c or not g64:
        return data
    seg = data[a1c[0].offset - 7 : a1c[0].offset + a1c[0].block_size]
    acs, ace = _path_chain_span(seg)
    audio_chain = seg[acs:ace]
    lptf = parse(click_lib)
    l1c = sorted((b for b in flat_blocks(lptf) if b.content_type == 0x261C), key=lambda b: b.offset)
    if not audio_chain or not l1c:
        return data
    lseg = click_lib[l1c[0].offset - 7 : l1c[0].offset + l1c[0].block_size]
    lcs, lce = _path_chain_span(lseg)
    click_chain = lseg[lcs:lce]
    if not click_chain or click_chain == audio_chain:
        return data
    g = g64[0]
    pos = data.find(click_chain, g.offset - 7, g.offset + g.block_size)
    if pos < 0:
        return data
    return _splice_region(data, pos, len(click_chain), audio_chain)
```

## VERIFICATION (post-removal, 2026-05-30)
- `grep` for all 8 dead defs in `body_synth.py` → **0 remaining**; grep for any lingering
  references to the 8 names → **0**. Live symbols intact (`add_click`, `extract_track`,
  `grow_one_track`, `_patch_counts`, `_HEADER_COUNTS`, `_set_overview_order`,
  `synthesize_mixed_session` all present).
- `python3 -c "import ptxformatwriter.body_synth; import ptxformatwriter.click_clone"` → **IMPORT_OK**.
- `python3 validate_click.py` → **byte_exact=True** (unchanged).
- `ptxformatwriter/body_synth.py`: **1290 → 1090 lines** (200 lines of dead code removed).
- Full suite (`python3 -m unittest discover -s tests`): **69 OK (skipped=1)** — identical
  to the pre-removal baseline. Cleanup verified safe. ✅
