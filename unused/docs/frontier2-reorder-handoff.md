# Frontier 2 — reorder the Click to the TOP (self-contained handoff, 2026-05-31)

>>> **RESOLVED 2026-05-31 — see `docs/frontier2-clickontop-findings-2026-05-31.md`.** <<<
> The plan below (build-then-reorder: permute body blocks + remap index) was the WRONG
> model — reordering body/index does NOT move the edit-window display (PT-load-confirmed).
> The actual field: edit-window track order is a PLAYLIST-ORDER LIST in the master index
> (0x2624 count==1 container `elements[1:]` + count==4 instance `child_refs[1]`). 
> `body_synth.move_click_to_top` / `add_click_anyN(at_top=True)` implement it; PT-CONFIRMED
> N=2 & N=12. Notes below kept for the body-reorder mechanics (part of the recipe) + controls.

Read THIS first. It's everything needed to build Frontier 2 from a fresh context. Deep history
is in `docs/click-on-top-findings-2026-05-30.md`; the project index is the auto-memory.

## The goal
The user wants a Click track at the **TOP** of a session. `body_synth.add_click_anyN(...)`
already puts a click at the **BOTTOM** of an any-N audio session (PT-confirmed). Frontier 2 =
move that click from the bottom to position 1. This is **general track reordering** (move the
last track to the front), applied to the click. The decode below is done; a multi-step BUILD remains.

## Run / validate basics (do this exactly)
- Tests: `python3 -m unittest discover -s tests` from repo root. MUST use `python3` (pyenv
  `python` errors). ~2 min. Currently **71 tests green** (skipped=1). Touch no production code
  without keeping them green.
- Load a session in the (lenient) reader:
  `from ptxformatwriter.core import PTFFormat; f=PTFFormat(); rc=f.load(path,48000); raw=f.unxored_data()`.
- Write a session for PT: `import ptxformatwriter.writer as W; open(p,'wb').write(W.encrypt_session_data(raw))`.
- **CRITICAL: our Python reader is LENIENT — `rc==0` does NOT mean Pro Tools loads it.** PT is the
  only real oracle (the user loads the file). "end of stream" (EOS) in PT = a length/size field
  that makes PT read past data; our reader only bounds reads to the whole file, so it misses it.
- **Methodology (the user insists):** validate every formula/transform by reproducing a REAL
  control byte-exact before calling it solved, and SHOW THE BYTES. Don't claim PT-loads without
  a PT load.

## Controls (in `control_files/lots of stereo tracks/`)
- `{1,2,3,4} stereo tracks.ptx`           — clean audio, N=1..4 (also 0..24, + 512).
- `{1,2,3,4} stereo plus click.ptx`       — same audio + a Click at the BOTTOM.
- `1 stereo plus click on top.ptx`, `2 stereo plus click on top.ptx` — click at the TOP.
  >>> These two TOP controls are the Frontier-2 validation targets. <<<
- `add_click_anyN` was validated vs the click-at-bottom controls; the test
  `tests/test_click.py::test_add_click_anyN_cross_layout` checks the body 0x2519 byte-exact.

## Neutralization for byte-exact comparison (cosmetics that legitimately differ per save)
- XOR seed: bytes 0x12/0x13 (zero them).
- Per-track GUIDs: zero the 8 bytes after EACH `2a 00 00 00` (`b.find(b"\x2a\x00\x00\x00")`).
- Session name: the `0x2067` block (filename "N stereo tracks" vs "...plus click on top") — IGNORE.
- Cumulative counters (the ~10-u32 runs in 0x261b/0x261c) — LOAD-INNOCENT (confirmed). They
  change with track position but don't affect loading; don't chase them for byte-exactness.

## THE TRANSFORM (decoded: `2 stereo plus click.ptx` bottom vs `...on top.ptx`)
393 blocks relocate intact (mod GUID); the rest are position-shifted per-track blocks + small
cosmetic deltas. Moving the click (last track) to the front =
1. **Per-track block runs -> click's block to the FRONT.** The click's `0x261e` playlist
   (+ its nested routing/view subtrees: `0x260d/0x260c/0x260a`, `0x200a/0x200b/0x2015`), its
   `0x261b` counter, and `0x2627` move to the front of their runs. body order
   `[Apl,Apl,Cpl]`->`[Cpl,Apl,Apl]` for 0x261c/0x261e. **These blocks are NESTED inside big
   containers (0x4501 / 0x1022 / 0x2031 …), NOT top-level** — so the move is a per-container
   CHILD reorder, not a flat byte splice. (Verify each block's parent via the block tree.)
2. **Lane-major `0x251a`** (children of `0x2519`): the lanes are laid out
   `[lane-0 of all tracks][lane-1 of all tracks]`. Move the click's lane-0 to the FRONT of the
   lane-0 group and its lane-1 to the FRONT of the lane-1 group.
3. **Own-byte regions:** move the "Click 1" name-table entry (in `0x2519` own-bytes) and the
   click's `0x2589` overview entry to the FRONT of their regions.
4. **INDEX (clean, the easy part):** the `0x2624` playlist-instance records reorder by ordinal:
   bottom `[1->261c, 2->261c, 3->261e]` becomes top `[1->261e, 2->261c, 3->261c]` (the click's
   0x261e childref goes to ordinal 1, audio shift to 2..N+1). And **every index offset must be
   remapped** to the relocated blocks (you know the old->new map because YOU move the blocks).
5. **Cosmetic riders (don't block on these):** counters (0x261b), view-state (0x200a/0x2015,
   and a `0x200b`->`0x200d` block-TYPE flip at 1-audio) — load-innocent; copy/leave as-is.

Block sizes do NOT change (intra-run/intra-container rearrangement); only positions change, so
no container `block_size` needs bumping — but the index offsets do (blocks moved).

## Why build-then-reorder (not diff-replay)
The Frontier-1 trick (recursive structural diff `click_clone.derive_click_patch`) is APPEND-
oriented; deriving from a click-TOP control gives a DEGENERATE diff (the click is at the front,
so the diff diverges immediately into a huge suffix — confirmed). So: build click-at-bottom with
`add_click_anyN`, then REORDER (move last track to front). The reorder is a block PERMUTATION +
index offset-remap + index-instance reorder.

## Implementation plan
1. `target = clean N-stereo` (or any audio session); `bottom = body_synth.add_click_anyN(target, clean2, click2)`
   where `clean2/click2 = 2 stereo {tracks, plus click}.ptx`.
2. Write `move_last_track_to_front(bottom) -> reordered`:
   - Parse the block tree (`click_clone._nodes` gives parent links; `body_synth.flat_blocks`).
   - For each per-track run (the click's block is the LAST / named "Click 1"), relocate the
     click's block to the front of that run WITHIN its container. Handle the lane-major 0x251a
     split. Move the name-table + overview entries.
   - Build the old->new offset map from the moves; remap EVERY index offset (childref + marker).
     `final_index.offset_holes(data)` enumerates them; or rebuild by mapping each hole's stored
     value through old->new.
   - Reorder the `0x2624` playlist-instance records (click childref 0x261e to ordinal 1; renumber
     audio ordinals to 2..N+1). Re-serialize the index. Fix the index-offset pointer
     (`body_synth._set_index_offset`).
3. **Validate (byte-exact, the gate):** `move_last_track_to_front(2-click-bottom)` should equal
   `2 stereo plus click on top.ptx` (and the 1-audio one) modulo {GUID, counters, session name}.
   Diff block-by-block (align by content_type+name+occ; neutralize GUIDs); the only diffs allowed
   are the cosmetic riders. SHOW THE BYTES.
4. Once 1&2 validate: build a **12-stereo** click-at-top (`add_click_anyN` then reorder), confirm
   our-reader rc==0 + index holes resolve + body order has the click first, write it, and have
   the USER load it in Pro Tools. (Frontier 1's 12-stereo click-at-bottom = `add_click_anyN(load("12 stereo tracks.ptx"), clean2, click2)`.)
5. Fold into production: `body_synth.add_click_anyN(..., at_top=True)` or a `move_track_to_front`
   in body_synth + a test (1/2-audio byte-exact mod cosmetics). Update docs/memory.

## Key code map
- `ptxformatwriter/click_clone.py`: `_nodes(body)` (block tree w/ parent links + names), `_sigs(nd)`
  (owner signatures), `_merge_reps`/`_finish_click` (body merge + counts/index tail),
  `derive_click_patch_structural`/`apply_click_patch_structural` (the Frontier-1 cross-N click,
  incl. `_restamp_track_count`, `_insert_click_2519`, `_per_track_templates`). `_NAME` regex
  matches "Audio|Click|MIDI N". `_chain(nd, z)` = a block + its ancestors.
- `ptxformatwriter/body_synth.py`: `parse`, `flat_blocks`, `_by_type(ptf, ct)`, `_parent_zmarks(ptf)`,
  `apply_insertions(data, ptf, [Insertion(offset, blob, parent_zmark)])`, `Insertion`,
  `track_types(data)`, `channel_count`, `_patch_counts`, `_set_index_offset`,
  `add_click`/`add_click_anyN`, `_name_table_entries`, `_clear_donor_window_markers`.
- `ptxformatwriter/final_index.py`: `compose_index`, `block_layout(data)->(zmark->type, type->[zmarks])`,
  `parse_records`, `serialize_final_block`, `offset_holes(data)`, `rebuild_index_offsets`,
  `final_index_ref(data).start`.
- `ptxformatwriter/core.py`: the reader (`PTFFormat.load`); block format = ZMARK `0x5a`, `block_type:u16`,
  `block_size:u32`, `content_type:u16`; content starts at `b.offset` (= zmark+7).

## Gotchas (paid for in blood this session)
- Reader is lenient -> PT is the oracle. Confirm with a PT load.
- Name-table entries carry an inter-entry `02 00` field and a u16 track-count after each
  `2a 00 00 00 <8B GUID>` (= track count); reorder/insert must preserve/restamp these.
- The 0x2519 name table has an entry-count at content-rel offset 16 (`_patch_counts` sets it).
- The first top-level block stores the index's absolute offset (low-16 looks like a "content
  type"); always `_set_index_offset` after any body-size change. (No size change here, but the
  index MOVES if you reorder blocks within the body? No — reorder is size-neutral, the index
  start is unchanged; but every offset INTO the body changes, hence the remap.)
- Don't trust per-type block COUNTS to catch reorder bugs — counts are unchanged by a reorder;
  diff block POSITIONS/content and validate byte-exact vs the TOP control.

## Confirmed-loadable artifacts kept
`control_files/test_clickbottom_{3,12}stereo.ptx` (Frontier-1 click-at-bottom, PT-confirmed).
