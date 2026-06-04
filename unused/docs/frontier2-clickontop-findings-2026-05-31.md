# Frontier 2 — Click on TOP: SOLVED & PT-CONFIRMED (2026-05-31)

`body_synth.move_click_to_top(data)` and `body_synth.add_click_anyN(..., at_top=True)`
place the Click track at the TOP of the edit window. **PT-CONFIRMED at N=2 and N=12
(stereo).** Byte-exact vs the real `2 stereo plus click on top.ptx` control's display
order; the production code reproduces both the 2-stereo and 12-stereo confirmed files
byte-for-byte. Tests: `tests/test_click.py::test_move_click_to_top` +
`test_add_click_anyN_at_top` (73 tests green total).

## THE FIELD (the whole hunt came down to this)
**Pro Tools stores the EDIT-WINDOW track display order as a PLAYLIST-ORDER LIST in the
master index (0x0002)** — specifically:
- the **`0x2624` count==1 flag==1 container** record's `elements[1:]` point at the
  playlists IN DISPLAY ORDER, and
- each **`0x2624` count==4 instance** record's `child_refs[1]` (the per-ordinal ref)
  points at its display-position playlist.

PT sorts the edit window by this list. For click-on-top, both must list the click's
`0x261e` playlist FIRST. At N=2 the entire difference between a click-bottom and a
click-top session (after neutralizing cosmetics) was **6 offsets / 12 bytes** here.

## What it is NOT (all ruled out by real Pro Tools loads — do not chase these again)
- **Body block order** — reordering the click's blocks to the front: loads, click still bottom.
- **Index playlist-instance ORDER / ordinals** (rotating the 0x2624 count==4 records): no effect.
- **Overview `<order>` u16** (0x2589): the overview STRIP order, not the edit window.
- **Window-config `0x2587` / `0x2551`** (order markers + a packed bitfield): reverting
  it on a working top control did NOT drop the click. Pure saved window/scroll state.
- **Per-track cumulative counters** (0x261b `xx35`/`xx62` runs): load-innocent AND
  display-innocent. Reassigning them click-first did nothing.
- **Per-track view-state** (0x200a/0x200b/0x200d/0x2015 chains): the 0x200d "view" block
  is present in BOTH click-bottom and click-top (a heuristic-scanner artifact made it
  look added); not the field. The click's 0x261e is FIXED-SIZE with a trailing 0x200d
  placeholder region — don't try to add/remove it (size-changing → "unexpected stream type").
- **0x2067 session-info**: transplanting it did not move the click.

## How it was decoded (the method that finally worked)
The two existing controls (`N stereo plus click.ptx` vs `...on top.ptx`) differed in too
many incidental ways (counters, window-state, view-state, filename, GUIDs) to isolate the
field — every single-field experiment failed. The breakthrough: the USER **dragged the
click to the top in a byte-identical SYNTHESIZED session and re-saved** (`*_DRAGGED.ptx`).
Diffing that against the exact synth bytes (which were already structurally click-first)
left only **3 body blocks + the index**; the index records were structurally identical
(0 diffs ignoring offsets); reconstructing the body left exactly **6 index offsets**, which
resolved to the playlists in display order. Lesson: to isolate a UI-state field, diff a
minimal drag-and-resave of a file you generated, not two independently-authored controls.

## The recipe (`move_click_to_top`, see `body_synth.py`)
Operates on a click-at-BOTTOM session (e.g. `add_click_anyN` output):
1. **Body** (`_reorder_click_to_front_body`): move the click's block to the FRONT of each
   per-track run — the 0x2519 name-table entry, both lane-major 0x251a groups, the 0x210b
   run, and the 0x2624 playlist subtree — renumbering the embedded 1-based position ordinal
   (the u16 right after each GUID) in the name/lane runs. Size-neutral rearrangement.
2. **Index rank-refill**: capture each hole's `(type, rank)` from the input, refill from the
   reordered layout. Lanes (one type, physically reordered) get display order automatically;
   per-track holes stay consistent. (The playlist list is the exception — see step 4.)
3. **Childtypes**: each 0x2624 count==4 instance's `child_refs[0].child_type` = `0x261e` if
   `ordinal == 1` else `0x261c` (ordinal-1 is the click; rank-refill already pointed its
   rank-0 childrefs at the now-first click blocks — NO record rotation; rotating breaks it).
4. **THE display-order fix**: set the 0x2624 count==1 container `elements[1:]` and each
   count==4 instance `child_refs[1]` to the playlists in display order (click first).
5. `_set_index_offset`.

## Gotchas paid for in blood
- Do NOT rotate the playlist-instance records. Rank-refill + relabel-by-ordinal is what works.
- Our heuristic block scanner (`core.py` scans for 0x5a ZMARKs) MIS-COUNTS nested blocks after
  a reorder — per-type block counts are unreliable for validating a reorder (the real top
  control parses to the same "wrong" counts). Validate by display order + PT load, not counts.
- The lane / name-entry position ordinal is the u16 immediately after the 8-byte GUID
  (`2a 00 00 00` marker + 12) — name-length-independent.

## GENERAL track reordering (index-only) — PT-CONFIRMED
The playlist-order list ALONE controls display order: setting it with the BODY
UNTOUCHED reorders the edit window (PT-confirmed — moved Audio 4 to the top of a clean
4-stereo whose body was byte-identical to the control; and the click to the MIDDLE
[Audio 1, Click 1, Audio 2]). So `move_click_to_top`'s body reorder is OPTIONAL (it just
mirrors PT's re-serialization on save; not required for display).

`body_synth.reorder_tracks(data, new_order)` reorders ANY tracks into ANY order, index-only:
`new_order` is a permutation of 0..N-1 over the track playlists in body/creation order
(`body_synth.track_playlist_order`). It sets the 0x2624 count==1 container `elements[1:]`,
each count==4 instance's `child_refs[1]`, and the instance childtype (0x261c audio /
0x261e click, by the display-position playlist's kind) to the target order; the body is
untouched. Test: `tests/test_click.py::test_reorder_tracks` (audio perms are body-byte-
identical + display list == target; click-to-middle). PT-CONFIRMED move-to-top & click-to-middle.

## Confirmed-loadable artifacts kept
`control_files/lots of stereo tracks/test_F2_12stereo_clicktop.ptx` (12-stereo click-on-top,
PT-confirmed); `..._DRAGGED.ptx` (the user's drag-and-resave that cracked the decode).
