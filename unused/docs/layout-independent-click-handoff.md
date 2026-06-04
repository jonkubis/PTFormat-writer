# Layout-independent add_click — findings + scope (2026-05-30)

## User request
A test session: **click at the top + 4 mono + 8 stereo** (12 audio tracks). When told
the click needs a matching control, the user chose **make `add_click` layout-independent**
(so a small click control applies to any-size session).

## DELIVERED
- **`control_files/synth_4mono_8stereo.ptx` — BUILT, reloads rc=0** (audio only, no click).
  `synthesize_mixed_session([1,1,1,1,2,2,2,2,2,2,2,2], donor=2 mono tracks,
  mono_lib=(6 mono tracks,6), stereo_lib=(16 stereo tracks,16))`. 12 tracks
  [mono×4, stereo×8], cumulative channels [0],[1],[2],[3],[4,5]…[18,19] (20), overview
  identity [0..11]. NOT yet PT-loaded by user.

## Why layout-independence is REAL RE, not a quick generalization (verified)
`add_click(synth_4mono_8stereo, mono stereo.ptx, mono stereo plus click.ptx)` → `KeyError 453`
(2-track patch offsets don't exist in the 12-track body). So a generalization is needed.
Decomposing `derive_click_patch` replacements by owner-block type across N=1/N=2/mono+stereo
(dbg_clickscale.py + dbg_stamp.py, both removed; recipe below to recreate):

- **9 CONSTANT owners** (the click's OWN contribution; count does NOT depend on audio-track
  count — re-keyable by (owner content_type, occurrence, offset-within-owner) and applied
  to any target): `0x1017`+`0x1018` ("Click II" plugin registry), `0x2027`→`0x2064`
  (session DigiClick reg), `0x2519` ("Click 1" name table, 2 reps), `0x2624` (+`0x261e`
  click playlist 2405 B), `0x2107` (+`0x210b`), `0x202a` (ordinals ×2), `0x2067`
  (session-name len), `0x201f`.
- **2 SCALING owners** (ONE replacement PER EXISTING AUDIO TRACK — these are the blocker):
  - **`0x258a`**: the overview `0x2589` entry per audio track (display/overview).
  - **`0x261b`**: an in-place 278-byte region per audio track; the change is a run of
    **~10 consecutive little-endian u32 counters** at owner-offset ~1056 (within the
    278B region, byte span 6–44+). DECODED (2-stereo click control):
    - track 1: old `0x2b99,0x2b9a,0x2b9b,…` → new `0x3571,0x3572,0x3573,…`
    - track 2: old `0x2ba3,0x2ba4,…`        → new `0x357b,0x357c,…`
    i.e. each audio track has a base counter, consecutive +1 within the track, and the
    click shifts every track's base by a CONSTANT (+0x9D8 = +2520 here: 0x3571-0x2b99).
    **These are the SAME "cumulative per-track counters" already documented** (see the
    MSM mix-and-match notes earlier in this file: "run of ~10 consecutive u32, base+0..+9,
    base shifts with cumulative channel context"). So this is a KNOWN, formula-able value
    — NOT opaque per-track data. Earlier draft said "only 4 bytes change" — WRONG, it's
    the counter run.
  - The per-track `0x258a` overview entry: same shape as the audio-track overview entry
    body_synth already synthesizes elsewhere.
  ⚠️ BUT the base-shift is NOT a constant: measured first-counter shift = **N1 stereo
  +2580, N2 stereo +2520, mono+stereo −30** (negative!). So it is context-dependent and
  NOT yet decoded — my "+2520 constant" guess was WRONG. These cumulative counters were
  long flagged COSMETIC for UNIFORM sessions (loaded fine when "wrong") but LOAD-CRITICAL
  for MIXED sessions (the MSM saga). For the 12-track 4mono+8stereo target (mixed), they
  likely DO matter. Decoding the counter base as a function of (track index, cumulative
  preceding channels, click present) is an OPEN sub-problem the project has circled before
  without a closed form.
  HONEST ASSESSMENT: layout-independent add_click is NOT a quick win. The 9 constant reps
  are easy; the 0x258a overview entry is probably easy (reuse the audio overview synth);
  but the 0x261b counter run needs the counter-base formula, which is undecoded and was
  explicitly the hard part of mix-and-match. Recommend PATH 1 (a real 12-track click
  control) unless/until that formula is cracked.

## To finish (next session, clean channel) — two paths
1. **Easiest reliable: ask the user for a 12-track click control.** Save
   `control_files/lots of stereo tracks/4 mono 8 stereo.ptx` (clean) + `4 mono 8 stereo
   plus click.ptx` (same + Click) — same folder/leaf. Then `add_click` is byte-exact
   immediately (proven type/count-agnostic GIVEN a matching pair). This sidesteps the RE.
2. **True layout-independence (the chosen path; real RE):**
   a. Decode the 4-byte `0x261b` per-track change: what value, as a function of track
      index / cumulative channel / click presence? (diff each audio track's 4 bytes in a
      multi-track click control, e.g. once a 12-track click control exists, or from the
      existing 2-track ones + the 1-track.)
   b. Decode the per-track `0x258a` overview `0x2589` entry the same way (likely the same
      shape as the audio-track overview entry already synthesized in body_synth).
   c. Rewrite `apply_click_patch` (or a sibling) to: apply the 9 CONSTANT reps re-keyed by
      owner signature against the TARGET; SYNTHESIZE the 2 per-track reps for ALL target
      audio tracks; then counts + `compose_index(click_tracks=)` + `_set_index_offset`.
   d. VALIDATE byte-exact on N=1/N=2/mono+stereo (must still pass), THEN build the 12-track
      click and PT-load.
   NOTE even path 2 ideally wants a 12-track click control to VALIDATE the result
   byte-exact — so path 1's control helps either way.

## "Click at the TOP" = overview order only
Overview order is COSMETIC (PT accepts any permutation — confirmed). Keep the click
structurally LAST (track N+1) and set its overview value to 0 with audio shifted +1:
after add_click, `_set_overview_order(body, [12,0,1,2,3,4,5,6,7,8,9,10,11])` (body slice
only; index untouched). Placing the click structurally first is unproven + unnecessary.

## Recreate the investigation (recipe)
`derive_click_patch(clean, click).replacements` = list of (start,end,new_bytes,owner_zmark)
in donor-body coords. Group by `_nodes(clean_body)[owner].t`; an owner type whose count
grows from N=1→N=2 is per-track (0x258a, 0x261b); the rest are constant. For the 0x261b
4-byte change: `old=clean[start:end]`, compare to `new`, the differing indices cluster at
~+1050+4.

## ⚠️ TOOLING — RESTART RECOMMENDED
Command OUTPUT garbled repeatedly this turn (empty returns, injected phantom text like
"wait the output is garbled", duplicated/`</parameter>`-tag noise, truncation). Writes +
Edits + the Read tool stayed reliable; only Bash stdout was corrupted. Per the user's
standing instruction, flagged + paused. Next session: restart for a clean channel.

## State / no regressions
- Only `synth_4mono_8stereo.ptx` written; `body_synth.py` / `click_clone.py` UNCHANGED
  (investigation only, no production edits). Dead-code cleanup from the prior turn stands;
  69 tests still green. Scratch `dbg_*.py` removed.
