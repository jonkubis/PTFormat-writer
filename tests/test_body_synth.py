"""Tests for the empty-stereo body synthesizer (`ptxformatwriter.body_synth`).

`synthesize_stereo_session` grows a donor session to N tracks. Validation is
structural (byte-exact is impossible across separate saves — FILETIMEs/session
metadata differ): the synthesized session must have the same per-type block
counts as the real N-track control and must re-parse cleanly. A 3-track result
from this pipeline is confirmed to open in Pro Tools.
"""
from collections import Counter
from pathlib import Path
import tempfile
import unittest

from ptxformatwriter.core import PTFFormat
from ptxformatwriter import body_synth as BS, writer as W, final_index as FI

ROOT = Path(__file__).resolve().parents[1]
CONTROL_DIR = ROOT / "control_files" / "lots of stereo tracks"
MONO_DIR = ROOT / "control_files" / "lots of mono tracks"
MIX_DIR = ROOT / "control_files" / "mixed tracks"
MTT_DIR = ROOT / "control_files" / "multiple track types"
MTT_NOCLICK = MTT_DIR / "multiple track types no click.ptx"
BIG_MONO = ROOT / "control_files" / "512 tracks" / "512 mono tracks.ptx"


def _control(n: int) -> Path:
    return CONTROL_DIR / f"{n} stereo tracks.ptx"


def _mono_control(n: int) -> Path:
    return MONO_DIR / f"{n} mono tracks.ptx"


def _mono_available() -> set[int]:
    return {n for n in range(1, 17) if _mono_control(n).exists()}


def _load_path(p: Path) -> bytes:
    ptf = PTFFormat()
    ptf.load(str(p), 48000)
    return ptf.unxored_data()


def _available() -> set[int]:
    return {n for n in range(1, 17) if _control(n).exists()}


def _load(n: int) -> bytes:
    ptf = PTFFormat()
    ptf.load(str(_control(n)), 48000)
    return ptf.unxored_data()


def _type_counts(data: bytes) -> Counter:
    # Exclude the first top-level block: its "content_type" is actually the low 16
    # bits of the stored index-offset pointer (session-specific), not a structural
    # block type, so it legitimately differs between synth and the real control.
    blocks = BS.flat_blocks(BS.parse(data))
    first = min(blocks, key=lambda b: b.offset)
    counts: Counter = Counter()
    for b in blocks:
        if b is first:
            continue
        counts[b.content_type] += 1
    return counts


@unittest.skipUnless(_available(), "stereo-track control files not present")
class BodySynthTests(unittest.TestCase):
    def _pairs(self):
        have = _available()
        return [(b, t) for (b, t) in [(2, 3), (2, 4), (3, 8)] if b in have and t in have]

    def test_structural_match(self) -> None:
        """Synthesized session has identical per-type block counts to the real
        control (using the target control as the per-track library)."""
        pairs = self._pairs()
        self.assertTrue(pairs, "need adjacent stereo controls")
        for base, target in pairs:
            synth = BS.synthesize_stereo_session(
                _load(base), base, target, library_data=_load(target), library_total=target
            )
            self.assertEqual(
                _type_counts(synth),
                _type_counts(_load(target)),
                msg=f"block-count structure mismatch for {base}->{target}",
            )

    def test_reloads_clean(self) -> None:
        """The synthesized + encrypted session re-loads without error."""
        for base, target in self._pairs():
            synth = BS.synthesize_stereo_session(
                _load(base), base, target, library_data=_load(target), library_total=target
            )
            with tempfile.NamedTemporaryFile(suffix=".ptx", delete=False) as tmp:
                tmp.write(W.encrypt_session_data(synth))
                path = tmp.name
            try:
                reloaded = PTFFormat()
                self.assertEqual(reloaded.load(path, 48000), 0, msg=f"reload failed for {base}->{target}")
            finally:
                Path(path).unlink(missing_ok=True)

    def test_overview_order_matches(self) -> None:
        """The synthesized session reproduces the real control's overview
        display-order permutation exactly (a permutation of 0..N-1). Growing the
        body track-by-track would otherwise leave a corrupt sequence with
        duplicate values; synthesize rewrites it from the target control."""
        for base, target in self._pairs():
            synth = BS.synthesize_stereo_session(
                _load(base), base, target, library_data=_load(target), library_total=target
            )
            got = BS.overview_order(synth)
            want = BS.overview_order(_load(target))
            self.assertEqual(got, want, msg=f"overview order mismatch for {base}->{target}")
            self.assertEqual(
                sorted(got), list(range(target)), msg=f"overview not a 0..N-1 permutation for {base}->{target}"
            )

    def test_no_stray_selection(self) -> None:
        """A synthesized session opens with no tracks selected. Growing inherits
        the donor's last-track selection AND the appended last track's selection;
        the synthesizer must normalise that away (else two tracks open selected)."""
        for base, target in self._pairs():
            synth = BS.synthesize_stereo_session(
                _load(base), base, target, library_data=_load(target), library_total=target
            )
            self.assertEqual(
                BS.selected_tracks(synth), [], msg=f"stray track selection for {base}->{target}"
            )

    def test_multidigit_names_no_phantom(self) -> None:
        """Growing past 9 tracks (multi-digit names "Audio 10"...) must (a) build
        a name table whose 0x2519 size matches the real control, and (b) not forge
        phantom 0x0003 blocks (the selection fix used to corrupt an adjacent
        size-2 0x2103 block's content type on multi-digit tracks)."""
        have = _available()
        if not ({6, 16} <= have):
            self.skipTest("need n6 + n16 controls")
        synth = BS.synthesize_stereo_session(_load(6), 6, 16, library_data=_load(16), library_total=16)
        real = _load(16)

        def _size_2519(data: bytes) -> int:
            return next(b.block_size for b in BS.flat_blocks(BS.parse(data)) if b.content_type == 0x2519)

        def _count(data: bytes, ct: int) -> int:
            return sum(1 for b in BS.flat_blocks(BS.parse(data)) if b.content_type == ct)

        self.assertEqual(_size_2519(synth), _size_2519(real), msg="0x2519 name-table size mismatch")
        self.assertEqual(_count(synth, 0x0003), _count(real, 0x0003), msg="phantom 0x0003 block(s)")
        # Overview scroll-extent: 0 for <=9 tracks, non-zero once tracks overflow
        # the window (n13-16=602). The grow leaves it 0; synthesize copies it from
        # the target control. A wrong value here loads in ptxformatwriter but Pro Tools
        # rejects it ("magic ID does not match").
        self.assertEqual(
            BS.overview_extent(synth), BS.overview_extent(real), msg="overview scroll-extent mismatch"
        )
        self.assertNotEqual(BS.overview_extent(synth), 0, msg="extent should be non-zero at 16 tracks")

    def test_window_state_and_size(self) -> None:
        """Window state must match the target: (a) no body-size shift (the
        session-info 0x2067 size is matched to the target, else a 1-byte
        session-name-length difference shifts every later block and Pro Tools
        rejects it at >=10 tracks); (b) the visible-track marker (0x261c run1)
        pattern matches the real control (no stray donor marker)."""
        have = _available()
        if not ({6, 16} <= have):
            self.skipTest("need n6 + n16 controls")
        synth = BS.synthesize_stereo_session(_load(6), 6, 16, library_data=_load(16), library_total=16)
        real = _load(16)
        self.assertEqual(len(synth), len(real), msg="body size shifted vs real n16")

        run1 = bytes.fromhex("efffdfbf")

        def visible(data: bytes) -> list[int]:
            from ptxformatwriter import final_index as FI
            body = data[: FI.final_index_ref(data).start]
            blks = sorted(
                (b for b in BS.flat_blocks(BS.parse(data)) if b.content_type == 0x261C),
                key=lambda b: b.offset,
            )
            return [i + 1 for i, b in enumerate(blks) if run1 in body[b.offset : b.offset + b.block_size]]

        self.assertEqual(visible(synth), visible(real), msg="visible-track marker pattern mismatch")

    def test_rename_track(self) -> None:
        """rename_track replaces a track's name in all 8 length-prefixed slots
        (shorter or longer), removes the old name, leaves other tracks untouched,
        keeps the index offsets valid, and re-parses cleanly."""
        from ptxformatwriter import final_index as FI
        if 3 not in _available():
            self.skipTest("need 3-track control")
        base = _load(3)
        name_block_types = {0x2519, 0x251A, 0x1052, 0x1014, 0x210B, 0x2619}

        def in_name_blocks(data: bytes, name: str) -> int:
            ptf = BS.parse(data)
            blocks = BS.flat_blocks(ptf)
            n = 0
            for j in BS.track_name_occurrences(data, name):
                owner = max((b for b in blocks if b.offset - 7 <= j < b.offset + b.block_size),
                            key=lambda b: b.offset, default=None)
                if owner is not None and owner.content_type in name_block_types:
                    n += 1
            return n

        def bad_offsets(data: bytes) -> int:
            ref = FI.final_index_ref(data); z2t, bt = FI.block_layout(data)
            recs = FI.parse_records(ref.data, set(bt), set(z2t))
            vals = []
            for r in recs:
                for c in r.child_refs: vals.append(c.offset)
                for el in r.elements: vals += el.offsets
            return sum(1 for v in vals if v and (v >= len(data) or data[v] != 0x5A))

        def index_ptr(data: bytes) -> tuple[int, int]:
            first = min(BS.flat_blocks(BS.parse(data)), key=lambda b: b.offset)
            return int.from_bytes(data[first.offset:first.offset+4], "little"), FI.final_index_ref(data).start

        for new in ("Kick", "Bass Guitar DI Box (comp)"):
            out = BS.rename_track(base, "Audio 2", new)
            self.assertEqual(in_name_blocks(out, new), 8, msg=f"{new!r} not in 8 name slots")
            self.assertEqual(BS.track_name_occurrences(out, "Audio 2"), [], msg="old name remains")
            self.assertEqual(in_name_blocks(out, "Audio 1"), 8, msg="Audio 1 disturbed")
            self.assertEqual(in_name_blocks(out, "Audio 3"), 8, msg="Audio 3 disturbed")
            self.assertEqual(bad_offsets(out), 0, msg="index offset doesn't land on a ZMARK")
            # the first block's stored index-offset pointer must track the moved index
            ptr, start = index_ptr(out)
            self.assertEqual(ptr, start, msg=f"stale index-offset pointer for {new!r} ({ptr} != {start})")
            with tempfile.NamedTemporaryFile(suffix=".ptx", delete=False) as tmp:
                tmp.write(W.encrypt_session_data(out)); path = tmp.name
            try:
                self.assertEqual(PTFFormat().load(path, 48000), 0, msg=f"reload failed for {new!r}")
            finally:
                Path(path).unlink(missing_ok=True)

    def test_set_session_name(self) -> None:
        """The embedded session name (the primary `.ptx` string in 0x2067) can be
        read and changed. Changing its length shifts the body, so this exercises
        the index-offset pointer fix on the 0x2067 block (the original
        session-name "magic ID" saga field) and must leave track names intact."""
        from ptxformatwriter import final_index as FI
        if 3 not in _available():
            self.skipTest("need 3-track control")
        base = _load(3)
        self.assertEqual(BS.session_name(base), "3 stereo tracks.ptx")
        for new in ("My Song", "A Much Longer Session Title Here"):
            out = BS.set_session_name(base, new)
            self.assertEqual(BS.session_name(out), new + ".ptx", msg=f"session name not set to {new!r}")
            first = min(BS.flat_blocks(BS.parse(out)), key=lambda b: b.offset)
            ptr = int.from_bytes(out[first.offset:first.offset+4], "little")
            self.assertEqual(ptr, FI.final_index_ref(out).start, msg="stale index-offset pointer after session rename")
            for k in (1, 2, 3):
                self.assertEqual(len(BS.track_name_occurrences(out, f"Audio {k}")), 8, msg=f"Audio {k} disturbed")
            with tempfile.NamedTemporaryFile(suffix=".ptx", delete=False) as tmp:
                tmp.write(W.encrypt_session_data(out)); path = tmp.name
            try:
                self.assertEqual(PTFFormat().load(path, 48000), 0, msg=f"reload failed for session {new!r}")
            finally:
                Path(path).unlink(missing_ok=True)

    def test_emitter_round_trips(self) -> None:
        """apply_insertions with no insertions reproduces the body exactly."""
        from ptxformatwriter import final_index as FI
        for n in sorted(_available())[:4]:
            data = _load(n)
            body = data[: FI.final_index_ref(data).start]
            ptf = BS.parse(body)
            self.assertEqual(BS.apply_insertions(body, ptf, []), body, msg=f"emitter round-trip n={n}")

    def test_track_types_stereo(self) -> None:
        """track_types reads every clean-series track as stereo, and
        channel_count == 2N == the session's 0x1054+2 field."""
        for n in sorted(_available())[:6]:
            data = _load(n)
            tracks = BS.track_types(data)
            self.assertEqual([t.kind for t in tracks], ["stereo"] * n, msg=f"n={n} kinds")
            self.assertEqual(BS.channel_count(data), 2 * n, msg=f"n={n} channel_count")

    @unittest.skipUnless(_mono_available(), "mono control files not present")
    def test_mono_synthesis(self) -> None:
        """synthesize_mono_session grows a mono donor to N mono tracks: identical
        per-type block counts to the real mono control, re-parses cleanly, yields
        an all-mono track list, and channel_count == N (one channel per track)."""
        def mono(n: int) -> bytes:
            ptf = PTFFormat(); ptf.load(str(_mono_control(n)), 48000); return ptf.unxored_data()
        have = _mono_available()
        pairs = [(b, t) for (b, t) in [(2, 3), (2, 4), (3, 5), (3, 6)] if b in have and t in have]
        self.assertTrue(pairs, "need adjacent mono controls")
        for base, target in pairs:
            synth = BS.synthesize_mono_session(mono(base), base, target, mono(target), target)
            self.assertEqual(_type_counts(synth), _type_counts(mono(target)),
                             msg=f"mono block-count mismatch {base}->{target}")
            self.assertEqual([t.kind for t in BS.track_types(synth)], ["mono"] * target,
                             msg=f"not all-mono {base}->{target}")
            self.assertEqual(BS.channel_count(synth), target, msg=f"mono channels {base}->{target}")
            with tempfile.NamedTemporaryFile(suffix=".ptx", delete=False) as tmp:
                tmp.write(W.encrypt_session_data(synth)); path = tmp.name
            try:
                self.assertEqual(PTFFormat().load(path, 48000), 0, msg=f"mono reload {base}->{target}")
            finally:
                Path(path).unlink(missing_ok=True)

    def test_set_track_channels(self) -> None:
        """set_track_channels rewrites a track's 0x1014 channel indices (mono or
        stereo) at the marker/name-relative positions, reads back correctly, and
        re-parses cleanly. (The channel-map rewrite that mix-and-match uses to place
        a track at its cumulative channel allocation.)"""
        if 2 not in _available():
            self.skipTest("need 2-track stereo control")
        data = _load(2)
        t0 = BS.track_types(data)[0]  # stereo Audio 1, channels [0,1]
        self.assertEqual(BS.track_channel_indices(data, t0.offset), [0, 1])
        out = BS.set_track_channels(data, t0.offset, [3, 4])
        self.assertEqual(BS.track_channel_indices(out, t0.offset), [3, 4], msg="stereo rewrite")
        # only the channel bytes changed (same length, re-parses)
        self.assertEqual(len(out), len(data))
        self.assertEqual(len(BS.flat_blocks(BS.parse(out))), len(BS.flat_blocks(BS.parse(data))))

    @unittest.skipUnless((MIX_DIR / "stereo mono stereo.ptx").exists(), "mixed control not present")
    def test_mixed_channel_allocation(self) -> None:
        """track_channel_indices reads the cumulative channel allocation from a real
        mixed control: stereo+mono+stereo -> [0,1], [2], [3,4]."""
        data = _load_path(MIX_DIR / "stereo mono stereo.ptx")
        got = [BS.track_channel_indices(data, t.offset) for t in BS.track_types(data)]
        self.assertEqual(got, [[0, 1], [2], [3, 4]])

    @unittest.skipUnless((MIX_DIR / "stereo mono stereo.ptx").exists() and _mono_available() and _available(),
                         "mixed + mono + stereo controls needed")
    def test_synthesize_mixed(self) -> None:
        """synthesize_mixed_session composes mono+stereo in any order: the result
        has the requested per-track types, the cumulative channel allocation, and
        re-parses cleanly. Covers a 2-track (no-growth) and 3-track (grown) case in
        both orderings."""
        def mono(n):
            p = MONO_DIR / f"{n} mono tracks.ptx"; ptf = PTFFormat(); ptf.load(str(p), 48000); return ptf.unxored_data()
        mono_lib = (mono(3), 3)
        stereo_lib = (_load(3), 3)
        cases = [
            ([2, 1], "stereo mono.ptx", "stereo mono.ptx"),
            ([1, 2], "mono stereo.ptx", "mono stereo.ptx"),
            ([2, 1, 2], "stereo mono.ptx", "stereo mono stereo.ptx"),
            ([1, 2, 1], "mono stereo.ptx", "mono stereo mono.ptx"),
        ]
        for specs, donor_fn, order_fn in cases:
            donor = _load_path(MIX_DIR / donor_fn)
            ov = BS.overview_order(_load_path(MIX_DIR / order_fn))
            synth = BS.synthesize_mixed_session(specs, donor, mono_lib, stereo_lib, ov, target_leaf="mixed tracks")
            kinds = [(t.kind, t.channels) for t in BS.track_types(synth)]
            self.assertEqual(kinds, [("stereo" if c == 2 else "mono", c) for c in specs], msg=f"types {specs}")
            chans, base = [], 0
            for c in specs:
                chans.append(list(range(base, base + c))); base += c
            got = [BS.track_channel_indices(synth, t.offset) for t in BS.track_types(synth)]
            self.assertEqual(got, chans, msg=f"channel allocation {specs}")
            self.assertEqual(BS.channel_count(synth), sum(specs), msg=f"channel count {specs}")
            with tempfile.NamedTemporaryFile(suffix=".ptx", delete=False) as tmp:
                tmp.write(W.encrypt_session_data(synth)); path = tmp.name
            try:
                self.assertEqual(PTFFormat().load(path, 48000), 0, msg=f"reload {specs}")
            finally:
                Path(path).unlink(missing_ok=True)

    @unittest.skipUnless({2, 3} <= set(_available()) and {2, 3} <= set(_mono_available()),
                         "need 2/3-track mono + stereo controls")
    def test_synthesize_mixed_arbitrary_default_order(self) -> None:
        """synthesize_mixed_session composes a config with NO matching control,
        using the default (identity) overview order — proving arbitrary mix-and-match
        needs no control. [stereo, stereo, mono] from a 2-stereo donor."""
        def mono(n):
            p = MONO_DIR / f"{n} mono tracks.ptx"; ptf = PTFFormat(); ptf.load(str(p), 48000); return ptf.unxored_data()
        specs = [2, 2, 1]
        synth = BS.synthesize_mixed_session(specs, _load(2), (mono(3), 3), (_load(3), 3))  # order defaults to identity
        self.assertEqual(BS.overview_order(synth), [0, 1, 2], msg="default identity order")
        self.assertEqual([(t.kind, t.channels) for t in BS.track_types(synth)],
                         [("stereo", 2), ("stereo", 2), ("mono", 1)])
        self.assertEqual([BS.track_channel_indices(synth, t.offset) for t in BS.track_types(synth)],
                         [[0, 1], [2, 3], [4]], msg="cumulative channel allocation")
        with tempfile.NamedTemporaryFile(suffix=".ptx", delete=False) as tmp:
            tmp.write(W.encrypt_session_data(synth)); path = tmp.name
        try:
            self.assertEqual(PTFFormat().load(path, 48000), 0)
        finally:
            Path(path).unlink(missing_ok=True)

    @unittest.skipUnless(BIG_MONO.exists() and 12 in _available(),
                         "need 512-mono universal library + 12-stereo control")
    def test_synthesize_mixed_high_n_512lib(self) -> None:
        """Arbitrary mix at N>9 sourcing mono units from the 512-mono control as a
        universal library: 9 mono (index 9 is past the old 0-8 mono lib) + 3 stereo,
        from a 2-mono donor. PT-confirmed at N=16/20 (test_stressA/B, 2026-05-30).
        Guards: track order/types, cumulative channel allocation, a clean final index
        (every offset hole resolves to a real block — no zeros/danglers), clean reload."""
        mono512 = _load_path(BIG_MONO)
        specs = [1] * 9 + [2] * 3            # mono at index 9 requires a >8-track mono lib
        synth = BS.synthesize_mixed_session(specs, _load_path(_mono_control(2)),
                                            (mono512, 512), (_load(12), 12))
        self.assertEqual([t.channels for t in BS.track_types(synth)], specs, msg="track order/types")
        self.assertEqual(BS.channel_count(synth), sum(specs))
        base, want = 0, []
        for ch in specs:
            want.append(list(range(base, base + ch))); base += ch
        self.assertEqual([BS.track_channel_indices(synth, t.offset) for t in BS.track_types(synth)],
                         want, msg="cumulative channel allocation")
        ref = FI.final_index_ref(synth)
        z2t, by_type = FI.block_layout(synth); zmarks = set(z2t)
        recs = FI.parse_records(ref.data, set(by_type), zmarks)
        offs = [c.offset for r in recs for c in r.child_refs] + \
               [o for r in recs for e in r.elements for o in e.offsets]
        self.assertTrue(offs and all(o in zmarks for o in offs), msg="all index offsets resolve")
        with tempfile.NamedTemporaryFile(suffix=".ptx", delete=False) as tmp:
            tmp.write(W.encrypt_session_data(synth)); path = tmp.name
        try:
            self.assertEqual(PTFFormat().load(path, 48000), 0)
        finally:
            Path(path).unlink(missing_ok=True)

    @unittest.skipUnless((CONTROL_DIR / "512 stereo tracks.ptx").exists() and 8 in _available(),
                         "need 512-stereo universal library + 8-stereo donor")
    def test_name_table_special_last_non_matching(self) -> None:
        """REGRESSION (PT-confirmed 2026-06-19, macpair N=30): with no matching N-track
        control, synthesize_mixed_session must rebuild the 0x2519 name table as
        [header][N-1 normal][1 special-last]. The grow path otherwise strands the donor's
        SPECIAL last entry mid-table (only base_n entries parse) -> Pro Tools throws
        'end of stream encountered'. Guards: all N name entries present + clean reload."""
        N = 30
        lib512 = _load_path(CONTROL_DIR / "512 stereo tracks.ptx")
        synth = BS.synthesize_mixed_session([2] * N, _load(8), (b"", 0), (lib512, 512))
        blk = BS._by_type(BS.parse(synth), 0x2519)[0]
        first_child = min(c.offset - 7 for c in blk.child)
        n_entries = len(BS._name_table_entries(synth[blk.offset:first_child]))
        self.assertEqual(n_entries, N, msg="0x2519 must contain all N name entries (special-last fix)")
        self.assertEqual(len(BS.track_types(synth)), N)
        with tempfile.NamedTemporaryFile(suffix=".ptx", delete=False) as tmp:
            tmp.write(W.encrypt_session_data(synth)); path = tmp.name
        try:
            self.assertEqual(PTFFormat().load(path, 48000), 0)
        finally:
            Path(path).unlink(missing_ok=True)

    @unittest.skipUnless(MTT_NOCLICK.exists(), "multiple-track-types control not present")
    def test_track_types_mixed(self) -> None:
        """track_types distinguishes mono / stereo / MIDI in the mixed control:
        Audio 1 (mono, 1ch), Audio 2 (stereo, 2ch), MIDI 1 (midi, 0ch); total
        audio channels = 3 = the 0x1054+2 field."""
        data = _load_path(MTT_NOCLICK)
        got = {t.name: (t.kind, t.channels) for t in BS.track_types(data)}
        self.assertEqual(got.get("Audio 1"), ("mono", 1), msg="Audio 1 should be mono")
        self.assertEqual(got.get("Audio 2"), ("stereo", 2), msg="Audio 2 should be stereo")
        self.assertEqual(got.get("MIDI 1"), ("midi", 0), msg="MIDI 1 should be midi")
        self.assertEqual(BS.channel_count(data), 3, msg="total audio channels")


class StereoInlineTests(unittest.TestCase):
    """The library-free, no-external-files stereo synthesizer (`synthesize_stereo_inline`):
    inlined 8-track scaffold + two inlined 512-control unit templates + `synth_stereo_unit`
    replace BOTH the per-N stereo library and the donor file. These tests need NO control
    files (they exercise the inlined assets) -- so they run everywhere."""

    def test_inline_builds_with_no_external_files(self) -> None:
        """`synthesize_stereo_inline(n)` builds a valid n-stereo session from inlined assets
        alone: correct track count, all n name-table entries, clean reparse + reader load.
        n=12 and n=30 are byte-identical to PT-confirmed files (verified at integration)."""
        for n in (8, 17, 30, 63):
            synth = BS.synthesize_stereo_inline(n)
            self.assertEqual(len(BS.track_types(synth)), n, msg=f"n={n} track count")
            blk = BS._by_type(BS.parse(synth), 0x2519)[0]
            first_child = min(c.offset - 7 for c in blk.child)
            n_entries = len(BS._name_table_entries(synth[blk.offset:first_child]))
            self.assertEqual(n_entries, n, msg=f"n={n}: all name entries present")
            with tempfile.NamedTemporaryFile(suffix=".ptx", delete=False) as tmp:
                tmp.write(W.encrypt_session_data(synth)); path = tmp.name
            try:
                self.assertEqual(PTFFormat().load(path, 48000), 0, msg=f"n={n} reload")
            finally:
                Path(path).unlink(missing_ok=True)

    def test_inline_guards(self) -> None:
        """n below the scaffold (8) has no grow path; n above the validated ceiling (418)
        hits the 0x5A-in-data scan-parse limit -- both raise ValueError, never silent
        breakage."""
        with self.assertRaises(ValueError):
            BS.synthesize_stereo_inline(5)
        with self.assertRaises(ValueError):
            BS.synthesize_stereo_inline(BS._STEREO_INLINE_MAX + 1)

    def test_inline_dup_mode_also_builds(self) -> None:
        """`unique=False` (per-track GUID/blob duplicated) is also PT-valid (confirmed:
        nolib30_dup) -- guard that it still produces a well-formed n-track session."""
        synth = BS.synthesize_stereo_inline(12, unique=False)
        self.assertEqual(len(BS.track_types(synth)), 12)

    def test_generated_free_fields_are_magic_free(self) -> None:
        """REGRESSION: the block parser scans payloads for the 0x5A magic, so the GENERATED
        free fields (per-track 8-byte handle + 160 B blob) must contain no 0x5A -- otherwise
        they form phantom blocks when the grow re-parses (handle 0xE000+346 = 0x..5A broke
        N>=347). The deterministic fields match a real control, so only these need guarding."""
        from ptxformatwriter import _stereo_data as SD
        t1, t2, t3 = SD.stereo_templates()
        for k in (90, 346, 512):   # k where the generated handle/blob would carry a 0x5A
            u = BS.synth_stereo_unit(k, t1, t2, t3)
            M = {1: BS._STEREO_MAP_1, 2: BS._STEREO_MAP_2, 3: BS._STEREO_MAP_3}[len(str(k))]
            a, b = M["b261c"]["blob"]
            self.assertNotIn(0x5A, u.b261c[a:b], msg=f"k={k}: blob carries the 0x5A magic")
            blocks = BS._stereo_unit_blocks(u)
            for key, spec in M.items():
                for off in spec.get("guid", []):
                    self.assertNotIn(0x5A, blocks[key][off:off + 2],
                                     msg=f"k={k} {key}@{off}: handle carries the 0x5A magic")


@unittest.skipUnless((CONTROL_DIR / "512 stereo tracks.ptx").exists(),
                     "need the 512-stereo control to validate the deterministic re-key")
class SynthStereoUnitTests(unittest.TestCase):
    """`synth_stereo_unit`'s DETERMINISTIC re-key (name, lane indices, track #, the ten
    element ids, the k/k-1 counters) must reproduce the real 512-control unit byte-for-byte
    -- the property that lets the generator replace the library. Only the self-contained
    GUID/blob are regenerated (PT needs them only present), so those sites are excluded."""

    def test_deterministic_rekey_reproduces_512_unit(self) -> None:
        lib512 = _load_path(CONTROL_DIR / "512 stereo tracks.ptx")
        for k in (3, 9, 10, 30, 63):                       # both digit ranges
            ref = BS.extract_track(lib512, k, 512, channels=2)
            ref_blocks = BS._stereo_unit_blocks(ref)
            M = BS._STEREO_MAP_1 if len(str(k)) == 1 else BS._STEREO_MAP_2
            for key in BS._STEREO_UNIT_KEYS:
                spec = M.get(key)
                if spec is None:
                    continue
                buf = bytearray(ref_blocks[key])           # start from the REAL unit
                o, w = spec["name"]
                buf[o:o + w] = str(k).encode()
                for off, wd, fn in spec["lin"]:
                    buf[off:off + wd] = int(fn(k)).to_bytes(wd, "little")
                self.assertEqual(bytes(buf), ref_blocks[key],
                                 msg=f"k={k} {key}: deterministic re-key must equal the real unit")


if __name__ == "__main__":
    unittest.main()
