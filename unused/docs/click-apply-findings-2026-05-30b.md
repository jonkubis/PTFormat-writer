# apply_click_patch — ✅ BYTE-EXACT ACHIEVED (2026-05-30, session B)

**`python3 validate_click.py` -> `byte_exact=True`** (out_sz=ctrl_sz=79742, per-type
diffs `{}`). `ptxformatwriter/click_clone.py` reproduces `1 stereo plus click.ptx` from
`1 stereo tracks.ptx` byte-for-byte via a recursive structural diff (16 replacements,
5115 new bytes). `control_files/synth_stereo_click.ptx` is written — load it in Pro Tools.

REMAINING WORK: fold into `body_synth.add_click` / add a `click` kind to
`synthesize_mixed_session` (channels=0, placed LAST, one per session). NOTE: the patch's
replacement offsets are in DONOR-body coordinates; for `add_click` against a *different*
target you must either re-derive against that target or remap each replacement by the
owner block's signature + offset-within-owner. (For this validation, target==donor.)

The full diagnosis + design that got here is below.

---

Picks up `docs/CLICK-HANDOFF.md`. Re-validate any time:
```
python3 validate_click.py                       # -> byte_exact=True
```
(The `dbg_*.py` scratch scripts used during diagnosis have been REMOVED now that the
result is byte-exact and PT-confirmed; `validate_click.py` is the canonical harness.
Everything below is the historical diagnosis log.)

## ⚠️ Two facts confirmed at the very end (read in a clean burst)
1. **A shadowing bug was found AND already fixed.** The forward-merge loop variable
   was named `_chain`, shadowing the module-level `_chain()` helper -> `apply_click_patch`
   raised `UnboundLocalError`. Renamed the loop var to `_ch`. (So the very first run
   after this is the first REAL test of the new algorithm.)
2. **The donor and control have DIFFERENT xor seeds:** `donor[0x12:0x14]=05f3`,
   `ctrl[0x12:0x14]=0543` (byte 0x13: f3 vs 43). Bytes 0x00–0x13 are NOT xored
   (they hold the key), so unxored byte 0x13 can NEVER match — Pro Tools re-rolls the
   seed on every save. **=> full `out == ctrl` is impossible; the real goal is
   BODY-exactness.** `validate_click.py` compares full unxored bytes, so even a
   perfect body will show `byte_exact=False` with first-diff @ 0x13. RECOMMENDATION:
   relax the compare to ignore the 16-byte header (`out[0x14:] == ctrl[0x14:]`, still
   allowing the 0x2067 session-name block), then re-run. `dbg_check.py` already
   reports `total_diff_bytes(common)` and the first diff so you can see whether the
   only remaining diffs are 0x13 (+ maybe 0x2067).
3. **insert[2] anchor confirmed:** donor `0x2027` has NO children (`kids=[]`), so the
   `after_sig is None` branch correctly anchors the new `0x2064` at the parent's
   content end (`tn[parent].end`).

## The REAL diagnosis (clean data; ignore any "0x2510" notes from a garbled earlier draft)
`derive_click_patch` yields **9 inserts + 2 grown (0x2067 session-name, 0x1017) +
32B "Click 1" name entry**. All 9 inserts have parents that exist in the target.
Confirmed insert table (control offset order, from `dbg_ins.py`):

| # | ctype  | parent (exists)        | after_sig             | notes |
|---|--------|------------------------|-----------------------|-------|
| 0 | 0x251a | 0x2519                 | 0x251a:0 (existing)   | click lane 0 |
| 1 | 0x251a | 0x2519                 | 0x251a:1 (existing)   | click lane 1 |
| 2 | 0x2064 | 0x2027                 | **None** (first child)| DigiClick session reg |
| 3 | 0x1017 | 0x1018                 | 0x1017:0 (existing)   | "Click II" registry entry |
| 4 | 0x210b | 0x2107                 | 0x210b:0 (existing)   | click 0x210b |
| 5 | 0x200b | 0x2624/0x261c          | 0x261b:0 (existing)   | chain inside AUDIO track's 0x261c |
| 6 | 0x261e | 0x2624                 | 0x261c:0 (existing)   | click playlist + DigiClick instance (2405B) |
| 7 | 0x2589 | 0x2587/.../0x258a:0    | 0x2581:0 (existing)   | audio overview entry (266B) |
| 8 | 0x2589 | 0x2587/.../0x258a:0    | **0x2589:0 (NEW = #7)** | click overview (278B) — **THE BUG** |

Only **#8** has an `after_sig` pointing at another NEW insert (#7). The old apply
fell back to `parent.off` (0x258a content start) for it, dropping both 0x2589
entries at the wrong offset; that misframed every later block and the parser
resynced onto bogus ZMARKs (0x25a/0xa5a/...), losing 0x261e etc. Net out was
**+756 B** vs the 79742 B control.

(NOTE: the donor/control are ~80 KB, not the 6952 B an early garbled read showed.)

## The fix (applied)
1. `derive_click_patch` now stores each insert as **(own_sig, parent_sig, after_sig,
   blob)** — `own_sig` added so a later insert can anchor after an earlier NEW one.
2. `apply_click_patch` rewritten:
   - Resolve anchors in control order, tracking `placed[own_sig] = anchor`:
     existing `after_sig` -> that block's `end`; `after_sig` that is a NEW insert ->
     the same gap (ordered after via `seq`); `after_sig is None` -> parent's first
     existing child (`min(kids)`), else parent content `end`.
   - Size bumps use each edit's **explicit parent chain** (`_chain`, walks `.par`),
     not byte-containment — avoids the boundary ambiguity at a block's `end`
     (append-inside vs insert-before-next-sibling). Grown blocks bump ancestors
     only (own size lives in the replacement bytes).
   - **Forward-merge splice** sorted by `(pos, seq)` so two inserts sharing one
     anchor (e.g. #7 then #8) keep control order. (The old high->low splice
     reversed same-offset inserts.)
   - Counts + `compose_index` + `_set_index_offset` tail unchanged (already exact).

## Open question to verify (couldn't read result)
- **xor-seed parity**: `validate_click` compares UNXORED bytes, which include the
  seed at offset 0x13. A garbled read suggested donor=0xf3 vs ctrl=0x43 there; if
  the donor and control genuinely differ in their xor seed, that byte can never
  match and "byte_exact" is impossible for the header even with a perfect body.
  `dbg_check.py` prints `donor[0x12:0x14] vs ctrl[...]` to settle this. If they
  differ, treat a lone 0x13 (+ 0x2067) diff as success, same spirit as the 0x2067
  allowance, and adjust `validate_click.py`'s comparison to skip the 16-byte header.
- **insert[2] (after=None)**: lands at 0x2027's first existing child / content end.
  `dbg_check.py` prints 0x2027's donor children to confirm that anchor is right.

## ⭐ RESOLUTION (2026-05-30, late): rewrote click_clone.py as a RECURSIVE STRUCTURAL DIFF
The per-subtree-insert model (below) got 7/9 inserts right but could NOT express two
real changes, both of which are "the divergent SUFFIX of a common parent's content
must be REPLACED, not inserted into":
- **A (overview 0x258a#2):** DONOR content = `[2581][270B own-content tail]`; CTRL =
  `[2581][4B][2589 audio 266][2589 click 278]`. `4+266 = 270`, so the donor's 270B
  tail is REPLACED (net +278), not appended-to. Pure inserts kept the 270B tail AND
  added 544B -> the audio 0x2589 dissolved (its children parsed directly under 0x258a)
  and +266 leaked. (Confirmed via `dbg_ov.py`: OUT 0x258a#2 size 1130 vs CTRL 864,
  with the 270B donor tail still present at the end.)
- **B (0x261c chain):** `dbg_ov.py` shows OUT has TWO 0x200b (z=60075 AND z=60583, both
  parent 0x261c) where CTRL has ONE (z=60089). The audio 0x261c's content after its
  last common child was inserted-into rather than suffix-replaced -> the chain doubled.

**Fix (IMPLEMENTED, full rewrite of `ptxformatwriter/click_clone.py`):** `derive_click_patch`
now does a recursive structural diff — for each block common to donor+control (matched
by signature), align children by signature prefix, recurse into common children, then
emit ONE replacement `(donor_start, donor_end, ctrl_bytes, owner_zmark)` for the
divergent suffix (bytes after the last common child, through content end). `ClickPatch`
now holds `replacements` (no more inserts/grown/name_entry). `apply_click_patch` bumps
each owner+ancestors' block_size by the net delta and rebuilds the body in one forward
merge, then the unchanged counts/compose_index/_set_index_offset tail. This UNIFIES
inserts, grows (0x1018/0x1017), the 0x2519 name entry, the overview reframe (A), and
the chain (B) with no special cases. The index-pointer first block is skipped.
`validate_click.py` updated: prints `replacements=` and neutralizes the xor-seed bytes
0x12/0x13 before the byte-exact check.

**VALIDATED — the recursive diff WORKS.** `dbg_check.py` after the rewrite:
`reload_rc=0 out_sz=79704 ctrl_sz=79742 byte_exact=False`, per-type diffs now just
`{0x0:(5,6), 0x1e60:(1,0), 0x1e86:(0,1)}`. Down from +756 B / 14 type-diffs.
- `0x1e60`/`0x1e86` are NOT misframes — they are the FIRST block's "content_type",
  i.e. the low-16 bits of the index-offset pointer (out 0x011e60=73312 vs
  ctrl 0x011e86=73350). They auto-match once the body length is right.
- **The ONE real remaining bug: out is 38 B SHORT and missing exactly one `0x0` null
  block (out 5 vs ctrl 6).** 73350-73312 = 38 = the missing block. So one divergent
  region is still not being replaced (or is replaced 38 B short) by the recursive diff.

### ⭐ FINAL FIX APPLIED (awaiting confirmation — channel stalled again)
TWO fixes, both in `derive_click_patch`:
1. **(real root cause of the −38 B)** `dbg_loc.py` (after fixing it to skip the index
   pointer) pinned the first real divergence to **@457 = the `0x2519` block's size field**
   (out `0x2d7`=727 vs ctrl `0x2f9`=761, −34). The `0x2519` name table holds the
   "Click 1" name entry as **own-content BEFORE the first child** (the `0x251a` lanes).
   The original `diff_block` replaced only the divergent SUFFIX (after the last common
   child) — it never compared the own-content PREFIX or inter-child gaps, so the name
   table growth was dropped. **Fixed:** `diff_block` now walks matched children and
   replaces the own-content GAP before each (the gap before the first child = the
   prefix, e.g. the name table), recurses into each, then replaces the suffix.
2. **(belt-and-suspenders)** the top-level walk now also appends a trailing body-suffix
   replacement for any NEW trailing top-level block. (My earlier guess that this alone
   was the −38 was WRONG — the name-table prefix is the real cause — but the handling
   is correct to keep.)

After both, every own-content region (prefix, gaps, suffix) at every level is diffed.

**Expected result after this fix:** `validate_click.py` -> `byte_exact=True`
(it neutralizes the seed). `dbg_check.py` (no seed-neutralize) -> `out_sz=79742`,
`per-type diffs: {}`, first diff @19 (the seed, expected). If confirmed: write
`control_files/synth_stereo_click.ptx` and have the user load it in Pro Tools.
If NOT confirmed, run `python3 dbg_loc.py ; cat /tmp/dbg_loc.txt` (already fixed to skip
the index-pointer; reports the real divergence + realignment shift).

### Finish it (next session, fresh channel) — fallback if the above didn't fully land
Run `python3 dbg_loc.py ; cat /tmp/dbg_loc.txt` — it neutralizes the xor seed, finds
the FIRST real body divergence, names the enclosing ctrl block + its ancestor chain,
and reports the realignment shift (should say ~"out is 38 bytes short here"). That
pinpoints which parent's suffix is mis-diffed. Likely a `0x0` null block whose
signature `(type=0, occ, name=None)` collides under occ-renumbering when the click
inserts a sibling, so the positional prefix-match in `diff_block` pairs the wrong
null and drops 38 B. Candidate fixes once located:
- If it's an occ-collision on null blocks: make the prefix-match also require byte
  equality for `0x0` (or stop the common-prefix walk at the first non-identical child
  of an ambiguous type), so the divergent suffix (incl. the extra null) is replaced.
- Or: in `diff_block`, when `len(d_kids) != len(c_kids)`, fall back to a longest-common-
  -subsequence alignment of child sigs instead of the positional prefix walk.
Then `python3 validate_click.py` should print `byte_exact=True` (seed already
neutralized there). THEN write `control_files/synth_stereo_click.ptx` and ask the
user to load it in Pro Tools.

(Tool-channel note: it stalled HARD again here — 30+ empty reads with no burst — right
after producing the -38 B result, so I could not run the locator. Restarting clears it.)

## (superseded) STATUS after the per-insert rewrite: 7/9 inserts fixed, A+B remained
`dbg_check.py` (post shadowing-fix) -> `reload_rc=0 out_sz=80498 ctrl_sz=79742`
(still +756 B), `byte_exact=False`. Per-type count diffs (out vs ctrl) NOW:
```
0x0:(4,6) 0x200a:(2,1) 0x200b:(2,1) 0x2015:(2,1) 0x2434:(3,2) 0x2037:(24,20)
0x2038:(12,10) 0x203b:(12,10) 0x2103:(2,1) 0x2104:(2,1) 0x217a:(1,0)
0x2580:(4,3) 0x2589:(1,2) 0x1e86:(0,1)
```
**Fixed by the rewrite** (gone from the diff): 0x261e, 0x251a, 0x258a, 0x2619,
0x261b, 0x2027, 0x201f, 0x4702, 0x102d, 0x25a, 0xa5a, 0x200d. So the anchor +
forward-merge fix WORKS for 7 of the 9 inserts. Two structural problems remain:

### Problem A — overview 0x258a#2 (out missing one 0x2589; extra 0x2038/0x203b)
Clean subtree byte math (from `dbg_tree.py`):
- DONOR 0x258a#2: content 586 B = `[2B hdr][0x2581 314B][270B TAIL]` (parser sees
  ONLY the 0x2581 child; the 270B is own-content, NOT framed blocks).
- CTRL  0x258a#2: content 864 B = `[2B hdr][0x2581 314B][4B gap][0x2589 266B][0x2589 278B]`.
- `2+314+4+266+278 = 864`; `2+314+270 = 586`. So ctrl = donor with the **270B tail
  REPLACED** by `4B + 0x2589(266) + 0x2589(278)`; net +278, NOT +544.
- **`4 + 266 = 270`** (!!): the donor's 270B tail is almost certainly the lone audio
  track's overview entry stored as raw own-content (or a 0x2589 that the parser
  doesn't frame). The control upgrades it to a real 0x2589 block + adds the click's.
- derive emits insert[7] (audio 0x2589, 266B, after 0x2581) and insert[8] (click
  0x2589, 278B, after the new audio one) as PURE inserts -> apply keeps the donor's
  270B tail AND adds 544B = **+266 too many** + the leftover 270B misframes into the
  bogus 0x217a/0x1e86 and the extra 0x2038/0x203b.
- **FIX direction:** this needs derive to recognize the 270B own-content tail as
  REPLACED, not preceded by inserts. Either (a) have derive emit a "grow/replace"
  for 0x258a#2's own-content region (treat [0x2581.end, parent.end) as old bytes ->
  control's [0x2581.end, parent.end) bytes — a whole-region replace covering both
  0x2589 + the 4B gap), or (b) special-case: when an insert's parent has trailing
  own-content that differs donor vs control, splice the control's whole post-last-
  existing-child region instead of inserting. Approach (a) is cleaner and also
  subsumes the inserts there. CONFIRM with `dbg_ov.py` (donor vs ctrl 0x258a#2
  recursive subtree + byte dump of the 270B tail) — written, result was buffer-stalled.

### Problem B — the 0x200b/0x200a/0x2015/0x2104/0x2103 chain is DUPLICATED (out 2, ctrl 1)
- Before the rewrite this chain was MISSING (0x200b 0/1); now it is DOUBLED (2/1).
- insert[5] (0x200b chain, 496B, own=2624:0/261c:0/200b:0, parent = the AUDIO 0x261c,
  after 0x261b) adds one. The second copy is either (i) inside insert[6]'s 0x261e
  blob (i.e. the chain lives in the CLICK playlist too and derive double-covers it —
  check whether cn[200b].z falls within cn[261e] range; if so, insert[5] is REDUNDANT
  and should be dropped), or (ii) a misframe artifact from Problem A's leftover bytes.
  `dbg_ov.py` lists every 0x200b in out vs ctrl with parent + offset to disambiguate.
- If (i): the chain belongs ONLY to 0x261e, and insert[5] is a spurious root from
  derive's `new`-root logic — fix derive to drop a "root" whose byte range is already
  inside another insert's blob.

These two likely account for the full +756: A contributes ~ +266 (kept 270B tail)
and B contributes ~ +496 (duplicated 496B chain): 266+496 = 762 ≈ 756 (±the 4B gap /
0x2580 churn). Resolve A and B and re-run `python3 validate_click.py`.

## After byte-exact
Write `control_files/synth_stereo_click.ptx`, have the user load it in Pro Tools,
then fold into `body_synth.add_click` / add a `click` kind to
`synthesize_mixed_session` (channels=0, placed LAST, one per session).

## Debug scripts left at repo root (rerun any; each writes /tmp/dbg_*.txt)
`dbg_check.py` (validate + seed + first-diff), `dbg_ins.py` (per-insert anchors),
`dbg_click.py` (patch summary — NOTE: still unpacks the OLD 3-tuple insert; update
its loop to 4-tuple before rerunning), `dbg_tree.py`, `dbg_2510.py` (obsolete).

## Tooling caveat
Output channel buffered badly: empty reads for many turns, then a one-turn burst
delivering everything at once, plus stray-text injection that corrupted a few
reads. Writes stayed reliable. If reads stall, the pending output usually bursts
in within a turn or two — or restart, which loses nothing (the fix is in the file).
