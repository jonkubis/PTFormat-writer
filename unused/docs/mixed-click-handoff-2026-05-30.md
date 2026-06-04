# Mixed mono+stereo + click — DONE byte-exact (2026-05-30)

**Result: BYTE-EXACT.** The recursive click diff is track-type-agnostic — it reproduces
a click on a mono+stereo audio session byte-for-byte, no code changes.

## What unblocked it
The diff needs clean+click in the SAME folder (same embedded session-path leaf). The
user saved the missing clean twin:
- `control_files/lots of stereo tracks/mono stereo.ptx` (78797 B, clean, leaf "lots of stereo tracks")
- `control_files/lots of stereo tracks/mono stereo plus click.ptx` (82384 B, +3587 B, same leaf)
both [Audio 1 mono, Audio 2 stereo].

## Validation (both pass)
- `add_click(clean, clean, click)` == `mono stereo plus click.ptx` BYTE-EXACT
  (seed-neutralized), 18 replacements, reload rc=0, per-type-diffs {}.
- `synthesize_mixed_session([1,2], donor=mono stereo, mono_lib=(3 mono,3),
  stereo_lib=(3 stereo,3), click_ref=(ms,msc))` -> reload rc=0, 1 click playlist,
  [mono,stereo] kept, 3 channels, overview order [0,1,2] (valid), block structure ==
  control; differs from the real control only in the 2-byte cosmetic overview-order
  permutation ([0,1,2] vs ctrl [1,0,2]) — same acceptable difference as all-stereo [2,2].
- Wrote `control_files/synth_mono_stereo_click.ptx` for PT confirmation.

## Tests (tests/test_click.py — now 6, all pass)
`MixedClickTests`: test_add_click_mixed_byte_exact, test_synthesize_mixed_mono_stereo_click.
`_MIX_CLEAN/_MIX_CLICK` point at the stereo-folder mono+stereo pair.

## Status
- DONE: user loaded `synth_mono_stereo_click.ptx` in Pro Tools — "Loads". Mixed
  mono+stereo+click is PT-CONFIRMED. Full suite 69 tests OK (skipped=1).
- This proves click is general across BOTH track count (N=1,2) AND track-type mix.
- Remaining click TODO: only the donor-coordinate caveat (add_click's replacements are
  byte offsets in clean_ref; works when clean_ref's audio layout matches `data`, which is
  the validated path). MIDI-track + click is untouched (no MIDI synthesis yet).
