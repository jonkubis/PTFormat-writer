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

---

## 16. Extended content-type catalog (corpus-dissected)

All offsets below are payload-relative (= zmark + 9), i.e. relative to the first byte after the 9-byte block frame (`5A | block_type:u16@z+1 | size:u32@z+3 | content_type:u16@z+7`). Confidence and count claims are quoted from the corpus dissection; low-confidence items are flagged explicitly.

### Paths, strings, and I/O routing

### 0x2064 — plug-in-settings (.tfx) file container

- **kind:** CONTAINER — immediate children exactly one 0x1000 then one 0x102a, in that order (362/362).
- **size:** variable; span = 9 (frame) + span(0x1000 child) + 1 (single 0x00 separator) + span(0x102a child). NOT the sum of the two child spans. Observed full span 270–352 bytes (median ~328) across 362 instances.
- **fields:**
  - No scalar payload of its own; first payload byte is the 0x5A of child[0].
  - child[0] = 0x1000 @ z+9 (block_type 0x0002) — system/factory absolute path (volume string, filename `*.tfx`, component list).
  - 1 byte 0x00 separator between child[0].end and child[1].start (339/339 where measured).
  - child[1] = 0x102a @ child0.end+1 (block_type 0x0001) — alternate absolute path rooted on the session's own volume, ending in its co-located `Plug-In Settings` folder. Block ends exactly at child[1].end.
- **notes:** One 0x2064 == one referenced .tfx settings file. Parent always 0x2027 (plug-in-settings collection). Per-session count(0x2064) == count(0x102a-as-child) == count(0x1000-as-child) (20/20). The two children carry different block_types (0x1000→0x0002, 0x102a→0x0001). PT does not reindex its contents. NOT a track/audio-file reference.
- **confidence:** high (362/362 across 20 sessions).

### 0x1000 — absolute filesystem-path record / empty path placeholder

- **kind:** LEAF (no child blocks in either variant; verified across 339 Variant-A + 18216 Variant-B).
- **size:**
  - Variant A (bt=0x0002): variable payload 159–208 bytes (span 168–217), driven by string lengths + component count N (N ∈ {7, 8, 10}).
  - Variant B (bt=0x0000): fixed 18-byte payload (span 27 bytes).
- **fields (Variant A, block_type 0x0002 — absolute path record, always child of 0x2064):**
  - u32 len + ASCII = str1, volume/root name (e.g. `Macintosh HD`); equals comps[0].
  - u32 len + ASCII = str2, final component / filename (e.g. `FabFFQ2pFQ2pFact.tfx`); equals comps[-1].
  - +0 u32 = 0 (always; 339/339).
  - +4 u32 machine/volume constant ∈ {0x00000000, 0x0000FF9C}; constant per machine/session.
  - +8 u16 = 0 (always; 339/339).
  - +10 u32 component_count N.
  - +14 N length-prefixed ASCII strings = full path (comps[0]==str1 volume, comps[-1]==str2 filename).
  - END exactly 4 bytes = a per-machine/volume identifier (see notes).
- **fields (Variant B, block_type 0x0000 — empty path placeholder, always child of 0x4403):**
  - 18-byte payload, all 0x00 (18216/18216).
- **notes:** Variant A parent always 0x2064; Variant B parent always 0x4403. The trailing 4 bytes of Variant A are NOT a directory-prefix hash (that claim is REFUTED): the value is constant across the whole session and shared across separate session files from the same machine, correlating 1:1 with the (volume-name, +4-constant) machine identity. Observed tail values: f4fc22d6, e0a9e1cb, 07b755cd, 96bdafd0, 13aa55cd. NOT a GUID (no preceding 2A 00 00 00 tag).
- **confidence:** high (verified across 19 sessions + Hipsters).

### 0x102a — session-side plug-in-settings folder path

- **kind:** LEAF (0/339 instances have any immediate child).
- **size:** variable; payload 64–143 bytes; span 73–152; declared size field 66–145 (payload == size−2, span == size+7).
- **fields:**
  - +0 u32 = 0 (always; 339/339). Equivalently +0 u16==0, +2 u16==0.
  - +4 u16 = 0 (always). First 6 bytes are all zero.
  - +6 u32 component_count N (339/339; N ∈ {3,4,5,7}).
  - +10 N length-prefixed strings (u32 len + ASCII) = path components. comps[0] is the leading volume/drive name; comps[-2] is `Plug-In Settings` in 258/339; comps[-1] is a plug-in name or `Plug-In Settings`.
  - END exactly 4 trailing bytes 0x00000000 (339/339); payload consumed exactly.
- **notes:** block_type 0x0001; parent always 0x2064 (339/339 across 19 non-huge sessions). Complementary to its 0x1000 sibling: this path is on the session/user volume and STOPS at a directory, while the 0x1000 sibling is the resolved absolute path on the boot volume ending in the actual `.tfx` FILE. No GUID/`2A 00 00 00` tag inside this payload.
- **confidence:** high.

### 0x1021 — I/O-Setup bus channel leg

- **kind:** Almost always LEAF (3560/3562 zero children). EXCEPTION: 2 instances (`BACKING TRACKS` bus in COGNAC and DOPE) contain one embedded 0x0001 child.
- **size:** variable, driven by short_name length, channel count, long_name length, and block_type variant. Observed block_size 44–119 (mono legs commonly 45/47/55; pairs commonly 51/55/63/71). Payload = block_size − 2.
- **fields:**
  - +0 u16 flags: HIGH byte = channel-count kind (0x01xx = stereo pair, 0x00xx = single); LOW byte = bus/path CATEGORY (0x00 Analog-in, 0x01 Reverse Rewire / multi-dest, 0x02 Out, 0x03 Insert).
  - +2 u32 short_name_len, then short_name ASCII (e.g. `1-2`, `Analog 1`, `Out 21-22`).
  - u32 channel_count M (1 mono leg, 2 pair).
  - M × u16 1-based channel index/indices.
  - u16 path_code: 0xFFFF for every stereo pair (1259/1259); for mono legs a per-category ordinal that increments (steps of 3 within a category).
  - u32 = 1 (constant, 3562/3562).
  - u8 = 0 (constant, 3562/3562).
  - u32 = 0x0000002A GUID tag (constant, 3562/3562).
  - 8 bytes GUID: unique within a session, frequently reused across sessions.
  - 3 bytes 00 00 00 (constant, 3562/3562).
  - u16 = 0xFFFF (constant, 3562/3562).
  - u8 long_name_flag: 0 ⇒ a long_name field follows (3328/3562); 1 ⇒ NO long_name, block ends with a 6-byte tail 00 00 00 00 00 00 (234/3562, all Reverse Rewire).
  - [if flag==0] u32 long_name_len + long_name bytes (== short_name in 3320/3328), then a trailer whose length depends on block_type: 2 bytes for block_type 0x0007; 10 bytes for block_type 0x000B.
- **notes:** block_type is BIMODAL: 0x0007 (2640/3562) or 0x000B (922/3562), exactly correlated with trailer form. Parent always 0x1022 (the bus). Count scales with I/O-Setup channel/path count (67–432 per session), NOT track count. GUID unique only intra-session.
- **confidence:** high.

### 0x2600 — signal-path / routing label entry

- **kind:** LEAF.
- **size:** variable; payload = 28 + strlen + tail. Tail is exactly 7 bytes when block_type==2 and exactly 22 bytes when block_type==7 (708/708). Payload sizes 38–83.
- **fields:**
  - 0..8 path handle/ID (8 bytes) — opaque; bytes[2:8] often carry a per-device/path-group signature, but treat all 8 as an opaque handle.
  - +8 u32 reserved == 0 (708/708).
  - +12 tag `2A 00 00 00` — GUID tag (708/708).
  - +16 8 bytes GUID — stable per-path identity (164 distinct, 113 recur; 0 cross-name collisions).
  - +24 u32 strlen — path name length (708/708).
  - +28 strlen ASCII name (e.g. `Out 1-2`, `Main Output L/R`, `Mix In 17-18`, bare pairs `1-2`..`39-40`).
  - +(28+strlen) 7-or-22 bytes tail — routing/channel flags. bt==2 always `01 01 00 00 00 00 00`; bt==7 always 22 bytes in 3 variants differing only in middle bytes (does NOT track name).
- **notes:** Parent always 0x2601 (chain 0x2600 → 0x2601 → 0x2602 → 0x2603). Parent 0x2601's first payload u32 = immediate-0x2600 child count (263/263 non-empty parents). block_type is NOT a mono-vs-multichannel discriminator — same name appears under both bt=2 and bt=7; block_type only reliably determines tail length.
- **confidence:** high (708/708 across 19 sessions + Hipsters).

### 0x2005 — named I/O / plugin-source routing-path wrapper

- **kind:** CONTAINER (block_type 1) holding AT MOST ONE 0x2006 child (block_type 3); can be EMPTY (payload_len=8, count=0).
- **size:** variable; payload_len = 8 when empty, else one of {91,105,108,111,117,120,133,136,141,144,147}. Driven by the one child's path-name length (payload ≈ 3×namelen + fixed_overhead); child COUNT never exceeds 1.
- **fields:**
  - 0 u8[4] head = 00 00 00 00 (108/108).
  - +4 u32 count/present-flag: 0 (empty) or 1 (one-child); only ever 0 or 1 — effectively a "child present" flag, not a general count.
  - +8 (count=1) a single 0x2006 block (block_type 3); its payload starts `ff ff` then a length-prefixed ASCII path name repeated three times down the chain 0x2006 → 0x2086 → 0x208c → {0x2097|0x208d|0x208e}.
- **notes:** Endpoint labels: ReWire/network I/O bus (`Network, Session 2`), virtual-instrument MIDI input port (`VPS PhalanxMidi In 1`), or plugin-instance (`Xpand2 1`, `Absynth 5 1`, `DR-660`). 108 non-Hipsters instances (36 empty, 72 one-child). Parents (immediate enclosing): 0x2004 (51), 0x2062 (39), 0x2023 (18). A `2A 00 00 00` GUID tag appears in only 14/72 subtrees — no reliable GUID field. Encodes a routing-path name, not track identity.
- **confidence:** medium.

### 0x2006 — MIDI-device / instrument-plugin routing chain (outermost node)

- **kind:** CONTAINER (exactly one immediate child: 0x2086). Frame block_type 0x0003 (72/72).
- **size:** variable. Local payload (start → first child) = 7 + name_len exactly (72/72; 13–28 bytes). Total block span 83–139 bytes including nested descendants.
- **fields:**
  - 0 u8[2] head/flag: `ff ff` for normal path nodes (65×); `10 6d` ONLY for the DR-660 hardware MIDI device (7×).
  - +2 u32 name_len.
  - +6 name_len ASCII device/plug-in name (e.g. `Absynth 5 1`, `Omnisphere 1`, `VPS PhalanxMidi In 1`, `DR-660`, `Xpand2 1`).
  - +(6+name_len) u8 trailing flag: 00 (54×) / 01 (14×) / ff (4×); `ff` coincides with grandparent 0x2023 (Network devices).
  - then the single 0x2086 child block.
- **notes:** Immediate parent always 0x2005; grandparent-container 0x2004 / 0x2062 / 0x2023 is top-level. Name string repeated verbatim at all three wrapper levels (0x2006/0x2086/0x208c), but per-level header width differs. Below 0x208c the child varies: 0x2097 (61×), 0x208d (7×), 0x208e (4×) — NOT a strict `>0x2097` chain. NOT master-indexed (writer must emit the whole subtree inline).
- **confidence:** high (72 instances / 15 sessions).

### 0x2086 — MIDI-device routing chain (second-level node)

- **kind:** CONTAINER (exactly one 0x208c child) in the MAIN form; LEAF in the 1-byte placeholder form. block_type 0x0002 for every instance.
- **size:**
  - MAIN: local part = 5 + name_len + 4 bytes (name_len 6–21 → local 15–30 B), followed by the inline 0x208c child; payload 48–89 B; block span 57–98 B.
  - PLACEHOLDER: declared size=3, payload=1 byte, block span 10 B.
- **fields (MAIN, 76 inst):**
  - 0 u8 head flag = 0x00 (all 76).
  - +1 u32 name_len (6–21).
  - +5 name_len ASCII device name (mirrors parent and child).
  - +(5+name_len) u16[2] = (flag, 0): second u16 always 0; first ∈ {0 (54×), 256 (14×), 1 (8×)}; correlates with device family (Absynth→256, `Network,*`→1). NOT a length/count.
  - then exactly one inline 0x208c child.
- **fields (PLACEHOLDER, 15 inst, parent = indexed 0x2050):**
  - 0 u8 = 0x01 (leaf, no name, no child).
- **notes:** MAIN parent is 0x2006 (72×) OR directly an indexed 0x2050 (4×: DWTS `Network, DWTS` / `Network, Session 2`). NOT master-indexed. Writer-set device-identity mirror.
- **confidence:** high (91 instances / 19 sessions).

### 0x208c — MIDI-device routing chain (third-level node)

- **kind:** CONTAINER (exactly one immediate child: 0x2097 (61×), 0x208e (8×, all `Network*`), or 0x208d (7×)). block_type 0x0001.
- **size:** variable = 9 + name_len + child_block_total (declared u32 size includes the nested child). Local prefix = 7 + name_len (13–28 B); total block span 33–66 B.
- **fields:**
  - 0 u8[2] head = `00 00` (76/76).
  - +2 u32 name_len.
  - +6 name_len ASCII device/path name (byte-identical to parent 0x2086's name).
  - +(6+name_len) u8 trailing = 0x01 (76/76).
  - then the single nested child block (included in declared size).
- **notes:** Always immediate child of a 0x2086; grandparent 0x2006 or 0x2050. NOT master-indexed. One outlier 0x2097 child (THE WIND `Mini Grand 1`) is block_type 0x0004 with a trailing 16-byte GUID-ish tail, which enlarges that instance but does not break the size formula. Writer-set.
- **confidence:** high (76 instances / 15 files / 6 families).

### 0x2097 — MIDI routing element handle (leaf)

- **kind:** LEAF (no block children).
- **size:** variable, NOT fixed: 60/61 instances have 13-byte payload (block_type 0x0003, size 15); 1/61 (THE WIND) has a 29-byte payload (block_type 0x0004, size 31) carrying a trailing 16-byte GUID.
- **fields:**
  - 0 u8 = 0x01 (61/61).
  - +1 u32 handle: a per-instance sequential MIDI routing/element ID assigned by PT, NOT a stable per-device ID (same device name → different values within one session; allocated in creation order, spacing 20). Referenced elsewhere as a raw LE u32. Range 611–3580, 21 distinct values.
  - +5 u8[8] all zero (13-byte form). In the 29-byte outlier: 8 zeros, then 1 byte, then a 16-byte GUID (appended directly, NOT preceded by a `2A 00 00 00` tag).
- **notes:** Always sole child of 0x208c; full chain 0x208c → 0x2086 → 0x2006 → 0x2005 → (0x2004 main tree | 0x2062 duplicate tree). NOT master-indexed. The u32 is a per-instance handle, NOT a stable per-device unique ID (this is the reason for the medium confidence).
- **confidence:** medium (61 instances / 11 sessions).

### Records (fixed / semi-fixed numeric and identity blocks)

### 0x103d — fixed-size numeric-parameter record

- **kind:** LEAF (no children, no strings, no GUID tag).
- **size:** fixed: size field = 0x3B (59); span z..z+66; payload 57 bytes (203/203 across 21 sessions).
- **fields:**
  - +0 u32 flags — parent-dependent: under 0x1040 (groove) ∈ {0x05, 0x17, 0x405}; under 0x2502 always 0x45.
  - +4 u32 = 0 (constant).
  - +8 f32 value A ∈ {8.831, 9.373, 10.831, 12.831}; under 0x2502 always 8.831.
  - +12 u32 = 0 (constant).
  - +16 f32 value B ∈ {7.916, 8.373, 9.831, 11.831}; under 0x2502 always 7.916. A−B is usually 0.916, exactly 1.0 only in the 10.831/9.831 and 12.831/11.831 cases.
  - +20 u32 mode/index — under 0x1040 ∈ {1,3,5}; under 0x2502 always 5.
  - +24 u32 = 3 (constant).
  - +28 u32 = 2 (constant).
  - +32 u32 — 100 under 0x1040, 0 under 0x2502.
  - +36 u32 = 0 (constant).
  - +40 u32 = 100 (constant across both parents).
  - +44 u32 = 0 (constant).
  - +48 u32 — 100 in 115/119 non-Hipsters records, 80 in 4 groove records; always 100 under 0x2502.
  - +52 u32 = 0 (constant).
  - +56 u8 = 1 (constant).
- **notes:** block_type 0x0002. Two immediate-parent contexts: (a) under groove-quantize template 0x1040 (exactly 2 per session) where float/flag fields VARY per groove; (b) under 0x2502 (grandparent 0x2505 or 0x1057) where the 57 payload bytes are BYTE-IDENTICAL across every instance — a fixed default constant, not a live mixer setting. Layout/offsets/constants are high-confidence; the human-readable field semantics (which float is swing-strength vs range vs pre-roll, meaning of `mode`) are unproven guesses.
- **confidence:** medium (offsets/constants high; field semantics low).

### 0x2502 — fixed-shape per-track/metronome settings record

- **kind:** CONTAINER (exactly one immediate child, 0x103d, framed 66 bytes / 57-byte payload; 81/81).
- **size:** fixed: payload_len = 154 on all 81 instances.
- **fields:**
  - 0 u8[4] header = 01 01 00 01 (invariant).
  - +4 the 0x5A-framed 0x103d child (bytes `5a 02 00 3b 00 00 00 3d 10`, block_type 0x0002, size 59). The entire child region [4:70] is byte-identical across all 81 (fixed-shape numeric sub-block; its two invariant f32s are at child-relative offsets 8 and 16 = payload 21/29: 8.8311 and 7.9155; child leading u32 = 0x45 is a fixed magic/count, not a length).
  - payload offset 70+: only bytes [76,77,78,79,82,83,84,105,106,114,115,116,117] ever vary; the other 141 bytes are invariant. bytes[82:85], [105:107], [114:118] co-vary in lockstep into exactly TWO configurations (41× vs 40×) — a binary mode/flag toggle. bytes[76:79]/[84] vary more freely.
- **notes:** Parents: 0x1057 (×62), 0x2505 (×19). Per-session count 1–14. The two invariant floats, the mode toggle (likely tied to the two parent types), and exact float semantics remain unpinned.
- **confidence:** medium.

### 0x2075 — analysis/breakpoint (position, value) point

- **kind:** LEAF.
- **size:** fixed 16-byte payload; 25-byte span (block_type 1, declared size 18). Verified on all 14208 instances across 26 sessions.
- **fields:**
  - 0 u64 position — sample offset; strictly monotonically increasing within the parent 0x2073 list (verified across all 26 sessions). High u32 is always zero in this corpus, so u64-vs-u32 is not forced by the data (u64 is the correct read given PT's format-wide u64 positions).
  - +8 f64 value — analysis value at that position, LE IEEE-754 double. Range is per-list (DWTS ~242.5–243.4; Reverse Rewire ~2.26–1e5).
- **notes:** Parent always 0x2073 (14208/14208 exclusive parent), a top-level container whose payload is a u32 count immediately followed by its child blocks; count == number of 0x2075 children (20/20 populated). No GUID, no strings. Value meaning is analysis-list-specific, not universal.
- **confidence:** high.

### 0x210b — per-named-entity identity record (name → GUID)

- **kind:** LEAF (628/628 zero children).
- **size:** variable = 40 + name_len bytes payload (628/628). Constant 40 = 8 (head + name_len field) + 4 (gap) + 4 (GUID tag) + 8 (GUID) + 16 (zero tail). Observed payload 42–80 (name_len 2–40).
- **fields:**
  - 0 u8[3] head = 00 00 00 (constant).
  - +3 u8 flag: 0x00 ×590 (regular track / most entities), 0x02 ×36 (aux/bus/subgroup, plus Click), 0x07 ×1 (Inst 1), 0x08 ×1 (C0004_1) — a track-CATEGORY flag.
  - +4 u32 name_len (LE).
  - +8 name_len ASCII entity name (e.g. `Click`, `COUNT`, `TC`, `WARPD REF`, `Kick.08`).
  - +(8+name_len) u8[4] gap = 00 00 00 00 (constant).
  - +(12+name_len) u8[4] GUID tag = 2A 00 00 00 (constant).
  - +(16+name_len) u8[8] GUID (entities created together share prefix bytes).
  - +(24+name_len) u8[16] tail = all zeros (constant).
- **notes:** Parent always 0x2107 (628/628), a registry that holds EXCLUSIVELY 0x210b children. NOT universal: appears in only 4 of 26 corpus projects (DWTS ×3 near-duplicate saves, MANOLITO, THE WIND) — feature/version-specific. This is the canonical name→GUID binding other blocks reference.
- **confidence:** high for structure/offsets/parent/leaf/size; medium for flag SEMANTICS (aux/bus category is a strong but not perfectly clean split — Click appears under both 0x00 and 0x02); medium-low for generality (only 4 projects).

### 0x2036 — ordered-permutation item record

- **kind:** LEAF (51/51; payload 34 bytes, no nested framing).
- **size:** fixed payload_len = 34 (51/51).
- **fields:**
  - 0 u32 = 0x60 (96) constant.
  - +4 u32 = 0 constant.
  - +8 u32 = 0x40 (64) constant.
  - +12 u32 ordinal — the only structurally-meaningful varying field; in file order across a session the ordinals form a permutation of 1..N.
  - +16 u32 = 0xC000FF80 (bytes 80 ff 00 c0) constant.
  - +20 u8 flag = 0x01 (44×) or 0x00 (7×); all zeros are in Reverse Rewire backups on the file-order-LAST 0x2036. Meaning undetermined.
  - +21 u8 = 0x00 constant.
  - +22 u32 = 0x60 (96) constant.
  - +26 u32 = 0 constant.
  - +30 u32 = 0x40 (64) constant; +34 = payload end.
- **notes:** Each 0x2036 is one immediate child of a 0x2611 wrapper (sibling 0x260a); count(0x2611) == count(0x2036). Chain 0x2611 → 0x2621 → 0x2624; exactly one 0x2624 per session, so all items live in a single ordered list. N is small (1/2/5/13) and does NOT match track count; 10/19 sessions have zero 0x2036 — this is an OPTIONAL ordered permutation list, not a per-track record. WHAT the ordinal maps to and the permutation direction are not resolvable from these blocks.
- **confidence:** high for LEAF/size/field-map/permutation/containment; medium-low for overall ROLE; low for the +20 flag meaning.

### 0x206f — constant `01 00 00 00` leaf inside a per-element edit-record

- **kind:** LEAF (0 children, 247/247).
- **size:** fixed 13-byte block span (block_type 0x0002, size field 6, 4-byte payload).
- **fields:**
  - 0 u32 = 1 (constant `01 00 00 00` across ALL 247 instances; never varies).
- **notes:** Direct parent 0x2621 (240/247) or the shorter 0x2620 variant (7/247); grandparent 0x2624 (single top-level session-wide edit container — NOT a per-track record). Immediately preceded by sibling 0x2010 (247/247). NOT master-indexed. The `enabled=1 / schema-version sentinel` interpretation is plausible but UNPROVEN (the value is invariant everywhere).
- **confidence:** high for structure/constant; low for semantic meaning.

### 0x230b — I/O-Setup default name slot (`Custom N`)

- **kind:** LEAF (912/912 zero children).
- **size:** variable (string-length driven): span 23 bytes for `Custom 1`..`Custom 9`, 24 bytes for `Custom 10`..`Custom 48`. Declared size_u32 = 16 or 17 (= 2 + payload_len).
- **fields:**
  - 0 u32 len + N bytes name = `Custom N` (N = 1..48, in file order); exact char count, no NUL terminator.
  - payload END 2 bytes trailing 0x0000.
- **notes:** block_type 0x0001. Exactly 48 instances per session (Counter={48:19}, total 912). Parent always a single 0x230a per session, holding ONLY 0x230b children, as `Custom 1`..`Custom 48` in strict file order. A constant default table — safe to emit verbatim; PT does not recompute it.
- **confidence:** high.

### Containers

### 0x2614 — per-plugin automatable-parameter list

- **kind:** CONTAINER (immediate children: 0x260f only; bijective — every 0x260f is a direct child of a 0x2614, 468/468).
- **size:** variable; payload = 4 + sum(child 0x260f spans). Empty (count=0) ⇒ payload exactly 4 bytes 00 00 00 00 (2841/2841 empty). Non-empty payloads 84–819 bytes (819 is corpus max, not a proven ceiling).
- **fields:**
  - 0 u32 child_count = number of inline 0x260f children (== actual immediate-child count, 3091/3091; observed {0,1,2,3}). When 0, payload is exactly the 4 zero bytes.
  - +4 inline 0x260f child blocks back-to-back; first at payload offset 4 (data z+13), last ends exactly at block end (250/250 non-empty).
- **notes:** block_type 0x0001. Full corpus 3091 instances / 26 sessions. Immediate parent 0x2616 (3083/3091) BUT 0x2618 in 8 instances (all `Bianca Long Road_Mix Prep.1`); grandparent 0x2627 (1636) or 0x2617 (94). No GUIDs or scalar fields beyond the count u32.
- **confidence:** high.

### 0x260f — single automatable plugin parameter (automation-list row)

- **kind:** CONTAINER (exactly one immediate child, a 0x260b, always at payload offset 1) + own trailing fields. The 0x260b child is a leaf; its own block_type word is always 0x0001.
- **size:** variable, 62–806 bytes payload. payload_len == 1 (lead) + child_span(7+child_size) + lp(name)[4+n] + 12 + optional lp(id)[4+m] (405/405).
- **fields:**
  - 0 u8 lead/version flag = 0x01 (405/405).
  - +1 the inline 0x260b child = the parameter's automation breakpoint/value data (child_span 41–785; child block_type always 0x0001).
  - cend (= 1 + child_span): lpstr parameter DISPLAY NAME (u32 len + ASCII), e.g. `Master Bypass`, `Gain`, `Key`, `Scale`. Let o1 = cend + 4 + n.
  - o1+0 u32 parameter_index — per-PLUGIN 1-based position (NOT a global constant; `Master Bypass` seen as both 1 and 26; `Band 1 Frequency`=3 collides with Key=3 across plugins).
  - o1+4 8 bytes per-param flag/type block — constant per param name but pattern varies by param (Gain/Feedback/etc. `00 01 01 00 00 00 00 00`, Key `00 00 01 0c 00 00 00 00`, Scale `00 00 01 1d 00 00 00 00`, Master Bypass `01 00 01 02 00 00 00 00`).
  - o1+12 optional lpstr parameter-ID short string (e.g. `KeyP`, `MasterBypassID`, `gain`); ABSENT for 74/405 (payload ends at o1+12), PRESENT for 331/405.
- **notes:** block_type word is a strict biconditional for the id-string: 0x0002 ⇔ id present (331); 0x0001 ⇔ id absent (74), 0 violations. This is the leaf that names automation lanes. No GUID tags in its own trailing fields (they live inside the 0x260b child, if at all).
- **confidence:** high (405/405 across 13 sessions).

### 0x260b — LEAF value-record blob (plugin/insert view-state)

- **kind:** LEAF (0/405 have any nested block).
- **size:** variable, 32–776 bytes payload. plen = 24 + 8*count + T, count = u32@10, T ∈ {0,6} (405/405). count ∈ {1,3,5,9,12,13,15,94}. The 6-byte trailer's driver is UNDETERMINED (NOT driven by u16@8).
- **fields:**
  - 0 u8 flag = 0x01 (405/405).
  - +1 u16 magic/subtype = 0x0146 LE (405/405).
  - +3 u8 pad = 0x00 (405/405).
  - +4 u32 self_length = payload_len − 10 (405/405).
  - +8 u16 variant/subtype selector ∈ {0,1} (0→40, 1→365). NOT the 6-byte-tail driver (u16@8=1 occurs with both trailers); exact meaning undetermined.
  - +10 u32 record_count — drives the trailing array with 8-byte stride; values {1,3,5,9,12,13,15,94}.
  - +14 u16 constant = 0x0004 (405/405). (0x260a differs here — carries 0x0002.)
  - +16 u16 index/count field = record_count−1 in the 0-trailer form; in the 6-trailer form it does not equal count−1.
  - +18 variable record array (+ 0/6-byte trailer) — built from recurring 4-byte interned tokens (e.g. a5d4e800, f34ff86c) rather than clean 8-byte position+value pairs; per-record semantics remain undecoded.
- **notes:** Fixed ancestry 0x260b → 0x260f → 0x2614 → 0x2616 → 0x2627 → 0x261b (405/405), under the 0x2616 plugin-state cluster. No `2A 00 00 00` GUID tags. Related to but NOT byte-identical with 0x260a (shares byte0=0x01 and magic 0x0146 but u16@14 differs, 0x0004 vs 0x0002; different parents). Header offsets/self_length/leaf/parent are high-confidence; the innermost record layout is only speculatively decoded.
- **confidence:** medium.

### 0x2611 — insert/plugin-instance entry (insert chain)

- **kind:** CONTAINER (immediate children in order: 0x2036 then 0x260a, with 1 own separator byte between them).
- **size:** fixed 83-byte payload (51/51) = 0x2036 span 43 + one 0x01 separator + 0x260a span 39.
- **fields:**
  - 0 (43 B) child 0x2036 (insert descriptor): block_type 4, size 36; payload fields include u32@12 = insert_ID (1-based ID, not a sequential index — e.g. `[1,2,3,5,4]`) and an 18-byte tail where the byte at 2036-payload offset 17 is 0x01 (44×) / 0x00 (7×), NOT fully constant.
  - +43 u8 own separator byte = 0x01 (51/51) — the block's only own byte.
  - +44 (39 B) child 0x260a (automation-data blob): block_type 1, size 32; payload begins with magic `01 46 01 00` (same family as 0x260b). Near-constant except an 8-byte GUID-like value near the end.
  - (+21 u32 insert_ID — same bytes as the 0x2036 payload offset-12 field; the join key to the insert descriptor.)
- **notes:** Immediate parent 0x2621 (44×) or 0x2620 (7×); grandparent 0x2624 (51/51). 51 instances / 10 sessions.
- **confidence:** high.

### 0x203a — single-slot view/lane attribute wrapper

- **kind:** CONTAINER (exactly one immediate child, always 0x2037; 0 own bytes before the child; 1 trailing scalar byte after it).
- **size:** fixed: own size field 12, payload 10 bytes (181/181) = a 9-byte inline 0x2037 child (header-only, empty payload) + 1 trailing scalar byte.
- **fields:**
  - 0 (9 B) inline 0x2037 child, header-only: `5A0100020000003720`, empty payload (181/181). own-bytes-before-child = 0.
  - +9 u8 trailing mode/enum scalar (NOT part of the child): 4-valued — 0x01 (160), 0x03 (17), 0x00 (3), 0x02 (1). Value 0x03 occurs iff the immediate parent is a 0x2580 automation lane (17/17). NOT a fixed boolean.
- **notes:** own block_type 0x0001. Parents (smallest enclosing, no intermediate encloser): 0x2015 (82), 0x200a (54), 0x2589 (28), 0x2580 (17). "This view/lane has attribute X = <small enum>."
- **confidence:** high (181 instances / 11 sessions).

### Per-track ordinal / view / edit-state lists

### 0x202a — track-GROUP definition (Edit/Mix group)

- **kind:** CONTAINER (exactly one immediate child, 0x258e = per-group display/state; 298/298).
- **size:** variable; payload_len 101–395 bytes. Formula (298/298): payload_len = 4 + name_len + 11 + 2*member_count + 12 + child_span, where the trailing 12 = 0xFFFE(2) + 8-byte trailer + 0xFFFF(2).
- **fields:**
  - 0 u32 name_len; +4 char[name_len] group name ASCII (e.g. `<ALL>`, `Drums`, `Toms`, `VOX A-B`).
  - +(4+name_len) u8 const = 0x02 (record-type/version marker).
  - +(5+name_len) u32 group_id (1-based ordinal; = 0xFFFFFFFF iff the built-in `<ALL>` group — biconditional 38/38).
  - +(9+name_len) u8 flag_a, +(10+name_len) u8 flag_b (only ever equal: (1,1)×150, (0,0)×148; `<ALL>` always (0,0)).
  - +(11+name_len) u16 member_count.
  - +(13+name_len) u16 separator = 0x0000 (constant).
  - +(15+name_len) u16[member_count] member track ordinals, 0-based (0..N−1); for `<ALL>` exactly range(N).
  - +(15+name_len+2*member_count) u16 delimiter = 0xFFFE (NOT the record end — 10 more bytes follow).
  - +(17+name_len+2*member_count) u32 trailer_a: 0x00000001 (×230), 0x00018010 (×54), 0x00000000 (×14).
  - +(21+name_len+2*member_count) u32 trailer_b = 0x00000000 (constant).
  - +(25+name_len+2*member_count) u16 = 0xFFFF (constant; immediately precedes the child).
  - +(27+name_len+2*member_count): the 0x258e child block.
- **notes:** Parent container 0x202b (one per session, holds every 0x202a). Each group is emitted TWICE per session (Edit-group list + Mix-group list), so instance count is 2× the distinct-group count. group_id is 1-based but NOT guaranteed contiguous (gaps from deleted groups). Encodes per-track ordering/membership directly as a 0-based u16 ordinal list.
- **confidence:** high (298 instances / 19 sessions).

### 0x202c — region/clip-group definition

- **kind:** LEAF.
- **size:** variable. Header size field = 30 + 4*N; payload = 28 + 4*N; span = 37 + 4*N, N = member count @8. block_type 0x0002.
- **fields:**
  - 0 u32 id — per-session group ordinal/key (unique within a session; range 1–627).
  - +4 u32 reserved = 0 (832/832).
  - +8 u32 N — member count ({1,2,3,4,6,9,10,13,15,17,18,19}).
  - +12 u32[N] members — sorted (strictly increasing, no dups) region/clip-list index references; often a constant arithmetic stride (~72%): stride 1 = consecutive regions on one track; stride = track count ⇒ one region per track at the same position (cross-track group).
  - +(12+4N) u32 reserved = 0 (832/832).
  - +(16+4N) tag `2A 00 00 00` — GUID tag (832/832).
  - +(20+4N) 8 bytes GUID — per-group identity (unique within a session; repeats across backups).
- **notes:** Every session has exactly ONE 0x202d and ONE 0x202e — top-level SIBLING containers holding 0x202c entries (each container's first payload u32 = its #0x202c children). 0x202d holds the bulk; 0x202e a smaller subset (a co-equal second container, not an occasional alternate). Members are sorted index refs (the "track IDs" reading is unverified — evidence favors region/clip-list indices).
- **confidence:** high (832 instances; 632 non-Hipsters + 200 Hipsters).

### 0x2011 — window/pane geometry + visibility record

- **kind:** LEAF (0 children, 515/515).
- **size:** variable by block_type. bt==1 → payload 18 B (size field 20, span 27). bt==2 → payload 19 B (size field 21, span 28); bt=2 adds one extra trailing byte at offset 18. 345 bt=1 + 170 bt=2 across 19 sessions (515 total; 697 incl Hipsters).
- **fields:**
  - 0 u32-le x — left position, pixels (main window 63; max 1766).
  - +4 u32-le y — top position (main 44; max 970).
  - +8 u32-le width — pixel width (main 1377; transport 687; collapsed = 1; max 1915).
  - +12 u32-le height — pixel height (main 856; transport 102; collapsed = 1; max 1030).
  - +16 u8 flag — visibility/state; bt==2 strictly {0,1}; bt==1 mostly 0/1 with a scattered tail (64,72,255).
  - +17 u8 flag — visibility/state; bt==2 strictly {0,1}; bt==1 occasionally other values (0xd5 on 0x2507).
  - +18 u8 state — bt==2 only; ALWAYS 0 in corpus (170/170; reserved).
- **notes:** Geometry is (x, y, width, height), NOT LTRB (decisively: 195/483 nonzero have width<x, 231/483 have height<y; 32 records are 1×1 collapsed windows). Appears exactly once inside each of 24 distinct single-instance view-container parents (0x2016, 0x2017, 0x2019, 0x201a…0x259c) PLUS 2–5×/session inside 0x2552 (browser/list windows) = 25 distinct parent content-types. Per-parent geometry is a stable role (0x2019 height always 102; 0x2016/0x2017 the ~1377×856 main windows). These are LIVE window geometries, not static constants.
- **confidence:** high.

### 0x258e — per-track/per-clip display-state flags record

- **kind:** LEAF (469/469 zero children).
- **size:** fixed per session. bt==2: size field 61, payload 59, span 68. bt==3: size field 62, payload 60, span 69 (payload = size−2, span = 7+size). block_type is a SESSION-WIDE format-version tag (never mixed within a session). bt=3's extra byte (offset 59) is always zero, but head-flag coupling changes.
- **fields:**
  - 0 u8 flag — boolean 0/1 (206 set / 263 clear); mirrors offset 8 exactly.
  - +1 u8 flag — boolean 0/1. In bt=2 INDEPENDENT of offset 0; in bt=3 always EQUAL to offset 0.
  - +2 (2 B) reserved = 0 (469/469).
  - +4 u8 flag — most-often-set (354/469).
  - +5 u8 flag — rarely set (4/469).
  - +6 u8 flag — rarely set (5/469).
  - +7 u8 flag — boolean (129/469); NOT the strict inverse of offset 4.
  - +8 u8 flag — mirrored pair of offset 0 (equal byte-for-byte in every instance).
  - +9 (16 B) reserved = 0.
  - +25 u16 LE sub-field — usually 0; observed 0x03FF (1023) or 0x0001; ONLY nonzero under parent 0x204a (5 instances).
  - +27 (6 B) reserved = 0.
  - +33 u16 LE sub-field — usually 0; observed 0x03FF or 0x0001; ONLY nonzero under 0x204a (co-occurs with offset 25).
  - +35 (24 B) reserved = 0; bt=3 adds offset 59, also always 0.
- **notes:** Immediate parents (smallest enclosing): 0x202a (298), 0x2510 (133), 0x204a (38); each in all 19 sessions. No GUID, no strings, no count field. Offsets 25 and 33 are optional u16 fields (only under 0x204a), NOT padding. bt=3 couples off1 to off0 — a layout/version change, not purely a trailing-byte addition. Downgraded to medium because the SEMANTICS of every flag are unproven; offsets/widths/domain/mirroring/sizes/leaf-ness/parent set are high-confidence.
- **confidence:** medium.

### 0x2103 — empty presence/type tag

- **kind:** LEAF (header-only, zero payload).
- **size:** fixed 9-byte span, ZERO payload (block_type 0x0001, size field 2). Full bytes literally `5A 01 00 02 00 00 00 03 21` (591/591).
- **fields:**
  - (none) — the 2-byte content_type is the entire content; presence is the only information carried.
- **notes:** Emitted once as the FIRST and ONLY child at the head of each parent. Parent 0x2104 (590) or 0x2105 (1, MANOLITO); grandparent ALWAYS 0x2015 (591/591). The meaningful numeric payload lives in the 0x2104 parent (12-byte tail after the child: `<ef ff> <byte3> <u32 ∈ {1,3,7,62,63}> <5×00>`), not in 0x2103 itself. Present only in DWTS/MANOLITO/THE WIND sessions — feature-specific. Its own role as a pure type/presence marker is the only high-confidence claim; the parent's meaning remains plausible-but-unproven speculation.
- **confidence:** high (structure); role speculative.

### 0x200d — per-track edit/playlist-state wrapper

- **kind:** CONTAINER (exactly one immediate child, 0x200a, on all 262 instances).
- **size:** variable; payload_len == the span of the single 0x200a child (the child fills the whole payload). Dominant values 178 (×158), 345 (×30), 156 (×21), 232 (×14), 176 (×12); driven by the number of nested edit/automation sub-blocks (one 0x203b/0x2037 pair per lane).
- **fields:**
  - own frame block_type 0x0001 (262/262).
  - payload offset 0: a single 0x5A-framed 0x200a child spanning the whole payload. Child block_type is NOT fixed: 0x05 (226) or 0x0a (36).
  - Interior sub-block markers always present (262/262): 0x200a, 0x2015, 0x203b, 0x2037 (and 0x2434). Conditional: 0x2038 (36/262), 0x203d (18/262), 0x2580 (36/262), 0x203e (9/262).
  - Fixed per-lane pattern (verbatim, once per 0x203b descendant, 262/262): `5a 01 00 0f 00 00 00 3b 20 5a 01 00 02 00 00 00 37 20` (a 0x203b wrapper enclosing a 0x2037 leaf).
- **notes:** Immediate parent 0x261e (262/262) — a SINGLE track (track name is a length-prefixed ASCII string at 0x261e payload offset +27, e.g. `Click`, `MIX BUS`, `Verb 1`). Enclosing track-list container 0x2624. Exactly one 0x200d per 0x261e track (NOT a conditional subset); the 0x261e/0x2624 set is a distinct smaller set of tracks unrelated to the 0x210b named-entity count. 0x2434 is a per-track sub-record present in every 0x200d subtree.
- **confidence:** high for structure (kind/parent/child/size/pattern, 262/262 across 19 sessions); medium for the deeper interior-grammar semantics of the 0x2015/0x203b/0x2037/0x2434 sub-records.

### 0x2010 — per-track/per-lane waveform view-state wrapper

- **kind:** CONTAINER with exactly ONE immediate child (0x200a), followed by a VARIABLE-LENGTH trailing region (u32 count + count bytes + 20-byte fixed footer). block_type 0x0003 (51/51).
- **size:** variable. block_size field 177–351 B (payload 175–349, span 184–358). Driven by the 0x200a child's contents plus the variable trailing region.
- **fields:**
  - lead: 0 bytes — child 0x200a starts immediately at payload+0 (51/51).
  - `<0x200a child>` occupies payload+0 to (e − trail).
  - trailing region = u32 count + count extra bytes + fixed 20-byte footer:
    - trail+0 u32 count = number of extra state bytes that follow (observed 0,1,2,3; equals the byte-gap exactly).
    - trail+4 (count B) extra state/flag bytes (e.g. `82`, `4082`, `018182`; empty when count=0). NOT constant.
    - FIXED 20-BYTE FOOTER, anchored from block END (e):
    - [e−20:e−16] u8[4] MARKER = `01 00 60 40` (constant 51/51). NOTE: the leading `82` sometimes seen is the LAST count-driven extra byte, present only when count≥1 (50/51) — only these 4 bytes are truly constant.
    - [e−16:e−8] f64 view_extent (samples) — VARIABLE per track/lane (observed 4000000, 4300000, 11400000, 12800000; always >0; differs within one session).
    - [e−8:e] f64 vertical_zoom_ratio — 0.5 (×42), 0.608878 (×7), 0.794118 (×1), 0.731152 (×1); range 0.5–0.794.
- **notes:** Containment 0x2624 > {0x2620|0x2621} > 0x2010 > 0x200a. NOT master-indexed (block_type 0x0003; no 0x2010 zmark in the 0x0002 index). Count of 0x2010 == n(0x2620)+n(0x2621) per session; one 0x2624 per session. A writer MUST honor the variable-length trailing region (total trail = 4 + count + 20 B) and anchor the marker/f64 footer from the block END, NOT a fixed −21 offset (a fixed offset corrupts the count=0 instance). Structure is high-confidence; the semantic labels `view extent in samples` and `vertical waveform zoom ratio` are plausible only.
- **confidence:** high for structure; medium for the two trailing-f64 semantic labels.

### Misc 0x20xx flags / markers

### 0x2037 — presence/boolean marker leaf (set flag)

- **kind:** LEAF (no children, no payload; 0 children across all 16530 instances).
- **size:** fixed: size field always 2 (covers only the content_type word); block_type always 0x0001; payload length always 0. Verified 16530/16530 across all 26 corpus sessions. `end == zmark + 9`.
- **fields:**
  - (none) — zero payload; presence is the only information carried.
- **notes:** Its existence as a direct child signals that a per-parent feature is on. Appears as EXACTLY ONE direct child per parent instance across the whole 0x2038–0x203e playlist/view/edit parent family. Observed parents: 0x2038 (9837), 0x203b (3438), 0x203a (181), 0x203e (113), 0x203d (80), 0x203c (63), 0x2039 (3). NOT the single most common block type (ranks 10th in the 19-session scope). The one-per-parent rule is universal, not 0x203a-specific.
- **confidence:** high.
