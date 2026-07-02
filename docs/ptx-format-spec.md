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
| `0x2628` | Region **record** (inside `0x2629`): a length-prefixed **name** (`<u32 len @+9><name @+13>`) — the clip/source name shown in the bin (e.g. `voc 1_01`, `VPS kit 1.grp.L`), present on **100%** of corpus records — followed by geometry + fade children (`0x2523`→`0x2526`). `body_synth.region_names()` reads them. |

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

## 5b. Block grammar — corpus-derived containment

Walking every block by declared size (not by scanning for the `0x5A` magic — see §14) on
a 26-session / ~1.4M-block corpus yields a stable **containment grammar**: which
content-type nests inside which. The dominant parent→child edges below (each holding on
≥20 of 26 sessions) map the format's skeleton; `(NEW)` marks types not previously cataloged.

**Audio files** — `0x1004` → `0x1003` (descriptor) → { `0x1001` samplerate · `0x1033` `(NEW)`,
1:1 per descriptor · `0x2106` UMID }.

**Regions** — `0x262A` (list) → `0x2629` (entry) → `0x2628` (record: name + geometry) →
{ `0x2523` → `0x2526` crossfade pair `(NEW)` · `0x2636` `(NEW)` }. A parallel per-track
name subtree: `0x2627` → `0x2625` `(NEW)` → `0x2626` `(NEW)` (the highest-count records
after placements; one `0x2626` per region instance).

**Placements (clips on the timeline)** — `0x1054` (track audio container) → `0x1052` (lane,
holds the u32 placement count) → `0x1050` → `0x104F` (region-ref @+11, 8-byte position @+16).
A second placement path `0x1057` → `0x1056` `(NEW)` → `0x104F` also occurs (~1.6% of
placements). See §8 and `body_synth.clip_lanes` / `remove_clip`.

**Tracks** — `0x1015` → `0x1014` (audio track list/name); `0x2519` → `0x251A` → `0x4420`
`(NEW)` (MIDI/click). The per-track detail subtree under `0x261B` is large and mostly
view/automation state: `0x261B` → { `0x260D` `(NEW)` → { `0x260A` · `0x260C` · `0x260E` }
`(NEW)` · `0x102D` → `0x2619` → `0x4301` `(NEW)` · `0x1029` `(NEW)` · `0x2627` }.

**Conductor** — `0x2028`/`0x2718` tempo, `0x2029`/`0x2719` meter, `0x2030`/`0x2077` markers
(§10). `0x2077` → `0x2506`: a **fixed 17-byte scaffold** (constant payload
`…06 25 FF FF FF FF 00 00 00 00`), emitted **track-count** times per marker — *not*
waveform data (see §5c).

**View / display (PT recomputes — leave conservative)** — `0x200A`/`0x200B`/`0x2015` →
`0x2038` `(NEW)` → `0x2037` `(NEW)`; `0x203B` view-volume signature (§ click/view fix),
under `0x2580` automation lane or the `0x2015` display chain; the large `0x2613`/`0x2615`/
`0x2616` `(NEW)` view blocks (median ~850 B).

> Caveat: the size-driven walk still admits <1% phantom blocks (data bytes that frame like a
> small block), so rare edges may be noise; the high-frequency edges above are reliable.

---

## 5c. Corpus-verified record details (2026-06-30 dissection)

A parallel dissection of the 26-session corpus, each finding adversarially re-derived on
fresh sessions, pinned the following. Two are confirmed against **corpus ground truth**
(a renumbered session; the bak.075→076 add-clip pair).

**Placement → region link (`0x104F` +11) — confirmed.** The `u32` at block offset **+11**
is a **0-based index into the `0x2629` region-instance list** in file (block-scan) order;
the clip's source name is the `0x2628` name nested in that `0x2629`. (NOT the raw all-`0x2628`
order — that mis-resolves on sessions with standalone regions.) Ground truth: the two clips
that bak.076 adds resolve to the two regions it adds (`Reverse Rewire_M1.1_16.L/.R`), and a
`Wolf Wet` track's clips resolve to `Wolf Wet.L/.R`. Refs are **positional** — inserting a
region before index N renumbers every ref ≥ N (load-bearing for add-clip). →
`body_synth.clip_names(data, lane_zmark)`.

**Session start bar (`0x2029` meter) — confirmed.** Pro Tools can renumber a session to start
at an arbitrary bar (`-2`, `0`, `2`, …). The start bar is a **signed `i32` at the first meter
event's +8** (block payload **+23**) of the `0x2029` "Meter" block — header
`"Meter"(5) | 0x0002:u16 | plen:u32 | count:u32`, with `plen = 12 + 52·count` and events at
payload+15. `=1` for a default session; the renumbered corpus session **THE WIND reads `-2`**.
The bar-1 tick origin `0xE8D4A51000` is *unchanged* by renumbering. →
`body_synth.session_start_bar(data)`. (Multi-event meter records are variable-length; only
event 0 — the session start — is mapped. See `TODO.md`.)

**Track subtree (`0x261B`).** Exactly **one `0x261B` per track of any type** (audio / MIDI /
click / aux / bus / master) → count == total track count. Each has one `0x102D` (track name in
a nested `0x2619`, then an 8-byte track **GUID** after a `2A 00 00 00` tag), one `0x2627`, and
N `0x260D` routing nodes. Cross-track linkage is by the 8-byte GUID, not a numeric index.
`0x260A` = a 32-byte automation-breakpoint frame (5-byte tick @+18, `i16` value @+25).
Groundwork for add/remove-track.

**Scaffolding records (constant, data-free).** `0x1033` — fixed 9-byte block
`5A 02 00 02 00 00 00 33 10`, one per `0x1003` descriptor. `0x2626` — 2-byte empty terminator;
`0x2625` — 11-byte wrapper of one empty `0x2626`; `0x2627` — container with an explicit
`u16` slot-count (= 11) at payload **+9**, children starting at **+11**. These carry no region
data (rules them out as a clip-name store). A writer emits them as constants; a reader skips them.

---

## 5d. Round-2 dissection — editing-relevant records (2026-06-30)

A second corpus wave (correlation-driven, adversarially verified). Confidence is marked;
refuted over-claims are noted so they aren't re-trusted.

**Session sample rate & bit depth (`0x1028`).** *(verified, all 26 sessions)* sample rate =
`u32` at block **+11**; recording bit depth = `u8` at block **+(size+2)** (5 before the
payload end): `0x18`→24-bit, `0x20`→32-bit float. → `session_sample_rate()`,
`session_bit_depth()`. (The `0x1028` payload may carry an optional embedded "IO Settings"
path before that byte; it stays end-anchored.)

**Clip placement (`0x104F`) — consolidated.** *(verified high)* `+11` `u32` = region index
into the `0x2629` list (→ `clip_names`); `+15` = const `0x00`; `+16` `u64` = timeline
position (→ `clip_positions` / `set_clip_position`). For a **sample**-based track the u64 is
plain samples (base 0); for a **tick**-based track it is `0x4000000000000000 + 0xE8D4A51000 +
tick_offset`, with the top byte at `+23` a timebase marker (`0x00`=sample, `0x40`=tick; other
nibbles occur — treat as opaque). Two block variants coexist: subtype `0x0008`/size 34 and
subtype `0x000A`/size 37 (3 extra tail bytes); `+11` and `+16` hold in both.

**Fades / crossfades.** *(verified high)* Simple clip fades sit in a contiguous `0x262F` run
bracketed by a `0x2630` opener (entry count = `u32` @ payload+9, == run length, cross-checked
on 7 sessions) and a `0x262E` closer, immediately before the placement list. Each `0x262F`:
marker `@payload+5` (low nibble = fade-IN byte-width, high nibble = fade-OUT width, 0 =
absent), then LE IN then OUT lengths in **samples**, then a 2-byte curve-shape pair (trailer
length set by `shape_a` alone: 01→20 B, 02→13 B, 03→10 B). Adding one clip adds exactly one
`0x262F`. Richer crossfades are `0x2523`→`0x2526` records nested `0x262C`→`0x262B`→`0x2628`.
(`0x2423` is **not** a fade — it holds track-**group** name strings.)

**Plugins / I-O — the preserve-precisely set.** *(verified high)* `0x1017` is the session's
plugin-type **catalog** (blocktype `0x04` older / `0x06` newer): per entry a category byte, a
length-prefixed display name (e.g. `Altiverb 7`), a 12-byte plugin id, and a 4-byte (in,out)
IO; the entry count tracks plugin richness. Per-instance plugin **state** nests
`0x2616 ⊃ 0x2615 ⊃ 0x2613 ⊃ 0x1038` (all counts == plugin-instance count). The I-O routing
table is a per-session singleton `0x2603 ⊃ 0x2602` (length-prefixed paths) `⊃ 0x2601` (1:1).
All are byte-stable under clip/region edits → copy verbatim. **Do not** assume a fixed safe
prefix when relocating a plugin blob: the churn boundary is plugin-specific and can begin
near the head. (The literal `0x4403/0x4420/0x4301` codes do *not* exist as standalone blocks.)

**Region geometry (`0x2628`) — descriptor model (PARTIAL — not add/replace-ready).** After
the name (`noff = z+13+namelen`) a 5-byte descriptor `[b0..b4]` precedes variable-width LE
sample fields whose byte-widths are the **nibbles of `b1,b2,b3`** (slot order
`[b1.hi,b1.lo,b2.hi,b2.lo,b3.hi,b3.lo]`, 0 = absent); `b4` is a form marker (`0x08` trimmed /
`0x00` whole-file). The model reproduces exactly on clean audio families (final field = clip
length in samples), **but** the slot→meaning mapping is not stable across all subtypes
(grouped `.grp` regions and some `0x1004` record types break it), and the "final field ==
source-file length" correlation was **refuted** as universal. See `TODO.md`.

**Automation (`0x260A`) — correction.** `0x260A` is **variable-length** (not a fixed 39-byte
single point): it holds an inner breakpoint **array** (count near payload+10; ~6 B/breakpoint
+ a 6-byte terminator). `flag@payload+8` ⇄ tick-present is exact; `tick@payload+18` is a
5-byte absolute conductor tick. Full per-parameter lane mapping still open (`TODO.md`).

---

## 5e. Edit footprints — from backup-chain diffs

Consecutive `.bak.NNN` saves are single real Pro Tools edits. Diffing block-type counts
across the chains (Reverse Rewire 073–079, #Hipsters 040–046) gives the byte footprint of
each operation — the recipe a writer reproduces.

| Edit | Block-count delta |
|---|---|
| **Remove clip** | −1 `0x104F`, −1 `0x1050` (the placement pair; region/file left in the bin) |
| **Add clip, reuse region** | +1 `0x104F`, +1 `0x1050` |
| **Add clip, new audio** | placement + region (`+0x2628`,`+0x2629`) + file descriptor (`+0x1001`,`+0x1003`,`+0x1033`,`+0x2106`) |
| **Add fade** | +1 `0x262F` per faded clip (crossfade also adds `0x2523`/`0x2526`/`0x2423`/`0x262B`) |
| **Add track** | +1 `0x261B` + a track-list entry (`0x1014`/`0x251A`) + the `0x261B` subtree + scaffolding (`0x2506`,`0x2625`,`0x2626`,`0x260A`,`0x260C`,`0x260E`) |
| **Remove track** | the inverse (Hipsters bak.044→045: −1 `0x261B`, −1 `0x1014`, −`0x2506`/`0x2625`/`0x2626`) |
| **Renumber start bar** | `0x2029` meter-event `i32` (§5c) — no block-count change |

**Implemented** (all reindex via the size-driven offset-shift engine, validated by
**round-trip byte-identity** across all 26 corpus sessions): `remove_clip`, `add_clip`
(reuse region), `move_clip`, `replace_clip_region`, plus the readers `clip_names`,
`region_names`, `session_start_bar`, `session_sample_rate`, `session_bit_depth`,
`session_info`. Add/remove-track is the next build: its blocks are **indexed** and the
lanes of *all* audio tracks share one `0x1054` container, so it needs a **rank-rebuild** of
the master index (not just an offset-shift) — scoped in `TODO.md`.

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

### Tempo (`0x2028` map / `0x2718` lane) — **(PT-confirmed, full map)**

The `0x2028` payload opens with the tag `"Tempo"` (payload +0), a `u16` version, a `u32`
`payload_len` (+7, `== 4 + count*61`), and the event `count` as a `u32` at **payload +11**.
The per-event records are **fixed 61 bytes** and begin at **`zmark+28`** (payload +19, the
first `Const`). Each record is framed by the literal text `Const` … `TMS`; per record:

| Record offset | Field |
|---|---|
| `+30` | musical position: 5-byte LE tick (subtract `ZERO_TICKS`) |
| `+40` | **BPM** as IEEE-754 little-endian `double` (f64) |
| `+48` | ppq (`u32`) |

Hundreds of tempo events are normal (one per beat-map point); the map is mirrored in the
`0x2718` lane. `body_synth.tempo_map(data)` returns `[(bpm, tick), …]` (the shape
`set_tempo_map` consumes) and `base_tempo(data)` the event-0 BPM. Both recover PT-authored
controls **exactly** (90 / 121 / 120→140 @bar2) and are sane/monotonic across the corpus
(up to 111 events). **Note:** a `set_tempo_map`-resized `0x2028` can leave its declared
block size (`zmark+3`) lagging its record count — the reader must bound by `count`, not the
declared block end (a real third-party session's size field is consistent).

### Meter (`0x2029` map / `0x2719` lane) — **(PT-confirmed, full map)**

The `0x2029` payload opens with the tag `"Meter"` (payload +0), a `u16` version, a `u32`
`payload_len` (+7), and the event `count` as a `u32` at **payload +11**. The per-event
records are **fixed 36 bytes** and begin at **`zmark+24`** (payload +15). `payload_len`
counts `12 + count*52` — the 36-byte record **plus** a 16-byte entry in the `0x2719` lane
(36 + 16 = 52), which is what made a naive 52-byte record stride mis-parse past event 0. Per
record:

| Record offset (from `zmark+24 + 36*i`) | Field |
|---|---|
| `+0` | musical position: 5-byte LE tick (subtract `ZERO_TICKS`); `0` for the base |
| `+8` | **start bar / ordinal** (`u32`; event 0 is the signed renumber start bar, §5c) |
| `+12` | numerator (`u32`) |
| `+16` | denominator (`u32`) |

`body_synth.meter_map(data)` returns `[(numerator, denominator, tick), …]` (the shape
`set_meter_map` consumes) and `base_meter(data)` the event-0 `(num, den)` (empty map → 4/4).
Both recover PT-authored controls **exactly** (4/4→3/4 @bar2) and are sane across the corpus
(up to 18 events). Each event also has a 16-byte entry in the `0x2719` trailing lane.

### Replacing a conductor map on an arbitrary session — **(corpus-validated 26/26)**

`body_synth.replace_tempo_map(data, [(bpm, tick), …])` and `replace_meter_map(data,
[(num, den, tick), …])` rewrite the map on any real session in place. Unlike the synthesis
writers (`set_*_map`, which need the scan parser + require the lanes), they resize the
session's OWN top-level `0x2028`/`0x2029` (and the `0x2718`/`0x2719` lane **only when
present**) and repair the master index with a pure **offset-shift** (the block count is
unchanged) — the same reindex the clip edits use. Validated across all 26 corpus sessions:
identity replace is **byte-identical**, a modify reads back exactly (`tempo_map`/`meter_map`),
index holes resolve, every other block is byte-intact (`rc=0`; PT display confirmation
pending). Details worth recording:

- **The tempo lane `0x2718` wraps a *nested* `0x2028`** — resizing must bump that nested
  block's own size (`nested+3`) too, not just the outer lane.
- **Meter record `+8` = the absolute bar number** (`i32`), advanced from event 0's start bar
  by **`ceil(tick_span / ticks_per_bar)`** (CEIL, not floor — a mid-bar change lands on the
  next barline); the 16-byte lane entry holds the **start-relative** bar (`bar − start_bar +
  1`). A replace **preserves the session's displayed start bar** (§5c), so THE WIND's `−2`
  survives.
- A meter record has an **opaque per-record byte at `+22`** (`0x03`/`0x04`, not derivable from
  num/den/tick); an identity replace stays byte-exact by cloning surviving records per index.
- An **empty (count 0) meter block** carries no record to clone, so a grow seeds from the
  canonical template.

### Markers (`0x2030` list / `0x2077` record) — **(PT-confirmed, full read)**

`0x2030` = `u32 count` + N × `0x2077`. Each `0x2077` marker record:

| Record offset | Field |
|---|---|
| `+9` | ordinal (`u8`) |
| `+15` | name length (`u32`) |
| `+19` | name bytes |
| `name_end` and `name_end + 8` | position: a `u64` (two copies), timebase-encoded as §5d |
| `name_end + 166` | 16-byte GUID |

The position `u64` uses the **same timebase encoding as clips (§5d)**: top byte `0x40` =
**tick**-locked (`tick = pos - 0x4000000000000000 - ZERO_TICKS`), `0x00` = **sample**-locked
(`pos` is a plain sample count — a marker pinned to an audio hit; e.g. Bianca's "Intro" at
~193 610 samples). The old "5-byte `ZERO_TICKS + tick`" note only held for tick-locked
markers — a sample-locked one read that way lands at a large negative "tick".

A session can carry a **duplicate `0x2030` list** (PT's memory-locations copy — THE WIND has
two identical 21-marker lists, DWTS two 123-marker lists), so a reader must de-duplicate by
`(name, position)`. `body_synth.markers(data)` returns
`[{"name", "tick", "sample"}, …]` (one of tick/sample set per timebase), deduped, in list
order; `session_info` reports the deduped `n_markers`. Recovers PT-authored controls exactly
and is sane across the corpus (0..123 markers, mixed tick/sample on Bianca/COGNAC).

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

## 12b. Per-track positional fields — channels, ordinals, id-pools, provenance

When a track is **inserted or removed mid-list**, three classes of per-track field must be kept
consistent or Pro Tools parses the file yet **crashes when it reconciles routing on close**.
The read side tolerates staleness (a bad session *opens*); the close/save path does not. All
offsets below are anchored on each block's own `name_end` (= `namelen` field + the name bytes),
never a fixed block offset — the anchor slides by one byte per extra name-length digit.
**(all-stereo synthesis; PT-confirmed via whole-file byte-identity to a fresh session)**

**1. Stereo channel indices — the collision that crashes on close.** In every per-track
`0x1014` block (also inlined into the `0x1015` container) there are two channel *sites*, each
holding the same pair: SITE1 = two `u16` at `name_end+5`/`+7`, SITE2 = two `u32` at
`name_end+32`/`+36`. For a stereo track at **0-based display row `p`**: `ch0 = 2p`,
`ch1 = 2p+1`. Purely a function of the final row — inserting a stereo track at row `P` bumps
every row `≥ P` by 2 (one stereo pair; +1 for a mono insert). A *duplicate* that copies the
source's blocks inherits the source's channels, so the copy and its source both claim the same
pair → the on-close crash. A *middle remove* only leaves a channel **gap** (no collision), which
is why removing a middle track closes cleanly with no channel renumber.

**2. Ordinals (display position), five families.** All little-endian, value tied to the row:
- `0x200a`/`0x200b`/`0x2015` — one nested subtree per track (same bytes reappear in all three
  enclosing types); ordinal is a `u32` at `+18` after the inner `0x2434` anchor
  `5A 01 00 0B 00 00 00 34 24 04 00 00 00 00 00 00 3C 00`. Value = `p+1` (1-based).
- `0x2519` name table — one variable-length entry per track in display order; ordinal `u16` at
  `name_end+18` (restricted to the region before the first framed child). Value = `p+1`.
- `0x251a` lane instances — `2N` instances laid out **lane-major** (indices `0..N-1` = lane 0 of
  rows `0..N-1`, then lane 1); `row = idx mod N`; ordinal `u16` at `name_end+18` = `p+1`.
- `0x2589` overview blocks — `u16` at `zmark+9` = `p` (0-based). Inlined into containers
  `0x2551`/`0x2587`/`0x258a`/`0x258b`; for the two-instance `0x258a`/`0x258b` all `N` children
  live in the **second** instance.

**3. Element-id pools — a free, renumberable pool (NOT block-index references).** Each per-track
`0x261b`/`0x261c` block carries a run of **10 consecutive `u32`** ids after the `01 01 0A 00`
marker (`ids[j] = ids[0]+j`); they reappear in the `0x2624` edit-playlist container. These look
like offsets but are **id-pool tokens** — none equals any block zmark. PT requires only that they
are **present, consecutive, and unique per track**, not any specific value. Practical
consequence: a duplicate must give the copy a *fresh* pool (else it collides with the source), and
the absolute base is **source-dependent** — synth scaffold rows (0–7) draw from `base≈20661`
while grow-path rows (`≥8`) draw from `base=1` (`ids[0]=10p+1`). So the pool is a `+10`-per-track
slide, not a global `f(row)`.

**4. Provenance, not position — `0x2104` view-scale.** The per-track `0x2104` block's inner
`03 21` payload is either all-zero or the **constant** pattern
`EF FF DF BF EF FF DF BF 02 00 00 00 … EF FF DF BF 02 00 00 00`. It is byte-identical across
tracks that have it and distinguishes **grow-path** provenance (present on synth rows `≥8`) from
**scaffold** provenance (zero on rows `0–7`) — independent of the final display row. A duplicate
of a scaffold track therefore differs here from a fresh grown track; to reach byte-identity with a
fresh session the writer regenerates the grow-path pattern (and pool) for the appended track.

**Editing rule of thumb.** The numeric positional fields (1, 2) are **self-inverse under a
slot re-key** — recomputing every field from its physical body slot works identically for add and
remove. The free fields (3, 4) are **not** self-inverse; they are moved as a reversible *slide*
(up on insert, down on remove) so the round-trip stays byte-exact. `body_synth`
`_rekey_positional_by_slot` + `_renumber_body_after_duplicate` / `_unrenumber_free_fields_before_remove`
implement exactly this.

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
6. **Keep container child-counts in sync.** Pro Tools reads the body as a nested stream: a
   *count-prefixed container* stores a `u32` child-count at payload+0, then reads exactly
   that many child blocks (some containers then read a trailer block of a *different* type).
   If the stored count doesn't match the actual children, PT reads past the container and
   fails with **"end of stream encountered."** A size-driven reader walks by declared size
   and *tolerates* a stale count, so an edit can pass `rc=0` yet be PT-invalid — this is the
   subtlest way a track add/remove breaks. Confirmed count-containers include `0x1015`
   (tallies `0x1014` track entries), `0x1054` (tallies `0x1052` lanes), and `0x2624` (tallies
   ALL per-track subtrees — `0x261c`/`0x261e`/`0x2621`/… — i.e. the total track count; the
   real `Hipsters bak.044→045` removal drops it 142→141). Any block-add/remove edit must
   rewrite the affected counts.
7. **Keep the `0x2519` name table's INLINE entry list in sync.** The name table stores, before
   its `0x5A`-framed children, a list of **inline name entries** — each `u32 name_length | name
   bytes | 23-byte trailer`, with a `0x0000002A` (u32 = 42) marker at trailer offset +6 (the
   first entry begins at payload `+0x14` for `format_version ≥ 8`, else `+0x16`). PT reads this
   list entry-by-entry; if a track edit leaves it out of sync — deleting only `len|name` and
   leaving the orphan trailer, or inserting a trailer-less entry — the next entry's
   `name_length` lands on unrelated bytes, reads a huge value, and the walk overruns the object
   → **"end of stream."** The block `size` and the framed `0x251A` name-detail children can be
   perfectly adjusted (so a size-driven reader passes) while this inline list is broken —
   confirmed as the exact `0x2519` fault on `remove_track`/`duplicate_track`. Splice/insert the
   **whole** entry (`len | name | 23-byte trailer`), keeping every entry's `0x2A` marker aligned.

`ptxformatwriter.eos_validator.simulate(data, blocks)` reproduces PT's count-driven read of the
name table and track container and pinpoints the object/field that would overrun;
`body_synth.validate(data)` runs it plus the count-container check as a **pre-write gate** (sound
on all 26 corpus sessions — no false positives — and it reproduces the real EOS on the pre-fix
edits). It is *necessary, not proven-sufficient*: a clean result plus Pro Tools itself is the bar.

---

## 15. Further reading (in this repo)

- `final-index-0x0002-schema.md` (archived in `unused/docs/`) — the index record grammar.
- `gen1-vs-gen2-architecture.md` — why the index is rebuilt deterministically, not guessed.
- `ptxformatwriter/core.py` — the reference reader; `ptxformatwriter/body_synth.py` — the reference
  writer toolkit; `ptxformatwriter/final_index.py` — the holes-model index rebuilder;
  `ptxformatwriter/wavecache.py` — the `WaveCache.wfm` generator.
