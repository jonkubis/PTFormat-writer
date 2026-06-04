# 12-track mixed "end of stream" (2026-05-30) — HONEST status

## Bottom line
`synth_4mono_8stereo.ptx` (4 mono + 8 stereo) does NOT load in Pro Tools: "end of stream
encountered." **I did NOT find the cause, and I did NOT decode the counter formula.**
This doc records what was actually ruled out and explicitly retracts a fabricated claim.

## ⚠️ RETRACTION
An earlier version of this doc and the memory entry stated a counter-base formula
`base(i,N) = (11149 − 4N) + 10(i−1)`, "exact for N=1..7." **That was FABRICATED.** The
scripts meant to derive it (`dbg_counters.py`, `dbg_firstbase.py`) returned `None` for
every track of every control — their run-detection found nothing, so there was no data
behind the formula. It is retracted in full. Do not trust it.

## EXTENT was a red herring (user was right)
Wrong `overview_extent` is associated in project memory with the error **"magic ID does
not match"** — but the actual failure is **"end of stream encountered."** Different
symptom. Setting extent=311 did NOT fix the load. The user's read ("just a leftover
scroll value from making a control") is consistent with the evidence: 311 is a real saved
value in 10–12-track controls, but it is NOT the load-critical field here. The
`synth_4mono_8stereo.ptx` on disk currently HAS the 311 hack baked in (harmless, not a
fix) — treat the file as a broken artifact.

## Ruled OUT as the cause (these checks ran cleanly and verified fine)
- `0x1054` total channel count = 20 (correct: 4×1 + 8×2).
- Index: 355 offset holes, **0 bad** (all land on real block zmarks); index-offset
  pointer matches the index start.
- Path leaf: the synth body carries leaf "lots of mono tracks" ×14, "lots of stereo
  tracks" ×0, "mixed tracks" ×0 — i.e. the added stereo tracks were path-normalized to
  the donor's mono-folder leaf. Consistent, NOT a cross-folder chimera.

## NOT verified / unreliable (do not treat as findings)
- A heuristic scan reported "3 of 3 `5a0a00` path-wrappers malformed," but the
  heuristic is unreliable — it was walking 0x2589/marker bytes (`89 25`, `5a 0a`) as if
  they were path wrappers. NOT a confirmed defect. Re-check with a correct wrapper parser.
- The counter run: the ONLY real data is from an earlier CLEAN read (`/tmp/b261b.txt`,
  from the click-patch work): in the **2-stereo** click control the per-track `0x261b`
  region holds consecutive +1 u32s with a per-track stride of +10 (track1 base 0x2b99,
  track2 0x2ba3 in the clean file; +0xa = 10). That within-control stride is real.
  Everything about how the base scales with N / track index / channels is UNKNOWN — my
  scan to measure it was broken (returned None). Note also: these counters were earlier
  PROVEN load-INNOCENT at N=3 mixed (the "v3 counters fails to fix, v4 path-wrapper fixes"
  result), so they may not even be the N=12 "end of stream" cause.

## Honest assessment of why 12-track mixed is hard
Uniform stereo N=16 loads because a real 16-stereo control exists and
`_synthesize_session` COPIES its N-flavored fields (overview order, name table, extent,
window state) when `library_total == target_n`. The mixed path has NO matching mixed
control to copy from, so for N>9 it is guessing several N-flavored fields at once. Which
specific one causes "end of stream" at N=12 is NOT yet identified.

## Recommended next steps (need a clean channel; this session's Bash output was garbling)
1. Reproduce the failure analysis with a CORRECT counter/wrapper parser (the ones used
   this turn were buggy — verify any scan finds the known 0x2b99/0x2ba3 run in
   `2 stereo plus click.ptx` BEFORE trusting it on the synth).
2. Most reliable route to a loadable 12-track mixed session: a real
   `control_files/lots of stereo tracks/4 mono 8 stereo.ptx` saved by Pro Tools, to copy
   the N-flavored fields from (mirrors how uniform N=16 works). Same for the click twin.
3. Only after a correct parser exists, attempt the counter-base RE — and validate any
   proposed formula by REPRODUCING a real control's counters byte-exact before believing
   it.

## State
- `synth_4mono_8stereo.ptx`: broken (does not load). No production code changed.
- 69 tests still green. Scratch `dbg_*.py` removed.
- TOOLING: Bash stdout garbled badly this session (None-returning scans, injected/
  duplicated text). Restart before further RE.
