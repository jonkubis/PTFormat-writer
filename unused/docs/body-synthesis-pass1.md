# Body Synthesis (Pass 1) — Characterization & Build Spec

> # ⚠️ THE #1 GOTCHA: THE FIRST-BLOCK INDEX-OFFSET POINTER ⚠️
>
> ## **The first top-level block (ZMARK @ 0x14, type 0x0001, size 4) holds the ABSOLUTE OFFSET of the 0x0002 index — NOT a counter.**
>
> **If you change the body size in ANY way (rename a track, change the session
> name, add the 17th track, add clips), the index moves and you MUST rewrite this
> 4-byte pointer (file offset 27) to the new index start — otherwise Pro Tools
> fails to open with "magic ID does not match".** Use `_set_index_offset(data)`.
> The "per-N counter content_type" (0x2504/0x102d/0x105a/…) is just the low 16
> bits of this same pointer. This was the root cause of every shift-related
> "magic ID" failure (session-name saga + track renaming). See
> `docs/final-index-0x0002-schema.md` for the full writeup.

Status: characterized 2026-05-28 against `control_files/lots of stereo tracks/`
(empty stereo sessions, N = 1..16). Pairs with `docs/final-index-0x0002-schema.md`
(Pass 2, the index — already implemented in `ptxformatwriter/final_index.py`).

> **✅ VALIDATED (2026-05-28): a synthesized 3-track session OPENS in Pro Tools
> with 3 audio tracks.** Built from an n=2 donor by inserting track-3's per-track
> subtree (lane-major positions) + the `0x2519` name-entry + `0x202a` ordinal +
> `0x1015` count + the per-N counter + the 18 N-dependent count fields below +
> `compose_index` + encrypt. The cumulative `0x261c`/`0x261b` counters are
> **cosmetic** (v6 had them wrong and still loaded). The empty-stereo body+index
> pipeline works end-to-end; remaining work is generalization (from-scratch
> content generation, multi-step N, other track types). Working reference:
> `/tmp/reconstruct_v2.py`.

## Multi-step growth & the overview display-order permutation (2026-05-28)

Single-step growth (n→n+1) is Pro-Tools-validated (the 3-track file). Growing in
**multiple steps** (e.g. 2→8, 2→16) initially failed "magic ID" for one reason: a
hidden **overview display-order permutation**.

Inside the **second `0x258a` block**, each track contributes one record
`89 25 <order:u16> 01 00 61 00 5a 01 …` (`89 25` = `0x2589` content_type; `<order>`
is the first content word). Across an N-track session the N order values form a
**permutation of 0..N−1** — the iteration order of a hash table keyed on track
identity. It reshuffles wholesale for small N (n4=`[2,3,1,0]`, n5=`[3,4,0,1,2]`,
n8=`[5,0,7,3,2,1,6,4]`) and stabilises into pure insertions for N≥8. There is **no
cheap closed form**.

Growing track-by-track corrupts it: the donor's original tracks keep their low-N
order values while appended tracks bring the library's values, yielding an
**invalid sequence with duplicate values** (2→4 produced `[1,3,1,3]`). A permutation
with duplicates is structurally invalid → rejection.

**Fix (`body_synth`):** don't try to derive/patch it per step. After the body is
grown, `synthesize_stereo_session` rewrites the whole sequence in one shot via
`_set_overview_order(body, overview_order(library))`, copying the exact N order
values positionally from the target control (requires `library_total == target_n`).
`overview_order(data)` reads them; `_overview_order_offsets` locates the `<order>`
u16 two bytes before each `01 00 61 00 5a 01` anchor. With this, 2→3 still equals
the validated v6 byte-for-byte, and 2→4/2→5/2→8/2→16 reproduce each control's
permutation exactly. **Open for arbitrary N>16 / mismatched library:** decode the
hash order (or carry a generator); currently raises `NotImplementedError`.

After this fix, a block-aligned content diff of synth-2→8 vs the real 8-track
control shows **every remaining differing byte equals the n2 donor's** — i.e. only
benign per-session identity data (8-byte GUID/hashes, cumulative `0x261b` clip
counters, session-name strings). No N-dependent field is left unpatched.

## N-dependent count fields (the "end of stream" fix)

These scalars encode the track count; Pro Tools reads them to know how many
entries to expect (the Python parser ignores them). Patch each to its formula.
Offsets are zmark-relative within the given occurrence:

| Block#occ | offset | value | width |
|---|---|---|---|
| `0x1015`#0 | +9 | N | u32 |
| `0x1054`#0 | +9 | 2N | u32 |
| `0x202a`#0, `0x202a`#1 | +25 | N | u32 |
| `0x202b`#0 | +38, +158 | N | u32 |
| `0x2107`#0 | +18 | N | u32 |
| `0x2519`#0 | +23 | N | u32 |
| `0x2519`#0 | +387 (deep) | N | u32 |
| `0x2551`#0 / `0x2587`#0 | +1095 / +1104 | N | u32 |
| `0x258a`#1 / `0x258b`#1 | +323 / +1016 | N | u32 |
| `0x2624`#0 | +9 | N | u32 |
| overview `0x2587`/`0x2551`/`0x258b`/`0x258a` (2nd count) | +1649/+1640/+1561/+868 | **N−1** | **u16** |

Gotchas: `0x258a`/`0x258b` counts are in occurrence **#1** (overview subtree, not
the `0x2016`-child #0); overview blocks have **two** N-deps each (one `=N`, one
`=N−1`); patch with the correct **width** or you clobber the adjacent field. The
deep ones (after the per-track insert) are best patched post-insertion or located
relative to a stable anchor. Enumeration method: mask FILETIMEs (`51 c3 3b e6`),
`SequenceMatcher`-align the block across n2/n3/n4, classify each differing u32.

Pass 1 builds the session **body** (every top-level block except the trailing
`0x0002` index) for N tracks. Pass 2 then regenerates the index.

## BUILD SPEC (clone + parameterize + table) — the body IS tractable

Full mapping (2026-05-28) shows the per-track unit is **nested insertion of
fixed-size, clonable subtrees** — NOT cross-track rewriting. Every per-track
block is *new* per track (count +1); the "grow" blocks are only their container
parents, whose size increases because they gain children (handled automatically
by `body_synth.apply_insertions`, which bumps every enclosing `block_size`).

**Per-track subtrees are fixed-size templates** (verified: consecutive tracks'
`0x261c`=1865B, `0x251a`=86B, `0x1014`=65B, `0x2589`=266B are equal-length). The
differing bytes are all derivable or copyable:

| Subtree (root) | template | per-track fields to set |
|---|---|---|
| `0x1014` (in `0x1015`) | 65B | name `Audio k`; channel u16 pair + u32 pair = `[2j, 2j+1]`; FILETIME |
| `0x1052` ×2 (in `0x1054`) | 26B | name `Audio k` (track-named) |
| `0x251a` ×2 (in `0x2519`) | 86B | name; ordinal (+40); 2× FILETIME |
| `0x210b` (in `0x2107`) | 56B | name; FILETIME |
| `0x2589` (in `0x258a`) | 266B | **1 byte** ordinal (+9) |
| `0x261c` subtree (in `0x2624`) | 1865B | name (+46); 3× FILETIME; 10 linear counters (+1065..+1101 stride 4, +10/track); 4 ordinals (+1337,+1343,+1521,+1846); **158B high-entropy region** (3 runs +1150,+1191,+1253) |

**The 158-byte high-entropy region (`0x261c`) is the only non-obvious field, and
it is DETERMINISTIC PER TRACK INDEX** — track-k's region is byte-identical across
every control (n2/n3/n4/n8 all match), and each track differs. So it is *not*
session-random: **bake a per-track table** (extract track 1..16's region from the
controls). Algorithm unknown (looks like a hash/GUID, 158B), but the table covers
the practical range. (Open Q for custom names/reorder: is it keyed by track
position or by name? In controls those coincide.)

Plus container/own tweaks: `0x1015` track-count u32 (+2) := N; `0x2519` has ~+12
own-byte growth/track (TBD field); two `0x202a` lists bump count + append ordinal
u16 (+2 each); first-top-level block type := `2530*N + 1886` (counter).

**Build algorithm:** for each new track k — clone each subtree template, set its
derivable fields + a valid FILETIME + the table region; insert at lane-major
position (`0x251a` lane-1 mid-run, else append within parent run); then patch the
container own-bytes/counts; finally `final_index.compose_index`. `apply_insertions`
recomputes all sizes. FILETIMEs are non-deterministic → not byte-exact vs a
control, but Pro Tools only needs valid ones (validate by load test).

The detailed topology tree and per-field offsets follow below.

## Why the body is tractable

The body has **no structured absolute-pointer network**. Verified two ways:
(1) of ~31–63 four-byte windows that equal a block ZMARK, only ~1 recurs at a
stable (container, offset) slot across N — the rest are coincidental scalars;
(2) the legacy donor-writer changes block sizes yet only patches the final index,
and still produces working files. So growing the body is **structural**: insert
per-track child blocks, update a few per-N fields, recompute sizes. Cross-block
pointers are the index's job (Pass 2).

## File / block layout

- File = 20-byte header `data[:20]`, then top-level blocks back-to-back to EOF
  (no gaps, no trailing). The last top-level block is the `0x0002` index.
- Block = `5A | u16 block_type | u32 block_size | content`. `content` begins with
  the `u16 content_type` and includes nested child blocks inline, with
  "own-byte" gaps between them.
- `ptxformatwriter/body_synth.py::render_block` rebuilds a block from the parse tree and
  recomputes `block_size` automatically (size = length of rebuilt content). With
  no edits it round-trips all 16 controls byte-exact. **This emitter is done.**

## The per-track body unit (empty stereo) — CORRECTED, much larger than first thought

**WARNING:** an early version of this doc listed a 7-block per-track unit. That
was wrong — it only captured the top-level structural *inserts* and missed the
extensive nested growth. The real per-track delta is **~2700 bytes** (not perfectly
constant: 2615–2721, so per-track content *varies*) across **31 new block types
plus 13 growing blocks**. A test file built from the 7-block model was rejected by
Pro Tools ("magic ID does not match"). Full breakdown (measured n2→n3):

**New blocks per track (count increases), 31 types:**
`0x2037`×12, `0x2625`×11, `0x2626`×11, `0x2038`×8, `0x260a`×5, `0x203b`×4,
`0x1052`×2, `0x251a`×2, `0x2580`×2, `0x4420`×2, and ×1 each: `0x1014`, `0x1029`,
`0x102d`, `0x200a`, `0x200b`, `0x2015`, `0x2103`, `0x2104`, `0x210b`, `0x2434`,
`0x2504` (the counter block), `0x2589`, `0x260c`(×2), `0x260d`, `0x260e`,
`0x2619`, `0x261b`, `0x261c`, `0x2627`, `0x4301`, plus a `0x0000` artifact.

**Grow-only blocks per track (count stable, bytes increase), 13 types:**
`0x2624` playlist parent **+1881**, overview chain `0x2551`/`0x2587`/`0x258a`/
`0x258b` **+266** each, `0x2519` **+206**, `0x1015` +65, `0x1054` +52, `0x2107`
+56, `0x202a`/`0x202b` +4, and the `0x0002` index +191.

Most of the bulk (and most of the 31 new types) lives **inside the `0x2624`
playlist subtree** (`0x261b` +1328/track, `0x261c` +1874, the `0x2625`/`0x2626`/
`0x2627`/`0x260a`–`0x260e`/`0x2619` families) — these hold **cross-track data**,
which is why so many *existing* blocks also grow. The overview chain
(`0x2587`→`0x2551`→`0x258b`→`0x258a`→`0x2589`/`0x258e`) is similarly cross-track.

Also: set the first top-level block's type field = **`2530*N + 1886`** (per-N
counter: n1=`0x1140`, n2=`0x1b22`, n3=`0x2504`, n4=`0x2ee6`); grow each of two
`0x202a` lists (bump count, append ordinal); bump `0x1015`'s track-count u32.

The header byte at 0x13 (`xor_value`) is a per-file random XOR seed and differs
between controls; it is *not* part of the per-track unit.

### Per-track topology tree (representative nesting; each type shown once)

```
0x2504  (counter block, first top-level; type = 2530N+1886)
0x2519  (grow +206)            global name sidecar (MIDI track full list)
  0x251a  (+2)                   name sidecar entry
    0x4420  (+2)
0x1015  (grow +65)             AUDIO tracks; track-count u32 := N
  0x1014  (+1)                   audio track metadata (name, channels)
0x1054  (grow +52)             region->track map
  0x1052  (+2)                   active lane (name)
0x2107  (grow +56)             global track name/order
  0x210b  (+1)                   global name entry
0x202b -> 0x202a  (grow +4)    [0..n-1] ordinal lists
0x2624  (grow +1881)           PLAYLIST SIDECAR (the bulk)
  0x261c  (+1)                   per-track playlist (1865B template)
    0x261b  (+1, 1328B)
      0x102d  (+1)
      0x2627  (+1) -> 0x2625 (+11) -> 0x2626 (+11)
      0x260d  (+1, 653B) -> 0x1029(+1), 0x260e(+1)->0x0000, 0x260a(+5), 0x260c(+2)->0x260a
    0x200b  (+1) -> 0x200a (+1) -> 0x2015 (+1) ->
      0x2038 (+8) -> 0x2037 (+12)
      0x2104 (+1) -> 0x2103 (+1)
      0x2580 (+2) -> 0x203b (+4) -> 0x2037
0x2587 -> 0x2551 -> 0x258b -> 0x258a  (grow +266 chain)   OVERVIEW
  0x2589  (+1, 266B)             per-track overview (1-byte ordinal at +9)
0x2619 (+1) -> 0x4301 (+1)     (0x2619 also shared under tempo/meter lanes)
0x2434 (+1)
0x0002  (grow +191)            MASTER INDEX -> regenerated by compose_index
```

All the `+count` blocks are obtained for free by cloning their subtree root's
bytes (children are inline). So the body builder inserts ~6 subtree roots per
track (`0x1014`, `0x1052`×2, `0x251a`×2, `0x210b`, `0x261c`, `0x2589`, plus the
small `0x2619`/`0x4301`/`0x2434`) — each a fixed-size template.

**Implication (revised):** body synthesis is tractable — clone fixed-size
subtree templates, parameterize the derivable fields, copy the per-track
high-entropy table entry, insert with size-recompute, then compose the index.

## Per-track fields (what differs between instances)

Derivable: name `"Audio k"` (lanes `0x1052` use lane names `"Audio 1".."Audio 2N"`),
stereo channel map `[2j, 2j+1]` (0-based track j), ordinals = track index.

Non-deterministic: **Windows FILETIME timestamps** (in `0x1014`, `0x210b`,
`0x251a`, `0x261c`). Pro Tools needs valid ones, not specific ones — copy from a
sibling track. (This is why the body is not byte-exact reproducible vs a control;
validate by re-parse + Pro Tools load.)

## The two open problems

### 1. The `0x261c` playlist (1874 B, ~179 bytes/track differ)

Decoded the per-track diff (track k vs k+1) into 21 runs:
- name (1 byte), 3 timestamp fragments;
- 10 single bytes at stride 4 (low bytes of u32s) that **increment by 10 per
  track** — a linear counter array;
- a few ordinals (+1/track);
- **three runs totalling ~158 bytes of high-entropy data** (offsets +1150, +1191,
  +1253) that change completely between tracks — almost certainly per-track
  GUIDs / unique IDs.

Open question: do these just need to be **unique** (generate random) or are they
validated (hash/derived)? **Fastest resolution: a Pro Tools load test.** Also
note: adding a track modifies the *existing* `0x261c` playlists too (they were
0/2 byte-stable across n2/n3) — an internal per-N field updates, so synthesis
must touch existing playlists, not only append a new one. `0x251a` similarly has
a per-N field in existing instances.

### 2. Index offset composition — SOLVED (`final_index.compose_index`, tested)

`compose_index(donor_data, target_body_data, base_n, target_n)` builds the exact
0x0002 index for a target body from a smaller donor — **byte-exact for every
single-step (N-1→N) and multi-step (e.g. 1→16 in one shot) pair** across the
control set (`tests/test_final_index.py::test_compose_index_byte_exact`). This
completes the entire index side: given any N-track body, the correct index is
generated deterministically. How it works (the key ideas that cracked it):

- **Match holes by track name, not occurrence.** Each hole's target block is
  resolved by `(content_type, "Audio N" name, occurrence-within-name)`. The name
  is embedded in per-track blocks, so it survives the body's per-track reordering
  (lane-major blocks) that defeated SequenceMatcher.
- **Lane-major 0x251a:** blocks are ordered lane-1-of-all-tracks then
  lane-2-of-all-tracks. The 0x2519 *parent* record references each track's lane-2
  block; the *table* record references lane-1. New ones are assigned by
  `(Audio k, lane)` positionally.
- **Per-N counter block** (first top-level, type `2530*N+1886`) gets a unique
  label so its type collision with a real content_type doesn't shift occurrences.
- New per-track instances are assigned tracks in document order (k-th instance of
  its type → track k), so one fill handles any number of added tracks.

The historical exploration (now obsolete) ruled out the easy approaches
(donor=real N-1, target=real N):

- **Greedy "next block of its type"**: ~70% only — holes legitimately target the
  same block repeatedly (first failure: a `0x1015` child-ref pointing back to
  `0x1015` occ 0 when greedy expects occ 1).
- **`_build_offset_maps` (SequenceMatcher) for existing holes**: ~223/239 right,
  but it is **off by exactly one block** on the container-marker holes — e.g.
  `0x1015` markers map to a `0x1014` 65 bytes too late, `0x2519` markers to a
  `0x251a` 86 bytes too late. Multiple per-track insertions per step make the
  aligner mis-group adjacent equal-runs. (This off-by-one is exactly what forced
  the legacy `_update_final_index` to carry so many special cases.)

Ground truth for one step (n=2→3), the 12 new holes and their targets:
`0x1015`→`(0x1014, last)`; `0x1054`→`(0x1052, last-2)`×2; the new `0x2519`
instance childref→`(0x2519, 0)` (shared parent) and marker→`(0x251a, last)`; the
new `0x2624` instance childrefs→`(0x2624,0)`,`(0x261c,last)`,`(0x261b,last)` and
marker→`(0x2627,last)`. Plus 5 *existing* `0x2519`/`0x2504` holes whose target
occurrence shifts +1.

**Correct approach (next to build):** assign each hole a logical `(type, occ)`
where `occ` comes from the marker's *position within its record* (the k-th
`0x1014`-marker in the `0x1015` record → `0x1014` occ k, etc.), not from the
donor offset value or a global SequenceMatcher. Resolve `(type, occ)` against the
target block layout. Validate byte-exact: synthesized index must equal the real
N index. Exploration scripts that produced the ground truth above:
`/tmp/ptx_compose_explore.py`, `/tmp/ptx_compose_measure.py`.

## Recommended build order

1. **Index offset composition** (problem 2) — finishes the index end-to-end,
   no Pro Tools needed. Validate byte-exact across control pairs.
2. **Body growth** — insert the per-track unit (clone + parameterize derivable
   fields, copy timestamps), update the per-N fields in existing `0x251a`/`0x261c`,
   grow `0x202a`, set the first-block counter, recompute sizes (emitter handles it).
3. **Compose** body + index → first synthesized `.ptx`; **load in Pro Tools** to
   resolve the `0x261c` high-entropy field (problem 1).
4. Extend per-track unit to mono / MIDI / click track, then naming, ordering,
   clips, tempo/meter (much of which the existing donor-based writer already does).

## 2026-05-29 — VALIDATED across 1–16 in Pro Tools + arbitrary naming (the load-failure fixes)

Empty-stereo synthesis now opens in Pro Tools for N=1..16, and `rename_track`
works for any name. Getting there required fixing a stack of N-dependent /
shift-sensitive fields the early structural model missed — each found by Pro
Tools rejecting a file, then diffing. All are implemented in `ptxformatwriter/body_synth.py`
and locked by `tests/test_body_synth.py`. **`#1 cause` is the first-block
index-offset pointer documented at the top of this file — read that first.**

- **Path "chimera" (8/16 "magic ID").** Every `.ptx` embeds its session FOLDER
  PATH (`<u32 len><ascii>` components) in each track's `0x261c` and in
  `0x2067`/`0x200b`. The controls were saved in two folders (n1-4 vs n5-16), so
  growing an n2 donor with an n8/16 library produced TWO paths in one file →
  reject. Fix: grow within one folder-era (donor n6 → library n8/16). Shows up as
  `0x261c`(1874 vs 1858)/`0x200b`/`0x2067`/`0x2624` block-SIZE mismatches.

- **Overview display-order permutation.** The 2nd `0x258a` block holds N records
  `89 25 <order:u16> 01 00 61 00 5a 01 …`; the N order values are a permutation of
  0..N-1 (hash-table iteration order, no closed form). Growing per-track yields
  duplicates → invalid. Fix: `synthesize` rewrites the whole sequence from the
  target control via `overview_order` / `_set_overview_order`.

- **Per-track "selected" flag.** Two 1-byte booleans in each `0x261c` at
  `run-40` and `run+186`, where `run` = the 9-byte marker `ef ff df bf ef ff df bf
  02`. Anchoring on the run is robust to name-length AND path-era (absolute
  offsets are not). `_set_track_selection(body, None)` deselects all (neutral
  default). MUST run before window-marker clearing (both touch the same run).

- **Multi-digit track names ("Audio 10"+).** The `0x2519` name-table entries are
  VARIABLE length (`<name_len:u32> name <23-byte suffix>`), so ≥10 tracks shift
  the table. Fix: `_set_name_table` transplants the target control's complete
  table (when `library_total==target_n`).

- **Overview scroll-extent.** A u32 that is 0 for N≤9 but non-zero once tracks
  overflow the edit window (n10-12=311, n13-16=602; window-scroll, no closed form
  beyond the controls). Replicated in fixed-size blocks `0x2016` (rel 34/924/1217)
  and `0x2581` (rel 6/299). Fix: `overview_extent` / `_set_overview_extent` copy
  it from the target control. (`>16` needs the next threshold beyond 602.)

- **Window "visible-track" markers.** The same `ef ff df bf …` run in
  `0x261c/0x200a/0x200b/0x2015/0x2104` marks the scrolled visible track range
  (real n10 = tracks 8-10, n16 = 8-16). A small donor's active track carries a
  STRAY marker outside that range. Fix: `_clear_donor_window_markers(body, base_n)`
  zeros it on donor tracks 1..base_n (no-op for N≤4 donors; library tracks keep
  theirs).

- **Session-info (`0x2067`) size match.** The embedded session name's length sets
  the `0x2067` block size; a donor whose name length differs from the target
  shifts the body. `_match_session_info_size` transplants the target's `0x2067` on
  a size mismatch. NOTE: the REAL cause of that shift failure was the index-offset
  pointer (above) — once `_set_index_offset` is used everywhere this hack can
  likely be retired.

### Arbitrary track naming — `rename_track(session, old_name, new_name)`

A track name lives in **8 length-prefixed slots**: `0x1014`, `0x1052`×2 (lanes use
the TRACK name, not "Audio 2N"), `0x251a`×2, `0x210b`, the `0x2519` name-table
entry, and `0x2619`. Each occurrence is preceded by a `u32 = name length` — the
robust discriminator (distinguishes "Audio 1" from "Audio 10" and real slots from
coincidental substrings; very short names <=4 chars can have harmless coincidental
matches in unrelated blocks). `rename_track`: (1) `track_name_occurrences` finds
the length-prefixed slots; (2) bump each containing block's `block_size` by
`(occurrences-within × delta)` via containment (this also fixes the internal
container length fields, which are nested block-sizes); (3) splice name+prefix
high→low; (4) capture the index holes from the ORIGINAL layout BEFORE the shift
(the index can't be re-parsed once the body moves), re-fill them at their stable
index-relative positions with the new block offsets; (5) `_set_index_offset` to
rewrite the first-block index pointer. Validated in Pro Tools (shorter/longer/
spaced/punctuated names, single- and multi-track). A SAME-LENGTH (delta-0) rename
is a pure in-place swap (no shift, index unchanged) and was the test that isolated
the index-offset pointer as the sole remaining cause.

**Session name** — `set_session_name(data, new)` / `session_name(data)`: the
embedded session name is the length-prefixed `….ptx` string in `0x2067`, so it's
just `rename_track` on that string (handles the shift via the index-offset
pointer). GOTCHA: `0x2067` holds MULTIPLE `.ptx` references (save-as remnants;
e.g. the "3 stereo tracks" control also embeds a stale "4 stereo tracks.ptx") —
the primary name is the FIRST length-prefixed one. COSMETIC: Pro Tools titles a
session by its filesystem filename and tolerates a mismatched embedded name.

### Pro Tools load-error reference (for diagnosing load failures)

Pro Tools' load errors are human-decodable and fall into two classes that point at
different bug shapes:

| PT message | class | meaning for us |
|---|---|---|
| "magic ID does not match" | POSITIONAL | PT computed an offset and the byte there isn't a valid block magic (`0x5A`) ⇒ the index-offset pointer, a bad index hole, or a wrong block size sent it to the wrong place. |
| "an unexpected stream type was encountered" | POSITIONAL | a serialization type-tag was read at a wrong position ⇒ a length/offset field led the reader astray. |
| "unexpected end of stream encountered" | LENGTH | a count/length field is too big (reads past EOF). |
| "end of stream encountered" | LENGTH | a length field overruns its object. |

Workflow when PT rejects a synth: read the error → classify positional (magic/stream)
vs length (end-of-stream) → diff the synth vs a real control (block-aligned by
`(type, occ)`; the working `synth_8/10/16` and `names/` controls are good
references) → check the index holes (`offset_holes`, all must land on `0x5A` of
the right type), the first-block index pointer (`_set_index_offset`), block sizes
(strict top-level + recursive tiling), and N-dependent counts. The
`eos_validator` / `validate()` pre-write gate reproduces the length-class errors
before you write.
