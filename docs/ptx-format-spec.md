# The Pro Tools `.ptx` session format — a reverse-engineered specification

This is a from-the-ground-up technical specification of the Pro Tools session file format
(`.ptx`, the "PTF v8+" little-endian generation, as written by Pro Tools 10 and later),
derived from an extensive byte-exact reverse-engineering effort to *read* and *write*
sessions Pro Tools accepts.

It is intended to be complete enough to implement a reader **and** a writer. Where a fact
was confirmed by Pro Tools opening a synthesized file, it is marked **(PT-confirmed)**.
Where it is read-side only (the lenient reader accepts it, but PT's exact requirement is
unverified), it is marked **(read-side)**.

> **The cardinal rule for writers.** The lenient reader (and your own model) will happily
> accept files Pro Tools rejects with *"end of stream encountered"* or *"magic ID does not
> match"*. The only ground truth is Pro Tools opening the file. Every structure below that
> a writer must get exactly right was validated by reproducing a real PT-authored session
> **byte-for-byte** (modulo GUIDs, nonces, and per-file identity).

---

## 1. File layout at a glance

```
+-----------------------------------------------------------+
| 20-byte plaintext preamble (incl. XOR descriptor bytes)   |
+-----------------------------------------------------------+
| XOR-obfuscated body (everything from offset 0x14 onward): |
|   block | block | block | … | master index (0x0002 block) |
+-----------------------------------------------------------+
```

- Bytes `0x00..0x13` are **not** obfuscated; they include the XOR descriptor (§2).
- Everything from `0x14` to EOF is XOR-masked. De-mask it to get the **body**.
- The body is a flat-then-nested stream of **blocks** (§3), ending with the **master
  index** block (`content_type == 0x0002`, §11), a pointer table referencing other blocks
  by absolute file offset.

A companion sidecar, **`WaveCache.wfm`** (§13), holds waveform overviews and lives next to
the `.ptx` in the session folder. Audio lives in `Audio Files/` as BWF WAVs (§7).

---

## 2. The XOR obfuscation layer

Bytes `0x14..EOF` are masked with a repeating 256-entry key. To de-obfuscate:

```
is_bigendian = bool(byte[0x11])          # 0 for the v8+ little-endian generation
xor_type     = byte[0x12]
xor_value    = byte[0x13]

if xor_type == 0x01:  delta = gen_xor_delta(xor_value, 53, negative=False)
elif xor_type == 0x05: delta = gen_xor_delta(xor_value, 11, negative=True)
else: unsupported

key[i] = (i * delta) & 0xFF                for i in 0..255
for i in 0x14 .. len-1:
    idx = (i & 0xFF)            if xor_type == 0x01     # byte-cyclic
        = ((i >> 12) & 0xFF)    if xor_type == 0x05     # 4 KiB-paged
    out[i] = byte[i] ^ key[idx]

def gen_xor_delta(xor_value, mul, negative):
    for i in 0..255:
        if (i * mul) & 0xFF == xor_value:
            return (-i) & 0xFF if negative else i
    return 0
```

Re-obfuscation is the same operation (XOR is its own inverse): mask `out[0x14:]` with the
same key. A writer that only *grows/splices* an existing session re-uses that session's
`xor_type`/`xor_value` unchanged.

---

## 3. Blocks

The body is a stream of blocks. Each block:

```
offset  size  field
  +0     1    0x5A          marker ("Z")
  +1     2    btype:u16     block subtype (LE)
  +3     4    block_size:u32  bytes AFTER this 7-byte header (LE)
  +7     2    content_type:u16  what the block IS (LE)  ← the important one
  +9     …    payload       (block_size - 2 bytes)
```

- **Total block length** = `block_size + 7`. A block spans `[start, start + block_size + 7)`.
- Blocks **nest**: a block's payload can contain child blocks (each a full `0x5A…` block).
  Containers are walked by scanning for `0x5A` markers within the parent's bounds.
- Convention used throughout this repo: a parsed block's `offset` points at its
  `content_type` (i.e. `start + 7`); its raw bytes are `data[offset-7 : offset+block_size]`.

To enumerate **top-level** blocks, walk from the body start, reading `block_size` to skip
to the next block, until the `0x0002` master index (always last).

---

## 4. Primitive encodings

| Type | Encoding |
|---|---|
| Integers | little-endian unsigned (`u16`, `u32`, `u64`); some fields are 5-byte LE (`read5`) |
| Strings | `u32 length` + `length` bytes, **latin-1**, no terminator (length-prefixed). Some fixed-width fields are zero-padded. |
| Ticks (musical position) | a **5-byte LE** value equal to `ZERO_TICKS + pt_ticks`, where `ZERO_TICKS = 0xE8D4A51000`. Subtract `ZERO_TICKS` to get PT ticks. |
| GUID / nonce | 16 raw bytes, session-unique; writers may use any deterministic value (PT does not validate them across sessions). |
| Windows FILETIME | `u64` = 100-ns ticks since 1601-01-01 UTC = `round((unix_mtime + 11644473600) * 1e7)`. |
| Three-point (region geometry) | a **variable-width** start/offset/length triple (§9). |

### Time and sample units

- **PT ticks**: `960000` ticks per quarter note. (MIDI tick → PT tick = `midi_tick *
  960000 / division`, where `division` is the SMF ticks/quarter.)
- **File samples**: clip timeline positions are in **audio sample frames** at the session
  sample rate (typically 44100). They are **not** ticks. The "head-sync" point of a song
  is converted from ticks → seconds (by integrating the tempo map) → file samples.
- Musical-position fields (tempo/meter/marker) use the 5-byte tick encoding above;
  audio-clip positions use 8-byte LE file-sample counts (§8).

---

## 5. Content-type catalog

Significant `content_type` values (those a reader/writer must understand). Names follow the
upstream parser; structure notes are from this project's decode work.

### Session / info
| Type | Meaning |
|---|---|
| `0x0030` | INFO product and version |
| `0x1028` | INFO sample rate |
| `0x2067` | INFO session name + path (the session's own identity string) |
| `0x0002` | **Master index** (final block; pointer table — §11) |

### Audio files
| Type | Meaning |
|---|---|
| `0x1004` | WAV file table (count-prefixed: `u32 count` + `0x103a` name list + N × `0x1003`) |
| `0x103a` | Audio-file **name list** + the session's `Audio Files/` path trailer (§7) |
| `0x1003` | WAV **descriptor** (per file: ordinal, length, UMID identity, source mtime — §7) |
| `0x1001` | WAV samplerate/size (child of `0x1003`; holds the u32 sample length @+15) |
| `0x2106` | A second UMID copy inside `0x1003` (the `00`-prefixed one — §7) |

### Regions (clips' source windows)
| Type | Meaning |
|---|---|
| `0x262A` | AUDIO region list (count-prefixed: `2*N` region records for N stereo clips) |
| `0x2629` | AUDIO region (name, channel, length, GUID, **findex** → file — §6, §9) |
| `0x2628` | Region name/group sub-block (inside `0x2629`) |

### Placements (regions on the timeline)
| Type | Meaning |
|---|---|
| `0x1054` | AUDIO region→track full map (all lanes) |
| `0x1052` | per-lane region→track map entries |
| `0x1050` | a placement entry |
| `0x104F` | placement sub-entry: **channel/region index** + **timeline position** (§8) |

### Tracks
| Type | Meaning |
|---|---|
| `0x1015` | AUDIO track list; `0x1014` = an audio track's name/number |
| `0x2519` | MIDI/Click track list; `0x251A` = a MIDI/click track's name/number |
| `0x261B` | per-track cumulative counter |
| `0x261C` / `0x261E` | track **playlist** records (audio / click) — referenced by the index for display order |
| `0x2627` | per-track routing/view subtree root |

### Conductor (tempo / meter / markers)
| Type | Meaning |
|---|---|
| `0x2028` / `0x2718` | TEMPO map / tempo lane (61-byte records — §10) |
| `0x2029` / `0x2719` | METER map / meter lane (36-byte records + 16-byte lane entries — §10) |
| `0x2030` / `0x2077` | MARKER list / marker record (§10) |

### Paths
| Type | Meaning |
|---|---|
| `0x0F3D` | Volume + mount-path block for the `Audio Files` location |
| `0x0F3C` | Audio-files path **marker** (one per distinct audio-files path; added to the index) |

### MIDI (read-side detail)
| Type | Meaning |
|---|---|
| `0x2000` | MIDI events block |
| `0x2001`/`0x2002`, `0x2633`/`0x2634` | MIDI region name/maps (v5 / v10) |

### Display / view-state (PT recomputes; writers can leave conservative values)
`0x2624` (playlist/edit-window order — §12), `0x2587`, `0x2016`, `0x2519`-adjacent view
blocks, `0x2519`/`0x2624` child tables. These are sensitive to the index (§11).

---

## 6. Audio-file linking (the part that makes clips play the right file)

A clip on the timeline resolves to audio through three linked structures:

```
0x1054 placement  --(payload[2] = region index)-->  0x262A region list
   0x2629 region  --(findex = 0-based index)------>  0x103a file list / 0x1004 descriptors
   0x1003 descriptor  --(UMID identity)---------->   the BWF WAV's `umid` chunk on disk
```

### The region → file index (`findex`) — **(PT-confirmed)**

A `0x2629` region links to its audio file by a **0-based index** (`findex`) into the
`0x103a` filename list (equivalently, into the `0x1003` descriptor order — they are
parallel). **It is stored twice:**

- once as a `u32` immediately **after the `0x2628` name sub-block** inside the region
  (this is the copy the reader/region-list uses); at region-relative offset `16 + (the
  0x2628 sub-block's size)`.
- once in the region's fixed trailer at **`region_len - 8`** — **this is the copy Pro
  Tools resolves for playback.**

Setting only the first gives correct region *names* but plays the first file on every
track (the "all tracks play the same stem" failure). **A writer must set both.**

### The descriptor (`0x1003`) — **(PT-confirmed)**

Within a 321-byte stereo descriptor (offsets relative to the descriptor block start):

| Offset | Field |
|---|---|
| `+9` | wav ordinal (1-based) |
| inside the `0x1001` child, `+15` | sample length (`u32`) |
| `+44` | UMID material (8 bytes, `2a <hash4> ef <b> 80`) — the `0x1001` copy, keeps its `2a` |
| `+100`, `+172` | source-WAV mtime as Windows FILETIME (`u64`) |
| `+182` | `0x01` (a flag Pro Tools sets on import; a clean template has `0x00`) |
| `+259` | `0x00` (a fresh import zeroes this `u64`; a stale template has an old timestamp) |
| `+292` | UMID material again, but **`00`-prefixed** (`00 <hash4> ef <b> 80`) — the `0x2106` copy |
| `+301` | 2-byte secondary id |

The UMID is what Pro Tools matches the on-disk WAV against; the mtime/flag fields are what
it keys waveform-cache **freshness** on (§13).

### The filename list (`0x103a`) — **(PT-confirmed)**

```
header: <u32 8+D> 01 <u32 7+D> <u32 11> "Audio Files" 00 00 00 00
entries (D of them, in file order):
        02 00 00 00 00 <u32 namelen> <name bytes> "EVAW"
path trailer:
        00 FF FF FF FF <u32 vollen> <volname> <u32 volID>
        then path components: 01 <u32 idx> <u32 len> <name> 00 00
```

`D` = number of files. The path-trailer component indices **continue from D** (`D+1, D+2,
…`); using a control's verbatim indices at a different `D` makes Pro Tools throw
`out_of_range`. Multiple clips of the **same** WAV share one descriptor/filename and add
exactly **one** `0x0F3C` index marker regardless of clip count.

---

## 7. The audio WAV (BWF/UMID) requirements

A WAV that Pro Tools will link must be a BWF carrying a UMID. A raw `fmt`+`data` WAV is not
enough. The fields Pro Tools matches on:

- **`fmt `** — PCM format (the clip controls are 44.1 kHz / 24-bit / stereo).
- **`data`** — the audio; `sample_count = data_size / block_align`.
- **`umid`** chunk — its 8-byte body `2a <hash4> ef <b> 80` is the file's content id
  (mirrored into the `0x1003` descriptor, §6).
- **`bext`** — Broadcast extension; the SMPTE-UMID marker `06 0a 2b 34` appears here.
- **`regn`** — region/overview metadata (carries the UMID material + frame count).

A writer that converts arbitrary audio (e.g. via ffmpeg → raw 24-bit WAV) must **wrap** it
into this BWF/UMID structure (graft the raw `data`/`fmt` into a known-good PT WAV
template, writing a fresh, content-derived UMID consistently into `umid`/`regn`/`bext`).

---

## 8. Clip placement (`0x104F`) — **(PT-confirmed)**

The `0x1054` map contains, per lane (one per channel; a stereo track has two lanes), a
`0x1052` → `0x1050` → `0x104F` chain. Within a `0x104F` payload:

| Payload offset | Field |
|---|---|
| `+2` | **channel / region index** = `2 * region_index + channel` (single-clip: 0 = `.L`, 1 = `.R`; multi-clip: a global lane/region selector) |
| `+7 .. +15` | **timeline position**, an 8-byte LE count of **file samples** (not ticks) |

Lanes are emitted in lane-major order; a lane carrying K clips has K `0x1050` placements
and the 2-byte lane trailer appears **once** at the lane's end (not per placement —
emitting it per placement is an EOS/"Audio Playlists magic ID" bug). Moving a clip is a
size-neutral edit of `payload[+7:+15]`; no reindex needed.

---

## 9. Region geometry: the variable-width "three-point" — **(PT-confirmed)**

A `0x2629` region's **length** (and start/offset) is **not** a fixed-width integer. It is
an Ardour-style three-point encoding placed just after the region name:

```
let j = 22 + namelen                 (region-relative; name is at +22)
byte[j+1] high nibble = offsetbytes   (# bytes for the sampleoffset value)
byte[j+2] high nibble = lengthbytes   (# bytes for the length value)
byte[j+3] high nibble = startbytes    (# bytes for the start value)
values are packed LE at j+5 in the order: offset, length, start
```

So a clip ≤ 65535 samples uses `lengthbytes = 2`; longer clips need 3–4 bytes, and the
nibble **must** widen accordingly. Writing a fixed 2-byte length silently truncates any
clip over ~1.5 s to its low 16 bits (the "region only 0.9 s long" bug). Other region
fields, in the 6-char-name template layout: **channel** at `+78`, region **GUID** at `+97`,
**name** (length-prefixed) at `+22`. The two `findex` copies are at `16 + size(0x2628)` and
`region_len - 8` (§6).

---

## 10. Conductor records

### Tempo (`0x2028` map / `0x2718` lane) — **(PT-confirmed)**

Block content = `u32 payload_len` + `u32 count` + `count × 61-byte records`, with
`payload_len == 4 + count*61`. Each 61-byte record:

| Record offset | Field |
|---|---|
| (near start) | the literal text `Const` … `TMS` framing |
| `+30` | musical position: 5-byte LE tick (subtract `ZERO_TICKS`) |
| `+40` | **BPM** as IEEE-754 little-endian `double` (f64) |
| `+48` | PPQ (`u32`) |

Hundreds of tempo events are normal (one per beat-map point). The map is mirrored in the
`0x2718` lane.

### Meter (`0x2029` map / `0x2719` lane) — **(PT-confirmed)**

Block content = `… u32 count …` + `count × 36-byte records`. Each 36-byte record:

| Record offset | Field |
|---|---|
| `+0` | musical position: 5-byte LE tick (subtract `ZERO_TICKS`) |
| `+8` | ordinal (`u32`) |
| `+12` | numerator (`u32`) |
| `+16` | denominator (`u32`) |

Each meter event also has a 16-byte entry in the `0x2719` trailing lane list.

### Markers (`0x2030` list / `0x2077` record) — **(PT-confirmed)**

`0x2030` = `u32 count` + N × `0x2077`. Each `0x2077` marker record:

| Record offset | Field |
|---|---|
| `+9` | ordinal |
| `+15` | name length (`u32`) |
| `+19` | name bytes |
| `name_end` and `name_end + 8` | position: `ZERO_TICKS + tick` as a 5-byte value (two copies) |
| `name_end + 166` | 16-byte GUID |

---

## 11. The master index (`0x0002`) — Pass 2

The final block is a **pointer table**: it references indexed blocks by **absolute file
offset**. Any body edit that changes a byte count shifts those offsets and the index must
be rebuilt. **This is the single hardest part of writing a `.ptx`** and the most common
source of `EOS` / `magic ID` failures.

### The right model: "holes," not guessing

An **offset hole** is a `u32` in the index that stores a block offset. Identify holes and
their targets **structurally**, never by inspecting the stored value (a count or length can
coincidentally equal a block offset — value-based offset detection is undecidable and
corrupts files). Two kinds of hole:

- **childref** — the `u32` in a container record's child reference.
- **marker element** — each of the `k` `u32` offsets in a marker/table element. `k == 1` is
  the familiar `01 04 00 01 00 <offset>`; `k > 1` is an offset table.

Each hole's **target** is identified by *logical identity*: the `content_type` of the block
it points at, and that block's **rank among blocks of the same type in file-offset order**.
To repair after an edit, re-resolve every hole's `(content_type, rank)` against the new
block layout and write the fresh offset. (See `final-index-0x0002-schema.md` for the full
record grammar; `ptxformatwriter/final_index.py` for the reference implementation.)

For content insertion that grows blocks without adding indexed records (clips, tempo,
markers), capture holes from the pre-resize (still-parseable) index and refill them in the
resized layout. For **track-count** changes, index *records* must be added first (clone an
existing track's record, fix ordinals + child-refs/childtype).

---

## 12. Track display order

The edit-window **track order** is governed by a **playlist-order list inside the master
index** (`0x2624`-related), not by the order of track blocks in the body. Reordering tracks
(e.g. moving a Click track to the top) can be done by rewriting that list alone — the body
blocks can stay in creation order. This index-only reorder is **name-independent** and robust
to renamed tracks. **(PT-confirmed)**

A separate gotcha lives in the *click splice* (not the reorder): a structural click-clone
matches the target's audio tracks by their canonical `Audio N` names, so splicing a click
onto already-renamed tracks silently fails to find them. The fix is to normalize the audio
tracks to `Audio N` for the splice and restore the names after — which is what the library's
`add_click_anyN` now does, so rename order no longer matters in practice.

---

## 13. The waveform-overview cache (`WaveCache.wfm`) — **(PT-confirmed)**

Pro Tools draws waveforms from a session-folder sidecar `WaveCache.wfm`, built only on
Import/Recalculate (never on plain Open). Generating it lets a synthesized session open
with waveforms drawn. Layout:

```
header(80): "DDZCHX" + u16 ver(=1) + u32 data_start(=80) + u32 index_off + 0 + u32 index_len + zeros
12 zero bytes
per-file data block × N:
   AnalysisSetsHdr { u32 count=nchannels; AnalysisSet × nch; 12-byte trailer 00000000 ff*8 }
   AnalysisSetsHdr footer (payload 00*8 ff*8)
   8 zero bytes
CAHIDX index: "CAHIDX" + u16 ver(=2) + u32 size + u32 count + entries
```

Every record is `name + u16 ver + u32 size + payload[size]`. Per audio channel, an
`AnalysisSet` wraps a `PacketStreamSetHdr` containing, for **two zoom levels** (256 and
16384 samples per overview point):

- `PacketStreamIndxHdr` (52-byte payload: a constant 16-byte stream-id per level, the data
  size, the point count, and `w11` = the absolute cache-file offset of this stream's peak
  bytes),
- `PacketStreamIndx` (`[4, samples_per_point, 0, 0]`),
- `PacketStreamData` (`u32 size` + the peaks).

**Peaks**: each overview point is `(max:int16, min:int16)` where the int16 =
`clip(round(sample_24bit / 256), -32768, 32767)`. Point count = `ceil(nsamples / spp)` (the
final partial window still yields a point).

**CAHIDX entry** (per file): `00*7 + UMID(8) + 00 + u32 namelen + name + u64 ft1 + u64 ft2 +
u64 filesize + 00*8 + u32 data_offset + 00*4 + u32 data_length + ff*8`. Pro Tools matches
cache entries to files **by UMID** (so reopening a session after one recompute always
draws); `ft1`/`ft2` are the source mtime FILETIME, `filesize` the referenced WAV's size.

---

## 14. Implementation guidance for writers

1. **Grow from a real control session.** Synthesizing every block from a model drifts from
   Pro Tools' exact byte layout and fails at scale. Start from a session PT wrote, change
   the minimum, and validate byte-exact against a control pair.
2. **Treat Pro Tools as the only oracle.** A clean parse / round-trip through your own code
   is not acceptance. Confirm by opening the output in Pro Tools.
3. **Rebuild the index deterministically** (§11) — never guess offsets from values.
4. **Order operations sensibly**: tempo/meter/markers → clips → click/reorder → track names
   is a clean default. Rename order is not load-bearing for the click (the splice normalizes
   audio names internally, §12); other reorder passes still assume canonical layouts.
5. **GUIDs/nonces are free** — any deterministic value works; PT doesn't validate them
   across sessions. Identity that *does* matter: WAV UMIDs, the region `findex`, descriptor
   mtimes (for the wave cache), and every index offset.

---

## 15. Further reading (in this repo)

- `final-index-0x0002-schema.md` (archived in `unused/docs/`) — the index record grammar.
- `gen1-vs-gen2-architecture.md` — why the index is rebuilt deterministically, not guessed.
- `ptxformatwriter/core.py` — the reference reader; `ptxformatwriter/body_synth.py` — the reference
  writer toolkit; `ptxformatwriter/final_index.py` — the holes-model index rebuilder;
  `ptxformatwriter/wavecache.py` — the `WaveCache.wfm` generator.
