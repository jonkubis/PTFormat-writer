# Mono and Stereo Audio Clip Writing Reference

This document is the current best reference for how this repo adds mono and
stereo audio clips to Pro Tools audio tracks.

It combines:

- reverse-engineering findings from real Pro Tools control files
- the current Python implementation
- the writer's known-safe behaviors
- the gaps that still require donor scaffolds or more decoding

Primary code files:

- `ptxformatwriter/writer.py`
- `ptxformatwriter/final_index.py`
- `ptxformatwriter/audit.py`
- `tests/test_writer.py`
- `tests/test_audit.py`

Long-form lab notebook:

- `docs/audio-writing-reverse-engineering.md`

Short status handoff:

- `docs/ptx-writer-handoff-2026-05-28.md`

## 1. Executive Summary

What currently works:

- writing mono audio clips into mono audio-track scaffolds
- writing stereo audio clips into stereo audio-track scaffolds
- multiple clips on one track
- multiple mono tracks with clips
- multiple stereo tracks with clips
- arbitrary audio trims and placements
- arbitrary WAV relinking for compatible BWF/UMID WAVs
- arbitrary source lengths
- audio combined with tempo, meter, and MIDI
- large stereo donor scaffolds, including the validated 512-track path

What the writer actually does:

1. starts from a PT12 template session
2. preserves opaque blocks we do not understand yet
3. rewrites the block families we do understand
4. repairs final-index offsets
5. optionally audits the result for known PT open hazards

The crucial idea is that audio clip writing is not "build the whole PTX from
scratch." It is "rebuild the audio-bearing blocks inside an already compatible
session scaffold."

For the simplest clip-writing cases, Pro Tools accepts a PTX when only these
top-level blocks are replaced:

- `0x1004` - audio file table
- `0x262a` - audio region library
- `0x1054` - active audio lane / placement map

For track renames, these also matter:

- `0x1015`
- `0x2107`
- `0x2519`

For larger scaffolds, track-count growth, and mixed/more fragile sessions,
these also matter:

- `0x2624`
- `0x2587`
- `0x0002` final index

## 2. Current Public API

The audio-writing surface lives in `ptxformatwriter/writer.py`.

Main dataclasses:

- `AudioFileSpec`
- `AudioClipSpec`
- `AudioTrackSpec`
- `AudioSessionSpec`

Main entry points:

- `with_audio_tracks(data, audio_files, tracks, ...)`
- `write_audio_session(template, output, session, ...)`
- `write_template_session(...)`
- `copy_audio_file_for_session(source_path, audio_files_dir, filename=None)`

### `AudioFileSpec`

Represents one source audio file reference to be written into the PTX.

Fields:

- `filename: str`
- `length: int | None = None`
- `channels: int = 1`
- `source_path: str | Path | None = None`

Meaning:

- `filename` is the session-visible filename that Pro Tools will try to link
  against.
- `length` is the source frame count. If `source_path` is set, it can be
  omitted and will be inferred from the WAV.
- `channels` must currently be `1` or `2`.
- `source_path` activates WAV-identity patching for BWF/UMID metadata and file
  timestamps.

### `AudioClipSpec`

Represents one clip placement.

Fields:

- `name: str`
- `file_index: int = 0`
- `startpos: int = 0`
- `sampleoffset: int = 0`
- `length: int | None = None`
- `source_start: int | None = None`

Units:

- all audio positions here are in samples, not ticks

Meaning:

- `file_index` points into `AudioSessionSpec.audio_files`
- `startpos` is the absolute timeline position in samples
- `sampleoffset` is the trim-in inside the source WAV
- `length` is the clip duration in samples
- `source_start` is the third value in the PT region three-point payload; it
  should usually be left unset so the writer can preserve a donor-safe origin

### `AudioTrackSpec`

Represents one audio track.

Fields:

- `name: str = "Audio 1"`
- `clips: Sequence[AudioClipSpec] = ()`
- `channels: int = 1`

`channels` must currently be `1` or `2`.

### `AudioSessionSpec`

Fields:

- `audio_files: Sequence[AudioFileSpec]`
- `tracks: Sequence[AudioTrackSpec]`
- `tempo_events: Sequence[TempoEvent] = ()`
- `meter_events: Sequence[MeterEvent] = ()`
- `preserve_name_widths: bool = False`

Important flag:

- `preserve_name_widths=True` pads shorter names back into the donor slot width
  instead of changing the string width. This is currently the safest mode for
  larger stereo milestone sessions.

## 3. Current Writer Pipeline

The high-level flow in `write_template_session()` is:

1. load and unxor the template
2. optionally copy complete top-level blocks from `block_sources`
3. if audio was requested, call `with_audio_tracks()`
4. if MIDI was requested, call `with_midi_tracks()`
5. if tempo was requested, call `with_tempo_events()`
6. if meter was requested, call `with_meter_events()`
7. xor/encrypt the final bytes back into PTX form
8. run audit checks by default

The audio-specific path in `with_audio_tracks()` is:

1. `_ensure_audio_track_scaffold(data, tracks)`
2. choose `seed_data` from `audio_template` or the current session
3. `_resolved_audio_clips(audio_files, tracks, seed_data)`
4. rebuild:
   - `0x1004`
   - `0x262a`
   - `0x1054`
5. optionally rebuild:
   - `0x1015`
   - `0x2107`
   - `0x2519`
6. replace those top-level blocks
7. patch final-index offsets

The writer is intentionally template-assisted. It does not claim to fully
understand PT12 session semantics outside the decoded block families.

## 4. The Minimal Audio Block Set

The first successful mono and stereo clip-writing controls proved that, once a
matching empty audio scaffold already exists, the smallest successful clip
write only needs:

- `0x1004` for file metadata and file-list state
- `0x262a` for region-library entries
- `0x1054` for active lane placements

This was validated by splicing:

- mono:
  - `one audio track no clips.ptx`
  - `one audio clip bar 1.ptx`
  - `one audio clip bar 2.ptx`
- stereo:
  - `one stereo audio track no clips.ptx`
  - `one stereo audio clip bar1.ptx`

Key conclusion:

- if the scaffold already has the right mono/stereo track and lane structure,
  adding clips does not necessarily require rewriting `0x2624`, `0x2587`, or
  every other sidecar for the simple no-fade case

## 5. Top-Level Blocks That Matter

### `0x1004` - audio file table

Purpose:

- session audio-file table
- contains one `0x1003` child per referenced audio file
- contains the `0x103a` file list used by link/path resolution

Known children:

- `0x1003` - one metadata record per audio file
- `0x1001` - child inside `0x1003` containing frame count
- `0x103a` - file/path list
- `0x2106` - nested child inside `0x1003` carrying some identity/timestamp
  metadata

Known fields:

- top-level payload bytes `2:6` = audio-file count
- inside each `0x1003`, payload bytes `2:6` = one-based file index
- the `0x1001` child inside each `0x1003` is the observed file-length field

Current writer behavior:

- updates the top-level file count
- rewrites the `0x103a` file list
- rewrites `0x1001` frame counts
- patches identity fields when `source_path` is set
- clones extra `0x1003` records when more files are needed than the donor
  template already contains

### `0x262a` - audio region library

Purpose:

- library of audio region definitions

Known shape:

- one `0x2629` child per region
- each `0x2629` contains a `0x2628` region-info child
- region tail after `0x2628` contains file/channel references

Known region semantics:

- region display name
- source file index
- source sample offset
- region length
- source-origin / source-start-ish value
- for stereo, per-channel references

### `0x1054` - active audio lanes and placements

Purpose:

- visible audio placements on the timeline

Known shape:

- `0x1054`
- `0x1052` per lane
- `0x1050` per placement group
- `0x104f` per placement record

Known semantics:

- lane/track display name
- number of placements on the lane
- region index
- timeline start

### `0x1015`, `0x2107`, `0x2519` - track naming sidecars

Purpose:

- redundant track metadata and visible display-name state

When they matter:

- if we only add clips into an existing scaffold without renaming tracks, these
  can often remain untouched
- if we rename audio tracks, these must be updated together with `0x1054`

### `0x2624`, `0x2587` - large audio sidecars

Purpose:

- still only partially understood
- definitely coupled to larger session state, playlist/cache state, or both

Practical rule:

- for simple clip insertion into an already compatible scaffold, these can
  sometimes stay untouched
- for track-count synthesis, large-session naming changes, mixed-type reorder,
  and some stereo milestone sessions, they become critical

### `0x0002` - final index

Purpose:

- master absolute-offset index for the session

Practical rule:

- any body rewrite can shift offsets
- the final index must be repaired
- some families, especially large `0x2624` record sets, are sensitive to
  over-eager bytewise patching

## 6. Audio File Metadata (`0x1004`, `0x1003`, `0x103a`, `0x2106`)

### 6.1 What must be coherent

For Pro Tools to resolve audio files without relink prompts, the following need
to agree:

- `0x1004` audio count
- `0x1003` one-based indexes
- `0x1001` lengths
- `0x103a` file-list counts and suffix chains
- sidecar UMID bytes
- nested BWF UMID bytes
- private file IDs
- PTX-side timestamp fields
- on-disk file modified time

### 6.2 `0x103a` file-list rules

The writer now rebuilds the file list instead of naively cloning it.

Known rules:

- payload `2:6` = `entry_count + 1`
- payload `7:11` = `entry_count`
- each audio filename entry carries a 9-byte suffix
- non-final audio entries use suffix tail `02 00 00 00 00`
- final audio entry uses suffix tail `00 ff ff ff ff`
- entries after the audio files preserve their type bytes but their stored
  entry index must shift to the new entry position

In test form, the audio suffix chain should look like:

- `EVAW 02 00000000` for all but the last audio entry
- `EVAW 00 ffffffff` for the last audio entry

This repair is what made the v9/v10 arbitrary-stem file-table path open
successfully.

### 6.3 Private file IDs

The writer creates deterministic synthetic 16-byte UUIDv4-shaped private IDs.

Implementation:

- `_synthetic_audio_file_private_id(audio_file, audio_file_index)`

Seed material:

- file index
- filename
- length
- channel count
- `source_path`

Observed result:

- arbitrary v4-shaped private IDs are accepted by Pro Tools when the
  surrounding metadata is coherent

### 6.4 Sidecar UMID and nested BWF UMID

The writer reads WAV identity with:

- `_audio_file_identity(path)`

It expects:

- RIFF/WAVE
- BWF `bext` chunk
- UMID bytes in the `bext` chunk

If a separate `umid` chunk exists and is at least 16 bytes:

- the first 16 bytes are used as the sidecar UMID prefix

If no sidecar `umid` chunk exists:

- the writer synthesizes the observed Pro Tools sidecar prefix shape from the
  BWF UMID material-package bytes

Identity bytes patched into `0x1003`:

- content `30:46` = sidecar UMID prefix
- nested BWF UMID bytes are patched at the located BWF marker offset

Important nuance from the controls:

- copying the first 24 bytes of the nested BWF identity field is always safe
- copying bytes `24:35` is only safe for WAVs that match the observed Pro
  Tools UMID tail pattern

The implementation reflects that:

- `_looks_pro_tools_bext_umid_tail()`
- `_patch_audio_file_identity()`

### 6.5 Timestamp fields

The crucial timestamp finding was that Pro Tools cares about timestamp
coherence, not just path strings.

`0x2106` timestamp mapping:

- child-relative `31:39` = WAV mtime as Windows FILETIME
- child-relative `102:107` = first five bytes of the BWF `bext` time reference
- child-relative `107:115` = BWF origination date/time as Windows FILETIME

Current rule:

- the canonical PTX timestamp is the BWF origination time, not the current
  filesystem mtime
- the copied file on disk must also have that same modified time

That is exactly what `copy_audio_file_for_session()` does.

### 6.6 Recommended WAV-staging path

Use:

```python
from ptxformatwriter import copy_audio_file_for_session

staged = copy_audio_file_for_session(
    "/path/to/source.wav",
    "Audio Files",
    "Stem 1.wav",
)
```

This:

- copies the file into the session `Audio Files` folder
- preserves the BWF origination time as filesystem mtime
- gives the writer a path whose on-disk timestamp matches the PTX metadata it
  will write

## 7. Region Library (`0x262a -> 0x2629 -> 0x2628`)

### 7.1 Region info layout

The region-info payload uses:

1. content type bytes
2. a 4-byte name length
3. the name bytes
4. a "three-point" encoded payload
5. a tail copied from the donor

The writer uses:

- `_encode_audio_region_points(sampleoffset, length, source_start)`

The three-point header bytes are:

- byte `0` = `0x00`
- byte `1` high nibble = sampleoffset byte count
- byte `2` high nibble = length byte count
- byte `3` high nibble = source_start byte count
- byte `3` low nibble = source_start byte count again
- byte `4` = `0x08`

Then follow, in order:

- sampleoffset as little-endian varint
- length as little-endian varint
- source_start as little-endian varint

Helper functions:

- `_varint_len()`
- `_three_point_end()`
- `_three_point_start_len()`

### 7.2 Known low-level offsets from the control files

For the simple mono controls:

- region name length field in `0x262a` was observed at local `0x0018`
- region name bytes in `0x262a` at local `0x001c`
- three-point header immediately after the name

For the bar-1 / trimmed controls:

- source offset and length values moved exactly as expected with the trim
- this proved that `0x262a` owns trim-in and region-length semantics

### 7.3 Region tail references

This was a major stereo finding.

The region tail after `0x2628` contains more than the parser-visible file
index. The writer must patch:

- tail offset `+0` = parser-visible file index
- tail offset `+4` = channel index
- tail offset `-8` = duplicate file index near the end of the tail

If only the visible file index is patched:

- Pro Tools can resolve all generated clips to the first donor file instead of
  the intended distinct files

That exact bug was fixed in the `v11` region-tail update and is now covered by
`test_audio_region_tail_references_follow_file_and_channel`.

### 7.4 `source_start`

The writer resolves source origin with `_audio_file_origins(seed_data)`.

Observed safe rule:

- `source_start` should usually be left unset
- the writer then uses:
  - donor origin for that file index, if available
  - otherwise the first donor origin it finds
  - otherwise `0`

The derived formula is:

- `source_start = origin + sampleoffset`

This donor-preserving behavior is important. For generated sessions, forcing
`source_start=0` caused failures where preserving the donor-style origin
opened successfully.

## 8. Active Audio Placements (`0x1054 -> 0x1052 -> 0x1050 -> 0x104f`)

### 8.1 Placement path

The active placement path is:

- `0x1054` top-level map
- `0x1052` one child per active lane
- `0x1050` one placement group per clip-on-lane
- `0x104f` one placement entry

### 8.2 Known placement fields

From the simple mono controls:

- `0x104f` payload offset `+4` = region index
- `0x104f` payload offset `+8` = one unknown byte
- `0x104f` payload offset `+9` = timeline start as 4-byte little-endian

Control observations:

- moving a clip from bar 1 to bar 2 only changed the placement-start bytes in
  `0x1054`
- trimming changed:
  - source offset and length in `0x262a`
  - placement start in `0x1054`

This clean separation is one of the most stable findings in the audio writer.

### 8.3 Writer behavior

The writer derives a placement template with:

- `_audio_placement_template(seed_data)`

And emits placements with:

- `_audio_placement_from_template(template, clip)`

It rewrites:

- region index
- timeline start

Lane content is assembled in:

- `_audio_active_content(seed_data, tracks, clips, ...)`

## 9. Mono Semantics

Mono track rules currently understood:

- one mono track has one `0x1014` metadata record with `channels=1`
- one mono track has one `0x1052` active lane
- one mono clip creates one region in `0x262a`
- one mono clip creates one active placement in `0x1054`

Multi-track mono observations:

- a two-mono-track empty scaffold already contains:
  - two `0x1014` entries
  - two empty `0x1052` lanes
  - two `0x2624` playlist children
  - larger `0x2587`
  - larger final index
- once that scaffold exists, adding a clip only on the second track was
  validated by changing only:
  - `0x1004`
  - `0x262a`
  - `0x1054`

This is the basis for the current mono multi-track writer path.

## 10. Stereo Semantics

The most important stereo rule:

- a stereo track is not modeled as two separate mono tracks

Instead:

- `0x1015` contains one `0x1014` record whose channel count is `2`
- `0x1054` contains two `0x1052` lanes, both named after the same track
- `0x2624` contains one playlist child for the stereo track

When one stereo clip is added:

- the file itself is one 2-channel WAV
- the region library gets two entries:
  - `.L`
  - `.R`
- both region entries point at the same file index
- both share the same sampleoffset and length
- the region-tail channel indexes differ:
  - lane/channel `0` for `.L`
  - lane/channel `1` for `.R`
- the active map gets two placements, one per lane

The writer models this by exploding a logical stereo clip into two resolved
channel clips.

Implementation details:

- `_channel_suffix()` adds `.L` / `.R`
- `_resolved_audio_clips()` creates one `_AudioClipResolved` per channel
- `_AudioClipResolved` carries:
  - `file_index`
  - `channel_index`
  - `sampleoffset`
  - `length`
  - `source_start`

This is why a single `AudioClipSpec(name="Print A", channels=2 track)` becomes:

- region `Print A.L`
- region `Print A.R`
- lane placement on stereo lane 0
- lane placement on stereo lane 1

## 11. Name Handling

### 11.1 Track names

Track names are duplicated across:

- `0x1015`
- `0x1054`
- `0x2107`
- `0x2519`

The current writer performs general length-prefixed string replacement in those
blocks:

- `_audio_track_metadata_replacements()`
- `_rebuild_block_with_string_replacements()`

### 11.2 Clip names

Clip names live primarily in `0x262a`.

### 11.3 Width preservation

This is still one of the practical guardrails.

The writer supports fixed-width padding helpers:

- `_fixed_width_latin1_string()`
- `_fixed_width_audio_clip_string()`

The audio clip helper preserves a `.L` / `.R` suffix at the end of the fixed
slot.

Example from tests:

- `"S01 Clip.L"` in width `14` becomes padded base text plus literal `.L`

Current production rule:

- for large stereo sessions, especially the 7-stem milestone path, use
  `preserve_name_widths=True`

Reason:

- true shorter-name rewrites still interact badly with larger-session sidecars,
  especially `0x2624`

## 12. Arbitrary Lengths and Trims

What we know:

- no hidden second file-length field has been confirmed in `0x1003`
- the only observed file length field is the `0x1001` child value
- clip length also lives in the region three-point payload inside `0x2628`

That means arbitrary lengths are handled by:

- patching `0x1001` in `0x1003`
- patching the region three-point length

Current writer behavior:

- `AudioFileSpec.length` is optional when `source_path` is set
- `AudioClipSpec.length` is optional
- if omitted, clip length becomes `source_length - sampleoffset`

Validated recipe:

1. stage the WAV with `copy_audio_file_for_session()`
2. set `AudioFileSpec(source_path=...)`
3. omit `AudioFileSpec.length` unless overriding is intentional
4. omit `AudioClipSpec.length` for a full-file clip
5. set `AudioClipSpec.length` for trims
6. leave `AudioClipSpec.source_start` unset unless a control file proves the
   replacement value is safe

## 13. Scaffold Requirements

The writer does not yet synthesize a full audio session from zero.

It requires the destination body to already have compatible audio scaffolding:

- requested `AudioTrackSpec.channels` must match `0x1015`
- requested total lane count must match `0x1054`

This is enforced by:

- `_ensure_audio_track_scaffold()`

If the scaffold does not match, the writer raises:

- `NotImplementedError("structured audio writing needs a template or block_sources with matching audio track/channel scaffold")`

What counts as a valid scaffold:

- mono same-track
- mono multi-track
- stereo
- larger stereo donor scaffolds such as 4-track, 7-track, 16-track, or
  512-track controls, once the body and final index are already compatible

## 14. Final Index and Track-Count Growth

If clips are added without changing the scaffold shape much, the legacy final
index patching in `writer.py` can be enough.

For track-count growth and more structural sessions, `ptxformatwriter/final_index.py`
matters.

Important functions:

- `parse_records()`
- `serialize_final_block()`
- `add_track()`
- `add_stereo_track()`
- `synthesize_index_records()`
- `rebuild_index_offsets()`
- `compose_index()`

What is known:

- adding one stereo track changes the final index in a regular structural way
- `0x1015`, `0x1054`, `0x2519`, and `0x2624` each gain references
- `0x1054` grows by the number of track channels
- `0x2519` and `0x2624` get new per-track instances

Validated large-session result:

- the 512-track stereo scaffold path now opens successfully when non-playlist
  final-index references are patched conservatively

Practical warning:

- large `0x2624` record families in the final index are dangerous to patch by
  broad bytewise scanning

## 15. Audit Rules Relevant to Audio

The writer audits generated PTX files by default.

Relevant checks in `ptxformatwriter/audit.py`:

- `0x1004` audio count matches `0x1003` metadata count
- `0x1003` indexes are sequential
- private file IDs are not duplicated
- `0x103a` header counts are correct
- `0x103a` audio-entry suffix tails are correct
- shifted path-entry indexes are correct

This catches the exact broken-file-list class that caused the older cloned
stereo metadata failures.

Useful commands:

```sh
python3 -m ptxformatwriter audit --strict file.ptx
python3 -m ptxformatwriter audit --strict --strict-final-index-refs file.ptx
```

## 16. Code Map

### Writer internals

Public/audio entry points:

- `write_audio_session()`
- `write_template_session()`
- `with_audio_tracks()`

Clip resolution:

- `_resolved_audio_clips()`
- `_audio_file_origins()`
- `_audio_track_channels()`
- `_audio_lane_names()`

File table:

- `_audio_files_content()`
- `_audio_file_metadata_content()`
- `_audio_file_list_content()`
- `_patch_audio_file_identity()`
- `_synthetic_audio_file_private_id()`

Region library:

- `_audio_region_templates()`
- `_audio_region_info_content()`
- `_audio_regions_content()`
- `_encode_audio_region_points()`

Active placements:

- `_audio_placement_template()`
- `_audio_placement_from_template()`
- `_audio_active_content()`

Track metadata / naming:

- `_audio_track_layout_from_metadata()`
- `_audio_track_metadata_replacements()`
- `_fixed_width_latin1_string()`
- `_fixed_width_audio_clip_string()`

Final index:

- `_replace_top_level_blocks()`
- `_update_final_index()`
- `ptxformatwriter/final_index.py`

Audit:

- `_raise_for_audit_issues()`
- `ptxformatwriter/audit.py`

### Tests worth reading

Most useful audio tests in `tests/test_writer.py`:

- `test_audio_region_tail_references_follow_file_and_channel`
- `test_write_audio_session_round_trips_same_track_files_regions_and_placements`
- `test_write_audio_session_round_trips_two_mono_tracks_with_scaffold`
- `test_write_audio_session_round_trips_stereo_track_with_scaffold`
- `test_write_template_session_round_trips_audio_midi_and_maps`
- `test_write_audio_session_clones_missing_file_metadata_records`
- `test_write_audio_session_round_trips_two_stereo_files_with_real_metadata`
- `test_write_audio_session_patches_wav_identity_from_source_path`
- `test_copy_audio_file_for_session_sets_bwf_origination_mtime`
- `test_write_audio_session_infers_source_file_and_clip_length`
- `test_write_audio_session_round_trips_four_stereo_files_with_real_metadata`

And in `tests/test_audit.py`:

- the cloned-file-list/audio-link failure checks

## 17. Example Usage

### Mono

```python
from ptxformatwriter import (
    AudioClipSpec,
    AudioFileSpec,
    AudioSessionSpec,
    AudioTrackSpec,
    write_audio_session,
)

session = AudioSessionSpec(
    audio_files=[
        AudioFileSpec(filename="Audio 1-SigGen_01.wav", length=264600, channels=1),
    ],
    tracks=[
        AudioTrackSpec(
            name="DX Left",
            channels=1,
            clips=[
                AudioClipSpec(
                    name="Line A",
                    file_index=0,
                    startpos=0,
                    sampleoffset=88200,
                    length=88200,
                ),
            ],
        ),
    ],
)

write_audio_session(
    "one-audio-track-template.ptx",
    "out.ptx",
    session,
    audio_template="one-audio-clip-template.ptx",
)
```

### Stereo with real WAV metadata

```python
from ptxformatwriter import (
    AudioClipSpec,
    AudioFileSpec,
    AudioSessionSpec,
    AudioTrackSpec,
    copy_audio_file_for_session,
    write_audio_session,
)

stem = copy_audio_file_for_session(
    "/path/to/Stem 1.wav",
    "Audio Files",
    "Stem 1.wav",
)

session = AudioSessionSpec(
    audio_files=[
        AudioFileSpec(
            filename=stem.name,
            channels=2,
            source_path=stem,
        ),
    ],
    tracks=[
        AudioTrackSpec(
            name="Stereo Print",
            channels=2,
            clips=[
                AudioClipSpec(
                    name="Print A",
                    file_index=0,
                    startpos=0,
                    sampleoffset=0,
                ),
            ],
        ),
    ],
)

write_audio_session(
    "one-stereo-audio-track-no-clips.ptx",
    "out.ptx",
    session,
    audio_template="one-stereo-audio-clip-bar1.ptx",
)
```

### Stereo with safe shorter names

```python
write_audio_session(
    template,
    output,
    AudioSessionSpec(
        audio_files=audio_files,
        tracks=tracks,
        preserve_name_widths=True,
    ),
    audio_template=audio_template,
)
```

## 18. Control Files and Proof Sessions

Useful real controls:

- `/Users/jonkubis/Music/temp/PT/lots of wave files/lots of wave files.ptx`
- `/Users/jonkubis/Music/temp/PT/lots of stereo tracks/lots of stereo tracks.ptx`
- `/Users/jonkubis/Music/temp/PT/names`
- `/Users/jonkubis/Music/temp/PT/multiple track types`

Important generated proofs:

- `generated/synth_audio_minimal_splice_bar1_v1.ptx`
- `generated/synth_audio_clip_renamed_262a_only_v1.ptx`
- `generated/synth_audio_trimmed_262a_1054_v1.ptx`
- `generated/synth_audio_second_track_clip_minimal_v1.ptx`
- `generated/synth_audio_stereo_clip_minimal_v1.ptx`
- `generated/synth_audio_structured_two_files_v1.ptx`
- `generated/synth_audio_four_stereo_arbitrary_wavs_cloned_filelist_v10.ptx`
- `generated/synth_audio_random_disk_sample_coherent_track1_v17.ptx`
- `generated/synth_audio_512_first7_index_non_playlist_refs_v6.ptx`
- `generated/synth_audio_512_first7_arbitrary_lengths_public_writer_v13.ptx`
- `generated/synth_audio_512_first7_arbitrary_lengths_imported_tempo_public_writer_v16.ptx`
- `generated/milestone_7_stereo_stems_imported_tempo_preserve_names_v2.ptx`

## 19. Practical Safe Recipe Today

If the goal is to build a real PT12 session with mono/stereo clips and have it
open reliably in Pro Tools today:

1. start from a known-good PT12 template
2. make sure the destination scaffold already has the right mono/stereo track
   layout
3. if needed, copy in a known-good empty scaffold first with `block_sources`
4. stage WAVs with `copy_audio_file_for_session()`
5. use `AudioFileSpec(source_path=...)`
6. keep `AudioClipSpec.source_start` unset unless you have a proven reason not
   to
7. use `preserve_name_widths=True` for large sessions or shorter names
8. leave `validate_output=True`
9. if Pro Tools asks to relink, inspect:
   - timestamps
   - `0x103a`
   - private IDs
   - file/channel tail refs in `0x262a`

## 20. Known Limitations

Still not fully solved:

- true from-zero synthesis of arbitrary audio track-count scaffolds
- fully decoded `0x2624` / `0x2587` large-session semantics
- robust mixed track-type reorder
- unpadded shorter name rewrites in large milestone sessions
- multichannel audio beyond stereo
- fades
- clip gain
- elastic audio
- grouped/compound regions
- non-PT12 session variants

Current hard boundaries in code:

- audio channels must be `1` or `2`
- file channel count must match destination track channel count
- the destination scaffold must already match the requested audio layout
- the writer still depends on at least one compatible audio metadata donor
  record for some stereo file-table paths

## 21. Bottom Line

The mono/stereo audio writer is now real and practically useful.

The core decoded model is:

- `0x1004` owns file identity and file-list/link state
- `0x262a` owns clip source range and file/channel mapping
- `0x1054` owns visible lane placements
- `0x1015` / `0x2107` / `0x2519` own redundant track-name metadata
- `0x2624` / `0x2587` are the still-dangerous large sidecars
- `0x0002` must be repaired whenever block offsets move

For mono and stereo clip insertion into a compatible scaffold, we know enough
to write useful Pro Tools sessions today. The remaining frontier is no longer
"how do clips work?" It is mostly large-session sidecar semantics, track-count
scaffold synthesis, and mixed-track ordering.
