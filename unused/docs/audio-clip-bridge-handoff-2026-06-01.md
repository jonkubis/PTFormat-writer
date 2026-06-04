# Audio/MIDI/tempo/meter content → robust framework: BRIDGE HANDOFF (2026-06-01)

Read THIS first. Self-contained. The goal is UNFINISHED; this is where to resume.

## The task (user's words)
The OLD Codex audio writer broke at arbitrary track configs (End-of-stream / Magic-ID).
Transplant the **audio / MIDI / tempo / meter / track-name** ability into the robust
`body_synth` + `final_index` framework (which synthesizes arbitrary scaffolds without
EOS/magic-ID) so content insertion stops failing at arbitrary configs.

## Two code paths (key context)
- **`body_synth` + `final_index` + `click_clone` + `mixed_order` (MINE, committed, PT-confirmed):**
  synthesizes EMPTY-track scaffolds from zero — mono/stereo/mixed any count, names, click
  bottom/top, `reorder_tracks`. Robust index via `final_index` (compose_index / rebuild_index_offsets
  / offset_holes). HEAD commit = "Add general track reordering"; pushed to github jonkubis/PTXFormatWriter.
- **`writer.py` (CODEX, separate):** adds CLIPS/content into a scaffold (template-assisted).
  API: `AudioSessionSpec/AudioFileSpec/AudioClipSpec/AudioTrackSpec`, `write_audio_session`,
  `with_audio_tracks`, `with_midi_tracks`, `with_tempo_events`, `with_meter_events`,
  `copy_audio_file_for_session`. 12 audio tests pass (in our reader). Decoded model:
  `0x1004` file table, `0x262a` region library, `0x2629/0x2628` regions, `0x1054`→`0x1052`→`0x1050`→`0x104f`
  placements, `0x1015/0x2107/0x2519` names, `0x0002` index. Full ref: `docs/audio-mono-stereo-clip-writing-reference.md`.

## The seam
ALL content fns funnel through `writer._replace_top_level_blocks` → which calls the LEGACY
`writer._update_final_index` (an offset GUESSER). `writer.py` does NOT import `final_index`.
`final_index.py` exists specifically because that guesser "is undecidable and corrupts files."

## Uncommitted changes made this session (working tree; NOT committed; 74 tests still green)
- `final_index.py`: added `reindex_after_resize(consistent_data, resized_data)` + `_set_first_block_index_pointer`.
  This is a by-(type,rank) refill. **FOUND UNSUITABLE for content-add** (see below) — consider reverting or leaving (it's opt-in, inert by default).
- `writer.py`: added `robust_index: bool = False` to `_replace_top_level_blocks` (True → use `reindex_after_resize`)
  and to `with_audio_tracks` (passthrough). Default False = legacy path unchanged.

## What was learned (CONFIRMED)
1. `final_index.rebuild_index_offsets` ROUND-TRIPS the real clip controls byte-exact → the
   final_index GRAMMAR already parses the AUDIO index correctly. (So final_index *can* model audio.)
2. The legacy `_update_final_index` corrupts at some configs (1-stereo: mangles `0x2587` so even
   our `parse_records` fails; mixed-12: dropped 6 index holes 355→349).
3. **The robust by-rank `reindex_after_resize` is WRONG for content-add:** a clip inserts NEW blocks
   of types that ALREADY exist in the scaffold (e.g. `0x4301` 6→7), shifting ranks, so by-rank refill
   resolves a hole to the WRONG same-type block → Pro Tools "unexpected stream type". (Content-add
   needs IDENTITY/offset-map remap, not by-rank — like the Frontier-2 mover's `newpos` map.)
4. The old `control_files/synth_*.ptx` are STALE artifacts: `synth_4mono_8stereo.ptx` has
   `extent=311` (the documented N>9-mixed EOS hack) → EOS empty, clip or not. NOT representative of
   current `body_synth` (which emits extent correctly). Don't use them as scaffolds.

## THE OPEN PROBLEM (where it stands)
With the writer's content builders, BOTH index paths produce PT-REJECTED clips:
- robust (by-rank): "unexpected stream type" (rank-shift bug, #3).
- legacy: ALSO errors in PT — **even on a CLEAN 1-stereo scaffold (`clip baseline.ptx`) with a
  MATCHING 1-stereo clip donor (`one clip bar 1.ptx`) + legacy index** (`legacy_audio_1stereo.ptx`).
So the writer's content-add does NOT currently yield a PT-loadable clip on this user's machine,
even in the simplest case. NB: I used the lower-level `with_audio_tracks`, NOT the full
`write_audio_session` pipeline (audit + `source_path` WAV-identity via `copy_audio_file_for_session`).
The writer's audio path may NEVER have been PT-confirmed on this user's machine (project memory never
logged an audio-clip PT load; the ref doc's "PT-confirmed" is unverified here).

## DECISIVE NEXT STEP (the methodology that cracked every prior frontier)
DIFF a writer-produced 1-stereo clip against the user's REAL PT-loadable ground truth, both
"1 stereo track + 1 clip", and find the exact differing block / length field PT rejects:
- writer output (PT-rejected): `control_files/lots of stereo tracks/legacy_audio_1stereo.ptx`
- real ground truth (PT-LOADS): `control_files/lots of stereo tracks/one clip bar 1.ptx`
Compare block-type counts, block sizes, and especially LENGTH/size fields: `0x2067` session-info
size, any `5a 0a 00 <u32 len>` PATH-WRAPPER (the documented rename-EOS cause — the wav path is a
candidate), container `block_size`s, the first-block index pointer. The residual reveals the bug.

## ALTERNATIVE (the user's literal ask): decode fresh + reimplement in body_synth
If the writer's builders are too broken to trust, decode the clip FRESH from the controls and
synthesize it with `body_synth`'s robust primitives (`apply_insertions`, the rename_track-style
identity offset-map, `final_index`). Decode already started:
- `clip baseline.ptx` (empty 1-stereo) vs `one clip bar 1.ptx` (1 clip): the clip adds, by block:
  `0x1003`(wav desc 314B)+`0x1001`(len)+`0x0f3c`(abs path)+`0x1033`+`0x1000`×2 [wav import];
  `0x2629`(275B)+`0x2628`(77B) ×2 named `.L`/`.R` [regions]; `0x104f`/`0x1050`/`0x4403` [placement helpers];
  `0x2106` (Pro-Tools metadata). A STEREO clip = 2 regions (.L/.R), one wav, both on the stereo track.
- TIMELINE position lives in the `0x2624` playlist as a VARIABLE-LENGTH 3-point (start/offset/length):
  bar1→bar2 grew `0x2624` by +19 bytes (reader `reg.start` 0 → 768000). Reader's `parse_three_point`
  decodes it. Also riders: `0x1054` +6, `0x2016` +6 (overview zoom), `0x2067` +1 (session length).
- INDEX footprint of one clip (PT's repr): only +1 element offset in the `0x0f3d` record (the
  audio-file-path index record). Tiny. (The writer instead keeps the file inside `0x1004` and adds
  no `0x0f3c`/`0x0f3d` — a different, possibly-incomplete representation.)

## Controls (KEEP — the decode set; all in `control_files/lots of stereo tracks/`)
- `clip baseline.ptx` — empty 1-stereo (resaved baseline).
- `one clip bar 1.ptx` — **PT-LOADABLE** 1-stereo + 1 stereo clip @ bar1 (GROUND TRUTH).
- `one clip bar 2.ptx` — same clip @ bar2 (isolates timeline-start field).
- `1 stereo + 1 clip.ptx` — extra.
- WAV: `Audio Files/(2017226 01116)-C2-GIGZ.wav` (stereo, ~11540 samples).
- MIDI controls for later (the MIDI/tempo/meter legs): `control_files/various/one midi note.ptx`,
  `one midi clip bar2.ptx`, velocity/pitch/length variants, tempo (`121bpm.ptx`, `120 to 140bpm.ptx`),
  meter (`3-4 meter.ptx`). Reader `tempoevents()`/`meterevents()`/`miditracks()`/`midiregions()` work.

## Reader gotchas
- `PTFFormat.tracks()/regions()/audiofiles()/miditracks()/midiregions()/tempoevents()/meterevents()`
  are **methods, not properties** — call with `()`. Empty tracks are filtered from `tracks()`.
- Region fields: `name, index, startpos, sampleoffset, length, wave, midi`.
- Our reader is LENIENT (rc==0 ≠ PT loads). PT is the only oracle (user loads). "unexpected stream
  type" = structural/framing mismatch; "End of stream" = a length field reads past data.

## Scratch to clean up (all PT-FAILED; safe to delete) in `control_files/lots of stereo tracks/`
`robust_audio_baseline.ptx`, `robust_audio_8stereo.ptx`, `robust_audio_8stereo_clean.ptx`,
`robust_audio_16stereo_clean.ptx`, `robust_audio_mixed12.ptx`, `legacy_audio_1stereo.ptx`,
`legacy_audio_8stereo.ptx`, `legacy_audio_16stereo.ptx`. (Keep the 4 user clip controls above.)

## Hard rules (project methodology)
- Validate by reproducing a REAL control byte-exact (mod GUID/seed) before claiming solved; SHOW BYTES.
- PT is the oracle. Run `python3 -m unittest discover -s tests` (must use `python3`; 74 green, skipped=1).
- Commit/push only when the user asks. HEAD is on `master`, tracking `origin` = github jonkubis/PTXFormatWriter.
