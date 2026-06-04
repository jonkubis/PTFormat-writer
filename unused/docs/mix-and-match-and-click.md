# Mix-and-match synthesis (mono/stereo) + the click build — STATE 2026-05-29

This is the **current, clean reference** for arbitrary track-type synthesis. The
blow-by-blow decode history is in `track-types-pass1.md`; the empty-stereo
foundation is in `body-synthesis-pass1.md`; the index machinery is in
`final-index-0x0002-schema.md`. Read this one first to continue.

## ✅ STATUS

| capability | state |
|---|---|
| empty **stereo** 1–16 | PT-confirmed |
| empty **mono** any N | PT-confirmed (`synthesize_mono_session`) |
| **arbitrary mono+stereo mix-and-match**, any count/order | **PT-confirmed** (`synthesize_mixed_session`) — no control needed |
| **MIDI** track | footprint decoded (0x1057 header + 0x2620 playlist); not built |
| **click** track | **fully decoded; build spec below**; not built |

63 tests green (`python3 -m unittest discover -s tests`, ~120s, MUST use `python3`).

## The synthesis API (`ptxformatwriter/body_synth.py`)

- `synthesize_stereo_session(donor, base_n, target_n, library, library_total)` — grow empty-stereo.
- `synthesize_mono_session(...)` — same, mono (1 lane/track, `0x1054`=N).
- **`synthesize_mixed_session(specs, donor_data, mono_lib, stereo_lib, overview_order=None, target_leaf=None)`**
  - `specs`: per-track channel counts in display order — `1`=mono, `2`=stereo (e.g. `[2,1,2]`).
  - `donor_data`: a session whose first `base_n` tracks already match `specs[:base_n]`;
    `base_n` = its track count and **must be ≥2** (the 1→2 grow has no `0x2589` anchor).
    The four 2-track donors all exist: `2 mono`, `2 stereo`, `mono stereo`, `stereo mono`.
  - `mono_lib`/`stereo_lib`: `(data, total)` uniform controls (≥N tracks) to source each
    added track's unit from, by index; **one of them must have `total == N`** for the
    name-table transplant.
  - `overview_order`: defaults to identity `[0..N-1]` (order is cosmetic — see below).
  - `target_leaf`: session-folder name to path-normalize added tracks to; defaults to the donor's leaf.
- Helpers: `track_types(data) -> [TrackInfo(name, kind, channels, offset)]` (kind=mono/stereo/midi),
  `channel_count(data)`, `set_track_channels(data, track_offset, channels)`,
  `track_channel_indices(data, track_offset)`, `_folder_leaf(data)`,
  `extract_track(data, track, total, channels)`, `grow_one_track(data, base_n, unit)`,
  `rename_track`, `set_session_name`, `session_name`.
- Index (`ptxformatwriter/final_index.py`): `compose_index(donor, target_body, base, target, channels=2)`,
  `synthesize_index_records(..., channels=2)`, `add_track(records, new_count, channels=2)` —
  **`channels` accepts an int OR a per-track list** (e.g. `[2,1,2]`) for mixed builds.

## Key decoded facts (the breakthroughs)

1. **Channel allocation is cumulative by track order.** Track k's channels =
   `[C, …, C+channels_k-1]` where `C = sum(channels of tracks before k)`. E.g.
   `[S,M,S]` → `[0,1]`, `[2]`, `[3,4]`.
2. **Channel indices live ONLY in `0x1014`** (the audio channel map). The big
   `0x261c` playlist does NOT encode the absolute channel (its per-track diffs are
   GUID/ordinal data, never the channel). To place a track at any channel, rewrite
   just `0x1014` via `set_track_channels`. Byte positions (within the 0x1014 block):
   - `chan0` pre-marker = `block + namelen + 11`; `chan0` post-ID = `block + marker + 19`
     where `marker = block.find(b"\x2a\x00\x00\x00")`.
   - stereo `chan1` pre = `block + namelen + 13`; `chan1` post = `block + marker + 23`.
   - These are marker/namelen-relative so they're correct for **both** mono and
     stereo (a mono `0x1014` is 2 bytes shorter pre-marker → its post channel sits
     at a different absolute offset). Hard-coding stereo offsets corrupts mono.
3. **Count fields** (decouple under mixed):
   - `0x1054 + 2` = **total audio channels** (mono+1, stereo+2, MIDI/click+0). A
     too-small value → Pro Tools "end of stream". `grow_one_track` sets it from
     `channel_count(body)` (correct for uniform AND mixed).
   - `0x1015 + 2` = **audio-track count** (excludes MIDI/click).
   - `0x2107 + 11` = **total-track count** (includes MIDI/click).
4. **The overview display-order is COSMETIC** — Pro Tools accepts any valid
   permutation of `0..N-1` (confirmed: `synth_3_mono` loaded with `[1,0,2]`,
   `[0,1,2]`, and `[2,1,0]`). It's a hash-iteration order with no closed form and
   is NOT a function of N (mono n4 `[1,0,3,2]` ≠ stereo n4 `[2,3,1,0]`), but since
   it's cosmetic, `synthesize_mixed_session` defaults to identity. No control needed.
5. **Mono = stereo minus one channel** (3 block edits + 1 field): `0x1014` 52B not
   58B (1 channel), one `0x1052` lane not two, `0x261c` empties the 2nd `0x260c`
   output-routing block (sz55→sz16, −39B), and `0x1054`=N not 2N. `0x251a`/`0x210b`
   are identical mono/stereo (the 78/48 vs 79/49 diffs were MIDI-vs-audio).
6. **Cumulative counters are NOT load-critical.** The ~10 consecutive u16 in each
   track's `0x261b` (a global session counter, 10/track) can be inconsistent/broken
   and Pro Tools still loads (confirmed across many tests). Earlier "end of stream"
   was the path-wrapper (below), never the counters.

## Path-normalization + the hidden wrapper (the "end of stream" saga)

The embedded session path is a chain of length-prefixed ASCII components
(`<u32 len><name>…`) inside `0x261c`/`0x2067`/`0x200b`, ending in the **session
folder leaf** (e.g. `mixed tracks`). `_folder_leaf` finds it as the consecutive
component chain with the most total chars (volume-independent — controls live on
both `Macintosh HD` and the Dropbox volume; beats coincidental `&&`=`0x2626` runs).

Combining tracks from different control folders (mono vs stereo) would make a path
**chimera** Pro Tools rejects, so the added tracks' leaf is renamed to one common
leaf via `rename_track`. **THE BUG that cost the most:** the path is wrapped by
content-internal **`5a 0a 00 <u32 size>`** length fields that *neither* the block
parser *nor* `final_index.block_layout` recognizes (they're raw data inside a
block). `rename_track` originally bumped only parsed block sizes, leaving a wrapper
stale (e.g. 487 vs 480 after a −7 leaf shrink) → Pro Tools reads past the end →
**"end of stream."** `rename_track` now also scans for these `5a 0a 00` wrappers
and bumps any that span a renamed occurrence.

`synthesize_mixed_session` renames **only the source-folder leaves of the ADDED
tracks** (never the donor's leaf or its stale `0x2067` template remnants — those
have *their own* wrapper that `rename_track` doesn't fix, and they're harmless if
left alone since the donor loads with them).

**TODO (arbitrary user-chosen session folder):** a `target_leaf` ≠ the donor's leaf
would require renaming the donor's tracks too, which hits the `0x2067` path-wrapper
that `rename_track` doesn't yet handle. Decode/fix that wrapper (or write the path
consistently from scratch) to support user-named session folders.

## Bug/lesson log (don't repeat these)

- **Validate the synthesized INDEX byte-exact against the real control**, not just a
  `ptxformatwriter` reload — ptxformatwriter is lenient and ignores the positional checks Pro
  Tools enforces (this hid the mono index bug and the path-wrapper bug for rounds).
- `0x1054` = total channels, NOT 2N (uniform-only). Too small → "end of stream".
- `add_track` baked in "2 markers/track" for `0x1054` — now `channels`-aware (1 for mono).
- `extract_track`'s `0x1052` lane base must be the **cumulative** channel count of
  preceding tracks, not `channels*(k-1)` (only true for uniform sources).
- Mono `0x1014` channel positions ≠ stereo's (marker-relative; see fact 2).

## THE CLICK BUILD SPEC (fully decoded; ready to implement)

Controls: `control_files/various/click only.ptx` (just a click), `stereo click.ptx`
(1 stereo + click). Click track unit derived from `stereo click` − `1 stereo`.

**What a click track is:** named **`Click 1`**; its playlist is block **`0x261e`**
(analog of audio `0x261c` / MIDI `0x2620`; same `0x261b→0x102d→0x2619` subtree, with
`19 26 07000000 "Click 1"`), parented by **`0x2624`** (the SAME container as audio
`0x261c`). It has **2× `0x251a` lanes** + `0x210b` + `0x2619` + `0x4420`×2 + `0x2589`,
but **no `0x1014`, no `0x1052`** → **0 channels**. It is **NOT** in the `0x2519`
own-byte name table (which holds only mono/stereo audio names). It embeds the
**DigiClick plugin** in two parts:
- the plugin **instance** `0x2616→0x2615→0x2613→0x1038` lives INSIDE the `0x261e`
  subtree (comes free when you clone `0x261e`);
- a **session-level registration** `0x2064→0x1000` under **`0x2027`** near the file
  top — the click's distinguishing extra insertion point (`Digiclik…` data).

**Placement:** the click is the **LAST track**. `0x251a` is lane-major
`[Audio…, Click 1]` per lane group; `0x2624` children are `[0x261c…, 0x261e]`.

**Counts** (1 stereo → stereo click): `0x2107 + 11` (total tracks) **+1**,
`0x2624 + 2` (playlist count) **+1**; `0x1015` (audio) and `0x1054` (channels)
**unchanged**.

**BODY — `grow_one_click(body)`** (append click as the last track of an all-audio
session, which keeps the lanes consistent — both lane inserts land at the end of
their lane group):
- clone `0x261e` → append to `0x2624` (brings the plugin instance);
- `0x251a`×2 lane-major: lane0 at `a51[ntracks-1]`, lane1 at end (same as `grow_one_track`);
- `0x210b`, `0x2619`, `0x2589` → append;
- session-level `0x2064→0x1000` → insert into `0x2027`;
- **skip** `0x1014`/`0x1052` and the `0x2519` name entry;
- patch counts: `0x2107`+1, `0x2624`-count+1; do NOT touch `0x1015`/`0x1054`.

**INDEX — `add_click_track(records, new_count)`** (a variant of `add_track`):
- **skip** the `0x1015` marker (no `0x1014`) and the `0x1054` markers (0 channels);
- append a marker to the `0x2519` (count==1) container and to the `0x2624`
  (count==1) container (+1 each);
- grow the `0x2519` table (count==2, flag==0) offset list by +1;
- insert a `0x251a` **lane instance** (`0x2519 count==2 flag==1 → 0x251a`, like audio);
- insert a `0x2624` **playlist instance** (count==4) whose childref **child_type is
  `0x261E`** (audio's is `0x261c`) — clone the audio template and switch the type,
  or clone from a click control's index.
- `0x251b`/`0x251c`/`0x2716` ordinals = track count (as `add_track` does).

**INDEX fill — the one real gotcha:** `_fill_offsets` resolves new lane/playlist
instances by track *name* (`by_name_lane` / `remap_newtrack` hard-code
`"Audio {k}"`). The click is `"Click 1"`. Two options:
1. make `_fill_offsets` track-name-aware (pass a per-track name list), or
2. resolve the click's instances via `pop_new(child_type)` — since the click is
   added LAST, `pop_new(0x261E)` (a click-only type) and the last `pop_new(0x251A)`
   give the click's blocks. (Verify ordering when audio + click are composed together.)

**Validate** by reproducing `stereo click` from `1 stereo` (add one click): match
per-type block counts (modulo benign `0x0000` undo-junk), `bad_offsets==0`, reload
rc=0, then Pro Tools load. Then fold a `"click"` kind into `synthesize_mixed_session`
(channels=0, placed last). Note: Pro Tools has ONE click track per session.
