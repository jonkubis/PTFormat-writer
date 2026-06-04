# Two generations of the Pro Tools `.ptx` writer

This document explains the two distinct approaches to **writing** Pro Tools session
files that have lived in this repo, why the second one exists, and — the crux —
**where the first one went wrong, especially around the master index and "holes."**

- **GEN-1** — the *spec-synthesis* writer engine (`write_audio_session` / `with_*` /
  `AudioSessionSpec`) with a heuristic index patcher (`_update_final_index`). This is the
  earlier, "Codex-era" approach. It now lives, deprecated, in `unused/`.
- **GEN-2** — the *control-grow* toolkit (`body_synth`) plus a **deterministic** index
  rebuilder (`final_index`, the "holes" model), and the beatmap→session conversion
  (`beatmap`, `wavecache`). This is the current, "Claude-era" library.

> **Provenance note.** The low-level *reader* (`core.py`) is a Python port of an existing
> open-source `.ptx` parser. Everything under "writing" — both generations — was
> added on top. The GEN-1/GEN-2 split is about *approach*, and maps onto the
> Codex-era → Claude-era timeline of this project; it is not a claim about any single
> commit's authorship.

---

## TL;DR

| | **GEN-1 (spec-synthesis)** | **GEN-2 (control-grow + holes)** |
|---|---|---|
| How a session is built | Assemble blocks from a typed spec (`AudioSessionSpec`) **from scratch** | Start from a **known-good PT control** session and splice/grow blocks |
| Source of truth | The spec + the code's model of the format | **Pro Tools itself** — byte-exact diffs of real control pairs |
| Master-index (`0x0002`) repair | `_update_final_index`: **scan & guess** which 4-byte values are offsets, rewrite them | `final_index`: **deterministically** rebuild every offset from the block layout |
| How offsets are identified | By **value** (`old_value in offset_map`) + a marker scan + special-cases | By **grammar** (the record structure) and **logical identity** (`content_type`, rank) |
| Failure mode | EOS / "magic ID does not match" at certain track counts & block types | None observed; validated byte-exact against real controls |
| Validation | Round-trips through the code's own model | **Reproduces real PT files byte-for-byte** (mod GUID/nonce/identity) |
| Status | Deprecated → `unused/writer_legacy.py` (+ `audit`, `mixed_order`, `midi`, `cli`) | The library (`ptxformatwriter.workbench` / `body_synth` / `beatmap` / `wavecache`) |

The single most important difference is the index. **GEN-1 guesses; GEN-2 derives.**
Guessing offsets is *undecidable* (a count field can equal a block's offset), and that
is precisely what corrupted files and produced Pro Tools' "end of stream" (EOS) and
"magic ID does not match" errors.

---

## 1. The shared substrate

Both generations sit on the same on-disk format (decoded by `core.py`):

- A `.ptx` file is **XOR-obfuscated**. `writer.load_unxored` de-obfuscates it;
  `writer.encrypt_session_data` re-applies the mask. (XOR type/seed live at bytes
  `0x12`/`0x13`.)
- The de-obfuscated body is a tree of **blocks**. Each block is
  `5A | btype:u16 | block_size:u32 | content_type:u16 | payload…`. A block's total
  length is `block_size + 7`.
- The session ends with the **master index**, a block of `content_type == 0x0002`. It is
  a *pointer table*: it references many other blocks **by absolute file offset**.

That last point is the whole ballgame. **Any edit to the body that changes a byte count
shifts the file offsets of everything after it — so the master index must be repaired.**
Get it wrong and Pro Tools reads a pointer into the middle of a block, then fails with
EOS or a magic-ID mismatch. This is "Pass 2" — and it is where the two generations
fundamentally diverge.

---

## 2. GEN-1 — spec-synthesis + the offset *guesser*

### The build model

GEN-1 builds a session **from a typed specification**:

```python
session = write_audio_session(AudioSessionSpec(tracks=[AudioTrackSpec(...), ...]))
```

It synthesizes each block (track lists, region lists, placements, tempo/meter) from the
spec and a seed template, then calls `_update_final_index(new_data, old_data)` to fix the
master index. The intent is reasonable; the index step is where it breaks.

### How `_update_final_index` works (`unused/writer_legacy.py`)

1. **Diff the block layouts** of the old vs new body to build an `offset_map`
   (`old_offset → new_offset`) and an `end_offset_map`.
2. **Marker scan.** Walk the index looking for the literal byte marker
   `_FINAL_INDEX_OFFSET_MARKER = 01 04 00 01 00`. The 4 bytes *after* a marker are
   treated as an offset and rewritten via `offset_map`.
3. **Unmarked offsets.** For 4-byte values *not* behind a marker, patch them too — *if*
   `old_value in offset_map` (i.e., the stored value happens to equal some old block
   offset). This is the guess.
4. **Band-aids.** Because step 3 is unsafe, it is hedged with special-cases:
   - `FINAL_INDEX_PATCH_OCCURRENCE_LIMIT = 64` and `_FINAL_INDEX_LARGE_UNMARKED_SKIP_TYPES
     = {0x2624}` — skip patching some record types once there are "too many."
   - `_FINAL_INDEX_2519_CHILD_REF_TYPES = {0x251A, 0x251B, 0x251C, 0x2716}` and
     `_FINAL_INDEX_2624_END_REF_TYPES = {0x261B}` — hand-tuned rules for which child-refs
     in `0x2519`/`0x2624` records are "really" offsets.
   - Per-value `occurrence_counts` to disambiguate repeats.

### Why this is fundamentally wrong

> The index references blocks by absolute offset. A 4-byte field elsewhere in the index
> (a **count**, a **length**, an **enum**, a sample position) can hold a value that
> *coincidentally equals a real block's offset*. There is no local, value-based way to
> tell "this u32 is an offset to patch" from "this u32 is a scalar that must not change."
> **The classification is undecidable from the value.**

So `old_value in offset_map` mis-fires: it rewrites scalars that merely *looked* like
offsets, corrupting records. The marker `01 04 00 01 00` covers the common, easy holes;
everything else is guesswork propped up by occurrence limits and per-type skip rules —
each of which is a patch for a specific configuration that broke, not a general law.

The observed symptoms (recorded throughout `unused/docs/`):
- **Corrupted records** — e.g. `0x2587`, `0x2624` at certain track counts.
- Pro Tools **"end of stream encountered"** on open, or **"magic ID does not match while
  translating …"** — both are downstream of an index pointer landing mid-block.
- The fix-of-the-week pattern: add another skip-type / occurrence cap / special-case,
  which moves the breakage to the next configuration.

`final_index`'s own docstring states it plainly: the historical approach "tries to *guess*
which 4-byte values are offsets and rewrite them, which is undecidable (a scalar can equal
a block offset) and corrupts files."

---

## 3. GEN-2 — control-grow + the *holes* model

### The build model

GEN-2 never synthesizes a session from a blank spec. It **starts from a real Pro Tools
control session** (one PT itself wrote and round-trips cleanly) and performs a *minimal,
byte-exact* transform: grow a block, splice in a clip, append a track. The tools live in
`body_synth` (`add_audio_clip`, `set_tempo_map`, `set_markers`, `add_click_anyN`,
`build_audio_clips`, `rename_track`, …) and the conversion in `beatmap`/`wavecache`.

The guiding rule is **"Pro Tools is the only oracle"**: a transform is correct only when
it **reproduces a real control byte-for-byte** (modulo GUIDs/nonces/identity). `load()
returning 0` from the lenient reader is *not* acceptance — only PT opening the file is.

### The index, done right: "holes"

A **hole** is a position in the master index that holds a block offset. GEN-2's insight is
to identify holes and their targets **structurally**, never by value:

- **A hole is found by grammar, not by scanning for offset-looking values.**
  `parse_records` frames the index into records; a hole is then either:
  - a **childref** — the u32 in a container record's child-reference, or
  - a **marker element** — one of `k` u32 offsets in a marker/table element
    (`k == 1` is the familiar `01 04 00 01 00 <off>`; `k > 1` is an offset table).
- **A hole's target is identified by *logical identity*, never by its stored value.**
  Each hole records `(content_type, rank)`: the type of the block it points at, and that
  block's rank **among blocks of the same type in file-offset order**.

Concretely, `offset_holes(data)` returns, per hole:

```
(abs_pos, value, target_type, target_rank, kind)
   abs_pos      absolute byte position of the u32 in the file
   value        the offset currently stored there
   target_type  content_type of the block it points at
   target_rank  index of that block among blocks of target_type (offset order)
   kind         "marker" | "childref"
```

To repair after an edit, GEN-2 simply **re-resolves each hole's `(content_type, rank)`
against the new block layout** and writes the fresh offset. No value matching, no marker
heuristics, no skip-types.

### Two repair entry points

- `rebuild_index_offsets(data)` — recompute every hole in place from the current layout.
- `reindex_after_resize(consistent_data, resized_data)` — the workhorse for content
  insertion. After a body resize the stored offsets can be *so* stale that the index can
  no longer even be parsed (framing a child-ref requires its offset to land on a live
  `5A` zmark). So it captures the holes from the **consistent** input (whose index still
  parses), then refills each at its **stable index-relative position** with the target's
  new offset, resolved by `(content_type, rank)` in the resized layout. Its docstring:
  *"No value-guessing, no re-parsing the stale index … the robust replacement for the
  legacy `writer._update_final_index` offset guesser (which corrupts records like 0x2587
  at some configs → Pro Tools EOS / magic-ID)."*

### When records are added/removed, not just resized

`reindex_after_resize` assumes the set of indexed `(content_type, rank)` records is
preserved (true for *content insertion* like clips/tempo/markers). When the **track
count** changes, records must be added: `add_track` / `add_stereo_track` /
`add_click_track` clone an index record for the new track (blank-cloning a template,
fixing ordinals and child-ref/childtype), and `compose_index` stitches a donor's records
onto the target. `body_synth` builds on these (`_synth_clip_index`, `_grow_blocks_from`)
to add exactly the right holes — e.g. N clips that share one audio-files path still add
exactly **one** `0x0f3c` marker.

**The library uses *zero* of GEN-1's index code.** Verified: nothing in
`body_synth`/`beatmap`/`wavecache`/`final_index` calls `_update_final_index`,
`_final_index_records`, or the offset-guesser helpers. After cleanup, `writer.py` is ~95
lines of pure I/O primitives; the guesser is archived in `unused/writer_legacy.py`.

---

## 4. Where GEN-1 got it wrong — itemized

1. **Guessing offsets is undecidable.** `old_value in offset_map` cannot distinguish an
   offset from a coincidental scalar. GEN-2 never inspects the stored value to decide what
   a field *is* — the grammar says childref-or-marker, and identity says which block.

2. **Marker-scanning is incomplete.** `01 04 00 01 00` finds the easy `k==1` holes but
   misses `k>1` offset *tables* and container child-refs, which GEN-1 then tried to catch
   by value-guessing (see #1).

3. **Special-cases don't generalize.** Occurrence limits (`> 64`), per-type skip sets
   (`0x2624`), and bespoke `0x2519` child-ref rules are evidence of the model failing
   per-configuration. Each is a symptom, not a fix; the breakage just moves to the next
   track count or block type.

4. **Record corruption → EOS / magic-ID.** When a scalar inside a record (e.g. in
   `0x2587` or a `0x2624` table) is wrongly rewritten, the record's framing is destroyed.
   Pro Tools then reads a pointer mid-block and fails to open. GEN-2's deterministic
   refill cannot do this — it only ever writes offsets into known offset holes.

5. **Synthesizing from a spec drifts from PT's real bytes.** Building blocks from a model
   of the format (rather than from a real PT session) accumulates small layout
   differences that PT rejects at scale. GEN-2 sidesteps this entirely by transforming a
   real control and *proving* the result byte-exact.

6. **"The reader opened it" is a false signal.** The lenient `core` reader (and the GEN-1
   self-round-trip) accept files Pro Tools rejects. GEN-1's confidence came from the wrong
   oracle. GEN-2 treats only Pro Tools as ground truth.

---

## 5. The philosophy difference (why GEN-2 is robust)

- **Pro Tools is the only oracle.** Every GEN-2 transform is validated by reproducing a
  real control session byte-for-byte (mod GUID/nonce/identity) and, ultimately, by Pro
  Tools opening the output. Examples from the build history: clip placement, tempo/meter
  maps at scale, multi-clip region linking, the waveform `WaveCache.wfm`, and click-on-top
  were each confirmed against real controls and PT loads.
- **Derive, don't guess.** Offsets come from the block layout; region→file links from the
  decoded `findex`; waveform peaks from `clip(round(sample/256))`. No field is patched on
  a hunch.
- **Grow from known-good, minimally.** Transforms change the smallest possible set of
  blocks and leave the rest of a real session untouched, so the result stays inside the
  envelope Pro Tools already accepts.
- **Every bug becomes a guard test.** The decode war-stories (EOS, magic-ID, the
  all-tracks-play-bass `findex` bug, the truncated-region-length bug) are pinned by tests
  so the same wrong turn can't recur.

---

## 6. What this means for the codebase today

- **Use:** `ptxformatwriter.workbench` (curated API) → `body_synth` tools, `beatmap` conversion,
  `wavecache`, `final_index` (holes), `core` (reader), `writer` (I/O primitives).
- **Deprecated (archived in `unused/`, reference-only):** `writer_legacy.py` (the GEN-1
  spec engine + offset guesser), `audit.py`, `mixed_order.py`, `midi.py`, `cli.py` /
  `__main__.py`, and the GEN-1 tests under `unused/tests/`.
- The deprecated code is **not import-clean from `unused/`** (its internal imports expect
  the old `writer` layout); it is preserved for reference and is recoverable from git if
  ever needed.

### Pointers

- The index grammar in detail: `unused/docs/final-index-0x0002-schema.md`.
- The holes implementation: `ptxformatwriter/final_index.py` (`offset_holes`,
  `reindex_after_resize`, `block_layout`, `parse_records`).
- The deprecated guesser: `unused/writer_legacy.py` (`_update_final_index`,
  `_final_index_records`, `_build_offset_maps`).
- The construction toolkit: `ptxformatwriter/body_synth.py`; the public surface:
  `ptxformatwriter/workbench.py`.
