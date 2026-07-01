# ptxformatwriter — TODO

Open research / feature items. Each entry should say what we want, what's unknown,
and why it matters. (Resolved items kept briefly for provenance.)

## Session bar-number offset — SESSION START BAR SOLVED (2026-06-30)

Pro Tools lets a session's bar numbering start at an arbitrary bar (`-22`, `-1`, `0`, `2`).
**Found + corpus-confirmed:** the session start bar is a signed `i32` at the first meter
event's +8 (block payload +23) of the `0x2029` "Meter" block; default sessions read `1`,
the renumbered `THE WIND` corpus session reads `-2`. The bar-1 tick origin `0xE8D4A51000`
is unchanged by renumbering (it's a relabel, not a tick shift). Shipped:
`body_synth.session_start_bar()` (read). See spec §5c.

**Reading the FULL maps is shipped:** `body_synth.tempo_map()` → `[(bpm, tick), …]` and
`meter_map()` → `[(num, den, tick), …]` (plus `base_tempo()`/`base_meter()` for event 0, and
`n_tempo_events`/`n_meter_events` in `session_info()`). Records are FIXED stride — tempo 61 B
at `zmark+28` (tick 5-byte @+30, BPM f64 @+40, ppq @+48), meter 36 B at `zmark+24` (tick
5-byte @+0, start-bar/ordinal @+8, num @+12, den @+16). The `0x2029` `payload_len == 12 +
count*52` counts the 36-byte record + its 16-byte `0x2719` lane entry — that 52 was NOT a
record stride (the earlier "variable-length" read). Both recover PT-authored controls exactly
(120→140, 4/4→3/4 @bar2) and are sane/monotonic across the corpus (tempo to 111, meter to 18).
See spec §10. NOTE (separate latent bug): `set_tempo_map` can leave a resized `0x2028`'s
declared block size (`zmark+3`) lagging its record count — scan-parser-tolerated and
PT-valid, but the readers bound by `count` not the declared end to compensate; worth fixing
the size field for cleanliness.

**Replacing a map on an ARBITRARY session is shipped:** `body_synth.replace_tempo_map(data,
[(bpm,tick),…])` / `replace_meter_map(data, [(num,den,tick),…])` resize the session's own
top-level `0x2028`/`0x2029` (+ the `0x2718`/`0x2719` lane when present) in place and offset-
shift the master index (block count unchanged). Corpus-validated 26/26 (identity byte-exact,
modify readback exact, holes resolve, others byte-intact, rc=0; PT display pending). Findings
folded into spec §10: the tempo lane wraps a nested `0x2028` (its own size needs bumping),
meter `+8` = absolute bar via CEIL(tick_span/ticks_per_bar) with the lane entry start-relative,
an opaque per-record byte @+22, and empty-block template seeding. The replace preserves the
displayed start bar, so a renumbered session round-trips.

Still open (need Pro Tools ground truth):
- **Writing a NEW renumbered start bar** (changing the displayed first bar, not just preserving
  it): the read + preserve sides are solved (event-0 `i32`), but authoring a *different* start
  needs PT to confirm what else moves (the empty-meter case, and whether anything outside the
  meter block references it) — see §5c.
- **Cleanliness:** fix `set_tempo_map`'s stale `0x2028` size field (the size-driven readers work
  around it, but real sessions keep it consistent).
- **Empty meter map** (`count==0`, e.g. COGNAC/MANOLITO): the i32 field only exists when
  there's ≥1 meter event. Renumber such a session in PT and find where the value lands
  (a forced `count=1` event, or a session-setup block like `0x2305`/`0x230A`?).
- The exact tick↔bar conversion once a session is renumbered (does anything else move?).

Why it matters: lets us place a musical downbeat on **Bar 1** with pickups in bars
`0 / -1 / …` natively (the proper fix for the beatmap-importer count-in approximation),
and round-trips renumbered sessions losslessly.

## Placement → region link — SOLVED (2026-06-30)

The `0x104F` placement's `u32` at **+11** is a **0-based index into the `0x2629`
region-instance list** (file order); the clip's source name is the `0x2628` name nested in
that `0x2629`. Corpus-confirmed: the bak.076 added clips resolve to the added regions, and a
`Wolf Wet` track's clips resolve to `Wolf Wet.L/.R`. Refs are **positional** (an insert
before index N renumbers refs ≥ N — load-bearing for add-clip). Shipped:
`body_synth.clip_names(data, lane_zmark)`. See spec §5c.

## 0x2628 region geometry — DESCRIPTOR MODEL FOUND, slot-meaning needs PT

Round-2: after the name (`noff = z+13+namelen`) a 5-byte descriptor `[b0..b4]` precedes
variable-width LE **sample** fields whose byte-widths are the nibbles of `b1,b2,b3` (slot
order `[b1.hi,b1.lo,b2.hi,b2.lo,b3.hi,b3.lo]`, 0 = absent); `b4` = form marker (`0x08`
trimmed / `0x00` whole-file). The model reproduces exactly on clean families (final field =
clip length in samples) — but the slot→meaning map is NOT stable across subtypes (grouped
`.grp` regions + some `0x1004` record types break it), and "final field == source length"
was refuted as universal. Positions are raw samples, NOT origin-relative ticks. To finish:
author PT sessions with known clip start/length (whole-file vs trimmed sub-region, and a
grouped region) and pin each slot. Gate for add/replace-clip. (See spec §5d.)

## 0x104F placement position — SOLVED; second variant tail open

Round-2 confirmed: position is the `u64` at `+16` (already shipped as `clip_positions` /
`set_clip_position`); sample-track = plain samples, tick-track =
`0x4000000000000000 + 0xE8D4A51000 + tickoff` with the timebase in the top byte at `+23`
(`0x00`=sample, `0x40`=tick, other nibbles opaque). `+11` = the `0x2629` region index.
Remaining (needs PT): the size-37 / subtype-`0x000A` variant's 3 extra tail bytes, and the
per-clip enum at `+24` (1/2/3) — author clips with/without a fade or sync point and diff.

## Automation `0x260A` — re-derive (variable breakpoint array)

Round-2 refuted the "fixed 39-byte single breakpoint" model: `0x260A` is VARIABLE-length and
holds an inner breakpoint **array** (count near payload+10; ~6 B/breakpoint + 6-byte
terminator). `flag@payload+8` ⇄ tick-present is exact; `tick@payload+18` = 5-byte absolute
conductor tick. Re-derive the per-breakpoint (tick, value) record and the lane→parameter
(volume/pan/mute/plugin) linkage. Lower priority — automation is in the "leave intact" set
for clip/track edits, but needed for an automation editor.

## Track GUID / 0x260E routing — needs PT

`0x261B → 0x102D` carries an 8-byte track GUID (after a `2A 00 00 00` tag) and cross-track
links are GUID-keyed; `0x260E` destination names are length-prefixed latin1 but not at
block end, and some slots (record-id `0xFFFF`) are unassigned. Confirm in PT which track-list
block holds each track type's GUID (a GUID showed up in a `0x251A`-family block for an audio
track) and which `0x260D` routing slots mean "unassigned". Groundwork for add/remove-track.

## add / remove-track — scoped (the next big build)

Ground truth exists in the chains: Rewire bak.078→079 ADDS a track (Δ0x261B +1), Hipsters
bak.044→045 REMOVES one (Δ0x261B −1, Δ0x1014 −1). Footprint per spec §5e. The crux vs.
clip edits: a track's blocks are **indexed** (0x1014/0x251A track-list entry, 0x1052 lanes,
0x261C playlists, the 0x261B subtree), so the reindex is a **rank-rebuild** of the master
index (remove/renumber the records for the removed track's blocks) — NOT the pure
offset-shift that clip edits use. Also the lanes of ALL audio tracks share ONE 0x1054
container (Stanaj: one 6.7KB 0x1054 holds all 16 lanes), so remove-track splices lanes out
of the shared container rather than dropping a per-track container. And the 0x251A track
list appears DUPLICATED (two copies in Stanaj).

PRECISE FOOTPRINT (ground truth: Hipsters bak.044→045 removes the EMPTY track 'GTR 13.dup2';
the simultaneous +7-clip add is separable via positive deltas). An empty-track removal
deletes **28 content-types / ~70 blocks**: the 0x261B subtree (0x261b, 0x102d, 0x2619,
0x2627, 0x260d, 0x260a×3, 0x260c×2, 0x260e×3, 0x1029, 0x2434, the view chain 0x2015/0x200a/
0x200b/0x2037×3/0x2038×3, the plugin cluster 0x1038×2/0x2613×2/0x2614×2/0x2615×2/0x2616×2),
the scaffolding (0x2506×13, 0x2625×9, 0x2626×11), the track-list entries (0x1014, 0x251a×2),
the playlist 0x261c, and the empty lane 0x1052×1. Of these, only **6 are INDEXED** (referenced
by master-index child_refs OR element offsets): **0x1014, 0x251a, 0x1052, 0x261b, 0x261c,
0x2627**. So the reindex = offset-shift for the 22 non-indexed types + a bounded RANK-REBUILD
(remove the index refs + renumber) for those 6. Plus editing the shared name table 0x2519
(the name is inside it, not a separate block) and the display-order 0x2624.

Sub-problems: (a) IDENTIFY one track's complete block-set — name-bearing blocks by name, the
0x261B subtree by containment, but the scaffolding (0x2506 per marker×track, 0x2625/0x2626
under the track's 0x2627) needs per-track attribution; (b) build the index rank-rebuild for
the 6 indexed types (remove a child_ref = splice 11 B + decrement the record count; remove an
element offset = splice 4 B + decrement k; then offset-shift). Validate: reproduce bak.045's
structure (track gone, other tracks byte-intact) + holes resolve + rc=0. Start with the
empty-track case (no clips/lanes-with-clips), then generalize to tracks with clips.

## Track edits — EOS bug FIXED, PT re-confirmation pending (2026-07-01)

PT verification (empty-track sessions): **verify_1 (replace_tempo_map/replace_meter_map) = PT
VALID** ✓. **remove_track + duplicate_track = "end of stream encountered"** (PT-rejected
despite rc=0). ROOT CAUSE: a *count-prefixed container* stores a `u32` child-count at
payload+0 and PT reads exactly that many children; the track edits changed the containers'
children but not the count → PT reads past the container → EOS. Our size-driven reader
tolerates the stale count (rc=0 ≠ PT-valid). FIXED: `_fix_container_counts` (called by both
track edits) rewrites the counts of `0x1015`(→`0x1014`), `0x1054`(→`0x1052`), `0x2624`(→total
per-track subtrees); idempotent on all 26 corpus + round-trip preserved. NEW TOOL:
`body_synth.validate(data)` = pre-write EOS gate replicating PT's container read (spec §14.6).
NEXT: user to re-test the regenerated verify_2/verify_3 in PT; if still failing, hunt the next
count-container / structural invariant (validate() is extensible — add the container type +
its tallied child type). Broaden validate() beyond the 3 track containers as more are confirmed.

## Track-edit EOS — the `0x2519` name-table fault FOUND + FIXED via a stream-walk validator (2026-07-01)

The count fix (above) was necessary but NOT sufficient — PT still EOS'd. Built a count-driven
stream-walk validator matching PT's read semantics in `ptxformatwriter/eos_validator.py` (clean, format-terms
only). It reproduced the real fault: the `0x2519` name table's INLINE name-entry list (each entry
`u32 len | name | 23-byte trailer` with a `0x2A` marker at trailer+6) was left MISFRAMED —
`remove_track` deleted only `len|name` (orphaned the trailer), `duplicate_track` inserted a
trailer-less copy. The block size + framed `0x251A` children were adjusted, so the size-driven
reader passed (rc=0) but PT read past the object. FIXED: both edits now splice/insert the WHOLE
entry (`+_NAME_ENTRY_SUFFIX`). `body_synth.validate()` now runs the ported `eos_validator.simulate`
(name table + `0x2624` container) + the count check; sound 0/26 corpus, reproduces the pre-fix EOS,
and both edits + round-trip are clean. PENDING: user re-tests the regenerated verify_2/verify_3 in
PT (third time). The validator is a real oracle now; extend `eos_validator` with more object grammars
(index records, per-track objects) as future edits need them.
