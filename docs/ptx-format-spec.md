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

### Phantom blocks — do not recurse into leaf payloads

`0x5A` is the byte `'Z'`, and it occurs freely inside **leaf data** — ASCII strings, GUIDs,
plug-in FourCC codes, the file header. A block walker that descends into *every* block's payload
looking for `0x5A`-framed children will therefore manufacture **phantom blocks**: a stray `0x5A`
inside a leaf gets read as a frame header, yielding a bogus `content_type` and a tiny span. In
this corpus the naive full-recursion walk over 26 sessions produced **23 phantom "types"**
(26 block instances, 0.002% of all blocks) — their signature is unmistakable: a **single
instance**, span ~8–12 bytes, an **implausible/high `content_type`** (e.g. `0xd297`, `0x9de1`,
`0x4e58`, `0x2fa3`), sitting **inside another block's payload** (one `0x1900` was a `0x5A` in a
`0x1017` plug-in FourCC; one `0xd297` was in the file header). A few phantoms even land on a
plausible-looking low `content_type`, so range alone is not a reliable filter.

Real content-types are confined to `0x0002`, `~0x1000–0x27xx`, and `~0x4300–0x45xx`. A correct
parser bounds recursion **by the grammar** — it only looks for children inside block types known
to be containers, never inside leaves (strings, GUIDs, `0x1000`/`0x1017` records, etc.). The
`0x0002` master index is the authority on which blocks are real; anything the walk finds that the
index never references, and that sits within a leaf's declared payload, is a phantom.

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

### Additional types — tiers 31–60 by frequency

### 0x2066 — Ruler/grid position-label cache

- **Kind:** LEAF (no 0x5A children, confirmed 69/69). Inner block_type u16@z+1 always 0x0002 (69/69). Parent is deterministic by group count G: G=1 → parent 0x2588 (26/26); G=2 → parent 0x2581 (17/17); G=3 → top-level/no enclosing 0x5A block (26/26). The proposed "always co-located with a sibling 0x2056" is FALSE (a 0x2056 sits within ±4 blocks only 36/55; the rest are preceded by 0x2011 or 0x2595).
- **Size:** Variable, driven by group count G. Exactly three sizes: declared u32 size 111 (G=1), 200 (G=2), 289 (G=3); total span = declared_size + 8 (119/208/297 bytes incl. the 9-byte 5A header), verified 55/55. Per session there is always a G=1 and a G=3 instance; the G=2 (0x2581-parented) instance appears in only 17/26 sessions. 55 instances / 19 sessions (excl. huge "Hipsters" set); 69 / 26 including it.
- **Fields (payload-relative, offset 0 = z+9):**
  - `+0 u32 G` = number of position groups (observed 1..3); verified G == #groups 69/69.
  - `+4, 3*(G+4)` preamble = EXACTLY 3 records, each = `[G flag bytes][u32 == G]`. The trailing u32 of every record equals G (55/55). The G flag bytes carry small enum/bool values (observed 0x00/0x01/0x04); per-group view/selection flags that vary across instances. Preamble length is fixed at 3*(G+4) = 15/18/21.
  - Then G groups, each = `[u32 count == 5 (110/110)][5 length-prefixed ASCII strings]`. The 5 strings are independent per-timebase renderings (see notes), NOT one shared instant. Fixed slot widths: slot0=12 (Bars|Beats|Ticks), slot1=10 (Min:Sec), slot2=14 (Timecode), slot3=11 (Feet+Frames), slot4=11 (Samples). All 110 groups matched.
  - Tail (after last group) = `[u32 == G][G u32 words alternating 0xffffffff, 0x00000000, ... starting with 0xffffffff]`. Exact forms: G=1 → `01000000 ffffffff`; G=2 → `02000000 ffffffff 00000000`; G=3 → `03000000 ffffffff 00000000 ffffffff` (55/55). One word per group — a per-group −1/0 flag/selection-mask array.
- **Notes:** Role is a display/view-state cache of pre-rendered ASCII position labels, each timeline value rendered in all 5 PT timebases (Bars|Beats|Ticks, Min:Sec, Timecode, Feet+Frames, Samples). PT recomputes it from the current edit/ruler view + session tempo/format — NOT authoritative session data (string values vary session-to-session). The 5 slots in a group demonstrably diverge (e.g. Samples='960000' = 20s@48k but Min:Sec='0:05.000'), so each slot is an independent per-timebase value; the 3 groups read like small/default/large grid increments. PARSING NOTE: `_raw_block_bounds` returns e = z+7+size, overshooting by one — true last content byte is z+6+size; ignore the single artifact byte the bounds sweep appends when reading the tail. Full schema validated 69/69 (incl. 14 Hipsters instances), byte-exact in ≥2 sessions per claim (DWTS, Quality Time, Reverse Rewire, DOPE).
- **Confidence:** high.

### 0x2056 — Per-view display/zoom-state record

- **Kind:** LEAF (0 children, all 55). block_type = 6. Parent distribution: top-level/ROOT 19×, 0x2588 19×, 0x2581 17×. Every non-root parent (36/36) also contains a 0x2066 sibling; 0x2552 and 0x258c also co-occur under those parents (19× each).
- **Size:** Fixed. size field = 43 (0x2B) → 41-byte payload, 50-byte total block span (9-byte header `5A 06 00 2B 00 00 00 56 20` + 41 payload). 55 instances / 19 sessions.
- **Fields (payload-relative, payload starts at z+9):**
  - `+0 u32 flag A`: 0 or 1. INVARIANT: = 0 whenever parent==0x2588 (19/19); = 1 otherwise EXCEPT one all-zeros-prefix outlier under 0x2581 (35× = 1, 1× = 0).
  - `+4 u32 = 0` (const, all 55).
  - `+8 u32 field B`: {0:1, 256:19, 1024:35} — small view enum.
  - `+12 u32 field C`: {512:28, 768:1, 1024:26}.
  - `+16 u32 field D`: {256:10, 768:45}.
  - `+20 u32 = 0` (const, all 55).
  - `+24 u32 field E`: {0:50, 256:2, 768:3} — mostly 0.
  - `+28 u32 packed flags`: byte28=0 (const); byte29 ∈ {0,1} (0:22/1:33); byte30 ∈ {0,1} (0:39/1:16); byte31 ∈ {2,3} (2:51/3:4). Observed values {0x02000000, 0x02000100, 0x02010100, 0x03010000}. Middle-flag semantics inferred, not proven.
  - `+32 u32 field F`: exactly {0x01000000:45, 0x08000000:10}, i.e. byte35 ∈ {1,8} (bytes 32–34=0). byte35 always 1 when parent==0x2588.
  - `+36 u8 = 0`; `+37 u8 = 0`; `+38 u8 = 0` (const).
  - `+39 u8 view value`: {80,96,100}. INVARIANT: = 100 whenever parent==0x2588 (19/19, no violations); = 80 or 96 otherwise. Plausibly a default track/lane height or zoom preset (inferred, not proven).
  - `+40 u8 = 0` (const, all 55).
- **Notes:** PT-recomputed per-view display/zoom state (one per view container alongside the 0x2066 position cache; 19 at top level). The trailing view value (80/96/100) looks like a default track/lane height or zoom preset. Treat as opaque/copyable — do not hand-author. **LOW-CONFIDENCE:** the SEMANTICS of fields B/C/D/E, the middle packed flags, and byte39 (height vs zoom) are INFERRED from value ranges, not proven; the const-0 fields and value distributions ARE proven. The sibling-0x2066 claim holds only for the 36 non-root instances (top-level instances have no container/sibling).
- **Confidence:** medium.

### 0x2070 — Default/template view-settings blob

- **Kind:** LEAF (no children; first-child scan None for all 57/57). block_type u16@z+1 == 1. Appears exactly 3× per session, one under each of parents 0x2074, 0x2071, 0x2072. The 0x2074 parent is nested under 0x206a (19/19); the 0x2071 and 0x2072 parents are top-level (19/19 each). 57 instances / 19 sessions (Hipsters excluded).
- **Size:** Fixed block span = 172 bytes (e−z); size-field u32@z+3 == 165; payload = 163 bytes (payload_len = size − 2, because content_type u16@z+7 is inside the size-counted region: span=[z, z+7+size], payload=[z+9, z+7+size]). 165 is the size field, NOT the payload length. Single size across all 57.
- **Fields (payload-relative, offset 0 = z+9):**
  - `+0 u8 discriminator`: 0x00 when parent==0x2074, 0x02 when parent==0x2071 or 0x2072. The ONLY payload byte that ever varies (57/57, exact parent correlation {(0x2074,0x00):19, (0x2071,0x02):19, (0x2072,0x02):19}). payload[1] and payload[2] always 0x00, so could equally be read as the low byte of a u16/u32.
  - `+1, 162 bytes` CONSTANT template (payload offsets 1..162; byte-identical across all 57). Complete non-zero map (offset=value): [3]=01 [4]=01 [7]=08 [19]=08 [23]=01 [24]=02 [25]=04 [26]=40 [27]=07 [28]=0a [29]=05 [30]=41 [31]=80 [36]=01 [37]=01 [39]=01 [40]=01 [42]=01 [45]=01 [99]=01 [100]=01; every other offset is 0x00.
  - `+3, 6 bytes` fixed sub-field = `01 01 00 00 08 00` (payload[3..8], inclusive).
  - `+23, 9 bytes` fixed "default-settings signature" = `01 02 04 40 07 0a 05 41 80` (payload[23..31]; NOT [24..32] — the proposed window was shifted +1).
  - `+99, 2 bytes` = `01 01` (payload[99..100]); with trailing zeros payload[99..102] = `01 01 00 00`.
- **Notes:** Pure PT-recomputed default view state; treat as opaque. Fixed constant across the entire corpus except payload byte 0 (a parent-derived discriminator). Nothing in the corpus (single 0x00 variant, single 0x02 variant) lets us decode the interior fields into meaningful semantics — interior offsets are reported as observed constants, not a decoded schema.
- **Confidence:** high.

### 0x252c — Built-in Click II plugin "Complete Controls State" snapshot

- **Kind:** LEAF (no children); parent ALWAYS 0x200b (434/434, rigorous innermost-enclosing nesting). Each 0x200b holds exactly one 0x252c plus one 0x200a name block (1:1, never shared). Full ancestor chain (inner→outer): 0x200b → 0x261c → 0x2624.
- **Size:** size field u32@z+3 = 95, 106, or 107 (counts 61 / 45 / 328); payload = size − 2 = 93 / 104 / 105. 434 instances / 12 sessions. NOTE: size does NOT cleanly map to "long" vs "inline" form — the 45 size-106 blocks split into 38 long-form and only 7 inline-name-form. All 61 size-95 are the Rhythmic-Click shorter form; all 328 size-107 are Poly-Click long form.
- **Fields (payload-relative):**
  - `+0 char[12]` plugin type-id, stored with each 4-byte group byte-swapped. VERIFIED: swap4('igiDPleFyloP')='DigiFelPPoly', swap4('igiDRleFtyhR')='DigiFelRRhyt' — the byte-swapped FIRST 12 chars of the name ONLY (NOT the full 16-char name; the full name appears separately as ASCII at +24). 2 values: Poly Click (373 inst) / Rhythmic Click (61 inst).
  - `+12 u32-LE A` = inner blob length: 0x58=88 (Poly), 0x4c=76 (Rhythmic). Constant per plugin type (434/434).
  - `+16 u32-BE B` = the SAME value as A but big-endian (byte-swapped mirror of A). A(LE)==B(BE) verified 434/434.
  - `+20 u32 form discriminant` (NOT a plain −1 sentinel): 0xffffffff → long "Complete Controls State" form (427/434); 0x01000000 → compact inline-name form (7/434, all in the 7 Reverse Rewire sessions). Does NOT correlate with block size (both forms occur at size=106).
  - `+24 char[16]` ASCII plugin name 'DigiFelPPolyelck' or 'DigiFelRRhytelck' (offset ALWAYS 24, 434/434; NOT length-prefixed). LONG form: followed at +40 by ASCII 'Complete Controls State\0'. INLINE form (the 7 blocks): followed by a small numeric header.
  - `+72, 4 bytes` constant marker 0x01010101 immediately before the first control token (434/434).
  - `+76, 6 bytes` ASCII control token 'd_c001' (fixed offset 76, 434/434). Followed by 2 pad bytes (00 00) then an 8-byte control-value blob at +84.
  - `+84, 8 bytes` control-value blob for d_c001. CONSTANT per plugin type: Poly = `3f a4 7a e1 47 c5 a1 ca` (373/373), Rhythmic = `3f df ff ff ff e1 47 af` (61/61). f32/f64 interpretation UNCONFIRMED/speculative; bytes never vary within a plugin type.
  - `+92, 6 bytes` ASCII control token 'l_c002' — present ONLY in the Poly Click forms (373/373 at fixed offset 92); ABSENT in the 61 Rhythmic blocks (hence the shorter 93-byte payload). Followed by a trailing all-zero blob.
- **Notes:** A CANNED plugin-defaults blob, NOT per-instance automation: only 4 distinct payloads exist corpus-wide (3 serialization forms of Poly Click + 1 Rhythmic Click); control-value bytes are byte-for-byte identical across all 373 Poly / all 61 Rhythmic instances. Always attached to the plugin/insert container 0x200b as a leaf sibling of a 0x200a name block. This is PT's built-in-Click default control state — do NOT over-claim a precise per-parameter schema; d_c001/l_c002 are FIXED-offset tokens with constant values, not per-instance parameters.
- **Confidence:** high.

### 0x2638 — Per-path signal-format sub-record

- **Kind:** LEAF (0 immediate children, 173/173). block_type u16@z+1 = 1. Parent is 0x2602 in 171/173 cases and 0x1021 in 2/173 (two strays). Each 0x2602 that has a 0x2638 holds exactly one 0x2601 + one 0x2638; most 0x2602 nodes (and ALL nodes in the other 16 sessions) have only a 0x2601 child. Grandparent verified 0x2603 (the single I/O path list) 100%.
- **Size:** Fixed: size u32@z+3 = 31; total span end−zmark = 38 bytes; payload (z+9..end) = 29 bytes. All verified 173/173. 173 instances across only 3 of 19 sessions.
- **Fields (payload-relative):**
  - `+0 u32 path format-class ordinal` (values 1,2,3,4,5,6,9,10,13,19; hi 3 bytes @1..3 always 0). NOT a free-running index — tightly correlated with the width field @16: ord 1..6 → byte17=8, ord 9..10 → byte17=16, ord 13/19 → byte17=24 (173/173). One 0x2638 per path.
  - `+4 u32 = 2` (const, 173/173).
  - `+8 u8 = 0` (const, 173/173).
  - `+9 u32 = 1` (const, 173/173).
  - `+13 u32 = 1` (const, 173/173).
  - `+16 u32 width/bit-depth field` = 0x800/0x1000/0x1800 i.e. exactly 256*byte17 with byte17 ∈ {8,16,24} — 159/9/5 split. byte16,18,19 always 0. Likely a channel/bit-width descriptor.
  - `+20 u32 small enum/flag` = 0/256/512/768 i.e. 256*byte21 with byte21 ∈ {0,1,2,3} — 92/54/12/15 split. byte20,22,23 always 0. (Raw u32 is 0/256/512/768, NOT 0/1/2/3; 0/1/2/3 is byte[21] only.)
  - `+24, 5 bytes = 0` (const, 173/173).
- **Notes:** A format/width descriptor for an I/O routing endpoint, part of the recomputable I/O-setup graph. Sits under an I/O path definition (0x2602) alongside that path's single 0x2601 child; the 2 strays sit directly under a 0x1021 bus/path node (whose payload carries the ASCII bus name, e.g. "BACKING TRACKS"). IMPORTANT: 0x2638 is NOT emitted just because the path list is populated — every corpus session has a full 0x2603 path list (132–312 nodes), yet only 3 sessions contain any 0x2638, and even there it attaches to only a subset of paths; the real trigger (PT version? specific bus/format config?) is unknown. The lpstr path-name + 0x2A GUID live in the PARENT node but NOT at parent-payload offset 0. **MEDIUM-CONFIDENCE:** schema/offsets/consts are HIGH (173/173 byte-exact); the SEMANTIC labels for @0 (format class), @16 (width), @20 (enum) are context-inferred, not independently proven.
- **Confidence:** medium.

### 0x2425 — Media/file cross-reference record

- **Kind:** LEAF (no 0x5A children; 39/39). Parent ALWAYS 0x2426 (39/39). 39 instances across 11 corpus files, but only ~5 DISTINCT session-contents (DWTS SHOW/__001/__002 are byte-identical; the 7 Reverse Rewire backups collapse to 2 distinct signatures).
- **Size:** Variable: 110/115/144/145/150/161/176/800 bytes (span = [z, z+7+size]). Fully determined by N (index count) and path string lengths: payload = 4 + 5*N + 4 + Σ(4+len_i over path components) + 4 + 6. All 39 close EXACTLY (zero slack). The 800-byte instances are just the largest N (=142), not a special "hybrid" shape.
- **Fields (payload-relative):**
  - `+0 u32 N` = index-entry count (≥1 in ALL 39; NOT a "kind (1 or 2)" field — that was N misread. No separate header before N, no opaque leftover u32s).
  - `+4, 5*N` index list: N entries, each 5 bytes = `<u8 tag=0><u32 item_index LE>`. Tags all 0 (39/39). Indices strictly monotone +1 within each block. Stride is tag-THEN-index (`00 XX XX XX XX`); the alternate `<u32 idx><u8 pad>` reading yields non-monotone garbage and is WRONG.
  - `+(4+5*N) u32 path_count` = number of path components. Observed 3, 4, AND 6 — NOT fixed at 3. True path depth (drive / folder chain / '<file>.ptx').
  - Then `path_count` length-prefixed ASCII components: each = u32 len (LE) + len bytes. comp[0]=volume ('MAC BOOK BU','Studio 1 Audio','Arts T1 SSD'), middle=folder chain, last=leaf filename ('...ptx').
  - Immediately after the last component: `u32 volume identifier` — NOT a per-file checksum. CONSTANT PER VOLUME: `50 5D 4D C8` → 'MAC BOOK BU', `88 42 74 CB` → 'Studio 1 Audio', `19 E6 18 D2` → 'Arts T1 SSD'. Different files on the same volume share the same 4 bytes.
  - Final `6 bytes` of 0x00 padding ending the block (all 39). Trailer is 10 bytes total = `<u32 volume_id><6× 00>`, with the volume id at the START.
- **Notes:** Media/file cross-reference under the 0x2426 container: each instance binds a contiguous run of global "referenced item" indices to a single on-disk file location (volume + folder chain + filename). ALL 39 carry BOTH an index list AND a filesystem path — they are NOT two co-existing shapes (path-only vs idxlist-only). Within a session the per-instance index ranges are contiguous, start at 0, and partition ONE global 0..M item-index space (e.g. DWTS: 0-14,15-29,30-171,172-313,314-328,329-343,344-361) — strongest evidence for the "referenced item indices" semantics. Genuinely undecoded: the SEMANTIC target of the global item indices (which table they index into) and the exact derivation of the per-volume 4-byte id.
- **Confidence:** high.

### 0x1041 — Groove-template descriptor

- **Kind:** CONTAINER. Parent ALWAYS 0x1040; ALWAYS exactly one immediate child, ALWAYS 0x1042 (child-count {1:38}). Frame block_type @z+1 = 0x0002; content_type @z+7 = 0x1041 (all 38).
- **Size:** Block SPAN = 104 bytes for the built-in default (declared size field @z+3 = 97; payload = span − 9 = 95); span = size_field + 7. Real imported grooves grow: Bianca's 'MPC 65% 16th Swing' is span 869 / size-field 862 / payload 860. Growth is entirely in the 0x1042 grid child (which for the custom groove holds 9× 0x1043 grid-point sub-blocks; the built-in 0x1042 is span 47 with no children). The 0x1041 header itself is fixed-length up to the name.
- **Fields (payload-relative):**
  - `+0 u16 flags` = 0x0000 in 34/38, 0x0100 in 4 (COGNAC ×2 + THE WIND ×2, NOT "the COGNAC set"). Semantics unpinned (display/preview-style bit) — only 4 samples.
  - `+2 u16 = 1` (const; likely an enabled/present flag).
  - `+4 u8 = 0` (const).
  - `+5 u16 = 100 (0x64)` (const, all 38). DOWNGRADED: NOT the per-template timing/quantize strength — stays 100 even for the 'MPC 65% 16th Swing' groove (whose 65% swing lives as tick deltas in 0x1042/0x1043). Best read as a fixed/default global apply-strength value, semantics unconfirmed.
  - `+7 u16 = 100 (0x64)` (const, all 38). Same downgrade as +5 (proposed 'velocity strength percent' unconfirmed — never varies).
  - `+9 u16 = 100 (0x64)` (const, all 38). Same downgrade as +5 (proposed 'duration strength percent' unconfirmed — never varies).
  - `+11 u16 = 0` (const).
  - `+13 u32 name_len`, then name_len ASCII bytes (length-prefixed string). 37× '<groove saved with session>' (len 27); 1× 'MPC 65% 16th Swing' (len 18).
  - `+17+name_len`: the inline 0x1042 grid sub-block begins (frame magic 0x5A, block_type 0x0001, content_type 0x1042) — the sole child in all 38.
- **Notes:** One appears per saved groove/quantize template in the session's groove-template table (parent 0x1040). Every corpus session with grooves carries exactly two 0x1041, and in 37/38 both are the built-in placeholder named '<groove saved with session>' (identical name in both slots — NOT two differently-named defaults). A user-imported groove replaces one slot with a real template (only corpus example: Bianca). The actual grid lives entirely in the single 0x1042 child (and its 0x1043 grid-point children). **MEDIUM-TO-LOW-CONFIDENCE on semantics** of +5/+7/+9 (invariant 100/100/100, never observed to vary) and the +0 flag meaning (4 samples).
- **Confidence:** high for structure; medium-to-low for the semantics of +5/+7/+9 and the +0 flag.

### 0x1042 — Groove-template grid data

- **Kind:** LEAF for empty-slot stubs (0/38 have enumerated children); CONTAINER for a populated custom groove (inline 0x1043 children). Parent is 0x1041 (38/38); grandparent 0x1040 (37/37 stub instances checked). block_type u16@z+1 = 1 (all 38).
- **Size:** Stub: size-field u32@z+3 = 40, span (z→e) = 47, payload = 38 (37/38 instances, byte-identical across 19 sessions). '40' is the size field, NOT the span. The one custom groove (Bianca Long Road): size=814, span=821, payload=812.
- **Fields (payload-relative):**
  - `+0 u8=0x04, +1 u8=0x04` — constant '04 04' prefix (both stub and custom; also mirrors the '04 04' in the parent 0x1041). Semantic (grid-resolution/quantize enum) UNCONFIRMED — only the constant bytes are verified.
  - `+2 u16 = 100 (0x0064)` (37/37 stub; custom also 100).
  - `+4 u16 = 0` — 37/37 STUB instances ONLY. NOT universal: the custom groove has 100 (0x0064) here, so '+4=0' is a stub-only constant.
  - `+6 u16 = 100 (0x0064)` (37/37 stub; custom also 100).
  - `+8 u8 = 1` (37/37 stub; custom also 1, then continues +9 = `01 00 01 00 ...`).
  - `+9..+37 (stub)`: 29 zero bytes. Full stub payload byte-identical across all 37: `04 04 64 00 00 00 64 00 01` + 29× `00`.
  - CUSTOM groove body (single example, provisional): after a 30-byte fixed header (`04 04`, then `64 00 64 00 64 00`, `01 00 01 00 01 00`, `00`, then u32=15000 (0x00003a98), then a small fixed run) comes a length-prefixed ASCII description string (u32 len=43, 'MPC 65% Swing. Fixed Velocity. No Duration.'), then a u32 = record count (17), then 17 inline 0x1043 grid-point sub-blocks.
  - 0x1043 grid-point record (each size-field=36, payload=34): 4× u64 + u16 tail = 32+2 = 34 bytes. u64_0=start tick, u64_1=end tick (== next record's start; contiguous grid), u64_2=reference/quantized tick, u64_3=constant 120000 (0x1d4c0, one PPQ bar), then u16 tail ALTERNATING 0x005a/0x005b across the 17 records (a flag/rounding bit, NOT a fixed 0x005b). The proposed '3× u64 + u16 tail' undercounts by one u64.
- **Notes:** The single required child of 0x1041 (a groove-quantize template slot). Every session carries a fixed 2-slot groove list (2× 0x1041 → 2× 0x1042), grandparent 0x1040. For an EMPTY/unused slot (parent 0x1041 named '<groove saved with session>') it is a fixed 38-byte-payload stub; for a real IMPORTED groove it expands into header + description string + u32 record-count + inline 0x1043 grid-points. The two instances are groove-list SLOTS, and stubs are EMPTY slots — NOT 'built-in default grooves'. **HIGH for the empty stub** (37/37 byte-identical); **MEDIUM-LOW for the custom-groove internals and the 0x1043 record schema** — only ONE custom instance exists (Bianca Long Road), so those details are inferred from a single example and are provisional. The '04 04' prefix and the u16 percents are UNCONFIRMED (could be view/quantize state PT recomputes).
- **Confidence:** high for the empty-slot stub; medium-low for the custom-groove internals / 0x1043 record schema (single example).

### 0x1055 — Audio-region catalog list-header

- **Kind:** Count+list header. Populated → CONTAINER whose inline children are 0x1053 (audio-region records; each 0x1053 in turn contains 0x1051/0x104f clip/anchor sub-records). Empty → LEAF (payload is just the 4-byte zero count). The TOPLEVEL catalog has NO enumerated parent (all 19 sessions); the always-empty stub's immediate parent is a single 0x2430 (which itself has no enumerated parent). CAVEAT: "parent" here means the immediate enclosing block in the size-driven enumeration; PT's true logical region/track container is not emitted as a spanning block, so neither 0x1055 is inside a per-track block.
- **Size:** Empty: span 14, size u32@z+3 = 6, payload 4 bytes (just the zero count) — 37/38 instances. Populated: span 462, size 454 — 1 instance (MANOLITO SIMONET), count=1, holding one inline 0x1053. NOTE the frame reports span = size + 8 and includes the following block's leading 0x5A in its end bound; the proposed '13 span'/'461 span' figures are off by one.
- **Fields (payload-relative):**
  - `+0 u32 count` = number of inline 0x1053 audio-region records that follow. VERIFIED count == #enumerated inline 0x1053 children (1==1 populated, 0==0 in all 37 empty). The ONLY field of the empty stub.
  - `+4..` : `count` inline 0x1053 region records (each a `5A..53 10..` block). Observed body (single example, MANOLITO region 'C0004_1'): length-prefixed name at 0x1053 payload+0 (u32 len=7, then 7 ASCII 'C0004_1'), then a u32 (0x00000008), then inline sub-blocks — pairs of 0x1051 and 0x104f (8 pairs here) carrying the clip/anchor entries. The 0x1053/0x1051/0x104f schema is summarized only.
- **Notes:** Appears EXACTLY TWICE per session (NOT per-track — count fixed at 2 regardless of track count) with two distinct duties: (a) a single session-level TOPLEVEL audio-region catalog (populated when the session has saved audio regions, empty otherwise); (b) a single always-empty count=0 stub nested inside one lone 0x2430 wrapper, sitting next to the 0x1058 MIDI-region-catalog analog. Instance tally: 38 = 19 TOPLEVEL (18 empty + 1 populated) + 19 always-empty 0x2430-wrapped. This is real saved-region catalog data, NOT PT-recomputed view state. **HIGH for the header itself** (count == inline-child count; all 37 empty stubs are '00 00 00 00'); **MEDIUM-LOW for the 0x1053 record body** — only ONE populated catalog exists in the loadable corpus (MANOLITO), so it rests on a single example. The 0x2430 stub's exact purpose is not established. 0x2430 wrapper full bytes: `5a 01 00 0f 00 00 00 30 24 5a 01 00 06 00 00 00 55 10 00 00 00 00`.
- **Confidence:** high for the header; medium-low for the 0x1053 record body.

### 0x1058 — MIDI-region / clip catalog list-header

- **Kind:** Count+list header; block_type u16@z+1 == 1 (all 38). Populated top-level catalog → CONTAINER whose immediate children are all 0x1057 (MIDI-region records). Empty → LEAF. Parents: (1) 0x242e for the one always-empty stub per session (the 0x242e wrapper contains ONLY the 0x1058 as its sole immediate child — NOT a 0x1055/0x2424/0x2426 cluster); (2) top-level / parent=None for the session catalog (both empty and populated forms). The top-level catalog is preceded by 0x2634 when empty and by 0x2628 when populated — a perfect discriminator (9/9 empty→0x2634, 10/10 populated→0x2628).
- **Size:** 13 bytes span (size u32@z+3 == 6; payload 4 bytes = '00 00 00 00') for every empty instance (28/38: 19 wrapped in 0x242e + 9 top-level empty catalogs). Populated top-level catalogs scale: span 359 (1 region), 1655 (6), 1721 (6), 13709 (13) — 10 populated instances, 1..13 regions.
- **Fields (payload-relative, +0 = z+9):**
  - `+0 u32 count` = number of inline 0x1057 region records. VERIFIED count == immediate-child 0x1057 count == any-nested 0x1057 count in ALL 10 populated (6→6, 13→13, 1→1) and == 0 with payload '00 00 00 00' in all 28 empty.
  - `+4..` : `count` MIDI-region records, each an inline `5A`/block_type=3/content_type=0x1057 block. Each 0x1057 payload BEGINS at its own +0 with a length-prefixed ASCII region name (u32 len + len bytes), e.g. `0c 00 00 00` 'Drums 1 midi', `11 00 00 00` 'Drums 1 midi.dup1', 'Inst 2.01', 'MIDI 1'. All 62 0x1057 children parse a valid length-prefixed name at +0. Note/anchor sub-structure inside each 0x1057 was NOT decoded (out of scope for the 0x1058 header).
- **Notes:** The MIDI analog of the audio 0x1055. Appears exactly twice per session: (a) one 0x242e-wrapped ALWAYS-EMPTY stub (session-level fixture, count=0 — NOT per-track); (b) one top-level SESSION-LEVEL catalog, EMPTY when no MIDI regions and POPULATED (inline 0x1057) when there are. The populated catalog is the SAME top-level block as the empty top-level stub, just filled in — not a separate role. The empty stub shape is uniform: `5a 01 00 06 00 00 00 58 10 00 00 00 00`. **HIGH** on every header field (invariant across 38 instances / 19 sessions; count==child-count in all 10 populated; preceding-block discriminator 19/19); **MEDIUM** only on the child 0x1057 internal note/anchor schema, which was not decoded.
- **Confidence:** high (header); medium (child 0x1057 internals).

### 0x258c — Ordered visibility/state index

- **Kind:** LEAF (no children — 0/44 instances; header block_type u16@z+1 == 0x0001 for all 44). Immediate PARENT is one of exactly two view-descriptor flavors: 0x2553 (25 instances) or 0x2588 (19 instances). Ancestor chains (inner→outer): 0x2588 → 0x2597 (always, 19/19); 0x2553 → 0x258b → 0x2016 (9), or 0x2553 → {0x258b|0x2551} → 0x2551/0x2587 (16).
- **Size:** Variable. The DECLARED u32 size FIELD (@z+3) ∈ {6, 18, 33, 36, 39}; the total block SPAN (= 7 + size_field) ∈ {13, 25, 40, 43, 46}. Payload = size_field − 2 = 4 + 3*count. Formulas: size_field = 6 + 3*count; span = 13 + 3*count. Observed counts: 0 (empty), 4, 9, 10, 11. count=0 (empty) occurs only under 0x2553 (8×); under 0x2588 count is invariantly 4.
- **Fields (payload-relative):**
  - `+0 u32 count` = number of records (LE).
  - `+4 + 3*i` (i in 0..count−1): one record = `{ u16 item_id (LE), u8 flag }`; records are contiguous and exactly consume the payload (4 + 3*count == payload_len for all 44, 0 leftover).
  - `item_id`: an ordinal referencing a column/group/slot. Under 0x2553 the ordered id run is a stable prefix: 50, 8, [51 only when count==11], 1,2,3,4,5,6,7, [9 only when count>=10] (observed sequences (50,8,51,1..7,9)×11, (50,8,1..7,9)×4, (50,8,1..7)×2, ()×8). Under 0x2588 the sequence is INVARIANT: ids 43,46,44,48 in that order.
  - `flag`: u8. Under 0x2553 it genuinely toggles per record (both 0 and 1 observed: 32 zeros / 147 ones; two 0x2553 instances differing ONLY in flags found → independent per-record state, likely shown/hidden). Under 0x2588 the flag is a CONSTANT 1 across all 76 records in all 19 instances — no variation, so its 'toggle' meaning is unverified for that flavor.
- **Notes:** A length-counted list of (item_id, flag) records recording, in display order, a set of column/group/item slots and a per-slot on/off byte. View/display state (recomputable UI layout), not primary session data — the leaf's block_type is always 0x0001 and it never contains children. NOTE: "PT recomputes it" is INFERRED from structure/placement, not directly proven. Record order IS meaningful (stable, not sorted). The two parent flavors reuse 0x258c for different, disjoint view lists. **HIGH** on the structural schema and the 'payload_len == 4 + 3*count, 0 leftover' invariant (all 44 / 19 sessions, hand-verified in DOPE, DWTS, Bianca, MANOLITO), and on LEAF-under-exactly-0x2553/0x2588 with disjoint id sets; **MEDIUM** on flag semantics (independent 0/1 toggle demonstrated only for 0x2553; constant 1 under 0x2588); **LOW-MEDIUM** on the precise UI meaning of item_id.
- **Confidence:** high on structure; medium on flag semantics; low-medium on item_id UI meaning.

### 0x207a — Per-view/window layout record

- **Kind:** LEAF (0 children, all 37). Parent ALWAYS 0x2031, but NOT one-per-container: a single 0x2031 holds ALL the session's 0x207a records (per-0x2031 count distribution {1:12, 2:1, 3:3, 4:2, 6:1} = 37 records; 7/19 sessions have >1). Exactly one 0x2031 per session, TOP-LEVEL (no parent), so 0x207a has no tree-siblings other than fellow 0x207a records. In top-level order the 0x2031 is consistently preceded by 0x2063 and followed by 0x204d (all 19). 0x2024/0x2056 also exist as top-level view blocks but are NOT adjacent to 0x2031.
- **Size:** span 35 bytes = header(9) + payload(26). Header: 5A, block_type u16@z+1 = 2, size u32@z+3 = 28, content_type u16@z+7 = 0x207a. (Declared size field 28 → 26-byte payload; span end = z+7+28 = z+35.) Constant across all 37.
- **Fields (payload-relative):**
  - `+0 u32 ordinal` = 1-based index of the record within its 0x2031 (== occurrence_index+1 for all 37; values 1..6). CONFIRMED invariant.
  - `+4 u32 = 1` (const, all 37 — bytes[4..7] = 01 00 00 00). CONFIRMED invariant.
  - `+8 u8 tag = 0x40 + ordinal`, i.e. 0x41..0x46 (== 0x40+ordinal for all 37). 0x40|ordinal and 0x40+ordinal are identical for ordinals 1..6, so OR-vs-ADD is indistinguishable. CONFIRMED invariant.
  - `+9 u16 view field A` (values 0, 16, or 20; byte[10] always 0). i16/u16 indistinguishable. Constant-per-session, varies by session — view state.
  - `+11 u16 view field B` (values 2, 16, or 20; byte[12] always 0). Small non-negative. Constant-per-session, varies by session — view state.
  - `+13 i32 signed view offset` (values 16, 24, or negative e.g. −1064/−1038/−1035). Equally readable as i16@+13 followed by i16@+15 that is 0 (positive) or −1 (negative, sign-extension) — i32 and paired-i16 readings numerically identical; width cannot be disambiguated. View-recomputed; varies by session.
  - `+17 i32 signed view offset` (values 512, 1024, 7936=0x1F00, or −256; byte[17] always 0). Same i32-vs-i16 ambiguity. View-recomputed; varies by session.
  - `+21 i32` = 255 (negative-offset sessions) or 0 (positive-offset sessions); byte[22..24] always 0 in positive sessions. Same width ambiguity; co-varies with sign of +13/+17. View-recomputed.
  - `+25 u8 = 0` (payload tail, const, all 37). CONFIRMED invariant.
- **Notes:** Per-view/window layout record inside the session's single top-level 0x2031 view-settings container. N = number of saved views/windows/rulers, each a fixed 26-byte record carrying small view coordinates/offsets. Display/view state PT recomputes on save — the coordinate fields (bytes 13..21) drift between saves and differ by session, so do NOT treat their specific values as stable data. **HIGH** for identity/structure (framing, LEAF, parent==0x2031, single 0x2031 per session, and the exact invariants +0 ordinal, +4==1, +8==0x40+ordinal, +25==0); **MEDIUM/LOW** for the coordinate fields +9/+11/+13/+17/+21 — offsets and values reproduce exactly but field WIDTHS (i32 vs paired i16) are undeterminable and meanings are inferred display state.
- **Confidence:** high for identity/structure; medium/low for the coordinate-field widths & semantics.

### 0x1018 — Session plug-in (insert) registry

- **Kind:** CONTAINER. Immediate children: 0x1017 ONLY (655/655; zero exceptions across 19 sessions). Parent: ROOT (top-level, all 19). block_type = 0x0001 (all 19). NOTE: the 0x1018→0x1017 relationship is exclusive, but 0x1017 is NOT exclusive to 0x1018 — 38 sibling 0x1017 blocks in the corpus live under 0x204a instead (empty/unassigned insert-slot descriptors: byte0=0xFF, zero-length name).
- **Size:** Variable. Observed payload (size field) range 90..2865 bytes; grows ~linearly with child count (2→90B, 4→176B, 10→781B, 47→1970B, 66→2732B, 69→2865B). CORRECTION: the proposal's '8-byte' minimum is NOT observed — no corpus session has an empty registry; smallest real block is 90 bytes (count=2). '8' is at best a theoretical unconfirmed empty case.
- **Fields (payload-relative):**
  - `+0 u32 count` = number of 0x1017 child entries. VERIFIED count == #immediate 0x1017 children (all 19, 0 mismatches).
  - `+4..` sequence of `count` 0x1017 child blocks, each a 0x5A-framed (header `5A <btype:2> <size:4> 17 10`) plug-in descriptor. Child block_type is 0x0004 or 0x0006 (NOT 0x0001 — that is the parent 0x1018's block_type).
- **Notes:** The flat de-duplicated list of every plug-in instantiated anywhere in the session. Top-level singleton, present exactly once in all 19 loadable non-huge sessions; child count/size scales with distinct plug-in instances. Child 0x1017 payload layout (MEDIUM, partially decoded): [byte0 = category flag, distribution {0:176, 3:411, 4:23, 8:45}] then [u32-length-prefixed ASCII plug-in name] (655/655 parsed cleanly: 'EQ3 7-Band','FabFilter Pro-Q 2','CLA-76','Click II', etc). The bytes after the name are NOT a name scramble — they are a FIXED 12-byte region = three reversed FourCC codes (AAX manufacturer + type + sub-type IDs), e.g. reversed manufacturer 'ksWV'=Waves(303×), 'Digi'=Digidesign(149×), 'FabF'=FabFilter(24×), 'NiIn'=Native Instruments, 'Antr'=Antares, 'iZtp'=iZotope. After the 12-byte FourCC region is a 7-byte flag trailer (dominant 01000100000000 / 02010201000000). Some children additionally carry a length-prefixed 'com.*' AAX bundle-ID string and/or a nested 0x5A sub-block (only 33/655), so the tail beyond the FourCC field is variable and not fully schematized.
- **Confidence:** high (container structure); medium (child 0x1017 payload).

### 0x1017 — Plug-in descriptor entry (parent 0x1018) / placeholder record (parent 0x204a)

- **Kind:** LEAF (no real children; any apparent child is a false positive from the raw 0x5A scan hitting a 0x5A='Z' byte inside the 12-byte code). Two parent contexts: parent 0x1018 (655 instances, the plug-in catalog) and parent 0x204a (38 instances, a different placeholder record). Both 0x1018 and 0x204a are top-level ROOT singletons, 1 per session.
- **Size:** Variable. Parent-0x1018 entries: block_type 0x04 (older sessions) or 0x06 (newer), payload ~21..90 B sized to name length (+ optional reverse-domain ID). Parent-0x204a entries: mostly 26/32 B, payloads dominated by 0xFF/00.
- **Fields (parent-0x1018 catalog entry, payload starts at z+9):**
  - `+0 u8` category/state flag; observed {0,3,4,8}; does NOT cleanly partition by plug-in identity (e.g. 'L2' under both 0 and 3) — treat as an opaque small flag.
  - `+1 u32 strlen1`, then strlen1 ASCII bytes = plug-in display name (e.g. 'Altiverb 7', 'FabFilter Pro-Q 2'). Decodes cleanly for all 655/655 entries in every one of 19 sessions.
  - `+(name_end), 12 bytes`: scrambled plug-in code (mfr/type/subtype-derived). NOT a pure name hash: 35 of 74 distinct names carry ≥2 distinct codes; the same plug-in name varies by instance.
  - `+(name_end+12), 7 bytes`: I/O descriptor. First u16 = input format, next u16 = output format (0x0001=mono, 0x0102=stereo); e.g. mono='01 00 01 00', stereo='02 01 02 01'. Varies per INSTANCE for one name → confirms it is I/O config, not a name field. Remaining 3 bytes are a small flag (usual '00 00 00'; also '64 00 00','03 00 00','00 00 01','02 00 00').
  - `+(name_end+19)` OPTIONAL: u32 strlen2 + reverse-domain plug-in ID (`com.<vendor>.aax…` — e.g. `com.fabfilter…`, `com.waves.aax…`, `com.Antares…`), then 2 trailing bytes ('00 00'/'01 01'/'00 01'). Present in 67 entries across 6 (block_type-0x06) sessions.
  - PARENT-0x204a entries are STRUCTURALLY DIFFERENT: byte0 usually 0xFF (36/38) with mostly-zero payloads; a couple (byte0=3) carry a name. These are placeholder/slot records, NOT plug-in descriptors.
- **Notes:** Decoded here to establish 0x1018's role; not in the originally requested set. Total 0x1017 = 693 (655 under 0x1018 + 38 under 0x204a) — 'parent: 0x1018 (always)' is FALSE. The 12-byte code is NOT a name-derived hash (same name → multiple codes; varies by instance). This aligns with docs/ptx-format-spec.md line 300-303 ('12-byte plugin id, and a 4-byte (in,out) IO') rather than a 'name-hash' framing. **HIGH** for the parent-0x1018 name-at-offset-1 layout and LEAF/parent facts (655/655 / 19 sessions); **MEDIUM** for the exact semantics of the +0 category byte and the 3-byte I/O tail (values enumerated, meaning not pinned); the parent-0x204a population is characterized but its field schema is not fully decoded.
- **Confidence:** high (parent-0x1018 layout); medium (category byte / I/O tail semantics; 0x204a schema not fully decoded).

### 0x1006 — External media / video-file table

- **Kind:** CONTAINER; children: one 0x103a name-list (ALWAYS, block_type 0x0001) + count × 0x1005 media descriptors (block_type 0x0009, only when non-empty). Parent: ROOT. 1 instance per session. Each 0x1005 in turn contains exactly one 0x1002 child.
- **Size:** Variable. Declared size field (u32@z+3) = 19 when empty (18/19 sessions, byte-identical); total block span is then 26 bytes and payload (after the 2-byte content_type) is 17 bytes. Grows with entries: MANOLITO size field = 4421 (span 4428) for 16 files.
- **Fields (payload-relative, +0 = z+9):**
  - `+0 u32 count` = number of 0x1005 media-descriptor children (0 in all 18 empty sessions, 16 in MANOLITO; matched child count exactly in all 19).
  - `+4..` one 0x103a name-list child (block_type 0x0001) — ALWAYS present. Empty form: 13 bytes `5A 01 00 06 00 00 00 3A 10 00 00 00 00` (its own inner u32 list-count = 0). When populated it holds the collection label ('Video Files') plus per-file name entries and volume names inline as length-prefixed ASCII strings — the same name-list role PT-confirmed for 0x103a under 0x1004, NOT merely an 'empty-list marker'.
  - `+...` then `count` × 0x1005 media descriptors (only when count>0). Each 0x1005 = `u32 count(=1)` + one nested 0x1002 child; internal schema not fully characterized (single populated session).
- **Notes:** The session's list of imported video/media files — the direct analog of 0x1004 (the WAV file table), with 0x1005 replacing 0x1003 and 0x1002 replacing 0x1001. Top-level ROOT singleton (1 per session, 19/19). Empty in 18/19; populated only by MANOLITO (16 files, 'Video Files', .mp4/.mov, volume 'Samsung_T3'). block_type = 0x0002 (z+1). CORRECTION: the claim that each 0x1005 'begins with a length/id header + reserved GUID space (2A 00 00 00 tag)' is UNCONFIRMED/likely wrong — NO 2A 00 00 00 GUID tag appears in any of the 16 0x1005 records (SMPTE-UL bytes `06 0a 2b 34...` appear inside the 0x1002, but not the tagged-GUID convention). The 0x1005/0x1002 field schema is observable in only one session — **treat as low-confidence.** Verified across ≥2 projects (18 empty instances span DWTS, Reverse Rewire, Bianca, COGNAC, DOPE, Let Her Go, Never Will Marry, Quality Time, THE WIND; single populated = MANOLITO).
- **Confidence:** high (table structure); low for the 0x1005/0x1002 internal schema (single populated session).

### 0x2024 — Default main-counter / nudge time-display setting

- **Kind:** TWO FORMS, both a single TOP-LEVEL (ROOT-parented) block. LEAF form: block_type=0x0005, block-size field=8 (payload=6 bytes: the 3 u16 fields), no children. CONTAINER form: block_type=0x0006, block-size field=106, exactly one child (content_type=0x0f3e at payload offset +6) carrying the string cache. Parent is ALWAYS root; exactly ONE 0x2024 per session (all 19). The proposed claim it 'also appears repeated inside other view containers' was NOT observed and is removed.
- **Size:** Bimodal by declared size field: 8 (leaf, block_type 0x0005, 10/19 sessions) or 106 (container, block_type 0x0006, 9/19 sessions). For the leaf the actual PAYLOAD is 6 bytes (3 u16); '8' is the size field (payload + the 2-byte content_type).
- **Fields (payload-relative):**
  - `+0 u16 fieldA` — timebase-mode enum; values {1,3} (3 in 14/19, 1 in 5/19). Correlates with fieldC.
  - `+2 u16 fieldB` — secondary enum; almost always 1 (18/19); one container outlier had 0.
  - `+4 u16 fieldC` — unit/format enum; values {1,8}; tracks fieldA exactly (A=3↔C=1, A=1↔C=8). Enum MEANINGS (which is 'mode' vs 'format' vs 'unit') are NOT derivable from the corpus and are labeled speculatively.
  - `+6 (CONTAINER form only)` one 0x0f3e child block (declared size field=91, payload=89 bytes) holding the 5-timebase string cache. The 3 leading u16 fields use an IDENTICAL schema in both forms.
- **Notes:** A coupled 3-field timebase-mode selector, plus (container form only) a cached rendering of one small reference time value across all 5 PT timebases. View/edit-preference state that PT recomputes; the string cache is derived, not authoritative. The 0x0f3e child schema is NOT plain length-prefixed strings — it is: u32 count(=5), then 5 records each = [1-byte index tag 0x00..0x04][u32 len][len ASCII], then 1 trailing 0x00. Head==5, tag sequence (0,1,2,3,4), childsize==91 held in ALL 9 containers; the 5 strings are byte-identical in all 9 and render one tiny reference time (Bars|Beats '    0| 0| 060', Min:Secs '  0:00.001', Timecode '00:00:00:00.01', Feet+Frames '    0+00.01', Samples '          1'). The leaf form omits this cache (PT regenerates it). **Field-value ENUM SEMANTICS are the only low-confidence part** — offsets/widths/structure are firmly confirmed; the labels 'mode/format/unit' are plausible but unconfirmable.
- **Confidence:** high (structure); low for the enum semantics of fieldA/B/C.

### 0x2501 — Timeline/edit-window horizontal zoom & ruler-scale view-state record

- **Kind:** LEAF (no children). block_type=0x0006. Parents observed: ROOT (exactly 1 top-level instance in every one of the 19 sessions) OR 0x258b (0..2 nested per session; 17 nested instances total). 0x258b (block_type 0x0001) is itself a per-view container that sits under EITHER 0x2016 or 0x2551 (not exclusively 0x2551).
- **Size:** Fixed: block_size=147, payload=145 bytes (span=[z, z+7+147]). Holds across ALL 36 instances / 19 sessions. 124 of 145 payload bytes are constant; the 21 variable bytes are the zoom/scroll/window-geometry scalars.
- **Fields (payload-relative):**
  - `+0 u8 flag = 0` (const, all 36).
  - `+1 u16 field A`: zoom/scroll param — VARIES (0,1001,1517,2425,2819,19080,26738,36147,55833).
  - `+3 u16 field B`: small enum in {0,1,4} — VARIES.
  - `+5 u32 field C`: scroll/offset param — VARIES (co-varies with A).
  - `+9 f64 horizontal scale/zoom factor` — VARIES (e.g. 4096.0, 4320.0, 270.0, 14336.0, 65536.0, 2529.384; 0.0 when unset).
  - `+17 u16 string-count = 5` (const) — NOTE: the proposal omitted this count prefix; the five strings begin at offset 19, not 17.
  - `+19 str(len-prefixed u32)` Bars|Beats: len13 '    0| 0| 240' — CONST cached ruler render.
  - `+36 str` Min:Secs: len10 '  0:00.100' — CONST.
  - `+50 str` Timecode: len14 '00:00:00:01.00' — CONST.
  - `+68 str` Feet+Frames: len11 '    0+01.00' — CONST.
  - `+83 str` Samples: len11 '        100' — CONST (strings occupy 19..97; all 5 identical across every instance).
  - `+98 u16 record-count = 5` (const), then 5× const 5-byte record '01 00 00 00 20' (per-ruler flag=1, value=space) spanning 100..124.
  - `+125 u16 tag 0x0013 (const=19)`, `+127 u16 X geometry value` — VARIES in {19,92,300}.
  - `+129 u16 tag 0x0017 (const=23)`, `+131 u16 Y geometry value` — VARIES in {23,97,320}; (X,Y) is always one of exactly 3 pairs {(300,320),(92,97),(19,23)}.
  - `+133, 8 bytes` const '00 d4 30 00 00 03 00 00'.
  - `+141, 4 bytes` trailing view-state field — VARIES; usually 0, else values like 18511/23855/25970 and 0xffffc940 (bytes 143-144 also vary, e.g. ff ff), so this is NOT a clean u16 — the proposed 'u16@141' undercounts by 2 bytes.
- **Notes:** Scroll/zoom parameters plus a cached 5-timebase text rendering of a reference position, one per view context. Purely display state PT recomputes; not authored data. The variable bytes are exactly offsets {1,2,3,5,6,9-16,127,128,131,132,141,142,143,144}. **CORRECTIONS:** (1) offset 17 is a u16 string-COUNT (=5), strings start at 19; (2) the field at 141 is a 4-byte tail, not a u16; (3) the 'nested instances share their 0x258b container value' claim is OVERSTATED — 13/19 sessions have byte-identical instances, but MANOLITO SIMONET has three DIVERGENT instances, so values are genuinely per-view-context and only coincidentally equal elsewhere; (4) 0x258b's parent is 0x2016 (9×) OR 0x2551 (8×), not always 0x2551; observed siblings 0x2011/0x2552..0x2556, NOT the claimed 0x258a. Confidence stays medium because the container structure is fully confirmed but the individual zoom scalars' exact meaning is not independently decodable.
- **Confidence:** medium.

### 0x2433 — Empty list/collection header

- **Kind:** LEAF (0 immediate children, all 32). Parents: ROOT (exactly 1 top-level instance in every session that has the type) and 0x271b (the innermost/only enclosing block for the second instance, present in 6 sessions). block_type=0x0001.
- **Size:** Fixed. size field u32 = 6 in all 32 instances (2-byte content_type + 4-byte payload). Full framed span (header 0x5A + type + size + content_type + payload) = 13 bytes. Verified across 26 sessions.
- **Fields (payload-relative):**
  - `+0 u32 count = 0` (empty collection) — const across all 32. Payload is exactly these 4 bytes; size=6 leaves no room for any member record, so the non-empty member schema is unobservable in this corpus.
- **Notes:** Structural placeholder — an empty group with no members. One instance is always top-level (ROOT); in ~1/3 of sessions a second instance is owned by container 0x271b and sits immediately before the ROOT one. block_type=0x0001 with a single u32=0 payload is PT's canonical 'empty collection' idiom. 32 instances / 26 sessions (incl. the 7 huge Hipsters), all byte-identical — no non-empty instance ever appears, so the member schema cannot be observed. The earlier raw hex showing a trailing '5a05' was header bleed from the next block (the byte after the 13-byte span is 0x5A in 32/32). The semantic meaning of 'what collection' is inferred, not observable (always empty).
- **Confidence:** high.

### 0x2437 — Empty count-prefixed collection/list header

- **Kind:** LEAF (0 immediate or nested children, all 25). Parents observed: ROOT (19 instances — one top-level copy per session, all 19 sessions that have the type) and container 0x271c (6 instances — when present, holds exactly one 0x2437 among children [0x2619, 0x2437]). block_type = 0x0001 (all 25).
- **Size:** Fixed: block_type=0x0001, declared size=6, total span=13 bytes (9 header bytes + a 4-byte payload) in all 25 / 19 sessions. 100% uniform.
- **Fields (payload-relative):**
  - `+0 u32 element_count = 0` (empty collection). Payload offset 0 = block offset z+9. Constant 0x00000000 across all 25; the collection is empty everywhere, so no member records are present to schematize.
- **Notes:** Always-empty structural placeholder. Paired with 0x2433 (identical encoding, always co-occurring, equal per-session counts) but owned by a different container: 0x271c (vs 0x2433's 0x271b). Both are same-size ROOT-level sibling containers with immediate children [0x2619, <the list>]. The 19 ROOT instances = 19 sessions (one top-level copy each); the 6 container-owned instances are the 6 sessions that additionally carry the 0x271b/0x271c container pair (those sessions have 2 copies of each). Looks like session-level view/collection state PT maintains, not per-track data. The STRUCTURE (encoding/size/parent/kind) is high-confidence, but the SEMANTIC role ('collection of X owned by 0x271c') is INFERRED because the list is empty in 100% of instances and its member schema is unobservable.
- **Confidence:** high.

### 0x2637 — Session automation/keyframe table

- **Kind:** CONTAINER-LIKE LEAF, opaque to the 0x5A framer: exactly one 0x2637 block per session, block_type(u16@z+1)=1 always, parent = final-index ROOT (top-level singleton, always positioned before the trailing 0x0002 master index). Its payload is a self-parsed table of inline records that are NOT 0x5A-framed, so the raw block enumerator sees zero children. Verified TOP-LEVEL / children=0 / bt=1 across all 26 corpus sessions (19 decoded in detail + 7 Hipsters).
- **Size:** block_type(u16@z+1)=1 always. Payload length observed 4..43264 bytes (grows with record count; 43264 = 1438 records). Present exactly once per session. Empty automation = payload is exactly 0x00000000 (record_count=0), seen in Let Her Go and Quality Time. Records are contiguous with ZERO trailing bytes (payload_len == 4 + Σ(record lengths) for every session).
- **Fields (payload-relative):**
  - `+0 u32 record_count` == number of records (VERIFIED count@0 == #records parsed, 0 failures across all 26 incl. empty rc=0).
  - `+4..` record[i]: variable-length inline record; exactly record_count of them, packed contiguously, no padding/trailing bytes (VERIFIED).
  - `record+0, 4` const 0x00014601 little-endian (record start marker; VERIFIED on all 4729 records, 0 mismatches).
  - `record+4 u32 rec_size` == record_length − 8 (VERIFIED all records).
  - `record+8 u16 == 0` (VERIFIED all records).
  - `record+10 u32 n` = number of breakpoints in this record (values 1,2,3,5; n=1 is 4683/4729 records).
  - `record+14 u32 descriptor`: low u16 == 4 (bytes-per-value = f32), high u16 == n−1 (VERIFIED both halves all records).
  - `record+18 u32 record-level anchor/position field`. HEDGED: for the 4682/4683 single-breakpoint records this holds a session-scale, increasing sample-position-like value while the breakpoint's own pos@22 is 0; for multi-breakpoint records it is often 0 (real timeline in the breakpoint positions) but sometimes non-zero and NOT equal to the first breakpoint position. Equals the first breakpoint's sample_position in only ~42% of records. Position/anchor-like but its exact meaning vs. the breakpoint positions is NOT fully resolved — do not rely on a single clean rule.
  - `record+22, 8*n` breakpoint[m] = `{u32 sample_position, f32 value}`. reclen == 22 + 8*n (VERIFIED all records). For multi-breakpoint records the sample_positions are monotonically non-decreasing EXCEPT a 0xFFFFFFFF (−1) sentinel may appear as the first position (3 records total, all in the DWTS trio; after dropping the sentinel, positions are strictly monotonic). value is an IEEE-754 f32, corpus range −32.9..+23.1, dB/gain-like.
- **Notes:** Real persisted automation data (a count-prefixed list of automation records, each holding breakpoints whose f32 values decode as dB-like gain/fader magnitudes, dominated by discrete half-dB steps), NOT a view-state recompute. Empty (0 records) in sessions with no automation. Because records are self-describing and not 0x5A-framed, a builder/editor must parse the inline table itself (walk by the 0x00014601 marker + rec_size). Record-length distribution: 30 B for n=1 (4683), 38 for n=2 (39), 46 for n=3 (5), 62 for n=5 (2). **HIGH for structure** (all structural invariants hold with zero exceptions across 26 sessions / 4729 records); **MEDIUM for exact time-position semantics** — specifically the record+18 'anchor' meaning (only ~42% match the first breakpoint) and the raw monotonicity claim (holds only after treating −1 as a sentinel), both over-precise in the proposal. The value column is solid; the exact position column is probable-but-hedged.
- **Confidence:** high (structure); medium (exact time-position semantics of record+18 and monotonicity).

### 0x2632 — Reserved/placeholder top-level singleton (always-zero u32)

- **Kind:** LEAF; no children (nchild=0 all instances); parent = final-index ROOT (top-level singleton — 0 enclosing blocks). Structurally always sits immediately before a 0x262a block; the block before it (start-order) is 0x262c or 0x2526.
- **Size:** block_type (u16@z+1) = 1 always; declared size field (u32@z+3) = 6 always; span = [z, z+13] = 13 bytes total; payload = size−2 = exactly 4 bytes always. Uniform across all instances (e == z+7+size in every case, so the 4-byte payload width is trustworthy).
- **Fields (payload-relative):**
  - `+0 u32 = 0x00000000` (payload bytes `00 00 00 00` in EVERY session; a reserved/always-zero dword — likely a count or flag that is 0 whenever the feature is unused. Cannot be distinguished from a generic reserved word given zero variability).
- **Notes:** A top-level session singleton carrying no data — a reserved/placeholder slot always present but byte-identical (payload all-zero) in every corpus session. Best read as a header/anchor for a feature that is unused or left at default in all sessions examined. **HIGH for structure/framing** (singleton; bt=1; size=6; 4-byte all-zero payload; LEAF; top-level; byte-identical — VERIFIED across all 26 corpus sessions incl. the 7 Hipsters, exactly 1 instance each); **LOW/UNCONFIRMABLE for field SEMANTICS** — because the payload is all-zero in 100% of instances, the precise meaning of the u32 cannot be determined; it is honestly just 'always-zero'. Do not over-claim a per-field schema.
- **Confidence:** high for structure/framing; low/unconfirmable for the u32 semantics.

### 0x2032 — Single-slot wrapper/selector (session config/display-mode)

- **Kind:** CONTAINER; block_type(u16@z+1)=2. Exactly 1 immediate child, content-type 0x2042 (10/19 sessions) or 0x270a (9/19) — mutually exclusive, never both, never zero. Parent = final-index ROOT (top-level singleton, no enclosing block, all 19). The child's OWN block_type is a size-class, not an identity: 0x2042 child has block_type=7; 0x270a child has block_type=8 when its payload is 31 B and 9 when 33 B.
- **Size:** 0x2032 payload 19 bytes when child is 0x2042 (empty form); 40 bytes (31-B child) or 42 bytes (33-B child) when child is 0x270a. (Whole-block span = payload+9.)
- **Fields (payload-relative):**
  - `+0..` embedded 0x5A child block: `5A <block_type:2> <size:4 = child_payload_len+2> <ct:2 = 0x2042 or 0x270a> <child payload>`. 0x2032 has no other fields.
  - CHILD 0x2042 (empty/default, payload always 10 bytes; byte-identical in all 10 = `01000000 01000000 0000`): +0 u32=1, +4 u32=1, +8 u16=0. CONFIRMED.
  - CHILD 0x270a (populated, payload 31 or 33 bytes): +0 u32=0; +4 u8 count ∈ {1,3}; +5 u8=1; +6..13 all zero; `+14 u8 ALWAYS 0` (the proposal's '+14 = value' is an OFF-BY-ONE ERROR); `+15 u8 value` = 0x06 when count=1, 0x0c when count=3 (equivalently u16@+14 = value); +16..29 all zero; +30 u8 tag = 0x02 when count=1, 0x01 when count=3. Optional 2-byte tail only in the 33-byte form: +31 = value byte (0x06 or 0x0c, repeats +15), +32 = 0x00. The 31-byte form has no tail after +30.
- **Notes:** A single-slot wrapper carrying no fields of its own beyond the embedded child — a small top-level session-config/display-mode record. Form selection is NOT session-size/automation-count driven: Bianca (26684 blocks, 9 automation records) uses the EMPTY 0x2042 form while the far smaller Never Will Marry (4309 blocks) uses 0x270a — confirming it is a display/config mode PT chooses (likely recomputed), not a structural count. count/value/tag pairing: count=1→value=6/tag=0x02, count=3→value=12/tag=0x01 (only these two combinations; no arithmetic law can be pinned from 2 data points). **HIGH** on all structure (wrapper=1 child, mutual exclusivity, always ROOT, 0x2042/0x270a occur ONLY under 0x2032; 0 exceptions across 19 sessions), on the 0x2042 byte layout (10/10 byte-identical), and on the corrected 0x270a offsets; **LOWER** only on the SEMANTIC meaning of the 0x270a count/value/tag triple.
- **Confidence:** high (structure & offsets); lower for the 0x270a count/value/tag semantics.

### 0x2031 — View/layout enumeration container

- **Kind:** CONTAINER; block_type (u16@z+1) = 3 (verified 20/20 sessions). Immediate children are 1..6 blocks of content_type 0x207a (each block_type=2, declared size=28); the payload's leading u32 count equals the number of 0x207a children exactly (all sessions). PARENT: none — 0x2031 is a top-level (depth-0) singleton in the raw 0x5A block tree, not enclosed by any 0x5A block. The final-index (master TOC) references it directly. In the top-level sequence it sits between content_type 0x2063 (previous) and 0x204d (next).
- **Size:** Payload = 4 + 35*n bytes, n = child count (each 0x207a child occupies a 35-byte span: 7-byte 0x5A header wrapper + 28-byte body). Observed: 39 (n=1), 74 (n=2), 109 (n=3), 144 (n=4), 214 (n=6). n=1 in most sessions; up to 6 (Bianca). Formula exact across all sessions. (Declared size field u32@z+3 = paylen+2, e.g. 41 for a 39-byte payload.)
- **Fields (payload-relative):**
  - `+0 u32 child_count` == number of embedded 0x207a children (all 20 sessions).
  - `+4..` child_count embedded 0x5A blocks of content_type 0x207a (block_type=2, size=28, 26-byte payload). Field offsets below are relative to each 0x207a PAYLOAD start (child z + 9):
    - `child+0 u32 ordinal`, 1-based, strictly sequential 1,2,3,... (all sessions).
    - `child+4 u32 = 1` constant (a sub-count; always 1 in every child of every session).
    - `child+8 u8 letter tag = 0x41 + (ordinal−1)` i.e. 'A','B','C',... strictly sequential (all sessions).
    - `child+9 u16 dim_w` — display geometry (16 in multi-child sessions; 20 or 0 in single-child). Meaning inferred from value ranges.
    - `child+11 u16 dim_h` — display geometry (16 in multi-child; 20 or 2 in single-child).
    - `child+13 i32 offset/position` — signed. POSITIVE small (+16 or +24) in multi-child; large NEGATIVE (~ −1035..−1064) in single-child. Interpreted as a scroll/geometry pixel-like offset; PT recomputes.
    - `child+17 u8 = 0` constant (all sessions).
    - `child+18 u16 flag` — per-session constant (same across all children within a session), varies between sessions: 0x001f, 0x0002, or 0x0004 in the positive-offset variant; 0xffff in the negative-offset variant.
    - `child+20, 6 bytes trailing` — '00 00 00 00 00 00' (positive-offset variant); 'ff ff 00 00 00 00' (negative-offset variant). NOTE: in the negative-offset variant bytes child+18..child+21 read 'ff ff ff ff', so the child+18 flag and the first 2 trailing bytes may together be a single wider sentinel — treat this boundary as tentative.
- **Notes:** Holds a count-prefixed list of enumerated view items, each embedded as a 0x207a block tagged with a sequential ASCII letter ('A','B','C',...) and carrying small dimension/offset geometry (16×16 or 20×20 sizes, a signed pixel-like offset, and a small per-session flag). Characteristic of edit-window view sub-lanes / ruler-row layout state; a display-state structure PT recomputes, not load-bearing session data. Two clear variants: single-child sessions use a negative-offset descriptor with a 0xffff sentinel and dim 20×20 or 0×2; multi-child sessions enumerate 16×16 items with positive +16/+24 offsets and a per-session flag (0x1f/0x02/0x04). **HIGH** on container structure & enumeration invariants (block_type=3; child_count==#0x207a; sequential ordinals/letters; subc==1; byte17==0; child block_type=2/size=28; size == 4+35*n; top-level singleton with no enclosing block — verified across 20 sessions incl. the 125k-block Hipsters); **MEDIUM** on the inner geometry field meanings (interpreted from value ranges into two variants); the exact child+18/child+20 boundary is tentative in the 0xffff variant.
- **Confidence:** high (container structure/enumeration); medium (inner geometry-field meanings; child+18/+20 boundary).

### 0x2025 — Session view/window-state singleton (display/UI extent)

- **Kind:** LEAF; no children (nchild=0 in all 26 instances). Parent = final-index ROOT — top-level singleton (verified TOP-LEVEL / no containing parent block in every one of the 26 sessions).
- **Size:** Exactly ONE instance per session (all 26). Two forms selected by block_type (u16@z+1): bt=1 → 18-byte payload (block size u32=20); bt=4 → 31-byte payload (block size u32=33). Corpus split: bt=1 = 17 instances (7 Hipsters + 7 Reverse Rewire + Bianca + Let Her Go + Quality Time); bt=4 = 9 instances (DWTS ×4, COGNAC, DOPE, MANOLITO, Never Will Marry, THE WIND). The proposal's counts were wrong (bt=4 is 9, not '11+'; bt=1 is 10 non-Hipsters / 17 total, not 4).
- **Fields (payload-relative, +0 = z+9; nearly all bytes are 0 in nearly all sessions):**
  - bt=4 form (31 bytes) VERIFIED TAIL (all 9 bt=4 instances): `+19 u16 = 0x2ee0 (12000)` — fixed extent/width default; `+21, 6 bytes = 0`; `+27 u8 = 0xfa (250)` — fixed extent/height default; `+28, 3 bytes = 0`. This 'e0 2e 00 00 00 00 00 00 fa 00 00 00' literal tail is a hardcoded default in every bt=4 instance.
  - bt=4 form: `+0 u8 flag ∈ {0,1}` (enabled/visible toggle; =1 in DWTS ×4 + COGNAC, =0 in DOPE/MANOLITO/NWM/THE WIND) — INFERRED meaning.
  - bt=4 interior variance (offsets 1..18): only two sessions deviate from all-zero. COGNAC: u8=1 at offset 9 (CORRECTION: the proposed '+14' is wrong; offset 14 is always 0). Never Will Marry: `u32@offset 10 = 0x553ad1 = 5585617` (a scroll/position value) — CONFIRMED.
  - bt=1 form (18 bytes): offset 0 is ALWAYS 0 across all 17 bt=1 instances (NO +0 flag in this form — the proposed universal '+0 flag' is bt=4-only). Only Reverse Rewire deviates from all-zero: field @1 = 54503 (`e7 d4 00 00`; reads identically as u16 or u32), enum/flag @9 = 1, field @11 = 2250 (`ca 08 00 00`). All other bt=1 sessions (Hipsters, Bianca, Let Her Go, Quality Time) are entirely zero. bt=1 has NO 0x2ee0/0xfa default suffix — that exists only in bt=4.
- **Notes:** A small top-level session view/window-state singleton (display/UI-extent structure PT recomputes). Payload is almost entirely zeros with a stable hardcoded default suffix in the bt=4 form (12000 and 250 = fixed extent). A handful of interior bytes are non-zero in a minority of sessions and encode per-session scroll/position/flag state. **MEDIUM** — singleton status, top-level/no-parent, leaf, the two block_type forms and their exact sizes, and the bt=4 hardcoded 12000/250 tail are all proven; the interior is sparse and mostly-zero, so individual interior field MEANINGS (view extent / scroll / flag) are INFERRED, not proven. Do not over-trust the zero interior as a fixed schema.
- **Confidence:** medium.

### 0x2026 — Session view/display-preferences singleton

- **Kind:** LEAF; no children (nkids=0 all 26). Parent = final-index ROOT: top-level block in the contiguous top-level chain (is_top_level=True; neighbors 0x1028 before, 0x2032 after). block_type(u16@z+1)=4 in ALL 26. Header: `5A 04 00 <size:u32> <26 20>`, payload starts at z+9.
- **Size:** 20-byte payload / block_size=22 (base form, 23 of 26 sessions incl. all 7 Hipsters) OR 28-byte payload / block_size=30 (extended form: exactly COGNAC, DOPE, Never Will Marry). Selection is deterministic: payload byte@15==1 ⇔ extended (28B) ⇔ byte@16==1 (all equivalences hold across every instance).
- **Fields (payload-relative):**
  - `+0 u8 boolean flag` (0x00 in 20 instances, 0x01 in 6).
  - `+1 u16 = 0x0006` (const tag/version, all 26; byte@1==0x06, byte@2==0x00).
  - `+3, 4 bytes always 0x00` (offsets 3,4,5,6).
  - `+7 u8 flag = 0x04 (19 instances) or 0x00 (7)`; a separate byte, NOT part of value@8. Does not map to any structural count.
  - `+8 u32 per-session value` (only byte@8 varies; bytes 9-11 always 0, so value<256; observed 11,14,40,89,97,167,171). NOT a track/region/wav/midi count — verified independent of all four.
  - `+12 u8 = 0x02` (const tag, all 26).
  - `+13 u8 high-bit mode/bitfield`, values {0x00, 0x80, 0xc0} (0x80 and 0x40 bits set/clear together in corpus).
  - `+14 u8 mode bit`, values {0x00, 0x01}.
  - `+15 u8 EXTENDED-FORM GATE`: ==0x01 → 28-byte extended form (also implies byte@16==0x01); ==0x00 → 20-byte base form.
  - `+16 u32 = 0` (base form only; offsets 16-19 all zero in every 20-byte instance).
  - `+16, 12 bytes fixed tail` (extended form only): u32=1, u32=1, u32=18 (0x12) — byte-identical in all 3 extended sessions.
- **Notes:** Top-level session view/display-preferences singleton, co-occurring with a single 0x2025 singleton (both once per session, all 26; 0x2025 sits 6-7 top-level positions after 0x2026, so 'paired' means co-occurring, NOT adjacent). value@8 disproof of a count: sessions with 2 vs 121 tracks BOTH have value=97; sessions with 24907 vs 2506 tracks BOTH have value=11; COGNAC(336)/DOPE(1120)/Never Will(404) tracks all have value=167 (checked against len(tracks)/regions/audiofiles/miditracks — no correlation). value=167 coinciding with the 3 extended-form sessions is a 3-sample coincidence, not a proven rule. **HIGH** on framing/structure (block_type=4, LEAF, top-level singleton, 0x2025 co-occurrence, const 0x0006@1, const 0x02@12, base-vs-extended split, byte@15==1 → extended-with-fixed-{1,1,18}-tail — all across ALL 26 instances) and that value@8 is NOT a track/region/wav/midi count (explicitly disproven); **LOW** on the precise SEMANTIC of value@8, byte@7, and the byte@13-15 mode bytes (inferred view/display preferences).
- **Confidence:** high (framing/structure); low for the semantics of value@8, byte@7, byte@13-15.

### 0x2033 — Session I/O Setup / hardware routing map

- **Kind:** LEAF (0 well-framed 0x5A sub-blocks inside the payload across all instances; internally a flat serialized path table of length-prefixed ASCII strings + 8-byte GUID refs). Parent: ROOT (top-level singleton, enclosed by no other block in all 26 files). Block order: IMMEDIATELY BEFORE 0x2000 (session data container) and immediately AFTER 0x1022, within the stable ROOT tail run ...0x1018, 0x2603, 0x1022, [0x2033], 0x2000, 0x2634, 0x1058 (identical across all 26). CORRECTION: the proposal's '0x1021/0x2638' predecessors are WRONG — neither content-type exists in ANY corpus session.
- **Size:** VARIABLE and session-specific: payload 502..3124 bytes (size field 504..3126) across the corpus. Grows with number of I/O paths and name lengths. 10 distinct payloads over the 19 non-Hipsters sessions (11 distinct over all 26; files sharing a session lineage share bytes). Exactly ONE instance per session (26/26 files).
- **Fields (payload-relative, +0 = zmark+9):**
  - `+0 u16 = 0xffff` in ALL 26 sessions (INVARIANT reserved/none marker). CORRECTION: the proposal's claim that +0 also takes 0x0004/0x0070/0x0054 is WRONG — those live at +2.
  - `+2 u16 optional index/count`, 0xffff = unset. Observed unset (0xffff) in 12 sessions and set to 0x0004, 0x0054(=84), 0x0070(=112) in others. Roughly tracks interface size but is NOT equal to +34 and NOT equal to the path-string count; exact meaning UNCONFIRMED — best described as an optional path-index/reference (0xffff when absent).
  - `+4 u32 = 0x0000000b (11)` — INVARIANT (format/version constant).
  - `+8, 22 bytes all 0xff` — reserved/null placeholder region (INVARIANT: all-0xff, all 26).
  - `+30 u32 = 0x0000000b (11)` again — INVARIANT repeated section marker.
  - `+34 u16 interface/hardware channel count (input side)`. Values 1,3,16,40,60,72. CORRECTION: NOT a 'path/slot count' of session I/O paths — does not match the number of path strings, tracks, or audio files (e.g. +34=1 with 132 path strings in THE WIND; +34=60 for both a 2-track and a 121-track session; +34=40 for both a 264-track and 2849-track DWTS). Reflects the physical I/O template/interface config of the saving machine, not per-session content.
  - `+36 u16` = ALWAYS EQUAL to +34 in all 26 sessions (output-side channel count == input-side; the interface is symmetric).
  - `+38..` path table: length-prefixed ASCII path names (u32 len + bytes), interleaved with 0x2A000000-tagged 8-byte GUID refs and 0x00/0xff filler; a repeated 8-byte GUID (same GUID appearing 3× in Reverse Rewire) = multiple paths referencing one I/O object. The 0x2A000000 GUID tag convention confirmed.
- **Notes:** The serialized list of input/output/bus signal paths, physical port labels, their surround formats, and GUID cross-references — the embedded equivalent of a .pio file. Payload literally contains 'Built-in Output 1-2', 'Output 1-2', 'Out (Multiple destinations) 1-2'..'11-12', 'ADAT 5-6/7-8', 'Input11'..'Input128', channel-pair labels '1-2'..'79-80', and format tags '7.1.2'. block_type in the 9-byte frame header (u16@zmark+1) takes 14, 15, OR 22 (distribution: 14=17 files, 22=6, 15=3; the proposal omitted 15, common in COGNAC/DOPE/Never Will Marry). Because much of this block encodes the physical I/O hardware of the saving machine (not session content), a synthesizer should treat +38.. as an opaque template payload. **HIGH** on role, kind (LEAF/ROOT/singleton), position (immediately before 0x2000), size range, and header invariants +0=0xffff/+4=11/22×0xff@+8/+30=11/+34==+36 (verified across the FULL 26-file corpus, 4+ sessions byte-by-byte); **MEDIUM** on the semantics of +2 (optional index, unconfirmed) and +34/+36 (an interface channel count, NOT a session-path count — the proposal's 'path/slot count' label is downgraded); **MEDIUM** on the path-record grammar past +38 (string+GUID interleave decoded, GUID-repeat semantics confirmed, but the full per-record schema is not exhaustively formalized).
- **Confidence:** high (role/kind/size/header invariants); medium (+2 semantics, +34/+36 label, and the +38.. path-record grammar).

### 0x103e — Fixed session default-preferences / default-view-parameters blob

- **Kind:** LEAF (0 children, all 19). Parent: 0x1040 (block_type=4 preferences-group container). Within 0x1040 it is the 3rd child, in the fixed child sequence 103d,103d,103e,103f,1041,104a,104b,104c,1041 (identical in all 19). Own block_type = 3.
- **Size:** FIXED: size field = 214, payload = 212 bytes (payload = size − 2 for the content_type u16). Exactly ONE instance per session (all 19 non-huge). Payload byte-for-byte identical across all 19 (single SHA-256).
- **Fields (payload-relative):**
  - `+0 u32 = 4` (const, all 19).
  - `+4 u32 = 1` (const, all 19).
  - `+8 u32 = 1` (const, all 19).
  - `+12..+203` heterogeneous fixed body. Contains many IEEE-754 f64 = 2.0 (0x4000000000000000) at 8-byte-aligned offsets 16,24,32,40,48,80,88,112,176,192 (plus near-2.0 values at 8,72,96,104,168,184), interleaved with small u32 ints at fixed offsets: 100@+56, 50@+64, 50@+120, 200@+124, 10@+148, 399@+156, 50@+164. Does NOT decompose into a clean count-prefixed or fixed-stride (u32,f64) array (a 12-byte-pair stride from +12 yields denormal garbage), because the 12-byte header leaves the f64=2.0 values 8-aligned but not at a uniform record stride. Treat as an opaque fixed blob.
  - `+204 u32 = 6` (const tail marker, all 19; second-to-last u32).
  - `+208 u32 = 9` (const tail marker, all 19; final u32, ending exactly at byte 212).
- **Notes:** PT writes this BYTE-IDENTICAL in every corpus session (a hardcoded defaults blob, not user/session data) — a writer can transplant it verbatim. The recurring f64=2.0 values suggest default 2× zoom/scale factors, but the precise per-field meaning is not established. **HIGH that it is a fixed defaults table** (byte-identical across ALL 19 sessions; head [4,1,1]@+0/+4/+8 and tail [6,9]@+204/+208 verified on all 19; size/placement/LEAF/parent/sibling-sequence all held in every session); **DELIBERATELY LOW / UNCLAIMED on a precise per-field schema** of the +12..+204 body — it is a heterogeneous fixed blob, not a clean array, so individual field meanings are NOT specified; the 'default zoom/scale' reading of the f64=2.0 values is a plausible guess, not established (values never vary, so nothing correlates them to session parameters).
- **Confidence:** high that it is a fixed defaults table; low/unclaimed on the interior per-field schema.

### 0x103f — Fixed session default-values table

- **Kind:** LEAF (0 children). Own block_type=1. Parent: 0x1040 (parent block_type=4, a top-level ROOT-child preferences group). 0x103f is the 4th child (index 3) in the fixed child sequence (0x103d,0x103d,0x103e,0x103f,0x1041,0x104a,0x104b,0x104c,0x1041), identical across all 19.
- **Size:** FIXED: size field = 58, payload = 56 bytes. Exactly one per session; byte-identical across all 19 non-Hipsters sessions (single distinct payload, count 19), re-confirmed by name on DWTS and DOPE.
- **Fields (payload-relative):**
  - Payload = 14 consecutive u32 LE = [1, 1, 64, 1, 1, 200, 1, 127, 1, 400, 0, 1, 127, 0]. Every stated per-offset value verified 19/19.
  - `+0 u32=1, +4 u32=1, +8 u32=64, +12 u32=1, +16 u32=1, +20 u32=200, +24 u32=1, +28 u32=127, +32 u32=1, +36 u32=400, +40 u32=0, +44 u32=1, +48 u32=127, +52 u32=0`.
  - Values are clearly default parameter/limit constants (64=0x40, 200=0xc8, 127=0x7f MIDI-max, 400=0x190). SEMANTICS UNRESOLVED: the exact PT preference each u32 maps to is unknown, and the '(flag/count, value) pairs' reading is speculative — the values do not split into a clean flag/value pairing, so treat only the raw 14-u32 layout as established.
- **Notes:** A short constant list of default numeric preferences (companion to 0x103e) that PT writes byte-identically every session → transplantable verbatim; not a computed/per-session field. **HIGH** on the exact byte layout, size, occurrence (1/session), parent (0x1040), and 4th-child position — all verified byte-identical over 19 sessions across ≥2 named sessions (DWTS, DOPE) with a brute-force-checked parent index; **LOW/UNRESOLVED** on the semantic label of each value (the pairing interpretation from the original proposal is not supported and was removed).
- **Confidence:** high (layout/size/placement); low/unresolved for per-value semantics.

### 0x2047 — Top-level session boolean-settings leaf

- **Kind:** LEAF (0 children, all 19). Parent: ROOT — top-level, NOT enclosed by any 0x5A block (0 enclosing blocks in all 19); a direct child of the implicit session root. block_type u16@z+1 = 0x0002 (all 19). In ROOT/top-level start order the sequence is invariant: 0x1040 group → 0x104d → 0x207e (size 5) → 0x2047 → 0x2049 → 0x206e → 0x2073 (track region). The block immediately before is 0x207e; immediately after is 0x2049.
- **Size:** FIXED. size field (u32@z+3) = 10, payload = 8 bytes, full span = 17 bytes ([z, z+9+8)). Exactly one instance per session (19/19).
- **Fields (payload-relative, +0 = z+9):**
  - `+0 u8 = 0x00` (const, 19/19).
  - `+1 u8 = boolean session flag`: 0x01 in 16/19, 0x00 in 3/19 (Bianca Long Road, Let Her Go_Stanaj, Quality Time). The ONLY byte that ever varies. Does NOT correlate with session size/track count (Bianca=0 has 26684 blocks; Quality Time=0 has 1618; COGNAC/Never Will Marry =1 are small) — a genuine independent per-session on/off setting.
  - `+2..+6 u8 = 0x00` (const, 19/19; reserved).
  - `+7 u8 = 0x01` (const, 19/19; a fixed terminator/enable byte).
- **Notes:** A small fixed-size top-level block holding one session-wide on/off flag (plus constant framing), sitting in the global-settings cluster between the 0x1040 preferences group and the track region. **HIGH** on layout, size, kind, parent, ROOT ordering, and on +1 being the only varying byte (all byte-exact across 19 non-Hipsters sessions, many distinct sessions); **LOW-TO-MEDIUM** on the UI/semantic MEANING of the +1 flag — demonstrably a per-session boolean that varies independently of session structure, but WHICH view/preference it gates is NOT pinned from the binary alone. Treating it as a display/behavior toggle PT owns is a reasonable inference, not a confirmed schema.
- **Confidence:** high (layout/structure/ordering; +1 as sole varying byte); low-to-medium for the +1 flag's semantic meaning.

### 0x2049 — Top-level session numeric-settings leaf

- **Kind:** LEAF (block_type u16@z+1 = 0x0002; no children in any of 19 instances). TOP-LEVEL, not nested: span-containment enumeration finds NO containing block in all 19 — there is no explicit ROOT wrapper block that holds it. In raw stream order invariably preceded by 0x2047 and followed by 0x206e (19/19), so it sits in the global-settings cluster before the track/media region.
- **Size:** FIXED: size field = 22, payload = 20 bytes (span = [z, z+7+22] = z..z+29). Exactly ONE per session. Byte-identical across all 19 corpus sessions (Hipsters excluded — did not load).
- **Fields (payload-relative):**
  - `+0 f64 = -48.0` (LE bytes `00 00 00 00 00 00 48 c0`; IEEE-754 u64 = 0xC048000000000000) — INVARIANT — inferred dB display/metering floor.
  - `+8 u32 = 500` (0x000001f4) — INVARIANT — inferred range/scale constant (units unknown).
  - `+12, 8 bytes = 00 00 00 00 00 00 00 00` (reserved/zero) — INVARIANT.
- **Notes:** Holds an invariant f64 = −48.0 (almost certainly a dB display/metering floor) plus an invariant u32 = 500 (a range/scale constant). Written byte-identically in every session; carries no per-session information, so it is transplantable verbatim. CORRECTIONS to the proposed entry: (1) it is a genuine top-level block with NO containing block — there is no ROOT container wrapping it; (2) the proposed f64 hex '0x4000000000000c000' was a malformed typo — the correct IEEE-754 u64 is 0xC048000000000000. **HIGH** on layout, size, singleton-ness, ordering (prev=0x2047, next=0x206e), and byte-level constancy (19/19, distinct-payloads count = 1, ≥2 distinct session families); **LOW-TO-MEDIUM** on semantics — −48.0-as-f64 is a clean tell for a double and −48 dB a plausible metering floor, but because the value NEVER varies there is nothing to correlate it against, so the exact PT setting name and the meaning/units of the 500 are inferred, not proven.
- **Confidence:** high (layout/constancy/ordering); low-to-medium for semantics.

### 0x104a — Fixed 4-byte constant leaf (session-preferences group)

- **Kind:** LEAF (block_type=1; no children in all 19 enumerated sessions). Parent: 0x1040 (block_type=4 preferences container, a top-level block). In the 0x1040 fixed immediate-child run, 0x104a sits at position 6 of 9: right after a 0x1041 and right before 0x104b. NOTE (corrected): the preceding 0x1041 is NOT a 6-byte leaf — it is block_type=2, size=97 (size=862 in one session), carrying the '<groove saved with session>' groove payload. Only the following 0x104b is an identically-shaped 6-byte leaf.
- **Size:** FIXED: size field = 6, payload = 4 bytes (payload_len == size−2). Exactly one per session, byte-identical across all 26 sessions (19 via full block enumeration + 7 Hipsters via raw signature `5A 01 00 06 00 00 00 4A 10 01 01 00 00`).
- **Fields (payload-relative):**
  - Payload = `01 01 00 00` (constant in all 26 sessions).
  - `+0 u8 = 1`; `+1 u8 = 1`; `+2 u16 (LE) = 0` (equivalently +0 u16 LE = 0x0101, +2 u16 LE = 0).
- **Notes:** A fixed 4-byte constant leaf inside the session-preferences group (0x1040); PT writes `01 01 00 00` identically in every session (all 26 corpus files, incl. the 7 Hipsters) → transplantable verbatim. Likely a small enable/flag+version pair, but because it is invariant this is unfalsifiable from static data; whether PT recomputes it as view/display state cannot be determined without a live-PT round-trip. Its sibling 0x104b is an identically-shaped 6-byte leaf with the identical 01 01 00 00 payload. **HIGH** on layout and constancy (size=6, block_type=1, LEAF, parent 0x1040 bt=4, sibling order 103d,103d,103e,103f,1041,104a,104b,104c,1041, payload 01 01 00 00 — all across 19 enumerated non-Hipsters sessions + payload signature in all 7 Hipsters, 26/26); **LOW** on distinguishing 'version' vs 'enable-flag' vs 'reserved' semantics (a 4-byte invariant; byte labels not falsifiable from static corpus data).
- **Confidence:** high (layout/constancy); low for byte-label semantics.

### Additional types — tiers 61–90 by frequency

### 0x2096 — Empty named-collection/list header (top-level)

- **kind:** CONTAINER, top-level (parent = None; NOT enclosed in any 0x5A block). block_type (u16@z+1) = 1 in all 19 instances. Exactly one immediate child, content_type 0x103a (block_type=1, size field=6, an empty list with its own +0 u32 count=0). Resembles the empty form of neighboring 0x1006 but is NOT byte-identical: 0x2096 has block_type=1 vs empty 0x1006's block_type=2, and 0x1006 can be populated (e.g. MANOLITO's 4421-byte 'Video Files' collection) whereas 0x2096 is empty in every corpus session.
- **size:** Total block = 26 bytes = 9-byte header (5A + block_type u16 + size u32 + content_type u16) + 17-byte payload. Size field (u32@z+3) = 19 (counts content_type(2)+payload(17) per the 0x5A frame; span = [z, z+7+size] = 26). Constant across all 19 instances.
- **fields:**
  - `+0 u32 entry_count = 0` — list empty in all 19 sessions; per-entry record schema UNKNOWABLE from this corpus (no session populates it).
  - `+4 (13 bytes)` — one inline child block `5A 01 00 06 00 00 00 3A 10 00 00 00 00` = a 0x103a block (block_type=1, size field=6, content_type=0x103a). The child's actual payload is `00 00 00 00` (its own u32 count=0); the bytes `3A 10` are the child's content_type word at child+7, not payload.
  - Whole-block payload hex, byte-identical in all 19: `000000005a0100060000003a1000000000`.
- **notes:** Top-level predecessor is 0x1004 (19/19), NOT 0x2106 (0x2106 never appears at top level in any corpus session — 55-1215 nested instances/session, 0 top-level). Byte-constant successor tail: 0x2096 → 0x1006 → 0x2027 → 0x2025 → 0x2058 → 0x1040 → 0x104d → 0x207e → 0x2047 → 0x2049 → 0x206e.
- **confidence:** HIGH for structure/constancy — 19/19 byte-identical across 10 distinct base projects (well above the ≥2-session bar). LOW for per-entry schema — entry_count is always 0, so the record layout of a populated 0x2096 cannot be inferred.

### 0x1040 — Groove-template / quantize-grid group container (top-level)

- **kind:** CONTAINER (block_type=0x0004), TOP-LEVEL (no enclosing 0x5A). Immediate children, in this exact order, identical in all 19 sessions: 0x103d, 0x103d, 0x103e, 0x103f, 0x1041(→0x1042), 0x104a, 0x104b, 0x104c, 0x1041(→0x1042). Each 0x1041 child carries exactly one 0x1042 grandchild. Parent of the 0x104b and 0x104c leaves (each once, parent==this 0x1040 in all 19). The two 0x1041 children carry groove-template names ('<groove saved ...>', 'MPC 65% 16th Swing') — this is the session Groove Template group.
- **size:** NOT a fixed-size record. 710-byte block (span 717) in 18/19 sessions including both fully-decoded ones (DOPE, DWTS), but 1475 (span 1482) in Bianca Long Road because its first 0x1041 groove-template child is larger. Size tracks the variable 0x1041/0x1042 groove-name payloads.
- **fields:**
  - `+0` (payload start = zmark+9): begins DIRECTLY with the first framed child (0x103d), frame header `5A 02 00 3B 00 00 00 3D 10 ...`. There are NO container-owned header bytes.
  - PURE container: payload[+0]==0x5A, 9 children perfectly contiguous (no gaps), last child ends exactly at block end (19/19). The container defines ZERO scalar fields of its own.
  - Any numeric contents (position/scale-like values) belong to the 0x103d/0x103e/0x103f leaves, not to 0x1040.
- **notes:** Previous top-level sibling is always 0x2058, immediately abutting (0x2058.end == 0x1040.zmark) 19/19. Exactly ONE 0x1040 per session. REFUTED from proposal: the 'u32 count=5 / f64 pair' container-owned fields do NOT belong to 0x1040 — those bytes are the first 0x103d child's payload, and the value is not stable (5 in DOPE, 1029/`05 04 00 00` in DWTS). Size is NOT constant 710 (Bianca is 1475).
- **confidence:** HIGH for parentage, top-level placement, 0x2058-abuts-before, singleton-per-session, exact 9-child order/nesting, and pure-container (0 gaps, first payload byte 0x5A, 19/19). The proposed header-numerics fields are REFUTED (first-0x103d-child payload, value not stable).

### 0x104b — Fixed placeholder leaf inside the 0x1040 group

- **kind:** LEAF; parent = 0x1040 in 19/19 (confirmed by innermost-enclosing-block). No children in 19/19. block_type (u16@z+1) = 1 in 19/19. Exactly 1 instance per session; the whole 0x1040 group is a 1:1:1:1 singleton (one each of 0x1040/0x104a/0x104b/0x104c). Immediate sibling 0x104a is BYTE-IDENTICAL to 0x104b (same size, same `01 01 00 00` payload) — the pair looks like a paired fixed header rather than a single meaningful toggle.
- **size:** Fixed 6-byte block: size u32@z+3 = 6 in 19/19. Full frame = 13 bytes = 5A + block_type(u16=1) + size(u32=6) + content_type(u16=0x104B) + 4 payload bytes. Payload length = size-2 = 4 bytes. Constant across all 19 (including the session whose parent 0x1040 group is larger — that difference is entirely in a sibling 0x1041 string).
- **fields:**
  - `payload@z+9 (4 bytes) = 01 01 00 00` — byte-identical in 19/19 across 8+ distinct projects; NEVER varies.
  - `+0 u8 = 0x01` (invariant) — meaning UNKNOWN; plausibly a version/present flag but unconfirmable.
  - `+1 u8 = 0x01` (invariant) — meaning UNKNOWN; proposed 'enabled' flag is speculative and undercut by 0x104a being byte-identical.
  - `+2 u16 = 0x0000` (invariant) — likely reserved/pad, unconfirmed.
- **notes:** Reads as a hard-coded default / structural placeholder rather than user-driven state; PT almost certainly writes it from defaults. Corrected from proposal: the '01 01 00 00 0' 5-byte read overran by one byte into the next block's 0x5A marker — true payload is exactly 4 bytes (e == z+7+size, verified in 19/19). Bianca Long Road has a larger 0x1040 group (1475 vs 710) but 0x104b itself is unchanged.
- **confidence:** Structure (parent, size, block_type, leaf-ness, count, exact `01 01 00 00` payload) = HIGH (19/19 across 7+ distinct project families). Field MEANINGS (version vs enabled vs pad) = LOW: payload is 100% invariant, so the named flag/version/pad split is inference from position/width only, with zero corroborating variation.

### 0x104c — Fixed default-config leaf inside the 0x1040 group

- **kind:** LEAF; parent = 0x1040 (19/19, parent block_type = 4). No children (19/19). block_type (u16@z+1) = 3 (19/19). Positioned immediately after 0x104b and immediately before 0x1041 (prev/next direct sibling = 0x104b / 0x1041 in 19/19). Appears exactly once per session.
- **size:** Fixed: size u32@z+3 = 49; total span (z..end) = 56 bytes; payload = 47 bytes. Constant across all instances (19/19 in the non-huge corpus; also 1 instance in the skipped Hipsters session — pattern is 1-per-session, always 47-byte payload).
- **fields:**
  - `+0 u8` = version/flag: 0 or 1. THE ONLY VARYING PAYLOAD BYTE across the corpus. =1 in DWTS (all 4 variants), DOPE, MANOLITO, Never-Will-Marry; =0 in Reverse-Rewire (7 bak versions), Bianca, COGNAC, Let-Her-Go, Quality-Time, THE-WIND, Hipsters.
  - `+1 byte = 0x04` (constant); reads as u32 = 4 (bytes +1..+4 = `04 00 00 00`).
  - `+5 u16 = 0` (constant zero pad; NOT covered by the proposed three-u32 reading).
  - `+7 byte = 0x03` (constant); reads as u32 = 3 (bytes +7..+10 = `03 00 00 00`).
  - `+11 u32 = 2` (constant; bytes +11..+14 = `02 00 00 00`).
  - `+15 f64 = 100.0` (constant; LE bytes `00 00 00 00 00 00 59 40` = 0x4059000000000000).
  - `+23 u16 = 16384` (0x4000; bytes +23..+24 = `00 40`) (constant).
  - `+25..+46 = 22 bytes, all zero` (reserved; constant).
- **notes:** Reads as a fixed default-configuration record whose only session-variable field is the leading version/flag byte (100.0 = percent/full-scale default; 16384 = normalized-unit constant). HONESTY DOWNGRADE: reading the small integers as three u32s at +1/+7/+11 hides a structural inconsistency — a u32 at +1 ends at +5, leaving +5/+6 as an unexplained zero gap, and the strides are 6 then 4 (not uniform). What the data guarantees is isolated single nonzero bytes 0x04@+1, 0x03@+7, 0x02@+11 with everything else in +1..+14 zero; the true widths (u8 vs u16 vs u32) and whether these are enum/type tags vs counts are UNCONFIRMABLE from static corpus data. Once-per-session, consistent with a fixed default/display-state config.
- **confidence:** HIGH on the raw facts (frame, size, parent/siblings, every constant value verified byte-for-byte across 19/19 plus a 20th Hipsters instance; flag-mapping exactly correct). MEDIUM on the field-semantics interpretation — the 'three u32 ordinals' framing is the weak point.

### 0x104d — Top-level ordinal + 0.5-pair leaf (view-state)

- **kind:** LEAF; top-level (no enclosing 0x5A; parent None in 26/26). No children (26/26). block_type (u16@z+1) = 3. Exactly one instance per session (26/26). Immediately follows 0x1040 and precedes 0x207e in every session.
- **size:** Fixed: size u32@z+3 = 36 in every instance (26/26, incl. the huge Hipsters sessions). Payload = size-2 = 34 bytes (the trailing 2 bytes of a naive z+9..z+9+size read belong to the next block's 0x5A marker). Block span = [z, z+7+size]; payload@z+9.
- **fields:**
  - `+0 u16 = 0` (constant; reserved).
  - `+2 u32 = ordinal: 0, 1, or 4` — the ONLY field that varies (0 in 13, 1 in 2 [DOPE, MANOLITO], 4 in 4 [all DWTS variants]). Loosely correlates with 0x104c/0x1040 view-state but is NOT byte-equal to any field there.
  - `+6 u16 = 0` (constant).
  - `+8 u8 = 0` (constant).
  - `+9 u8 = 1` (constant; present/enable flag — value 1 in all 26, so 'flag' is inferred, not observed toggling).
  - `+10..+17 = 0x0000000000000000` (constant; 8 reserved bytes).
  - `+18 f64 = 0.5` (constant; 0x3FE0000000000000, in all 26).
  - `+26 f64 = 0.5` (constant; last 8 payload bytes, in all 26).
- **notes:** REFUTED from proposal: the ordinal does NOT track the same session flag as 0x104c/0x1040 — an exact-match scan of every u32 offset in 0x104c (min payload 47B) and 0x1040 (min payload 708B) found NONE equal to the ordinal across all sessions; 0x1040[+10] agrees for ord in {0,4} but reads 0 for the two ord=1 sessions, and 0x104c[+0] mis-tracks on Never Will Marry. Treat as one of several loosely-correlated global view-state values PT recomputes on open. Safe to hard-set to 0 when synthesizing (13/19 non-huge + 7/7 Hipsters use 0). The 50/50-split / active-index semantics are speculative — the 0.5 pair is a hard default, meaning unconfirmed.
- **confidence:** HIGH on structure (size=36, leaf, top-level, block_type=3, one-per-session, position after 0x1040 before 0x207e, every offset/width/constant) — 26/26 across ~9 distinct projects. LOW on the ordinal's meaning and the 0.5-pair semantics (proposed co-variation DISPROVEN by exact-match scan; no PT-oracle correlation established).

### 0x207e — Fixed 3-byte default marker (top-level leaf)

- **kind:** LEAF; top-level (parent None in 26/26 — no enclosing 0x5A block). No children (26/26). block_type (u16@z+1) = 2. Sits between 0x104d (immediately before) and 0x2047 (immediately after) in the final-index block stream.
- **size:** Fixed: size field (u32@z+3) = 5; full block span = 12 bytes (z..z+7+size); payload region (z+9..z+7+size) = 3 bytes (= size-2, since size counts the 2-byte content_type plus 3 payload bytes). Constant across all 26 instances.
- **fields:**
  - `+0 u8 = 0x00` (constant; meaning unknown — invariant).
  - `+1 u8 = 0x01` (constant; meaning unknown — invariant).
  - `+2 u8 = 0x01` (constant; meaning unknown — invariant).
- **notes:** Full span byte-identical: `5a 02 00 05 00 00 00 7e 20 00 01 01`. A constant default/toggle marker, NOT user-varying state — most likely a PT-emitted default flag/version stamp. Corrected from proposal: count is 26/26 corpus files (the 19 excluded the 7 Hipsters backups); per-byte labels ('reserved/off', 'enable/version flag', 'enable/mode flag') are NOT supported since the bytes are 100% constant — downgraded to 'constant, meaning unknown'.
- **confidence:** HIGH on structure (frame/size/kind/parent/children/neighbors, 26/26 across ~13 projects, full span byte-identical). LOW on field semantics (payload is a frozen constant `00 01 01`, so per-byte meanings are unverifiable from data alone).

### 0x2058 — Global session-preferences / options blob (versioned)

- **kind:** LEAF (0 children in all 19) and top-level (parent None in 19/19 — never nested). Discriminated union keyed by block_type/version (33/36/40/41). Standard frame: data[z]=0x5A, block_type u16@z+1, size u32@z+3, content_type u16@z+7 (=0x2058), payload@z+9.
- **size:** Fixed per version, reported three ways to resolve ambiguity: DECLARED size field (u32@z+3) = 137/151/168/169; PAYLOAD length (= size−2 = e−(z+9)) = 135/149/166/167; full block SPAN (= size+7 = e−z) = 144/158/175/176 — for v33/v36/v40/v41 respectively. Instance counts by version: v33=10, v36=3, v40=4, v41=2 (19 total).
- **fields:**
  - `z+1 u16 = SCHEMA VERSION / block_type` (only 33, 36, 40, 41 seen) — selects payload layout & length. CONFIRMED discriminator.
  - `z+3 u32 = declared size`; `z+7 u16 = content_type = 0x2058`; payload begins at z+9. CONFIRMED header.
  - `payload +12..+15 == 3C 00 EE FF` — CONSTANT ANCHOR in 19/19 across all 4 versions and all 11 projects. The single most reliable structural landmark.
  - `payload +58 == 0x20, +59 == 0x03` ('20 03') — CONSTANT in 19/19 (all versions).
  - `payload +113 == 0x19, +114 == 0x00` ('19 00') — CONSTANT in 19/19 (all versions). (Byte AT +113 is 0x19; +112 is 0x00.)
  - `payload +0/+1` (u8 each) — leading flag bytes; 0x01 in 17/19, 0x00 in 2/19 (both v33). The leading byte that varies most is `+2` (6 distinct values: 00/69/A0/AD/B2/D0). Treat +0..+2 as opaque; +2 most session-specific.
  - remaining bytes = scattered 0/1/small option flags. Varying offsets per version: v40 EXACTLY [26,35,48,62,64,68,82]; v33 [0,1,2,10,17,26,33,35,36,38,48,62,64,71,92,125]; v41 only [26,68]; v36 only [2,20,35,68]. No length-prefixed strings, no `2A 00 00 00` GUID tags, no plausible non-zero finite f64 (0/19).
- **notes:** A versioned preferences/options bag PT reads/writes wholesale, NOT a clean field-per-offset schema. CORRECTIONS from proposal: (1) '137/151/168/169-byte block' were the DECLARED size field, not the byte span (actual spans 144/158/175/176; payload = size−2); (2) '+0/+1 vary by session' overstates it — +0/+1 are constant 0x01 in 17/19; the genuinely session-varying leading byte is +2. For synthesis/edit: preserve the whole blob and its version (block_type @z+1) verbatim rather than editing individual bytes. Positioned between 0x2025 (prev) and 0x1040 (next), 19/19.
- **confidence:** HIGH for framing (version==block_type discriminator, per-version fixed length, the three constant anchors +12/+58/+113 all 19/19, LEAF + top-level + between-0x2025-and-0x1040, absence of strings/GUIDs/f64), verified across 11 distinct projects with every version represented by ≥2 projects. LOW for any precise per-offset field MAP — the bulk are undecoded option flags whose meaning (persisted pref vs. recomputed view-state) is not recoverable from static samples.

### 0x2055 — Click / metronome options block

- **kind:** LEAF (no children). Parent: none — top-level singleton (exactly one per session in all 26 corpus sessions). Consistent top-level neighbors: prev=0x2433, next=0x2023 (26/26). block_type u16@z+1 = 5 in all 26.
- **size:** Fixed. span(z..e)=38, declared size u32@z+3=31, payload=29 bytes.
- **fields:** (offset, width, meaning)
  - `0  2  u16` click MIDI note (60=C3 default in 19 sessions; 37 in the 7 Reverse Rewire sessions). ALWAYS equals field@6.
  - `2  2  u16` click velocity A (const 100). Proposed label 'accented' is unverifiable (equals field@4 everywhere).
  - `4  2  u16` click velocity B (const 100). Proposed label 'unaccented' is unverifiable (equals field@2 everywhere).
  - `6  2  u16` click MIDI note (duplicate of field@0: 60 or 37). Accented-vs-unaccented split between @0/@6 cannot be resolved from data.
  - `8  2  u16` const 127 (max velocity / fixed) — 26/26.
  - `10 2  u16` const 100 — 26/26.
  - `12 2  u16` click play/output mode enum: 1 (10 sessions) or 2 (16 sessions). byte@13 always 0.
  - `14 2  u16` const 257 (bytes `01 01`) — 26/26.
  - `16 1  u8` mode-mirror byte: 0 when mode@12==1, 1 when mode@12==2 (== mode@12 - 1, 26/26). NOT an independent flag.
  - `17 1  u8` const 1 — 26/26.
  - `18 2  u16` default click tempo ×100, integer (9300/12000/6500/10500). Equals round(f64@21 × 100) in all 26. CAVEAT: all corpus BPMs are integers, so '×100 fixed-point' vs coincidence is not provable for fractional tempo.
  - `20 1  u8` flag (0 in 20 sessions, 1 in 6). Independent of mode@12 and byte@16.
  - `21 8  f64` default click tempo in BPM (93.0/120.0/65.0/105.0). Ties exactly to u16@18. This is the authoritative tempo value.
- **notes:** Fully decodable; numeric fields are editable; f64@21 is the safe authoritative tempo to read/write. A stable persisted options block (not PT-recomputed view-state). CORRECTIONS from proposal: (1) corpus has 26 instances, not 20; (2) byte@16 is a mirror of mode@12 (not a free flag), so bytes 14-17 = `01 01 <mode-mirror> 01`; (3) field@0==field@6 and field@2==field@4 always, so 'accented'/'unaccented' labels CANNOT be confirmed.
- **confidence:** HIGH (26 sessions, ≥2 distinct projects; structure/size/kind/parent/neighbors rock-solid 26/26). Some field LABELS (accented vs unaccented) unverifiable because the pairs are always equal.

### 0x2511 — Fixed default preset-name table ('Snap1'..'Snap48')

- **kind:** LEAF (no children — first_child=None in all 26). Parent: none — top-level singleton (26/26). block_type u16@z+1 = 1. Flat-list neighbors: prev=0x258e (26/26), next=0x2056 (26/26).
- **size:** Fixed. span = 484 bytes (z..z+7+size). size u32@z+3 = 477. payload = 475 bytes = size-2. Constant across all 26.
- **fields:**
  - `0  4  u32  count = 48` (constant across all 26).
  - `4  var  48 × length-prefixed string`; each = u32 len (5 for 'Snap1'..'Snap9', 6 for 'Snap10'..'Snap48') + len ASCII bytes; values 'Snap1'..'Snap48' in order. Byte budget 4 + Σ(4+len) = 475 = payload_len exactly; parser consumes to block end (off==e).
- **notes:** Byte-identical in every corpus session (1 distinct payload sha256 across all 26) — PT's default set, never user-modified. The specific UI meaning (Snapshot vs Memory-Location preset names) is INFERRED from the 'Snap' prefix, NOT proven — no session deviates from the default. CORRECTIONS from proposal: prev neighbor is 0x258e, not 0x204a (0x204a sits ~5 blocks earlier).
- **confidence:** HIGH for structure (LEAF, top-level, offsets/widths, span=484/size=477, block_type=1, count=48, lp-string schema, consumed-to-end — all 26/26). Role interpretation (Snapshot/Memory-Location name table) is INFERRED and unconfirmable from the corpus.

### 0x2504 — Empty/placeholder collection (zero-count stub)

- **kind:** LEAF — 0 immediate children in all 26. Parent: none — top-level singleton near the end of the file (well past the audio body, always before the trailing 0x0002 master index). block_type u16@z+1 = 1. Neighbors (contiguous, verified prev.end==z and this.end==next.start): prev = 0x262a (20/26) or 0x2721 (6/26); next = 0x2630 (26/26); next+2 = 0x262e (26/26). NOTE: a flat position-sorted list shows 0x2628 immediately before, but that 0x2628 is a nested CHILD of the preceding 0x262a/0x2721 sibling, not a top-level neighbor.
- **size:** Fixed. size u32@z+3 = 6; content_type u16@z+7 = 0x2504; payload = 4 bytes @z+9. span = 13 in every instance. Raw frame: `5A 01 00 | 06 00 00 00 | 04 25 | 00 00 00 00`.
- **fields:**
  - `0  4  u32  value = 0x00000000` in all 26 (empty / count = 0).
- **notes:** A fixed 4-byte all-zero payload (a u32 = 0, i.e. count = 0 of some late-file list). Invariant across the corpus; carries no per-session data — almost certainly a zero-count stub for a list PT recomputes/re-emits. Because the payload is identical zero in every session, the exact list it heads CANNOT be resolved from data alone. CORRECTION from proposal: true count is 26/26 (the '20 sessions' undercounted, excluding the 7 Hipsters backups).
- **confidence:** HIGH — every offset/width/value verified exactly (block_type=1, size=6, span=13, payload == `00 00 00 00`, zero variation), kind/parent/neighbors confirmed at true top-level-sibling granularity. The only thing NOT resolvable is the semantic identity of the empty list it heads.

### 0x2063 — Track/mix list column-configuration (display/view-state)

- **kind:** LEAF (no children; first_child=None). Parent: none — top-level singleton (26/26). block_type u16@z+1 = 1. Next neighbor ALWAYS 0x2031 (26/26). Prev neighbor is NOT fixed: 0x2024 in 17/26 and 0x0f3e in 9/26.
- **size:** Fixed. span = 43; header = 9 bytes (5A + block_type u16@z+1=1 + size u32@z+3=36 + content_type u16@z+7=0x2063); payload = data[z+9 : z+7+size] = 34 bytes (size-2). Identical across all 26.
- **fields:**
  - `0  4  u32  len1 = 13` (const, 0x0d000000).
  - `4  13  bytes visibility flags`, one 0/1 byte per column. Per-column across 26: col2,3,9,10,11 CONSTANT 0; col5,6,12 CONSTANT 1; col0 varies (1 only in DOPE + MANOLITO); col1 varies (0 only in COGNAC/Never Will Marry/THE WIND, else 1); col4 varies (1 only in Let Her Go); col7 varies (1 only in the 7 Reverse Rewire backups); col8 varies (0 only in COGNAC/Never Will Marry/THE WIND, else 1).
  - `17 4  u32  len2 = 13` (const, 0x0d000000).
  - `21 13  bytes display-order list`: first 11 bytes are a permutation of the ordinal SET {0,1,2,3,4,5,6,8,9,10,11} (10 present, 7 absent), then 2 trailing 0x00 pad slots (slots 11,12 = 0 in all 26). Order = 0,1,2,10,3,4,6,8,9,11,5 in 25/26; sole outlier is Bianca (moves ordinal 4 to the last real slot: 0,1,2,10,3,6,8,9,11,5,4).
- **notes:** A 13-element per-column visibility flag array + a 13-element display-order list (permutation of column ordinals + trailing pad). Display/view state PT recomputes/persists — NOT structural session data. arr1 uses 13 slots incl. col7 while arr2's ordinal set includes 10 but excludes 7, so the column→ordinal mapping is not a clean 1:1 identity; per-slot 'meaning' is best-guess. CORRECTIONS from proposal: prev neighbor is 0x2024 OR 0x0f3e (9/26 are 0x0f3e); col5/6/12 are CONSTANT 1.
- **confidence:** HIGH — re-derived across 26 instances (12 distinct session families); two-array layout exactly fills the 34-byte payload; 6 distinct payloads / 5 distinct flag-arrays / 2 distinct order-arrays; all spot-check claims hold. Exact column→ordinal semantics only weakly inferable.

### 0x204d — SMPTE/timecode ruler setup (top-level singleton, two variants)

- **kind:** LEAF (no children in any of 26). Parent: none (top-level singleton; never enclosed). Immediate neighbors stable: prev = 0x207a (26/26), next = 0x2637 (26/26). Two variants keyed by block_type u16@z+1 (4 or 3).
- **size:** Variant-dependent. bt=4 (9/26): span=49, size=42, payload=40. bt=3 (17/26): span=34, size=27, payload=25. Frame confirmed exactly (data[z]=0x5A, block_type@z+1, size@z+3, content_type@z+7=0x204d, end=z+7+size).
- **fields:**
  - `bt=4  0   4  u32  kind ordinal` (observed 2/7/9; meaning unconfirmed).
  - `bt=4  4   4  u32  flag` (0 or 1).
  - `bt=4  8   1  u8  pad = 0` (all).
  - `bt=4  9   4  u32  rate-like value` (90000=25fps×3600, 86400=24fps×3600, also 1036800 and 0 which break the fps formula; NOT a pure function of kind — meaning unconfirmed).
  - `bt=4  13  4  u32  constant marker = 0x91888C01` iff flag@4==1, else 0 (NOT a per-save nonce; fixed value, verified across DOPE/COGNAC/Never-Will-Marry).
  - `bt=4  17  4  u32  constant = 1` (all bt=4).
  - `bt=4  21  4  u32  kind echo == field@0` (8/9 bt=4; MANOLITO is the sole exception: kind@0=9, this=2).
  - `bt=4  25  var  length-prefixed string`: u32 len=11 then 11 ASCII = '24:00:00:00' (consumes payload to end; slen==11 in all 9).
  - `bt=3  0   4  u32  kind ordinal = 7` (all 17 bt=3).
  - `bt=3  4   4  u32  value = 0xE8 (232)` in 16/17, 0x48 (72) in Bianca only; meaning unconfirmed.
  - `bt=3  8   5  bytes  zero pad = 00 00 00 00 00` (all).
  - `bt=3  13  4  u32  per-save nonce/hash`: distinct on every backup (7 distinct values across 7 Hipsters backups AND across 7 Reverse-Rewire backups).
  - `bt=3  17  4  u32  flag = 1` (all).
  - `bt=3  21  4  u32  kind echo = 7 == field@0` (all 17).
- **notes:** bt=4 = full timecode-ruler record (rate-like value + session-length SMPTE string '24:00:00:00'). bt=3 = shorter companion session-stamp record whose only per-save-varying field is a nonce at +13. Exact semantics of the small kind ordinals and rate value NOT fully pinned — timecode/session-setup state PT recomputes on save. CORRECTIONS from proposal: (1) prev neighbor is 0x207a, NOT 0x2031; (2) the bt=4 '@13 8-byte flags/nonce region' is WRONG — it is two u32s (@13 constant marker 0x91888C01 iff flag@4==1, @17 constant 1); NO nonce in bt=4, the only genuine nonce is bt=3 @13; (3) bt=3 count is 17 (not 9), bt=4 count is 9, total 26.
- **confidence:** MEDIUM. HIGH/confirmed: singleton/top-level/LEAF, frame, two block_type variants and spans, next=0x2637, bt=4 pad@8=0/@17=1/string, f0==f21 echo (except MANOLITO), all bt=3 fields. DOWNGRADED to medium for fields with unresolved meaning: bt=4 kind ordinal@0 (2/7/9), rate@9 (90000/86400 fit fps×3600 but 1036800 and 0 do not, and rate is not a function of kind), bt=3 v@4 (232 vs 72).

### 0x206e — Edit-window view/selection state (top-level singleton)

- **kind:** LEAF (0 descendants in all 26). Parent: none (top-level singleton). block_type u16@z+1=4. Neighbors: prev=0x2049, next=0x2073 (both 26/26).
- **size:** Fixed. span=120, size u32@z+3=113, payload=111 bytes (payload@z+9, ends at z+7+size). Identical across all 26 incl. the 7 Hipsters backups.
- **fields:**
  - `0  4  char[4]  4CC tag` for the last-focused edit-group / track-view object. Corpus set is exactly {tbdi, grps, htms, mfnc}; 'idbt' NEVER appears. Constant within a backup chain but the SAME 4CC recurs across unrelated projects (tbdi spans 7 songs/studios), so NOT a per-session content fingerprint.
  - `4  8  u64  timeline START`, plausibly samples. Hi dword (@8-11) always 0; low u32@4 holds the value. END>=START in all 26. u64-vs-u32 indistinguishable here; 'samples' is inferred from magnitude/ordering.
  - `12 8  u64  timeline END`, same encoding (@16-19 always 0). ≥ START; == START in cursor-only sessions.
  - `20 4  u32  viewport/first-visible-track value` (low 2 bytes only; @22-23 always 0). NOT a selection endpoint (does not scale with the sample selection, does not collapse to @36 when cursor-only).
  - `24 4  u32  small index/lane accompanying @20` (observed 1 or 4; @25-27 always 0).
  - `36 4  u32  second viewport value paired with @20` (@38-39 always 0). @36 > @20 by a small session-family-clustered amount (2/4/6/12) EVEN when START==END — consistent with a visible-track-range span, not a selection end.
  - `40 4  u32  small index/lane accompanying @36` (observed 1 or 3; @41-43 always 0).
  - `52 24 bytes  view-config vector`, MOSTLY-but-not-constant: @52 and @56 = 4 normally but 6/8 in the huge Hipsters session; @60 = 0x00030001 normally, 0x00040001 in Hipsters. Only @64=0, @68=2, @72=3 are truly constant.
  - `76 4  u32` varies (0x10000/0x30000/0x30002-ish) — additional view-config, opaque.
  - `80 ~30 bytes  zoom / track-height / scroll display fields`, recomputed by PT. Observed varying u16s: @86=100 or 95, @92=100 or 111, @96=5 or 25. Values (100/111/95/25/5) look like zoom-% / track-height, but the schema is unconfirmed.
- **notes:** Per-edit-session UI state PT recomputes/persists; most fields are display state, not session content. CORRECTIONS from proposal: (1) removed 'idbt' — corpus set is {tbdi,grps,htms,mfnc}; (2) @20/@36 are NOT selection start/end — the claimed 'cursor implies @20==@36' invariant is falsified (DWTS/Reverse Rewire/Hipsters have START==END but @20<@36), and Bianca/Let Her Go/Quality Time have byte-IDENTICAL payloads despite 3493/121/2 tracks — read as a visible-track viewport range PT recomputes; (3) the @52 'constant 04,04,01,03,02,03' header is not constant (@52/@56/@60 vary, larger in Hipsters).
- **confidence:** MEDIUM. HIGH/confirmed: top-level LEAF singleton, span=120/size=113/block_type=4/payload=111, parent=none, prev=0x2049, next=0x2073, 4CC@0-3 clusters within a backup chain, u64@4(START)/u64@12(END) hi-dword=0 and END>=START. Nearly all payload SEMANTICS beyond the 4CC and START/END pair are display/view state PT recomputes and cannot be pinned from static bytes.

### 0x2604 — Session-level display/preference toggle-state record

- **kind:** LEAF (no children in any of 19; parent: ROOT/top-level in all 19). Exactly 1 instance per session (19/19 non-Hipsters; Hipsters skipped per harness).
- **size:** Fixed. Header = 9 bytes (5A + block_type u16 + size u32=0x11 + content_type u16=0x2604), declared u32 size = 17, payload = 15 bytes, block span = [z, z+24).
- **fields:**
  - `+0 10 bytes  always 0x00` (payload +0..+9, verified 0 in all 19).
  - `+10 u8  boolean flag A` (0 or 1); 0x00 in 7 sessions, 0x01 in 12.
  - `+11 u8  boolean flag B` (0 or 1); 0x01 only when A=1 (no A=0,B=1 case); 0x01 in 7, 0x00 in 12.
  - `+12 3 bytes  always 0x00` (payload +12..+14, verified 0 in all 19).
- **notes:** Payload almost entirely zero; only two adjacent boolean flag bytes vary. Small view/preference state PT recomputes; meaning of the two flags unresolved. Observed (A,B) combos = {(0,0)×7, (1,0)×5, (1,1)×7}; B=1⇒A=1 held with zero violations. Root siblings: preceded by 0x2437 (19/19), followed by 0x2624 (19/19). FRAMING NOTE: the block_type word (u16@z+1, NOT payload) is not constant — 0x0003 in 13 sessions, 0x0004 in 6; a header/framing field that does not affect the payload schema.
- **confidence:** HIGH — payload verified byte-for-byte across all 19 sessions (≥8 distinct families); LEAF confirmed (no child 0x5A in any payload). The exact meaning of flags A/B is NOT resolved.

### 0x2013 — Selection/view-state wrapper container

- **kind:** CONTAINER (parent: ROOT). block_type (u16@zmark+1) = 3 in all 26. Children ALWAYS exactly [0x2020, 0x2021, 0x2203] in that order (26/26). No own scalar fields — payload is exactly its three child blocks back-to-back (all 76 gaps = 0 across 19 re-derived sessions).
- **size:** Variable. block size observed = 45 / 67 / 68 (payload = size-2 = 43 / 65 / 66). 1 instance per session, present in ALL 26 (NOT 19/19 — the 7 Hipsters sessions are all size-45). Size is driven by which child list is populated: 45 = all three children empty (child spans 17+13+13); 67 = 0x2020 grows by one 0x2079 record (39+13+13); 68 = 0x2021 grows by one 0x2078 record (17+36+13). 0x2203 is empty (span 13, count 0) in all 26. At most ONE child is populated (single entry) in the corpus.
- **fields:**
  - `+0` (payload start = zmark+9): child 0x2020 block begins immediately (0-byte gap; no scalar prefix on the wrapper).
  - child 0x2021 follows 0x2020 with 0-byte gap.
  - child 0x2203 follows 0x2021 with 0-byte gap; 0-byte tail gap after last child (wrapper end = zmark+7+size exactly).
- **notes:** A pure wrapper — all payload bytes belong to the three children. Populated forms carry edit-selection/view-state records (0x2078/0x2079) PT recomputes. Root siblings: preceded by 0x202b, followed by 0x2050 (19/19). CORRECTIONS from proposal: count is 26/26 (1 each); the size story is not 'both sub-lists' — each of the three children is an independent length-prefixed list (u32 count at child+9) and the increase comes from a SPECIFIC child gaining a single record.
- **confidence:** HIGH for structure (child sequence [0x2020,0x2021,0x2203], block_type=3, ROOT parent, 1-per-session, zero gaps, payload=children-back-to-back — all 26). MEDIUM/informational for the size/payload meaning: 45/67/68 are the only observed values but the payload is recomputed view-state, so do NOT treat them as an exhaustive fixed size set.

### 0x2020 — Selection / window-membership view-state list (child of 0x2013)

- **kind:** LEAF when empty (15/19); CONTAINER of exactly one 0x2079 when populated (4/19). Parent: ALWAYS 0x2013 (19/19); always the FIRST of siblings [0x2020, 0x2021, 0x2203]. 0x2079 occurs ONLY here (0 or 1 per session).
- **size:** Empty form: block_size=10, payload=8 bytes (all zero). Populated form: block_size=32, payload=30 bytes (one u32 count=1, one zero u32, and one 22-byte 0x2079 block).
- **fields:**
  - EMPTY payload (8 bytes): two zero u32s (`00 00 00 00 00 00 00 00`), byte-identical in all 15 empty sessions. Reads as count=0, pad=0.
  - POPULATED payload (30 bytes): two u32 words (one count=1, one zero/pad) plus the embedded 22-byte 0x2079 block. The zero/pad u32 FLOATS relative to the child — TWO layouts observed: (A) `00 00 00 00 | 01 00 00 00 | <0x2079 22B>` (child at payload offset 8, nothing after); (B) `01 00 00 00 | <0x2079 22B> | 00 00 00 00` (child at offset 4, trailing zero u32). Count word is always 1, but its position (before vs after the pad/child) is NOT fixed.
  - EMBEDDED 0x2079: btype=0x0001, block_size=15, payload=13 bytes. Widths confirmed: u32 a, u32 b, u16 c, u16 d, u8 flag(=1). Observed a in {38,38,49,39}, b in {440,422,53,525}, c in {55,14,46,28}, d in {5,2,1,0}. MEANINGS UNPROVEN — a/b/c/d tested against block-type population counts, only inconsistent/coincidental matches; treat as opaque selection/reference indices.
- **notes:** Low-frequency view-state, exactly one per session, always inside 0x2013. Empty in 15/19; populated in 4/19 (Reverse Rewire ...073 and ...078, Bianca Long Road, THE WIND). Because it is empty-by-default and PT-recomputable, safest synthesis is the empty 8-zero-byte form. Hipsters skipped by the harness.
- **confidence:** MEDIUM. Empty form (8 zero bytes) rock-solid (15/15 byte-identical, LEAF, parent 0x2013). Populated form + 0x2079 field WIDTHS confirmed in 4 sessions. DOWNGRADED because: the relative order of count-u32 vs pad-u32 around the child is NOT fixed (2 layouts), and the 0x2079 a/b/c/d meanings are inferred, not proven.

### 0x2021 — Selection/view-state list #2 (middle child of 0x2013)

- **kind:** LEAF when empty (22/24); CONTAINER of exactly one 0x2078 when populated (2/24). Parent ALWAYS 0x2013 (24/24); fixed MIDDLE child in [0x2020, 0x2021, 0x2203]. Framing: data[z]=0x5A, block_type u16@z+1 = 0x0002 (container), size u32@z+3, content_type u16@z+7, payload@z+9.
- **size:** Empty form: size=6, payload=4 bytes (single u32 count=0). Populated form: size=29, payload=27 bytes (u32 count=1 + one embedded 0x2078 block). The embedded 0x2078 is 23 bytes on disk (5A + block_type u16=0x0001 + size u32=16 + ct u16=0x2078 + 14-byte payload) and exactly fills to the container's end.
- **fields:**
  - `payload +0 u32 count` (0 = empty leaf; 1 = one embedded 0x2078). Only values 0 and 1 observed.
  - `payload +4` (populated only): embedded 0x2078 block (`5A 01 00 | size=10 00 00 00 | ct=78 20 | 14-byte payload`).
  - `0x2078 payload +0 u32` (=84, 38 in the 2 samples) — meaning unknown.
  - `0x2078 payload +4 u32` (=284, 802) — meaning unknown.
  - `0x2078 payload +8..+13 = 6 trailing bytes`, always `46 00 00 00 00 01` / `0f 00 00 00 00 01`. Reads EITHER as u32 (=70, 15) + u16 tail 0x0100, OR (paralleling 0x2079) as u16 + u16(=0) + u8(=0) + u8(=1). Only 2 samples, both with high half zero — these inner boundaries are INFERRED, not confirmed.
- **notes:** Empty in nearly all sessions; the 'selection/view-state' semantic is INFERRED (never observed carrying authored data) — almost certainly editor state PT recomputes. CORRECTIONS from proposal: (1) counts are 22/24 empty and 2/24 populated once Hipsters are included; (2) parent 0x2013 also contains 0x2203 as a third sibling — the triple [0x2020,0x2021,0x2203] is fixed; (3) the 'simpler header than 0x2020' claim is NOT supported (0x2020's populated form embeds a 0x2079 whose payload can contain 0x5A bytes, confounding naive parsing) — assertion dropped; (4) 0x2078 occurs ONLY as a child of 0x2021 (2 total, both parented by 0x2021); (5) the 'u32 a, u32 b, u16 c, u16 d, u8, u8' split is one valid reading but presented with false precision — with n=2 and the third u32's high half always zero it is indistinguishable from 'u32, u32, u32, u16 tail'.
- **confidence:** HIGH for structure/size/kind/parent/sibling-order (empty in 22, populated in 2; 0x2078 block framing and 'u32 count + one record' layout). LOW for the internal field split of the 14-byte 0x2078 payload and ALL field MEANINGS (only 2 instances, two equally-consistent decompositions, no observed semantics).

### 0x2203 — Fixed zero-count/reserved placeholder leaf (third child of 0x2013)

- **kind:** LEAF (parent: ALWAYS 0x2013; grandparent: none — 0x2013 is top-level). block_type (u16@z+1) = 0x0001 in all 26. ALWAYS the third and last child of 0x2013.
- **size:** Fixed. size u32@z+3 = 6; total block span = 13 bytes (5A + type:2 + size:4 + content_type:2 + payload). Payload (@z+9) = 4 bytes. size(6) = content_type word(2) + payload(4). Raw bytes identical every session: `5A 01 00 06 00 00 00 22 03 00 00 00 00`.
- **fields:**
  - `+0 u32 = 0` always (@z+9; count or reserved flag, never observed non-zero).
- **notes:** Every 0x2013 has exactly three children in fixed order (0x2020, 0x2021, 0x2203), one 0x2013 per session, so 0x2203 appears exactly once per session. Because the u32 is ALWAYS 0, its exact semantic (empty count vs reserved flag) is genuinely indistinguishable from data. A static structural block, not PT-recomputed view-state — byte pattern is invariant. CORRECTIONS from proposal: sample size is 26 sessions (not 19); size=6 spans content_type word + 4-byte payload (payload is 4 bytes, not 6); block_type is 0x0001.
- **confidence:** HIGH — verified against ALL 26 sessions, exactly 1 instance each: block_type=0x0001, size=6, payload u32=0, parent=0x2013, position=3rd/last child. Zero variance.

### 0x204b — Referenced/template audio-format descriptor (singleton)

- **kind:** LEAF (parent: ROOT, 19/19). Payload contains inline GUID(s) and/or a length-prefixed name string but NO framed 0x5A child blocks (n_children=0, 19/19). The generic block-header type u16@z+1 is a per-instance list/count header (observed 0x0007/0x000c/0x0011), NOT part of the content schema.
- **size:** Variable; observed {35(×10), 54, 62, 70, 87, 95(×4), 99}. payload=size-2 (19/19). Exactly 1 per session (19/19 non-huge; Hipsters excluded). Size 35 = minimal Form-A (GUID, no name); larger sizes carry a name string and/or a second GUID.
- **fields:**
  - `+0 u16 flag/index`: 0xffff (unset) in 8/19; else a small value (4, 84, 96, 112) in 11/19.
  - `+2 u8 / +3 u8 format/version code pair` — observed exactly {03 03 (×11), 08 03 (×4), 03 02 (×4)}.
  - `+4 u16 SOURCE bit depth` (16/24/32; 16×4, 24×13, 32×2). NOT the session bit depth — mismatches the 0x1028 session-header bit-depth byte (=24 in all 19) in 6/19. Correlates with +6.
  - `+6 u32 SOURCE sample rate` (44100/48000/96000). Matches PTFFormat.sessionrate() in only 14/19; the 5 mismatches (4× DWTS src=44100 vs session 48000; Bianca src=48000 vs session 44100) prove this is a source/template rate, not the session master.
  - `+10 u16 = 5` — constant (19/19).
  - `+12 u32 channel/format enum = 1 or 2` (10× value 1, 9× value 2).
  - `+16 u16 = 0` — constant (19/19).
  - VARIABLE TAIL — two distinct forms (do NOT treat as one fixed schema):
    - Form A (GUID-bearing; 9/19): at +18 the tag `2A 00 00 00` then an 8-byte source GUID, then 1-3 trailing bytes (e.g. `00 01 01`). Minimal size-35 sessions end here. DOPE additionally carries a SECOND `2A 00 00 00`+same GUID at +48 and a length-prefixed name string (u32 len + bytes) at +60 ('DOPE BGV BOUNCE').
    - Form B (no GUID; 10/19): +18..+45 is mostly zeros with a 01/01 01 marker near +32; a length-prefixed name string (u32 len + bytes, can be UTF-8 e.g. '•setup') begins at +46 WHEN a name is present. COGNAC and THE WIND are Form B with no name.
- **notes:** Records a REFERENCED SOURCE/TEMPLATE's format, not the live session master clock — +6 mismatches sessionrate() in 5/19 and +4 mismatches true session bit depth in 6/19. The Form-A source GUID also appears in the audio-source-reference blocks 0x2033/0x2602/0x260e in the same file, confirming it links this descriptor to a specific source object. Root-sibling invariant: 0x2050 before, 0x207b after (19/19; the raw byte-adjacent predecessor is a child of 0x2050's subtree). CORRECTIONS from proposal: (1) '+4 matches session bit depth' is FALSE — it is SOURCE bit depth, mismatches in 6/19; (2) the tail is NOT a single '+18 GUID then optional name' layout — the name-string offset is form-dependent (Form A ends at GUID or +60; Form B at fixed +46).
- **confidence:** HIGH for the fixed header +0..+17 (+10=5, +16=0 constant; +12 in {1,2}; +2/+3 pair set; +6 cross-checked vs sessionrate(); +4 in {16,24,32}) and structural facts (singleton, LEAF/ROOT, sibling 0x2050/0x207b, payload=size-2, 8-byte GUID after `2A 00 00 00` cross-corroborated in 0x2033/0x2602/0x260e). MEDIUM for the variable tail: the two forms and their GUID/name-string offsets are decoded and consistent within each form, but not unified into one generalized schema.

### 0x207b — Session timecode/SMPTE-config record (two forms)

- **kind:** Both, determined by block_type. block_type=0x02 (13/19) → LEAF, span 18, 9-byte payload, no children. block_type=0x03 (6/19) → CONTAINER, span 115, with exactly one child: content-type 0x4530 (block_type 0x01, whose OWN span is 97, NOT 115). Parent always top-level (None) in all 19. block_type deterministically predicts child presence.
- **size:** Bimodal, exactly 1 per session (19/19). Small form: span=18, size_field=11, payload=9 bytes, block_type=0x02 (13 sessions). Large form: span=115, size_field=108, payload=9 bytes of own data + nested 0x4530 child (child size_field=90, child span=97), block_type=0x03 (6 sessions).
- **fields:**
  - `+0 u8 = 0` (constant across all 19).
  - `+1 u16 = enum`, observed {0:4, 1:10, 3:5}. UNMAPPED. Independent of the small/large form. NOTE: the '1:10' bucket is inflated by backup duplication — 7 of those 10 are Reverse Rewire backups of one logical session (~3 distinct sessions with value 1). Do not treat the histogram as 10 independent samples.
  - `+3 u32 = 1000` (0x03E8), CONSTANT across all 19. Meaning speculative (proposal guesses a subframe/ppq base; not proven).
  - `+7 u16 = enum {3:17, 5:2}`. CONFIRMED distribution. UNMAPPED. In the small form this occupies payload bytes [7:9]. Value 5 appears only in 2 large-form sessions (DWTS-for-2703, THE WIND).
  - `+9 (large form only) nested 0x4530 block`: block_type=0x01, size_field=90, span=97. BYTE-IDENTICAL across all 6 large sessions. Its payload ends in a u32-length-prefixed ASCII string (len=11) at offset +61 within the child = '00:00:00:00' (HH:MM:SS:FF session start timecode). No GUID / `2A 00 00 00` tags anywhere in the block (all 19).
- **notes:** In the top-level session-preferences region; top-level neighbors byte-for-byte constant: ...0x2050, 0x204b, [0x207b], 0x2016, 0x2017... (19/19). PT-owned session-config state PT recomputes/rewrites; not for hand-authoring. The 'timecode/SMPTE-config' role is well-grounded for the large form (direct HH:MM:SS:FF evidence) but INFERRED for the 13 small-form sessions (no timecode string). IMPLEMENTATION CAVEAT: small form payload is 9 bytes with no child / no timecode string — consumers must not read past +9. CORRECTIONS from proposal: (1) the 0x4530 child spans 97 (size_field 90), NOT 115 (115 is the 0x207b PARENT span); (2) +1 does NOT track form/child-presence — only block_type predicts child presence, +1 varies independently within both forms.
- **confidence:** HIGH on frame, size/kind bimodality, per-session count (1), parent=top-level, neighbor order, constant +0=0 and +3=1000, block_type-predicts-child, and the byte-identical 0x4530 child + its '00:00:00:00' timecode string. MEDIUM-LOW on the meaning of the two u16 enums (+1, +7) — distributions stable but unmapped, and +1's spread is partly a backup-duplication artifact.

### 0x2018 — Small fixed boolean/flags block (top-level window/view-state)

- **kind:** LEAF (0 children, 26/26). Parent: top-level (None, 26/26). block_type=0x08 (all instances). prev top-level sibling = 0x2019 (26/26). next top-level sibling = 0x201a (20/26) OR 0x4300 (6/26 — the 4 DWTS variants + MANOLITO + THE WIND, where an 11-byte 0x4300 block sits between 0x2018 and 0x201a).
- **size:** Constant. Exactly 1 per session (26/26). span=22, size_field=15 (u32@z+3), payload 13 bytes — identical across ALL 26 incl. the 7 huge Hipsters backups. Header = `5A 08 00 (block_type=8) | 0F 00 00 00 (size=15) | 18 20 (content_type)`. Payload @z+9.
- **fields:**
  - `+0 u32 (LE) = 1` (constant, 26/26). Labeling it a 'count' is speculative — it is followed by 9 more payload bytes, so it does not act as a byte count; treat as a constant leading u32.
  - `+4 u8 = 1` (constant).
  - `+5 u8 = toggle A` (boolean). Full corpus {0:18, 1:8}. (Non-huge 19-session subset: {0:11, 1:8}.)
  - `+6 u8 = toggle B` (boolean). Full corpus {0:23, 1:3}. Observed only when A=1: (A,B) combos are (0,0)×18, (1,0)×5, (1,1)×3 — (0,1) never occurs.
  - `+7..+12 u8 = 1` each (constant, 26/26).
- **notes:** A fixed 13-byte record almost entirely 0x01 with exactly two bytes (+5, +6) that toggle 0/1 between sessions — consistent with a small boolean/flags array (display/view-state PT recomputes). CORRECTIONS from proposal: (1) full corpus is 26 (adding 7 Hipsters, all +5=0/+6=0); (2) 'next 0x201a' holds in only 20/26 (6 sessions have an 11-byte 0x4300 between 0x2018 and 0x201a); (3) dropped the '+0 = count' assertion (+0 is just a constant u32=1); (4) +6=1 only co-occurs with +5=1.
- **confidence:** HIGH on the fixed 22-byte block / 13-byte payload, block_type=0x08, LEAF + top-level, and the fact that ONLY +5 and +6 vary (byte-for-byte 26/26 incl. Hipsters). MEDIUM/LOW on semantics: +5/+6 are clearly booleans but not mapped to named preferences; the '+0 = count' and 'block_type 0x08 = bool-array record type' labels are PT-internal interpretations not verifiable from the corpus.

### 0x201b — Saved floating-window geometry (populated member of window-geometry family)

- **kind:** CONTAINER. Exactly one child, content-type 0x2011 (the rect record). Parent: top-level (None) in all 19. Immediately preceded by top-level sibling 0x201a and followed by 0x201c (then 0x201d/0x201e/0x201f). Parent block_type is 0x02 in all instances. One member of the contiguous ordered window-geometry family 0x201a/0x201b/0x201c/0x201d/0x201e/0x201f — 0x201b is NOT the 'first' (0x201a precedes it with the same schema).
- **size:** Exactly 1 per session (19/19). span=38 when child block_type=0x01 (13 sessions, child size 20, child span 27) / span=39 when child block_type=0x02 (6 sessions, child size 21, child span 28 — one extra 0x00 byte inside the child after the marker). Parent size_field = 31 or 32 (span == 7 + size_field). +2 trailing flag bytes always follow the child inside the parent. (The proposed 'size_field=29' never occurs.)
- **fields:**
  - Parent payload (z+9) IS the nested 0x2011 child block: parent_payload+0 = child 0x5A marker; child content-type u16 @ parent_payload+7 = 0x2011.
  - Rect offsets — from the CHILD payload start (cz+9): `+0 u32 = x, +4 u32 = y, +8 u32 = width, +12 u32 = height`. (The proposed +9/+13/+17/+21 were off by 9 bytes and produced garbage.)
  - `child payload +16 u16 = position-saved / populated flag = 0x0100` in all 19 (byte sequence `00 01`). It is 0x0000 (with an all-zero rect) for the empty 0x201c sibling, confirming its boolean role. (Value is 0x0100 not 0x0001, at +16 not +25.)
  - when child block_type=0x02: one extra 0x00 byte inside the child after the marker (child size 21 vs 20).
  - after the child, inside the 0x201b parent: 2 trailing bytes. Observed values 0x0101 (13×), 0x0000 (5×), 0x0100 (1×) — plausibly per-window open/visible booleans (view-state, not verified).
- **notes:** This window is populated (non-zero rect, marker=0x0100) in all 19; contrast 0x201c which is always empty (rect 0,0,0,0 / marker 0x0000). Pure display/view-state PT recomputes and rewrites on save. Rect x/y/w/h vary per session; width clusters ~623-647, height ~135-529. In DOPE.ptx rect=(698,371,647,529) where field3 (647) < field1 (698), so field3 must be a WIDTH, not a right-edge.
- **confidence:** HIGH on structure/counts/rect; MEDIUM on exact byte-level semantics of the flag words. Every claimed field offset in the proposal was off by 9 bytes (rect) or misplaced (the flag: value 0x0100 at +16, not 0x0001 at +25), and 'size_field=29' does not occur.

### 0x201c — Saved geometry slot for an UNUSED floating window

- **kind:** CONTAINER. Exactly one child, content-type 0x2011 (a rect record). Parent: top-level (None) in all 26. Preceding sibling 0x201b, following sibling 0x201d (raw prev/next BLOCK is each sibling's 0x2011 child, since storage is depth-first). Parent block_type is always 1.
- **size:** Exactly 1 per session (26/26 incl. the 7 large Hipsters sessions). Two byte-identical variants driven by the CHILD's block_type: child bt=1 → parent span=37, size_field=29, 18-byte child payload (20/26); child bt=2 → parent span=38, size_field=30, 19-byte child payload (6/26). NO trailing bytes after the child (parent end == child end).
- **fields:**
  - Parent payload (z+9) == the child 0x2011 block. Child content-type u16 @ z+16 = 0x2011; child zmark == z+9 in all 26.
  - Child rect, four u32 at child-zmark offsets (== parent-payload offsets `+9/+13/+17/+21`): x, y, w, h. ALL FOUR = 0 in all 26. (Offsets confirmed against populated sibling 0x201b, e.g. DOPE rect=(698,371,647,529).)
  - Position-valid flag: a single BYTE at parent-payload offset +26 (== child-payload +17). Value 0x00 = never positioned (0x00 in all 26). Populated siblings 0x201b/d/e have 0x01 here. Byte +25 (== child-payload +16) is a constant 0x00 separator. Reading a u16 @ +25 yields 0x0000 for 0x201c and 0x0100 (=256) for the populated siblings — NOT 0x0001.
  - Child block_type=2 variant appends one extra trailing 0x00 byte after the flag (child payload +18); a record-version difference, not extra data.
- **notes:** Its rectangle is all-zeros in every session and its position-valid flag is 0 — this window was never positioned. Pure display/view-state PT recomputes; not needed for a synthesized session beyond matching the donor byte pattern. Flag semantics corroborated by the populated siblings 0x201b/d/e (non-zero rects, flag byte=0x01, same child schema). CORRECTIONS from proposal: (1) parent span is 37/38 (size_field 29/30 right; span=size_field+8); (2) full-corpus split is 20/6, not 13/6 (the 7 Hipsters also have child bt=1); (3) the flag is a single BYTE at +26 valued 0x01/0x00, not a u16=0x0001/0x0000 at +25; (4) the 18-vs-19-byte difference is a child block_type (record-version) difference.
- **confidence:** HIGH — rect=(0,0,0,0) and flag byte=0x00 identical across all 26 (incl. 7 Hipsters, 3 DWTS variants); offsets re-derived against a populated instance so byte positions are not ambiguated by the all-zero payload.

### 0x201d — Saved geometry for a narrow floating window (middle of the run)

- **kind:** CONTAINER. Exactly one child, content-type 0x2011, in all 19. Parent: top-level (None). Immediate top-level neighbors always ...0x201b, 0x201c, [0x201d], 0x201e, 0x201f... — 0x201d is the MIDDLE member of a 5-block run (not the 'third of a quartet').
- **size:** Exactly 1 per session (19/19). span=36 when child 0x2011 block_type=0x01 (size_field=29) — 13 sessions; span=37 when child block_type=0x02 (size_field=30) — 6 sessions. NO own payload before/after the child: child begins at parent_payload_start (z+9, gap=0) and ends exactly at the parent end (no trailing bytes).
- **fields:**
  - 0x201d payload = a single nested 0x2011 block and nothing else. Child header at z+9: `5A <child_block_type:1 of {0x01,0x02}> 00 <size:u32> 11 20`.
  - GEOMETRY LIVES IN THE CHILD 0x2011 PAYLOAD, at child_payload_start = cz+9: `+0 u32 = x (left), +4 u32 = y (top), +8 u32 = width, +12 u32 = height`. (NOT at parent_payload+9 as proposed.)
  - Observed values (19): x = 400 in ALL 19; y = 400 (16) or 402 (3); width in {92 (10), 98 (7), 102 (2)}; height in {125,134,163,293,329}. All rects taller than wide.
  - After the 16-byte rect: byte @cz+9+16 = 0x00, @cz+9+17 = 0x01 in all 19 (u16 LE @+16 = 0x0100). A fixed trailer, NOT the '0x0001 flag @+25' the proposal claimed. When child block_type=0x02, one extra trailing 0x00 (payload_len 19 vs 18), accounting for span=37 vs 36.
  - No trailing bytes on the 0x201d parent itself.
- **notes:** One narrow window (width clusters 92/98/102 px, taller than wide). x is always 400 (PT's default horizontal spawn origin) and y is 400 (or 402), so this window was essentially never repositioned — display/view-state PT writes and recomputes. CORRECTIONS from proposal: (1) all field offsets were off by +9 (measured from parent_payload+9 instead of the child 0x2011 payload start); (2) x=400 in 19/19, not 15/19; (3) the trailing flag is a fixed 0x00,0x01 pair (u16 0x0100), not '0x0001 @+25'; (4) it is the middle of a 5-block run, not the third of a quartet.
- **confidence:** HIGH for structure/count/parent/siblings/span and the corrected child-relative rect offsets (+0/+4/+8/+12, incl. both child-block-type variants). MEDIUM for the precise 'width/height of THIS specific window' semantics — labels strongly implied by value ranges but PT recomputes this view-state and no moved-window control exists in the corpus.

### 0x201e — Saved geometry for a wide/short floating window (last of the quartet)

- **kind:** CONTAINER. Exactly one immediate child, content-type 0x2011 (holds the rectangle). Parent: top-level (None) in all 19. Immediate top-level prev sibling = 0x201d, next = 0x201f in all 19. The parent has NO payload of its own before the child (child begins at parent_payload+0 = z+9; child content-type reads at z+16 = parent_payload+7).
- **size:** Exactly 1 per session (all 19 non-huge). Two byte-identical variants keyed by block_type: block_type=0x01 → span=36, size_field=29, child_payload=18 bytes, no parent trailing (13 sessions); block_type=0x02 → span=38, size_field=31, child_payload=19 bytes, +1 parent trailing byte 0x00 (6 sessions). Child block's block_type matches the parent's in every case.
- **fields:**
  - Parent block: `5A <bt:2> <size:4> <ct=0x201e:2>` then immediately the child 0x2011 block (no own prefix). Child content-type u16 @ z+16 (= parent_payload+7) = 0x2011 in all 19.
  - Child rect, offsets from CHILD payload start (= child_z+9): `+0 u32 = x` (400 when defaulted/centered, else real e.g. 1307/1344/45/163), `+4 u32 = y` (400 default, else 66/316/53/970), `+8 u32 = width` (574 in 12, 484 in 4 DWTS, 478 in 1 → 478..574), `+12 u32 = height` (86 in 18, 72 in 1 → 72..86).
  - After the 16-byte rect (child payload +16): the two bytes `00 01` in ALL 19. Read as u16 LE = 0x0100 (NOT 0x0001). Likely a position-saved / window-visible flag pair, but its exact meaning is UNVERIFIED (could be two u8 flags rather than one u16).
  - block_type=0x02 variant ONLY: child payload gains one extra 0x00 at +18 (child payload = rect + `00 01 00`), AND the parent gains one trailing 0x00 after the child block.
- **notes:** Fourth/last member of the 0x201b..0x201e quartet (0x201b ~623-647 wide big window; 0x201c always collapsed 0,0,0,0; 0x201d narrow-tall ~92-102 wide; 0x201e wide-short bar, width 478-574, height 72-86). The wide-short shape is consistent with PT's Transport window, but that identity is an INFERENCE, not proven. Height stays 72-86 while width and on-screen position vary widely — hallmark of a horizontal toolbar/Transport-style window. Display/view-state PT recomputes/rewrites; treat x/y/w/h/flags as advisory. CORRECTIONS from proposal: the flag is `00 01` (u16=0x0100), not 0x0001; and block_type=0x02 adds TWO bytes total (one in child payload, one parent trailing), not one.
- **confidence:** HIGH on structure (count, single 0x2011 child, top-level parent, 0x201d/0x201f neighbors, rect at child_payload +0/+4/+8/+12, the `00 01` pair, both block_type variants — exact across 19 sessions in ≥9 families). MEDIUM on the 'Transport window' semantic label.

### 0x201f — Saved geometry for one floating/tool window (with 16-byte tail)

- **kind:** CONTAINER. Own block_type=5. Exactly one immediate child (0x2011); no grandchildren. Parent = ROOT (always top-level). Verified 26/26.
- **size:** Header size field 45 (bt1) or 46 (bt2); payload 43/44; span 52/53. The +1 variance is driven ENTIRELY by the 0x2011 child's block_type (a session-wide format-version tag): child bt=1 → child size 20, child span 27, parent size 45; child bt=2 → child size 21, child span 28, parent size 46. Every 0x2011 in a given session shares one block_type (13/19 non-Hipsters bt1, 6/19 bt2; the 7 Hipsters all bt1).
- **fields:**
  - `+0..+8`: nested 0x2011 child header (`5A <bt:2> <size:4=0x14 bt1 / 0x15 bt2> 11 20`). child bt is a SESSION-WIDE format-version tag, not per-window.
  - GEOMETRY + FLAGS live INSIDE the 0x2011 child's declared payload (child_end = 27 for bt1, 28 for bt2):
  - `+9  u32 LE  x` — window left, px. Observed 0..1150 (live; corner interpretation ruled out since field3 < field1 in some sessions).
  - `+13 u32 LE  y` — window top, px. Observed 38..596 (live).
  - `+17 u32 LE  w` — window width, px. Observed 109..312 (width, not x2).
  - `+21 u32 LE  h` — window height, px. Observed 150..645 (height, not y2).
  - `+25 u8 flag1`: 0x00 (16/19) / 0x01 (3/19; the DWTS-SHOW trio). `+26 u8 flag2`: 0x01 constant (19/19). `+27 u8 flag3`: present ONLY for bt2, always 0x00 (6/6). These three are the child's trailing bytes.
  - PARENT TAIL (the 0x201f's own body): begins at parent payload +27 (bt1) / +28 (bt2), ALWAYS exactly 16 bytes. Per-byte across 19: [0]=0x01; [1]=0x00 (18/19) / 0x01 (1/19, Bianca); [2]=0x00 (constant, OMITTED by the proposal); [3]=0x01; [4]=0x04 (16/19) / 0x01 (3/19: Bianca, Let Her Go, Quality Time); [5..11]=0x01; [12] = 0x01/0x02 EQUAL TO the format-version (0x02 iff child bt=2); [13]=0x00; [14..15]=0x01.
- **notes:** Live display/view-state PT recomputes from the GUI (window position, default-open state) — NOT session content; several distinct projects share identical default geometries. Part of the fixed top-level window-layout band: order 0x201b 0x201c 0x201d 0x201e → 0x201f → 0x205e 0x205f 0x206a 0x209f 0x206c (19/19). CORRECTIONS from proposal: (1) tail is 16 bytes but its map must include byte [2]=0x00 constant (proposal omitted it); (2) tail[12] is NOT an independent enum — it equals the session format-version (== child bt), redundant with the +0 child block_type; (3) geometry (+9..+24) and the 3 flag bytes (+25..+27) sit INSIDE the child; the 16-byte tail is the parent's own body; (4) single-instance is 26/26 (Hipsters each have one, cbt=1, geom 1150,526,140,374).
- **confidence:** HIGH for structure/offsets/sizes/parent/child (26/26, geometry offsets, block_type coupling, 16-byte tail boundary — zero exceptions). LOW/unproven for the SEMANTICS of flag1 and the 16-byte tail toggles (tail[1], tail[4] vary session-to-session with no decoded meaning) — view-state PT recomputes.

### 0x205e — Fixed-width docked-strip window geometry (top-level singleton)

- **kind:** CONTAINER — exactly one immediate child, 0x2011 (19/19). Parent = ROOT (top-level). Previous top-level sibling is ALWAYS 0x201f, next is ALWAYS 0x205f (19/19) — a fixed 0x201f → 0x205e → 0x205f run. Own block_type = 2 (always).
- **size:** Outer size field = 33 (span 40) or 34 (span 41); payload 31 or 32. The +1 variance is driven by child_bt (the session-global window format tag): child_bt=1 → child_size 20, outer span 40 (13/19); child_bt=2 → child_size 21, outer span 41 (6/19). The extra byte is a trailing 0x00 appended at the END of the 0x2011 CHILD PAYLOAD, NOT in any header (child 0x2011 header is a fixed 9 bytes in all 19).
- **fields:**
  - Frame (19/19): data[z]=0x5A; block_type u16@z+1 = 2; size u32@z+3; content_type u16@z+7 = 0x205e; payload @z+9.
  - `payload+0..+8`: the child 0x2011 block HEADER — a FIXED 9 bytes: 5A, child_bt u16 (@z+10), child_size u32 (@z+11), child_ct u16 = 0x2011 (@z+16). child_bt is a per-session GLOBAL format tag shared by every 0x2011 in the file.
  - `payload+9  u32 x` — LIVE. Observed {0, 3, 8} (left edge, near 0).
  - `payload+13 u32 y` — LIVE. Observed {0, 37, 41} (top edge).
  - `payload+17 u32 w` — CONSTANT 785 (19/19).
  - `payload+21 u32 h` — CONSTANT 150 (19/19).
  - `payload+25 u8 = 0x00` (19/19).
  - `payload+26 u8 = 0x01` (19/19). Last byte of the child payload for child_bt=1.
  - `payload+27 u8 = 0x00` — present only for child_bt=2 (this trailing 0x00 IS the entire source of the +1 variance). For child_bt=1 this offset is already the first tail byte.
  - TAIL (after the child, last 4 bytes of the outer span): CONSTANT `01 32 00 00` (19/19). Reads as u8=1, u8=0x32(50), u16=0 — a fixed constant, meaning not independently determined.
- **notes:** Fixed-width docked strip/pane (w/h always 785×150; x,y live). The fixed dimensions strongly imply a dockable, non-resizable pane whose only live state is its (x,y) position — almost certainly editor/mixer display/view-state. HONESTY NOTE: no PT round-trip available in this pass, so 'PT recomputes on load' is inference from the frozen 785×150 dims + constant tail, not verified. Treat w/h and the tail as safe constants to copy verbatim; only x/y appear intended to be live. CORRECTIONS from proposal: (1) the child 0x2011 header is a FIXED 9 bytes (does NOT grow to 10) — the +1 variance is a trailing 0x00 inside the child PAYLOAD for child_bt=2; (2) child_bt is a session-GLOBAL tag, not a per-0x205e attribute.
- **confidence:** HIGH (19/19, ≥2 distinct families) for all byte offsets, the constants (w=785, h=150, tail `01 32 00 00`), ROOT parent, and 0x201f/0x205f neighbors. Geometry semantics (x/y/w/h) inferred from value ranges + fixed-dimensions pattern, not from a PT round-trip.

### 0x205f — Resizable-pane window geometry (top-level singleton)

- **kind:** CONTAINER. Own block_type=1. Exactly ONE immediate child, content_type 0x2011, whose span == the entire 0x205f payload (child.end == 0x205f.end, 26/26). Parent = ROOT (top-level). Root order invariably ...0x205e, 0x205f, 0x206a... (26/26).
- **size:** Two variants keyed by the child's block_type. child_bt=1 → header size field 29, payload 27, span 36. child_bt=2 → size 30, payload 28, span 37 (one extra trailing 0x00 byte, belonging to the child). The 0x205f block itself has NO tail: the 0x2011 child fills the whole payload.
- **fields:**
  - `+0..+8`: 0x2011 child header (`5A <child_bt:2> <child_size:4> 11 20`). child_bt is a SESSION-CONSISTENT UI-record-format flag (1 or 2), NOT per-window — verified equal to sibling 0x205e's child_bt in all 26; does NOT track the session's first-block block_type (always 1). Its value toggles the payload width by 1 byte.
  - `+9  u32 x` — window origin x. LIVE; observed {25,49,63,128,456,561}, range 25..561.
  - `+13 u32 y` — window origin y. LIVE; observed {95,106,315,502,614,691}, range 95..691.
  - `+17 u32 w` — window width. Two near-fixed values only: 778 or 805 (paired with h).
  - `+21 u32 h` — window height. 209 (with w=778) or 227 (with w=805).
  - `+25 u8 = 0` (invariant, 26/26).
  - `+26 u8 = 1` (invariant, 26/26).
  - `+27 u8 = 0` — present ONLY when child_bt=2 (this last byte is inside the child's payload).
  - NO tail on the 0x205f wrapper — the child occupies the entire payload, so x/y/w/h and +25/+26/+27 physically live INSIDE the child.
- **notes:** One resizable pane (~778×209 or 805×227). The wrapper carries no own fields beyond its single 0x2011 child. Live display/view-state PT recomputes and persists — treat geometry as advisory; a builder should copy a known-good donor rect rather than compute x/y. CORRECTIONS from proposal: (1) coverage is 26/26 (Hipsters bak.040-046 also match), not 19/19; (2) 'child_bt = session format tag' is directionally right but must be an inference — child_bt is session-wide (equals 0x205e's) and does NOT equal the session's first-block block_type; (3) the (w,h) split correlates loosely: (778,209) 17×, (805,227) 9×; child_bt=2 appears in 6 of the 9 (805,227) sessions but the two axes are independent flags.
- **confidence:** HIGH (26/26, 1 each) for structure and every offset/width/invariant (x,y,w,h,+25,+26,+27, size variants). The one INFERENCE is that (w,h)/x/y are live view-state PT recomputes; the 'window/pane geometry' label is inferred from the rect-shaped payload, so which specific pane is not established.

### 0x206a — List/browser dialog window record (geometry + selection index + config sub-block)

- **kind:** CONTAINER (exactly two immediate children in order: 0x2011 geometry, then 0x2074 config sub-block); parent = ROOT. Position fixed: always between top-level 0x205f (prev) and 0x209f (next), 19/19. Own block_type = 3.
- **size:** Header size field 219 or 220; span = size+7 (226/227); payload = span-9 (217/218). The +1 is driven entirely by the 0x2011 geometry child's format tag: geom bt1 → child size 20 → own size 219 (13/19); geom bt2 → child size 21 → own size 220 (6/19).
- **fields:** (offsets relative to payload start pay = z+9)
  - Frame: data[z]=0x5A; own block_type u16@z+1 = 3; size u32@z+3; content_type 0x206a u16@z+7; payload @z+9.
  - `+0..+8`: 0x2011 geometry child header (`5A <bt> 00 <size u32> 11 20`). Child bt = session format tag (1 or 2).
  - `+9  u32 x` (window left): observed {10, 63, 100, 102} → 10..102. LIVE.
  - `+13 u32 y` (window top): observed {46, 58, 100, 109} → 46..109. LIVE.
  - `+17 u32 w` (window width): observed {367, 372, 387}. LIVE.
  - `+21 u32 h` (window height): observed {80, 276} — TWO values (3/19 have h=80, 16/19 have 276). LIVE.
  - `+25 u8=0 ; +26 u8=0 ;` (`+27 u8=0`, present only when geom child bt=2). End of the 0x2011 child = pay + (27 if geom bt1 else 28); call this offset 'mid'.
  - `+mid+0`: u16 = 0x0000, then 3 bytes `01 01 00` (constant, 19/19). i.e. bytes at pay+mid = `00 00 01 01 00`.
  - `+mid+5`: u32 selection/scroll index. 0xFFFFFFFF (no selection) in 9/19; a small index in 10/19 with values {0, 19, 21, 22, 33} (21 appears 6×). VARIABLE and meaningful.
  - `+mid+9`: the 0x2074 config sub-block (`5A 01 00 AE 00 00 00 74 20 ...`), 181 bytes, BYTE-IDENTICAL across all 19. It nests a 0x2070 grandchild. Last thing in the payload (no trailing bytes).
- **notes:** A small list/browser dialog window record (roughly 372×276, or 367×80 in a few sessions) carrying a selected-item/scroll index. Geometry (x/y/w/h) and the selection index are live view-state PT recomputes; the trailing 0x2074 block (which itself contains a nested 0x2070) is a constant default preset PT always emits verbatim. CORRECTIONS from proposal: geometry ranges were too narrow (x 10..102 not 63..100; y 46..109; w includes 367) and h is 80-or-276, NOT near-fixed 276. The '0x2070' is a grandchild NESTED INSIDE 0x2074, not an alternate direct child of 0x206a.
- **confidence:** HIGH (19/19, one instance each) — framing invariant span=size+7; two-children order + ROOT parent + 0x205f/0x209f neighbors constant; geometry confirmed live (4 distinct x/y/w/h tuples); selection u32 confirmed variable; 0x2074 tail confirmed a constant preset.

### 0x2074 — Invariant default-configuration sub-record (wraps one 0x2070)

- **kind:** CONTAINER: exactly one immediate child, content_type 0x2070 (19/19), whose declared span exactly fills the 0x2074 payload. Parent content_type = 0x206a (19/19); never top-level (always nested under 0x206a). Own block_type = 1 (19/19).
- **size:** Fixed, zero variance across 19/19: header size field = 174, payload = 172 bytes, full span = 181. Own header (9 bytes) = `5A 01 00 AE 00 00 00 74 20`.
- **fields:**
  - CONSTANCY VERIFIED: the entire 0x2074 block (header+payload) is byte-identical across all 19 non-Hipster sessions (single SHA1). 16 of 19 are genuinely distinct sessions (DWTS SHOW appears as 3 identical copies). No variable fields exist.
  - `+0..+8` (payload): child 0x2070's 9-byte header = `5A 01 00 A5 00 00 00 70 20` (block_type=0x0001, size field=165, content_type=0x2070). FRAMING NOTE: size=165 gives the child a 172-byte span reaching the end of the 0x2074 payload; the child's OWN payload is 172-9 = 163 bytes (declared 165 and actual child-payload 163 are consistent, not a discrepancy).
  - `+9..end` (payload): the 163-byte 0x2070 child payload, also byte-identical 19/19. Non-zero byte map (child-payload-relative): starts `00 00 00 01 01 00` (bytes 3,4 = 01 01); a u32==8 at offset 7 and again at offset 19 (two u32=8 markers); a fixed 9-byte run `01 02 04 40 07 0a 05 41 80` at offsets 23..31; then sparse 01 markers (01 01 pairs at offsets 36 and 99, plus lone 01s at 39/42/45/100) and otherwise all zeros. Only 21 of 163 bytes are non-zero.
  - NEGATIVE RESULTS (verified by scan): no length-prefixed ASCII string and no GUID tag (`2A 00 00 00`) anywhere. No decodable per-session counts, offsets, or references. Opaque, fixed constant.
- **notes:** A byte-identical static template PT emits verbatim; carries no per-session data. Its parent 0x206a varies (9 distinct parent hashes) but this specific sub-record is a fixed constant embedded inside. There are exactly 3 total 0x2070 blocks per session; only the one under 0x2074 is in scope here (the invariant one). 'Default view/zoom preset' is an inference from context (nested under 0x206a in the view/preset region), not a decoded meaning.
- **confidence:** HIGH for structure, size, parent/child topology, and byte-constancy (19/19, independently re-derived from raw 0x5A framing). LOW for field SEMANTICS — because the block is a byte-identical static template, no schema can be induced (the u32=8 markers, the `01 02 04 40 07 0a 05 41 80` run, the trailing 01 markers cannot be attributed to any session variable).

### 0x209f — Fixed-default window/geometry record (top-level singleton)

- **kind:** CONTAINER with exactly one immediate child 0x2011 (itself a LEAF, no grandchildren). Parent = ROOT; at root level it sits directly between sibling 0x206a (prev) and 0x206c (next) in 19/19. Own block_type is CONSTANT 1.
- **size:** Header size field is 31 (13/19) or 32 (6/19). Formula: size = 9 (child 0x2011 header) + child_size + 2 (parent's own trailing `00 00`). child_size = 16 (geometry) + child_tail, where child_tail is 2 bytes (child bt=1 → child_size 20 → parent 31) or 3 bytes (child bt=2 → child_size 21 → parent 32). The 31/32 split is driven by the CHILD's block_type (1 vs 2 = the session-wide 0x2011 format tag). Block span = z+7+size (38 or 39). Parent block_type itself is always 1.
- **fields:** (offsets from the 0x209f PAYLOAD start = zmark+9; the 0x209f has NO own header fields before its child — the child begins at payload+0 and the geometry lives inside the CHILD's payload)
  - `payload+0..+8`: the 0x2011 child block header (`5A <child_bt:2> <child_size:4> 0x2011`). child_bt = session 0x2011 format tag (1 or 2). CONFIRMED 19/19.
  - `payload+9   u32 x = 300` (0x0000012C). CONSTANT 19/19. (== child-payload +0.)
  - `payload+13  u32 y = 300` (0x0000012C). CONSTANT 19/19.
  - `payload+17  u32 w = 200` (0x000000C8). CONSTANT 19/19.
  - `payload+21  u32 h = 400` (0x00000190). CONSTANT 19/19.
  - `payload+25  child tail bytes = 00 00` (child bt=1) OR `00 00 00` (child bt=2). CONSTANT-zero 19/19. Belong to the CHILD 0x2011.
  - parent own tail: the LAST 2 bytes of the 0x209f payload (at payload+27 for bt1-child / payload+28 for bt2-child) = `00 00`. CONSTANT 19/19.
- **notes:** Across the entire corpus the rect is CONSTANT (300,300,200,400) — a stored default placement rather than a user-moved window; this is view-state, not session data. CORRECTIONS from proposal: (1) the geometry (x,y,w,h) is owned by the CHILD 0x2011, not the parent 0x209f (parent contributes no fields before the child, only a trailing `00 00` after it); (2) the 31-vs-32 split is because the child's block_type (1 vs 2) makes the child tail 2 vs 3 bytes, not '+1 = child bt' as a counter; (3) parent block_type is a true CONSTANT 1; (4) the child 0x2011 is a leaf (no grandchildren).
- **confidence:** HIGH for structure and constants (19/19 distinct-project sessions). MEDIUM on the semantic claim that (300,300,200,400) is 'PT's default rect that PT recomputes' — the geometry is empirically invariant, but the corpus is too narrow (no session ever moved this window) to prove PT recomputes vs. merely persists a stored default. Treat as fixed view-state, not session data.

### Additional types — tiers 91+ (remaining real types)

### 0x206b — Editor/UI window geometry rect (view-state)

- **Kind:** CONTAINER — exactly one immediate child, a 0x2011 geometry LEAF that fills the entire parent payload (child_end == parent_end; parent has zero own trailing bytes, 19/19). Own block_type=1. Parent = ROOT/top-level (19/19).
- **Size:** Exactly 1 per session (19/19 non-Hipsters). span=36 (size_field=29) when the session-wide 0x2011 format tag is block_type=1 (13 sessions); span=37 (size_field=30) when block_type=2 (6 sessions). The +1 comes entirely from the child 0x2011 payload's trailing zero-pad run (3 bytes for bt=2 vs 2 for bt=1); parent header/layout otherwise identical.
- **Fields (offsets payload-relative = zmark+9):**
  - `+0..+8` (9 B): child 0x2011 block header — `5A`, child_bt u16@+1 (session-wide format tag, 1 or 2), child_size u32@+3 (20 for bt=1, 21 for bt=2), child content_type u16@+7 = 0x2011.
  - `+9` u32: window x (left). Values seen: 10, 63, 200, 202. (= child-payload +0)
  - `+13` u32: window y (top). Values seen: 46, 200, 209. (= child-payload +4)
  - `+17` u32: window width. Values seen: 115, 374, 420 — always a real size, never 1. (= child-payload +8)
  - `+21` u32: window height. Values seen: 73, 276. (= child-payload +12)
  - `+25..+26` (2 B): child 0x2011 payload trailing zero-pad; 0x00 in all 19. Meaning UNKNOWN — every observed value is 0, so cannot be shown to be semantic flags vs. plain padding (do NOT label as shown/expanded flags).
  - `+27` (1 B, present ONLY when child_bt=2): a third trailing 0x00 of the child payload; part of the same all-zero pad run, driven by the session-global bt=2 format tag, not a per-window field.
- **Notes:** Fixed member of an ordered top-level window family (0x206c, 0x258f, 0x259b, 0x259c, [0x206b], 0x2068, 0x2069, 0x2595) confirmed 19/19; constant top-level neighbors prev=0x259c, next=0x2068. Unlike siblings 0x2068/0x2069, 0x206b ALWAYS persists a real size regardless of open state (and its `+25` byte is always 0). Rect clusters: (200,200,420,276) ×8; (63,46,374,276) ×7; (202,209,115,73) ×3; (10,46,374,276) ×1 (Never Will Marry). View-state PT recomputes on load — a builder should copy a donor rect verbatim rather than compute it.
- **Confidence:** HIGH on structure and geometry offsets (single 0x2011 child, ROOT parent, neighbors, family run, one-per-session, own bt=1, x/y/w/h at payload +9/+13/+17/+21). LOW on the meaning of the trailing bytes at +25/+26/+27 (always 0x00, indistinguishable from padding). MEDIUM on which specific PT window this maps to (cannot be resolved from bytes).

### 0x2068 — Collapsible editor/UI window geometry (view-state)

- **Kind:** CONTAINER — exactly one immediate child, a 0x2011 geometry LEAF that fills the parent payload with zero parent-own trailing. Own block_type=1. Parent = ROOT/top-level (19/19). Consistent neighbors: prev sibling 0x206b, next sibling 0x2069 (19/19).
- **Size:** Exactly 1 per session (19/19). Keyed by the session-wide 0x2011 format tag (child_bt): child_bt=1 → size_field=29, span=36 (13 sessions); child_bt=2 → size_field=30, span=37 (6 sessions). The +1 span for child_bt=2 is a single trailing 0x00 inside the child payload. Same shape as siblings 0x206b/0x2069.
- **Fields (offsets payload-relative = zmark+9):**
  - `+0` (9 B): child 0x2011 header — `5A`@+0; child_bt u16@+1 (1 or 2, matches session-wide tag); child_size u32@+3 (20 for bt=1, 21 for bt=2); child content_type u16@+7 = 0x2011. Child payload begins at +9.
  - `+9` u32: window x (left) — live pixel coord; observed 53..871.
  - `+13` u32: window y (top) — live pixel coord; observed 34..621 (NOT limited to 34–57).
  - `+17` u32: window width — real when shown, else exactly 1 (collapsed).
  - `+21` u32: window height — real when shown, else exactly 1 (collapsed); when collapsed BOTH w and h are 1, never just one (15/19 collapsed).
  - `+25` u8: shown/expanded flag FOR THIS RECORD TYPE — =1 exactly when a real rect is stored (w,h≠1) [4/19]; =0 when collapsed to (x,y,1,1) [15/19]; perfect correlation within 0x2068. Record-type-specific, NOT a universal 'rect present' bit (sibling 0x206b always stores a real rect yet has this byte =0).
  - `+26` u8: secondary flag = 0 in all 19.
  - `+27` u8: trailing 0x00 — present ONLY when child_bt=2 (last byte of the child payload; child payload is 18 B for bt=1, 19 B for bt=2). Absent when child_bt=1.
- **Notes:** Same window-geometry family as siblings 0x206b/0x2069; 0x2068 (like 0x2069) collapses to (x,y,1,1) when hidden and stores a real rect when shown. When hidden, PT stores only the anchor (x,y) with w=h=1. Geometry lives in the child 0x2011 payload = parent-payload+9 = zmark+18. child_bt equals the session-wide 0x2011 block_type (each session uses one 0x2011 format). Do NOT read +25 as a global 'has-rect' bit (0x206b is a documented counterexample). View-state, not authored data.
- **Confidence:** HIGH on structure and on the +25 shown-flag ↔ geometry correlation WITHIN 0x2068 (4 real-rect flag=1 vs 15 collapsed flag=0, exact across 19). MEDIUM on the exact PT window identity and on generalizing +25 beyond 0x2068 (0x206b is a counterexample).

### 0x2069 — Collapsible editor/UI window geometry, sibling of 0x2068 (view-state)

- **Kind:** CONTAINER — exactly one immediate child, a 0x2011 geometry LEAF. Own block_type=1. Parent = ROOT/top-level (19/19). Root-level siblings consistent: prev=0x2068, next=0x2595 (19/19).
- **Size:** Exactly 1 per session (19/19). span=36 (child block_type=1, child payload 18 B, 13 sessions) or span=37 (child block_type=2, child payload 19 B, 6 sessions). Parent's own block_type=1 in all 19. No parent-own trailing (child begins at parent-payload+0 = z+9, child_end == parent_end).
- **Fields (offsets payload-relative = zmark+9):**
  - `+0` (9 B): child 0x2011 header — `5A`@+0; child block_type u16@+1 (1 → child_size 20; 2 → child_size 21); child_size u32@+3; content_type u16@+7 = 0x2011.
  - `+9` u32: window x (left). Observed {284, 672, 722, 739, 753, 802}.
  - `+13` u32: window y (top). Observed {38, 39, 84, 321}.
  - `+17` u32: window width. Real when shown, else 1. Observed real widths = 89 (2 instances).
  - `+21` u32: window height. Real when shown, else 1. Observed real heights = 502, 572.
  - `+25` u8: shown/expanded flag = FIRST trailing byte of the child 0x2011 payload (child-payload+16). =1 iff real rect stored (w≠1 or h≠1); =0 iff collapsed to (x,y,1,1). Perfect correlation: (0,collapsed)×17, (1,expanded)×2.
  - `+26` u8: second trailing byte = 0 in all 19 (reserved).
  - `+27` u8: third trailing byte, present ONLY when child block_type=2 (payload 21 B; 6/19), always 0. Tracks child block_type/payload length, NOT shown state — reserved/padding.
- **Notes:** Immediately adjacent to and structurally identical to sibling 0x2068. Stores a rect plus a shown/expanded flag; collapses to (x,y,1,1) when the window is not shown. The +25/+26/+27 bytes ARE the child 0x2011 payload's trailing bytes (child-payload+16/+17/+18), not separate parent fields. Cluster: raw block (x=284,y=84) appears 7× (6 collapsed to 1,1 + 1 expanded to 89×502); (753,321,1,1) appears 4×. PT-recomputed view/display state; do not treat as authored content.
- **Confidence:** HIGH on structure and on the +25 shown-flag correlation (upgraded from the 2-sample 0x2069 evidence by the corroborating expanded cases in the structurally identical sibling 0x2068). MEDIUM on the exact PT window identity.

### 0x258f — Default-anchor window pane (view-state)

- **Kind:** CONTAINER — exactly one immediate child 0x2011 (geometry LEAF) that fills the entire parent payload (zero parent-own trailing, 19/19). Own block_type=1. Parent = ROOT/top-level. Structural top-level neighbors: prev=0x206c, next=0x259b (19/19). NOTE: in the raw size-ordered block list the immediate index-neighbors are the 0x2011 geometry CHILDREN of the adjacent windows, not those windows themselves.
- **Size:** Exactly 1 per session (26/26, INCLUDING all 7 huge Hipsters sessions). True on-disk block length = own_size + 7 = 36 B (child_bt=1, child_size=20) or 37 B (child_bt=2, child_size=21). child_bt=1 vs 2 split = 20:6 over the full corpus (13:6 in the 19-session subset). No parent-own trailing. CONVENTION NOTE: `_raw_block_bounds` returns an `end` pointing AT the following block's 0x5A marker, so `e - z + 1` reads as 37/38 — the extra byte belongs to the next block.
- **Fields (offsets payload-relative = zmark+9):**
  - `+0` (9 B): child 0x2011 header (`5A`@+0; child block_type u16@+1 = 1 or 2; child size u32@+3 = 20 or 21; content_type u16@+7 = 0x2011). Verified 26/26.
  - `+9` u32: x = 45 in all 26 (fixed default window anchor X).
  - `+13` u32: y = 45 in all 26 (fixed default window anchor Y).
  - `+17` u32: width = 0 in all 26.
  - `+21` u32: height = 0 in all 26.
  - `+25` u8 = 0 in all 26 (constant flag; semantic unconfirmed).
  - `+26` u8 = 1 in all 26 (constant 'valid/present'-looking flag; semantic unconfirmed) — the LAST owned byte when child_bt=1 (length 36).
  - `+27` u8 = 0x00, present ONLY when child_bt=2 (extra child-payload byte, length 37); when child_bt=1 the 0x5A read there belongs to the NEXT block.
- **Notes:** The plain member of the 0x258f/0x259b pair — same 0x2011 child as 0x259b but WITHOUT 0x259b's ~24-byte trailer (60−36 = 61−37 = 24). Represents a never-user-resized / purely-flag window pane; geometry constant (45,45,0,0). Constant across the corpus, copyable verbatim; treat as PT-owned display/view-state.
- **Confidence:** HIGH on structure and byte layout: geometry (45,45,0,0) and flags (0,1) hold across all 26 sessions; single 0x2011 child, ROOT parent, 0x206c/0x259b neighbors, own bt=1, and every field offset/width reproduce exactly. MEDIUM on semantics: meaning of the two flag bytes and why child_bt differs is unproven (view-state PT owns).

### 0x259b — Window/panel anchor record with 24-byte config trailer (view-state)

- **Kind:** CONTAINER (own block_type=1) with exactly one immediate child (0x2011 geometry LEAF) followed by the parent's OWN 24-byte trailing config blob (not a child block). Parent = ROOT/top-level (19/19). Byte-adjacent siblings: prev=0x258f, next=0x259c (19/19) — the three form a contiguous panel-record group.
- **Size:** Exactly 1 per session (19/19). span=60 (size_field=53) in 13 sessions; span=61 (size_field=54) in 6 sessions. Own block_type=1 always. The +1 byte is the child 0x2011's container block_type: child_bt=1 (child_size=20) → span-60; child_bt=2 (child_size=21, one extra trailing null in the child payload) → span-61. child_bt is a session-WIDE format-version tag shared by 0x258f/0x259b/0x259c (bt=2 sessions: 4 DWTS files, MANOLITO, THE WIND).
- **Fields (offsets payload-relative = zmark+9):**
  - `+0` (9 B): child 0x2011 header — `5A`; child_bt u16@+1 (=1 span-60 / =2 span-61); child_size u32@+3 (=20 / =21); child_ct u16@+7 = 0x2011. Child payload begins at +9.
  - `+9` u32: x (geometry) = 45 (15/19) or 0 (4/19).
  - `+13` u32: y (geometry) = 45 (15/19) or 0 (4/19); (x,y) move together: (45,45) or (0,0).
  - `+17` u32: width = 0 in all 19.
  - `+21` u32: height = 0 in all 19.
  - `+25` u8 = 0 in all 19 (part of the child payload).
  - `+26` u8: valid-position/anchor flag = 1 when 0x259b's OWN (x,y)=(45,45) [15/19], = 0 when (0,0) [4/19] — perfect correlation with 0x259b's OWN geometry (NOT with 0x258f). Last byte of the child payload in span-60 (child_bt=1).
  - `+27` u8 = 0, present ONLY in span-61 (child_bt=2) as the child 0x2011's extra trailing null (child's last byte, not a parent field). In span-60, payload+27 is instead the first byte of the 24-byte trailer.
  - **trailer:** child block ends at payload+27 (span-60) or payload+28 (span-61); the parent's OWN 24-byte config blob begins there and runs to block end. BYTE-IDENTICAL across all 19: `00 01 00 01 01 00 00 01 01 01 01 03 00 00 00 01 00 00 00 00 05 00 00 00`.
- **Notes:** NOT the same anchor as 0x258f — 0x258f is (45,45,0,0) in ALL 19 and never takes the (0,0,0,0) variant; only 0x259b takes (0,0,0,0) (4 sessions: COGNAC, Let Her Go, Quality Time, THE WIND). The span-60/61 split is a session-wide container-format version tag, not part of 0x259b's config trailer. To synthesize, copy the whole block verbatim from a donor whose child_bt matches. Pure display/view-state PT recomputes.
- **Confidence:** HIGH on structure — 24-byte trailer byte-identical in all 19; geometry offsets, +25=0, +26 flag, the (45,45)/(0,0) split (15/4) and its correlation with own geometry, and the container shape all hold 19/19. MEDIUM-LOW on the MEANING of individual bytes inside the 24-byte blob (opaque constant; the '3,1,5' u32 reading is not cleanly aligned and is speculative).

### 0x242f — Zero-valued session/view-state scalar (LEAF)

- **Kind:** LEAF — no children (0/26 have any 0x5A child). Parent = ROOT/top-level (26/26). Own block_type=1 (26/26). Prev top-level sibling is always 0x202e. Next top-level sibling is 0x2030 (13/19) or 0x271a (6/19) — this split is NOT a property of 0x242f: next=0x271a exactly when the session contains a top-level 0x271a block (6 sessions); every session has a 0x2030.
- **Size:** Exactly 1 per session (26/26, incl. all 7 huge Hipsters). span=13, size_field=6, payload length=4 in all 26. size(6) = content_type(2 B @ z+7) + payload(4 B @ z+9). Framing verified: z+7+size == block end (z+13).
- **Fields (offset payload-relative = zmark+9):**
  - `+0` u32 = 0 in all 26 (constant zero scalar; payload hex `00000000` in every session, Hipsters included).
- **Notes:** Appears once per session immediately after the top-level 0x202e block. The next-sibling split tracks a session-wide feature (presence/absence of a top-level 0x271a block), not this block. Simplest of this cluster — a fixed 4-byte zero payload with no children. Safe to emit verbatim as a constant; do not infer a live counter.
- **Confidence:** HIGH on structure and constancy (payload u32=0, size=6, bt=1, LEAF with 0 children, ROOT parent, prev sibling 0x202e — all exact across 26 sessions, verified two ways: raw frame reads and interval-stack nesting). LOW on semantics — value is 0 in every session (incl. the largest), so the field's true meaning cannot be determined from this corpus.

### 0x206d — View-scale ladder pair (LEAF)

- **Kind:** LEAF (top-level body block, no enclosing 0x5A parent; 0 child frames). Parent=None and n_children=0 for all 26.
- **Size:** Fixed. size_field (u32@z+3) = 90 for all 26; block span = z..z+7+size = 97 bytes; payload (z+9 to end) = 88 bytes, laid out 4+40+4+40. block_type (u16@z+1) = 0x0003 invariant across all 26.
- **Fields (offsets payload-relative = zmark+9):**
  - `+0` u32: count1 = 5 (element count of array A; literally `05 00 00 00` in every instance).
  - `+4` f64[5]: array A — strictly-descending ladder, e.g. [3200, 1610, 270, 28, 0.0625]; last element always 0.0625 (=1/16) across all 26; DWTS family scales the whole array up, e.g. [819200, 51200, 1423.24, 224, 0.0625] (element [2] is a real fraction, so A is not all-integer).
  - `+44` u32: count2 = 5 (element count of array B; literally `05 00 00 00`).
  - `+48` f64[5]: array B — a second strictly-descending ladder, e.g. [139200, 70035, 11745, 1218, 2.71875]; last element is 2.7 (18/26) or 2.71875 (8/26). B is NOT a fixed scalar multiple of A: per-element B/A ratio is uniform (43.5) ONLY in the Reverse Rewire baks + Never Will Marry; in the other families the ratio varies element-by-element.
- **Notes:** Two parallel 5-element f64 arrays forming descending 'ladders' — consistent with an Edit-window horizontal-zoom / samples-per-view preset ladder plus a companion scale array. arrA[0] tracks timeline extent (DWTS long-timeline → 819200; typical music → 3200 or 8623), so PT recomputes this from the current view. Most of the ladder is a shipped default (arrA[1:] has only 2 distinct tails, arrB[1:] only 3) with the leading element(s) tracking the session. The PT-safe schema: two u32 counts of 5, two strictly-descending f64[5] arrays, 88-byte payload, block_type 0x0003, top-level LEAF, one per session. Do NOT hand-author exact values.
- **Confidence:** HIGH on structure (size / kind / children / parent / count fields / offsets / widths and the f64[5]+f64[5] layout identical 26/26). LOW on semantics — the labels 'samples-per-view' and 'companion scale' are INFERRED (no ground-truth mapping); the earlier 'arrB/arrA constant-ratio coupling' claim is FALSE for most sessions (only 8/26 show a constant 43.5).

### 0x208f — Fixed 21-byte constant tail-settings record (LEAF)

- **Kind:** LEAF, top-level body block. No block starts strictly inside any instance; every instance is top-level (parent content_type = None). Preceding top-level sibling is ALWAYS 0x1049; following sibling is 0x2302 or 0x209e (session-dependent). block_type = 0x0002.
- **Size:** Declared size u32@z+3 = 23 (0x17). Full block frame (z .. z+7+size) = 30 bytes. Payload (z+9 to end) = 21 bytes. 21/21 sessions byte-identical. Frame header byte-identical in every session: `5a 02 00 17 00 00 00 8f 20`. Exactly one instance per session.
- **Fields (offsets payload-relative = zmark+9):**
  - `+0..+20` (21 B): constant payload — `01 00 00 00 00` tiled ×4 then a trailing `01`, i.e. byte 0x01 at payload offsets 0, 5, 10, 15, 20 and 0x00 everywhere else. Single distinct payload across all sessions.
  - **MEANING UNVERIFIED:** the field WIDTHS/OFFSETS above are one self-consistent tiling of the constant payload, but because the payload NEVER varies there is NO evidence to distinguish '5× u8 enable-flag + 4× u32 value' from 'u32 count=1 then a fixed 17-byte body' from any other packing. Do NOT treat the flag/value/record-N labels as established — they are conjecture.
- **Notes:** Immediately follows the 0x1049 block in every session. Likely a view/window or global enable-state constant PT writes and recomputes, but this is a plausible guess only. Safe to emit the 21-byte payload verbatim when synthesizing; do not rely on any per-field semantic interpretation.
- **Confidence:** HIGH for existence, block_type=0x0002, size=23, 21-byte byte-identical payload, LEAF status, and placement after 0x1049 (verified across 19 non-Hipsters + 2 spot-checked Hipsters = 21 sessions). LOW for the internal field semantics — unconfirmable because the payload is invariant.

### 0x2302 — Count-prefixed list of 0x2301 (CONTAINER)

- **Kind:** CONTAINER of 0x2301 (immediate child content-type 0x2301 when non-empty); degenerates to a bare LEAF (count only) when count=0. Parent = NONE — top-level. block_type = 0x0001 (all sessions, incl. the huge Hipsters session). Followed by top-level sibling 0x230a; preceded by 0x209e or 0x208f.
- **Size:** Exactly one 0x2302 per session. block size(u32@z+3)=6 → 4-byte payload when empty (count=0): 18/20 sessions. size=38 → 36-byte payload when count=1: 2/20 sessions (COGNAC, THE WIND). Grows by one inline 0x2301 frame (32 bytes: 9 header + 23 payload) per element. (Denominator = 20 distinct sessions here.)
- **Fields (offsets payload-relative = zmark+9):**
  - `+0` u32: count = number of 0x2301 elements (0 → empty list, no further bytes; 1 → exactly one inline 0x5A frame follows). Only 0 and 1 observed.
  - `+4` (32 B, only when count≥1): inline 0x2301 block — standard `5A` frame with block_type=0x0001, size=25 (0x19), content_type=0x2301, then 23-byte payload. Present only in the 2 non-empty sessions; both byte-identical.
- **Notes:** A view/selection-state list PT populates on demand (recomputed display state, not authored data) — the two non-empty instances are byte-identical, looking like a fixed PT-generated default. The inline 0x2301 payload (23 B: `1300000000000d0000005a000100000006000100000000`) begins u32=19 (0x13), then `0000`, then u32=13 (0x0d), then bytes that resemble but do NOT cleanly parse as a nested 0x5A frame. 0x2301's interior is genuinely UNDER-SAMPLED (n=2, identical) and only partially decoded.
- **Confidence:** HIGH — the outer 0x2302 schema (count-prefixed list of 0x2301) is solidly confirmed across all 20 sessions. The 0x2301 element type is reported but its interior is not reliably field-mapped.

### 0x2305 — Always-empty count-prefixed list (LEAF in practice)

- **Kind:** LEAF in practice (count u32 = 0 → zero child frames in all 19 instances). Structurally a count-prefixed CONTAINER, parallel to the confirmed-non-empty 0x2302. Parent = None (top-level). block_type = 0x0001 (all 19). Positioned immediately after a 0x230b block and immediately before a 0x20a0 block in every session.
- **Size:** size field (u32@z+3) = 6; block span = 13 (span = 7 + size); payload = 4 bytes (payload len = size − 2). Frame byte-identical across all 19: `5a 01 00 06 00 00 00 05 23 00 00 00 00`. Exactly one per session.
- **Fields (offset payload-relative = zmark+9):**
  - `+0` u32: count = 0 in all 19 (empty list; when >0 this would be followed by that many inline child block frames, by analogy to the confirmed 0x2302 count-prefix behavior).
- **Notes:** Never observed non-empty, so its own semantics can't be disambiguated directly; but the adjacent 0x2302 (same block_type 0x0001, byte-identical empty frame) IS observed non-empty with a valid inline 0x2301 child, confirming the leading u32 is a count prefix and supporting 'empty count-prefixed container' over 'flag'. Likely display/view-state PT recomputes. When synthesizing, emit the whole frame `5a 01 00 06 00 00 00 05 23 00 00 00 00`.
- **Confidence:** HIGH on structure (size=6, span=13, payload 4 bytes, count=0, block_type=0x0001, LEAF/0 children, parent=None) verified across all 19 loadable non-huge sessions (~11 distinct projects). Container-vs-flag interpretation of the count prefix is supported indirectly via 0x2302.

### 0x1049 — Zeroed count-prefixed tail-settings state (LEAF)

- **Kind:** LEAF (no child frames; count/value=0). Parent = None (top-level; not nested in any session). block_type (u16@z+1) = 0x0002 (26/26, distinguishing it from the nearby 0x2305 which is block_type 0x0001).
- **Size:** declared size u32@z+3 = 6 in all 26; payload = size−2 = 4 bytes; FULL FRAME SPAN = 13 bytes = [z, z+13] (9-byte header: `5A` + block_type u16 + size u32 + content_type u16, then 4-byte payload). Exactly one instance per session.
- **Fields (offset payload-relative = zmark+9):**
  - `+0` u32: count/value = 0 in all 26 sessions (payload `00 00 00 00`).
- **Notes:** In the tail settings/view-state region. 0x208f is the EXACT immediate successor (+1) in all 26; the immediate predecessor is 0x258e or 0x2083 (NOT 0x2510, which is ~8–9 blocks upstream), and 0x2305 is ~52–53 blocks downstream. A LEAF placeholder — either an empty list (count=0) or a zeroed scalar setting; the two cannot be distinguished because the value is never non-zero. Consistent with PT-emitted session view/preferences state PT recomputes.
- **Confidence:** HIGH on structure (verified 26/26: one per session, size=6/payload 4/span 13, block_type=0x0002, LEAF, top-level, payload all-zero). Semantics of the u32 unconfirmed (only its always-zero value is); 'empty-list vs zeroed-scalar' ambiguity is retained honestly.

### 0x2550 — Fixed all-zero head-of-tail-cluster default (LEAF)

- **Kind:** LEAF (no 0x5A child frames; 0 immediate children in all 26). block_type u16@z+1 = 0x0001 (26/26). Top-level: no enclosing 0x5A block (26/26). Immediately preceded by a top-level 0x2030 (end==z, 26/26) and immediately followed by the top-level 0x2597 view-settings block (start==e, 26/26).
- **Size:** Fixed. Declared size u32@z+3 = 9 (26/26). Total frame span = 7 + size = 16 bytes. Payload = span − 9 = 7 bytes (unusual odd length). Full frame always: `5a 01 00 09 00 00 00 50 25 00 00 00 00 00 00 00`. Exactly one instance per session.
- **Fields (offset payload-relative = zmark+9):**
  - `+0..+6` (7 B): all zero — `00 00 00 00 00 00 00` in ALL 26 sessions. No non-zero field ever observed, so internal field boundaries are UNRESOLVABLE from static data. Report as a 7-byte fixed default, not a decoded schema (could be 7 packed flag bytes, or u32+3 reserved, etc. — indeterminate).
- **Notes:** Sits at the head of the tail view/display-state cluster, immediately before 0x2597. Because the payload is all-zero everywhere, emit as 7 zero bytes when synthesizing. The view-state role is INFERRED from placement next to documented view-state blocks (0x2597/0x2588/0x258c), NOT directly proven for 0x2550 itself.
- **Confidence:** HIGH that it is a fixed 32... — HIGH that it is a fixed 7-byte all-zero, block_type=0x0001, span-16 record occurring once per session (verified 26/26 across ≥2 projects). LOW on any internal field meaning (undeterminable because every instance is all-zero).

### 0x20a0 — Timeline/edit view-state root container

- **Kind:** CONTAINER (exactly 1 immediate child, content_type 0x20a1, fully nested). Parent = ROOT (top-level; the preceding on-disk block is an unrelated 0x2305 size-6 block, not a parent). block_type=0x0001.
- **Size:** Fixed. Header size field (u32@z+3) = 0x41 = 65; that 65 counts the content_type word (2) + payload (63), so the actual parent payload is 63 bytes and the whole block span is [zmark, zmark+72]. Exactly 1 instance per session (19/19 non-Hipsters).
- **Fields (offsets payload-relative = zmark+9):**
  - `+0` u16: per-session view-state scalar (NOT constant). Observed: 0x0000 (6 sessions), plus 0xd53c, 0xd61c, 0x6a00, 0x5c00, 0xd54c (×2), 0x2200 (13 sessions nonzero total). Independent of the preceding block. Meaning undecoded; likely a horizontal scroll/zoom position or edit-cursor sub-value.
  - `+2 .. +62`: embedded child block 0x20a1 begins at zmark+11. Child header = `5A 01 00 | 36 00 00 00 | A1 20` (block_type=0x0001, size=0x36=54, content_type=0x20a1). Child span = [zmark+11, zmark+72] (consumes the rest of the parent). Child payload (52 bytes) is a run of little-endian u16 view-state values (e.g. [1024,1024,256,0,…,256,1]); several vary per session and are also opaque view state.
- **Notes:** Root container for session-level timeline/edit view-state (zoom/scroll/selection). Holds a small 2-byte scalar of its own, then wraps exactly one 0x20a1 leaf that carries the bulk of the view/selection state. The earlier proposal's claim that the 2 bytes after the ct word are 'always 00 00' is FALSE — 00 00 in only 6/19, nonzero per-session u16 in 13/19. Display/view state PT recomputes; do NOT rely on the inner values being fixed.
- **Confidence:** MEDIUM. Structure/offsets/child are rock-solid 19/19 (block_type=0x0001; size field=0x41; 63-byte payload; exactly 1 child of type 0x20a1 that nests fully; parent=ROOT). Downgraded because the +0 scalar and the child's u16 run are undecoded view state PT recomputes.

### 0x20a1 — Timeline selection/range + view params LEAF

- **Kind:** LEAF (no children in any of the 19). Parent = 0x20a0 (19/19). block_type u16@z+1 = 0x0001.
- **Size:** Fixed: size field u32@z+3 = 54 (0x36) in all 19; payload region data[z+9:e] = 52 bytes in all 19.
- **Fields (offsets payload-relative = zmark+9):**
  - `+0` u16 = 1024 (0x0400) in 18/19; 768 (0x0300) once (Bianca). Byte +0 itself always 0x00; variation is in byte +1. View/cache resolution constant.
  - `+2` u16 = 1024 in all 19: second resolution/scale constant.
  - `+4` u32: view width / samples-per-pixel-ish. 256 in 14/19, 537088 in DWTS family (4), 1024 in The Wind (1). Low byte +4 is ALWAYS 0x00; varying bytes are +5/+6.
  - `+8` u8 = 0x00 (const).
  - `+9` u8: flag = 0 or 1. ==1 EXACTLY when the +34/+42 range is nonzero (perfect correlation, 19/19: 9 ones, 10 zeros). Range-populated flag.
  - `+10..+18` (9 B): constant `00 02 00 01 00 01 00 01 00`.
  - `+19` u8 = 3 in 17/19; 4 in exactly 2 (Let Her Go, Quality Time). Small view-mode enum.
  - `+20..+33` (14 B): constant `00 00 00 00 00 02 00 00 00 03 00 00 00 00`.
  - `+34` u32: timeline range LOW / selection start (0 when unset, 10/19). Recurs byte-identically across unrelated sessions: 3906250000 (×4: Bianca, Cognac, Let Her Go, Quality Time), 3937701250 (×4 DWTS), 3906295000 (×1 The Wind). Bytes +38..+41 always 0. A PT default timeline position in internal rational units (does not divide evenly by 44100/48000), NOT a content-derived sample count.
  - `+38` u32 = 0x00000000 (const).
  - `+42` u32: timeline range HIGH / selection end. ALWAYS EQUAL to +34 (start==end, zero-length selection), 19/19.
  - `+46..+51` (6 B): fixed tail `00 00 00 01 01 00` in all 19 (i.e. +46/+47/+48 = 00, +49 = 0x01, +50 = 0x01, +51 = 0x00).
- **Notes:** Stores a saved timeline selection/range (a start==end pair, i.e. an empty/zero-length selection) plus fixed view/resolution params. Both the range and the +9 flag are 0 when the session was never given this state → display/view state PT restores or recomputes. Varying payload offsets are exactly +1, +5, +6, +9, +19 and the two u32 range fields; every other byte is constant.
- **Confidence:** HIGH for structure/constancy (count, 1-per-session, size 54, payload 52, block_type 0x0001, parent always 0x20a0, no children — all 19/19; +34==+42 within every session). Semantic labels (view resolution, selection range, view-mode enum) remain interpretive but the display-state / PT-recomputed framing is well supported (the +34 value recurs byte-identically across unrelated projects and does not divide cleanly by any sample rate).

### 0x20a2 — Fixed-size on-screen element geometry (view-state)

- **Kind:** CONTAINER, parent = ROOT (parent content_type = None, 19/19). Exactly ONE immediate child: 0x2011 (19/19). No other children. Parent block_type = 0x0008 (19/19).
- **Size:** Parent size (u32 header field) = 40 in 13/19 sessions, 41 in 6/19. The 40-vs-41 split is driven entirely by the CHILD 0x2011's block_type: child bt 0x0001 → 18-byte child payload → parent size 40; child bt 0x0002 → 19-byte child payload → parent size 41 (child trailing pad = child_bt + 1). Parent block_type is 0x0008 regardless. 1 per session, 19/19 (7 Hipsters excluded as huge).
- **Fields (offsets payload-relative = zmark+9):**
  - `+0` (payload start): 0 own pre-child bytes. The child 0x2011 header begins immediately: `5A <child_bt:u16> <child_size:u32> 11 20` (9 B). child_bt = 0x0001 or 0x0002; child ct = 0x2011.
  - `+9` u32: 'left' (varies per session; e.g. 891, 553, 437, 25, 402, 802) — INFERRED position X.
  - `+13` u32: 'top' (varies; e.g. 673, 381, 273, 500, 79, 38) — INFERRED position Y.
  - `+17` u32: width = 549 in ALL 19 — constant (INFERRED size W).
  - `+21` u32: height = 100 in ALL 19 — constant (INFERRED size H).
  - `+25`: child trailing pad — 2 zero bytes (child_bt=1) or 3 (child_bt=2); all-zero in 19/19. Ends the 0x2011 child block.
  - **PARENT TRAILING** (11 bytes, at payload +27 for size-40 / +28 for size-41): byte[0] enum in {2,3,4} (varies); byte[1]=0x00; byte[2] enum in {1,5,6} (varies); byte[3]=0x00; byte[4]=0x00; byte[5]=0x01 (tag); byte[6] value in {0x17,0x2b,0x4b} (varies); byte[7]=0x00; byte[8]=0x01 (tag); byte[9] value in {0x00,0x08,0x33,0x3c,0x4b} (varies); byte[10]=0x00. Reads as two '01 <val> 00' tagged bytes (5–7, 8–10) preceded by two small enums (bytes 0 and 2). Meaning UNCONFIRMED (plausibly scroll/zoom or a secondary pane extent).
- **Notes:** One small top-level block per session wrapping a single 0x2011 rectangle plus 11 trailing enum/value bytes. Rectangle's 3rd/4th u32 constant (549, 100) while 1st/2nd vary per session → a fixed-SIZE on-screen element whose POSITION PT saves. left/top labels are INFERRED (could be top,left). PT recomputes on layout; NOT structural session data.
- **Confidence:** MEDIUM (block is display/view-state PT recomputes; the trailing enum/value semantics are unconfirmed). Structure CONFIRMED 19/19: 1-per-session, parent block_type 0x0008, single 0x2011 child at ROOT, width=549 & height=100 constant while left/top move.

### 0x252a — Saved window/dialog geometry, resizable (view-state)

- **Kind:** CONTAINER (single child: 0x2011); no own pre-child bytes and no trailing parent bytes (child begins at zmark+9 and child_end == block_end); parent = ROOT (top-level). Outer block_type = 0x0001 (19/19).
- **Size:** 36-byte block (outer size field 29) OR 37-byte block (outer size field 30). The 29-vs-30 split correlates EXACTLY with the child 0x2011's own block_type: child bt 0x0001 → 18-byte payload → 2-byte '00 00' tail → outer size 29 (13/19); child bt 0x0002 → 19-byte payload → 3-byte '00 00 00' tail → outer size 30 (6/19). 1 per session, 19/19 across 6 distinct projects.
- **Fields (offsets payload-relative = zmark+9):**
  - `+0` (own header/pre-child): NONE. Outer frame is `5A | block_type u16=0x0001 | size u32 (29 or 30) | content_type u16=0x252A`, then the 0x2011 child header begins immediately at zmark+9 (`5A | child_block_type u16 (0x0001 or 0x0002) | csize u32 | 11 20`).
  - child 0x2011 payload (starts at child_zmark+9): `+0` u32 left, `+4` u32 top, `+8` u32 width, `+12` u32 height. Confirmed width/height (NOT absolute right/bottom): field3 < left in 6 instances (e.g. L=735 w=304), and field4 values of 29/32/55 are far too small to be absolute bottom coords given top=500.
  - child 0x2011 tail after the 16-byte rectangle: '00 00' (2 B, child bt 0x0001) or '00 00 00' (3 B, child bt 0x0002). All-zero in 19/19.
  - no parent trailing bytes: child_end == block_end (19/19).
- **Notes:** A second, variably-sized saved window distinct from 0x20a2. Both width and height vary here (width ∈ {304,250,200,272}; height ∈ {236,29,32,200,55}) → a resizable window, whereas 0x20a2 locks width/height at 549/100. Recurring default corner [25,500,…] seen in 11/19, matching the same default-corner family as 0x20a2. PT-recomputed UI position; treat the rectangle as an opaque saved-geometry blob.
- **Confidence:** HIGH. All structural claims hold 19/19 (exactly 1 per session, outer block_type 0x0001, parent=ROOT, single 0x2011 child, no pre-child or trailing bytes; rect layout left/top/width/height). Verified across 6 distinct projects.

### 0x2428 — Session-level cross-reference / dependency link table

- **Kind:** CONTAINER, parent = ROOT. Single child 0x1054; 0x1054 → N × 0x1052; each 0x1052 → M × 0x1050 (variable M, e.g. 2,3,8,72,79,169); each 0x1050 wraps exactly one 0x104f leaf. block_type of 0x2428 and 0x1054 = 0x0001. block_type of 0x1052 = 0x0002 OR 0x0003 (0x0002 in Reverse/Bianca, 0x0003 in DWTS). 0x1050/0x104f come in two size variants (see size).
- **Size:** Reported using the block SIZE field (payload+2). Exactly 1 per session, 19/19. Empty = size 15 (total span 22 bytes): child 0x1054 size 6 with a single count=0 u32. Non-empty range: 308 (Reverse, 2 entries) up to 45645 (DWTS, 362 entries; Bianca 25698 / 15 entries). block_type 0x0001. Leaf sizes NOT fixed: SMALL variant 0x1050 size=44 wrapping 0x104f size=34 (Reverse/Bianca; 0x1050 btype 0x0001, 0x104f btype 0x0008); LARGE variant 0x1050 size=48 wrapping 0x104f size=37 (DWTS; 0x1050 btype 0x0002, 0x104f btype 0x000a).
- **Fields:**
  - child 0x1054 payload `+0` u32 = ENTRY COUNT; VERIFIED == number of 0x1052 sub-blocks in every session (0,2,4,15,362). 0 ⇒ empty. The 0x2428 block itself has no scalar fields of its own; its payload IS the 0x1054 child.
  - each 0x1052 payload `+0` u32 = 1 in every record (a fixed sub-count/version marker), then M × 0x1050 sub-blocks.
  - each 0x1050 wraps exactly one 0x104f leaf and carries no scalar fields of its own beyond that child.
  - 0x104f leaf payload (offsets from z+9; 32 B small / 35 B large variant): `+0` u16 usually 0 (0 in 2761/2879, 1 in 118 — NOT a constant); `+2` u32 object index/id (822 distinct, range 1..4739); `+6` 8 bytes object HANDLE/ADDRESS, not a unique GUID (only 456 distinct across 2879 records, heavily reused; no 2A-00-00-00 GUID tag precedes it); `+14` u16 small enum, 6 values {0x100,0x140,0x200,0x240,0x300,0x340}; `+16` u16 mostly 0xfffe (2494/2879) but 0x0001 in 381 and rarely 0x0003/0x0005/0x0007 — NOT constant; `+18` u32 = 0 (0 in 2876, 1 in 3); `+22` 8 bytes = `0xFFFFFFFFFFFFFFFF` null-link sentinel, CONSTANT in all 2879; `+30` u16 = 0. SMALL variant ends at +32. LARGE (0x000a) variant has 3 EXTRA tail bytes at +32 that VARY (00-00-00 ×1578, 00-01-00 ×468, 00-02-00 ×48).
- **Notes:** Root container holding a length-counted list of N binary link/cross-reference records; no printable strings. Entry count scales with session edit/clip history (0..362 across corpus), 0 in lightly-edited sessions → a session-wide dependency/link table, not a per-track structure. The 0x1054/0x1052/0x1050/0x104f family is PT's generic nested list/record container. Only the +22 8-byte 0xFF sentinel is a true constant; many earlier 'constant' claims are only majority values. Per-record semantics (what each +2 index / +6 handle points at, what +14/+16 select) are NOT pinned; may be internal edit-history/undo or object-linking state.
- **Confidence:** MEDIUM. Structure (container nesting, 1-per-session, count==#0x1052, empty=count-0, size scaling) is CONFIRMED byte-exactly across the corpus (≥3 projects, 19 files, 2879 leaf records). Semantics are inferential.

### 0x2508 — Network/host-identity record (name + IPv4)

- **Kind:** LEAF (no children, 19/19). block_type 0x0007 (19/19). Top-level / parent = ROOT (19/19).
- **Size:** Two forms. NAMED form: block span 62 bytes = 7-byte header + declared_size 55 (payload 53); occurs 12/19. EMPTY-NAME form: span 55 = header + declared_size 48 (payload 46); occurs 7/19 (Reverse Rewire family). The 7-byte delta is exactly 'name-string len 7 + "Default" (11 bytes)' vs 'name-string len 0 (4 bytes)'. Exactly 1 per session, 19/19, ≥10 distinct projects.
- **Fields (offsets payload-relative = zmark+9):**
  - `+0` u32 = 28283 (0x6e7b): constant head magic/type id (19/19).
  - `+4` u32 = 28282 (0x6e7a): constant head magic (19/19).
  - `+8` u32 = 0x00000000: const (19/19).
  - `+12` u32 = 0xffffffff: null sentinel, const (19/19).
  - `+16` u16 = 0x0000: const (19/19).
  - `+18` (8 B): opaque field. All-zero in 4 sessions (DWTS family, Manolito, The Wind, DWTS-155); a non-zero pointer/handle-like value elsewhere (e.g. Reverse `80 fd 49 0c 28 03 f4 0c`). NOT a GUID (no `2A 00 00 00` tag anywhere, 19/19).
  - `+26`: length-prefixed string (u32 len, then len ASCII). PRESENT IN BOTH FORMS. NAMED: len=7 → 'Default' (bytes +30..+36). EMPTY-NAME (Reverse): len=0 → no bytes. This length prefix is what makes the block 7 bytes shorter in the Reverse family; the field is never actually absent.
  - after string (+37 named / +30 empty), 4 bytes: IPv4 ADDRESS. Valid private/loopback/link-local IPv4 in every non-zero instance (192.168.1.161, 192.168.1.56 ×2, 192.168.1.209, 172.22.89.181 ×3, 169.254.221.48, 127.0.0.1, 10.0.0.13 ×3). The only all-zero post-string IP is the Reverse empty-name form.
  - trailing bytes after the IPv4: ~10–12 varying bytes (per-session; e.g. '00 ff ff ff ff 00', '30 d9 52 d1', '28 e9 1d 00'), NOT constant. ONLY the final 2 bytes (`00 01`) are byte-constant across all 19.
- **Notes:** A length-prefixed node/host name (literally 'Default' in 12/19) plus an embedded IPv4 that decodes as a valid address in EVERY non-zero instance → almost certainly a NETWORK/HOST IDENTITY record (host name + machine IP, e.g. a WAN/collaboration/'last saved by' node). The literal 'Default' is the host/node name, not a settings preset. The earlier 'constant 8-byte tail' claim is FALSE (only last 2 bytes constant).
- **Confidence:** MEDIUM. Header (+0..+17) + length-prefixed name at +26 + the IPv4 immediately after are the stable, high-confidence parts, verified 19/19 across ≥10 projects. Downgraded because the precise semantics of +18 (8 bytes) and the post-IP trailing bytes are session-specific and not pinned; the network-identity role itself is a strong inference, not RE-proven.

### 0x25c0 — Network/hardware peripheral node record

- **Kind:** CONTAINER (exactly one child block 0x2509); top-level, no parent. Outer block_type is always 2.
- **Size:** payload_len = size−2. Two shapes: payload 49 / size 51 / span 58 (child 0x2509 block_type=2), or payload 53 / size 55 / span 62 (child 0x2509 block_type=3). One instance per session: 26/26 (17 of the len-49 shape, 9 of the len-53 shape).
- **Fields (offsets payload-relative = zmark+9):**
  - `+0` u8: flag/count = 0x01 (constant across all 26).
  - `+1..+4` (4 B): IPv4 address in dotted-quad byte order (big-endian). Varies exactly as IP octets across 6 distinct values from ≥6 families: 192.168.1.160, 192.168.0.18, 192.168.1.56, 192.168.1.161, 10.0.0.13, 127.0.0.1. Cross-checked: the identical 4-byte IP appears in the adjacent 0x2508 record right after the ASCII 'Default' string.
  - `+5..+8` (4 B): tag = `6c 72 00 00` ('lr\x00\x00'), constant across all 26.
  - `+9 ..`: embedded child block 0x2509 whose 0x5A frame begins exactly at payload+9. Child span = 7+child_size = 30 bytes (child bt=2) or 34 bytes (child bt=3). Child bytes fully constant within each variant, contain no IP data. No GUID (no `2A 00 00 00` tag).
  - **trailer** (bytes after the child to end of span): 10 bytes `00 00 00 00 00 01 01 00 00 00` (constant across all 26).
- **Notes:** Identifies one I/O peripheral by its IPv4 plus a constant 4-byte tag. Lives in the settings tail, immediately after the 0x2508 peripheral record and adjacent to the 0x2107 I/O-setup block. The 'block_type 2 vs 3' variation is on the CHILD 0x2509, NOT the outer 0x25c0 (outer is ALWAYS 2). The payload-49-vs-53 split tracks the child block_type/size. IP decode is unambiguous (private-LAN + loopback varying per machine).
- **Confidence:** HIGH. Verified byte-exact across all 26 corpus sessions (incl. 7 Hipsters). content_type 0x25c0, top-level/no-parent, single 0x2509 child, one-per-session, +0=0x01, IPv4 at +1..+4 (corroborated by the matching IP in 0x2508), constant 'lr' tag at +5..+8, child frame at payload+9, span 58/62, 10-byte constant trailer.

### 0x2509 — Peripheral/network peer connection sub-record

- **Kind:** LEAF (no children, 19/19). Parent always 0x25c0 (19/19), itself a top-level block.
- **Size:** payload_len 21 (block_type=2) or 25 (block_type=3). 19 instances / 19 distinct sessions (one per session). block_type 2: 10 inst; block_type 3: 9 inst.
- **Fields (offsets payload-relative = zmark+9):**
  - `+0` u32 = 0 (constant, reserved).
  - `+4` u32 = 0 (constant, reserved).
  - `+8` u32 = 0xFFFFFFFF (−1 'unset ID' sentinel, constant across all 19).
  - `+12` u8 = 0 (constant).
  - `+13` u32 = 28283 (0x00006e7b) in bt=2, 0 in bt=3 — plausibly a peer port, NOT a channel count.
  - `+17` u32 = 28282 (0x00006e7a) in bt=2, 0 in bt=3 — plausibly a peer port, NOT a latency count.
  - bt=3 adds 4 trailing zero bytes at +21..+24 (payload 25 vs 21); always `00000000`.
- **Notes:** The sole child of a top-level 0x25c0 node record whose payload begins with an IPv4. Holds a −1 'unset ID' sentinel plus, for a real LAN peer (bt=2), a near-equal u32 pair (28283/28282 — most plausibly two port numbers, also valid as u16 ports). Zeroed for loopback/unreachable nodes (bt=3). The bt=2-vs-bt=3 split correlates 1:1 with reachability: LAN peers (192.168.x) ⇒ bt=2 with non-zero pair; loopback 127.0.0.1 and 10.0.0.13 ⇒ bt=3 with the pair zeroed. Looks like Satellite Link / network-peer state PT recomputes from the host environment. CAVEAT: the bt=2 pair is observed-constant only because 8 of 10 bt=2 instances are backups of a single project — treat 28283/28282 as this user's setup, not a proven fixed default.
- **Confidence:** MEDIUM-HIGH (byte layout HIGH; semantics LOW). Framing/offsets verified byte-exact 19/19; the +0/+4 reserved zeros, +8 sentinel, +12=0, and bt=3 zero-pad are CERTAIN. The +13/+17 port interpretation and the constancy of the pair are INFERRED, not proven.

### 0x2530 — Grid/Nudge time-value display cache (edit-prefs, LEAF)

- **Kind:** LEAF (block_type u16@z+1 == 0x0001; inline length-prefixed ASCII strings; ZERO child 0x5A blocks). Top-level: no parent in 26/26. Preceded by 0x25c0 (20/26) or 0x271f (6/26), always followed by 0x2516.
- **Size:** FIXED. header size field u32@z+3 == 289; payload_len == 287; full span == 296 (= z .. z+7+289). Exactly one per session (26/26, Hipsters INCLUDED). Every string length is constant across all sessions → the whole block is a fixed 287-byte layout.
- **Fields (offsets payload-relative = zmark+9):**
  - `+0` u32 = 3 (record count). CONSTANT 26/26.
  - `+4` u8: per-record display/unit format enum for record 0 (values {0x04:19, 0x00:7}); correlates with whether record0's Bars|Beats|Ticks reads whole-beats ('0| 1| 000') vs sub-beat ticks ('0| 0| 240').
  - `+5..+6` = `00 00` (const). `+7..+10` u32 = 3 (group/record marker, const).
  - `+11` u8: format enum (record 1; {0x00:25, 0x01:1}). `+12` u8: format enum ({0x01:17, 0x00:9}), tracks record0/1 beat-vs-tick display. `+13` = 01 const. `+14..+17` u32=3. `+18..+20` =00. `+21..+24` u32=3. (Header is a fixed 25-byte region: count + three '03 00 00 00' group markers interleaved with per-record format-enum bytes 4/11/12.)
  - **RECORD region** (3 records, fixed 82 bytes each): rec0 @+25, rec1 @+107, rec2 @+189. Each record = u32 marker (== 5) then five length-prefixed ASCII strings with CONSTANT lengths [12,10,14,11,11]: Bars|Beats|Ticks(12), Min:Secs(10), Timecode(14), Feet+Frames(11), Samples(11).
    - rec0 (@+25) VARIES: Samples ∈ {'100','1000','10000'}; B|B|T ∈ {'0| 1| 000','0| 0| 240'} etc.
    - rec1 (@+107) VARIES: Samples almost always '10000' (25/26; one session '100').
    - rec2 (@+189) CONSTANT 26/26: '   2| 0| 000' / '  0:05.000' / '00:00:05:00.00' / '   20+00.00' / '     960000' (2 bars / 5.000s / 960000 samples).
  - **TRAILER** @+271, 16 bytes, byte-identical 26/26: `03 00 00 00 FF FF FF FF 00 00 00 00 FF FF FF FF` (u32=3 then two −1 sentinels separated by a 00000000).
- **Notes:** THREE time-position VALUE records, each pre-rendered as fixed-width cached display strings in all five rulers. Almost certainly the session's Grid / Nudge value set — rec0/rec1 vary per session with the user's grid/nudge amount, rec2 is a hardcoded 5s/960000 default. The display STRINGS are caches PT reformats, but the LAYOUT is a RIGID fixed-offset schema. The grid/nudge role is a reasonable inference, not provable from bytes.
- **Confidence:** HIGH on structure (one per session; block_type 0x0001; size 289/payload 287/span 296; LEAF; top-level; 3 records × 5 fixed-width ruler strings + 16-byte trailer; markers count=3, per-record=5; trailer byte-identical) — verified 26/26. The Samples column varies over {100,1000,10000}; note 240/480 are TICKS values inside the B|B|T string, never the Samples value.

### 0x2516 — Reserved 32-byte all-zero placeholder slot (LEAF)

- **Kind:** LEAF (0 children, 26/26); top-level (no parent, 26/26). block_type (u16@z+1) = 0x0018 (24) in 26/26.
- **Size:** Fixed. Header byte-identical across all 26: `5a 18 00 22 00 00 00 16 25` → size(u32@z+3)=0x22 (34), payload_len = size−2 = 32, full span = 7+size = 41. Exactly one instance per session (26/26, incl. 7 huge Hipsters).
- **Fields (offset payload-relative = zmark+9):**
  - `+0..+31` (32 B): 32 zero bytes in ALL 26 sessions. 0 non-zero payloads. No internal field boundaries observable (could be 8×u32, 4×u64, a zeroed GUID pair, or pure pad — indeterminate).
  - CONTEXT (not payload): the 4 bytes immediately preceding the frame are `ff ff ff ff` in all 26 (trailing bytes of the preceding 0x2530 block).
- **Notes:** Reserved/placeholder slot immediately following 0x2530. The successor is NOT fixed: in 20/26 sessions 0x2516 is the LAST top-level body block (ends exactly at final_index.start); only in 6/26 (DWTS family + THE WIND / MANOLITO) is it followed by 0x4400. Describe as 'immediately after 0x2530, and either the terminal top-level body block or followed by 0x4400' — do NOT assert a fixed 0x2530..0x4400 sandwich. Emit as 32 zero bytes when synthesizing.
- **Confidence:** HIGH that it is a fixed 32-byte all-zero, block_type=0x18, span-41 record occurring once per session (verified 26/26, ≥2 projects). LOW on any internal field meaning — undeterminable because every instance is all-zero.

### 0x4421 — Format/effect-category registry entry (name + type-code)

- **Kind:** LEAF (no children; bisect first-child = None on all 18). block_type (u16@z+1) = 1 for every instance. Parent is ALWAYS 0x4422 (a count-prefixed registry table), itself a direct child of the single 0x2519 name-table block near the file head.
- **Size:** block_size (u32@z+3) is one of exactly three constant values 21 / 28 / 33 (payload lengths 19 / 26 / 31; payload = block_size − 2). Exactly 3 instances per session, one of each size, always in the fixed order 21,28,33. 18 instances across 6 corpus sessions (3 truly-distinct lineages: DWTS, MANOLITO, THE WIND).
- **Fields (offsets payload-relative = zmark+9):**
  - `+0..+3` (4 B): type-code tag (ASCII, all end in 'TP'): slot0='FCTP' (46 43 54 50), slot1='d7TP' (64 37 54 50), slot2='mATP' (6d 41 54 50). Constant across every session. Whether byte-reversed FourCC is unconfirmed (reversed forms 'PTCF'/'PT7d'/'PTAm' are not obviously meaningful) — treat as an opaque tag that happens to end 'TP'.
  - `+4..+8` (5 B): constant `01 00 00 00 00`. Naturally read as u32 version = 1 (+4) then u8 flag = 0 (+8); the exact partition is not load-bearing (never varies).
  - `+9`: length-prefixed ASCII string (u32 length then that many bytes) = display name: slot0 len6 'ClipFX', slot1 len13 '7.1.2 Formats', slot2 len18 'Ambisonics Formats'. No trailing bytes after the string.
- **Notes:** A named entry in a fixed, built-in format/effect-category registry mapping a 4-byte internal type-code to a display name. Each entire payload is byte-identical across all 6 sessions per slot — a hard-coded built-in table, not per-session data. PT writes an identical 3-entry set (ClipFX + two surround/Ambisonics format groups) only into sessions from PT versions that support these features; older sessions omit the whole table. This is stable persisted data (NOT view-state PT recomputes), so a precise schema is warranted.
- **Confidence:** HIGH. Confirmed across ≥3 distinct sessions: parent=0x4422, LEAF, tags, version/flag constants, string encoding, location in the early 0x2519 name-table region (~9–28 KB from head).

### 0x259a — Window display-state wrapper (0x259a→0x2599→0x2552→0x2011 chain)

- **Kind:** CONTAINER (bt=1), exactly one child 0x2599 (bt=3); subtree chain 0x259a→0x2599→0x2552(bt=2)→0x2011(bt=1). Top-level in EVERY session (parent=None). 0 or 1 per session, NEVER more than 1.
- **Size:** Full span 61 bytes (constant, all 17). size field u32@z+3 = 54. Payload (z+9..e) = 52 = exactly the full span of the single 0x2599 child (zero trailing bytes in 0x259a). Present in 17 sessions incl. all 7 Hipsters, 7 Reverse Rewire, Bianca, Let Her Go, Quality Time.
- **Fields:**
  - 0x259a payload = the entire embedded 0x2599 block. 0x259a has NO scalar fields of its own before or after the child (pure wrapper), all 17.
  - 0x2599 (bt=3): 9-byte header, one child 0x2552, then a 3-byte tail: `01 05 00` (Hipsters/Bianca/LetHerGo/QualityTime) or `01 00 01` (Reverse Rewire) — session-dependent.
  - 0x2552 (bt=2): 9-byte header, then a CONSTANT 2-byte scalar field `28 00` (=40 u16) BEFORE the 0x2011 child (all 17), then a 2-byte tail `00 00` after the child.
  - 0x2011 leaf payload (18 bytes) = 4 u32 LE + 2 trailing bytes: u32@+0, u32@+4, u32@+8, u32@+12. u32[0],u32[1] session-dependent (small set: (1766,781) shared by Hipsters+Bianca+LetHerGo+QualityTime; (514,247) all Reverse Rewire). u32[2]=679, u32[3]=678 CONSTANT across every session. 0x2011 trailing 2 bytes: `00 00` (Hipsters, Reverse Rewire) or `40 00` (Bianca, Let Her Go, Quality Time).
- **Notes:** A thin outer container around exactly one 0x2599 child; the session-varying data lives 3 levels down in a 0x2011 leaf that looks like window/view geometry PT recomputes. Cluster context: top-level neighbors 0x259c 0x206b 0x2068 0x2069 0x2595 [0x259a] 0x2066 0x2071 0x2072 0x202d 0x202e, stable order in all sessions. u32[2]/u32[3] being identical (679/678) across 3 unrelated machines argues against a raw per-window pixel rectangle. Display/view-state PT recomputes.
- **Confidence:** HIGH that 0x259a is a pure top-level wrapper (no own fields), single-0x2599-child, span 61/size 54/payload 52, chain to 0x2011, one-per-session — reproduced across 17 instances in 5 distinct session families. MEDIUM that the 0x2011 u32s are window/view geometry. LOW on any individual u32's exact pixel meaning.

### 0x2599 — Floating-window geometry snapshot

- **Kind:** CONTAINER — exactly one immediate child 0x2552 (17/17), which holds exactly one nested 0x2011 rect (17/17). Parent is ALWAYS 0x259a (top-level/ROOT); 0x259a contains exactly one 0x2599 (single-purpose wrapper). Own block_type=3; child 0x2552 block_type=2; nested 0x2011 block_type=1.
- **Size:** Fixed: size_field=45, payload=43 bytes, span=52 (17/17 across every corpus session that has it: 7 Hipsters + 7 Reverse Rewire + Bianca + Let Her Go + Quality Time). Low-frequency: exactly one per session, and only 17 of 26 corpus sessions contain it. No child_bt=2 size variant observed.
- **Fields (offsets payload-relative = zmark+9):**
  - `+0..+8`: nested 0x2552 child header — `5A 02 00` (bt=2), size u32@+3 = 0x21=33, ct u16@+7 = 0x2552.
  - `+9` u16: 0x2552 payload leadword = 0x0028 (constant 17/17).
  - `+11..+19`: nested 0x2011 rect header — `5A 01 00` (bt=1), size u32@+14 = 0x14=20, ct u16@+18 = 0x2011. 0x2011 rect payload starts @+20.
  - `+20` u32: rect x (window left). VARIES but only 2 distinct values corpus-wide (1766 ×10, 514 ×7 [Reverse Rewire]) — often a shared default.
  - `+24` u32: rect y (window top). 781 (×10) or 247 (×7).
  - `+28` u32: rect width — CONSTANT 679 (0x2A7) across all 17.
  - `+32` u32: rect height — CONSTANT 678 (0x2A6) across all 17.
  - `+36` u16: flags/position word inside the 0x2011 rect payload — 0x0000 (14) or 0x0040 (3: Bianca, Let Her Go, Quality Time). Final u16 of the 18-byte 0x2011 rect payload (rect payload = +20..+38).
  - `+38..+39`: 0x2552 trailing bytes (after its 0x2011 child) = `00 00` (constant 17/17).
  - `+40..+42`: 0x2599 OWN trailing bytes (after the 0x2552 child, until 0x2599 payload ends) = 3 view-flag bytes, per-session: `01 05 00` (Hipsters+Bianca+LetHerGo+QualityTime, ×10) or `01 00 01` (Reverse Rewire, ×7).
- **Notes:** A saved geometry snapshot for a floating window/pane; pure fixed-size integer geometry (no strings, no GUIDs), byte-identical across all backups of a given session. w=679/h=678 constant with variable x/y ⇒ a fixed-size window PT repositions. 0x2552 is a GENERIC reused rect/window sub-record (also appears under 0x2588, 0x258b, 0x2551, 0x2556) — do NOT over-attribute a 'browser/list' meaning. It REUSES the same 0x2011 rect sub-record as the 0x2019–0x201f window-geometry family. x/y are NOT reliably per-session-unique (only 2 distinct pairs; 4 of 5 families share (1766,781)).
- **Confidence:** HIGH on structure/counts/parent/child/grandparent, size/span, and every header + rect offset/width (verified 17/17 across 5 distinct families, byte-decoded). MEDIUM on 'view-state geometry snapshot' semantics ('PT recomputes on save' is INFERENCE, no PT oracle). LOW on the exact meaning of the +36 flags word and the 3 own-trailing view-flag bytes.

### 0x2083 — Optional all-zero placeholder record (LEAF)

- **Kind:** LEAF — zero descendants (17/17). Parent = ROOT/top-level (17/17). Own block_type (u16@z+1) = 2 (17/17). STABLE slot: immediately preceded by 0x258e (span ends exactly at this block's zmark) and immediately followed by 0x1049, in all 17 instances.
- **Size:** Fixed: size_field (u32@z+3) = 31, payload = 29 bytes (z+9..end), full span = 38 (17/17). At most ONE per session where present, and 0 where absent.
- **Fields (offset payload-relative = zmark+9):**
  - `+0..+28` (29 B): ALL ZERO in every one of the 17 present instances (0 non-zero payloads corpus-wide, verified by both size-driven enumeration and an independent raw signature scan `5a 02 00 1f 00 00 00 83 20`). No decodable internal structure — no length-prefixed strings, no `2A 00 00 00`-tagged GUIDs, no non-zero numerics.
- **Notes:** OPTIONAL / not written for every session: present in 17/26 sessions and provably ABSENT (0 occurrences anywhere in the byte stream) in the other 9 (DWTS family, COGNAC, DOPE, MANOLITO, Never Will Marry, THE WIND). A synthesizer can either omit it entirely or copy it verbatim (both patterns occur in valid sessions). Consistent with a session-scoped feature/preference slot PT materializes only when that (default-off) feature slot exists, leaving it blank. Contrast with 0x209e (singleton-at-ROOT but non-zero).
- **Confidence:** HIGH on STRUCTURE (fixed 38-byte / 29-payload all-zero LEAF at ROOT, block_type=2, fixed 0x258e→[0x2083]→0x1049 slot; 17/17 across 5 distinct projects) and on the PRESENCE PATTERN (optional, 17/26; absence raw-scan-confirmed, not a parse artifact). LOW/UNKNOWN on SEMANTICS — all-zero payload leaves nothing to decode; role is 'reserved/default-empty optional record'.

### 0x209e — Session-global settings/flags record (LEAF)

- **Kind:** LEAF — zero children (17/17). Parent = ROOT/top-level (17/17). Own block_type = 3 (17/17). Stable ROOT-neighbor sequence: …0x2083, 0x1049, 0x208f, [0x209e], 0x2302, 0x230a, 0x2305….
- **Size:** Fixed: size_field=11, payload=9 bytes, span=18 (17/17). Exactly one per session where present (confirmed by size-driven enumeration AND an independent raw signature scan `5a 03 00 0b 00 00 00 9e 20`).
- **Fields (offsets payload-relative = zmark+9):**
  - `+0` u8 = 0x00 (const). `+1` u8 = 0x00 (const).
  - `+2` u8: the ONLY byte that varies corpus-wide — 0x01 in 10 instances, 0x00 in the 7 Reverse Rewire backups. Meaning UNKNOWN.
  - `+3` u8 = 0x01 (const). `+4` u8 = 0x00 (const). `+5` u8 = 0x01 (const). `+6` u8 = 0x00 (const). `+7` u8 = 0x00 (const). `+8` u8 = 0x00 (const).
- **Notes:** Small session-global settings/flags record, a ROOT sibling appearing between 0x208f and 0x2302. Payload alphabet is exactly two 9-byte constants: `00 00 01 01 00 01 00 00 00` (×10) and `00 00 00 01 00 01 00 00 00` (×7, Reverse Rewire only); constant within each .bak lineage → a stored session preference. NOTE: presence is itself session-dependent — 0x209e is carried by 17 of 26 corpus sessions (absent from DWTS family and others); the '17/17' figures require including the 7 Hipsters sessions.
- **Confidence:** HIGH on structure (fixed 9-byte LEAF at ROOT, block_type=3, size_field=11, span=18, 0 children, stable neighbors) and on WHICH byte varies (+2 is the sole variable offset). LOW/UNCONFIRMABLE on the MEANING of +2 — the 17 instances collapse into only 5 distinct lineages, and the 0x00 value is witnessed by exactly ONE lineage (Reverse Rewire), giving no basis to decode +2's semantic.

### 0x262d — Source-take list per-entry wrapper (specialized name list)

- **Kind:** CONTAINER, own block_type=1 (16/16). Exactly one immediate child 0x2628 (16/16), a LEAF (block_type=2, 0 sub-blocks). Parent = ALWAYS 0x262e (16/16), a single top-level container (block_type=1, parent=ROOT) whose ONLY direct children are these 16 0x262d entries. gparent = ROOT. NOTE: 0x262e is present (count=1) in EVERY corpus session but wraps 0x262d children only in this one session; elsewhere the general 0x2628 records sit under 0x2629 instead.
- **Size:** Variable, driven by name length and base-vs-edit class. 0x262d size_field (span = 7 + size_field): BASE clips (flag 0) = 71 (7-char name) or 78 (14-char name); EDIT sub-clips (flag 1, '…-01' name) = 81/82 (10-char) or 88/89 (17-char). Payload = child-0x2628-total (7 + child_size_field) + 5 trailing bytes. Observed in only ONE corpus session (MANOLITO SIMONET TEATRO MELLA.bak.018), all 16 instances internally consistent.
- **Fields (offsets payload-relative = zmark+9):**
  - `+0` (=z+9): child 0x2628 header `5A 02 00` (block_type=2), child size u32 @ payload+3, content_type 0x2628 @ payload+7.
  - `+9`: child payload begins: u32 name_len, then name_len ASCII = clip/take name. Observed lengths 7 ('C0004_1'), 10 ('C0005_1-01'), 14 ('card 3 C0001_1'), 17 ('card 3 C0001_1-01').
  - immediately after name (variable offset, name-length-dependent — no fixed absolute offset): u8 class flag = 0x00 (base take) / 0x01 (edited sub-clip). Correlates 1:1 with a '-01' name suffix.
  - after class flag: 4-byte tag. BASE (flag 0): tag = `02 20 00 00` (16/16 of base). EDIT (flag 1): tag = `02 10 00 08` or `02 20 00 08` — the 4th tag byte 0x00 vs 0x08 selects offset width; the 2nd byte 0x10 vs 0x20 tracks offset magnitude, NOT base/edit.
  - after tag: signed sample offset. BASE = i32 (e.g. `91 1e fe ff` = 0xfffe1e91 = −123247; all base offsets fall in 0xfffeXXXX, i.e. −65537..−131072). EDIT = i64 (e.g. −29, −90, −108, −64079, −65158) sign-extended with 0xFF bytes.
  - after offset: byte-identical constant tail (16/16): `00 00 00 00 FF FF 04 00 04 00` + 24 zero bytes + `FF FF FF FF` (ends the child 0x2628 span).
  - **0x262d OWN trailing** (after child 0x2628 ends): 5 bytes = u32 clip ORDINAL (0x00..0x0f, unique per entry = position in the 0x262e list) + 1 constant pad byte 0x00.
- **Notes:** Each 0x262d wraps one 0x2628 name record holding a PT auto-capture/take name (e.g. 'C0004_1', 'card 3 C0001_1', with '-01' edit variants) plus a signed sample offset, and appends a clip-list ordinal + one pad byte. Structurally the exact analogue of 0x2629 (the per-entry wrapper of the GENERAL clip list under 0x262a). Best interpretation: a source-media / imported-take list, distinct from the region list (the general clip/region list is 0x262a → 0x2629 → 0x2628; here 807 regions live there vs 16 raw source takes under 0x262e). 0x262d and 0x2629 appear to share the same inner 0x2628 name-record schema.
- **Confidence:** MEDIUM-HIGH on STRUCTURE, LOW on GENERALITY & SEMANTICS. All 16 instances verified: container→single-0x2628-leaf, block_type=1, parent 0x262e (top-level/ROOT), len-prefixed name, class flag, base i32 vs edit i64 offset (width selected by tag byte 4), byte-identical FF-FF tail, 5-byte trailing (ordinal + pad). **Low confidence caveat:** 0x262d occurs in ONLY 1 of 26 corpus sessions, so NOTHING here is proven to hold in other sessions; the offset meaning (source-file/sync sample offset) and the source-take interpretation are INFERRED.

### 0x2716 — DUAL-ROLE: name-table placeholder (A) / session-metadata store (B)

- **Kind:** Two variants, both block_type=1, distinguished by payload length (4 vs 64). (A) LEAF, no children; block-tree parent = 0x2519 container (immediate child, 4/4 distinct sessions). (B) CONTAINER with exactly one child 0x2715; block-tree parent = ROOT/top-level (4/4). Exactly one A and one B per session that has any (4/4).
- **Size:** (A) size_field=6, payload=4, span=13. (B) size_field=66, payload=64, span=73. Both byte-identical across every instance. SAMPLE: only 6 corpus files contain 0x2716 and 3 (DWTS SHOW / __001 / __002) are byte-identical duplicates, so effectively 4 DISTINCT sessions (DWTS-2703, DWTS-SHOW, MANOLITO, THE WIND); the other 20 sessions contain NO 0x2716 at all — genuinely low-frequency / absent from most sessions.
- **Fields:**
  - **VARIANT A** (payload 4): `+0..+3` = `00 00 00 00` (all-zero, 4/4). No fields; a fixed structural name-table slot. Position is invariant: the 0x2519 name-table container lists N×0x251a lane-name entries, then this single 0x2716, then 0x4422, then 0x4420. The ordinal==track-count semantics belong to the 0x2519 CONTAINER (payload+16 u32 = track count = #0x251a lane records / 2 for stereo) and to the final-index track-count IndexRecord (ordinal = #lane-instance records), NOT to this block's 4 zero bytes.
  - **VARIANT B** (payload 64): `+0` u32 = 1 (entry count, 4/4). `+4`: embedded child block 0x2715 header = `5A | 01 00 (bt=1) | 35 00 00 00 (size=0x35=53) | 15 27 (ct=0x2715)`. 0x2715 payload (@payload+13) = three u32-length-prefixed ASCII strings: key 'sessionMetadataBase64' (len 21), value 'AQAAAAAAAAA=' (len 12), format tag 'Base64' (len 6). Byte-identical 4/4. The value base64-decodes to `01 00 00 00 00 00 00 00` (u64 LE = 1).
- **Notes:** Variant A is a structural name-table placeholder emitted as part of the master name-table trailer (0x2716, 0x4422, 0x4420) — an editor rebuilding the name table must preserve the trailer trio and keep the container's payload+16 count and the track-count IndexRecord's ordinal in sync with the lane count. NOTE: variant A is NOT itself pointed to by a 0x2716 child_ref — the final-index track-count record (a 0x2519 IndexRecord) carries a child_ref whose TYPE TAG is 0x2716, but its OFFSET points at the 0x2519 CONTAINER block, not at the physical 4-byte 0x2716 block; `final_index.py` keys ordinal==track-count off `child_refs[0].child_type in (0x251B,0x2716)`. Variant B is the SAME frozen empty-default constant in 100% of cases (the base64 value = u64 LE 1 in every session; no real metadata is ever populated) — the empty-default form of an extensible base64 metadata store; treat as PT-recomputed/default boilerplate.
- **Confidence:** MEDIUM-HIGH (downgraded from HIGH). Byte-level facts are rock-solid and identical across every instance; the ordinal==track-count relationship is independently corroborated by `final_index.py` and the 0x2519 container's own count field. Downgraded because: (1) effective sample is only 4 DISTINCT sessions from a narrow PT-version range; (2) variant B's 'schema' is one observed constant, not a format seen to vary; (3) the physical-leaf-vs-child_ref distinction needed correction.

### 0x261d — Master-fader / master-track definition record

- **Kind:** CONTAINER — exactly two immediate children in ALL 11: 0x261b (definition bulk) then 0x200c (track view chain). Own block_type=1 (11/11). PARENT = ALWAYS 0x2624 (11/11), the top-level track-definitions container. 0x261d is NOT top-level/ROOT — it is a CHILD of 0x2624; 0x2624 (block_type 1) is the top-level block, present in all 26 sessions, holding ALL track records as siblings (0x261c many, 0x261e, 0x2621, 0x2620, 0x2623, and — when present — a single 0x261d).
- **Size:** size u32 field = payload_len + 2; frame span end = z+7+size (11/11). Size-field values: 851 (Hipsters ×7), 849 (Bianca), 870 (Quality Time), 892 (Let Her Go), 1885 (DOPE). Payload = size−2. Baseline cluster ~849–892; DOPE grows to payload 1883 because one I/O slot carries an embedded automation subtree (0x2616 → 0x2615/0x2613/0x2614/0x1038/0x260e). Size is driven by embedded automation, not by a header field. PRESENT in only 11/26 corpus files (5 independent projects; 6 are Hipsters backup revisions of one project); ABSENT in 15/26 (DWTS ×4, Reverse Rewire ×7, COGNAC, MANOLITO, Never Will Marry, THE WIND). At most one per session, present only when a Master fader exists.
- **Fields:**
  - 0x261d payload has NO scalar fields of its own: child 0x261b starts at z+9 (payload+0), 11/11. Header: `5A 01 00`, size u32, ct=0x261b @ +7.
  - 0x261b has exactly THREE immediate children (11/11): 0x102d (name/GUID wrapper) @ z+18, then 0x2627 (I/O slot list), then 0x260d (config) — 0x260d is the third peer child of 0x261b, not a sub-item of 0x102d.
  - 0x102d @ z+18 (bt=2) has ONE nested child: 0x2619 @ z+27 (bt=1); 0x102d then has a 6-byte trailer `00 01 00 00 00 01`. The name/GUID exists exactly ONCE, in the nested 0x2619 leaf (not duplicated).
  - 0x2619 leaf header @ z+27; its payload @ z+36 (31 bytes, 11/11): `+0` u32 name_len(=8), `+4` ASCII 'Master 1' (8 B), `+12` u8=0x01, `+13` u16=0x0100 (bytes `00 01` — NOT 0), `+15` u32=0 (`00 00 00 00`), `+19` tag `2A 00 00 00`, `+23` 8-byte GUID. GUID is unique per independent project (5 distinct values across 5 projects; the 7 Hipsters backups all share one GUID = same session identity).
  - 0x2627 I/O slot list: payload+0 u16 = slot count (=0x000B=11 in all 11). Then N slots. A slot = 0x2625 (9-byte payload framing one empty 0x2626 child, plen 0). In 10/11 all 11 slots are 0x2625; in DOPE one slot is a 0x2616 automation blob instead (10× 0x2625 + 1× 0x2616), still 11 total.
  - 0x260d config subtree (child of 0x261b): 0x1029 (bt=7) always present, plus 0x260a (×1–2), 0x260c (×2), and 0x260e (present in most but ABSENT in Bianca).
  - 0x200c @ end of 0x261b (offset varies 669–1705, depends on 0x261b size): view/display chain 0x200a → [0x2015, 0x203b]; 0x2015 → [0x203b (×1 or ×2), 0x2434]. 'Two 0x203b' is common (10/11) but Quality Time has only ONE. Display/view-state, varies by session.
- **Notes:** The session's Master fader/track definition — track name ('Master 1') + unique per-track GUID, an I/O-assignment slot list (0x2627), an automation/config subtree (0x260d, plus optional embedded automation), and the master track's edit-window view chain (0x200c). Name/GUID/I-O portions are AUTHORED session data; the 0x200c view chain is display/view-state PT recomputes and varies by session. Structurally a normal track record specialized as the master.
- **Confidence:** HIGH on top-level structure verified 11/11 (parent=0x2624, children [0x261b, 0x200c], block_type=1, name='Master 1', unique 8-byte GUID at 0x2619-payload+23 after the `2A 00 00 00` tag, fixed relative offsets 0x261b@+9, 0x102d@+18, 0x2619@+27) and that it is a master-track record. MEDIUM on the exact meaning of the 0x2619 +12/+13/+15 scalar fields, the 0x2627 slot semantics, and 0x260d config sub-fields (structure confirmed, per-field meanings inferred).

### 0x271a — Markers ruler / memory-location group root container

- **Kind:** CONTAINER — always exactly 2 immediate children in fixed order, back-to-back with ZERO padding: 0x2619 (ruler/group descriptor) then 0x2030 (marker / memory-location data blob). Parent = ROOT (parent_of None, all 6). block_type (u16@z+1) = 0x0001. NOTE the 0x2619 child is not flat: it contains a fixed nested 0x4301 sub-block (block_type 0x0001, size 34) at 0x2619-payload offset +91, present in all 6.
- **Size:** Size FIELD (u32@z+3) ranges 176 (MANOLITO, empty marker list) to 351500 (DWTS full). Exact decomposition (byte-verified, all 6): size = span(0x2619 child) + span(0x2030 child), no pre/between/post bytes. span(0x2619) fixed at 157 or 158 bytes (payload 148/149 + 9-byte header; the 158 case has one extra trailing 0x00). ALL variance is in the 0x2030 child: span 17 (size-field 10) when empty (MANOLITO), up to span 351341 when populated (DWTS). Total 0x271a SPAN when empty is 183 B (MANOLITO). Session-level singleton; present in ~1/3 of the corpus (6 of 19 loaded non-huge = 31.6%; 6/21 overall).
- **Fields (offsets payload-relative = zmark+9):**
  - `+0` (var): 0x2619 CHILD (ruler/group descriptor). Its payload (child z+9): +0 length-prefixed name string: u32 len=7 then ASCII 'Markers' (`07 00 00 00 4d 61 72 6b 65 72 73`); +11 flags/zeros region with a 0x01 marker byte at +13; the 8-byte ruler GUID appears 3× (each immediately after a `2A 00 00 00` tag) at 0x2619-payload offsets +18, +34, +136 (byte-identical within a session). A nested 0x4301 block (bt 0x0001, size 34) at 0x2619-payload +91. GUID observed: `98 17 0b d5 cc 8c ca 59` (DWTS, all 4 files incl. the 155-BPM derivative), `28 9b 86 d6 52 87 4b f6` (MANOLITO), `73 ed 90 d7 fe 0a cc b1` (THE WIND). The GUID is a PERSISTENT ruler identity that survives Save-As/edits (the 155-BPM DWTS is a differently-edited derivative with a different marker count yet the same GUID) — stable within a session lineage, distinct across unrelated sessions, NOT a strict per-file id.
  - `+var` (var): 0x2030 CHILD — the marker / memory-location list data blob. Empty = size-field 10 (payload 8 zero bytes). When populated it holds per-group 0x2077 blocks and per-marker 0x2506 records (DWTS full: 123× 0x2077 + 18081× 0x2506; THE WIND: 21 + 1281; MANOLITO: 0 + 0). This leaf is the marker payload; NOT decoded here.
- **Notes:** Root-level container for the 'Markers' ruler / memory-location group: holds the marker-ruler identity (name + 8-byte ruler GUID, in the 0x2619 child) plus the actual marker/memory-location list (the 0x2030 child). Present only in sessions that carry a markers ruler; its size is dominated entirely by the 0x2030 marker-data payload. 0x2619 and 0x2030 are GENERIC, heavily-reused content-types — the schema above holds only for the specific instances that are immediate children of 0x271a.
- **Confidence:** HIGH. Re-derived and verified in MANOLITO + all 4 DWTS files + THE WIND (6 instances). Root-level (parent None), btype 0x0001, the name string, the 3× GUID at +18/+34/+136, the nested 0x4301 at +91, and the size decomposition (size = span(0x2619) + span(0x2030), zero padding) are byte-exact. The flag/zero bytes between name and GUIDs, the 0x4301 contents, and the 0x2030 marker-record internals were not exhaustively mapped.

### 0x4422 — Format-group registry table (count-prefixed container of 0x4421)

- **Kind:** CONTAINER — frame is `5A` + block_type u16=0x0001 + size u32 + content_type u16=0x4422; payload is a u16 child-count (=3) followed by exactly 3 immediate children, all 0x4421 (each a full 0x5A-framed block, NOT an inline field). Parent = 0x2519 (name/string table). Grandparent = ROOT (measured: 0x2519 has no enclosing block).
- **Size:** size FIELD (u32@z+3) = 107 in every instance (all 6). Total block SPAN (z..e inclusive, e = z+7+size) = 115 bytes; PAYLOAD (z+9..e) = 106 bytes. No variation across 6 instances / 3 independent families (DWTS, MANOLITO, THE WIND).
- **Fields:**
  - `+0` (frame) u8: `5A` marker. `+1` (frame) u16: block_type = 0x0001. `+3` (frame) u32: size = 107 (block ends at z+7+size). `+7` (frame) u16: content_type = 0x4422.
  - `payload+0` (=z+9) u16: child-count = 0x0003 (number of 0x4421 children).
  - `payload+2 .. end`: exactly 3 back-to-back 0x4421 child BLOCKS (each a full 0x5A frame). Each 0x4421 payload = { +0 4 B tag code ('FCTP','d7TP','mATP'); +4 5 B constant `01 00 00 00 00`; +9 u32 str-len; +13 str-len ASCII }. Decoded, identical in all instances: FCTP→'ClipFX'(6), d7TP→'7.1.2 Formats'(13), mATP→'Ambisonics Formats'(18).
- **Notes:** A small fixed factory string-table registering plugin/clip FORMAT-GROUP display names keyed by 4-char codes. Written verbatim by PT and byte-identical (single distinct 115-byte span) across unrelated sessions. PRESENCE IS OPTIONAL/VERSION-GATED: only 6 of 26 corpus sessions contain it (3 independent families; 4 of the 6 are DWTS variants); 20 sessions — including whole families (Reverse Rewire, Hipsters, DOPE) — have zero 0x4422 and open fine, so it is not required for validity and is almost certainly gated on a PT version new enough to register these newer format groups. If a synthesizer treats 107 as span or payload it will mis-place following blocks (span 115, payload 106). Safest treatment: a canned factory table to copy verbatim when targeting a recent PT version.
- **Confidence:** HIGH (schema) / MEDIUM (universality). Schema + exact bytes CONFIRMED across 3 unrelated sessions (parent=0x2519, grandparent=ROOT both measured; block_type=0x0001; child-count=3; all 3 children 0x4421 with the tag/const/len-prefixed-name layout). Universality downgraded because 20/26 sessions omit the block entirely. The 4-char tags look obfuscated/hashed; stable but their derivation is unconfirmable.

### 0x2435 — Score/print-page header field definition (LEAF)

- **Kind:** LEAF (no child blocks; 0 descendants in all 7). block_type=0x0004. Parent = 0x2023 (a top-level session-settings/properties group, block_type=0x0012). Since 0x2023 is top-level, grandparent = ROOT.
- **Size:** Fixed: span=84 bytes, declared size=77, payload=75 bytes across all 7 — byte-identical. IMPORTANT: all 7 instances belong to a SINGLE project ('Reverse Rewire', 7 sequential .bak backups); no other project family in the corpus carries 0x2435 at all, so cross-project size/value variance is UNPROVEN — a one-project observation.
- **Fields (offsets payload-relative = zmark+9):**
  - `+0` u32 len=5, then 'Title' (5 ASCII) → label #1 (occupies [0:9)).
  - `+9` u32 len=8, then 'Composer' (8 ASCII) → label #2 (occupies [9:21)).
  - `+21` 8 bytes of 0x00 (zero fill / separator) [21:29).
  - `+29` u32 = 1 (count/flag) [29:33).
  - `+33` f32 = 6.0 (page-layout dimension; specific meaning inferred, not proven).
  - `+37` f32 = 15.0, `+41` f32 = 15.0, `+45` f32 = 15.0, `+49` f32 = 15.0 (a run of four 15.0 at [37:53)).
  - `+53` 4 bytes = `01 01 01 01` (four boolean/enable flags) [53:57).
  - `+57` f32 = 5.5, `+61` f32 = 9.25 (layout dims).
  - `+65` 8 bytes of 0x00 (zero fill) [65:73).
  - `+73` 2 bytes = `01 01` (trailing flags) [73:75), end of payload.
- **Notes:** Stores the two default title-block field labels ('Title', 'Composer') as length-prefixed strings, followed by a small run of f32 page-layout geometry values (Score Editor page setup). A singleton child under the 0x2023 session-settings group, immediately after a sibling 0x2434. Score-editor page-setup state PT would recompute/rewrite from the Score Setup dialog. The 'Title'/'Composer' strings make the score/print-header role certain; WHICH geometric quantity each f32 represents is inferred from context, NOT proven.
- **Confidence:** MEDIUM. All f32 offsets, the +53 flags, both strings, and the fixed 77-byte size are CONFIRMED exact across the 7 instances. GENERALITY CAVEAT: all 7 are one project, so treat as a one-project sample — role confident, exact geometric semantics best-effort, cross-project stability untested.

### 0x2612 — I/O routing-path entry (single output/bus signal path)

- **Kind:** CONTAINER (block_type u16 = 0x0003) with exactly 2 immediate children in fixed order: 0x2626 (a 2-byte empty/placeholder marker, block_type 0x0001) then 0x260e (path-detail record carrying the name, block_type 0x0001). Parent = 0x2627 (path list; sibling children there are 0x2625 ×6 and 0x2616 ×4). Grandparent = 0x261b (I/O/bus group). NOTE: the size-driven enumerator gives the two children a 1-byte overlap (0x2626 rel-end = 0x260e rel-start = 18) because they share the 0x5A frame marker.
- **Size:** Whole 0x2612 span is 74 bytes, byte-identical across all 7 instances. The u32 size FIELD in the header is 66 (0x42); on-disk span = size+8.
- **Fields (offsets payload-relative except header):**
  - `+0..+8` (9 B): frame header — `5A`; block_type u16 = 0x0003; size u32 = 66; content_type u16 = 0x2612. Payload begins at +9.
  - `+9`: child 0x2626 — header block_type 0x0001, size 2; its real payload is empty (the 1 declared payload byte is the shared 0x5A that begins the next child). Fixed empty 2-byte marker.
  - `+18`: child 0x260e — header block_type 0x0001, size 48; payload (47 bytes) = { `+0..+1` two-byte index/ID (0xFFFF = unassigned here); `+2..+3` = `01 01` (flag pair; usually 0101 but sometimes 0100/0000 across sibling 0x260e, so NOT a fixed constant); `+4..+35` = 32 bytes of 0x00 (reserved / GUID slot, all zero here); `+36` u32 str-len = 5; `+40` ASCII 'A 1-2'; `+45` trailing 0x01; `+46` = 0x5A block-boundary marker (frame boundary, not the name) }. The routing-path NAME at 0x260e-payload +36 is the one field that generalizes (149 string-bearing 0x260e blocks in a single session all place their name at +36).
- **Notes:** A single output/bus signal-path entry inside the session's I/O-setup section; its 0x260e child carries a length-prefixed routing-path name (here 'A 1-2', a PT hardware/bus output label). Grandparent 0x261b holds sibling path names 'Mix Bus', 'A 1-2', 'Bus 35-36', 'Bus 37-38'. I/O/routing display+setup state PT owns and recomputes; treat as opaque configuration. 0x2612 occurs exactly 7 times total — ONCE per session in 7 sequential backups of a SINGLE project (Reverse Rewire_M1.1.bak.073-079), effectively one real occurrence re-saved; zero 0x2612 in all other sessions.
- **Confidence:** MEDIUM. CONFIRMED (byte-identical across all 7 backups): the 2-child structure/order, parent 0x2627, grandparent 0x261b, the I/O-setup role, 74-byte span, embedded name 'A 1-2'. The 0x260e '+0 ff ff' and '+2 01 01' are instance-specific (ID is 0xFFFF only when unassigned; the flag pair is not always 0101 across sibling records), so those widths/meanings are NOT independently proven as a general schema; the only cross-corroborated 0x260e field is the length-prefixed name at +36. Because 0x2612 lives in only one project family, the 0x2612-level field widths are not proven cross-project.

### 0x200c — Per-Master-strip default-settings sub-container (CONTAINER)

- **Kind:** CONTAINER — exactly one immediate child 0x200a. Ancestor chain confirmed in all 11: 0x200c ← 0x261d ('Master 1' strip) ← 0x2624 (top-level tracks/playlists group; no further parent). Nested tree of 0x200a: 180-byte form = 0x2015 → [0x203b(→0x2037), 0x203b(→0x2037), 0x2434] plus one sibling 0x203b(→0x2037) directly under 0x200a (3 × 0x203b/0x2037 + 1 × 0x2434 total); 158-byte form = 0x2015 → [0x203b(→0x2037), 0x2434] plus one sibling 0x203b(→0x2037) under 0x200a (2 × 0x203b/0x2037 + 1 × 0x2434 total). Block header type 0x0001; 0x200a is header type 0x0005; 0x2015 is header type 0x000f.
- **Size:** Two size classes. 180 bytes (span 187): 10/11 instances (all 7 Hipsters baks, Bianca, DOPE, Let Her Go). 158 bytes (span 165): 1 instance (Quality Time). The 22-byte difference is exactly one 0x203b/0x2037 pair (span 22) removed from inside 0x2015 — a count difference of one nested key record, NOT a byte-different content variant. OPTIONAL: present in only 11/26 sessions (once each); absent from the other 15.
- **Fields:**
  - `+0` (9 B): 0x200c block header `5A | block_type u16=0x0001 | size u32 (180 or 158) | content_type u16=0x200c`. Payload starts at +9.
  - `+9` (var): single child 0x200a (block header type 0x0005), holding the entire nested settings tree; no direct scalar fields between the 0x200c header and this child.
  - Inside the tree, each key record is a 0x203b block whose payload begins with a 0x2037 sub-tag (block-size 2, ZERO payload bytes — an empty marker, NOT a 2-byte value) followed by the actual value (e.g. a u32 `01 00 00 00`). The value trails the 0x2037 inside the 0x203b.
  - 0x2434 (block-size 11) has an INVARIANT 9-byte payload across all 11: `04 00 00 00 00 00 00 3c 00`.
  - The 0x2015 payload contains session-VARYING numeric fields: an 8-byte little-endian IEEE-754 f64 whose high 2 bytes read `.. e0 3f` (=0.5 in most sessions) or `.. e1 3f` in Hipsters (`08 21 84 10 42 08 e1 3f` ≈ 0.502), plus u32 counters (e.g. `5c 00 00 00`, `12 7a 00 00`, `50 c3 00 00`). The meaning of each individual 0x203b/0x2037 key was NOT decoded.
- **Notes:** An optional per-strip default-settings sub-container carried by the session's single Master channel-strip definition (under the one 0x261d whose length-prefixed strip name is 'Master 1'). Wraps a small nested tree of tag/value setting records (0x200a → 0x2015 → 0x203b/0x2037 pairs + one 0x2434). The 'default-settings' role is inferred from position under the Master strip, not proven. The varying f64/u32 values suggest stored settings rather than pure view geometry, but round-trip recompute-vs-persist behavior was not tested.
- **Confidence:** MEDIUM. Container shape, single-0x200a child, the 0x200a→0x2015→0x203b/0x2037 + 0x2434 tree, header types, ancestor chain, and the invariant 0x2434 payload are CONFIRMED across all 11 instances spanning 5 distinct sessions. Semantics of individual key/value pairs undecoded; role inferred from position.

### 0x2554 — Timeline/timebase ruler view-state settings LEAF

- **Kind:** LEAF (zero child blocks in all 8). Parent = 0x2551 (ruler-scale settings) in every instance. Grandparent = 0x2587 (whose subtree holds the timebase display-format strings). Sibling set under 0x2551 varies (0x2552, 0x2553, 0x258b, sometimes 0x2555/0x2556). NOT always the last child of 0x2551 — in MANOLITO a 0x2555 sibling follows it.
- **Size:** Variable and correlated with the framing block_type (u16@z+1): size 28 ↔ bt 0x0004 (Bianca, Quality Time); size 44 ↔ bt 0x0006 (DOPE); size 53 ↔ bt 0x000b (DWTS ×2 + copies, MANOLITO). Payload length = size − 2. Different sizes = different payload layouts/versions; do NOT assume one schema. ABSENT from 11 of 19 distinct corpus sessions (incl. both huge Hipsters sessions).
- **Fields (offsets payload-relative = zmark+9; apply ONLY to the 0x01-form):**
  - `+0` u8: format/version discriminator — 0x01 in 7/8 (Bianca, DOPE, DWTS ×4, MANOLITO) ⇒ flag-oriented layout; 0x08 in Quality Time ⇒ a structurally UNRELATED layout (opaque bytes `98 17 e8 f5 … ff ff ff ff`; NO `2A 00 00 00` GUID tag — treat as opaque). The offsets below apply ONLY to the 0x01-form.
  - `+1` u32 LE: per-session id (observed 105 / 143 / 165 / 105); differs per session. (Low 3 bytes usually zero → effectively a small int.)
  - `+5` u32 LE: per-session value (observed 520 / 552 / 723 / 970); differs per session.
  - `+10..+18` (short 26 B form) / `+10..+25` (44 B & 53 B forms): run of 01/00 boolean display flags; mostly stable with a few differing bytes (e.g. +18, +23).
  - `+26` (2 B) const `05 0a` — LONG-FORM ONLY (DOPE-44, DWTS/MANOLITO-53). ABSENT in Bianca-28 (too short to reach it). NOT universal to the 0x01-form.
  - `+43` (1 B) const 0x55 — size-53 form ONLY (absent in DOPE-44, Bianca-28).
- **Notes:** The settings LEAF under a grid/ruler-scale block. The 0x2587 subtree holds timebase display-format strings (bars|beats '0| 0| 240', min:sec '0:00.100', SMPTE '00:00:00:01.00', feet+frames '0+01.00', and a raw sample count). Confirmed display/view state PT recomputes, not stable session data (absent from >half the distinct sessions; payload version/format-dependent). DECLINE to claim a single cross-size schema.
- **Confidence:** LOW for field schema; HIGH for role/parent/kids/absence. CONFIRMED: LEAF (0 children), parent always 0x2551, grandparent always 0x2587, and the 0x2587-subtree timebase strings; the 0x01-form skeleton (+0=01, u32 id@+1, u32@+5, flag run, '05 0a'@+26 in long forms, 0x55@+43 in size-53) holds across DOPE-44 + DWTS-53 ×2 + MANOLITO-53. 8 instances across 8 files but only ~5–6 distinct projects (3 byte-identical DWTS copies + 1 same-project variant). The Quality Time 0x08-form has no verifiable GUID (downgraded from 'GUID-bearing' to 'opaque').

### 0x254d — NOT A REAL CONTENT TYPE (parsing artifact of the file-header index pointer)

- **Kind:** REFUTED — not a block. It is the file-header index-pointer region (data[0x10:0x1f]) that the 0x5A size-walk erroneously enumerates as a top-level block at z=0x14. The header stub is framed `5A 01 00 04 00 00 00` + a 3-byte little-endian offset to the final index + `00`. The walker reads content_type=u16@z+7 = the LOW 16 bits of that offset, and payload u16@z+9 = the HIGH 16 bits. Reports parent=None and no children only because it is a header field, not a tree node.
- **Size:** Not applicable. The 'size=4' the walker reports is the coincidental header constant 0x00000004 at data[0x17], identical in EVERY session, carrying no session-specific meaning. The next block (real header) always begins exactly at z+11 = 0x1f.
- **Fields:**
  - `@0` u16 = 0x003E (62 in DWTS) — REFUTED as a version. It is the HIGH 16 bits of the 24-bit final-index file offset; varies per session with total file size (observed 62, 5, 124, 93, 9, 2, 17, 7, 11 across the corpus). 'Byte-identical across the 3 DWTS files' only because they are the same session re-saved to the same byte size.
  - NOTE: the reported content_type 0x254D itself = final_index.start & 0xFFFF (LOW 16 bits of the same offset). `ct | (field<<16)` reconstructs final_index.start exactly (verified 19/19 sessions). For DWTS the final index lives at file offset 0x3E254D, so the low half reads 0x254D and the 'version' reads 0x3E=62 — two halves of one 24-bit file-offset pointer, not a schema revision.
- **Notes:** Do NOT add a 0x254d entry to the content-type spec. Instead document data[0x10:0x1f] as the fixed file-header index-pointer (template `31 00 05 xx  5A 01 00 04 00 00 00  <u24 final_index_offset> 00`) and note that a naive 0x5A scan/size-walk manufactures a phantom top-level block there whose 'content_type' equals final_index.start & 0xFFFF. The REAL first structural block is at z=0x1f: content_type 0x2067 (name-table header) in 2018+ sessions (DWTS/MANOLITO/THE WIND), or 0x0003 in older 10.x/11.x sessions. DWTS still CONTAINS a genuine 0x0003 version-string block near the tail (plain 'Pro Tools Ultimate … 2018.4.0 … Release …').
- **Confidence:** HIGH (REFUTATION confirmed). The `ct | (payload<<16) == final_index.start` identity holds in 19/19 loadable sessions across every corpus family. The 'schema-version 0x254d' interpretation is disproven, not merely low-confidence.

### 0x210c — Session-header scalar flag/count, always default (LEAF)

- **Kind:** LEAF (top-level; parent = ROOT; zero descendants, verified across all 6 via a stack-based parent map). block_type=0x0001. Its top-level sibling sequence is (…0x2507, 0x2508, 0x25c0, 0x2107, [0x210c], 0x271f, 0x2530, 0x2516, 0x4400, 0x2716…). NOTE: in raw address order the block immediately BEFORE it is 0x210b, but those 0x210b records are CHILDREN of the preceding top-level 0x2107 block; the immediately-preceding top-level SIBLING is 0x2107.
- **Size:** Fixed: block_type=0x0001, size field=6 (= 2-byte content_type + 4-byte payload). On-disk frame = `5A 01 00 06 00 00 00 0C 21 <4-byte payload>`; payload occupies z+9..z+12. (`_raw_block_bounds` reports an inclusive end of z+13, which is the 0x5A marker of the NEXT block.)
- **Fields (offset payload-relative = zmark+9):**
  - `@0` u32 (LE) = 0x00000000 — flag/count, always 0 (6/6, byte-identical). The 0x5A byte seen after the u32 is the next block's marker, NOT payload/padding.
- **Notes:** A session-header scalar in the session-settings region. Present in exactly 6 corpus sessions (DWTS SHOW / __001 / __002, DWTS-for-2703, MANOLITO, THE WIND — newest-format only); absent from all older-format sessions. Likely a session-setting/display-state default PT writes and recomputes; value never varies, so no precise semantic beyond 'default scalar flag or counter'.
- **Confidence:** HIGH on layout/structure (fixed size=6, single u32=0, LEAF top-level, byte-identical header across 6 instances in 3 distinct families; raw byte-pattern scan matches the size-driven parser count exactly, no phantoms/undercounts). LOW on precise semantic — value never varies (always 0).

### 0x271f — Session-header scalar flag/count, sibling of 0x210c (LEAF)

- **Kind:** LEAF (top-level; parent = ROOT; no children). block_type=0x0001.
- **Size:** Fixed: size=6 (block_type=0x0001). Payload = 4 bytes. Total span 13 bytes [z, z+12]. Header `5A 01 00 06 00 00 00 1F 27`; payload `00 00 00 00`.
- **Fields (offset payload-relative = zmark+9):**
  - `@0` u32 = 0x00000000 — flag/count, always 0 (6/6 across all sessions that contain it).
- **Notes:** Sits in the settings region immediately AFTER a run of 0x210b blocks and a single 0x210c, and immediately BEFORE 0x2530: layout [… 0x210b (long run) → 0x210c → [0x271f] → 0x2530 → 0x2516 → 0x4400…]. Byte-identical to the immediately-preceding 0x210c except the content-type word (0x210c = …0C 21, 0x271f = …1F 27). PERFECT co-occurrence with 0x210c: exactly the sessions containing 0x210c also contain 0x271f (and none otherwise), once each — present in the same 6 newest-format instances (DWTS SHOW/__001/__002 are save-variants of ONE session, so ~4 distinct sessions), absent from all older-format sessions. Likely display/session-preference state PT writes at default; treat as always-0 default when synthesizing.
- **Confidence:** HIGH on layout/framing/co-occurrence (byte-exact, 6/6). LOW on precise semantic — value is invariant 0, so meaning (flag vs count vs enum) is unconfirmable.

### 0x2715 — Session-metadata (key, value, encoding) string triple (LEAF)

- **Kind:** LEAF (child of 0x2716; 0 children of its own). The size=66 0x2716 container has exactly one immediate child, this 0x2715, and its container payload begins with u32=1 which functions as the child-count.
- **Size:** Fixed: size=53, block_type=0x0001. Frame span = [z .. z+7+size]; content_type u16 @ z+7; payload begins at z+9 and is 51 bytes (= size−2), fully consumed by the three length-prefixed strings (leftover=0). Byte-identical across all instances.
- **Fields (offsets payload-relative = zmark+9):**
  - `@0` u32 len=21 + 21 ASCII = 'sessionMetadataBase64' (metadata KEY).
  - `@25` u32 len=12 + 12 ASCII = 'AQAAAAAAAAA=' (VALUE; Base64 of `01 00 00 00 00 00 00 00` = u64 LE 1).
  - `@41` u32 len=6 + 6 ASCII = 'Base64' (ENCODING tag).
- **Notes:** A single session-metadata entry — the sole leaf item of the 0x2716 metadata-list container. Length-prefix = u32-then-ASCII throughout; no GUID `2A 00 00 00` tag (a string-triple, not a GUID blob). PRESENCE: found in only 6 of the 19 loadable non-Hipsters sessions (13 lack it) — a newer-format-only structure. CAVEAT on raw 0x5A scans: they surface a spurious size=6 '0x2716' frame nested inside a 0x2519 name-table block in every affected session; the REAL container is the unique size=66 0x2716 and the REAL entry is its size=53 0x2715 child. The decoded u64=1 most plausibly encodes a metadata format/version marker.
- **Confidence:** HIGH for the fixed layout (size=53, block_type=0x0001, three length-prefixed strings at payload @0/@25/@41 consuming all 51 bytes, parent=0x2716, 0 children) — byte-identical in 6 sessions; value round-trips to u64 LE 1. MEDIUM for the SEMANTIC interpretation of u64=1 (format/version vs id vs flag is unproven; corpus is monomorphic).

### 0x2716 (variant B, container view) — Session-metadata LIST container

- **Kind:** CONTAINER, top-level (parent = ROOT). block_type=0x0001. Immediate (and only) child = one 0x2715. The 0x2715's own payload is three length-prefixed ASCII strings (key/value/encoding); that inner schema belongs to the 0x2715 entry, not this container.
- **Size:** Fixed at size=66 in all 6 instances (block_type=0x0001). Frame span = [zmark, zmark+7+size] = 73 bytes. Payload starts at zmark+9 and is 64 bytes (= size−2) = 4 (u32 count) + 60 (the full 0x2715 child frame: 7-byte header `5A 01 00 <size:u32>` + its 53-byte content). Child span end == parent span end (no trailing bytes).
- **Fields (offsets payload-relative = zmark+9):**
  - `@0` (=zmark+9) u32: count = 1 — number of 0x2715 children; only ever observed as 1.
  - `@4` (=zmark+13): embedded 0x2715 block — `5A 01 00 <size=53:u32> 15 27 …`; child zmark = parent zmark+13, child block_type=0x0001 @child+1, child content_type=0x2715 @child+7 (= parent zmark+20), child payload @child+9. Decodes to key='sessionMetadataBase64', value='AQAAAAAAAAA=' (base64 → u64 LE = 1, opaque/PT-managed), encoding='Base64'.
- **Notes:** The 64-byte session-metadata LIST container (variant B of the 0x2716 dual-role type; see the 0x2716 dual-role entry for variant A, the 4-byte name-table placeholder). Top-level position confirmed between 0x4400 (prev) and 0x0003 (next) in all 6; the 0x2530→0x2516→0x4400→0x2716→0x0003 subsequence holds in all 6. A naive 0x5A scan also reports a spurious size=6 '0x2716' per session (the bytes '16 27' occur inside a 0x2519 name-table entry, parent=0x2519, count=0, no real child) — filter by requiring a real 0x2715 child, not by content_type alone. Present in only 6/19 non-huge sessions (format-gated). The count>1 layout is inferred (never observed >1); the base64 value is opaque PT-managed metadata.
- **Confidence:** HIGH on structural claims — bt=0x0001, size=66, count=1, one embedded 0x2715 (child bt=0x0001, ct=0x2715 at zmark+20) verified byte-identical across all 6 real instances in 3 distinct projects; top-level position confirmed. LOW on interpretation of the base64 payload (treated as opaque). Presence is format-gated (absent in 13/19).

### 0x2301 — Low-frequency config/view-state singleton (LEAF)

- **Kind:** LEAF from the framer's view. Its sole parent is 0x2302, a TOP-LEVEL container (no grandparent) whose ONLY direct child is this single 0x2301 — a strict 1:1 wrapper. The inner 13-byte 'blob' is NOT a valid sub-block (decoded as-if-a-block its type word is 0x0100 with high byte set, so the recursive framer does not descend). Framed children = [].
- **Size:** Fixed: block_type=0x0001, size=25. Header is 9 bytes (`5A` + type u16 + size u32 + content_type u16); block spans the HALF-OPEN range [z, z+7+size) = [z, z+32). Payload = data[z+9 : z+32] = 23 bytes. The framer's end is EXCLUSIVE (== start of the next block, the 0x230a 'Custom' block); the 0x5A at rel-offset +32 belongs to that next block. A naive data[z+9:e+1] slice yields a spurious 24th byte.
- **Fields (offsets payload-relative = zmark+9):**
  - `@0` u32 = 19 (0x13) — byte-count of the remaining payload (23−4). CONFIRMED both instances.
  - `@4` u16 = 0 — CONFIRMED both instances.
  - `@6` u32 = 13 (0x0D) — length of the trailing blob. CONFIRMED both instances.
  - `@10` (13 B): opaque blob = `5A 00 01 00 00 00 06 00 01 00 00 00 00` (exactly 13 bytes; framing-shaped but NOT a real block — inner type word 0x0100 has high byte set so the framer skips it; content constant). Byte-identical both instances.
- **Notes:** A low-frequency singleton in the 0x23xx setup region: by file position it sits immediately after 0x208f and its own container 0x2302, and immediately before 0x230a (which holds 'Custom 1'..'Custom 8+' name strings) and a run of 0x230b — the memory-location / window-configuration / custom-naming setup area. Best read as PT-recomputed config/view-state, not stable user-data. Appears in exactly 2 of the 25 non-Hipsters sessions (COGNAC …BASIC TRACKING 07.21.18.bak.000 and THE WIND_Vocal Tracking.bak.000), 1 instance each, payload byte-identical.
- **Confidence:** MEDIUM on structure (CONFIRMED): size, block_type, all four field offsets/widths/values, parent, leaf-ness, and byte-identical payload re-derived from scratch across the 2 instances. LOW on semantic — no decodable strings inside; appears to be recomputed view/config state, not stable user data.

### 0x2302 (count-behavior view) — Container wrapping 0 or 1 0x2301

- **Kind:** CONTAINER (top-level; grandparent = ROOT; immediate child = 0x2301 only when count=1; NO children when count=0). block_type always 0x0001. Immediately preceded by 0x208f OR 0x209e and (in the count=0 case) followed by 0x230a.
- **Size:** Variable, driven by count. count=0 → size=6 (payload = 4-byte count only); count=1 → size=38 (payload = 4-byte count + one embedded 25-byte 0x2301 block, 13-byte outer framing incl. the child's own 5A/bt/size/ct). Only sizes 6 and 38 observed. Exactly one 0x2302 per session; count=0 in 17/19 sessions, count=1 in 2/19 (appears in 19 of 26 loadable sessions).
- **Fields (offsets payload-relative = zmark+9):**
  - `@0` u32: count = number of embedded 0x2301 children (0 or 1 observed). Fully predicts size: count=0→size=6, count=1→size=38.
  - `@4` (only when count≥1): embedded child block `5A <bt=0x0001> <size=25 (u32)> <ct=0x2301> …`; child zmark starts at payload+4 = 0x2302_zmark+13. When count=0 there is NO @4 field — payload ends after the count.
- **Notes:** Single top-level container in the 0x23xx setup/view-state region wrapping zero or one 0x2301 config/view-state record. count is 0 in the large majority (17/19), so 'count=1 + embedded 0x2301' is the minority case; the size=6 instances are legitimate root-level count=0 containers (NOT phantoms). The two count=1 spans (COGNAC, THE WIND) are byte-identical over the full span including the child (`5a0100260000000223010000005a01001900000001231300000000000d0000005a0001000000060001000000005a`). Given it lives in the 0x23xx display/config region and is almost always empty, most likely display/view-state PT recomputes rather than load-bearing data.
- **Confidence:** HIGH on: single top-level instance per session, block_type=0x0001, count field @payload+0, count→size mapping (6/38), byte-identical count=1 span across the 2 sessions that have it. MEDIUM on the 0x2301 child's internal semantics (inherited). Verified across 19 sessions (≥10 distinct project names). [This is the same content-type as the earlier 0x2302 entry, described here from the count-behavior angle.]

### 0x4400 — Channel Strip (cfx_cscs) serialized-parameter table

- **Kind:** LEAF at the .ptx block level (block_type=0x0002; kids_inside=0 across all 6 — the internal cfx_cscs/cfx_csc/hcmp/cfx_param_chnk/px tags are the plugin's own nested chunk format, not 0x5A sub-blocks). Always top-level (parent=TOP). Sits between top-level 0x2516 (prev) and 0x2716 (next).
- **Size:** size = 2 (ct word) + 4 (count u32) + count*2554 + 169 (trailing footer). Verified exactly: count=1 → size 2729 (payload 2727); count=6 → size 15499 (payload 15497). Per-record stride is a CONSTANT 2554 bytes. A fixed 169-byte footer is present regardless of count.
- **Fields (offsets payload-relative = zmark+9):**
  - `@0` u32: record_count = number of Channel Strip plugin instances (verified 1 and 6, matching the count of 16-byte GUID records and of 'Process Order'/72-px-tag records).
  - `@4` then record_count records, each EXACTLY 2554 bytes at payload-rel (4 + i*2554): [0:16] 16-byte plugin-instance GUID (distinct within a file; last 8 bytes = the constant class/type suffix `74 53 6B 43 EA 09 00 00` = 'tSkC'+ea090000 in every record), followed by a nested plugin chunk: raw-ASCII fourcc 'cfx_cscs' + u16 version(0x0001) + u32 size, then 'cfx_csc' + u16 ver + u32 size wrapping 'hcmp'/'cfx_param_chnk' and 72 'px' param entries.
  - Each 'px' param entry: 'px' + u16 0x0001 + u32 chunk_size + u32 name_len + name ASCII + 8 value bytes. The 72-NAME list is byte-identical across all records and sessions ('Process Order','Phase Switch In/Out','EQ On/Off',…,'Fader Volume','Meters Tab'). The interleaved VALUES differ per instance (record0 vs record1 differ in 22 non-GUID bytes).
  - **FOOTER** (after the last record): a fixed 169-byte trailing chunk BYTE-IDENTICAL across all sessions — a separate 'cfx_cscs' chunk whose cfx_param_chnk declares 3 params only: 'Dynamics Reveal','EQ Reveal','Filters Reveal'. NOT included in the @0 record_count.
- **Notes:** A count-prefixed array of GUID-keyed records, one per Channel Strip plugin instance, each carrying that instance's serialized 72-parameter chunk. Lets PT resolve automation ordinals to human-readable names AND restore per-instance settings — it is NOT a pure name-only dictionary/cache: the param NAMES are identical across all instances/sessions, but each record embeds distinct per-instance parameter VALUES, so it is primary instance state. Cross-file identity: the record with GUID 7aba93f9 56281a9d… is index 4 of the count=6 DWTS session AND the SOLE record of every count=1 session (THE WIND / MANOLITO / DWTS-for-2703) — same plugin-instance state reused across those files. Present in 6 instances across 4 distinct newest-format sessions.
- **Confidence:** HIGH on role, framing, and every top-level/size/offset claim (block_type=0x0002, top-level, LEAF, next-top=0x2716; record_count u32@0 == instance count == GUID-record count; 2554-byte constant stride; 16-byte GUID with constant last-8 suffix; 72 px param names byte-identical across 6 instances in 4 sessions; +169 footer and its 3 extra names byte-identical). MEDIUM/characterization-only on the DEEP plugin-internal sub-chunk layout (the outer cfx_cscs/cfx_csc tag+ver+size framing is clean, but hcmp/cfx_param_chnk internal sub-sizes do NOT parse as a uniform tag+ver+size scheme — a plugin-proprietary format characterized but not fully schematized).

### 0x2623 — Clip-group / compound-clip (region-group) definition record

- **Kind:** CONTAINER; parent 0x2624 (the top-level clip/playlist list, whose payload begins with a u32 that equals its immediate-child count — confirmed 79 header == 79 children). Immediate children of 0x2623, in order: 0x2619 then 0x200e. First child (0x2619) begins at payload offset 0 (0x2623 has no own header/prefix bytes before it). block_type(u16@z+1)=0x0001. VERIFIED on 1 instance only.
- **Size:** Variable. ONLY ONE instance exists in the local corpus (payload_size u32 = 436). Cannot confirm any broader size range/modes — those come from a larger corpus not available here. 436 is a single data point.
- **Fields (offsets payload-relative = zmark+9):**
  - `+0` u16: first child 0x2619 identity block begins immediately at payload offset 0 (0x2623 has NO own header bytes). In the one instance, 0x2619 payload = length-prefixed name ('C0004_1') then flags then one or more GUIDs, each following a `2A 00 00 00` tag (8 GUID bytes). 0x2619 spans payload offsets [0,157) (span = 7+150).
  - `+157` (4 B): gap between the two children = `00 00 00 00` (u32 0). The two children are NOT back-to-back adjacent. (1 instance; may be a fixed separator or a variable field.)
  - `+161` u16: second child 0x200e view-state block; spans payload offsets [161,424) (span = 7+256). Its immediate child is 0x2015 (part of the 0x200e view chain).
  - `+424` (10 B): trailer after the last child, before block end = `01 00 00 00 00 00 00 00 01 00` (u32 1, u32 0, u16 1). Real tail data after 0x200e. (1 instance; structure/variability unknown.)
- **Notes:** Defines one named, GUID-identified clip group, pairing its identity (child 0x2619: length-prefixed name + GUID) with its saved edit-window display/automation view state (child 0x200e). Lives among the session's playlists inside the top-level 0x2624 container, interleaved with 0x261c/0x261e track-playlist blocks (in the one instance: 71× 0x261c + 7× 0x261e + 1× 0x2623). Role inferred from the single available instance; the 0x200e view-chain fields look like display/view-state PT recomputes.
- **Confidence:** MEDIUM — SEVERE SAMPLING LIMITATION: 0x2623 appears in EXACTLY 1 instance in EXACTLY 1 session (MANOLITO SIMONET TEATRO MELLA.bak.018) across the entire local 26-session corpus (confirmed by both the descent enumerator and an independent raw byte-signature scan). The schema rests on a single observation; the '≥2 sessions / multiple instances' requirement is unmet locally. CONFIRMED on the single instance: block_type=0x0001; span=[z,z+7+size); parent=0x2624; parent u32-header == child-count (79==79); child order [0x2619, 0x200e]; 0x2619 = length-prefixed name + GUID(s) after `2A 00 00 00`; 0x200e has immediate child 0x2015. The 4-byte gap between children and the 10-byte trailer after 0x200e could be fixed constants or variable-length fields — unknown.

### 0x2619 — Generic per-object NAME + GUID record

- **Kind:** FORM-DEPENDENT (keyed by block_type, which tracks session vintage). SHORT form (block_type 0x0001, 1219/1877) is a LEAF with NO children. BIG form (block_type 0x0007=540, 0x0008=118) is a CONTAINER whose payload embeds exactly one nested 0x4301 (a 34-byte 'valid'/reference placeholder), 658/658. Parent is 0x102d for 1846/1877 (grandparent 0x261b, the track/name list); conductor tracks add a handful of 0x2718–0x271c parents; exactly 1 lone instance is under a 0x2623 (an incidental audio-clip name). block_type is uniform WITHIN a session (session-vintage correlated: 0x0001 older; 0x0007/0x0008 for the DWTS/MANOLITO/THE WIND family).
- **Size:** TWO regimes. SHORT form (block_type 1): payload_len == name_len + 23 EXACTLY (1219/1219), i.e. 4(name_len u32) + name_len + 7(flags) + 4(GUID tag) + 8(GUID). BIG form (block_type 7/8): payload_len ≈ 146–155 (658 instances), because after the single-GUID head it appends a second GUID copy, a zero-run with a `01 00 00 00` marker, the embedded 34-byte 0x4301 child, and a third GUID copy.
- **Fields (offsets payload-relative = zmark+9):**
  - `+0` u32: name_len.
  - `+4` char[name_len]: object name (ASCII, decoded 1877/1877). Examples: 'Audio 1', 'Click', 'Tempo', 'Meter', 'Key Signature', 'Verb 1', 'Inst 3', 'voc 1.dup1'.
  - `+(4+name_len)` 7 bytes: flags. Value `01 00 01 00 00 00 00` for ALL short-form (1219/1219) and most big-form; a `00 00 01 00 00 00 00` variant appears on 30 big-form conductor-track instances (so NOT fully constant).
  - `+(11+name_len)` u32: GUID tag = `2A 00 00 00` (1877/1877).
  - `+(15+name_len)` 8 bytes: GUID#1 (the object's GUID; unique per object within a session).
  - SHORT form ends here (block_type 1): total payload = name_len+23; tail after name = flags(7)+tag(4)+GUID(8) = 19 bytes. Single GUID only. LEAF.
  - BIG form only (block_type 7/8): `+(23+name_len)` u32 sep = `00 00 00 00`; `+(27+name_len)` u32 tag = `2A 00 00 00`; `+(31+name_len)` 8 bytes GUID#2 (byte-identical to GUID#1, 658/658); then a mostly-zero run containing a `01 00 00 00` marker; then an EMBEDDED nested 0x4301 child block (`5A 01 00 22 00 00 00 01 43 …`); then a final `2A 00 00 00` + 8-byte GUID#3, also byte-identical to GUID#1 (658/658). So the big form stores the GUID THREE times.
  - GUID note: the 8-byte GUID's high 4 bytes cluster per-session (a session/creator prefix), consistent with the project's `2A 00 00 00` GUID convention.
- **Notes:** A generic per-object name+GUID record — the identity payload inside a 0x102d name-wrapper. Any named object (audio/aux/MIDI track, sub-lane, conductor Tempo/Meter/Key/Chord, even audio clips) gets one; NOT specific to clip-groups. Matches the project's own docs (ptx-format-spec.md: '0x102D → 0x2619 → 0x4301' and 'a nested 0x2619, then an 8-byte track GUID after a 2A 00 00 00 tag'). Core project/session structure PT reads back (name table), not recomputed view-state.
- **Confidence:** HIGH (structure). The generic-name+GUID-under-0x102d role is high-confidence; the earlier clip-group/0x2623 role is REJECTED (only 1 of 1877 instances is under a 0x2623, and that is an audio-clip name). Verified across 19 corpus sessions = 1877 instances (Hipsters skipped). Parent-map result: parent=0x102d ×1846, plus 0x2718/0x2719/0x271a/0x271b/0x271c ×6 each (conductor tracks) and 0x2623 ×1.

### 0x200e — Clip-group display/view-state wrapper (CONTAINER)

- **Kind:** CONTAINER. block_type(u16@z+1)=0x0002 in every instance. Parent=0x2623 (verified 7/7 distinct sessions). Wraps exactly ONE immediate child, a 0x2015 deep view chain (0x2039/0x2037, 0x2105/0x2103, 0x2434, and a 0x2580→0x203b volume-automation-lane group when present). The 0x2015 child begins at payload offset 0 (no pre-child bytes) and is followed by exactly 8 trailing 0x00 bytes, so payload_size = 9 + child_0x2015_declared_size + 8.
- **Size:** payload_size (u32@z+3) is data-dependent: observed 160, 179, 237, 241, 256 across the corpus (256 by far the most common). Driven by the wrapped 0x2015 view chain: ~256 when the chain includes a 0x2580 volume-automation-lane group; ~179 when it does not; smaller/other values reflect fewer 0x2039 view-state lanes and/or the 0x2580 group. LOW-FREQUENCY, session-specific type — rare in distinct sessions (~1.5–1.8% in random samples); copied into every backup of a session that has it, so per-FILE counts are inflated (256 files / 303 instances across only ~3 known session families).
- **Fields (offsets payload-relative = zmark+9):**
  - `payload+0` — first and only child: the 0x2015 view-chain block; starts exactly at payload offset 0 (no header/prefix bytes of its own). Its span is 9 + its declared size. All size variation of 0x200e comes from here.
  - `payload+(9+child_size) .. end` — 8 fixed 0x00 bytes (reserved/padding); always all-zero across every instance and size variant.
- **Notes:** The second (last) child of a 0x2623 clip-group block (the 0x2623's two children are always [0x2619, 0x200e]); holds the group's saved edit-window view chain (track/lane display + automation lanes) as a single wrapped 0x2015 sub-block. Carries no meaningful data of its own beyond an 8-byte zero tail; its size is entirely a function of the wrapped 0x2015 view chain → PT recomputes it from display state. The 179-vs-256 delta is the 0x2580 automation-lane group; smaller variants (160/237/241) are driven by the number of 0x2039 view lanes as well. GUIDANCE: do not author fields inside it; copy the 0x2015 chain wholesale and append the 8 zero bytes.
- **Confidence:** HIGH. Verified on ≥6 DIFFERENT distinct sessions spanning ALL observed size classes: block_type=0x0002; parent=0x2623 with children exactly [0x2619,0x200e] and 0x200e at index 1 (7/7); exactly one child = 0x2015 at payload offset 0; 179-vs-256 delta is the 0x2580 group; payload_size = 9 + child_size + 8 exactly. NOTE: '102 instances across 83 sessions' style counts are per-file/per-backup, not distinct-session frequency (which is much lower).

### 0x2555 — Trailing footer/terminator of a 0x2551 edit-group (LEAF)

- **Kind:** LEAF (0 immediate children in the single instance found). parent = 0x2551; grandparent (0x2551's parent) = 0x2587. Both confirmed on ONE instance only.
- **Size:** In the ONE instance: block_type(u16@z+1)=0x0002, size field(u32@z+3)=7, payload length = 5 bytes (=size−2, since the size field covers the 2-byte content_type word plus payload). CANNOT confirm any multi-width table — this corpus has 1 instance total.
- **Fields (offsets payload-relative = zmark+9):**
  - Header layout (confirmed on the 1 instance): `5A`@z; block_type u16@z+1; size u32@z+3 (block span = [z, z+7+size]); content_type u16@z+7 = 0x2555; payload@z+9.
  - `+0` (bt=0x0002): payload = 5 bytes, all zero (`00 00 00 00 00`) in the single observed instance. Consistent with a reserved/all-zero footer, but only 1 data point.
  - UNVERIFIED: a proposed bt=0x0001 → 3-byte payload branch (with a 'packed ~20-bit field') and a bt=0x0003 → 6-byte payload branch. ZERO bt=1 and bt=3 instances exist in this corpus; cannot confirm or refute.
  - Pairing: the sibling 0x2554 immediately preceding this 0x2555 has block_type=0x000b (=11), consistent with a 'bt=11 pairs with 0x2555 bt=2/3' hypothesis — but only 1 example.
- **Notes:** A trailing footer/terminator sub-record of a 0x2551 track edit-group (region/clip grouping) block; the LAST of 0x2551's 6 children (order: 0x2552, 0x258b, 0x2552, 0x2553, 0x2554, 0x2555), immediately after the 0x2554 sibling. In the one instance available it is a small all-zero reserved field. The block_type↔payload-width version-tag hypothesis is plausible (block_type is a schema-version tag elsewhere in this format) but rests on a single observed width. Safe-to-copy guidance is reasonable for the observed all-zero bt=2 case; do NOT assume the bt=1/bt=3 branches.
- **Confidence:** LOW — for a corpus-coverage/honesty reason, NOT because anything was contradicted. Both the size-driven walk AND a raw 0x5A scan agree it appears exactly ONCE in the entire local corpus (26 sessions), solely in 'MANOLITO SIMONET TEATRO MELLA.bak.018', as bt=0x0002 / size=7 / payload `00 00 00 00 00`. Any broader frequency/width tables (bt=1 3-byte, bt=3 6-byte) are UNCONFIRMABLE here.

### 0x2298 — NOT A REAL CONTENT-TYPE (spurious decode of the file-header stub)

- **Kind:** N/A — a spurious decode of the fixed 11-byte file-header stub at file offset 0x14, framed `5A 01 00 04 00 00 00 <word:2> <payload:2>`. The two bytes at z+7 (the nominal content_type slot) are a per-file variable checksum-like value that only coincidentally read as 0x2298 in one corpus session (COGNAC). PT does not parse this stub as a block: `parse_version()` (core.py:452) hardcodes `parse_block_at(0x1F)` and reads the REAL header block at offset 0x1F (content_type 0x0003 or 0x2067). Deep enumeration over 19 diverse sessions found exactly ONE 0x2298 read total (the COGNAC stub at 0x14); zero real 0x2298 blocks anywhere in the block tree.
- **Size:** Stub size u32 = 4, total 11 bytes: span [0x14, 0x1F). Layout: 7-byte frame prefix (`5A` + block_type u16=1 + size u32=4) + 2-byte word at z+7 + 2-byte payload at z+9. The next block (real header) always begins exactly at z+11 = 0x1F (verified 19/19 and 800/800). The stub frame `5A 01 00 04 00 00 00` is constant in 19/19 loaded sessions and 800/800 sampled backup-corpus files; as a raw block its zmark=0x14, end=0x1F, parent=None, no children (the real header at 0x1F lies OUTSIDE its span).
- **Fields:**
  - `+0` (=z+7, 2 B): NOT a content_type. A per-file checksum-like word (content-derived: byte-identical files share it; each differing save changes it). Distinct in 784/800 sampled files. Reading it as content-type 0x2298 is a false positive present in exactly one session (COGNAC).
  - `+2` (=z+9, u16): a volatile per-file counter, NOT the session version. Ranges 0..823 across the corpus (mean ~77, 212 distinct values in 800 files); stable within a backup series, occasionally +1 on save. The 'session format version (05 00 = version 5)' reading is a COGNAC-only coincidence (COGNAC payload=5 is PT 11.3.2; Reverse Rewire payload=124/125 is PT 10.3.5; Quality Time payload=2 is PT 10.3.5). The real version is parsed from the 0x1F header block.
- **Notes:** Do NOT model 0x2298 as a block type. It is the offset-0x14 header stub whose z+7 word is a per-file (content-derived checksum-like) value that only reads as 0x2298 in one session. The z+9 word is a save-counter-like field, not the session format version. xor note: for xor_type=5, bytes < 0x1000 use key index 0 → key 0, so 0x14..0x1F are raw (verified byte-identical to on-disk); the same handling applies to xor_type=1 after unxor.
- **Confidence:** HIGH (refutation confirmed). Verified via 19 fully-loaded corpus sessions with deep block enumeration + 800 randomly-sampled files from the 43k-file backup corpus.
