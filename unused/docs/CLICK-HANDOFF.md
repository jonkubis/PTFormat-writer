# CLICK TRACK — SESSION HANDOFF (start here)

**Mission:** finish click-track synthesis — adding a click track to an audio
session — then fold it into `synthesize_mixed_session`. The hard parts are done;
ONE localized bug remains.

Repo: `/Volumes/Dropbox SSD/Dropbox/PYTHON/__CODEX/PTFORMAT`. **Always use `python3`.**
Full test suite: `python3 -m unittest discover -s tests` (~120 s, currently GREEN).

---

## THE ONE REMAINING TASK

Make **`apply_click_patch`** in **`ptxformatwriter/click_clone.py`** reproduce the control
**`control_files/lots of stereo tracks/1 stereo plus click.ptx`** byte-for-byte from
the donor **`control_files/lots of stereo tracks/1 stereo tracks.ptx`** (the two share
the identical audio track, so the byte-delta IS the clean click contribution).

Validate with the saved script:
```
python3 validate_click.py
```
Goal: `byte_exact=True` (the `0x2067` session-name string may legitimately differ —
treat a single diff localized to the 0x2067 block as success). Then load the written
`control_files/synth_stereo_click.ptx` in Pro Tools (the user does this).

### The bug (localized)
`derive_click_patch` is CORRECT (9 top-level new subtrees + 2 grown childless blocks
`0x2067`/`0x1017` + a 32-byte "Click 1" name entry — no artifacts). `apply_click_patch`
RUNS and reloads (`rc=0`) but is **+756 B**, **missing `0x261e`** (and its subtree
`0x261b`/`0x2619`/the `0x200b→0x200a→0x2015→0x2104→0x2103` chain), and contains
**misframed blocks** (bogus parsed types `0x25a`/`0xa5a`/`0x217a`). Causes to fix:
1. **`after_sig` that points at another NEW block resolves to the wrong anchor**
   (`apply_click_patch` falls back to `parent.off`, i.e. the parent's content START,
   instead of *after the new sibling*). The 2nd `0x2589` overview entry (its preceding
   sibling is the 1st, also new) is the clear case. Fix: process inserts in control
   order, tracking each inserted block's resulting position so a later insert whose
   `after_sig` is an earlier *new* insert anchors after it.
2. A subtree landing at a wrong byte offset corrupts block framing (→ the bogus
   types). The missing `0x261e` is the loudest symptom — check its anchor
   (`after_sig` = the last `0x261c`) and the high→low splice vs. the `0x200b`
   insertion that grows `0x261c` underneath it.
3. Re-check `_ancestors_containing` + `size_delta`: every block whose `[z,end)`
   contains an insert position must bump by the net delta; replaced (grown) blocks
   bump ancestors only (their own size is inside the new bytes).

When byte-exact: port the replay into `body_synth.add_click` (replace its
`grow_one_click` body path with the click_clone diff-replay), then add a `click`
kind to `synthesize_mixed_session` (channels=0, placed LAST, one per session).

---

## WHAT IS ALREADY DONE — DO NOT REDO

1. **Index machinery (`ptxformatwriter/final_index.py`) is byte-exact for clicks.**
   Proven: recomposing `1 stereo plus click.ptx`'s index from its own body via
   `compose_index(donor, body, base, base+1, channels=[...,0], track_names=[...,"Click 1"],
   click_tracks={base+1})` reproduces it **byte-for-byte**. Key fix already in place:
   in `_fill_offsets.remap_newtrack`, a `0x261C → 0x261E` substitution (fires only
   when the `0x261c` name lookup is empty, i.e. a click), plus `add_click_track`,
   `synthesize_index_records(..., click_tracks=...)`, `compose_index(..., track_names=,
   click_tracks=)`, and `_NAME_RE` extended to `(?:Audio|Click|MIDI)`. **Audio/mono/
   mixed stay byte-exact; 63 tests green.** Do not touch the index path.

2. **Click fully decoded** — see `docs/click-findings-2026-05-30.md` (READ IT). It has
   the exact per-type delta, the overview structure (audio `0x2589` order0 sz259 +
   click order1 sz271 in `0x258a#1`), the `0x1018` "Click II" registry growth (a new
   `0x1017` child), the `0x2519` "Click 1" name entry, and the `0x200b` chain placement.

3. **Path / overview / counts / index-offset** handling exists in `body_synth.py`
   (`_transplant_click_path`, `_splice_region`, `_path_chain_span`, `_set_overview_order`,
   `_patch_counts`, `_set_index_offset`). Reusable.

4. The OLD `various/stereo click.ptx` control is **ATYPICAL** (junk-laden, missing the
   name entry). **Build the body from the clean `lots of stereo tracks/1 stereo plus
   click.ptx` pair only.**

---

## CONTROL FILES
- **Donor + clean target (SAME audio — use these):**
  `control_files/lots of stereo tracks/1 stereo tracks.ptx` and
  `control_files/lots of stereo tracks/1 stereo plus click.ptx`.
- Atypical (do NOT trust for the body): `control_files/various/stereo click.ptx`,
  `control_files/various/click only.ptx`.
- Helpful next control to generalize beyond N=1 (ask the user): a `2 stereo + click`.

## KEY API QUICK-REF
- `body_synth.parse / flat_blocks / _by_type / _parent_zmarks / block_bytes`
- `body_synth._set_index_offset(full_bytes)` — fixes the first-block index pointer.
- `final_index.compose_index(donor, target_body+donor_index, base, target, channels=,
  track_names=, click_tracks=)`
- `final_index.offset_holes(data)` → `(ref, [(abs_pos, value, type, rank, kind)…])`;
  `bad = sum(1 for h in holes if h[1] not in block_layout(data)[0])` must be 0.
- Reload check: `PTFFormat().load(path, 48000) == 0`; write via
  `writer.encrypt_session_data(unxored_bytes)`.

## CAVEAT
The previous session's tool-output channel was badly buffered (every command needed
many retries to read). If that recurs, restart the session — it makes iteration
~6–8× slower and is the only reason the click took so long.
