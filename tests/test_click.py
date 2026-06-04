"""Tests for click-track synthesis (`ptxformatwriter.click_clone` + `body_synth.add_click`
+ the `synthesize_mixed_session(click_ref=...)` fold).

The click is added by a recursive structural diff between a clean control PAIR that
shares the same audio: `N stereo tracks.ptx` (audio only) and `N stereo plus
click.ptx` (same audio + a Click track). Replaying that diff onto the audio-only
session reproduces the click control BYTE-FOR-BYTE (modulo the per-file XOR seed at
bytes 0x12/0x13, which Pro Tools re-rolls on every save). N=1 is Pro-Tools-confirmed;
the mixed-session fold ([2,2]+click) is Pro-Tools-confirmed too.
"""
from pathlib import Path
import tempfile
import unittest

from ptxformatwriter.core import PTFFormat
from ptxformatwriter import body_synth as BS, click_clone as CC, final_index as FI, writer as W

ROOT = Path(__file__).resolve().parents[1]
STER = ROOT / "control_files" / "lots of stereo tracks"
MONO = ROOT / "control_files" / "lots of mono tracks"


def _display_order(data: bytes) -> list[str]:
    """The edit-window track display order (e.g. ['CK','A1','A2']) read from the
    index's playlist-order list — the field Pro Tools sorts the edit window by. The
    0x2624 count==1 container's elements[1:] point at the playlists in display order;
    'CK' = the click (0x261e) playlist, 'A{i}' = the i-th audio (0x261c) by body order."""
    ref = FI.final_index_ref(data)
    zt, bt = FI.block_layout(data)
    recs = FI.parse_records(ref.data, set(bt), set(zt))
    fb = BS.flat_blocks(BS.parse(data))
    click = [b.offset - 7 for b in fb if b.content_type == 0x261E][0]
    audio = sorted(b.offset - 7 for b in fb if b.content_type == 0x261C)
    label = {click: "CK"}
    label.update({a: f"A{i + 1}" for i, a in enumerate(audio)})
    for r in recs:
        if r.content_type == 0x2624 and r.count == 1 and r.flag == 1:
            return [label.get(e.offsets[0], "?") for e in r.elements[1:] if e.offsets]
    return []
# Mono+stereo click pair — same folder as the all-stereo pairs (same embedded leaf,
# so the clean/click structural diff is pure click, no path-leaf noise).
_MIX_CLEAN = STER / "mono stereo.ptx"
_MIX_CLICK = STER / "mono stereo plus click.ptx"


def _load_path(p: Path) -> bytes:
    ptf = PTFFormat()
    ptf.load(str(p), 48000)
    return ptf.unxored_data()


def _clean(n: int) -> Path:
    return STER / f"{n} stereo tracks.ptx"


def _click(n: int) -> Path:
    return STER / f"{n} stereo plus click.ptx"


def _pairs_available() -> list[int]:
    return [n for n in range(1, 9) if _clean(n).exists() and _click(n).exists()]


def _strip_seed(data: bytes) -> bytes:
    # Bytes 0x12 (xor type) / 0x13 (xor value) are the un-xored key; Pro Tools
    # re-rolls them on every save, so they legitimately differ between two saves of
    # the same session and are not part of the session body. Neutralize before the
    # byte-exact check (the same allowance validate_click.py makes).
    out = bytearray(data)
    if len(out) >= 0x14:
        out[0x12:0x14] = b"\x00\x00"
    return bytes(out)


def _reload_ok(data: bytes) -> int:
    with tempfile.NamedTemporaryFile(suffix=".ptx", delete=False) as tmp:
        tmp.write(W.encrypt_session_data(data))
        path = tmp.name
    try:
        return PTFFormat().load(path, 48000)
    finally:
        Path(path).unlink(missing_ok=True)


@unittest.skipUnless(_pairs_available(), "stereo+click control pairs not present")
class ClickSynthesisTests(unittest.TestCase):
    def test_add_click_byte_exact(self) -> None:
        """add_click(data, clean, click) replayed onto the clean control reproduces
        the real click control byte-for-byte (seed-neutralized) for every available
        N, and the result re-parses cleanly. This is the regression guard for the
        recursive structural diff in click_clone."""
        for n in _pairs_available():
            clean, ctrl = _load_path(_clean(n)), _load_path(_click(n))
            out = BS.add_click(clean, clean, ctrl)
            self.assertEqual(
                _strip_seed(out), _strip_seed(ctrl),
                msg=f"add_click not byte-exact vs {n} stereo plus click",
            )
            self.assertEqual(_reload_ok(out), 0, msg=f"add_click N={n} reload failed")

    def test_derive_patch_shape(self) -> None:
        """The derived patch is a non-empty list of in-bounds, non-overlapping,
        offset-sorted byte-region replacements against the clean (donor) body."""
        for n in _pairs_available():
            clean, ctrl = _load_path(_clean(n)), _load_path(_click(n))
            patch = CC.derive_click_patch(clean, ctrl)
            reps = patch.replacements
            self.assertTrue(reps, f"empty patch for N={n}")
            prev_end = -1
            for (start, end, new_bytes, _owner) in reps:
                self.assertLessEqual(0, start, msg="replacement start < 0")
                self.assertLessEqual(start, end, msg="replacement end < start")
                self.assertGreaterEqual(start, prev_end, msg="overlapping/out-of-order replacements")
                self.assertIsInstance(new_bytes, (bytes, bytearray))
                prev_end = end

    def test_add_click_anyN_cross_layout(self) -> None:
        """Frontier 1 (PT-confirmed N=3 & N=12): add_click_anyN sources a click from the
        2-stereo pair and splices it onto a LARGER session (N=3,4) with no matching click
        control. Validates the EOS fix: the body 0x2519 (name table + lane-major 0x251a lanes,
        incl. the `02 00` separator and the re-stamped track count) is byte-exact vs the real
        N-click control (GUID-neutralized); the result reloads, gains exactly one click
        playlist, and keeps its N audio tracks."""
        if 2 not in _pairs_available():
            self.skipTest("need the 2-stereo + click pair (cross-N source)")
        clean2, click2 = _load_path(_clean(2)), _load_path(_click(2))

        def b2519(data: bytes) -> bytes:
            x = [b for b in BS.flat_blocks(BS.parse(data)) if b.content_type == 0x2519][0]
            return data[x.offset - 7 : x.offset + x.block_size]

        def neut_guids(b: bytes) -> bytes:
            b = bytearray(b); i = 0
            while True:
                j = b.find(b"\x2a\x00\x00\x00", i)
                if j < 0:
                    break
                b[j + 4 : j + 12] = b"\x00" * 8; i = j + 4
            return bytes(b)

        tested = [n for n in (3, 4) if n in _pairs_available()]
        self.assertTrue(tested, "need a 3- or 4-stereo + click control pair to validate cross-N")
        for n in tested:
            target, ctrl = _load_path(_clean(n)), _load_path(_click(n))
            out = BS.add_click_anyN(target, clean2, click2)
            self.assertEqual(neut_guids(b2519(out)), neut_guids(b2519(ctrl)),
                             msg=f"N={n}: body 0x2519 not byte-exact vs the real click control")
            n261e = sum(1 for b in BS.flat_blocks(BS.parse(out)) if b.content_type == 0x261e)
            self.assertEqual(n261e, 1, msg=f"N={n}: expected exactly one click playlist")
            self.assertEqual(len(BS.track_types(out)), n, msg=f"N={n}: audio track count changed")
            self.assertEqual(_reload_ok(out), 0, msg=f"N={n}: cross-N click reload failed")

    def test_move_click_to_top(self) -> None:
        """Frontier 2 (PT-confirmed N=2 & N=12): move_click_to_top reproduces the
        edit-window display order of the real click-on-top control. The order lives in
        the index's playlist-order list (NOT body order / overview / counters); the
        result reloads, lists the click FIRST (matching the `N stereo plus click on
        top.ptx` control), keeps its N audio tracks, and has one click playlist."""
        for n in (1, 2):
            bottom_p, top_p = _click(n), STER / f"{n} stereo plus click on top.ptx"
            if not (bottom_p.exists() and top_p.exists()):
                continue
            out = BS.move_click_to_top(_load_path(bottom_p))
            self.assertEqual(_reload_ok(out), 0, msg=f"N={n}: move_click_to_top reload failed")
            order = _display_order(out)
            self.assertEqual(order[0], "CK", msg=f"N={n}: click not first in display order")
            self.assertEqual(order, _display_order(_load_path(top_p)),
                             msg=f"N={n}: display order differs from the click-on-top control")
            # body playlist order is click-first; audio preserved; one click playlist
            pls = sorted((b for b in BS.flat_blocks(BS.parse(out))
                          if b.content_type in (0x261C, 0x261E)), key=lambda b: b.offset)
            self.assertEqual(pls[0].content_type, 0x261E, msg=f"N={n}: click block not first")
            self.assertEqual(len(BS.track_types(out)), n, msg=f"N={n}: audio count changed")
            self.assertEqual(sum(1 for b in BS.flat_blocks(BS.parse(out))
                                 if b.content_type == 0x261E), 1, msg=f"N={n}: not one click")

    def test_add_click_anyN_at_top(self) -> None:
        """add_click_anyN(at_top=True) sources the click from the 2-stereo pair and
        places it as the TOP track on a larger session (N=3,4): reloads, click is first
        in display order, N audio tracks preserved, one click playlist."""
        if 2 not in _pairs_available():
            self.skipTest("need the 2-stereo + click pair (cross-N source)")
        clean2, click2 = _load_path(_clean(2)), _load_path(_click(2))
        tested = [n for n in (3, 4) if _clean(n).exists()]
        self.assertTrue(tested, "need a 3- or 4-stereo control to validate at_top")
        for n in tested:
            out = BS.add_click_anyN(_load_path(_clean(n)), clean2, click2, at_top=True)
            self.assertEqual(_reload_ok(out), 0, msg=f"N={n}: at_top reload failed")
            self.assertEqual(_display_order(out)[0], "CK", msg=f"N={n}: click not on top")
            self.assertEqual(len(BS.track_types(out)), n, msg=f"N={n}: audio count changed")

    def test_reorder_tracks(self) -> None:
        """reorder_tracks permutes the edit-window display order via the index
        playlist-order list ONLY (body untouched) — PT-confirmed the index list alone
        controls display. Validates several audio permutations of 4-stereo (reload,
        body byte-identical, container list == target) plus a click-to-middle case,
        and that a bad permutation raises."""
        if _clean(4).exists():
            s4 = _load_path(_clean(4))
            body0 = s4[: FI.final_index_ref(s4).start]
            pls = [o for o, _t in BS.track_playlist_order(s4)]  # body order
            for perm in ([3, 0, 1, 2], [3, 2, 1, 0], [1, 0, 3, 2]):
                out = BS.reorder_tracks(s4, perm)
                self.assertEqual(_reload_ok(out), 0, msg=f"perm {perm}: reload failed")
                self.assertEqual(out[: FI.final_index_ref(out).start], body0,
                                 msg=f"perm {perm}: body changed (must be index-only)")
                ref = FI.final_index_ref(out)
                zt, bt = FI.block_layout(out)
                recs = FI.parse_records(ref.data, set(bt), set(zt))
                clist = next(([e.offsets[0] for e in r.elements[1:] if e.offsets]
                              for r in recs if r.content_type == 0x2624 and r.count == 1 and r.flag == 1))
                self.assertEqual(clist, [pls[i] for i in perm],
                                 msg=f"perm {perm}: index display list mismatch")
            with self.assertRaises(ValueError):
                BS.reorder_tracks(s4, [0, 1, 2])  # not a permutation of 0..3
        if 2 in _pairs_available():  # click to a non-extreme position
            ck = _load_path(_click(2))  # body order [A1, A2, Click]
            out = BS.reorder_tracks(ck, [0, 2, 1])  # -> [A1, Click, A2]
            self.assertEqual(_reload_ok(out), 0, msg="click-to-middle reload failed")
            self.assertEqual(_display_order(out), ["A1", "CK", "A2"],
                             msg="click-to-middle display order wrong")

    def test_click_adds_one_playlist(self) -> None:
        """The clicked result has exactly one more 0x261e (click playlist) than the
        audio-only session, and the same audio track list (the click is not an audio
        track: track_types stays unchanged)."""
        for n in _pairs_available():
            clean = _load_path(_clean(n))
            out = BS.add_click(clean, clean, _load_path(_click(n)))

            def n261e(d: bytes) -> int:
                return sum(1 for b in BS.flat_blocks(BS.parse(d)) if b.content_type == 0x261E)

            self.assertEqual(n261e(out), n261e(clean) + 1, msg=f"N={n} click playlist count")
            self.assertEqual(
                [(t.name, t.kind) for t in BS.track_types(out)],
                [(t.name, t.kind) for t in BS.track_types(clean)],
                msg=f"N={n} audio track list changed",
            )

    def test_mixed_session_with_click(self) -> None:
        """synthesize_mixed_session(click_ref=...) appends a click last: the result
        re-parses cleanly, gains one click playlist, and its overview display-order
        is a VALID permutation of 0..N (the click-diff fix-up must not leave a
        duplicate). Uses the 2-stereo pair as the no-growth donor."""
        if 2 not in _pairs_available():
            self.skipTest("need 2 stereo + click pair")
        s2, c2 = _load_path(_clean(2)), _load_path(_click(2))
        out = BS.synthesize_mixed_session(
            [2, 2], donor_data=s2, mono_lib=(s2, 2), stereo_lib=(s2, 2), click_ref=(s2, c2)
        )
        self.assertEqual(_reload_ok(out), 0, msg="mixed[2,2]+click reload failed")
        n261e = sum(1 for b in BS.flat_blocks(BS.parse(out)) if b.content_type == 0x261E)
        self.assertEqual(n261e, 1, msg="expected exactly one click playlist")
        order = BS.overview_order(out)
        self.assertEqual(sorted(order), list(range(len(order))),
                         msg=f"overview order {order} is not a valid 0..N permutation")
        # audio tracks unchanged (2 stereo); click contributes 0 audio channels
        self.assertEqual([(t.kind) for t in BS.track_types(out)], ["stereo", "stereo"])
        self.assertEqual(BS.channel_count(out), 4, msg="click must add 0 audio channels")


@unittest.skipUnless(_MIX_CLEAN.exists() and _MIX_CLICK.exists(),
                     "mono+stereo click control pair not present")
class MixedClickTests(unittest.TestCase):
    """The click is track-type-agnostic: the recursive diff reproduces a click on a
    MIXED (mono+stereo) audio session byte-for-byte, exactly as for all-stereo."""

    def test_add_click_mixed_byte_exact(self) -> None:
        """add_click on the mono+stereo control reproduces 'mono stereo plus
        click.ptx' byte-for-byte (seed-neutralized) and re-parses cleanly."""
        clean, ctrl = _load_path(_MIX_CLEAN), _load_path(_MIX_CLICK)
        out = BS.add_click(clean, clean, ctrl)
        self.assertEqual(_strip_seed(out), _strip_seed(ctrl),
                         msg="add_click not byte-exact on mono+stereo")
        self.assertEqual(_reload_ok(out), 0, msg="mixed add_click reload failed")
        self.assertEqual([(t.name, t.kind) for t in BS.track_types(out)],
                         [("Audio 1", "mono"), ("Audio 2", "stereo")])

    def test_synthesize_mixed_mono_stereo_click(self) -> None:
        """synthesize_mixed_session([1,2], click_ref=mono+stereo pair) appends a click
        to a mono+stereo session: reloads, gains one click playlist, keeps [mono,
        stereo] audio (3 channels, click adds 0), valid overview permutation, and the
        same block-type structure as the real control (differs only in the cosmetic
        overview-order permutation). No-growth case: donor already matches [1,2]."""
        from collections import Counter
        ms, msc = _load_path(_MIX_CLEAN), _load_path(_MIX_CLICK)
        mono3, ster3 = MONO / "3 mono tracks.ptx", STER / "3 stereo tracks.ptx"
        if not (mono3.exists() and ster3.exists()):
            self.skipTest("need 3 mono + 3 stereo libraries")
        out = BS.synthesize_mixed_session(
            [1, 2], donor_data=ms, mono_lib=(_load_path(mono3), 3),
            stereo_lib=(_load_path(ster3), 3), click_ref=(ms, msc),
        )
        self.assertEqual(_reload_ok(out), 0, msg="mixed[1,2]+click reload failed")
        self.assertEqual([t.kind for t in BS.track_types(out)], ["mono", "stereo"])
        self.assertEqual(BS.channel_count(out), 3, msg="click must add 0 audio channels")
        n261e = sum(1 for b in BS.flat_blocks(BS.parse(out)) if b.content_type == 0x261E)
        self.assertEqual(n261e, 1, msg="expected exactly one click playlist")
        order = BS.overview_order(out)
        self.assertEqual(sorted(order), list(range(len(order))),
                         msg=f"overview order {order} not a valid permutation")
        self.assertEqual(Counter(b.content_type for b in BS.flat_blocks(BS.parse(out))),
                         Counter(b.content_type for b in BS.flat_blocks(BS.parse(msc))),
                         msg="block structure differs from real mono+stereo+click")


if __name__ == "__main__":
    unittest.main()
