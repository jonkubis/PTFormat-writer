# The 0x0002 Master Index — Build-Ready Schema

> # ⚠️ THE INDEX-OFFSET POINTER — READ THIS FIRST ⚠️
>
> ## **THE FIRST TOP-LEVEL BLOCK STORES THE ABSOLUTE BYTE OFFSET OF THIS 0x0002 INDEX.**
>
> The first block in the file (ZMARK at **0x14**, `block_type 0x0001`, `block_size 4`)
> is **NOT a counter**. Its 4-byte payload (at file offset **27**) is the **absolute
> offset where the `0x0002` index begins**. The value long mistaken for a per-N
> "counter content_type" (`0x2504`, `0x102d`, `0x105a`, …) is simply the **low 16
> bits of this offset**.
>
> ### **ANY edit that changes the body size moves the index — you MUST rewrite this pointer, or Pro Tools fails with "magic ID does not match".**
>
> This single field was the root cause of **every** shift-related load failure:
> the multi-digit / session-name "magic ID" saga (sidestepped by matching the
> 0x2067 size — which only worked because it landed the index where the stale
> pointer happened to point) **and** arbitrary track renaming. Empty-stereo
> synthesis survived N≤16 only by luck: the index stays under `0x20000`, so the
> unset high 16 bits were coincidentally `0x0001`.
>
> **Fix:** `body_synth._set_index_offset(data)` rewrites the first block's 4-byte
> payload to `final_index_ref(data).start`. Call it after ANY body-size change
> (rename, custom session name, >16 tracks, clips). Discovered 2026-05-29 by
> finding a `u32 == index_start` at body offset 27 in every control (Pro
> Tools-confirmed: it fixed arbitrary track renaming).

Status: decoded 2026-05-28 against `control_files/lots of stereo tracks/` (empty
stereo sessions, N = 0..16). Every claim here is measured, not inferred. This is
the spec needed to *regenerate* the final index from a known block layout
("Pass 2"), instead of copy-a-donor-and-patch-guessed-offsets.

## Why this document exists

The final block (content_type `0x0002`) is the session's master pointer table:
it cross-references every block in the file by **absolute byte offset**. Any
structural edit (adding a track) reshuffles those offsets. The current writer
(`_update_final_index`) tries to *guess* which 4-byte values are offsets and
rewrite them, which is undecidable in general (a count/size field can
coincidentally equal a block offset) and is the root cause of the
"EOF / magic ID does not match" corruption. The fix is to rebuild `0x0002`
deterministically from the schema below.

## Block-level framing

The `0x0002` block is the last top-level block. Offsets relative to its ZMARK:

| Offset | Size | Field | Notes |
|---|---|---|---|
| +0 | 1 | `0x5A` ZMARK | block start |
| +1 | 2 | block_type = `0x0002` | u16 LE |
| +3 | 4 | block_size | u16... u32 LE; **content** byte count (excludes the 7-byte header) |
| +7 | 2 | content_type = `0x0002` | u16 LE; this is `block.offset` in the parser |
| +9 | 4 | record_count | u32 LE |
| +13 | … | record stream | concatenated records, no padding between them |

`block.offset` (in `core.py`) points at +7. So `record_count` is at
`block.offset + 2` and the record stream starts at `block.offset + 6`.

## Record grammar

Records are variable-length and laid end to end. Grammar (offsets relative to
record start):

```
+0   u32   count          # small int; per-type constant — copy from template
+4   u16   content_type    # the block family this record indexes
+6   u32   0xFFFFFFFF      # record sentinel (always ff ff ff ff)
+10  u8    flag            # 0 or 1, per-type constant — copy from template
+11  u32   ordinal         # 0 for singletons; track index for per-track records
+15  ...   child-ref list  # zero or more 10-byte child-refs (see below)
+..  u32   entry_count     # number of markers that follow
+..  N ×   marker entry    # entry_count markers, 15 bytes each
```

The simplest and most common record (130 of 165 at N=3) has **no child-refs**
and **one marker**, giving a fixed 34-byte record:

```
01 00 00 00     count = 1
TT TT           content_type
ff ff ff ff     sentinel
00              flag
00 00 00 00     ordinal = 0
01 00 00 00     entry_count = 1
01 04 00 01 00  marker tag                 (+19)
oo oo oo oo     u32 absolute offset         (+24)  <-- the only offset hole
00 00 00 00 00 00   marker trailer (6 zero bytes)
```

### Marker / offset-table element (4k + 11 bytes)

The "marker" is really a count-prefixed offset table. `k == 1` is the familiar
single marker; `k > 1` packs several offsets under one tag.

```
01 TT 00            tag; TT == 4*k (04 for k=1, 08 for k=2, 0c for k=3)
kk kk               u16 k = number of offsets
(oo oo oo oo) * k   k u32 ZMARK offsets (each an offset hole)
00 00 00 00 00 00   trailer (6 zero bytes)
```

`entry_count` counts elements, not offsets. The offset always points at a block
**ZMARK** (`block.offset - 7`). Across all controls there are 0 unresolved
offsets.

### Child-ref entry (11 bytes)

```
cc cc               u16 child content_type
oo oo oo oo         u32 absolute offset of that child block's ZMARK (offset hole)
ff ff               up to two flag bytes (0x00 or 0x01 each)
00 00 00            three zero bytes
```

Child-refs appear in many record types (not only the obvious containers). The
forward parser distinguishes a child-ref from the trailing `entry_count` by a
strict test: the u16 is a known content_type **and** the following u32 is an
actual block ZMARK **and** the last 3 trailer bytes are zero. `entry_count` never
satisfies it because its trailing bytes form a huge non-offset u32.

> Note on "scalars vs offsets": a child-ref's offset is a *real* offset even when
> it does not move across a given diff (e.g. the `0x2716` child-ref value `450`
> looks like a constant between N=2 and N=3 only because the block it targets sits
> *before* the insertion point). It *is* a block ZMARK and must be refilled from
> the layout. The genuinely coincidental values (a count/size that merely equals
> some block offset at one N — observed for `0x2037` at N=7 and `0x4420` at
> N=4/5) never recur at a stable record position, which is exactly how the
> builder excludes them (see "Finding holes").

## Offset holes — the only fields the builder computes

An **offset hole** is a u32 that stores an absolute block ZMARK offset. There are
two kinds, and both come from **walking the parsed grammar, never from scanning
for values**:

* **marker hole** — each of the `k` u32 offsets in a marker/table element.
* **childref hole** — the u32 in each child-ref entry.

> **Do not detect holes by "value equals some block offset."** That over-detects:
> at N=1 and N=7 the index contains scalars (counts/sizes) that coincidentally
> equal a block offset — e.g. ~141 four-byte windows equal a `0x2037` ZMARK at
> N=7. A value-scanning detector (what the legacy patcher effectively does)
> rewrites those scalars and corrupts the file. Grammar parsing is immune: it
> only ever touches a u32 inside a marker element or a child-ref. Verified —
> hole counts land exactly on the formulas below with **zero** scalar noise at
> every N including N=1 (see `tests/test_final_index.py`).

Counts (measured, all N ≥ 1, grammar-accurate):

- marker-element offsets `= 8N + 164`
- child-ref offsets `= 4N + 51`
- total offset holes `= 12N + 215`

(The often-cited `7N + 162` is the count of literal `01 04 00 01 00` tags — i.e.
`k == 1` elements only. It undercounts by one `k > 1` table offset per track,
which is why the grammar count is `8N + 164`.)

Everything else in the record is copied verbatim from the template.

A reference implementation of detection + lossless rebuild lives in
`ptxformatwriter/final_index.py` (`offset_holes`, `rebuild_index_offsets`,
`block_layout`). Round-trip rebuild reproduces all 16 stereo controls byte-exact.

## Per-track scaling

For a stereo session of N tracks (N ≥ 1), measured exactly:

| Quantity | Formula | Checks |
|---|---|---|
| record_count | `2N + 159` | N2=163, N3=165 (== parsed record count) |
| marker-element offsets | `8N + 164` | N1=172, N2=180, N16=292 |
| child-ref offsets | `4N + 51` | N1=55, N2=59, N16=115 |
| literal `01 04 00 01 00` tags | `7N + 162` | N2=176, N8=218 (k==1 elements only) |
| block_size (content) | `191N + 6048` | N2=6430, N3=6621 (exact) |
| full block (incl. 7-byte header) | `191N + 6055` | matches the older notebook figure |

### What a single stereo track adds

**+2 record instances**, each appended at the end of its own type-run (verified
by index position; all other records keep their relative order and merely shift):

- one `0x2519` instance (the global-name sidecar entry for the track)
- one `0x2624` instance (the playlist sidecar entry for the track)

**+7 markers**, distributed as:

| Where | Target | Per track |
|---|---|---|
| `0x1015` container record (marker list grows in place) | `0x1014` | +1 |
| `0x1054` container record (marker list grows in place) | `0x1052` | +2 |
| `0x2519` parent record (marker list grows in place) | `0x251a` | +1 |
| new per-track `0x2519` instance | `0x251a` | +1 |
| `0x2624` parent record (marker list grows in place) | `0x261c` | +1 |
| new per-track `0x2624` instance | `0x2627` | +1 |

So there are two distinct growth mechanisms:

- **Container records grow in place**: `0x1015` carries N markers to `0x1014`;
  `0x1054` carries 2N markers to `0x1052`; the `0x2519` parent carries N markers
  to `0x251a`; the `0x2624` parent carries N markers to `0x261c`. The record's
  `entry_count` and `block_size` grow; the record count does not.
- **Per-track instance records are appended**: exactly one `0x2519` and one
  `0x2624` instance per track, each with `ordinal` = track index.

## Pass 2 build algorithm

Given the fully laid-out body (all blocks placed, every block's absolute ZMARK
offset known) and a reference index of the same track-type mix:

1. **Seed the record list** from a reference file's parsed records (preserve
   order). For each per-track container/instance, know its growth rule above.
2. **Set counts**: emit container records with `entry_count` = N (or 2N for
   `0x1054`) markers; append N per-track `0x2519` and N per-track `0x2624`
   instances with `ordinal = 0..N-1`.
3. **Fill every offset hole** from the final layout: for each marker/child-ref,
   write the absolute ZMARK offset of the block it targets. Hole positions are
   found by schema (marker tag / child-ref slot), never by value-scanning the
   output. Reuse `ptxformatwriter.final_index.rebuild_index_offsets`.
4. **Recompute header**: `record_count` = number of records; `block_size` =
   total content length. Both must equal the formulas above as a sanity check.
5. **Verify**: re-parse the rebuilt index; assert 0 unresolved offsets and that
   record/offset/size counts match `2N+159 / 8N+164 / 191N+6048`.

The key invariant that makes this deterministic: **hole positions are stable; only
their values (offsets) and the ordinals/counts change with N.** No value-guessing
is required, which is what the heuristic patcher could never get right.

## Proven vs. remaining

Proven (with `ptxformatwriter/final_index.py` + `tests/test_final_index.py`):

- **Grammar parser frames every record exactly.** `parse_records` forward-parses
  the stream and *tiles* the record region with zero leftover, yielding exactly
  `record_count` (== `2N+159`) records on all 16 controls. It replaces the
  pattern-scanning `writer._final_index_records`, which misframed ~31/165 records.
- **Lossless serialize.** `serialize_final_block(parse_records(...))` reproduces
  every control's index byte-for-byte — the dataclasses capture everything.
- Hole detection is exact and scalar-immune (offsets land on `8N+164` markers and
  `4N+51` child-refs at every N, including the noisy N=1/N=7).
- Offsets rebuild losslessly from the layout — all 16 stereo controls round-trip
  byte-exact.
- **Record synthesis.** `synthesize_index_records(base, base_n, target_n)` grows a
  smaller index to N tracks (clone per-track `0x2519`/`0x2624` instances with
  `ordinal=N`; grow `0x1015`→N, `0x1054`→2N, `0x2519` parent and packed-table,
  `0x2624` parent; bump the fixed `0x251b`/`0x251c`/`0x2716` ordinals to N). The
  synthesized structure matches the real control exactly, and is byte-identical
  once layout offsets are placed — verified for 2→3, 3→4, 4→5, 7→8, 2→8, 1→16.

Remaining for a complete Pass 2:

- **Compose synthesis + offset fill against a real body.** Both halves are proven
  in isolation; the bridge is assigning each *newly synthesized* hole its target
  block in the final layout (cloned holes map from the donor via block
  correspondence; new-track holes point at the new track's blocks). This couples
  to Pass 1, which must produce that body.
- **Pass 1 owns block order.** A marker/child-ref target is "the k-th block of
  its type in file order." When a per-track block inserts mid-range, later blocks'
  ranks shift — that is fine as long as Pass 1 lays blocks out in the exact order
  Pro Tools uses and Pass 2 points at the same logical block. The two passes must
  agree on ordering; the index is not independent of body layout.

This synthesis transform is specific to the **empty stereo** track family. Mono
tracks, MIDI tracks, and mixed/reordered layouts will add their own per-track
record shapes — extend `add_stereo_track` (or add siblings) as those are decoded.
