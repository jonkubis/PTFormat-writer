# Audio Writing Reverse-Engineering Notes

This is the read-side map inherited from the original C++ `ptxformatwriter` repo,
plus the control-file plan for turning that knowledge into PT12 audio writing.

The original project did not write Pro Tools sessions, but it did parse audio
files, audio regions, audio track names, and audio region placements. Those
parser paths are now ported in `ptxformatwriter/core.py` and should be the first source
of truth when designing audio writer control files.

## Original C++ Knowledge Already Ported

The relevant C++ functions were:

- `parseaudio()`
- `parserest()`
- `parse_region_info()`
- `parse_three_point()`

The Python equivalents are in `ptxformatwriter/core.py`:

- `PTFFormat.parseaudio()`
- `PTFFormat.parserest()`
- `PTFFormat.parse_region_info()`
- `PTFFormat.parse_three_point()`

## Audio File References

Top-level block:

- `0x1004` - WAV list full

Important children:

- `0x103a` - file/path/name list
- `0x1003` - WAV metadata group
- `0x1001` - WAV samplerate/size metadata

Current parser behavior:

- Reads total WAV-ish count from `0x1004` at content offset `+2`, 4 bytes.
- Scans `0x103a` from child content offset `+11`.
- Each filename is a 4-byte little-endian length followed by Latin-1 bytes.
- After each filename, the parser reads 4 bytes of file type, then skips 9 bytes.
- Ignores `.grp`, `Audio Files`, and `Fade Files` entries.
- For PT10+:
  - accepts entries whose type contains `WAVE`, `EVAW`, `AIFF`, or `FFIA`
  - also accepts zero-type entries if the name contains `.wav` or `.aif`
- File length comes from each `0x1001` child at content offset `+8`, 8 bytes.

Writer implications:

- First audio writer probes should keep one simple mono WAV at the session sample
  rate to avoid resampling and channel-layout questions.
- Diff `0x1004`, especially `0x103a`, `0x1003`, and `0x1001`, between no-audio,
  imported-audio, and placed-audio sessions.

## Audio Region Library

PT5-era blocks:

- `0x100b` - audio region list
- `0x1008` - audio region name/number

PT10+/PT12 blocks:

- `0x262a` - audio region list
- `0x2629` - audio region name/number

Current parser behavior:

- For each `0x1008` or `0x2629`, reads the region name at child content offset
  `+11`.
- Immediately after the name, calls `parse_region_info()`.
- `parse_region_info()` calls `parse_three_point()` and interprets the three
  values as:
  - `start`: absolute/source-ish position
  - `sampleoffset`: offset into the source audio file
  - `length`: region length
- The source WAV index is read from the first region-info child at
  `child.offset + child.block_size`, 4 bytes.
- The parser stores:
  - `Region.name`
  - `Region.index`
  - `Region.sampleoffset`
  - `Region.length`
  - `Region.wave.index`
  - `Region.wave.filename`

Units:

- Audio positions and lengths are sample-based in the parsed API.
- Values are scaled by `target_rate / session_rate`.
- For writer reverse-engineering, use sessions where target rate equals session
  rate when possible, so raw decoded values match visible sample positions.

Writer implications:

- The first audio control files should vary only one axis at a time:
  - clip name
  - clip start on timeline
  - source offset
  - clip length
  - source WAV index

## Audio Track Names

Top-level block:

- `0x1015` - audio tracks

Child block:

- `0x1014` - audio track name/number

Current parser behavior:

- Reads track name from each `0x1014` at content offset `+2`.
- After name, skips 5 bytes.
- Reads channel count, 4 bytes.
- Reads channel map entries, 2 bytes each, up to `MAX_CHANNELS_PER_TRACK`.
- Creates a dummy `Track` for each mapped channel until placements attach
  real regions.

Writer implications:

- Track-name writing will probably require changing `0x1015/0x1014` and one or
  more PT12 metadata sidecars, similar to the MIDI track-name pass.
- First controls should use mono audio tracks before stereo or multi-channel.

## Audio Region Placement Maps

Older map:

- `0x1012` - audio region-to-track full map
- `0x1011` - audio region-to-track map entries
- `0x100f` - audio region-to-track entry
- `0x100e` - audio region-to-track subentry

PT8+/PT12 map:

- `0x1054` - audio region-to-track full map
- `0x1052` - audio region-to-track map entries
- `0x1050` - audio region-to-track entry
- `0x104f` - audio region-to-track subentry

Current parser behavior for `0x1054`:

- Iterates one `0x1052` per track.
- Iterates `0x1050` placement groups.
- Skips fades when byte `0x1050.content_offset + 46` is `0x01`.
- Reads region index from each `0x104f` at content offset `+4`, 4 bytes.
- Reads timeline start after one unknown byte, at content offset `+9`, 4 bytes.
- Finds the audio track by the `0x1052` order and the region by raw region index.
- Assigns `track.reg.startpos = scaled(start)`.

Writer implications:

- `0x1054 -> 0x1052 -> 0x1050 -> 0x104f` is the first active-arrangement path
  to target for PT12 audio clips.
- Keep fade-free sessions for initial controls.
- Confirm whether the `0x104f` start field remains 4 bytes for all PT12 audio
  positions we care about, and whether larger positions require a different
  field or sidecar update.

## Known Parser Surface For Validation

After loading a file, the parser exposes:

- `session.audiofiles()`
- `session.regions()`
- `session.tracks()`

The CLI prints:

- audio file names and lengths
- region name, WAV index, source offset, and length
- track name, region index, and absolute timeline position
- combined track/filename/start/source-offset/length summary

This is enough to validate early generated audio sessions locally before asking
Pro Tools to open them.

## First Audio Control Batch

Keep all sessions PT12, same sample rate, mono, no fades, no clip gain, no
elastic audio, no groups, no stereo, no playlists.

Request 2-3 files at a time:

1. `one audio track no clips.ptx`
2. `one audio clip bar1.ptx`
3. `one audio clip bar2.ptx`

Use the same short mono WAV in the session `Audio Files` folder for both clip
sessions.

### Batch 1 Findings

Control files received in `generated/`:

- `one audio track no clips.ptx`
- `one audio clip bar 1.ptx`
- `one audio clip bar 2.ptx`

All three are PT12 sessions at 44100 Hz with default 120 bpm, 4/4.

Decoded through `PTFFormat.load(path, 44100)`:

- Empty-track control:
  - 0 WAVs
  - 0 regions
  - 0 active audio placements
  - audio track metadata still names the track `Audio 1`
- Bar-1 and bar-2 controls:
  - one WAV: `Audio 1-SigGen_01.wav`
  - WAV length: 264600 samples
  - one region: `Audio 1-SigGen_01-01`
  - region sample offset: 88200 samples
  - region length: 88200 samples
  - track name: `Audio 1`
  - bar 1 placement start: 0 samples
  - bar 2 placement start: 88200 samples

Top-level block comparison:

- `0x1004` is identical between bar 1 and bar 2. It carries the audio file
  reference and WAV metadata, not timeline placement.
- `0x1015` is identical between bar 1 and bar 2. It carries the audio track
  metadata for this mono `Audio 1` track.
- `0x2519` is identical between bar 1 and bar 2. It also contains `Audio 1`
  display metadata in these PT12 controls.
- `0x262a` is semantically identical between bar 1 and bar 2. Only four bytes
  near the tail differ, likely volatile identity/timestamp metadata. The region
  name, source offset, length, and source WAV index stay unchanged.
- `0x1054` is the active timeline placement map. Bar 1 vs bar 2 differs by
  exactly the three low bytes of the placement start:
  - local top-level payload offset `0x0037`
  - bar 1: `00 00 00 00`
  - bar 2: `88 58 01 00` (88200 little-endian)

The PT12 active audio placement path is:

- `0x1054` top-level map
- `0x1052` per-track entry
- `0x1050` placement group
- `0x104f` placement entry

In these controls, the `0x104f` payload contains:

- raw region index at payload offset `+4` as 4-byte little-endian
- one unknown byte at payload offset `+8`
- timeline start at payload offset `+9` as 4-byte little-endian

The `0x262a -> 0x2629 -> 0x2628` region entry uses the same three-point
encoding as the parser:

- region name starts at `0x2629` payload offset `+11`
- three-point data starts immediately after the length-prefixed name
- sample offset and length are encoded as 3-byte little-endian values for
  88200 samples in this fixture
- source/start-ish value is encoded as a 4-byte little-endian value and is
  stable between bar 1 and bar 2

Generated probes:

- `synth_audio_minimal_splice_bar1_v1.ptx`
- `synth_audio_minimal_splice_bar2_v1.ptx`

Both were built from `one audio track no clips.ptx` by replacing only these
top-level blocks from the corresponding clip control:

- `0x1004`
- `0x262a`
- `0x1054`

Both parse back identically to their source clip control through the Python
reader. If Pro Tools opens them, those three blocks are sufficient for the
first minimal audio writer path.

Pro Tools validation:

- both minimal splice probes open/load cleanly
- conclusion: for this simplest mono/no-fades case, replacing only `0x1004`,
  `0x262a`, and `0x1054` is enough for Pro Tools to accept the generated PTX

Second batch:

1. `one audio clip renamed.ptx`
2. `one audio track renamed.ptx`
3. `one audio clip trimmed.ptx`

### Batch 2 Findings

Control files received in `generated/`:

- `one audio clip renamed.ptx`
- `one audio track renamed.ptx`
- `one audio clip trimmed.ptx`

Decoded through `PTFFormat.load(path, 44100)`:

- Clip rename:
  - region name changes from `Audio 1-SigGen_01-01` to `renamed clip`
  - WAV reference, source offset, length, track name, and placement start stay
    unchanged
- Track rename:
  - track name changes from `Audio 1` to `Track Renamed`
  - WAV reference, region name, source offset, length, and placement start stay
    unchanged
- Trim:
  - region source offset changes from 88200 to 132300 samples
  - region length changes from 88200 to 44100 samples
  - placement start changes from 0 to 44100 samples
  - WAV reference, region name, and track name stay unchanged

Clip rename block behavior:

- `0x262a` changes size from 303 to 295 because the region name is 8 bytes
  shorter.
- Replacing only top-level `0x262a` from the renamed-clip control into the
  validated bar-1 session parses back with the renamed clip.
- Candidate probe:
  - `synth_audio_clip_renamed_262a_only_v1.ptx`
- Pro Tools validation:
  - loads cleanly

Trim block behavior:

- `0x262a` changes the region three-point payload.
- `0x1054` changes only the active placement start.
- Replacing top-level `0x262a` and `0x1054` from the trimmed control into the
  validated bar-1 session parses back with the trimmed source range and
  placement.
- Candidate probe:
  - `synth_audio_trimmed_262a_1054_v1.ptx`
- Pro Tools validation:
  - loads cleanly

Track rename block behavior:

- `0x1015` changes the main audio track metadata.
- `0x1054` also stores the track name in the active placement map's `0x1052`
  entry.
- `0x2107` stores another track-name metadata copy.
- `0x2519` stores three copies of the track display name in these controls.
- `0x2624` still contains `Audio 1` after the control rename, so it is probably
  not required for the first track-name writer path.
- Replacing top-level `0x1015`, `0x1054`, `0x2107`, and `0x2519` from the
  renamed-track control into the validated bar-1 session parses back with
  `Track Renamed`.
- Candidate probe:
  - `synth_audio_track_renamed_core_v1.ptx`
- Pro Tools validation:
  - loads cleanly

Region three-point details from `0x262a -> 0x2629`:

- region name length field local to top-level `0x262a`: `0x0018`
- region name bytes local to top-level `0x262a`: `0x001c`
- three-point header starts immediately after the name:
  - bar 1 local `0x0030`: `00 30 30 44 08`
  - renamed clip local `0x0028`: `00 30 30 44 08`
  - trimmed local `0x0030`: `00 30 20 44 08`
- for little-endian PT12 files, byte-count nibbles are:
  - source offset byte count: high nibble of header byte `+1`
  - length byte count: high nibble of header byte `+2`
  - source/start-ish byte count: high nibble of header byte `+3`
- bar 1:
  - source offset local `0x0035`, 3 bytes: 88200
  - length local `0x0038`, 3 bytes: 88200
  - source/start-ish local `0x003b`, 4 bytes: 158848200
- trimmed:
  - source offset local `0x0035`, 3 bytes: 132300
  - length local `0x0038`, 2 bytes: 44100
  - source/start-ish local `0x003a`, 4 bytes: 158892300

Active placement details from `0x1054 -> 0x1052 -> 0x1050 -> 0x104f`:

- `0x104f` payload starts local to top-level `0x1054` at `0x002e`
- region index is local `0x0032`, 4 bytes
- placement start is local `0x0037`, 4 bytes
- bar 1 placement start: 0
- bar 2 placement start: 88200
- trimmed placement start: 44100

Third batch:

1. `two audio clips same track.ptx`
2. `two audio tracks one clip each.ptx`

### Batch 3 Findings

Control files received in `generated/`:

- `two audio clips same track.ptx`
- `two audio tracks one clip each.ptx`

Decoded through `PTFFormat.load(path, 44100)`:

- Two clips on same track:
  - still one WAV: `Audio 1-SigGen_01.wav`
  - still one region-library entry: `Clip 1`
  - two active placements on track `Audio 1`
  - placement starts: 0 and 176400 samples
- Two tracks with one clip each:
  - still one WAV: `Audio 1-SigGen_01.wav`
  - still one region-library entry: `Clip 1`
  - one active placement on `Audio 1` and one on `Audio 2`
  - both placement starts: 0 samples

Two clips on same track:

- `0x262a` changes because the region name is `Clip 1`.
- `0x1054` grows from 101 to 157 bytes.
- The single `0x1052` track entry grows from 75 to 131 bytes and contains two
  `0x1050` placement groups.
- Placement summary local to top-level `0x1054`:
  - track entry `0x1052` local `0x000d`, size 131, name `Audio 1`
  - placement 0: `0x1050` local `0x0025`, `0x104f` local `0x002e`,
    region 0, start 0
  - placement 1: `0x1050` local `0x005d`, `0x104f` local `0x0066`,
    region 0, start 176400
- Candidate probe:
  - `synth_audio_two_clips_same_track_minimal_v1.ptx`
- Probe contents:
  - built from `one audio clip bar 1.ptx`
  - replaced only `0x262a` and `0x1054`
  - parses back correctly

Two tracks with one clip each:

- `0x262a` changes because the region name is `Clip 1`.
- `0x1054` grows from 101 to 183 bytes.
- The active map contains two `0x1052` track entries:
  - track entry 0 local `0x000d`, size 75, name `Audio 1`
  - track entry 1 local `0x005f`, size 75, name `Audio 2`
- Each `0x1052` has one `0x1050 -> 0x104f` placement:
  - track 0 placement: `0x1050` local `0x0025`, `0x104f` local `0x002e`,
    region 0, start 0
  - track 1 placement: `0x1050` local `0x0077`, `0x104f` local `0x0080`,
    region 0, start 0
- `0x1015` grows from 78 to 137 bytes and contains two `0x1014` audio track
  metadata entries.
- `0x2107` grows from 71 to 127 bytes and contains two track-name metadata
  entries.
- `0x2519` grows from 561 to 767 bytes and contains four `0x251a` name entries
  in these controls.
- `0x2624` also grows, but prior track-name validation showed it is not needed
  for a minimal accepted rename path, so the first two-track probe does not copy
  it.
- Candidate probe:
  - `synth_audio_two_tracks_core_v1.ptx`
- Probe contents:
  - built from `one audio clip bar 1.ptx`
  - replaced `0x262a`, `0x1054`, `0x1015`, `0x2107`, and `0x2519`
  - parses back correctly
- Pro Tools validation:
  - fails with "unexpected stream type ... while translating Audio Playlists"
  - conclusion: parser-visible track/placement metadata is not enough for
    multiple audio tracks; Pro Tools also validates playlist sidecar metadata

Two-track playlist sidecar candidates:

- Blocks changed in the two-track control but not copied by
  `synth_audio_two_tracks_core_v1.ptx`:
  - `0x202b`: grows from 240 to 244 bytes
  - `0x2587`: grows from 1556 to 1822 bytes
  - `0x2624`: grows from 1838 to 3670 bytes
- `0x2624` is the leading playlist candidate:
  - one-track control has one top-level `0x261c` child inside `0x2624`
  - two-track control has two top-level `0x261c` children inside `0x2624`
  - the first `0x261c` also gains a `0x200b -> 0x200a` child in the two-track
    control
- Follow-up probes:
  - `synth_audio_two_tracks_plus_2624_v2.ptx`
    - core two-track blocks plus `0x2624`
  - `synth_audio_two_tracks_plus_2624_2587_v2.ptx`
    - core two-track blocks plus `0x2624` and `0x2587`
  - `synth_audio_two_tracks_full_sidecars_v2.ptx`
    - core two-track blocks plus `0x2624`, `0x2587`, and `0x202b`
- All three parse back correctly.
- Pro Tools validation:
  - all three fail with the same "unexpected stream type ... while translating
    Audio Playlists" error

Final index finding:

- The real two-track control's final `0x0002` index block is 6415 bytes.
- The v1/v2 generated two-track probes kept the one-track base final index size
  of 6239 bytes even when their parser-visible two-track state was correct.
- This suggests Audio Playlists translation depends on extra entries in the
  final index, not only the obvious playlist sidecar blocks.
- Follow-up probes:
  - `synth_audio_two_tracks_source_index_v3.ptx`
    - core two-track blocks plus `0x2624`, `0x2587`, `0x202b`, and source-sized
      final `0x0002`, with source offsets remapped into the generated file
    - keeps the generated/base `0x2067` path metadata
  - `synth_audio_two_tracks_source_path_index_v3.ptx`
    - same as above, but also copies source `0x2067` path metadata
    - effectively a near-source positive control, differing from the source
      unxored bytes only at header offset 19
- Both parse back correctly.
- Pro Tools validation:
  - both open successfully
  - conclusion: multiple audio-track playlist translation needs the larger
    source-style final `0x0002` index, remapped into the generated session
- Writer helper update:
  - `with_blocks_from()` now treats `0x0002` specially
  - when `0x0002` is copied from a source session, final-index offsets are
    patched from the source session's block offsets into the generated session,
    instead of from the original base template's offsets
  - `synth_audio_two_tracks_source_index_v4.ptx` was generated through the
    public `write_template_session(..., block_sources=...)` path and is
    byte-identical to `synth_audio_two_tracks_source_index_v3.ptx`

## Batch 4 Findings

Control files received in `generated/`:

- `two distinct audio clips same track.ptx`
- `two audio files one clip each.ptx`

Decoded through `PTFFormat.load(path, 44100)`:

- Two distinct clips on same track:
  - one WAV: `Audio 1-SigGen_03.wav`
  - WAV length: 352800 samples
  - two region-library entries:
    - region 0: `Audio 1-SigGen_03-02`, WAV 0, source offset 88200,
      length 88200
    - region 1: `Audio 1-SigGen_03-03`, WAV 0, source offset 176400,
      length 88200
  - two active placements on `Audio 1`:
    - timeline 0 uses region 1
    - timeline 88200 uses region 0
- Two audio files, one clip each:
  - two WAVs:
    - WAV 0: `Audio 1-SigGen_01.wav`, length 264600
    - WAV 1: `Audio 1-SigGen_02.wav`, length 264600
  - two region-library entries:
    - region 0: `Audio 1-SigGen_02-01`, WAV 1, source offset 88200,
      length 88200
    - region 1: `Clip 1`, WAV 0, source offset 88200, length 88200
  - two active placements on `Audio 1`:
    - timeline 0 uses region 1
    - timeline 176400 uses region 0

Multi-region behavior:

- `0x262a` contains one `0x2629` child per region.
- Region order does not have to match timeline order; active `0x104f` placement
  entries store the region index explicitly.
- In `two distinct audio clips same track.ptx`:
  - `0x262a` grows to 600 bytes
  - child `0x2629` region 0 is local `0x000d`, size 290
  - child `0x2629` region 1 is local `0x0136`, size 290
  - both region entries point at WAV index 0
- In `two audio files one clip each.ptx`:
  - `0x262a` grows to 586 bytes
  - child `0x2629` region 0 is local `0x000d`, size 290, WAV index 1
  - child `0x2629` region 1 is local `0x0136`, size 276, WAV index 0

Multi-WAV behavior:

- `0x1004` contains one `0x1003` metadata child per audio file.
- The `0x1004` count field at payload offset `+2` matches the number of audio
  files.
- In `two audio files one clip each.ptx`:
  - `0x1004` count field is 2
  - `0x1004` grows to 904 bytes
  - two `0x1003` children carry the two WAV metadata records

Same-track multi-region probes:

- `synth_audio_two_distinct_regions_minimal_v1.ptx`
  - built from `one audio clip bar 1.ptx`
  - replaced only `0x1004`, `0x262a`, and `0x1054`
  - parses back correctly
  - Pro Tools validation: opens successfully
- `synth_audio_two_files_minimal_v1.ptx`
  - built from `one audio clip bar 1.ptx`
  - replaced only `0x1004`, `0x262a`, and `0x1054`
  - parses back correctly
  - Pro Tools validation: opens successfully

## Structured Audio Writer

Implemented in `ptxformatwriter/writer.py`:

- `AudioFileSpec`
- `AudioClipSpec`
- `AudioTrackSpec`
- `AudioSessionSpec`
- `with_audio_tracks()`
- `write_audio_session()`
- `copy_audio_file_for_session()`

V1 scope:

- PT12
- mono or stereo audio track layouts, as long as the output template already
  has matching audio track/channel scaffolding
- multiple mono audio tracks when the template has matching mono track
  scaffolding
- one stereo track when the template has stereo scaffolding
- multiple audio files
- multiple region-library entries
- multiple active placements
- source WAV frame-count inference when `AudioFileSpec.source_path` is set
- optional audio track rename through existing metadata string replacement

Current V1 boundary:

- playlist/index scaffolding is not synthesized from scratch
- the template, or `block_sources` applied before structured audio writing, must
  already contain the requested track/channel layout
- Pro Tools-safe distinct stereo WAV references still depend on an audio-file
  metadata donor, but the writer can now clone extra `0x1003` records by
  refreshing their private file IDs and BWF/UMID identity fields
- stereo audio needs a stereo audio template so the opaque file-channel metadata
  comes from a known-good source

Implementation notes:

- `0x1004` is rebuilt from an audio-file table template:
  - updates the top-level count at payload `+2`
  - rewrites accepted audio filename entries in the first `0x103a`
  - rewrites `0x1001` file length fields inside each `0x1003` metadata child
  - reuses real `0x1003` records first, then clones additional records as
    needed with refreshed private UUIDv4-shaped IDs
- `0x262a` is rebuilt from `0x2629 -> 0x2628` region templates:
  - creates one `0x2629` child per `AudioClipSpec`
  - rewrites region name, source offset, length, source/start-ish value, and
    WAV index
  - computes source/start-ish as template file origin plus `sampleoffset` unless
    `AudioClipSpec.source_start` is provided
  - if `AudioClipSpec.length` is omitted, uses the remaining source-file length
    from `sampleoffset`
- `0x1054` is rebuilt from active placement templates:
  - creates one `0x1052` lane entry for each requested audio channel
  - mono tracks create one lane named after the track
  - stereo tracks create two lanes with the same visible track name
  - creates one `0x1050 -> 0x104f` placement per lane clip
  - rewrites region index and timeline start

Generated structured probe:

- `synth_audio_structured_two_files_v1.ptx`
  - built with `write_audio_session()`
  - base template: `one audio clip bar 1.ptx`
  - audio template: `two audio files one clip each.ptx`
  - two WAVs: `Audio 1-SigGen_01.wav`, `Audio 1-SigGen_02.wav`
  - two synthesized region names: `First File`, `Second File`
  - placements on `Audio 1` at samples 0 and 176400
  - parses back correctly
  - Pro Tools validation: loads perfectly

## Batch 5 Findings

Control files received in `generated/`:

- `two audio tracks no clips.ptx`
- `two audio tracks second track only.ptx`
- `one stereo audio track no clips.ptx`
- `one stereo audio clip bar1.ptx`

Decoded through `PTFFormat.load(path, 44100)`:

- Two mono audio tracks, no clips:
  - no WAVs
  - no regions
  - no active placements
- Two mono audio tracks, clip on second track only:
  - one WAV: `Audio 2-SigGen_01.wav`
  - one region: `Audio 2-SigGen_01-01`
  - one active placement on the second `0x1052` entry, named `Audio 2`
  - parser currently renumbers the single populated track to `t(0)`, so
    direct block inspection is needed to distinguish "second visible track"
- One stereo audio track, no clips:
  - no WAVs
  - no regions
  - no active placements
- One stereo audio clip at bar 1:
  - one WAV: `Audio 1-SigGen_04.wav`
  - actual WAV file is 2-channel, 44100 Hz, 264600 frames
  - two region-library entries:
    - `Audio 1-SigGen_04-01.L`
    - `Audio 1-SigGen_04-01.R`
  - both regions point at WAV index 0 with source offset 88200 and length 88200
  - two active placement lanes both named `Audio 1`, using region indexes 0 and
    1 at timeline start 0

Mono multi-track shape:

- Empty two-track sessions already require the larger playlist/index scaffold:
  - `0x1015` contains two `0x1014` mono entries:
    - `Audio 1`, channel map `[0]`
    - `Audio 2`, channel map `[1]`
  - `0x1054` count is 2 and contains two empty `0x1052` entries
  - `0x2624` contains two `0x261c` playlist entries
  - `0x202b` grows from 240 to 244 bytes of payload
  - `0x2587` grows from 1556 to 1822 bytes of payload
  - final `0x0002` index grows from 6231 to 6407 bytes of payload
- Adding a clip on the second track changes the normal audio blocks:
  - `0x1004` grows from no WAV entries to one WAV entry
  - `0x262a` grows from no regions to one region
  - `0x1054` keeps count 2, with the first `0x1052` empty and the second
    containing one `0x1050 -> 0x104f` placement
- Adding a clip on the second track also changes some sidecars:
  - `0x2624` grows by 19 bytes of payload
  - `0x2587` grows by 572 bytes of payload and gains a `0x255c` child under
    `0x2551`
  - final `0x0002` grows from 6407 to 6422 bytes of payload

Stereo track shape:

- A stereo audio track is not represented as two separate mono audio tracks.
- `0x1015` contains one `0x1014` entry named `Audio 1` with channel count 2 and
  channel map `[0, 1]`.
- `0x1054` contains two `0x1052` lane entries, both named `Audio 1`.
- `0x2624` contains one `0x261c` playlist entry, slightly larger than the mono
  one-track playlist entry.
- A stereo clip creates two region-library entries in `0x262a`, one `.L` and
  one `.R`, both pointing at the same 2-channel WAV file.
- A stereo clip creates two active placements in `0x1054`, one per lane:
  - lane 0 uses region 0 (`.L`)
  - lane 1 uses region 1 (`.R`)
- Adding a stereo clip also changes sidecars:
  - `0x2624` grows by 19 bytes of payload
  - `0x2587` grows by 572 bytes of payload and gains a `0x255c` child under
    `0x2551`
  - final `0x0002` grows from 6246 to 6261 bytes of payload

Generated probes:

- `synth_audio_two_tracks_empty_scaffold_v1.ptx`
  - built from `one audio track no clips.ptx`
  - copies the empty two-track scaffold from `two audio tracks no clips.ptx`:
    `0x1054`, `0x1015`, `0x2107`, `0x2519`, `0x2624`, `0x2587`, `0x202b`, and
    source-sized/remapped `0x0002`
  - parses back with no WAVs, no regions, and no active placements
  - Pro Tools validation: opens successfully
- `synth_audio_second_track_clip_minimal_v1.ptx`
  - built from `two audio tracks no clips.ptx`
  - copies only `0x1004`, `0x262a`, and `0x1054` from
    `two audio tracks second track only.ptx`
  - intentionally leaves the empty two-track sidecars in place
  - parses back with one region placed on the `Audio 2` lane
  - Pro Tools validation: opens successfully
  - conclusion: an empty two-track scaffold is enough for a newly added
    second-track clip; `0x2624`/`0x2587`/`0x0002` do not need per-clip updates
    for this simple no-fade case
- `synth_audio_stereo_clip_minimal_v1.ptx`
  - built from `one stereo audio track no clips.ptx`
  - copies only `0x1004`, `0x262a`, and `0x1054` from
    `one stereo audio clip bar1.ptx`
  - intentionally leaves the empty stereo sidecars in place
  - parses back with two `.L`/`.R` regions placed on the two stereo lanes
  - Pro Tools validation: opens successfully
  - conclusion: stereo clip writing can follow the same minimal block strategy
    as same-track mono once the stereo track scaffold exists

Structured writer update:

- The one-track-only guard in `with_audio_tracks()` was replaced with a
  scaffold check:
  - requested `AudioTrackSpec.channels` must match the output template's
    `0x1015 -> 0x1014` channel layout
  - requested lane count must match the output template's `0x1054 -> 0x1052`
    lane count
- For mono multi-track sessions, each `AudioTrackSpec(channels=1)` creates one
  active lane.
- For stereo sessions, `AudioTrackSpec(channels=2)` creates two active lanes
  with the same visible track name, and each `AudioClipSpec` expands to `.L`
  and `.R` region entries.
- `AudioFileSpec.channels` is used to ensure a clip's source file channel count
  matches the destination track channel count. The actual opaque channel
  metadata still comes from the audio template.

Generated structured probes:

- `synth_audio_structured_two_mono_tracks_v1.ptx`
  - built with `write_audio_session()`
  - base template: `two audio tracks no clips.ptx`
  - audio template: `one audio clip bar 1.ptx`
  - one mono WAV
  - two synthesized region names:
    - `Track One Clip`
    - `Track Two Clip`
  - placements:
    - `Audio 1` at sample 0
    - `Audio 2` at sample 176400
  - parses back correctly
  - Pro Tools validation: opens successfully
- `synth_audio_structured_stereo_clip_v1.ptx`
  - built with `write_audio_session()`
  - base template: `one stereo audio track no clips.ptx`
  - audio template: `one stereo audio clip bar1.ptx`
  - one stereo WAV with `AudioFileSpec(channels=2)`
  - one `AudioTrackSpec(name="Audio 1", channels=2)`
  - one `AudioClipSpec(name="Stereo Clip", ...)` expands to:
    - `Stereo Clip.L`
    - `Stereo Clip.R`
  - both lanes are placed at sample 0
  - parses back correctly
  - Pro Tools validation: opens successfully

Next structured probes generated for Pro Tools validation:

- `synth_audio_structured_two_mono_tracks_two_files_v1.ptx`
  - built with `write_audio_session()`
  - base template: `two audio tracks no clips.ptx`
  - audio template: `two audio files one clip each.ptx`
  - two mono WAV references
  - `Track One File A` placed on `Audio 1` at sample 0
  - `Track Two File B` placed on `Audio 2` at sample 88200
  - parses back correctly
  - Pro Tools validation: opens successfully
- `synth_audio_structured_stereo_clip_bar2_v1.ptx`
  - built with `write_audio_session()`
  - base template: `one stereo audio track no clips.ptx`
  - audio template: `one stereo audio clip bar1.ptx`
  - one stereo clip named `Stereo Bar 2`
  - expands to `.L`/`.R` regions, both placed at sample 88200
  - parses back correctly
  - Pro Tools validation: opens successfully
- `synth_audio_structured_two_stereo_clips_v1.ptx`
  - built with `write_audio_session()`
  - base template: `one stereo audio track no clips.ptx`
  - audio template: `one stereo audio clip bar1.ptx`
  - two stereo clips using the same stereo WAV
  - expands to four region entries:
    - `Stereo First.L`
    - `Stereo First.R`
    - `Stereo Second.L`
    - `Stereo Second.R`
  - placements are sample 0 and 176400 on both stereo lanes
  - parses back correctly
  - Pro Tools validation: opens successfully

Next structured naming probes generated for Pro Tools validation:

- `synth_audio_structured_two_mono_custom_names_v1.ptx`
  - built with `write_audio_session()`
  - base template: `two audio tracks no clips.ptx`
  - audio template: `two audio files one clip each.ptx`
  - renames tracks to `DX Left` and `DX Right`
  - creates clips `Line A` and `Line B`
  - places `Line A` at sample 0 and `Line B` at sample 176400
  - parses back correctly
  - Pro Tools validation: opens successfully
- `synth_audio_structured_stereo_custom_name_v1.ptx`
  - built with `write_audio_session()`
  - base template: `one stereo audio track no clips.ptx`
  - audio template: `one stereo audio clip bar1.ptx`
  - renames the stereo track to `Stereo Print`
  - creates stereo clip `Print A`, expanding to `Print A.L` and `Print A.R`
  - places both stereo lanes at sample 88200
  - parses back correctly
  - Pro Tools validation: opens successfully

Next custom audio-file reference probes generated for Pro Tools validation:

- `synth_audio_structured_custom_wav_names_mono_v1.ptx`
  - copied real WAVs into `generated/Audio Files/` as:
    - `DX Line A.wav`
    - `DX Line B.wav`
  - built with `write_audio_session()`
  - base template: `two audio tracks no clips.ptx`
  - audio template: `two audio files one clip each.ptx`
  - rewrites `0x1004` to reference `DX Line A.wav` and `DX Line B.wav`
  - places `Line A` on `DX Left` at sample 0
  - places `Line B` on `DX Right` at sample 176400
  - parses back correctly
  - Pro Tools validation: opens successfully
- `synth_audio_structured_custom_wav_name_stereo_v1.ptx`
  - copied a real stereo WAV into `generated/Audio Files/` as
    `Stereo Print.wav`
  - built with `write_audio_session()`
  - base template: `one stereo audio track no clips.ptx`
  - audio template: `one stereo audio clip bar1.ptx`
  - rewrites `0x1004` to reference `Stereo Print.wav`
  - places `Print A.L` and `Print A.R` on `Stereo Print` at sample 88200
  - parses back correctly
  - Pro Tools validation: opens successfully

Next structured source-range probes generated for Pro Tools validation:

- `synth_audio_structured_source_ranges_mono_v1.ptx`
  - built with `write_audio_session()`
  - base template: `two audio tracks no clips.ptx`
  - audio template: `two audio files one clip each.ptx`
  - references custom WAV names `DX Line A.wav` and `DX Line B.wav`
  - `Line A Head` uses source offset 0, length 88200, timeline start 0
  - `Line B Trim` uses source offset 132300, length 44100, timeline start
    132300
  - parses back correctly
  - Pro Tools validation: opens successfully
- `synth_audio_structured_source_range_stereo_v1.ptx`
  - built with `write_audio_session()`
  - base template: `one stereo audio track no clips.ptx`
  - audio template: `one stereo audio clip bar1.ptx`
  - references custom WAV name `Stereo Print.wav`
  - `Print Trim` expands to `.L`/`.R` regions
  - both regions use source offset 132300, length 44100, timeline start 44100
  - parses back correctly
  - Pro Tools validation: opens successfully

Next audio + tempo/meter probes generated for Pro Tools validation:

- `synth_audio_structured_tempo_meter_mono_v1.ptx`
  - built with `write_audio_session()`
  - base template: `two audio tracks no clips.ptx`
  - audio template: `two audio files one clip each.ptx`
  - references custom WAV names `DX Line A.wav` and `DX Line B.wav`
  - includes the source-range mono content from
    `synth_audio_structured_source_ranges_mono_v1.ptx`
  - adds tempo events:
    - 120 bpm at tick 0
    - 140 bpm at tick 3840000
  - adds meter events:
    - 4/4 at tick 0
    - 3/4 at tick 3840000
  - parses back correctly
  - Pro Tools validation: fails with "end of stream encountered"
  - root-cause finding:
    - the local musical payload is correct
    - the failure is isolated to the final `0x0002` index block
    - adding tempo first can make a child block land at absolute offset
      `0x10000`
    - the mono multi-track final index already contains many literal
      `0x00010000` constants
    - the next meter update was treating those constants as offsets and
      rewriting hundreds of them
- `synth_audio_structured_tempo_meter_stereo_v1.ptx`
  - built with `write_audio_session()`
  - base template: `one stereo audio track no clips.ptx`
  - audio template: `one stereo audio clip bar1.ptx`
  - references custom WAV name `Stereo Print.wav`
  - includes the source-range stereo content from
    `synth_audio_structured_source_range_stereo_v1.ptx`
  - adds the same tempo and meter events as the mono probe
  - parses back correctly
  - Pro Tools validation: opens successfully

Final-index patcher update:

- `_update_final_index()` now counts candidate old-offset occurrences inside
  the final index before patching.
- Candidate values that occur more than
  `FINAL_INDEX_PATCH_OCCURRENCE_LIMIT` are treated as likely scalar constants,
  not offsets.
- This prevents the mono multi-track `0x00010000` constants from being
  rewritten when a child block temporarily lands at offset `0x10000`.

Corrected mono audio + tempo/meter probe generated for Pro Tools validation:

- `synth_audio_structured_tempo_meter_mono_v2.ptx`
  - same intended musical/session content as
    `synth_audio_structured_tempo_meter_mono_v1.ptx`
  - generated after the final-index patcher update
  - parses back correctly
  - final index no longer has the repeated accidental `0x10068` block-start
    references seen in v1
  - Pro Tools validation: opens successfully

Combined audio + MIDI + tempo/meter probe generated for Pro Tools validation:

- `synth_audio_midi_tempo_meter_stereo_v1.ptx`
  - built with `write_template_session()`
  - base template: `one stereo audio track no clips.ptx`
  - audio template: `one stereo audio clip bar1.ptx`
  - MIDI template: `bins/TestPTX.ptx`
  - writes stereo audio track `Stereo Print` with clip `Print A`
  - writes MIDI track `Tempo Guide` with clip `Guide Note`
  - writes tempo events:
    - 120 bpm at tick 0
    - 140 bpm at tick 3840000
  - writes meter events:
    - 4/4 at tick 0
    - 3/4 at tick 3840000
  - parses back correctly
  - Pro Tools validation: opens successfully

Standard MIDI File tempo-map importer:

- Implemented `read_midi_tempo_map(path)`.
- Reads SMF tempo meta events (`0xff 0x51`) and time-signature meta events
  (`0xff 0x58`).
- Scales source MIDI ticks into the writer's 960000 PPQ Pro Tools tick space.
- Adds default 120 bpm and 4/4 events at tick 0 if the MIDI file omits them.
- SMPTE-time MIDI files are not supported yet.

Imported-tempo-map combined probe generated for Pro Tools validation:

- `generated/stem_tempo_map_120_150_4-4_3-4.mid`
  - small generated SMF control file
  - 480 PPQ
  - 120 bpm and 4/4 at tick 0
  - 150 bpm and 3/4 at MIDI tick 1920, imported as Pro Tools tick 3840000
- `synth_audio_midi_imported_tempo_map_stereo_v1.ptx`
  - built with `write_template_session()`
  - same stereo audio + MIDI guide content as the manual combined probe
  - tempo and meter events come from `read_midi_tempo_map()`
  - parses back correctly
  - Pro Tools validation: opens successfully

## Batch 6 Findings

Control files received in `generated/`:

- `two stereo audio tracks no clips.ptx`
- `two stereo audio tracks second track only.ptx`
- `four stereo audio tracks no clips.ptx`

Decoded through direct block inspection:

- Two stereo tracks, no clips:
  - `0x1015` layout is `[2, 2]`
  - channel maps are `[0, 1]` for `Audio 1` and `[2, 3]` for `Audio 2`
  - `0x1054` has four empty `0x1052` lane entries:
    - `Audio 1`, `Audio 1`, `Audio 2`, `Audio 2`
  - `0x2624` has two `0x261c` playlist entries
  - final `0x0002` payload grows to 6430 bytes
- Two stereo tracks, clip on second track only:
  - `0x1015` layout remains `[2, 2]`
  - `0x1054` still has four lanes
  - the first two `Audio 1` lanes are empty
  - the two `Audio 2` lanes each contain one placement
  - Pro Tools also creates full-file `.L`/`.R` region-library entries in
    addition to the active clip `.L`/`.R` regions
- Four stereo tracks, no clips:
  - `0x1015` layout is `[2, 2, 2, 2]`
  - channel maps are `[0, 1]`, `[2, 3]`, `[4, 5]`, and `[6, 7]`
  - `0x1054` has eight empty lane entries
  - `0x2624` has four `0x261c` playlist entries
  - final `0x0002` payload grows to 6827 bytes

Initial writer update later superseded by Pro Tools validation:

- The `0x1004` audio-file table builder can now synthesize more file
  references than the audio template originally contained.
- For extra files, it clones the last known-good audio filename entry and
  `0x1003` metadata record, updates the metadata index, and rewrites the file
  length in each cloned `0x1001` child.
- Pro Tools validation later showed cloned stereo metadata is not safe: it can
  parse locally but hangs Pro Tools during waveform overview loading.
- The writer now rejects requests with more distinct audio files than the audio
  template has real `0x1003` records.

Generated multi-stereo probes:

- `synth_audio_structured_two_stereo_tracks_v1.ptx`
  - built with `write_audio_session()`
  - base template: `two stereo audio tracks no clips.ptx`
  - audio template: `one stereo audio clip bar1.ptx`
  - two stereo tracks: `Stem 1`, `Stem 2`
  - both tracks reuse `Stereo Print.wav`
  - parses back correctly
  - Pro Tools validation: opens successfully
- `synth_audio_structured_four_stereo_tracks_v1.ptx`
  - built with `write_audio_session()`
  - base template: `four stereo audio tracks no clips.ptx`
  - audio template: `one stereo audio clip bar1.ptx`
  - four stereo tracks: `Stem 1` through `Stem 4`
  - all tracks reuse `Stereo Print.wav`
  - parses back correctly
- `synth_audio_structured_four_stereo_tracks_distinct_wavs_v1.ptx`
  - copied real stereo WAVs into `generated/Audio Files/` as:
    - `Stem 1.wav`
    - `Stem 2.wav`
    - `Stem 3.wav`
    - `Stem 4.wav`
  - built with `write_audio_session()`
  - base template: `four stereo audio tracks no clips.ptx`
  - audio template: `one stereo audio clip bar1.ptx`
  - clones the one-file stereo metadata template into four stereo file refs
  - places all four stereo clips at sample 88200
  - parses back correctly
  - Pro Tools validation: fails with "magic ID does not match"
- `synth_audio_structured_four_stereo_tracks_distinct_wavs_tempo_v1.ptx`
  - same four distinct stereo WAV refs and four stereo tracks
  - clip starts are samples 0, 44100, 88200, and 132300
  - tempo/meter events are imported from
    `stem_tempo_map_120_150_4-4_3-4.mid`
  - parses back correctly
  - Pro Tools validation: fails with "magic ID does not match"
  - conclusion: the four-stereo-track scaffold is promising, but cloning one
    stereo file metadata record into multiple distinct WAV refs is not yet
    sufficient for Pro Tools

Multi-WAV metadata ordering fix:

- The v1 distinct-WAV probes placed cloned `0x1003` audio metadata records
  after the trailing empty `0x103a` child in `0x1004`.
- Real multi-file controls place all `0x1003` records before the trailing empty
  `0x103a`.
- The writer now inserts cloned `0x1003` records immediately after the last
  existing metadata record, preserving the real child order:
  - populated `0x103a`
  - all `0x1003` metadata records
  - trailing empty `0x103a`

Corrected multi-WAV probes generated for Pro Tools validation:

- `synth_audio_structured_two_stereo_tracks_distinct_wavs_v2.ptx`
  - same target content as the two-stereo distinct-WAV diagnostic
  - verifies cloned metadata order with two stereo WAV refs
  - parses back correctly
  - Pro Tools validation: hangs while reading/opening waveform overviews with
    an assertion
- `synth_audio_structured_four_stereo_tracks_distinct_wavs_v2.ptx`
  - same target content as the failing v1 four-stem probe
  - cloned metadata now appears before the trailing empty `0x103a`
  - parses back correctly
  - Pro Tools validation: hangs while reading/opening waveform overviews with
    an assertion
- `synth_audio_structured_four_stereo_tracks_distinct_wavs_tempo_v2.ptx`
  - same target content as the failing v1 four-stem tempo-map probe
  - cloned metadata now appears before the trailing empty `0x103a`
  - parses back correctly
  - Pro Tools validation: hangs while reading/opening waveform overviews with
    an assertion
  - conclusion: cloned metadata ordering was fixed, but cloned stereo `0x1003`
    records still share opaque per-file identity/overview metadata that Pro
    Tools validates while opening waveform overviews

Cloned metadata identity update:

- Comparing real two-file controls showed that `0x1003` records differ in more
  than their visible file index.
- Real files vary several opaque timestamp/overview identity fields and a
  16-byte UUID-like field.
- In v2 cloned stereo records, the only difference between records was the
  visible file index, which matches the Pro Tools hang during waveform overview
  loading.
- The writer now deterministically refreshes those opaque per-file identity
  ranges when cloning a metadata record.

Identity-refreshed probes generated for Pro Tools validation:

- `synth_audio_structured_two_stereo_tracks_distinct_wavs_v3.ptx`
  - same target content as the v2 two-stereo distinct-WAV probe
  - cloned `0x1003` record now differs from the template in the same number of
    bytes as real two-file controls
  - parses back correctly
  - Pro Tools validation: hangs while adding files / reading overviews with an
    assertion
- `synth_audio_structured_four_stereo_tracks_distinct_wavs_v3.ptx`
  - same target content as the v2 four-stem distinct-WAV probe
  - each cloned `0x1003` record has refreshed opaque identity fields
  - parses back correctly
  - Pro Tools validation: hangs while adding files / reading overviews with an
    assertion
- `synth_audio_structured_four_stereo_tracks_distinct_wavs_tempo_v3.ptx`
  - same target content as the v2 four-stem tempo-map probe
  - each cloned `0x1003` record has refreshed opaque identity fields
  - parses back correctly
  - Pro Tools validation: hangs while adding files / reading overviews with an
    assertion
  - conclusion: deterministic identity refresh is still not enough; distinct
    stereo WAV references need real Pro Tools-generated overview/cache or
    per-file identity metadata

Follow-up distinct-stereo-WAV controls received:

- `one stereo track two distinct wav clips.ptx`
  - one stereo audio track
  - two distinct stereo WAV files imported/referenced
  - one active stereo clip from each file
  - decoded as two WAVs, four `.L`/`.R` regions, and four active lane
    placements
  - `0x1004` contains two real `0x1003` metadata records
  - `0x2587` size is 2128 bytes, matching the one-stereo-clip control, and
    includes a `0x255c` child under `0x2551`
- `two stereo tracks two distinct wav clips.ptx`
  - two stereo audio tracks
  - one distinct stereo WAV/clip on each track
  - decoded as two WAVs, four `.L`/`.R` regions, and one stereo clip per
    destination track
  - `0x1004` contains two real `0x1003` metadata records
  - `0x2587` size is 2394 bytes, matching the two-stereo-track/one-clip
    control and gaining the same `0x255c` overview/cache child
  - `0x2624` size is 3748 bytes, 38 bytes larger than the empty/generated
    two-stereo scaffold, with one extra `0x2038 -> 0x2037` cluster

Diagnostic probes generated from these controls:

- `synth_audio_two_stereo_distinct_real_metadata_v1.ptx`
  - base template: `two stereo audio tracks no clips.ptx`
  - audio template: `two stereo tracks two distinct wav clips.ptx`
  - uses the two real `0x1003` metadata records instead of cloned records
  - keeps the empty two-stereo `0x2587`/`0x2624` sidecars
- `synth_audio_two_stereo_distinct_real_metadata_copy2587_v1.ptx`
  - same structured output
  - also copies real `0x2587` from
    `two stereo tracks two distinct wav clips.ptx`
- `synth_audio_two_stereo_distinct_real_metadata_copy2624_2587_v1.ptx`
  - same structured output
  - also copies real `0x2624` and `0x2587` from
    `two stereo tracks two distinct wav clips.ptx`

These isolate three hypotheses: whether real per-file metadata alone fixes the
hang, whether the overview/cache block `0x2587` is required, and whether the
playlist sidecar `0x2624` must move with it.

Pro Tools validation:

- all three probes open successfully
- conclusion: real Pro Tools-authored `0x1003` per-file metadata alone fixes
  the hang; copying `0x2587` and `0x2624` is not required for this two-stereo
  distinct-WAV case

Follow-up custom-name probes generated:

- `synth_audio_two_stereo_distinct_real_metadata_custom_names_v1.ptx`
  - same two real metadata records from
    `two stereo tracks two distinct wav clips.ptx`
  - rewrites visible file references to `Stem 1.wav` and `Stem 2.wav`
  - renames tracks to `Stem 1` and `Stem 2`
  - places the clips at samples 88200 and 176400
- `synth_audio_two_stereo_distinct_real_metadata_custom_names_tempo_v1.ptx`
  - same custom stereo stem content
  - imports tempo/meter from `stem_tempo_map_120_150_4-4_3-4.mid`

Pro Tools validation:

- both sessions open, but Pro Tools cannot find the renamed `Stem 1.wav` /
  `Stem 2.wav` files automatically and enters relink
- those files are byte-identical to the older `Audio 1-SigGen_04.wav`, not to
  the two WAVs whose metadata records were borrowed
- conclusion: for automatic relink, the Pro Tools-authored `0x1003` metadata
  must match the actual WAV/BWF file identity, not only the visible filename

Matching-WAV-identity probes generated:

- copied `Audio 1-SigGen_05.wav` to `Stem 1 ID Match.wav`
- copied `Audio 1-SigGen_06.wav` to `Stem 2 ID Match.wav`
- `synth_audio_two_stereo_distinct_real_metadata_matching_ids_v1.ptx`
  - visible file references are renamed to the `Stem ... ID Match.wav` files
  - underlying WAV bytes/identity still match the borrowed `0x1003` records
- `synth_audio_two_stereo_distinct_real_metadata_matching_ids_tempo_v1.ptx`
  - same matching-id audio content
  - imports tempo/meter from `stem_tempo_map_120_150_4-4_3-4.mid`

Pro Tools validation:

- both matching-id sessions open successfully without relink
- conclusion: Pro Tools accepts visible filename changes when the actual WAV
  file bytes/identity still match the borrowed `0x1003` metadata record

Next production path is either:

The next step was to synthesize the WAV/BWF identity fields inside `0x1003`
from each target WAV file, while still using real donor records for the unknown
parts of the metadata record.

Four-stereo-stem metadata template control received:

- `four stereo tracks four distinct wav clips.ptx`
  - four stereo audio tracks
  - four distinct stereo WAV files:
    - `Audio 1-SigGen_05.wav`
    - `Audio 1-SigGen_06.wav`
    - `Audio 3-SigGen_01.wav`
    - `Audio 4-SigGen_01.wav`
  - eight `.L`/`.R` regions and eight active placements
  - `0x1004` contains four real `0x1003` metadata records
  - `0x1015` layout is four stereo tracks
  - `0x2624` contains four playlist entries
  - final `0x0002` payload matches the four-stereo empty scaffold size, 6827
    bytes

Four-stereo milestone probes generated:

- `synth_audio_four_stereo_distinct_real_metadata_v1.ptx`
  - base template: `four stereo audio tracks no clips.ptx`
  - audio template: `four stereo tracks four distinct wav clips.ptx`
  - keeps the real control WAV filenames/identities
  - renames tracks to `Stem 1` through `Stem 4`
  - renames clips to `Stem N Region`
  - places clips at samples 0, 44100, 88200, and 132300
- `synth_audio_four_stereo_distinct_real_metadata_tempo_v1.ptx`
  - same four-stereo audio content
  - imports tempo/meter from `stem_tempo_map_120_150_4-4_3-4.mid`

Pro Tools validation:

- both four-stereo milestone probes open successfully
- conclusion: with a Pro Tools-authored metadata donor made from the same WAV
  files, the writer can create the core milestone session shape:
  four stereo audio tracks, distinct WAVs, custom track/clip names, arbitrary
  sample placement, and imported MIDI tempo/meter

## Arbitrary WAV Linking Investigation

Goal: replace the requirement for a Pro Tools-authored metadata donor made from
the exact target stems.

Comparing `0x1003` records from
`four stereo tracks four distinct wav clips.ptx` against the actual WAV files
shows direct embedded WAV identity:

- `0x1003` offset `30:46`
  - matches the first 16 bytes of the WAV `umid` chunk
  - this also includes BWF UMID bytes `16:24`
- `0x1003` offset `93:101`
  - matches the WAV file mtime encoded as Windows FILETIME UTC
- `0x1003` offset `164:169`
  - matches the BWF `bext` time reference, stored as five little-endian bytes
- `0x1003` offset `169:177`
  - matches the BWF origination date/time encoded as Windows FILETIME UTC
- the BWF UMID is stored inside the nested `0x2106` child, not reliably at a
  fixed outer-record offset
  - in the fixed-size stereo controls it starts at outer `0x1003` offset `274`
  - the stored value matches the first 36 bytes of the WAV `bext` UMID
    (`bext[348:384]`), including the `06 0a 2b 34` SMPTE prefix
- a private Pro Tools file ID is stored inside the nested `0x2106` child
  - it is the 16 bytes immediately before the trailer
    `5a 01 00 22 00 00 00 01 43`
  - in the fixed-size stereo controls this happens to begin at outer `0x1003`
    offset `199`, but the offset moves when the `0x2106` child size changes
  - it varies per real Pro Tools-imported file, but no exact source has been
    found yet in the WAV chunks inspected

Generated boundary probes from the previously relinking
`synth_audio_two_stereo_distinct_real_metadata_custom_names_v1.ptx`:

- `synth_audio_two_stereo_arbitrary_wav_patch_umid_v1.ptx`
  - points at `Stem 1.wav` and `Stem 2.wav`
  - patches only the observed BWF/sidecar UMID fields in `0x1003`
- `synth_audio_two_stereo_arbitrary_wav_patch_link_fields_v1.ptx`
  - patches UMID fields plus mtime, BWF time reference, and BWF origination
    FILETIME fields
- `synth_audio_two_stereo_arbitrary_wav_patch_link_fields_uuid_v1.ptx`
  - also replaces the then-unknown 16-byte varying field at the fixed
    `0x1003` offset `199:215` with a deterministic identity-derived value
  - later large-session analysis showed this fixed offset is only valid for
    one record shape

These three probes isolate whether Pro Tools relink primarily uses the BWF UMID,
the timestamp/time-reference fields, or the remaining unknown 16-byte field.

Pro Tools validation:

- all three probes open successfully without relink
- conclusion: for the tested BWF files, patching only the observed UMID fields
  is sufficient for automatic Pro Tools relink
- the timestamp/time-reference fields and unknown 16-byte field are not required
  for this relink case, though they may still matter for cache/overview identity

Writer update:

- `AudioFileSpec` now has optional `source_path`
- when set, the writer reads the target WAV and patches:
  - `0x1003` offset `30:46` from the WAV `umid` sidecar chunk, with a BWF UMID
    fallback for files that do not carry the sidecar chunk
  - the nested `0x2106` BWF UMID marker from the first 36 bytes of the WAV
    `bext` UMID
- when extra `0x1003` records are cloned, the writer patches the cloned
  record's private Pro Tools file ID with deterministic RFC 4122 v4-shaped bytes

Arbitrary-WAV milestone probes generated:

- `synth_audio_four_stereo_arbitrary_wavs_v1.ptx`
  - base template: `four stereo audio tracks no clips.ptx`
  - audio template/donor: `four stereo tracks four distinct wav clips.ptx`
  - visible files are `Stem 1.wav` through `Stem 4.wav`
  - each `AudioFileSpec` uses `source_path` to patch the BWF/UMID identity from
    the corresponding stem file
  - custom track/clip names and staggered sample placement
- `synth_audio_four_stereo_arbitrary_wavs_tempo_v1.ptx`
  - same arbitrary-WAV content
  - imports tempo/meter from `stem_tempo_map_120_150_4-4_3-4.mid`

Pro Tools validation:

- both arbitrary-WAV milestone probes open successfully
- conclusion: `AudioFileSpec.source_path` plus BWF/sidecar UMID patching is
  sufficient for Pro Tools to resolve renamed arbitrary BWF WAV files in this
  four-stereo-track workflow

Cloned-donor-count boundary probes generated:

- `synth_audio_four_stereo_arbitrary_wavs_cloned_1003_v1.ptx`
  - base template: `four stereo audio tracks no clips.ptx`
  - audio template/donor: `two stereo tracks two distinct wav clips.ptx`
  - intentionally uses only two real donor `0x1003` records, cloning/patched to
    four arbitrary `Stem N.wav` BWF/UMID identities
- `synth_audio_four_stereo_arbitrary_wavs_cloned_1003_tempo_v1.ptx`
  - same cloned-donor-count audio content
  - imports tempo/meter from `stem_tempo_map_120_150_4-4_3-4.mid`

If these open, `source_path` UMID patching is also enough to make cloned donor
`0x1003` records Pro Tools-safe for this case, and the writer can relax the
"one real donor record per file" limit.

Pro Tools validation:

- both cloned-donor-count probes assert/hang while opening
- conclusion: BWF/sidecar UMID patching solves relink for real donor records
  but does not make cloned extra `0x1003` records Pro Tools-safe
- later large-session analysis supersedes this restriction by locating the
  cloned-record private ID structurally

Follow-up cloned-count probes generated:

- `synth_audio_four_stereo_arbitrary_wavs_cloned_1003_link_fields_v2.ptx`
  - cloned from the two-file donor
  - patches UMID plus observed mtime/BWF time-reference/origination-time fields
- `synth_audio_four_stereo_arbitrary_wavs_cloned_1003_link_fields_uuid_v2.ptx`
  - also patches the unknown 16-byte varying field at fixed `0x1003` offset
    `199:215` with a deterministic per-file value
  - later shown to patch the wrong slice for variable-size `0x2106` records

These isolate whether the clone hang is caused by the duplicated timestamp-ish
fields or the remaining unknown 16-byte field, but they still used the earlier
fixed-offset assumption.

Pro Tools validation:

- both follow-up cloned-count probes assert/hang while opening
- conclusion: the cloned-record failure is not fixed by patching the observed
  BWF/UMID fields, timestamp-like fields, or a fixed-offset guess at the
  unknown 16-byte varying field inside `0x1003`
- later large-session analysis shows the private ID must be found from its
  local trailer, not from absolute outer-record offset `199`

Additional UUID-shape cloned-count probes generated:

- `synth_audio_four_stereo_arbitrary_wavs_cloned_1003_uuid4_v3.ptx`
  - patches UMID and replaces fixed slice `0x1003[199:215]` with deterministic
    bytes shaped like an RFC 4122 version-4 UUID
  - leaves timestamp-like fields inherited from the donor records
- `synth_audio_four_stereo_arbitrary_wavs_cloned_1003_uuid4_link_fields_v3.ptx`
  - same UUID-shaped unknown field
  - also patches the timestamp-like fields

These test whether the unknown 16-byte field is UUID-like and rejected the v2
probe only because the deterministic bytes did not have UUID version/variant
bits. Large-session analysis later confirmed the ID is UUIDv4-shaped, but also
showed that the fixed offset was the wrong way to locate it.

## Lots-Of-WAVs Control Findings

Control session:

- `/Users/jonkubis/Music/temp/PT/lots of wave files/lots of wave files.ptx`
- 1300 parsed WAV file references
- 1300 `0x1003` audio metadata records
- 2600 audio regions
- 0 active audio placements

The large sample confirms that each audio file has one corresponding `0x1003`
record and that the record index at content offset `2:6` is one-based and
sequential.

Each inspected `0x1003` record is structurally:

- short outer header/index bytes
- child `0x1001`, which contains sample rate, source length, and the sidecar
  UMID prefix used for Pro Tools relink
- child `0x1033`
- child `0x2106`, which carries timestamps, creator strings, the private
  Pro Tools file ID, and the BWF UMID

Important correlations:

- `0x1001` sidecar identity:
  - the outer-record bytes `30:46` match the first 16 bytes of the WAV `umid`
    chunk for 1293/1300 files
  - the seven misses are files without a sidecar `umid` chunk
- `0x2106` private file ID:
  - locate the trailer `5a 01 00 22 00 00 00 01 43`
  - the 16 bytes immediately before that trailer are unique across all 1300
    records
  - all 1300 have RFC 4122 v4 UUID version/variant bits
  - each appears exactly once in the unxored PTX, all within `0x1004`
  - none appear in the corresponding WAV payloads or in `WaveCache.wfm`
- `0x2106` BWF UMID:
  - stored near the end of the child, usually at `record_size - 44`
  - matches `bext[348:384]`, not the earlier fixed-slice assumption
  - because `0x2106` sizes vary, this must be patched by locating the BWF UMID
    marker instead of using an absolute outer `0x1003` offset
- `WaveCache.wfm`:
  - contains the WAV sidecar UMID prefixes for 1293/1300 files
  - does not contain the private Pro Tools file IDs

Conclusion:

- the v2/v3 cloned-count probes patched the wrong fixed slice for the private
  file ID; that slice straddled the real ID and the following trailer in some
  record shapes
- cloned `0x1003` records need:
  - a refreshed one-based index
  - patched source length
  - patched sidecar UMID prefix
  - patched full BWF UMID at the dynamic `0x2106` marker
  - a fresh private file ID at `trailer_offset - 16`

Writer update:

- cloned `0x1003` records are enabled again
- `_audio_file_private_id_offset()` locates the private ID structurally
- `_audio_file_bext_umid_offset()` locates the BWF UMID structurally
- cloned records receive deterministic v4-shaped private IDs

New structural cloned-count probes generated:

- `synth_audio_four_stereo_arbitrary_wavs_cloned_structural_id_v4.ptx`
- `synth_audio_four_stereo_arbitrary_wavs_cloned_structural_id_tempo_v4.ptx`

Both parse locally with four distinct `Stem N.wav` file references, four
distinct v4-shaped private IDs, and patched BWF/sidecar UMID identity fields.

Pro Tools validation:

- both still assert/freeze while opening
- follow-up inspection shows all four `Stem N.wav` files in `generated/Audio
  Files` are byte-identical copies with the same BWF/sidecar UMID
- a known-good four-record arbitrary-WAV probe also points all four file records
  at this same duplicate UMID but uses four distinct Pro Tools-authored
  overview/private metadata records, so duplicate UMID alone is not the whole
  story
- remaining likely failure causes:
  - cloned records still duplicate additional overview/cache-facing fields in
    `0x2106`
  - or Pro Tools is less tolerant of duplicate source UMIDs when the surrounding
    private metadata is synthetic

Distinct-UMID cloned-count probes generated:

- `synth_audio_four_stereo_distinct_wavs_cloned_one_donor_v5.ptx`
- `synth_audio_four_stereo_distinct_wavs_cloned_one_donor_tempo_v5.ptx`

These use the same one-donor cloned-record strategy, but reference four
Pro Tools-authored WAV files with distinct BWF/sidecar UMIDs:

- `Audio 1-SigGen_05.wav`
- `Audio 1-SigGen_06.wav`
- `Audio 3-SigGen_01.wav`
- `Audio 4-SigGen_01.wav`

They parse locally with four unique source UMIDs and four unique private IDs.
They isolate whether the v4 freeze was primarily caused by the duplicate source
UMIDs in the `Stem N.wav` copies.

Pro Tools validation:

- both still assert/freeze while opening
- conclusion: distinct source UMIDs are not sufficient

`0x2106` timestamp mapping:

- child-relative `31:39`
  - WAV filesystem mtime encoded as Windows FILETIME
- child-relative `102:107`
  - first five bytes of the BWF `bext` time reference
- child-relative `107:115`
  - BWF origination date/time encoded as Windows FILETIME using local time

These are outer `0x1003` offsets `93:101`, `164:169`, and `169:177` in the
fixed-size stereo controls, but the child-relative locations are the safer map.

Writer update:

- `AudioFileSpec.source_path` now patches the `0x2106` timestamp triplet in
  addition to sidecar UMID, BWF UMID, and private file ID

Timestamp-refreshed cloned-count probes generated:

- `synth_audio_four_stereo_distinct_wavs_cloned_link_fields_v6.ptx`
- `synth_audio_four_stereo_distinct_wavs_cloned_link_fields_tempo_v6.ptx`

These use the one-donor cloned-record strategy and reference the same four
distinct Pro Tools-authored WAVs as the v5 probes. They parse locally with:

- four unique source UMIDs
- four unique v4-shaped private IDs
- patched filesystem mtime FILETIME
- patched BWF time-reference prefix
- patched BWF origination FILETIME

Against the known-good `four stereo tracks four distinct wav clips.ptx`, each
v6 `0x2106` child now differs only in the 16-byte private file ID.

Pro Tools validation:

- both v6 probes still assert/freeze while opening

Private-ID same-size probes:

- `same_size_good_private_ids_synthetic_v7.ptx`
  - starts from known-good `four stereo tracks four distinct wav clips.ptx`
  - changes only the four private `0x2106` file IDs to deterministic v4-shaped
    synthetic IDs
- `same_size_good_private_ids_foreign_real_v7.ptx`
  - starts from the same known-good session
  - changes only those private IDs to real Pro Tools-authored IDs copied from
    the 1300-WAV session

Pro Tools validation:

- both v7 probes open successfully
- conclusion: arbitrary v4-shaped private IDs are accepted when the surrounding
  Pro Tools-authored structure is intact
- the clone freeze is therefore not caused by the private ID bytes themselves

Exact-semantic cloned probe generated:

- `synth_audio_four_stereo_distinct_wavs_cloned_exact_semantics_v8.ptx`
  - uses one real donor `0x1003` and cloned file records
  - matches the known-good session's filenames, track names, clip names, source
    offsets, region lengths, and timeline starts

Hybrid `0x1004` probe generated:

- `known_good_with_cloned_1004_exact_semantics_v8.ptx`
  - starts from known-good `four stereo tracks four distinct wav clips.ptx`
  - swaps in only the cloned exact-semantic `0x1004` file table
  - replacement is same-size, so all later block offsets and final-index shape
    are preserved

These two v8 probes isolate whether the remaining freeze is in the cloned
`0x1004` file table itself or in the writer-generated region/playlist/sidecar
blocks.

Pro Tools validation:

- `synth_audio_four_stereo_distinct_wavs_cloned_exact_semantics_v8.ptx`
  still asserts/freezes
- `known_good_with_cloned_1004_exact_semantics_v8.ptx` opens far enough to show
  a Missing Files dialog, then freezes during relink/skip
- the saved notes report corrupt/error file entries pointing at repeated
  session/path folders

`0x103a` file-list correction:

- header bytes:
  - payload `2:6` stores `entry_count + 1`
  - payload `7:11` stores `entry_count`
- audio entries:
  - all audio entries except the last use suffix `EVAW 02 00000000`
  - the final audio entry uses suffix `EVAW 00 ffffffff`
- path entries after the audio files:
  - preserve their type bytes
  - suffix tail stores the entry's shifted index

The writer now rebuilds these counts, audio-entry suffixes, and shifted path
indices instead of copying them from the donor file list.

File-list-repaired probes generated:

- `synth_audio_four_stereo_distinct_wavs_cloned_filelist_v9.ptx`
  - exact semantic clone with repaired `0x103a`
- `known_good_with_cloned_1004_filelist_v9.ptx`
  - known-good session with only the repaired cloned `0x1004` swapped in

After the v9 repair, the cloned `0x1004` differs from the known-good `0x1004`
only in the four private 16-byte file IDs. The hybrid should therefore behave
like the previously validated same-size private-ID edits if `0x1004` is now
correct.

Pro Tools validation:

- both v9 files open successfully
- conclusion: cloned `0x1003` records are Pro Tools-safe when the surrounding
  `0x103a` file-list counts, audio-chain suffixes, and shifted path indices are
  rebuilt correctly

Arbitrary-stem cloned probes generated:

- `synth_audio_four_stereo_arbitrary_wavs_cloned_filelist_v10.ptx`
- `synth_audio_four_stereo_arbitrary_wavs_cloned_filelist_tempo_v10.ptx`

These return to the practical `Stem 1.wav` through `Stem 4.wav` workflow using
one real stereo audio donor record cloned to four file records. They parse
locally with:

- four distinct private file IDs
- patched BWF/sidecar UMID fields
- patched `0x2106` timestamp fields
- correct track/clip names and staggered sample placements
- duplicate source UMIDs, because the local `Stem N.wav` files are copies of
  the same Pro Tools WAV source

They test whether the now-correct file list also makes the duplicated-source
arbitrary-stem case Pro Tools-safe.

Pro Tools validation:

- both v10 files open successfully
- conclusion: one compatible Pro Tools-authored stereo `0x1003` donor can be
  cloned to multiple arbitrary stereo file records when the writer patches:
  - sidecar UMID
  - BWF UMID prefix/material bytes
  - filesystem/BWF timestamp fields
  - private v4-shaped file ID
  - `0x103a` file-list counts, audio-chain suffixes, and shifted path indices
- duplicate source UMIDs in copied local WAVs are tolerated once the rest of the
  file-list and private metadata are coherent

Random non-Pro-Tools WAV linking probe:

- source file:
  `/Users/jonkubis/Music/Audio Music Apps/DLS-Giga Samples/01 VC_SHORT-NOTES.wve/VC_8-120_mp1/VC_8-120_mp1_A3.wav`
- converted into a Pro Tools-shaped 44.1 kHz stereo 24-bit WAV by replacing
  the sample data in a known-good Pro Tools-authored WAV container
- Pro Tools validation showed:
  - replacing audio samples only, while preserving donor `0x1003` identity,
    opens successfully
  - patching `0x1003` sidecar UMID prefix only opens successfully
  - patching `0x1003` BWF UMID bytes `16:23` opens successfully
  - patching `0x1003` BWF UMID bytes `24:35` fails with end-of-stream
  - writing the current filesystem mtime into `0x1003` while the BWF
    origination time remains donor-authored causes Pro Tools to request relink
  - preserving the BWF origination FILETIME in both timestamp fields opens and
    links successfully
  - preserving the copied WAV's filesystem mtime at that same BWF origination
    time is also required; otherwise the PTX opens but Pro Tools requests relink
  - the observed `0x103a` path entries were structurally identical between the
    relink and no-relink probes, so the differentiator was timestamp coherence
    rather than path-list shape

Conclusion:

- the nested `0x2106` BWF identity field is not always entirely safe to copy
  from the WAV
- the 24-byte prefix/material portion is always the safe minimum:
  - bytes `0:16`: fixed SMPTE UMID prefix
  - bytes `16:24`: material-package bytes mirrored by the sidecar UMID
- bytes `24:35` are copied only when the WAV has the observed Pro Tools UMID
  tail pattern `29 31 18 14 fc a4 00 00 00 00` at tail bytes `2:12`;
  arbitrary/non-Avid tails are preserved from the donor record

Writer update:

- `_patch_audio_file_identity()` now patches the sidecar UMID prefix and either
  the first 24 bytes of the nested BWF identity field or the full 36 bytes for
  WAVs with the observed Pro Tools UMID tail pattern
- `_audio_file_identity()` now uses the BWF origination time as the canonical
  `0x1003` file timestamp instead of the transient filesystem mtime
- `copy_audio_file_for_session()` stages WAVs into a session `Audio Files`
  folder and sets the copied file's filesystem mtime to the same BWF
  origination timestamp used in `0x1003`
- generated practical probe:
  - `synth_audio_random_disk_sample_coherent_track1_v17.ptx`
  - `Audio Files/Random Disk Sample Coherent.wav`
- Pro Tools validation: v17 opens successfully without relink

Large real-file length control:

- source session:
  `/Users/jonkubis/Music/temp/PT/lots of wave files/lots of wave files.ptx`
- decoded as:
  - 1,300 audio-file entries
  - 2,600 stereo region entries
  - 1,300 matching WAV files in the session `Audio Files` folder
  - all WAVs are 44.1 kHz stereo 24-bit
  - frame-count range: 379 to 12,545
  - 1,022 distinct frame counts
- every parsed PTX audio-file length matches the corresponding WAV's actual
  frame count
- every region is a full-file region with sample offset 0 and length equal to
  its source file frame count
- inside each `0x1003` record, the only 8-byte occurrence of the corresponding
  source frame count is the `0x1001` child value at local offset 21
- conclusion: no additional hidden file-length field has been observed in
  `0x1003`; arbitrary source length writing should be handled by patching
  `0x1001` plus the region three-point length fields

Writer update:

- `AudioFileSpec.length` is now optional when `source_path` is set; the writer
  infers the source frame count from the WAV
- `AudioClipSpec.length` is now optional; when omitted, the clip consumes the
  rest of the source file from `sampleoffset`
- `AudioClipSpec.source_start` should normally be left unset; setting it to 0
  caused `magic ID does not match`, while preserving the donor/template source
  origin opened successfully
- Pro Tools validation:
  - `synth_audio_lots_short_379_single_full_bext_tail_v3.ptx` opens
  - `synth_audio_lots_long_12545_single_default_origin_full_bext_tail_v4.ptx`
    opens
  - `synth_audio_lots_exact_lengths_two_wavs_default_origin_full_bext_tail_v4.ptx`
    opens
  - `synth_audio_lots_short_379_public_writer_v5.ptx` opens
  - `synth_audio_lots_exact_lengths_two_wavs_public_writer_v5.ptx` opens

Validated writer recipe:

- stage WAVs with `copy_audio_file_for_session()`
- pass staged WAVs as `AudioFileSpec(source_path=...)`
- omit `AudioFileSpec.length` to infer real frame count
- omit `AudioClipSpec.length` for full-file clips, or set it explicitly for
  trims inside the source-file length
- leave `AudioClipSpec.source_start` unset for generated sessions unless a
  control file proves the replacement value is safe

## Large Stereo Scaffold / Final Index Findings

Goal: support a practical 4-7 stereo stem writer against a larger empty-track
scaffold.

Control received:

- `/Users/jonkubis/Music/temp/PT/lots of stereo tracks/lots of stereo tracks.ptx`
  - 512 empty stereo audio tracks
  - 1024 active audio lanes in `0x1054`
  - 512 track metadata records in `0x1015`
  - 512 playlist records in `0x2624`
  - final `0x0002` payload count `1183`, matching `159 + 2 * 512`

Initial 512-scaffold probes proved the audio payload was parser-correct but
Pro Tools rejected the files:

- bytewise-patched final index:
  - "magic ID does not match"
- marker-only final index:
  - "magic ID does not match while translating MIDI Operation Data"

Final-index grammar finding:

- the final `0x0002` block is a record table; the count field at payload `+2`
  matched 1183 detected records in the 512-track control
- explicit offset references usually use marker bytes
  `01 04 00 01 00` followed by a 4-byte absolute block offset
- some records also contain unmarked embedded offsets
- in the 512-track file, the dangerous unmarked offsets are concentrated in
  the high-volume `0x2624` audio-playlist record family

Winning patch rule:

- patch every marker-form offset reference
- patch unmarked references in small/non-playlist record families
- skip unmarked `0x2624` references when the final index contains more than
  `FINAL_INDEX_PATCH_OCCURRENCE_LIMIT` `0x2624` records

Validated probe:

- `synth_audio_512_first7_index_non_playlist_refs_v6.ptx`
  - built on the 512-stereo-track scaffold
  - first 7 stereo tracks populated
  - 7 audio files, 14 `.L`/`.R` regions, 14 active lane placements
  - Pro Tools validation: opens successfully

Writer update:

- `_update_final_index()` now treats the final `0x0002` block structurally
  instead of blindly patching bytewise matches
- added a regression test that reconstructs the failed 512-track body and
  verifies the new patcher reproduces the Pro Tools-accepted v6 bytes

Public-writer probes generated after the fix:

- `synth_audio_512_first7_public_writer_v7.ptx`
- `synth_audio_512_first7_public_writer_tempo_v7.ptx`

Both parse locally with 7 audio files, 14 regions, and 14 active placements.
The tempo version also parses the imported tempo/meter map:

- tempo events: 120 bpm at tick 0, 150 bpm at tick 3840000
- meter events: 4/4 at tick 0, 3/4 at tick 3840000

Seven-stem arbitrary-length milestone probes:

- `synth_audio_512_first7_arbitrary_lengths_v8.ptx`
  - generated through the public writer path
  - uses the 512-stereo-track scaffold, preserving all unused tracks/lanes
  - first seven stereo tracks are renamed `Stem 01` through `Stem 07`
  - audio files are staged from the real `lots of wave files` session
  - inferred frame counts: 379, 1243, 2967, 5289, 8142, 10566, 12545
  - clip timeline starts: 0, 22050, 44100, 88200, 132300, 176400, 220500
  - local parse: 7 audio files, 14 regions, 14 active placements
- `synth_audio_512_first7_arbitrary_lengths_imported_tempo_v8.ptx`
  - same seven arbitrary-length stereo clips
  - imports tempo/meter from `stem_tempo_map_120_150_4-4_3-4.mid`
  - local parse: tempo events 120 bpm at tick 0 and 150 bpm at tick 3840000;
    meter events 4/4 at tick 0 and 3/4 at tick 3840000
- Pro Tools validation: pending

Seven-stem media-reference correction:

- `synth_audio_512_first7_arbitrary_lengths_v8.ptx`
  - Pro Tools validation: opens, but all seven clips resolve to
    `Milestone Stem 01.wav`
- `synth_audio_512_first7_arbitrary_lengths_unique_origins_v9.ptx`
  - Pro Tools validation: opens, but all seven clips still resolve to
    `Milestone Stem 01.wav`
- `synth_audio_512_first7_arbitrary_lengths_unique_origins_refresh_ids_v10.ptx`
  - Pro Tools validation: opens, but all seven clips still resolve to
    `Milestone Stem 01.wav`
- region-tail finding:
  - after the parser-visible file index, the `0x2629` region entry tail also
    stores a channel index at tail offset `+4`
  - near the end of the same tail, the `MdTEL`-like subrecord repeats the file
    index at tail offset `-8`
  - the writer was patching only the parser-visible file index, so Pro Tools
    used the stale duplicate reference and resolved all generated regions to
    the first donor file
- `synth_audio_512_first7_arbitrary_lengths_region_tail_refs_v11.ptx`
  - patches parser-visible file index, tail channel index, and duplicate tail
    file index
  - Pro Tools validation: opens; clips 1-7 correctly resolve to distinct
    `Milestone Stem 01.wav` through `Milestone Stem 07.wav`
- `synth_audio_512_first7_arbitrary_lengths_region_tail_refs_guids_v12.ptx`
  - same as v11 plus refreshed per-region GUID bytes
  - Pro Tools validation: opens; clips 1-7 correctly resolve to distinct
    `Milestone Stem 01.wav` through `Milestone Stem 07.wav`
- conclusion: the duplicate region-tail file/channel references are required;
  refreshing region GUID bytes is not required for this case

Writer update:

- `_AudioClipResolved` now carries `channel_index`
- `_audio_regions_content()` patches:
  - tail offset `+0`: parser-visible file index
  - tail offset `+4`: channel index
  - tail offset `-8`: duplicate file index
- `AudioFileSpec(source_path=...)` now refreshes the private file ID even for
  records copied directly from the audio template, not only cloned records
- generated public-writer follow-ups:
  - `synth_audio_512_first7_arbitrary_lengths_public_writer_v13.ptx`
  - `synth_audio_512_first7_arbitrary_lengths_imported_tempo_public_writer_v13.ptx`
- Pro Tools validation: pending

Seven-stem imported-tempo final-index correction:

- `synth_audio_512_first7_arbitrary_lengths_public_writer_v13.ptx`
  - Pro Tools validation: opens successfully
- `synth_audio_512_first7_arbitrary_lengths_imported_tempo_public_writer_v13.ptx`
  - Pro Tools validation: EOF on open
- split probes:
  - `synth_audio_512_first7_arbitrary_lengths_tempo_only_v14.ptx`
  - `synth_audio_512_first7_arbitrary_lengths_meter_only_v14.ptx`
  - `synth_audio_512_first7_arbitrary_lengths_meter_then_tempo_v14.ptx`
  - Pro Tools validation: all EOF on open
- marker-only final-index probes:
  - `synth_audio_512_first7_arbitrary_lengths_tempo_only_marker_index_v15.ptx`
  - `synth_audio_512_first7_arbitrary_lengths_meter_only_marker_index_v15.ptx`
  - `synth_audio_512_first7_arbitrary_lengths_imported_tempo_marker_index_v15.ptx`
  - Pro Tools validation: all open successfully
- conclusion:
  - the tempo and meter payloads were valid
  - the EOF failure came from overpatching opaque unmarked values in the final
    `0x0002` index after resizing early global timeline blocks
  - tempo/meter replacement should patch explicit marker-prefixed offsets only
    until the unmarked index grammar is better understood

Final-index control findings:

- Pro Tools-authored empty stereo-track controls from 5 through 16 tracks and
  512 tracks show simple linear growth in the final `0x0002` index:
  - final index record count: `2 * stereo_track_count + 159`
  - final index marker count: `7 * stereo_track_count + 162`
  - final index byte size: `191 * stereo_track_count + 6055`
- repeated record counts:
  - `0x2519` records: `stereo_track_count + 9`
  - `0x2624` records: `stereo_track_count + 1`
- marker-list records use a 19-byte record header followed by 15-byte marker
  entries shaped as:
  - `01 04 00 01 00 <u32 little-endian absolute block start> 00 00 00 00 00 00`
- Pro Tools' own 512-track tempo-only control updates all 3,746 marker-prefixed
  offsets after the tempo/meter blocks grow. It also updates many unmarked
  offset-like values, but leaves a small class of offset-looking payload values
  untouched. The current writer therefore keeps the broader unmarked-offset
  patch for audio/midi scaffold rewrites, but tempo/meter replacements now use
  marker-only index patching.
- public-writer follow-up after the marker-only tempo/meter fix:
  - `synth_audio_512_first7_arbitrary_lengths_imported_tempo_public_writer_v16.ptx`
  - local parse: 7 distinct stereo WAV clips, imported 120 -> 150 bpm tempo
    map, and 4/4 -> 3/4 meter map
  - Pro Tools validation: pending

Seven-track synthesized scaffold final-index finding:

- `synth_empty_7_stereo_from_16_synth_scaffold_conservative_final_copy_v15.ptx`
  - Pro Tools validation: opens successfully
- `synth_empty_7_stereo_from_16_synth_scaffold_source_scan_donor_counts_v15.ptx`
  - Pro Tools validation: EOF on open
- split probes:
  - `synth_empty_7_stereo_from_16_synth_scaffold_source_scan_record0_only_v16.ptx`
    opens successfully
  - `synth_empty_7_stereo_from_16_synth_scaffold_source_scan_2624_only_v16.ptx`
    EOF on open
  - `synth_empty_7_stereo_from_16_synth_scaffold_source_scan_record0_2624_v16.ptx`
    opens successfully
- conclusion:
  - the EOF trigger was overpatching unmarked values inside final-index record
    `0x0003`
  - `0x2624` record differences are tolerated for this seven-stereo-track
    scaffold
  - the writer now skips broad unmarked-offset rewriting inside record
    `0x0003`, while still patching explicit marker-prefixed offsets

## Mixed Track Type Findings

Initial mixed empty-track control:

- `/Users/jonkubis/Music/temp/PT/multiple track types/multiple track types.ptx`
- no clips or MIDI regions
- parser-visible tempo map: 120 bpm at tick 0
- global track-name/order records contain four tracks:
  - `Click 1`
  - `MIDI 1`
  - `Audio 1`
  - `Audio 2`
- audio scaffold:
  - `0x1015` contains two `0x1014` records:
    - `Audio 1`, channel count 1
    - `Audio 2`, channel count 2
  - `0x1054` contains three `0x1052` active audio lanes:
    - `Audio 1`
    - `Audio 2`
    - `Audio 2`
- MIDI scaffold:
  - `0x1058` contains one `0x1057` active MIDI track:
    - `MIDI 1`
  - `0x2000` and `0x2634` are empty, as expected for no MIDI clips
- global name/order sidecars:
  - `0x2107` contains four `0x210b` records in order:
    - `Click 1`, `MIDI 1`, `Audio 1`, `Audio 2`
  - `0x2519` count field is 4 and contains:
    - four compact per-track name records in the prefix
    - eight `0x251a` children, two copies per global track
  - `0x202b` contains two `0x202a` lists with count 4 and values
    `[0, 1, 2, 3]`
- playlist sidecar `0x2624` contains four top-level playlist children, matching
  the global track order:
  - `0x261e` for `Click 1`
  - `0x2620` for `MIDI 1`
  - `0x261c` for `Audio 1`
  - `0x261c` for `Audio 2`
- overview/cache sidecar `0x2587` contains three `0x2589` children, matching
  the existing empty-track pattern of `global_track_count - 1`
- final `0x0002` index:
  - 166 records
  - selected track-related record counts:
    - `0x1015`: 2
    - `0x1054`: 2
    - `0x1058`: 1
    - `0x2107`: 1
    - `0x2519`: 13
    - `0x2587`: 4
    - `0x2624`: 4
    - `0x2000`: 1
    - `0x2634`: 1

Interpretation:

- mixed-track construction is not just "audio blocks plus MIDI blocks"; several
  global sidecars (`0x2107`, `0x2519`, `0x202b`, `0x2587`, `0x2624`, and final
  `0x0002`) use the full visible track order
- audio lane count and MIDI track count remain type-specific in `0x1054` and
  `0x1058`
- `0x2624` is the first confirmed block where playlist child type varies by
  track type:
  - audio tracks: `0x261c`
  - MIDI track: `0x2620`
  - click track: `0x261e`

Mixed reorder control:

- `/Users/jonkubis/Music/temp/PT/multiple track types/multiple track types reorder 1.ptx`
- same audio/MIDI track types as the initial control
- global order changes to:
  - `Audio 1`
  - `MIDI 1`
  - `Audio 2`
  - `Click 1`
- unchanged byte-for-byte from the initial mixed control:
  - `0x1015` audio track metadata
  - `0x1054` audio active lanes
  - `0x1058` MIDI active track
  - `0x202b` index lists (`[0, 1, 2, 3]` in both children)
- changed to match the new visible order:
  - `0x2107`
  - `0x2519`
  - `0x2624`
  - `0x2587`
- `0x2624` playlist children move with global order:
  - initial: `0x261e Click 1`, `0x2620 MIDI 1`, `0x261c Audio 1`,
    `0x261c Audio 2`
  - reorder 1: `0x261c Audio 1`, `0x2620 MIDI 1`, `0x261c Audio 2`,
    `0x261e Click 1`
- `0x1054` remains type-local audio-lane order:
  - `Audio 1`, `Audio 2`, `Audio 2`
- `0x1058` remains type-local MIDI order:
  - `MIDI 1`

Generated reorder probes:

- `mixed_reorder_probe_names_only_v1.ptx`
  - copies `0x2519` and `0x2107` from reorder 1
  - leaves `0x2624` playlist order from the initial control
- `mixed_reorder_probe_names_playlists_v1.ptx`
  - copies `0x2519`, `0x2107`, and `0x2624` from reorder 1
- `mixed_reorder_probe_names_playlists_overview_v1.ptx`
  - copies `0x2519`, `0x2107`, `0x2624`, and `0x2587` from reorder 1
- local parser validation: all three load successfully
- Pro Tools validation:
  - names only opens but still displays the original order:
    `Click 1`, `MIDI 1`, `Audio 1`, `Audio 2`
  - names + playlists fails with `magic ID does not match`
  - names + playlists + overview fails with `magic ID does not match`
- interpretation:
  - `0x2519`/`0x2107` alone do not drive visible order
  - copying reordered `0x2624` without its coupled final-index records leaves
    stale playlist-sidecar validation data

Generated reorder final-index probes:

- `mixed_reorder_probe_names_playlists_final2624_v2.ptx`
  - copies `0x2519`, `0x2107`, `0x2624`
  - also copies/remaps final-index records `136..139` (`0x2624`)
- `mixed_reorder_probe_names_playlists_final2624_2587_v2.ptx`
  - copies `0x2519`, `0x2107`, `0x2624`, `0x2587`
  - also copies/remaps final-index records `122..125` (`0x2587`) and
    `136..139` (`0x2624`)
- `mixed_reorder_probe_names_playlists_final_all_order_v2.ptx`
  - copies `0x2519`, `0x2107`, `0x2624`, `0x2587`
  - also copies/remaps final-index records:
    - `80` (`0x2107`)
    - `105..117` (`0x2519`)
    - `122..125` (`0x2587`)
    - `136..139` (`0x2624`)
- local parser validation: all three load successfully
- Pro Tools validation:
  - `mixed_reorder_probe_names_playlists_final2624_v2.ptx` opens and displays
    the reordered track order:
    `Audio 1`, `MIDI 1`, `Audio 2`, `Click 1`
  - `mixed_reorder_probe_names_playlists_final2624_2587_v2.ptx` also opens and
    displays the reordered track order
  - `mixed_reorder_probe_names_playlists_final_all_order_v2.ptx` fails with
    `magic ID does not match`
- conclusion:
  - visible track order is driven by `0x2624` playlist-child order plus the
    matching/remapped `0x2624` final-index records
  - `0x2587` may move with the reorder but is not required for the visible
    order
  - wholesale copying final-index records for `0x2519`/`0x2107` is unsafe;
    for reorder, keep the base final-index records for those name sidecars

Mixed no-click control:

- `/Users/jonkubis/Music/temp/PT/multiple track types/multiple track types no click.ptx`
- same MIDI/audio type-local scaffold as the initial mixed control:
  - `0x1015` is byte-identical:
    - `Audio 1`, channel count 1
    - `Audio 2`, channel count 2
  - `0x1054` is byte-identical:
    - `Audio 1`, `Audio 2`, `Audio 2`
  - `0x1058` is byte-identical:
    - `MIDI 1`
  - empty `0x2000` and `0x2634` are byte-identical
- global order/count changes to three tracks:
  - `MIDI 1`
  - `Audio 1`
  - `Audio 2`
- changed global sidecars:
  - `0x2107`: three `0x210b` records
  - `0x2519`: count 3, three compact prefix records, six `0x251a` children
  - `0x202b`: both lists are count 3 with values `[0, 1, 2]`
  - `0x2624`: three playlist children:
    - `0x2620 MIDI 1`
    - `0x261c Audio 1`
    - `0x261c Audio 2`
  - `0x2587`: shrinks from 2368 to 2090 payload bytes
- final `0x0002` index:
  - record count changes from 166 to 164
  - selected record counts:
    - `0x2519`: 12 instead of 13
    - `0x2624`: 3 instead of 4
    - `0x2587`: still 4

Generated no-click probes from the click-bearing mixed control:

- `mixed_no_click_probe_2624_full_final_v1.ptx`
  - copies no-click `0x2624` and full final `0x0002`
  - leaves `0x2519` global names as `Click 1`, `MIDI 1`, `Audio 1`,
    `Audio 2`
- `mixed_no_click_probe_global_no2587_v1.ptx`
  - copies no-click `0x2107`, `0x2519`, `0x202b`, `0x2624`, and full final
    `0x0002`
- `mixed_no_click_probe_global_with2587_v1.ptx`
  - copies no-click `0x2107`, `0x2519`, `0x202b`, `0x2624`, `0x2587`, and
    full final `0x0002`
- local parser validation: all three load successfully
- Pro Tools validation: all three open successfully
- interpretation:
  - the no-click `0x2624` playlist/order block plus a matching no-click final
    index is accepted even if `0x2519` still contains the stale `Click 1`
    global name/order records
  - copying the full no-click global sidecars, with or without `0x2587`, is
    also accepted
  - next isolation target is replacing the borrowed full no-click final index
    with a synthesized final index that removes only the final records made
    obsolete by dropping the click playlist/name records

Generated no-click synthesized-final probes:

- `mixed_no_click_probe_2624_synth_final_v2.ptx`
  - copies only no-click `0x2624`
  - keeps the synthesized base final index
- `mixed_no_click_probe_2624_synth_final2624_v2.ptx`
  - copies only no-click `0x2624`
  - replaces/remaps only the `0x2624` final-index family
- `mixed_no_click_probe_global_synth_final_v2.ptx`
  - copies no-click `0x2107`, `0x2519`, `0x202b`, and `0x2624`
  - replaces/remaps only those final-index families, not the whole final index
- Pro Tools validation:
  - `0x2624` body only opens successfully
  - `0x2624` body plus `0x2624` final-index family opens successfully
  - the global synthesized-final version fails with `magic ID does not match`
- follow-up diff:
  - the known-good full-final global v1 file and failing global v2 file have
    byte-identical bodies before the final index
  - the only final-index differences are in selected `0x2519` records and two
    `0x2624` records
  - next probes isolate whether remapping `0x2519` or `0x2624` final records is
    the cause; the known-good v1 file effectively keeps these copied final
    records in their raw donor form
- `mixed_no_click_probe_global_remap2624_raw2519_v3.ptx`
  - remaps only the `0x2624` final-index records while keeping `0x2519` final
    records raw
  - Pro Tools validation: opens successfully
- `mixed_no_click_probe_global_remap2519_raw2624_v3.ptx`
  - remaps only the `0x2519` final-index records while keeping `0x2624` final
    records raw
  - Pro Tools validation: fails with `magic ID does not match`
- conclusion:
  - broad unmarked-value patching inside final-index `0x2519` records is
    unsafe
  - the changed values are not marker-prefixed absolute offsets; they appear to
    be internal `0x2519` record-local fields that happen to equal known block
    starts
  - the writer now skips broad unmarked-offset rewriting inside final-index
    `0x2519` records while still allowing explicit marker-prefixed offset
    patching

Generated no-click synthesized `0x2624` reorder probes:

- source: `/Users/jonkubis/Music/temp/PT/multiple track types/multiple track types no click.ptx`
- both probes physically reorder only the three `0x2624` playlist children and
  then run the patched final-index updater; no Pro Tools-authored reorder donor
  is used
- `mixed_no_click_reorder_audio_midi_audio_2624_synth_v5.ptx`
  - requested visible order: `Audio 1`, `MIDI 1`, `Audio 2`
- `mixed_no_click_reorder_audio_audio_midi_2624_synth_v5.ptx`
  - requested visible order: `Audio 1`, `Audio 2`, `MIDI 1`
- local parser validation: both load successfully
- Pro Tools validation:
  - both files open, but both display as `Audio 1`, `Audio 1`, `Audio 2`
    with no visible MIDI track
  - opening/closing these malformed files can make Pro Tools unstable or crash
    on later session opens/closes
- interpretation:
  - moving only `0x2624` is not a valid mixed-track reorder path
  - the `0x2620` MIDI playlist child remains physically present in the PTX,
    but its slot no longer agrees with global track order/name sidecars
  - `0x2107`, the compact prefix and `0x251a` child groups inside `0x2519`,
    and `0x2624` must agree on the same global order

Generated no-click globally aligned reorder probes:

- `mixed_no_click_reorder_audio_midi_audio_global_sidecars_synth_v7.ptx`
  - synthesizes `0x2107`, `0x2519`, and `0x2624` in order:
    `Audio 1`, `MIDI 1`, `Audio 2`
- `mixed_no_click_reorder_audio_audio_midi_global_sidecars_synth_v7.ptx`
  - synthesizes `0x2107`, `0x2519`, and `0x2624` in order:
    `Audio 1`, `Audio 2`, `MIDI 1`
- local parser validation: both load successfully
- Pro Tools validation: not yet performed; these are intended to replace the
  unsafe `0x2624`-only probes once Pro Tools is restarted/clean
- Pro Tools validation:
  - `mixed_no_click_reorder_audio_midi_audio_global_sidecars_synth_v7.ptx`
    opens, but still displays `Audio 1`, `Audio 1`, `Audio 2` with no visible
    MIDI track
  - `mixed_no_click_reorder_audio_audio_midi_global_sidecars_synth_v7.ptx`
    fails with `magic ID does not match`
- follow-up comparison against the real click-bearing reorder control:
  - synthetic `0x2107` exactly matches Pro Tools
  - synthetic `0x2519` does not; compact prefix records and `0x251a` children
    carry position-dependent ordinals that must be rewritten after moving
    records
  - synthetic `0x2624` does not; playlist children carry an internal global
    slot ordinal as well

Generated no-click globally aligned + ordinal-patched reorder probes:

- `mixed_no_click_reorder_audio_midi_audio_global_ordinals_synth_v8.ptx`
  - order: `Audio 1`, `MIDI 1`, `Audio 2`
  - patched `0x2519` compact-record ordinals, `0x251a` child ordinals, and
    `0x2624` playlist-child ordinals
- `mixed_no_click_reorder_audio_audio_midi_global_ordinals_synth_v8.ptx`
  - order: `Audio 1`, `Audio 2`, `MIDI 1`
  - same ordinal patches
- local parser validation: both load successfully
- Pro Tools validation:
  - `mixed_no_click_reorder_audio_midi_audio_global_ordinals_synth_v8.ptx`
    opens, but still displays `Audio 1`, `Audio 1`, `Audio 2` with no visible
    MIDI track, and Pro Tools crashes on close
  - `mixed_no_click_reorder_audio_audio_midi_global_ordinals_synth_v8.ptx`
    fails with `magic ID does not match`
- hard warning:
  - these mixed-track reorder probes are not safe to keep opening in Pro Tools
  - matching visible sidecar order plus the currently known ordinals is still
    insufficient; at least one more hidden track-type/slot table or
    type-specific playlist field is involved
  - next step should be a real Pro Tools-authored no-click reorder control, not
    more inferred reorder probes

Real no-click reorder control:

- `/Users/jonkubis/Music/temp/PT/multiple track types/multiple track types no click A1 M1 A2.ptx`
- requested/validated order: `Audio 1`, `MIDI 1`, `Audio 2`
- comparison with `mixed_no_click_reorder_audio_midi_audio_global_ordinals_synth_v8.ptx`:
  - synthetic `0x2107` already matches the Pro Tools-authored reorder
  - synthetic `0x2519` is nearly correct; both moved MIDI `0x251a` children
    have the slot ordinal patched at the wrong byte:
    - synthetic writes bytes `01 02`
    - Pro Tools writes bytes `02 00`
    - audio `0x251a` ordinals are byte-correct
  - synthetic `0x2624` is not correct:
    - Pro Tools expands the `MIDI 1` playlist child from 712 to 714 bytes when
      it is in slot 2
    - audio playlist children contain additional slot/order-dependent fields,
      including 10 two-byte values per audio track that are not block offsets
    - these fields were not patched in the v8 probe
  - Pro Tools also changes `0x2587` in a slot-dependent way for the reorder
- generated sanity probe:
  - `mixed_no_click_reorder_A1_M1_A2_real_order_blocks_full_index_v9.ptx`
  - starts from the original no-click base, but copies the Pro Tools-authored
    `0x2107`, `0x2519`, `0x2624`, and `0x2587` blocks from the real reorder
    control
  - uses a remapped real final index
  - local parser validation: loads successfully
- Pro Tools validation:
  - fails with `magic ID does not match`
- follow-up:
  - v9 copied the full real final index while leaving several Pro Tools-changed
    noncritical-looking blocks from the base session
  - the mismatched blocks are top-level records `0`, `1` (`0x2067`), `69`
    (`0x2016`), `79` (`0x201f`), `82` (`0x206a`), and `133` (`0x4823`)
  - therefore a full real final index is not valid unless the whole real
    reordered session state moves with it
- generated narrower final-index probes:
  - `mixed_no_click_reorder_A1_M1_A2_real_blocks_base_index_v10.ptx`
    - copies real `0x2107`, `0x2519`, `0x2624`, and `0x2587`
    - keeps the base-derived final index
  - `mixed_no_click_reorder_A1_M1_A2_real_blocks_critical_index_v10.ptx`
    - copies the same real reordered blocks
    - replaces only final-index families for `0x2107`, `0x2519`, `0x2624`, and
      `0x2587`, rather than copying the full real final index
  - local parser validation: both load successfully
- Pro Tools validation:
  - `mixed_no_click_reorder_A1_M1_A2_real_blocks_critical_index_v10.ptx`
    fails with `magic ID does not match`
  - `mixed_no_click_reorder_A1_M1_A2_real_blocks_base_index_v10.ptx` opens,
    but displays `Audio 1`, `Audio 1`, `Audio 2`, no MIDI track, and crashes
    Pro Tools on close
- interpretation of v10:
  - copying the real reordered body blocks is not enough if the final index
    remains base-derived; Pro Tools still decodes stale/incorrect track type
    state
  - replacing only the obvious reordered final-index families is still
    malformed, so the final-index coupling is broader or more positional than
    the simple family swap
  - more guessed reorder probes are unsafe; use real Pro Tools-authored
    controls to identify the remaining slot/index tables
- interpretation:
  - mixed-track reordering needs real slot-specific playlist child templates,
    not just moving existing per-track playlist records
  - a robust writer will likely need to synthesize `0x2624` by global slot and
    track type, then update `0x2587` to match

Real no-click save-noise and reorder controls:

- `/Users/jonkubis/Music/temp/PT/multiple track types/multiple track types no click resaved no changes.ptx`
- `/Users/jonkubis/Music/temp/PT/multiple track types/multiple track types no click A1 A2 M1.ptx`
- used with the earlier real reorder:
  `/Users/jonkubis/Music/temp/PT/multiple track types/multiple track types no click A1 M1 A2.ptx`
- no-change resave isolates ordinary Pro Tools save churn:
  - top-level marker block `0`
  - `0x2067` session file/path history
  - some cache-ish changes in `0x2624`, `0x2587`, `0x4823`, and final index
- reorder-specific blocks after subtracting save churn:
  - visible/global order sidecars: `0x2107`, `0x2519`, `0x2624`, `0x2587`
  - hidden/order state blocks: top-level index `79` (`0x201f`) and top-level
    index `82` (`0x206a`)
- top-level index `79` (`0x201f`):
  - byte range `85..89` changes from `05 00 00 00` in the original
    `MIDI 1`, `Audio 1`, `Audio 2` order to `ff ff ff ff` in authored reorder
    controls
- top-level index `82` (`0x206a`):
  - byte `42` stores the zero-based visible slot of `MIDI 1`
  - values observed:
    - `0` for `MIDI 1`, `Audio 1`, `Audio 2`
    - `1` for `Audio 1`, `MIDI 1`, `Audio 2`
    - `2` for `Audio 1`, `Audio 2`, `MIDI 1`
- `0x2519` real-control behavior:
  - compact prefix and both `0x251a` child groups reorder to visible track
    order
  - MIDI slot ordinal is stored at child byte `39`
  - audio slot ordinal is stored at child byte `40`
- `0x2624` real-control behavior:
  - child order follows visible order
  - early controls suggested MIDI child size was slot-dependent, but the full
    six-permutation grid below shows it is affected by audio suborder/cache
    state too
  - audio playlist children have additional slot/order/cache fields beyond the
    visible slot ordinal; these include ten two-byte sequence-like values in
    each audio child
  - therefore, `0x2624` cannot be safely reordered by moving child blocks and
    patching one ordinal
- `0x2587` real-control behavior:
  - changes with visible order and/or cache state
  - the clean `MIDI 1`, `Audio 1`, `Audio 2` to `Audio 1`, `Audio 2`,
    `MIDI 1` comparison changes three compact slot bytes at offsets `1110`,
    `1376`, and `1644`
  - the earlier `Audio 1`, `MIDI 1`, `Audio 2` control has much larger
    `0x2587` differences, so a fresh control may be useful before inferring
    generalized `0x2587` rules
- updated conclusion:
  - mixed-track reorder requires at least `0x2107`, `0x2519`, `0x2624`,
    `0x2587`, top-level index `79` (`0x201f`), and top-level index `82`
    (`0x206a`) to agree
  - final-index updates for mixed reorder are still unresolved; avoid opening
    generated mixed-reorder probes until these body/index dependencies are
    modeled from real controls

Additional real no-click reorder controls:

- `/Users/jonkubis/Music/temp/PT/multiple track types/multiple track types no click fresh.ptx`
  - this is the fresh `Audio 1`, `MIDI 1`, `Audio 2` control
- `/Users/jonkubis/Music/temp/PT/multiple track types/multiple track types no click A2 M1 A1.ptx`
  - order: `Audio 2`, `MIDI 1`, `Audio 1`
- `/Users/jonkubis/Music/temp/PT/multiple track types/multiple track types no click M1 A2 A1.ptx`
  - order: `MIDI 1`, `Audio 2`, `Audio 1`
- `/Users/jonkubis/Music/temp/PT/multiple track types/multiple track types no click A2 A1 M1.ptx`
  - order: `Audio 2`, `Audio 1`, `MIDI 1`
- the fresh `Audio 1`, `MIDI 1`, `Audio 2` control confirms:
  - `0x2107`, `0x2519`, and `0x2624` visible child order matches the earlier
    `Audio 1`, `MIDI 1`, `Audio 2` control
  - `0x2624` still differs between the two valid same-order saves, so the
    large sequence-like values inside audio playlist children are at least
    partly save/cache/ID churn, not stable reorder rules
- hidden reorder blocks:
  - top-level index `79` (`0x201f`) full bytes `85..89` are:
    - `05 00 00 00` only in the untouched/resaved natural order
      `MIDI 1`, `Audio 1`, `Audio 2`
    - `ff ff ff ff` in all five authored reorder controls, including
      `MIDI 1`, `Audio 2`, `Audio 1`
  - top-level index `82` (`0x206a`) byte `42` continues to store MIDI's
    zero-based visible slot:
    - `0` for `MIDI 1`, `Audio 1`, `Audio 2` and
      `MIDI 1`, `Audio 2`, `Audio 1`
    - `1` for both `Audio 1`, `MIDI 1`, `Audio 2` and
      `Audio 2`, `MIDI 1`, `Audio 1`
    - `2` for both `Audio 1`, `Audio 2`, `MIDI 1` and
      `Audio 2`, `Audio 1`, `MIDI 1`
- `0x1015`/`0x1054` audio type-local reorder:
  - when the audio tracks keep their relative order (`Audio 1` before
    `Audio 2`), these blocks remain byte-stable aside from offsets
  - when audio tracks swap (`Audio 2` before `Audio 1`), Pro Tools updates both
    blocks regardless of MIDI position:
    - `0x1015` audio-track metadata order becomes `Audio 2`, then `Audio 1`
    - `Audio 2` owns channel indexes `0` and `1`
    - `Audio 1` owns channel index `2`
    - `0x1054` active audio lane order becomes `Audio 2`, `Audio 2`,
      `Audio 1`
  - `0x1058` MIDI track metadata remains unchanged across these no-click
    reorders
- `0x2519` ordinal rule refinement:
  - MIDI slot ordinal is byte `39` inside each `0x251a` child
  - audio slot ordinal is byte `40` inside each `0x251a` child
  - both duplicated `0x251a` groups follow the visible order
- `0x2624` stable observations:
  - child order follows visible order
  - MIDI child byte `448` stores the one-based visible slot
  - MIDI child size is not purely slot-dependent:
    - `MIDI 1`, `Audio 1`, `Audio 2`: slot 1, 712 bytes
    - `MIDI 1`, `Audio 2`, `Audio 1`: slot 1, 714 bytes
    - `Audio 1`, `MIDI 1`, `Audio 2`: slot 2, 714 bytes
    - `Audio 2`, `MIDI 1`, `Audio 1`: slot 2, 714 bytes
    - `Audio 1`, `Audio 2`, `MIDI 1`: slot 3, 712 bytes
    - `Audio 2`, `Audio 1`, `MIDI 1`: slot 3, 714 bytes
  - audio child visible-slot ordinals observed:
    - `Audio 1` uses byte `1482`
    - `Audio 2` uses byte `1521`
  - audio playlist child sequence-like values vary across valid saves and
    should not be treated as the core reorder signal without more controls
- six-permutation summary:
  - `0x2107`, `0x2519`, and `0x2624` child order follows visible order in all
    controls
  - `0x1015` and `0x1054` follow audio-relative order only
  - `0x1058` remains unchanged
  - `0x201f` appears to mark natural order versus authored reorder
  - `0x206a` tracks MIDI's visible slot when populated with a concrete slot;
    `0xff` appears to mean unset/ignored and is accepted by several
    known-opening probes
  - the final index still changes broadly because many records contain shifted
    offsets; the body-block rules are now much clearer than the final-index
    rewrite rules
- codified offline extractor:
  - module: `ptxformatwriter.mixed_order`
  - CLI: `python3 -m ptxformatwriter mixed-order --validate <ptx> [<ptx> ...]`
  - add `--strict` to make validation issues return a non-zero exit code
  - add `--natural-order "MIDI 1,Audio 1,Audio 2"` when intentionally
    validating the known `0x201f` natural/reordered marker
  - regression test: `tests/test_mixed_order.py`
  - the validator checks the known six-permutation body-block rules without
    generating or opening synthetic PTX files

Offline open-risk audit:

- module: `ptxformatwriter.audit`
- CLI: `python3 -m ptxformatwriter audit --strict <ptx> [<ptx> ...]`
- stricter CLI: `python3 -m ptxformatwriter audit --strict --strict-final-index-refs <ptx>`
- public writer calls run this audit after writing by default
- pass `validate_output=False` to `write_template_session()`,
  `write_audio_session()`, or `write_midi_session()` only for intentional
  reverse-engineering probes
- combines:
  - final `0x0002` presence, header final-index marker, and marker-prefixed
    final-index offset checks
  - audio-link/file-table checks for `0x1004`, `0x1003`, and `0x103a`
  - mixed-order validation when the session actually contains a MIDI playlist
    record
- control-derived nuance:
  - invalid marker-prefixed refs in final-index record 0 are high risk and
    correlate with `end of stream encountered`
  - invalid marker-prefixed refs in other final-index record families can occur
    in locally round-tripping or known-opening files, so they are reported but
    not treated as fatal yet
  - `--strict-final-index-refs` additionally treats invalid refs outside the
    tolerated `0x1058`, `0x2519`, `0x2587`, `0x2597`, and `0x2624` families as
    fatal; this catches the old `mvp_midi_tempo_meter_v2.ptx` failure while
    allowing `mvp_midi_tempo_meter_v3.ptx`
  - mixed-order audit uses an open-risk subset of the stricter
    `mixed-order --validate` command:
    - a playlist can be a global-order subsequence, which covers the
      known-opening no-click probes that leave stale click-track name sidecars
    - a concrete `0x206a` MIDI slot must match global visible order, while
      `0xff` is treated as unset/ignored
    - stale `0x2519` ordinals are fatal only when playlist order and global
      order claim to match exactly
  - audio-link audit catches the cloned-file-list failure class:
    - `0x1004` audio count must match `0x1003` metadata records
    - `0x1003` one-based indexes must be sequential
    - `0x2106` private file IDs must not be duplicated
    - `0x103a` header counts, audio-entry suffix tails, and shifted path-entry
      indexes must match the v9/v10 repaired shape
    - this flags `known_good_with_cloned_1004_exact_semantics_v8.ptx` while
      allowing `known_good_with_cloned_1004_filelist_v9.ptx` and
      `synth_audio_four_stereo_arbitrary_wavs_cloned_filelist_v10.ptx`
  - writer validation checks audio-link issues only when the writer rewrites
    audio file metadata or explicitly copies `0x1004`; MIDI-only writes can
    inherit old audio metadata from a template without being blocked
- regression test: `tests/test_audit.py`

## Open Questions For Audio Writing

- Which PT12 sidecar blocks must be updated in addition to `0x1004`, `0x262a`,
  `0x1015`, and `0x1054`?
- Which blocks carry display-only waveform/cache/clip-list metadata?
- How does Pro Tools represent missing/offline files versus found local files?
- What changes for stereo, interleaved, multichannel, or non-session-rate audio?
- What extra fields are required for fades, clip gain, elastic audio, and
  grouped/compound regions?
