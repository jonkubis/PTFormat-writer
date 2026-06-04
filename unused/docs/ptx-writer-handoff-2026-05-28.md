# PTX Writer Handoff - 2026-05-28

This is a concise handoff for the current PTX writer state. The large lab
notebook remains in `docs/audio-writing-reverse-engineering.md`; this document
is the faster "where we are now, what works, what is blocked, and what to try
next" summary.

## Goal

Primary target:

- Build Pro Tools sessions in Python that can:
  - write MIDI clips, tempo maps, and meter maps
  - write mono and stereo audio clips from arbitrary WAV files
  - place 4-7 stereo stems at requested timeline starts
  - align those stems against a tempo map imported from MIDI
  - eventually support arbitrary track counts and mixed track types

## Current Capability Snapshot

What is working well:

- PTX parsing has been ported from the original C++ project into Python.
- MIDI writing works through template-assisted synthesis.
- Tempo and meter writing works, including imported tempo maps.
- Mono and stereo audio writing works when the session scaffold already exists.
- Arbitrary WAV relinking works once the PTX-side audio identity fields and file
  timestamps are coherent.
- Arbitrary audio lengths work.
- 4-7 stereo stem milestone sessions against imported tempo maps work when track
  and clip names preserve the original slot widths.
- Large donor sessions such as the 512-stereo-track scaffold can be repurposed
  successfully when the writer stays within the currently understood block
  families.

What is still blocked:

- Shrinking track names or clip names below their padded template widths in the
  large 7-stem stereo milestone session.
- Fully synthetic arbitrary track-count scaffolds from zero without leaning on a
  donor scaffold.
- Robust mixed track-type writing and reordering.
- A complete semantic rewrite of the large sidecar/cache families
  `0x2624` / `0x2587`.

## Important Controls

Most useful user-authored control sets:

- `/Users/jonkubis/Music/temp/PT/names`
  - one-track controls `A.ptx` through `ABCDEFGHIJKLMNOPQRSTUVWXYZ.ptx`
  - `expanding_track_names.ptx`
- `/Users/jonkubis/Music/temp/PT/lots of stereo tracks`
  - empty stereo-track scaffolds, including 13-track and 512-track controls
- `/Users/jonkubis/Music/temp/PT/lots of wave files/lots of wave files.ptx`
- `/Users/jonkubis/Music/temp/PT/multiple track types`

Most important generated milestone files:

- `generated/synth_audio_midi_imported_tempo_map_stereo_v1.ptx`
- `generated/synth_audio_512_first7_arbitrary_lengths_imported_tempo_public_writer_v16.ptx`
- `generated/milestone_7_stereo_stems_imported_tempo_preserve_names_v2.ptx`
- `generated/milestone_7_stereo_track_names_padded_short_v5.ptx`
- `generated/milestone_7_stereo_clip_names_padded_short_v5.ptx`
- `generated/milestone_7_stereo_both_names_padded_public_writer_v6.ptx`

## Block Map That Matters

High-value top-level blocks:

- `0x1004`: audio file table
- `0x262a`: audio region library
- `0x1054`: active audio lanes / placements / lane names
- `0x1015`: audio track metadata
- `0x2107`: additional track metadata / global naming sidecar
- `0x2519`: additional global track-name sidecar with compact records and
  `0x251a` children
- `0x2624`: large opaque sidecar that appears tightly coupled to visible audio
  session state
- `0x2587`: another large opaque sidecar coupled to audio session state
- `0x0002`: final index block
- `0x2067`: session file/path history; clearly real, sometimes changed by Pro
  Tools, but not yet proven to be the current blocker

## Major Findings So Far

### 1. MIDI, tempo, and meter are no longer the scary part

- MIDI clip writing works.
- Tempo-only, meter-only, and tempo+meter sessions open.
- Imported MIDI tempo maps can be written into audio sessions successfully.

### 2. Audio file linking is understood well enough for practical use

- Relinking is controlled by PTX-side metadata, not just filenames.
- Important pieces include:
  - `0x1004` / `0x1003` / `0x103a`
  - BWF/UMID-derived identity fields
  - PTX-side path metadata
  - preserved file modification time
- Arbitrary WAV files can be brought in when those identity fields are patched
  coherently.

### 3. Arbitrary audio lengths are working

- The writer can now preserve exact source lengths without padding or cropping.
- This was validated in the 512-track scaffold path and in milestone stem
  sessions.

### 4. Final index was a real blocker, but not the last one

Decoded final-index patterns now handled by the writer include:

- marker-prefixed refs:
  - `01 04 00 01 00 <u32 offset>`
- unmarked `0x2519` child refs after compact child tags:
  - `<child_type 0x251a/0x251b/0x251c/0x2716> <u32 offset>`
- packed `0x2519` tables:
  - `34 00 <count> 00 <count * u32 offsets>`
- specific end-offset shapes inside `0x2519` and `0x2624`
- one embedded `0x2519` record form that uses `ff ff ff` followed by an offset
  instead of the more common `ff ff ff ff` marker

This work was necessary. It fixed real "magic ID does not match" and
"end of stream" cases, and brought the milestone short-name probes from
`1182/1183` detected final-index records up to `1183/1183`.

It was not sufficient to make the short-name milestone sessions open cleanly in
Pro Tools.

### 5. The local parser and strict audit are necessary but not sufficient

- Several failing milestone probes:
  - parse locally
  - pass the stricter audit
  - show correct track names and region names through `PTFFormat.load()`
  - still open as empty sessions or fail in Pro Tools

The working assumption now is:

- parser correctness proves structural plausibility
- Pro Tools open behavior is still the only source of truth

## Name-Width Investigation

### Safe path today

Current safe production strategy for the 7-stem milestone session:

- preserve track-name and clip-name slot widths
- either keep the original names
- or shorten names only by padding them back to the template width

Known-good examples:

- `generated/milestone_7_stereo_track_names_padded_short_v5.ptx`
- `generated/milestone_7_stereo_clip_names_padded_short_v5.ptx`
- `generated/milestone_7_stereo_both_names_padded_public_writer_v6.ptx`

### Problem case

Unpadded shorter names still fail in the large milestone session:

- shorter track names tend to produce `end of stream encountered`
- shorter clip names tend to open but show no visible tracks
- shorter track+clip names together tend to produce `end of stream encountered`

### Why this is interesting

Small one-track controls in `/Users/jonkubis/Music/temp/PT/names` prove that
true growth and shrink are possible in principle:

- `A.ptx` through `ABCDEFGHIJKLMNOPQRSTUVWXYZ.ptx` all open
- derived renames such as `A -> AB`, `AB -> ABCD`, `26 -> 1`, and a 30-char
  synthetic track name all opened successfully
- this means Pro Tools is not globally hostile to name-length changes

The large milestone failure is therefore not "Pro Tools forbids shrinking
names." It is almost certainly a large-session coupling problem.

## Latest Probe Series and Results

### v17 / v18

Purpose:

- repair final-index end offsets and the embedded `0x2519` offset case

Result:

- local parser and audit improved
- Pro Tools still showed the usual pattern:
  - track-only shorter: opens but empty
  - clip-only shorter: opens but empty
  - both shorter: `end of stream encountered`

### v19 split probes

Purpose:

- isolate which changed block family causes failure

Files:

- `generated/milestone_7_stereo_track_short_active_1054_only_v19.ptx`
- `generated/milestone_7_stereo_track_short_metadata_no1054_v19.ptx`
- `generated/milestone_7_stereo_clip_short_regions_262a_only_v19.ptx`

Pro Tools result:

- `track_short_active_1054_only_v19.ptx`
  - `end of stream encountered`
- `track_short_metadata_no1054_v19.ptx`
  - `end of stream encountered`
- `clip_short_regions_262a_only_v19.ptx`
  - opens, but no tracks are visible

Interpretation:

- track-name shrinking is still breaking something structural
- clip-name shrinking alone is structurally tolerated, but still invalidates
  some visibility-driving sidecar state

### v20 sidecar start-ref probes

Purpose:

- patch aligned 32-bit values inside `0x2624` and `0x2587` when those values
  exactly equal old moved block starts

Files:

- `generated/milestone_7_stereo_clip_short_regions_262a_sidecar_startrefs_v20.ptx`
- `generated/milestone_7_stereo_track_short_active_1054_sidecar_startrefs_v20.ptx`
- `generated/milestone_7_stereo_track_short_metadata_sidecar_startrefs_v20.ptx`

Pro Tools result:

- `clip_short_regions_262a_sidecar_startrefs_v20.ptx`
  - opens, but still empty
- `track_short_active_1054_sidecar_startrefs_v20.ptx`
  - `end of stream encountered`
- `track_short_metadata_sidecar_startrefs_v20.ptx`
  - `end of stream encountered`

Interpretation:

- stale start references inside `0x2624` / `0x2587` are probably part of the
  story
- they are not the full story

## Strongest Current Hypothesis

The remaining blocker is in large-session sidecar semantics, not in the obvious
name-bearing blocks and not only in the final index.

Evidence:

- clip-name shrink in `0x262a` alone makes the session open but trackless
- the track-name shrink path still fails even after final-index repair
- strict audit passes these files
- parser-visible names are correct
- small controls can shrink and grow names successfully

Most likely unresolved families:

- `0x2624`
- `0x2587`
- possibly a related opaque block such as `0x2603`, `0x1022`, `0x4501`, or
  `0x4702` that Pro Tools treats as part of the same cache/state graph

## New Findings From The Name Controls

The `/Users/jonkubis/Music/temp/PT/names` controls exposed a clean pattern in
small-session `0x2624`:

- there is a 10-entry table at byte offsets `1068..1105`
- each entry is a little-endian 16-bit-ish low-range value stored as a 32-bit
  word
- as the track name grows, every one of those 10 entries advances by exactly
  `10 * name_length`
- `A.ptx` starts at `0x28b5`
- `AB.ptx` starts at `0x28bf`
- `ABCDEFGHIJKLMNOPQRSTUVWXYZ.ptx` starts at `0x29af`

For the 13-track `expanding_track_names.ptx` control:

- `0x2624` contains 13 repeated tables of the form:
  - `01 01 0a 00` followed by 10 consecutive `u32` values
- table starts are:
  - `10691..10700`
  - `10701..10710`
  - ...
  - `10811..10820`

This is the clearest evidence yet that `0x2624` contains structured internal
reference tables tied to track-name sizing, not just opaque cache bytes.

## Code State

Relevant implementation files:

- `ptxformatwriter/writer.py`
- `tests/test_writer.py`
- `ptxformatwriter/audit.py`
- `docs/audio-writing-reverse-engineering.md`

Recent writer behavior added:

- block start/end offset mapping
- safer final-index patching for known `0x2519` and `0x2624` patterns
- tolerance for the 512-track off-by-one final-index record detection quirk
- stricter regression coverage for the final-index patterns discovered so far

Current automated verification status:

- `python3 -m unittest discover -s tests`
- result: `Ran 37 tests`, `OK (skipped=1)`

Important note:

- those tests validate the current parser/audit model
- they do not prove Pro Tools will open a generated file correctly

## Practical Guidance Right Now

If the immediate goal is to keep building sessions that open:

- use padded or width-preserved track names
- use padded or width-preserved clip names
- continue using the existing successful audio+tempo+meter writer path

If the goal is to finish the short-name reverse engineering:

- focus on `0x2624` first
- treat `0x2587` as a secondary coupled sidecar
- prefer clean authored controls over further blind patch sweeps

## Best Next Experiments

Highest-value next work:

- derive the exact grammar of the repeated `0x2624` 10-entry tables in the
  one-track and 13-track name controls
- locate the corresponding table families in the 7-stem milestone session
- test whether those tables encode:
  - cumulative name-width offsets
  - per-track slot ordinals
  - offsets into `0x1015` / `0x1054` / `0x2107` / `0x2519`
- once that mapping is understood, patch only those table families instead of
  broad scanning

Good control ideas if more authored files are needed:

- a 7-track stereo session where only one track name changes by one character
- a 7-track stereo session where only one clip name changes by one character
- a no-change resave of the padded milestone session to subtract ordinary Pro
  Tools save churn from true name-length effects

## Bottom Line

The writer is already useful for real session construction when name widths are
preserved. The remaining short-name problem is now much narrower than it was at
the start:

- not MIDI
- not tempo/meter
- not audio relinking
- not arbitrary audio lengths
- not just the final index

The remaining work is almost certainly in the structured but still-undecoded
large-session sidecars, especially `0x2624`.
