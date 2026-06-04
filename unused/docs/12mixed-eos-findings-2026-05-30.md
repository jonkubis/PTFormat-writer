# 12-track mixed "end of stream" — bytes-backed elimination + hypothesis (2026-05-30)

Approach (a) from `docs/512-track-decode-handoff.md`: bracket the EOS empirically.
Our Python reader is **load-innocent** here — it returns rc=0 on the broken file
(`synth_4mono_8stereo.ptx` parses as 12 tracks, 4 mono + 8 stereo, 20 lanes). So
PT is the only oracle; everything below is local byte analysis to NARROW the cause
before spending PT loads. NOTHING here is PT-confirmed yet.

## Ruled out (all bytes-backed, on the actual broken file)
- **Count fields** — every track-scaled header field = 12 ✓; `0x1054` total-channels
  = **20** ✓ (4·1 + 8·2; the field the code comment names as a known EOS trigger,
  but it's correct here); `0x1015 = 4117 + 65536·N = 790549` ✓.
- **Final index (0x0002)** — `record_count` 183 (== good 12-stereo), clean tiling
  (0 leftover), `0x1054` index record has 21 channel markers = 20 + 1 self-marker
  (the +1 is in every good file), and **all 355 offset holes resolve — 0 zero, 0
  dangling.** Index is pristine.
- **Path wrappers / rename** — only 3 opaque `5a 0a/0b 00` wrappers, **0 overflow**;
  the cross-folder leaf rename completed clean (14 "lots of mono tracks", 0 stray
  "lots of stereo tracks"). Memory's prime suspect (the `5a 0a 00` path-wrapper) is
  NOT the N=12 cause — that was the N=3 fix.
- **0x2067 session-info** — internally consistent (its lone `5a 0b 00` is the block's
  own header; size lands exactly on block end). The artifact's 0x2067 is byte-identical
  to the 2-mono donor's.
- **overview_extent** — independently the doc says setting 311 did not fix it; symptom
  for wrong extent is "magic ID", not EOS.

## The anomaly found (THE hypothesis — pending PT)
The `ef ff df bf` window/selection markers are **arbitrary saved UI state**, NOT a
function of N and NOT type-agnostic (stereo: N3=[], N5=[1-5], N7=[7], N12=[8-12],
N20=[19,20]; mono N8=[1-8]). PT tolerates many CONSISTENT snapshots (it loads all
controls). The mixed path builds an **inconsistent** one:
- `8 mono` lib markers = `[1-8]` → mono tracks placed at session pos 3,4 drag in stray markers.
- `12 stereo` lib markers = `[8-12]` → stereo tracks at pos 5-12 land correctly as `[8-12]`.
- `_clear_donor_window_markers(body, base_n=2)` cleans only the 2-mono donor's tracks 1,2.
- Result: `0x261c = [3,4,8-12]` — the correct `[8-12]` PLUS stray `[3,4]` no real file has.
This is exactly the "stray marker outside the visible range → load failure once tracks
overflow (N>9)" case the code comment in `body_synth._clear_donor_window_markers` warns about.

## Decisive PT battery (written to control_files/, awaiting user load)
| file | track types | 0x261c markers | extent |
| --- | --- | --- | --- |
| test_C_12mixed_baseline.ptx     | 4m+8s | [3,4,8-12] | 0   |
| test_A_12allstereo_mixedpath.ptx| 12s   | [8-12] ✓   | 0   |
| test_D_12mixed_fixedwindow.ptx  | 4m+8s | [8-12] ✓   | 311 |

(good 12-stereo = [8-12]/311 loads; artifact = [3,4,8-12]/311 FAILED.)
- **D loads** ⇒ window-state patchwork CONFIRMED as the cause (D vs failed artifact ≈ only the stray [3,4] removed). Fix = produce a consistent N>9 window state.
- **A loads** ⇒ extent=0 is tolerated; fix is markers-only. **A fails** ⇒ extent also matters.
- **C** = current-code baseline (expected fail, matches artifact minus extent).

## If confirmed, the fix (not yet written — no production code changed)
Mixed path must emit a CONSISTENT window state instead of a cross-control patchwork.
Options: (1) clear ALL window/selection markers then set the exact visible-range set +
extent borrowed from a same-track-count control (the uniform-path pattern; we have
stereo controls 0-24); or (2) clear all markers to the neutral "scrolled-to-top"
state (extent 0, no markers) — but N=10-16 controls all have extent 311/602, so a
non-zero extent may be required for N>9 (A vs D answers this).

## CONFIRMED (PT, 2026-05-30) — RESOLVED
User loaded all three in Pro Tools: **C, A, and D all load.** With the prior artifact
failure, the full truth table is:

| 0x261c markers | extent | loads | file |
| --- | --- | --- | --- |
| [8-12] good     | 311 | YES | good 12-stereo, D |
| [3,4,8-12] stray| 311 | NO  | old artifact |
| [3,4,8-12] stray| 0   | YES | C (current code, unmodified) |
| [8-12] good     | 0   | YES | A |

**EOS ⟺ (extent ≠ 0 AND a marker points outside the scrolled-visible range).** Either
condition alone loads. The stray markers are NOT fatal on their own.

### Upshot — the "12-track mixed EOS" was NOT a synthesis bug.
`test_C` is the UNMODIFIED output of `synthesize_mixed_session([1,1,1,1,2,2,2,2,2,2,2,2], ...)`
and it loads. The original `synth_4mono_8stereo.ptx` failed only because the previous
session baked in a manual `extent=311` hack (the doc mislabeled it "harmless"); that
scrolled the view without fixing the library-inherited markers, creating the inconsistency.
Current code emits `extent=0`, which is a legitimate state (real PT sessions at N=20/24
also ship extent=0) and is immune. **Mixed mono+stereo at N=12 is PT-CONFIRMED working.**

### Practical rule for synthesis
Keep `extent=0` (scrolled-to-top) and ANY window-marker set loads. Only set a non-zero
extent if you ALSO make the markers consistent with the scroll range (the uniform path
does this by copying a complete snapshot from a same-N control). The stray-marker cleanup
(`_clear_donor_window_markers` with the full added-track range) is optional polish, not
required to load.

## GENERALITY PT-CONFIRMED (2026-05-30) — mix-and-match is now general
User loaded both: **`test_stressA_16alt.ptx`** (16 tracks, strict m/s alternation, 24 ch)
and **`test_stressB_20mono.ptx`** (20 tracks, odd order `mmmsmmmmsmmmsmmmmsmm`, 16 mono +
4 stereo, 24 ch) — **both open in Pro Tools.** Built via `synthesize_mixed_session` with
`mono_lib=(512 mono tracks.ptx, 512)` (sources mono at indices >8 — impossible with the old
0-8 lib) and `stereo_lib=(N-stereo, N)` (also supplies the matching name table). Each passed
full offline checks first (order, cumulative channel indices, index: all holes resolve / clean
tiling, extent=0, path normalized). ⇒ **mix-and-match = ANY count/order/mono:stereo ratio up
to the 512-track library ceiling; the 512 controls are universal libraries; no per-track-field
formula decode is needed.**

Regression guard added: `tests/test_body_synth.py::test_synthesize_mixed_high_n_512lib`
(gated on the 512-mono control). **70 tests green.**

## Scratch
dbg_*.py + dbg_probe.out — DELETED (investigation complete). Confirmed-loadable artifacts kept:
control_files/test_{C,A,D}_12*.ptx (the bracket) + test_stress{A,B}_*.ptx (generality).
