# Click-on-top (track reordering) — findings (2026-05-30)

User wants a Click track at the TOP of a session. This is the first real TRACK-REORDERING
task. Controls created: `lots of stereo tracks/{1,2} stereo plus click on top.ptx`
(= the click-at-bottom twins with the Click dragged to the top, otherwise identical).

## What encodes display order (decoded)
- **Display order = BODY BLOCK ORDER** for audio-like tracks. Proof: dragging the click to
  the top physically moved its `0x261e` playlist block to the FRONT of the body
  (`[Apl,Cpl]`→`[Cpl,Apl]`, `[Apl,Apl,Cpl]`→`[Cpl,Apl,Apl]`). The audio tracks kept their
  relative order.
- The click behaves like an AUDIO track here (moves in the body), NOT like MIDI (which stays
  body-last but can display anywhere via a separate field).
- The `0x2589` overview-order permutation is NOT the display lever — a clean creation-order
  3-stereo has overview `[1,0,2]`, not identity. (Confirms the old "overview is cosmetic /
  hash-iteration order" note.)

## Body: SOLVED (byte-exact)
`derive_click_patch(clean, click_on_top_ctrl)` + `apply_click_patch(clean, patch, clean)`
reproduces the click-on-top BODY **byte-exact** (seed-stripped) for 1- and 2-audio. The
existing recursive structural diff already expresses a FRONT insertion — zero code changes.
So the click-first body is a solved problem when a matching click-on-top control exists.

## Index: the one remaining blocker (precisely isolated)
The final `0x0002` index differs from the click-bottom case in EXACTLY the `0x2624`
playlist-instance records (1-audio: records #135/#136 of 163; everything else identical):

| slot | click BOTTOM childref | click TOP childref |
| --- | --- | --- |
| ordinal 1 | 0x261c (audio) | **0x261e (click)** |
| ordinal 2 | 0x261e (click) | **0x261c (audio)** |

i.e. the click's playlist instance must be at **ordinal 1** (front), audio shifted to 2..N+1.
The lane-instance records (`0x2519` cr[251a]) are structurally identical either way (both
251a); only their OFFSETS differ. So the index transform for click-at-front is:
(1) put the click's 0x2624 playlist instance first (childref 0x261e, ordinal 1) and renumber
the audio instances to 2..N+1; (2) fill every offset against the click-first body.

`compose_index`/`synthesize_index_records`/`add_click_track` only APPEND the new track last,
so feeding a click-first body to the click-last composer yields a malformed index
(`parse_records` fails: "bad element tag … record type 0x2519"). This is the bounded fix.

## Remaining work (two tiers)
- **Tier 1 (small N, byte-exact-validatable NOW):** teach the index composer to place the
  click's per-track instance at its display position (front) instead of last; validate
  byte-exact against the two `… plus click on top.ptx` controls; PT-confirm.
- **Tier 2 (the 12-track ask, generality):** build a click-FIRST BODY with NO matching
  control. The body-replay patch is donor-coordinate (only fits a same-layout clean_ref), so
  a 12-track click-top needs either (a) a 12-track click-on-top control, or (b) cracking the
  general body reorder ("move a track's blocks — incl. the lane-major 0x251a lanes, name
  entry, overview entry — to the front"), which the `{1,2} … on top` controls can validate.

## Index composer prototype (dbg, not in production)
`add_click_front` (insert the click's lane + playlist instance at the FRONT of their runs;
playlist childref 261c→261e; renumber lane+playlist ordinals 1..N+1) produces the CORRECT
index STRUCTURE (same length as the control, parseable, click playlist-instance at ordinal 1).
The target structure (from the control) is exactly:
- lane instances `0x2519 c2 f1 cr[251a]`: ordinals 1..N+1, all 251a (only offsets differ);
- playlist instances `0x2624 c4`: ordinal 1 → 261e (click), 2..N+1 → 261c (audio).

BUT the OFFSET-FILL (`_fill_offsets`) then mis-resolves several holes, because it assumes the
new track is APPENDED LAST and maps by document-order/position: `261c`/`261e` swapped,
`0x251a` lane offsets shifted to the wrong lane, `0x2627` swapped, and `0x2519`/`0x2107`
refs left 0. So generalizing `_fill_offsets` from "append-last" to "arbitrary position" is the
real (non-trivial) work.

## CLEANER STRATEGY (recommended): build-correct-then-permute
Don't rework `_fill_offsets`. Instead:
1. Build the click-LAST session normally (body+index, already byte-exact via `add_click`).
2. Compute the block PERMUTATION that moves the click's per-track blocks to the front of each
   run (track-major runs 261c/261e/210b/1052; the lane-major 0x251a lanes; the 0x2519 name
   entry; the 0x2589 overview entry). Container sizes are unchanged (pure intra-run rearrange).
3. Rebuild the body in the new order, and **remap every index offset by the known old→new
   block-move map** — correct BY CONSTRUCTION, no logical resolution needed.
4. Reorder the index per-track instance RECORDS (261e to ordinal 1) + renumber ordinals.
5. Handle the small overview order-encoding delta (the ~12 B change in 0x2551/2587/258a/258b +
   the 0x2589 entries — the only non-block-move change the body diff showed).
This is the path to GENERAL reordering (any track to any position), validatable byte-exact
against the 1&2-audio click-on-top controls.

## SHORTCUT (reliable, per-layout): clone body + index from a matching control
Since the click-on-top BODY is byte-exact to a matching control, that control's INDEX (its
offsets point into that identical body) is valid verbatim. So body-clone + index-clone = a
byte-exact click-on-top session for THAT exact layout — no index recomposition, no reorder
code. Needs one click-on-top control per target layout (the diff-replay's donor-coordinate
constraint). This is the fast, low-risk way to get click-at-top for a specific session.

## THE REAL COST — TWO unsolved frontiers (discovered 2026-05-30)
Click-at-top-of-12-track requires solving BOTH:
1. **Click at an arbitrary layout.** `add_click` (click_clone diff-replay) is DONOR-COORDINATE:
   replacements are byte offsets in `clean_ref`, valid only when `clean_ref`'s audio layout
   matches the target. The largest click controls are 2-audio (`{1,2} stereo plus click`) +
   2-track mixed (`mono stereo plus click`). NONE match a 12-track layout, so a click can't yet
   be added to a 12-track session even at the BOTTOM. (Generalization = re-key replacements by
   (owner-signature, offset-within-owner) — the standing click TODO.)
2. **General reordering is NOT a pure block move.** Content-matching click-LAST vs click-TOP
   (1-audio) shows ~20 per-track blocks CHANGE content when the track moves — cumulative
   counters (0x261b), lane fields (0x251a), 0x2624/0x261c — and the view-state chain changes
   block TYPE (0x200b→0x200d). So a correct reorder must recompute position-dependent fields,
   not just relocate blocks. The build-then-permute "remap offsets by block moves" idea is
   necessary but NOT sufficient (the block CONTENTS also change).

=> A fully programmatic click-at-top-12-track is a multi-session build across both frontiers.

## Reliable fallback (the "naive method"): CLONE a matching control
Make a click-on-top control of the EXACT target session (open it in PT, add a Click, drag to
top, Save As). Since the body is byte-exact to such a control, body-clone + index-clone
reproduces it byte-exact — sidestepping BOTH frontiers. Caveat: one control per layout, and for
a one-off it's somewhat circular (making the control ≈ making the file); its value is
programmatic regeneration/parameterization (rename tracks, session name, etc. on top of it).

## FRONTIER 1 full characterization (2026-05-30) — generalize click insertion to any N
Derived from comparing the click patch at 1-audio vs 2-audio (`derive_click_patch`). The
click's contribution splits into THREE classes; only the first re-keys trivially:
1. **Stable reps** — same bytes, anchored at the owner's content START or END (rel offset 0
   from one side, stable across N): `0x2067`(whole), `0x2027`, `0x1018`/`0x1017`, `0x201f`,
   `0x2107`(2 reps: start+end), `0x2624`(2 reps: start+end). Re-key = (owner-signature,
   start|end anchor, rel, bytes). Tractable.
2. **Per-audio-track reps** — `0x261b`: ONE per audio track (1-audio→1 rep, 2-audio→2 reps),
   each a 278 B append at that track's block end. The 278 B are NOT identical across tracks —
   they differ only in the cumulative COUNTER values (`seed+10*i`, the LOAD-INNOCENT counters).
   => load-correct can replicate with any valid counters; BYTE-EXACT needs the counter formula
   (which we'd otherwise never need).
3. **N-dependent CONTENT reps** — must be RECOMPUTED, not copied:
   - `0x2519` name table: +~86 B per audio track (holds every "Audio N" entry + "Click 1").
   - `0x202a` ordinal list: +2 B (one u16) per audio track.
   - `0x258a` overview: RESTRUCTURES (1 rep at 1-audio → 2 reps at 2-audio). Hard sub-problem.

Validation ceiling: byte-exact only across 1↔2 (no 3+ click control). The scaling test is
`apply(rekey(derive(1clean,1click)), 2clean) == 2click-control` (and the 2→1 direction). Full
confidence at 12 tracks needs a PT load.

Implementation = re-key(1) + replicate(2) + recompute(3, incl. the hard overview). Note much of
(3) already has machinery in the uniform synth (`_set_name_table`, `_patch_counts`, the 0x202a
ordinal append, `compose_index(click_tracks=)`, `_set_overview_order`) — the generalization may
be: apply the click's STRUCTURAL contribution (1+2) then run the existing N-dependent
finalization (3). That's the most promising architecture for the build.

CONCLUSION: Frontier 1 is multi-session (like Frontier 2). Click-at-top-of-12-track needs both.
The reliable deliverable remains the CLONE (matching click-on-top control → byte-exact).

## BUILD PROGRESS
- **Milestone A — DONE + IN PRODUCTION (2026-05-30).** `click_clone.derive_click_patch_structural`
  + `apply_click_patch_structural` re-key the patch to (owner-signature, offset-within-owner);
  `apply_click_patch` refactored to share `_apply_resolved`. The re-key is LOSSLESS for
  donor==target — reproduces `apply_click_patch` byte-for-byte at 1 & 2 audio. Guarded by
  `tests/test_click.py::test_structural_rekey_lossless`. 71 tests green.
- **Milestone B — per-track replication (NEXT).** Detect the per-audio-track 0x261b reps
  (they appear once per audio track in the donor; from a 2-audio donor that's 2 reps) and
  apply each to ALL of the target's audio tracks' 0x261b blocks. Counters in the 278 B body
  are load-innocent, so a load-correct replicate can copy them; byte-exact needs the seed formula.
- **Milestone C — recompute name-table (0x2519) + ordinals (0x202a)** for the target N (reuse
  `_set_name_table` / the 0x202a ordinal-append / `_patch_counts`).
- **Milestone D — recompute the overview (0x258a) rep** (it restructures with N). The hard one.
- Validation ceiling for cross-N is structural + PT (no 3+ click control); donor==target stays
  byte-exact. Suggested architecture: apply the click's STRUCTURAL contribution (A+B) then run
  the existing N-dependent finalization (C+D via the synth pipeline's helpers).

## Milestone B attempt — the WALL (2026-05-30)
The 3 & 4 stereo+click controls are CLEAN: the apparent 213-byte later-track diffs are just
per-save GUID regeneration (`2a 00 00 00 <8B>`, like the XOR seed/FILETIMEs) — `Audio 1`'s GUID
is stable clean↔click, `Audio 3/4`'s are re-rolled. Neutralizing GUIDs makes them comparable.
(N=3's *degenerate* clean↔click diff — root #34 `0x2504` occ shift collapsing to a 71KB suffix —
only affects DERIVING from the 3-control; we derive from the clean 2-pair, so it's moot.)

Per-track detection works: the only per-track template in the 2-patch is `0x261b` (1043..1321).
But replicating just that to 3-audio yields a STRUCTURALLY BROKEN result — block-count diffs vs
the real 3-control across `0x200a/b/2015` (view chain, 1 vs 3), `0x251a` (lanes, 5 vs 8),
`0x251c` (10 vs 5, over-applied), `0x2589` (overview, 3 vs 4), `0x261b` (3 vs 4). The click's
cross-N contribution is ENTANGLED across ~6 per-track structures, not one template — so a clean
general transplant requires MODELING the full per-track click contribution (the view chain +
lanes + overview entry + name entry + ordinal + counter chain, each placed per audio track and
for the click's own track). That is the substantial remaining work; the diff-from-small-donor
re-key alone does not crack it. (This is why the original click work used the same-layout
diff-replay rather than a hand-enumerated transplant.)

## Honest assessment
- **Milestone A (re-key foundation): DONE, byte-exact, in production, tested (71 green).** Real,
  durable progress; the right primitive.
- **Milestones B–D (cross-N transplant): a deep modeling problem**, bigger than the "tractable
  half" framing. Cracking it = model the click's full per-track entanglement OR re-architect the
  click as a grown "track" (which the original work found fragile). Either is a dedicated effort
  with uncertain payoff.
- **The CLONE remains the reliable path** to an actual click-at-top file for a specific layout.

## BREAKTHROUGH (2026-05-30) — the click footprint is FIXED, not entangled
Per-type count delta (click − clean) for N=1..4 is CONSTANT in N: `0x261e +1, 0x261b +1,
0x200a/b/2015 +1, 0x251a +2, 0x2589 +2, 0x2037 +20, 0x2038 +10, 0x2625 +10, 0x2626 +11,
0x260a +4 …`. The click adds the SAME blocks regardless of track count (the "irregular" ones
— `0x0000`, `0x2504`, `0x324x` — are per-save scratch/undo noise). So cross-N is NOT a
per-track-scaling problem; it's a PLACEMENT problem. Two fixes cracked most of it:
1. **END-ANCHORING.** Reps whose region ends at the owner's content end must resolve against
   the TARGET owner's (grown) end, not offset-from-start. Containers like `0x2624` grow with N,
   so offset-from-start lands mid-container. (2→2 stays byte-exact; donor==target unaffected.)
2. **Replicate per-track templates ONCE per target track** (the `0x261b` in-place rewrite). The
   first attempt double-applied to extra tracks (once per donor rep) → over-apply + mis-frame.
Result: derive-from-clean-2-pair → **apply-to-3 is within 4 bytes of the real 3-control**;
diffs are only `0x0`/`0x324x` (scratch noise) + ONE `0x251a` lane.

## THE LAST STRUCTURAL PIECE (precise)
The 1-lane diff: the `0x2519` track-list rep (which CONTAINS the lane-major `0x251a` run) is
N-dependent — its END-anchored 501-byte replaced tail includes per-track AUDIO lane data, so the
2-audio version clobbers `Audio N`'s lane when applied to a bigger session. FIX = recompute:
insert ONLY the click's additions (its 2 `0x251a` lanes — lane-major: lane-0 after the last
audio lane-0, lane-1 at the end — plus the "Click 1" name entry) into the TARGET's `0x2519`,
instead of wholesale-replacing the tail. (Reuse `body_synth`'s lane-major insertion +
name-table machinery.) The other N-dependent reps (`0x202a` ordinals, `0x258a` overview) showed
NO count diffs at N=3 — verify their content next, but they may already be fine.

Validation plan: once the 0x2519 recompute lands, apply-to-3/4 should match the 3/4-controls
byte-exact modulo the per-save GUIDs (`2a 00 00 00 <8B>`) — neutralize those (like the XOR seed).

## CROSS-N CLICK BUILDS AT GOAL SCALE (2026-05-30) — dev_f1_v3.py
The hybrid apply (diff-replay for most reps + targeted recompute) now builds a structurally
correct, loadable click at N=3, 4, AND 12. Four fixes, in order of discovery:
1. **END-ANCHORING** — reps ending at owner content-end resolve vs the target's grown end.
2. **Replicate per-track templates ONCE per target track** (the `0x261b` rewrite).
3. **SKIP `0x2519`; rebuild via grow** — insert the click's 2 lanes lane-major (lane-0 after the
   last audio lane-0, lane-1 at end) + the "Click 1" name entry, into the TARGET's `0x2519`
   (reusing `body_synth.apply_insertions`). The patch's `0x2519` rep wholesale-replaced a tail
   containing per-track audio lane data → clobbered a lane.
4. **SKIP `0x2067`** — it's the session NAME/path (metadata: `"N stereo tracks"` vs the control's
   `"N stereo plus click"`), NOT a click change (DigiClick reg is in `0x2064`). The patch's
   `0x2067` rep was END-anchored whole-block; at N=12 (different save era → smaller `0x2067`)
   `start_from_end` overshot into the file header and corrupted the XOR-type byte. Keeping the
   target's own `0x2067` fixes it.
RESULT: N=3 (vs control) + N=12 (goal scale, no control) → `audio=N, 1 click playlist,
index resolves, reader rc=0`. Remaining diffs vs controls are COSMETIC: the `0x2067` session
name (acceptable metadata), the counter-block content-type (index-offset low16), `0x0`
scratch/undo, and a 2-byte name-table boundary quirk (the `02 00` before "Click 1").
Wrote `control_files/test_clickbottom_{3,12}stereo.ptx` for PT confirmation.

## EOS ROOT CAUSE + FIX (2026-05-30) — first PT attempt failed "end of stream"
First `test_clickbottom_{3,12}stereo.ptx` EOS'd in PT (even N=3, below scroll → not extent).
Our reader (rc=0) hid it; it's a length-field read-past. Diagnosis: the ONLY structural diff
vs the real control was the body `0x2519` (name table + lane-major `0x251a`). Two sub-bugs:
1. The "Click 1" name entry was missing its `02 00` separator (a single-entry extract drops
   the inter-entry field), making the name table 2 B short → PT reads a wrong entry length →
   EOS. FIX: append the click's exact name-table addition = `click_region[len(clean_region):]`
   (length-based suffix; captures the `02 00` + full entry). The entry-count@16 is bumped to
   N+1 by `_patch_counts`.
2. The click's own bytes (its 2 `0x251a` lanes + the name entry) carry the SOURCE control's
   track count (`src_audio+1`) as a u16 right after each `2a 00 00 00 <8B GUID>`. Wrong for a
   different-N target. FIX: re-stamp that u16 to `target_audio+1` (`_restamp_track_count`).
RESULT: the `0x2519` is now BYTE-EXACT (GUID-neutralized) vs the real 3-control (0 diffs).
Remaining diffs are cosmetic/metadata only: `0x2067` session name, the counter-block
content-type (index-offset low16), `0x0` scratch/undo. Rebuilt `test_clickbottom_{3,12}stereo.ptx`.

## FRONTIER 1 DONE — PT-CONFIRMED + IN PRODUCTION (2026-05-30)
User loaded `test_clickbottom_3stereo.ptx` AND `test_clickbottom_12stereo.ptx` in Pro Tools:
both open ("Both sessions open great"). A click can now be synthesized onto an audio session of
ANY track count, from a small (2-stereo) control pair.

PRODUCTION (folded from the dev prototype):
- `click_clone.py`:
  - `_apply_resolved` split into `_merge_reps` (body merge) + `_finish_click` (counts + index)
    so the cross-N path can insert the 0x2519 rebuild between them. `apply_click_patch` (the
    matching-layout, byte-exact path) is unchanged.
  - `StructuralClickPatch` now carries `sreps` (owner-signature-anchored, kind start/end/suffix)
    + `lane_bytes` + `name_addition` + `src_audio_n`.
  - `derive_click_patch_structural(donor, ctrl)` — end-anchored re-key + extracts the click's
    0x2519 contribution (2 lanes + name-table addition) + source audio count.
  - `apply_click_patch_structural(target, spatch, donor_index_src)` — the cross-N hybrid:
    resolve reps (end-anchored), replicate per-track templates once per target track, SKIP the
    0x2519 (rebuilt via grow: lanes lane-major + name addition + track-count re-stamp) and the
    0x2067 (session name kept), then finish.
  - helpers: `_name_table_region`, `_extract_click_2519`, `_restamp_track_count`,
    `_insert_click_2519`, `_per_track_templates`, `_AUDIO_NAME`.
- `body_synth.add_click_anyN(data, clean_ref, click_ref)` — public entry; click placed LAST,
  data's session name preserved; clean_ref/click_ref = a >=2-audio pair.
- `tests/test_click.py::test_add_click_anyN_cross_layout` — derive from the 2-stereo pair, apply
  to N=3/4, assert the body 0x2519 is byte-exact vs the real N-click control (GUID-neutralized),
  reloads, exactly one click playlist, audio count unchanged. Full suite green.

## FRONTIER 2 — full decode (2026-05-31): move the click (last track) to the TOP
Characterized via `2 stereo plus click.ptx` (bottom) vs `2 stereo plus click on top.ptx` (top):
393 blocks relocate intact (mod GUID); the rest are position-shifted per-track blocks + small
cosmetic changes. The transform = **move the click's per-track blocks to the FRONT of each run**:
- body order `[Apl, Apl, Cpl]` -> `[Cpl, Apl, Apl]` (0x261e playlist to front).
- the per-track blocks (0x261c/0x261e playlists + their routing/view subtrees 0x260d/e/c/a +
  0x200a/b/2015, the 0x261b counters, 0x2627) are NESTED in containers (0x4501/0x1022/0x2031…),
  NOT top-level — so the move is "reorder children within each container", not a flat splice.
- the 0x251a lanes are LANE-MAJOR children of 0x2519: move the click's lane-0 to the front of
  the lane-0 group and its lane-1 to the front of the lane-1 group.
- the "Click 1" name-table entry and the click's 0x2589 overview entry move to the front of
  their own-byte regions.
- INDEX (clean): the 0x2624 playlist-instance records reorder — bottom ordinals
  [1->261c, 2->261c, 3->261e] become top [1->261e, 2->261c, 3->261c] (click to ordinal 1);
  EVERY index offset must be remapped to the relocated blocks.
- COSMETIC riders (load-innocent / tolerated): the cumulative counters (0x261b), the view-state
  chain (0x200a/0x2015, and 0x200b->0x200d block-TYPE change at 1-audio), small overview deltas.

Implementation plan (build-then-reorder): `add_click_anyN` -> click-bottom, then move the last
track's blocks to the front of each nested run (+ lane-major split), remap all index offsets by
the known old->new block moves, reorder the index playlist-instances. Validate vs the
`{1,2} stereo plus click on top.ptx` controls (body byte-exact mod GUID/counter/session-name),
then PT-test at 12. This is a SUBSTANTIAL multi-step build — the deepest piece — because of the
nested per-track runs, lane-major interleave, and full index remap.

## Status
**Frontier 1 (click on any-N session, at the BOTTOM) — DONE, PT-confirmed, in production +
tested.** Frontier 2 (reorder the click to the TOP) — FULLY DECODED (above), implementation is a
focused multi-step build. The CLONE (make a click-on-top control of the target session,
reproduce byte-exact) remains the reliable per-layout shortcut for an immediate click-at-top file. Decoded: display order = body
block order; index click-front structure; the offset-fill generalization needed; the
build-then-permute strategy; the clone shortcut. Body byte-exact (1&2 audio). Broken trial
outputs were NOT kept.
