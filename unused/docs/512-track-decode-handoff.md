# 512-track decode — setup + plan (2026-05-30). START A FRESH SESSION FIRST.

## Why this doc exists
The user created large count controls to enable decoding the N-flavored per-track fields
(the blocker for arbitrary mono/stereo/click/MIDI synthesis). The channel had been garbling earlier this session, but at handoff time all three files
read CLEANLY and consistently (two independent probes agreed; sizes match `ls`). Composition
below is trustworthy. Decode work itself should still start on a fresh session for safety,
but the files are confirmed good and the click-count claim is settled (see below).

## The new controls (present + intact, ALL cleanly verified rc=0)
`control_files/512 tracks/`  (two independent probes agreed; sizes match `ls`)
- `512 mono tracks.ptx`   (1,423,357 B)  — 512 mono tracks, 0 click playlists.
- `512 stereo tracks.ptx` (1,473,948 B)  — 512 stereo tracks, 0 click playlists.
- `128 click tracks.ptx`  (473,955 B)    — **0 audio tracks, 0 audio playlists (0x261c),
  128 click playlists (0x261e).** ⭐ CORRECTION: my earlier claim "Pro Tools allows only
  ONE click track per session" is FALSE — PT allows many (here 128). The click code's
  docstrings/comments that say "one per session" should be fixed. This file is a clean
  click-only count-series → it's the substrate to decode the per-CLICK-track unit + its
  N-flavored fields, same as mono/stereo. (track_types() returns 0 because it only
  enumerates 0x1014/0x1057 audio/MIDI headers; clicks aren't audio tracks. Count clicks
  via 0x261e.)

## First steps next session (clean channel)
1. Re-confirm composition of all three (track_types + 0x261e count), writing results to a
   file and reading a few lines — and SANITY CHECK the reader before trusting it (e.g. a
   load rc must be in {0,-1,-2,-3,-4}; a value like 128 means garbled output, stop).
2. Decode goal: turn the "copy N-flavored fields from a same-N control" dependence into
   per-track FORMULAS, so synthesize_mixed_session works for arbitrary N/mix without a
   matching control. The 512 files give 512 clean samples of "track at index i".

## What is ALREADY verified-true about the target fields (trust these; bytes-backed)
- **Per-track counters (0x261b run):** within ONE session, counter(track i) = seed + 10*i
  (stride +10 EXACT, validated by an extractor that reproduced 2-stereo bases 11161/11171).
  The per-session SEED is NOT a function of (index, N) alone and NOT per-track-type:
  3-stereo seed→11131, 3-mono→13191, SMS→13581, MSM→13611 (all N=3, different seeds).
  Seed determinant UNKNOWN. ALSO: these counters were proven LOAD-INNOCENT at N=3 mixed
  earlier (v3-counters did NOT fix a load; v4-path-wrapper did) — so they may NOT be the
  12-track "end of stream" cause at all. Don't assume counters == the bug.
- **Overview extent:** stereo controls n10-12=311, n13-16=602, ≤9=0. It is keyed to TRACK
  COUNT. BUT it is NOT the 12-mixed EOF cause: wrong extent → "magic ID does not match",
  whereas the failure is "end of stream" (different symptom; user correctly flagged extent
  as a red herring; setting 311 did not fix the load).

## RETRACTED (do not believe earlier notes that say these)
- A counter formula `base(i,N)=(11149-4N)+10(i-1)` — FABRICATED, struck. No data behind it.
- "extent=0 is the 12-track EOF bug" — wrong symptom; retracted.

## The actual open problem — RESOLVED (2026-05-30). It was NOT a synth bug.
>>> Full writeup + bytes: `docs/12mixed-eos-findings-2026-05-30.md`. <<<
Approach (a) was taken. PT-CONFIRMED by user: 12-track MIXED (4 mono + 8 stereo) built by
the UNMODIFIED current `synthesize_mixed_session` **LOADS** (`control_files/test_C_12mixed_baseline.ptx`).
The old `synth_4mono_8stereo.ptx` failed ONLY because the prior session baked in a manual
`extent=311` hack (this doc mislabeled it "harmless"); that scrolled the edit window while
the library-inherited window markers still flagged tracks 3,4 (above the scrolled range) →
inconsistent window state. The rule (4-file PT bracket):
  **EOS ⟺ (extent ≠ 0 AND a window marker points outside the scrolled-visible range).**
Either alone loads. Current code emits extent=0 (a legit state — real PT N=20/24 controls
ship extent=0 too) and is immune. Everything else was ruled out byte-by-byte: counts incl.
0x1054=20, the whole final index (clean tiling, all 355 holes resolve), path wrappers (0
overflow), 0x2067.
CONSEQUENCES for the two retracted-but-lingering threads:
 - **Counters are load-innocent even for MIXED.** test_C's per-track cumulative counters are
   the raw mono+stereo patchwork (current code never recomputes them) and it loads. So the
   "decode the counter seed formula" goal is NOT needed for loading. (Confirms this doc's own
   earlier suspicion; the real N=3 fix was v4-path-wrapper, not v2-counters.)
 - **Approach (b) (decode each N-flavored field as f(index)) is not required to load mixed N.**
   The 512 uniform files now serve as universal libraries (mono_lib=(512mono,512),
   stereo_lib=(512stereo,512)) so any mix up to 512 tracks can be sourced by index.

## Remaining (post-resolution)
 - VERIFY generality: build a larger / >8-mono mix with the 512 libs and PT-confirm (the old
   mono lib only reached 8; 512mono lifts that). Likely the last loose end for arbitrary mix.
 - OPTIONAL polish: make the mixed path emit a CONSISTENT window state (clear ALL stray
   markers; only then may extent be set non-zero). Not required to load.
 - Untouched: MIDI tracks (need a `N MIDI tracks.ptx` series), click at high N, reorder, clips, tempo/meter.

## State
- No production code changed. 69 tests green. synth_4mono_8stereo.ptx is a broken artifact
  (does not load; has a harmless extent=311 hack baked in). Scratch dbg_*.py removed.
- Click feature (small sessions) remains PT-confirmed + byte-exact; untouched.
