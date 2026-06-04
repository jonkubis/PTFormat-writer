# Beatmap → Pro Tools pipeline: VISION + ROADMAP handoff (2026-06-02)

START HERE for the end-to-end goal. Self-contained. Pairs with project memory
(`MEMORY.md`) which has the per-feature byte-level decodes.

## The end goal (user's words, scoped 2026-06-02)
A tool that takes the user's **beatmapping software output** and produces ONE
self-contained Pro Tools session:
- INPUT 1: a **MIDI file** with markers, a meter map, a tempo map, and a MIDI
  **Start message (0xFA)** on a dedicated track marking the audio head-sync point.
- INPUT 2: **MP3s** (stereo) → convert to WAV at the **session format (44.1 kHz /
  24-bit / stereo)**.
- OUTPUT: a single `.ptx` + local `Audio Files/` (no externalities) containing:
  - 1–16 **stereo** audio tracks, one stereo clip each, **all starting at the 0xFA
    head-sync** (which can be anywhere — usually bar 1–3, but must be arbitrary),
    clips named after the MP3 filenames.
  - The **tempo map** (potentially HUNDREDS of changes).
  - The **meter map** (a dozen+ changes).
  - **Markers** from the MIDI.
  - A **Pro Tools click track at the TOP** of the session.

## What's DONE + PT-CONFIRMED (the foundation — `ptxformatwriter/body_synth.py`)
- **1–16 stereo track scaffolds**: `synthesize_stereo_session` / `synthesize_mixed_session`
  (mix-and-match confirmed at scale). Click handling: `add_click_anyN(data, clean_ref,
  click_ref, at_top=True)` + `move_click_to_top(data)` — **click at TOP, PT-confirmed**.
- **Audio clip insertion**: `add_audio_clip(data, clean_ref, clip_ref)` — transplants a
  clip (or MULTIPLE clips, or a different-wav clip) from a matched control pair; reindex
  via `final_index.reindex_after_resize`.
- **Arbitrary WAV linking (BWF)**: `set_clip_wav(clip_data, wav_path, *, filename, region_name)`
  + `wav_clip_identity(wav_path)`. THE FIELD MAP (all read from the wav, no mtime games):
  sample_count = data/blockalign → 0x1001 len@+15 + 0x2628 region len@(namelen+18); umid
  material = wav `umid` chunk [7:15] (`2a <hash4> ef <B> 80`, B VARIES per file) → 0x1003
  TWO copies: 0x1001 copy @+44 keeps the `2a`; **0x2106 copy @+292 is `00`-PREFIXED (not
  2a — the last subtle relink bug)**; 2-byte id = bext UMID marker(`060a2b34`)+24 → @+301;
  filename → 0x103a; **region name = FILENAME STEM** → 0x2628. NON-GATING (keep donor's):
  0x1003 @+100/@+172 timestamps, @+202 16-byte private id, file mtime.
- **Arbitrary WAV (raw, no umid)**: `wrap_raw_wav(raw_data, template_wav, *, seed)` —
  grafts raw fmt+data into a PT WAV template + generates a deterministic SMPTE-UMID
  written to umid[7:15]/regn[11:19]/bext(marker+16,+21,+24) + regn[28:32]=frames. Then
  `set_clip_wav` links it. Requires raw to be 44.1k/24-bit/stereo (matches template).
- **Tempo / meter (single + ONE mid-session change)**: `set_tempo`/`set_meter` +
  `set_tempo_map(events=[(bpm,tick)])`/`set_meter_map(events=[(num,den,tick)])`. TICKS_PER_QUARTER
  =960000; stored pos = ZERO_TICKS(0xE8D4A51000)+tick. Tempo record 61B "Const..TMS" in
  0x2028+0x2718 (bpm f64 @+40, pos @+30); meter record 36B after ") Meter" in 0x2029+0x2719
  (pos@0, ordinal@+8, num@+12, den@+16).
- **MIDI note** + **MIDI editor-window suppression** (pass an editor-closed midi_ref).
- **Track names** (`rename_track`). Self-contained staging: put WAVs in `Audio Files/`.
- **98 tests green** (`python3 -m unittest discover -s tests`). Last commit before this
  session's batch = `4264575`; THIS batch (set_clip_wav/wrap_raw_wav/add_audio_clip
  generalize/midi-editor) is being committed now.

## GAPS to build (critical path, in recommended order)
**Build 1 — audio core: ✅ DONE + PT-CONFIRMED (2026-06-02).** The whole audio half is one
call: `body_synth.build_audio_clips(data, tracks, clip_ref)` — `tracks` is a list (per
track) of `(wav_path, position_file_samples)` clip lists; it assembles the file table /
region library / placements / index from scratch for ANY number of distinct wavs, ANY
clips per track, each its own staged BWF WAV + position. Plus `add_clip_to_track` /
`add_clips_to_tracks` (transplant path), `set_clip_wav` / `set_clip_wavs` (re-point),
`set_clip_position` (per-clip positions). PT-CONFIRMED: distinct-wav + same-wav, multi-per-
track, staggered positions. From-scratch invariants that bit (each now guard-tested vs a
control): the 0x103a folder-path TRAILER (omit → "end of stream"), its D-dependent path
component indices (`_renumber_path_trailer`, wrong → "out_of_range Cmn_PolyVectorImpl::At"),
and the per-lane placement trailer once-at-end (per-placement → "magic ID … Audio Playlists").
118 tests. The per-feature decode notes below remain for reference.

**Build 1 detail (per-feature decode, for reference):**
1. **Stereo both-channels** — channel mapping DECODED (2026-06-02, structural): the file
   channel index is `0x104f` placement payload[3] (0=lane .L, 1=lane .R) AND `0x2629`
   region byte 97 (0/1). `one clip bar 1.ptx` already encodes `.L→ch0 / .R→ch1`, and all
   control WAVs (GIGZ / WRAPPED RAW / 01.wav) are genuine stereo (L≠R). So the earlier
   "one channel" report is most likely a relink/overview-cache artifact, NOT structure.
   ORACLE CHECK PENDING: does untouched `one clip bar 1.ptx` play both channels in PT?
2. **Arbitrary clip POSITION — DONE + byte-exact (2026-06-02):** `body_synth.set_clip_position(
   data, position_file_samples)` + `clip_positions(data)`. The timeline start lives SOLELY
   in the `0x104f` placement field (8-byte LE FILE samples, full[16:24]=offset+9), mirrored
   per lane; pos × file_rate (44100). Ground truth bar1=0 / bar2=88200 / bar3=176400 (linear,
   2.0s/bar @120). Patching bar2's field to 176400 reproduces real bar3 with ZERO unexpected
   diffs — the only other bar2↔bar3 changes are display caches PT recomputes (0x2016/0x2056/
   0x2587/0x2624 overview-ramp+flag), the 0x262a region-GUID save-nonce, and derived index
   (0x0002 / 0x2067 identity / first-block 0x1654-5 pointer). Move is SIZE-NEUTRAL → no reindex.
   pos>0 needs a pos>0 template (the 0→nonzero transition adds a 0x2038 sub-block to 0x2624,
   +19B); build from a `one clip bar 2`-style ref. ORACLE files: SYNTH_clip_bar3_from_bar2 /
   _offgrid_pos(250000) / _bar7. Controls now have `one clip bar 3.ptx`. 101 tests green.
3. **Clip onto a SPECIFIC track of N — DONE + PT-CONFIRMED (2026-06-02):**
   `body_synth.add_clip_to_track(data, clip_ref, track_index, *, position_file_samples=None)`.
   PT-confirmed (clip on tracks 1/2/3 of a 3-stereo all load with correct placement). The
   ENTIRE load-bearing footprint = the 0x1054 placement (clip lands on track K's two lanes
   BY NAME) + a SYNTHESIZED master index (a clip adds exactly one 0x0f3c marker on the
   0x0f3d record — `_synth_clip_index`, validated byte-exact). The 0x2519 clip flag, the
   0x2624 position block, and the 0x2515→0x2a34 view block are display state PT rebuilds
   (omitting them loads fine). Global wav/region/path blocks transplant from clip_ref;
   position composes via set_clip_position. REMAINING: MULTI-CLIP (one clip per track, each
   own WAV) — N 0x0f3c markers + 0x1004/0x262a/0x0f3d holding N wavs + positional region
   linkage; current add_clip_to_track assumes a clip-FREE base (single clip).

**Build 2 — conductor (scaled maps + markers):**
4. **Tempo/meter map at scale — DONE (2026-06-02, PT-pending):** `set_tempo_map`/`set_meter_map`
   now BUILD N records (was: transplant fixed slots). Helpers `_resize_tempo_block` (+61/rec:
   61-byte "Const..TMS", count@rec0-8, length@rec0-12=4+61N, fixed trailer ending in f64
   44100.0; record flags@21&39, pos@30, bpm@40) and `_resize_meter_block` (+52/rec = a 36-byte
   record [pos@0/ord@8/num@12/den@16] + a 16-byte trailing-list entry [ord@0]; count@tag+13,
   length@tag+9=12+52N; 0x2719 wraps a nested 0x2029 so bump BOTH sizes). 0x1040 = +42 one-time
   (count-independent, confirmed across 1/2-event controls). Pattern: grow the ref's blocks to
   N records → reindex ref self-consistent → `_transplant_top_level` → patch each record. Both
   reproduce the 2-event controls BYTE-EXACT and build 50 events reading back exactly. Also
   FIXED `changed_top_level_types` to exclude the first-block index-pointer (its type shifts on
   resize). PT-verify: SYNTH_tempo_6changes / SYNTH_tempo_50changes / SYNTH_meter_4changes (various/).
   REMAINING risk only at N≥3 byte-exactness (per-record ordinal vs first-flag) — confirm with a
   3–4 event control. 13 tempo/meter tests.
5. **Markers** (`0x271a` MARKER list): undecoded. Want a control with a few named markers
   at positions; decode + build via the same clean-pair method.

**Build 3 — orchestration (the actual tool):**
6. Compose: synth N tracks → click on top → clip per track at head-sync → tempo map →
   meter map → markers → stage WAVs → one self-contained `.ptx`. Features touch mostly
   DISJOINT top-level blocks (only `0x1040` shared by tempo+meter; index rebuilt each
   step) — chaining `_transplant_top_level` steps should compose; VALIDATE no conflict.
7. External glue (NOT PT-format): MP3→WAV via **ffmpeg** (target 44.1k/24-bit/stereo);
   **MIDI parse** (`mido`) for tempos/meters/markers/the 0xFA head-sync tick → PT ticks.

## Methodology (unchanged, non-negotiable)
- Pro Tools is the ONLY oracle. `load() rc==0` (our lenient reader) ≠ PT opens it. The
  user loads files and reports. Validate by reproducing a REAL control byte-exact (mod
  GUID/seed) and SHOW BYTES before claiming solved.
- The decode method that's cracked everything: a CLEAN before/after control PAIR (same
  session without/with the feature), diff at the de-XORed block level. `W.load_unxored(path)`
  to de-XOR (NOT raw bytes — `parse_unxored` assumes already de-XORed).
- Tests: `python3 -m unittest discover -s tests` (must use `python3`; 98 green, skipped=1).
- Commit/push ONLY when the user asks. HEAD on master → origin github jonkubis/PTXFormatWriter.
  Commit messages end with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- control_files/ is gitignored (controls + SYNTH_*/wrapped artifacts are NOT in git).

## Controls needed from the user for the next builds
- `one clip bar 3.ptx` (3rd position data point for the 0x2624 3-point).
- a 2–3 stereo session with a clip on a NON-first track (per-track placement).
- a session with a few named markers at positions.
- a session with 3–4 tempo changes AND 3–4 meter changes (validate building >2 records).
- (have already: clip baseline / one clip bar 1 / bar 2 / two clips same track / clip diff
  wav / 1–16 stereo + click controls / Untitled+90/121bpm+3-4 meter+at-bar-2 / MIDI controls.)
