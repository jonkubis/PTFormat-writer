# Building Pro Tools sessions with `ptxformatwriter`

A practical guide to using this library to **read and programmatically construct Pro Tools
`.ptx` sessions** — tracks, audio clips, regions, tempo maps, meter maps, markers, a click
track, MIDI notes, and the waveform-overview cache — and to convert a beatmap MIDI + audio
stems into a finished, self-contained session.

For the on-disk byte format, see `ptx-format-spec.md`. For *why* the library is shaped the
way it is, see `gen1-vs-gen2-architecture.md`.

---

## 1. The mental model

Pro Tools is extremely strict: a file that your own code (or even this library's lenient
reader) parses fine can still fail to open with *"end of stream"* or *"magic ID does not
match."* So the library does **not** synthesize sessions from a blank spec. Instead it:

1. **Starts from a known-good control session** — a real `.ptx` that Pro Tools itself
   wrote and round-trips cleanly.
2. **Applies a minimal, byte-exact transform** — grow a track, splice in a clip, write a
   tempo map — changing the smallest possible set of blocks.
3. **Deterministically rebuilds the master index** (the block-offset pointer table) from
   the new block layout — never by guessing.

The practical consequence: **most operations need one or more control `.ptx` files** as
byte templates (§5). The payoff is reliability — every operation here was validated by
reproducing a real Pro Tools file byte-for-byte and by Pro Tools opening the result.

---

## 2. Install and import

The package has one runtime dependency beyond the stdlib: **numpy** (for the waveform
cache). `ffmpeg` on `PATH` is needed only for MP3→WAV conversion in the beatmap pipeline.

```python
import ptxformatwriter as ptx                 # curated API: ptx.add_audio_clip, ptx.set_tempo_map, …
# or reach modules directly:
from ptxformatwriter import workbench, body_synth, donorpack, wavecache, writer, core
from ptxformatwriter.core import PTFFormat
```

`ptxformatwriter.workbench` (and the top-level `ptxformatwriter` namespace) re-export the whole
construction surface; `ptxformatwriter.writer` holds the low-level I/O primitives.

---

## 3. Reading / inspecting a session

```python
from ptxformatwriter.core import PTFFormat

ptf = PTFFormat()
assert ptf.load("Session.ptx", 48000) == 0          # de-obfuscate + parse; 0 == ok
for t in ptf.tracks():        print(t.index, t.name)
for r in ptf.regions():       print(r.name, r.startpos, r.length, r.wave.filename)
for e in ptf.tempoevents():   print(e.pos, e.bpm)
for m in ptf.meterevents():   print(m.pos, m.numerator, m.denominator)
raw = ptf.unxored_data()                             # the de-obfuscated body bytes
```

> Note: `load() == 0` means *the lenient reader parsed it* — it is **not** proof Pro Tools
> will open it. Only Pro Tools opening the file is.

Lower level (what the construction tools operate on):

```python
from ptxformatwriter import writer as W
data = W.load_unxored("Session.ptx")    # de-obfuscated bytes
# … transform `data` with the tools below …
open("Out.ptx", "wb").write(W.encrypt_session_data(data))   # re-obfuscate + save
```

---

## 4. The fastest path: the beatmap example (MIDI + stems → a finished session)

A complete end-to-end application lives **outside the library**, in
[`examples/beatmap_example.py`](../examples/beatmap_example.py). Given a beatmapping MIDI
(tempo map, meter map, named markers, and a `0xFA` Start message marking the audio
head-sync) plus audio stems, one call does everything — N named stereo tracks, each stem's
clip at the head-sync, the tempo/meter/marker maps, a click track on top, and the waveform
cache. Run it directly:

```sh
python3 examples/beatmap_example.py song/TempoMap.mid song/synth/MySong.ptx \
    song/bass.mp3 song/drums.mp3 --pack control_files/donors.pack
# writes MySong.ptx + Audio Files/*.wav + WaveCache.wfm — self-contained.
```

or call it from Python. It takes a `Controls` bundle — the few donor byte-templates a build
needs (§5); build one by hand or load it from a donor pack (§5.1):

```python
from examples.beatmap_example import build_session_from_beatmap, parse_beatmap_midi
from ptxformatwriter import Controls, writer as W

CT = "control_files/lots of stereo tracks"
controls = Controls(
    nstereo      = lambda n: W.load_unxored(f"{CT}/{n} stereo tracks.ptx"),
    wav_template = open(f"{CT}/Audio Files/01.wav", "rb").read(),
    click_ref    = W.load_unxored(f"{CT}/2 stereo plus click.ptx"),   # omit to disable the click
    # tempo, meter, marker + clip templates are all INLINED (body_synth._templates) — no donor
    # needed. The only donors a build uses are the N-stereo scaffold, a WAV, and the click pair.
)
# Easier: build a donor pack once (§5.1) and `controls = ptx.load_controls("donors.pack")`.

bm = build_session_from_beatmap(
    midi_path  = "song/TempoMap.mid",
    mp3_paths  = ["song/bass.mp3", "song/drums.mp3"],   # any number of stems
    out_ptx    = "song/synth/My Song.ptx",
    controls   = controls,
    track_names= ["bass", "drums"],   # default: the MP3 stem names
    clip_names = ["bass", "drums"],   # default: track names
    click      = True,                # add a Click track as track 1
)
```

`build_session_from_beatmap` returns the parsed `Beatmap` (tempos, meters, markers,
`head_sync_tick`). It converts each MP3 to a 44.1 kHz/24-bit/stereo WAV, wraps it as a
PT/BWF WAV, builds the tracks/clips/conductor maps, adds the click, and writes the cache.

To just parse a beatmap MIDI without building: `parse_beatmap_midi(path)` → a `Beatmap`
with `.tempos`, `.meters`, `.markers`, `.head_sync_tick`, `.pt_tick(midi_tick)`,
`.seconds_at(midi_tick)`.

---

## 5. Control files you need

Most of a session is built from byte templates that are now **inlined** in the library
(`body_synth._templates`, each ≤512 B). Only a few donors are still needed at build time:

| Build-time donor | Used for |
|---|---|
| `N stereo tracks.ptx` (a family, N = 1..16+) | the clean N-track scaffold to build on |
| `2 stereo tracks.ptx` + `2 stereo plus click.ptx` | the clean/click pair that sources a Click track |
| a PT/BWF WAV (44.1k / 24-bit / stereo) | wrapping arbitrary audio into a linkable WAV |

The clip/region, tempo-map, meter-map, and marker templates are **inlined** — no donor needed
to build. Their source donors are only consulted to **regenerate** the inlined copies (via
`donorpack.write_inline_templates`, e.g. after a Pro Tools version bump):

| Template-source donor | Regenerates |
|---|---|
| `3 stereo 3 different clips.ptx` | the audio clip / region / placement templates |
| `120 to 140bpm.ptx` | the tempo-map (`0x2028`) + ruler (`0x2718`) templates |
| `3-4 meter at bar 2.ptx` | the meter-map (`0x2029`) + ruler (`0x2719`) templates |
| `a few named markers.ptx` | the marker-list (`0x2030`) template |

You can also pass any "clean_ref / ref" pair (a session **without** and **with** a feature)
straight to `set_tempo_map`/`set_meter_map`/`build_audio_clips` as an override to reproduce
that specific control byte-for-byte; the library copies exactly what differs between them.

### 5.1 Consolidate them into one donor pack

Managing ~30 loose donor files (the `N stereo tracks.ptx` family is one per track count) is
tedious. `ptxformatwriter.donorpack` bundles them all — de-obfuscated, by role — into a single
regenerable zip, then expands back into the same `Controls`:

```python
import ptxformatwriter as ptx
from examples.beatmap_example import build_session_from_beatmap
ptx.build_pack("control_files", "control_files/donors.pack")   # build once from the sources
controls = ptx.load_controls("control_files/donors.pack")      # -> a Controls bundle
bm = build_session_from_beatmap(midi, mp3s, out_ptx, controls)
```

Or regenerate from the command line:

```sh
python3 -m ptxformatwriter.donorpack control_files control_files/donors.pack
```

- **One file to carry at build time** instead of thirty — the pack expands into the exact
  `Controls` the pipeline already takes (builds from the pack are byte-identical to builds
  from the loose files), and the bundled 2-stereo scaffold doubles as the click clean-ref.
- **Still fully recreatable**: the source donor `.ptx` files remain the truth (re-author
  them in Pro Tools); `build_pack` rebuilds the pack, `donorpack.write_inline_templates`
  regenerates the inlined clip/tempo/meter/marker templates, and the manifest inside records
  each role's source path. To support a new Pro Tools version: re-author the sources, rebuild
  the pack + the inlined templates, re-run the tests. (Note: the N-stereo family can't be
  grown from one donor — the stereo "overview order" is a hash-table iteration order with no
  closed form — so each track count is bundled as its own control.)
- The pack is a build artifact (git-ignored, like `control_files/`); it's not committed.

---

## 6. The toolkit (`body_synth`) — recipes

Every function takes and returns **de-obfuscated `data` bytes**, so they compose: feed one
function's output into the next, then `writer.encrypt_session_data` at the end.

### 6.1 Track scaffolds

```python
from ptxformatwriter import body_synth as B
# Grow an empty N-stereo session from a donor + a track-library control:
data = B.synthesize_stereo_session(donor_data, base_n, target_n, library_data, library_total)
data = B.synthesize_mono_session(...)                                  # mono tracks
data = B.synthesize_mixed_session(specs=[2, 1, 2, 1], donor_data=...,  # 2 = stereo, 1 = mono
                                  mono_lib=(d, n), stereo_lib=(d, n))
# Or grow one track at a time from a "unit" extracted from a control:
unit = B.extract_track(control_data, track=0, total=N, channels=2)
data = B.grow_one_track(data, base_n, unit)
```

In the beatmap pipeline you usually just start from `controls.nstereo(n)` (a pre-made clean
N-stereo control), which avoids synthesis entirely.

### 6.2 Track names, rename, reorder, channels

```python
data = B.set_track_names(data, ["bass", "drums", "vox"])     # list (positional) or {index: name}
data = B.rename_track(data, "Audio 2", "drums")              # rename one by its current name
order = B.track_playlist_order(data)                          # current playlist order
data = B.reorder_tracks(data, [3, 0, 1, 2])                   # a permutation of 0..N-1
data = B.set_track_channels(data, track_offset=0, channels=[2, 1, 2])
names = [t.name for t in B.track_types(data)]                # inspect
```

### 6.3 Audio clips — the unified builder

`build_audio_clips(data, tracks, clip_ref)` places clips across tracks in one shot.
`tracks` is a list **per track** of clip tuples:

```python
tracks = [
    [("Audio Files/bass.wav", 88200)],                  # track 0: one clip at sample 88200
    [("Audio Files/drum.wav", 0, "Verse drums")],       # track 1: a named clip at 0
    [("Audio Files/fx.wav", 0), ("Audio Files/fx.wav", 176400)],  # track 2: two clips (shared wav)
    [],                                                 # track 3: empty
]
data = B.build_audio_clips(data, tracks)   # clip templates are inlined; pass clip_ref only to override
```

- Each clip is `(wav_path, position_file_samples)` or `(wav_path, position, name)`.
- **Positions are file samples, not ticks** (sample 88200 = 2.0 s at 44.1 kHz).
- Each distinct WAV becomes one region pair (`.L` / `.R`); clips of the same WAV share it.
- The WAVs must be staged on disk as PT/BWF WAVs (see 6.4).
- Inspect placements with `B.clip_positions(data)`; move all/specific lanes with
  `B.set_clip_position(data, samples, lanes=(...))`.

### 6.4 Linking arbitrary audio (wrap raw WAVs)

Pro Tools only links BWF WAVs carrying a UMID. Convert your audio to a raw 24-bit/stereo
WAV, then wrap it against a template WAV:

```python
raw = open("ffmpeg_out.wav", "rb").read()           # 44.1k/24-bit/stereo, no UMID
wrapped = B.wrap_raw_wav(raw, controls.wav_template, seed="bass-0")  # unique, deterministic UMID
open("Audio Files/bass.wav", "wb").write(wrapped)
sc, umid, id2 = B.wav_clip_identity("Audio Files/bass.wav")          # read back its identity
```

To re-point clips in an existing clip session to other WAVs: `B.set_clip_wav(data, wav)`
(single clip) or `B.set_clip_wavs(data, [wav, ...])` (one per clip, in track order).

### 6.5 Tempo map, meter map, markers

Positions here are **PT ticks** (960000 per quarter note). From a MIDI tick:
`pt_tick = midi_tick * 960000 // division`.

All three grow their blocks from INLINED templates (`body_synth._templates`) — no donor:

```python
# events = [(bpm, pt_tick), …]
data = B.set_tempo_map(data, [(120.0, 0), (140.0, 1_920_000), (128.0, 3_840_000)])
# events = [(numerator, denominator, pt_tick), …]
data = B.set_meter_map(data, [(4, 4, 0), (2, 4, 1_920_000)])
# markers = [(name, pt_tick), …]
data = B.set_markers(data, [("Intro", 0), ("Verse", 960_000), ("Chorus", 3_840_000)])
# single-value convenience: B.set_tempo(data, clean, ref, bpm=128.0); B.set_meter(data, clean, ref, ...)
```

To reproduce a specific control byte-for-byte, pass a donor pair as the optional override:
`B.set_tempo_map(data, events, tempo_ref, clean_ref)` (likewise `set_meter_map(data, events,
meter_ref, clean_ref)`). These scale to hundreds of events (one tempo point per beat is normal).

### 6.6 Click track (on top)

```python
clean2 = W.load_unxored("control_files/lots of stereo tracks/2 stereo tracks.ptx")
click2 = W.load_unxored("control_files/lots of stereo tracks/2 stereo plus click.ptx")
data = B.add_click_anyN(data, clean2, click2, at_top=True)   # Click becomes the first track
# (the bottom variant is at_top=False; B.move_click_to_top(data) reorders an existing click)
```

The Click is a Pro Tools Click track (uses PT's click instrument/sound, like any click
track) — it has no audio file and needs no WAV / cache entry.

### 6.7 MIDI notes

```python
data = B.add_midi_note(data, clean_ref, midi_ref, pitch=60, velocity=100, length_ticks=480_000)
```

### 6.8 Session name

```python
print(B.session_name(data))
data = B.set_session_name(data, "My Song")
```

### 6.9 Waveform-overview cache

So the session opens with waveforms drawn (Pro Tools otherwise needs a manual *Recalculate
Waveform Overviews* on first open):

```python
from ptxformatwriter import wavecache as WC
WC.write_wavecache("song/synth", ["song/synth/Audio Files/bass.wav", ...])  # writes WaveCache.wfm
# or get the bytes: WC.build_wavecache_for_wavs([...])
```

The cache is keyed by each WAV's UMID, so it must be generated from the **same wrapped
WAVs** the session references. (`build_session_from_beatmap` does this for you.)

---

## 7. The recommended operation order

A tidy default order is conductor → clips → click → names, which the beatmap pipeline uses:

```
nstereo scaffold
  → set_tempo_map → set_meter_map → set_markers      (conductor)
  → build_audio_clips                                 (clips)
  → add_click_anyN(at_top=True)                       (click / reorder)
  → set_track_names                                   (names)
  → encrypt_session_data + write   →  write_wavecache
```

Renaming no longer has to be last: `add_click_anyN` is **name-robust** — it normalizes the
audio tracks to `Audio N` for the click splice and restores your names afterward, so the
click lands correctly whether you rename before or after it (byte-identical either way).

---

## 8. Saving a self-contained session

```python
open(out_ptx, "wb").write(W.encrypt_session_data(data))     # the .ptx
# stage the wrapped WAVs in  <out_ptx dir>/Audio Files/
WC.write_wavecache(out_ptx_dir, wav_paths)                  # the WaveCache.wfm
```

A self-contained session folder is: `My Song.ptx` + `Audio Files/*.wav` + `WaveCache.wfm`.

---

## 9. Units & conventions (cheat sheet)

| Quantity | Unit |
|---|---|
| Musical position (tempo / meter / marker) | **PT ticks**, 960000 per quarter note |
| Audio clip position | **file samples** at the session rate (44100) — *not* ticks |
| MIDI → PT tick | `midi_tick * 960000 // division` |
| Head-sync → samples | integrate the tempo map to seconds, then `* sample_rate` |
| BPM | IEEE-754 double |
| Timestamps in WAV / cache | Windows FILETIME = `round((unix_mtime + 11644473600) * 1e7)` |

---

## 10. Gotchas

- **Clip positions are samples, not ticks.** A common mistake; `88200` is 2.0 s, not 2 beats.
- **Raw WAVs don't link.** Wrap them (`wrap_raw_wav`) so they carry a UMID, or Pro Tools
  won't relink them.
- **Pro Tools is the only oracle.** A clean parse / reload is not acceptance — open it in PT.
- **Rename order is free** with `add_click_anyN` (name-robust, §7) — but other reorder passes
  (`reorder_tracks`) still expect canonical layouts, so when in doubt, rename last.
- **Wave-cache coherence:** the cache is keyed by WAV UMID + mtime; generate it from the
  final wrapped WAVs and don't re-touch the WAVs afterward (mtime drift invalidates it).
- **GUIDs are free** but **UMIDs, region findex, and index offsets are not** — those must be
  exactly right or playback/linking breaks (see `ptx-format-spec.md` §6, §9, §11).

---

## 11. A complete manual build (no beatmap MIDI)

```python
from ptxformatwriter import body_synth as B, writer as W, wavecache as WC
CT = "control_files/lots of stereo tracks"
L = lambda p: W.load_unxored(p)

data = L(f"{CT}/3 stereo tracks.ptx")                                  # 3 clean stereo tracks
data = B.set_tempo_map(data, [(120.0, 0), (128.0, 1_920_000)])         # tempo template inlined
data = B.set_meter_map(data, [(4, 4, 0)])                              # meter template inlined
data = B.set_markers(data, [("Intro", 0), ("Verse", 960_000)])         # markers inlined
# stage WAVs (wrapped) in ./Audio Files/ first, then:
data = B.build_audio_clips(data, [[("Audio Files/bass.wav", 0)],       # clip templates inlined
                                  [("Audio Files/drums.wav", 0)],
                                  [("Audio Files/vox.wav", 88200)]])
data = B.add_click_anyN(data, L(f"{CT}/2 stereo tracks.ptx"),
                        L(f"{CT}/2 stereo plus click.ptx"), at_top=True)
data = B.set_track_names(data, ["bass", "drums", "vox"])               # LAST
open("My Song.ptx", "wb").write(W.encrypt_session_data(data))
WC.write_wavecache(".", ["Audio Files/bass.wav", "Audio Files/drums.wav", "Audio Files/vox.wav"])
```

---

## 12. Public API quick reference

**Beatmap example** (`examples/beatmap_example.py`, an application — not in the library):
`build_session_from_beatmap`, `parse_beatmap_midi`, `convert_to_wav`, `Beatmap`. The donor
bundle it takes is `ptxformatwriter.Controls` (produced by `donorpack` / `load_controls`).

**Construction** (`ptxformatwriter.body_synth`): `synthesize_stereo_session` /
`synthesize_mono_session` / `synthesize_mixed_session`, `extract_track`, `grow_one_track`,
`set_track_names`, `rename_track`, `reorder_tracks`, `set_track_channels`, `track_types`,
`track_playlist_order`, `build_audio_clips`, `add_audio_clip`, `add_clip_to_track`,
`add_clips_to_tracks`, `set_clip_position`, `clip_positions`, `set_clip_wav`,
`set_clip_wavs`, `wrap_raw_wav`, `wav_clip_identity`, `set_tempo` / `set_tempo_map`,
`set_meter` / `set_meter_map`, `set_markers`, `add_click` / `add_click_anyN` /
`move_click_to_top`, `add_midi_note`, `session_name` / `set_session_name`.

**Waveform cache** (`ptxformatwriter.wavecache`): `build_wavecache_for_wavs`, `write_wavecache`.

**I/O primitives** (`ptxformatwriter.writer`): `load_unxored`, `encrypt_session_data`,
`parse_unxored`, `top_level_refs`, `BlockRef`.

**Reader** (`ptxformatwriter.core`): `PTFFormat` (`.load`, `.tracks`, `.regions`, `.tempoevents`,
`.meterevents`, `.unxored_data`, …).

**Index repair** (`ptxformatwriter.final_index`): `reindex_after_resize`, `offset_holes`,
`rebuild_index_offsets` (used internally by the construction tools).
