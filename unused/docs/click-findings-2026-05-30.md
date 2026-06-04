# Click track — corrected findings (2026-05-30)

After several Pro Tools "magic ID does not match" failures, a **clean control**
— `control_files/lots of stereo tracks/1 stereo plus click.ptx` (created by
opening `1 stereo tracks.ptx` and adding a Click track, SAME audio + folder as
the synth donor) — pinned down the real cause.

## What's already SOLVED
- **Index machinery is byte-exact perfect.** Recomposing the real click index
  from its own body (isolation test) reproduces it byte-for-byte (6392 B). The
  one real index bug — the click playlist-instance childref **chain**
  (`0x2624 → 0x261e → 0x261b`, declared types `0x261e/0x261b/0x2627`) was knocked
  off-by-one — is FIXED in `final_index._fill_offsets`: removed the `0x261E`
  special-case; added a `0x261C → 0x261E` substitution in `remap_newtrack` (fires
  only when the `0x261c` lookup is empty, i.e. a click, so audio stays byte-exact).
  Confirmed against BOTH `various/stereo click.ptx` and `1 stereo plus click.ptx`.
- Path chimera handling (`_transplant_click_path`): swaps the click_lib session
  path embedded in `0x2064` for the audio session's, leaving the DigiClick
  plugin-settings path (`Macintosh HD/.../DigiclikCkRTFact.tfx`) intact.
- Overview-order finalize, index-offset pointer, counts (0x2107/0x2624/0x202a +1,
  0x1015/0x1054 unchanged).
- 63-test regression suite stays green.

## The REMAINING bug: the BODY transplant (3 gaps)
The old `various/stereo click.ptx` is **ATYPICAL** — junk-laden (undo history
baked into its `0x2589` subtree) and missing pieces. Diffing `1 stereo plus click`
against `1 stereo` (the clean delta) shows a real click ALSO needs:

1. **`0x2519` name-table entry "Click 1"** (+34 B; `<u32 len>"Click 1"<23B suffix>`).
   Real names = `['Audio 1','Click 1']`; my synth = `['Audio 1']`.
   (My earlier "click is NOT in the name table" conclusion was WRONG — derived
   from the atypical control. `grow_one_click` currently SKIPS the name entry; it
   must ADD it, like `grow_one_track` does.)

2. **`0x1018` plugin-registry growth** 88 B → 171 B (count `1→2`, adds **"Click II"**).
   `0x1018` head: donor `1810 01.. 5a0900 4b.. 1710 04 0a.. "Polyphonic…"`;
   real `1810 02.. 5a0900 4c.. 1710 03 08.. "Click II"…`. The click registers the
   DigiClick plugin ("Click II") here. `grow_one_click` does NOT touch `0x1018`.

3. **`0x2589` overview: TWO clean entries**, not one + junk.
   Real `0x258a#1` children = `{0x2581:1, 0x2589:2}` (clean): audio entry
   sz=259 (child `0x2038`, order=0) + click entry sz=271 (child `0x203b`, order=1).
   My synth `0x258a#1` = `{0x2038:2,0x203b:4,0x2580:1,0x2581:1,0x2589:1}` — only ONE
   `0x2589` PLUS undo junk dragged in by cloning the atypical `stereo click` b2589.
   Going 1→2 tracks adds BOTH overview entries (the audio track gains one too).

(Benign diffs: `0x2067` ±4 B = session-name length; `0x0000`/`0x1017` = null/undo
churn; first-block "type" = low16 of index-offset pointer.)

## Plan
Re-derive `extract_click_unit`/`grow_one_click` to source the click body from the
**clean** `1 stereo plus click.ptx` (same audio ⇒ exact clean delta): add the
`0x2519` "Click 1" name entry, the `0x1018` "Click II" registry entry, and TWO
clean `0x2589` overview entries; do NOT clone the junk subtree. Validate
**byte-exact reproduction of `1 stereo plus click.ptx` from `1 stereo tracks.ptx`**
(body + index) before any further Pro Tools test.

## Implementation status (`ptxformatwriter/click_clone.py`)

Structural diff-and-replay (the robust path for the click's entanglement):
- **`derive_click_patch(donor, ctrl)` — WORKS.** Body-only structural diff with a
  name-aware, interleave-stable signature (`(type, occ-among-same-(type,name), name)`,
  with the index-pointer first block pinned to `('FIRST',)`). On the
  `1 stereo` / `1 stereo plus click` pair it yields **9 top-level new subtrees + 2
  grown childless blocks (0x2067, 0x1017) + the 32-byte "Click 1" name entry** — no
  more sibling-occ artifacts.
- **`apply_click_patch(target, patch, donor_index_src)` — RUNS but NOT byte-exact.**
  Result reloads (`rc=0`) but is +756 B vs the control, is **missing `0x261e`
  (+`0x261b`, +the `0x200b/0x2103/0x2104` chain)**, carries extra `0x2037/0x2038/0x203b`,
  and contains **misframed blocks** (bogus types `0x25a`/`0xa5a`/`0x217a`) ⇒ the
  insertion **offset / ancestor size-bump** logic is wrong for some inserts. Debug:
  the `after_sig → target offset` anchor mapping and the `_ancestors_containing` +
  `size_delta` splice (an insert landing at a wrong offset corrupts framing; the
  missing `0x261e` suggests its anchor (`after_sig` = last `0x261c`) resolved wrong,
  or its blob spliced before a size-bump). Validate by **byte-exact reproduction of
  `1 stereo plus click.ptx` from `1 stereo tracks.ptx`** (modulo the `0x2067`
  session-name string), then port to `add_click` / fold into `synthesize_mixed_session`.

NOTE: this session's tool-output channel was badly buffered (every probe took many
retries), so the `apply` debug loop is best finished in a fresh session.

## Key control files
- Donor / clean target pair (SAME audio): `lots of stereo tracks/1 stereo tracks.ptx`
  and `lots of stereo tracks/1 stereo plus click.ptx`.
- Atypical (do NOT trust for the body): `various/stereo click.ptx`, `various/click only.ptx`.
