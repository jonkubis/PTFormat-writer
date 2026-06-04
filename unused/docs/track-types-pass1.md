# Track types (mono / MIDI / click) — Pass-1 decode

Status 2026-05-29. Empty-**stereo** synthesis is PT-confirmed for 1–16 tracks
(see `body-synthesis-pass1.md`). This doc records the first-pass structural
decode of the OTHER track types, derived from `control_files/multiple track
types/` compared against the clean stereo count-series.

## ⚠️ Methodology caveat — the multi-type controls are NOISY

The stereo decode was clean because of a **count series** (`N stereo tracks.ptx`,
N=0..24) where N and N+1 differ by *exactly one stereo track*. There is no such
series for mono/MIDI/click. The only controls with these types are
`multiple track types/*.ptx`, each a **single fixed session** containing
`Audio 1 (mono)`, `Audio 2 (stereo)`, `MIDI 1`, eight buses (`Bus 1..8`), a
`Master`, and `Markers` — plus a `+ Click` variant. Because they are *separately
saved* sessions, diffing them conflates track-type structure with session-state
churn (some block types *decrease* from "no click" → "full", e.g. 0x2038 14→10,
0x2056 5→4). So these files are good for **identifying** each type's footprint
but not for a precise per-track-unit extraction the way the stereo series was.

To finish + PT-confirm mono/MIDI/click synthesis we want **clean count-series
controls** for each type (the proven path).

## What the no-click session actually contains

`multiple track types no click.ptx` = `Audio 1` (MONO), `Audio 2` (STEREO),
`MIDI 1`, `Bus 1..8`, `Master`, `Markers`. Confirmed mono/stereo via the channel
map (below). This is lucky: it gives a mono AND a stereo audio track *in one
file* for direct comparison.

## Name footprint per track type (length-prefixed name slots)

A stereo track's name lives in **8** length-prefixed slots (see naming decode).
Per type, the name-carrying block set is:

| block        | stereo | mono | MIDI | meaning                                  |
|--------------|:-----:|:----:|:----:|------------------------------------------|
| 0x1014       |   1   |  1   |  0   | audio channel map (audio only)           |
| 0x1057       |   0   |  0   |  1   | **MIDI track header** (replaces 0x1014)  |
| 0x1052       |   2   |  1   |  0   | audio lane/playlist — **one per channel**|
| 0x251a       |   2   |  2   |  2   | per-track (2 regardless of type)         |
| 0x210b       |   1   |  1   |  1   | per-track                                |
| 0x2519 entry |   1   |  1   |  1   | name-table own-byte entry                |
| 0x2619       |   1   |  1   |  1   | per-track                                |
| **name total** | **8** | **7** | **6** |                                      |

So: **mono = stereo with one fewer 0x1052 lane**; **MIDI = no 0x1014/0x1052, plus
a 0x1057 header**.

## Channel map (0x1014) — encodes channel count

```
mono  Audio 1 (size 52): 14 10 <u32 sz> "Audio 1" 01 00 00000000 0000 0000 00 2a000000 <FILETIME> ...
stereo Audio 2 (size 58): 14 10 <u32 sz> "Audio 2" 01 02 000000 0100 0200 0000 00 2a000000 <FILETIME> ...
```
After `"name" 01`: the next byte is the **channel count** (`00`/absent for mono’s
single channel, `02` for stereo). Stereo then lists channel indices as u16
(`01 00`,`02 00` = channels [1,2]); each control’s pair is `[2k-2, 2k-1]`-ish
(stereo n3: A1=[0,1] A2=[2,3] A3=[4,5]). Mono is **6 bytes shorter** (one fewer
channel entry).

## Audio playlist (0x261c) differs by type

Mono `0x261c` = **1818 B**, stereo `0x261c` = **1857 B** (Δ≈39). MIDI has **no
0x261c** at all (it isn’t an audio playlist). So per-type 0x261c content genuinely
differs — a mono track can’t just clone a stereo playlist.

Per-type presence of the non-name blocks (from counts):
- `0x261c` (audio playlist): mono ✓, stereo ✓, **MIDI ✗**, bus ✗, master ✗.
- `0x1052` (audio lane): stereo 2, mono 1, MIDI 0.

## MIDI track (decoded 2026-05-29)

**MIDI footprint** (the blocks a MIDI track contributes), confirmed against the
no-click control:

| block            | role                                                       |
|------------------|------------------------------------------------------------|
| `0x1057` (198B)  | **MIDI track header** (audio's `0x1014` analog). Subtree: `0x1057 → 0x2502 (158B) → 0x103d (61B)`. |
| `0x210b` (48B)   | per-track (same slot audio has, but 48B vs audio's 49B)     |
| `0x2519` entry   | name-table own-byte entry                                  |
| `0x251a` ×2 (78B)| per-track lanes (78B; audio's are 79B — the 1-byte diff is MIDI-vs-audio) |
| `0x2619` (151B)  | per-track                                                  |
| **`0x2620`** (705B) | **MIDI playlist** (audio's `0x261c` analog) — contains a `0x2619`/`0x102d` subtree with the "MIDI 1" name, like `0x261c` does |

MIDI has **no** `0x1014`, **no** `0x1052`, **no** `0x261c`, and contributes **0
audio channels** (not counted in `0x1054`/`0x1015`; counted in `0x2107+11` total
tracks). The `0x1057` header:
```
57 10 <sz> "MIDI 1" 00 00 00 00 | 5a 02 00 9e000000 (0x2502) | 02 25 01 01 00 01 | 5a 04 00 3d000000 (0x103d) | 3d 10 45 ...
```

**Ambiguous (noise wall):** `0x2010` (344B), `0x2036` (69B), `0x203a` (×12, 13B),
`0x206f` (6B), `0x2611` (118B) appear in the no-click session but can't be cleanly
attributed to the MIDI track vs the eight buses / master / markers (none contain
"MIDI 1"). Like the click track, fully isolating the MIDI unit needs a clean
MIDI-only control (`N MIDI tracks.ptx`). The definitively-MIDI blocks above
(`0x1057` subtree + `0x2620` playlist + the shared per-track slots) are solid.

## Click track — decoded (2026-05-29): named "Click 1", uses 0x261e + DigiClick plugin

The click track is named **`Click 1`** and its playlist is block type **`0x261e`**
(the analog of audio `0x261c` / MIDI `0x2620`; its subtree is the same
`0x261b → 0x102d → 0x2619` shape, with `1926 07000000 "Click 1"`). It is **NOT** in
the `0x2519` own-byte name table (which holds only the mono/stereo audio tracks —
`Audio 1`, `Audio 2`; MIDI/Click names live in `0x2519` child blocks, not the
own-byte table). It adds 2× `0x251a` lanes + `0x210b` + `0x2619` + `0x4420`×2, but
**no `0x1014` and no `0x1052`** (no audio channel map / lanes).

The click also embeds a **DigiClick plugin instance** — the click-only block types
`0x1000`, `0x1038`, `0x2613`, `0x2615`, `0x2616`, `0x2064`, `0x200d`, `0x2d10` all
carry `DigiclikCkRT…` / `Digiclik…Fact` plugin data (the click sound generator,
referencing `Macintosh HD/…/Digiclik…`). This makes the click the most complex
type — it's effectively an aux-style track plus a bundled plugin.

`"Click"` appears 10× in the full control (plugin names + the `Click 1` track
name). Synthesizing it = cloning the `0x261e` playlist subtree + the DigiClick
plugin block chain + the 2 lanes. Like mono/MIDI, a clean control series (e.g. a
click-only session, and click + 1 audio) is the tractable path.

## (historical) Click track (full vs no-click) — biggest footprint, most confounded

The `Click` track is **NOT** stored as a length-prefixed `"Click"` name (0
occurrences). Adding it (no-click → full) introduces these click-correlated
types (absent without click): `0x2d10`, `0x2613`, `0x2615`, `0x2616`, `0x261e`,
`0x2064`, `0x200d`, `0x1038`, `0x1000`; and bumps `0x251a +2` (2 lanes like
stereo), `0x2619 +1`, `0x210b +1`, `0x4420 +2`. But the full vs no-click diff is
the noisiest (clip/automation types 0x2625/0x2626/0x203b/0x2037 also jump, and
two types *decrease*), so the click unit needs a dedicated control to pin down.

## Synthesis implications

- **Mono** is the closest to the solved stereo case: it’s "stereo minus one
  0x1052 lane, shorter 0x1014, slightly smaller 0x261c". Most tractable next.
- **MIDI** needs the 0x1057 header subtree + its MIDI-specific blocks decoded.
- **Click** is the most divergent.
- For all three, the same back-end applies once the body is correct:
  `compose_index` for the 0x0002 index, then `_set_index_offset` (THE master
  fix), then `writer.encrypt_session_data`.

## PRECISE MONO DECODE (2026-05-29) — mono = stereo minus one channel

Comparing a mono track (no-click `Audio 1`) against stereo tracks (clean series,
same track *position* to isolate structure), the mono-vs-stereo per-track delta
is exactly **three block edits + one session field**:

### 1. `0x1014` channel map (mono 52B vs stereo 58B, Δ−6)
Content layout after `<type:2><namelen:4><name>`:
```
rest[0]   = nchan - 1          (stereo 1, mono 0)
rest[1]   = nchan              (stereo 2, mono 1)
rest[2:5] = 00 00 00
rest[5]   = channel index 0    (byte)         <- chan0
rest[6]   = 00
rest[7]   = channel index 1    (byte) STEREO ONLY  <- chan1   (mono OMITS rest[6:8])
rest[..]  = 00 padding up to the 0x2a marker
2a 00 00 00
ID (8 bytes)  -- per-track unique id (e.g. 51c33be6 d17ee44a); constant per track
              position across the whole stereo series; differs per session
post-ID routing (stereo 20B): 01 00 01 00 00 00 00 00 <chan0> 00 00 00 <chan1> 00 00 00 01 00 00 00
                              (mono drops the <chan1> group of 4 bytes)
```
So stereo→mono `0x1014` = set rest[0]=0, rest[1]=1; delete the `00 <chan1>` pair
before the 0x2a marker; delete the `<chan1> 00 00 00` group in the post-ID region.
Net −6 bytes.

### 2. `0x1052` audio lane — one block per channel
Stereo emits two `0x1052` (both 19B, identical content `52 10 <len> "name" 00 00 00 00 01 00`); mono emits **one**. Transform = drop the track's 2nd `0x1052`.

### 3. `0x261c` audio playlist (mono 1818B vs stereo 1857B, Δ−39)
The only structural difference is in the `0x260d` output-routing subtree:
stereo has **two** active `0x260c` (sz55, each wrapping a `0x260a` routing entry,
32B); mono replaces the **second** `0x260c` with an empty one (sz16, content
`0c 26` + 12 zero bytes, **no** `0x260a` child). `0x260c` 62B→23B = −39. The GUID/
ID-bearing `0x102d`/`0x2619`/`0x4301` subtree is byte-identical (only per-session
ID bytes differ, same as everywhere). `0x251a`/`0x210b`/`0x2589`/`0x2619` are
IDENTICAL between mono and stereo audio (the 78/79 & 48/49 size diffs seen earlier
were MIDI-vs-audio, not mono-vs-stereo).

### 4. Session channel-count field
`0x1054 + 2` = **total audio channels** (no-click 1+2=3; n6 6×2=12). Mono adds 1,
stereo adds 2. (Also: `0x1015 + 2` = audio-track count [excludes MIDI];
`0x2107 + 11` = total-track count [incl. MIDI].) These decouple under mix-and-match.

## ✅ MIX-AND-MATCH (mono + stereo, any order) — PT-CONFIRMED (2026-05-29)

`synth_stereo_mono_stereo.ptx` `[S,M,S]` and `synth_mono_stereo_mono.ptx`
`[M,S,M]` both open in Pro Tools. Decoded + built:
- **Channel allocation is cumulative by track order** (`[S,M,S]` → ch [0,1],[2],[3,4]).
- **Channel indices live ONLY in `0x1014`** (not in `0x261c`); rewritten via
  `set_track_channels` at name/marker-relative positions (pre chan0 = `blk+namelen+11`,
  post chan0 = `marker+19`; stereo chan1 = `+namelen+13` / `marker+23`).
- **`0x1054` body field = total audio channels** (sum of every track's channels;
  a too-small value → "end of stream"). `compose_index(channels=…)` passes the
  added track's channel count (the `0x1054` index record gains that many markers).
- **Path-normalization** (combining tracks from different source folders) via
  `rename_track`, which now also fixes the content-internal `5a 0a 00 <u32>`
  path-wrapper length fields that the block parser doesn't see (a stale one →
  "end of stream").
- **Counters are NOT load-critical** (a broken/inconsistent counter sequence loads
  fine — confirmed across multiple PT tests); the earlier "end of stream" was the
  path-wrapper, not the counters.

REMAINING for *fully arbitrary* configs (no matching control): the overview
display-order permutation is a hash-iteration order that depends on the exact
type sequence (`[S,M,S]`=[1,2,0] vs `[M,S,M]`/uniform=[1,0,2]) — currently copied
from a matching control; needs generation. The full orchestration still lives in
`/tmp/make_mixed.py` (helpers are in the module); porting to a clean
`synthesize_mixed_session` is the next step.

## ✅ MONO SYNTHESIS — PT-CONFIRMED (2026-05-29)

`synth_3_mono.ptx` and `synth_6_mono.ptx` **both load perfectly in Pro Tools.**
Empty-mono synthesis joins empty-stereo as a confirmed capability.


The user created the mono count-series `control_files/lots of mono tracks/{0..6}
mono tracks.ptx`, which resolved both blockers below: (1) the real mono `0x1014`
post-ID region is **all zeros** — that IS the mono layout (no routed-mono
mystery), and (2) the mono control's own `0x0002` index is the model (and
`compose_index` works unchanged since tracks are still named `Audio N`).

`body_synth` was refactored channel-aware (stereo path unchanged — the 2→3 stereo
synth is still byte-identical to the PT-confirmed `synth_3_stereo_v6.ptx`):
`extract_track(data, track, total, channels)`, `grow_one_track`,
`_patch_counts(body, n, n_channels)`, shared `_synthesize_session(..., channels)`,
and public **`synthesize_mono_session(...)`** (channels=1). Validated: mono
2→3/2→4/3→5/3→6 match the real control's per-type block counts, reload cleanly,
are all-mono, and `channel_count == N`. Wrote `control_files/synth_3_mono.ptx`
and `synth_6_mono.ptx` — **awaiting Pro Tools load confirmation**.

The mono per-track unit == the stereo unit except: `0x1052` 1 lane (not 2),
`0x260a` 4 (not 5; the emptied 2nd `0x260c`), `0x1014` 52B (not 58B), and the
session field `0x1054 = N` (not 2N).

### Index bug found + fixed (the first mono attempt failed PT)

The first `synth_*_mono.ptx` failed Pro Tools with *"magic ID does not match while
translating Audio Playlists"* even though ptxformatwriter reloaded it fine. Cause:
`final_index.add_stereo_track` hard-coded **2** markers for the `0x1054`
channel-container record per track (stereo has 2 channels). Mono has 1, so the
mono index carried one extra offset (249 vs the real 248); inserting it early
shifted every later record and PT's strict block-magic check landed on a bad magic
in the `0x261c` audio-playlist section. Fixed by making it channel-aware:
`add_track(records, new_count, channels)`, threaded through
`synthesize_index_records` and `compose_index(..., channels)`.
`compose_index(channels=1)` now reproduces the real mono index **byte-exact**.
**Lesson: validate the synthesized index byte-exact against the real control — a
ptxformatwriter reload is too lenient (it ignores the positional checks PT enforces).**

## (historical) TWO BLOCKERS that needed a clean mono control — now RESOLVED

1. **Routing region ambiguity.** no-click's mono `Audio 1` is **unrouted** — its
   `0x1014` post-ID region and its `0x260a` route bytes are all-zero — while every
   stereo control is **routed** (`01 00 01 … 9cff/6400 …`). So the existing files
   never show a *routed* mono track, and a freshly created PT track normally routes
   to the main output. Need a routed-mono example to know the correct bytes.
2. **Index drops references.** Converting stereo→mono *removes* blocks (one
   `0x1052`, one `0x260a`); the trailing `0x0002` index references them, so it must
   drop those entries + reflow offsets. `final_index.compose_index` is currently a
   stereo *grow* (maps `Audio N` by occurrence); it needs a mono donor/target to
   produce a correct mono index. A real mono control gives both the correct mono
   body structure (byte-level, incl. routing) AND a correct mono index to model.

## What would make this clean (control-file ask)

Mirroring the stereo series, ideally saved in ONE folder/era (same volume as the
stereo controls so the embedded session path matches):
- **`N mono tracks.ptx` for N = 1..4** (highest priority — unblocks both blockers:
  clean diff vs `N stereo tracks.ptx`, routed-mono bytes, and a real mono index).
- `N MIDI tracks.ptx` for N = 1..4.
- A click series: click-only; click + 1 audio; click + 2 audio.
- For mix-and-match later: a few mixed sessions in known orders (e.g. mono+stereo,
  stereo+mono+stereo) to decode channel allocation across types.
