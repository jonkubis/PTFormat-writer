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


_MONO512 = ROOT / "control_files" / "512 tracks" / "512 mono tracks.ptx"


@unittest.skipUnless(_MONO512.exists(), "need the 512-mono control to validate the mono re-key")
class SynthMonoUnitTests(unittest.TestCase):
    """`synth_mono_unit`'s deterministic re-key must reproduce the real 512-mono unit
    byte-for-byte (the property that lets the generator replace a mono library). Mono is
    the audio structure with ONE 0x1052 lane and channel index k-1."""

    def test_deterministic_rekey_reproduces_512_mono_unit(self) -> None:
        d = _load_path(_MONO512)
        for k in (3, 9, 10, 99, 100, 256, 300, 511):     # both digit ranges + the u16 transition
            ref_blocks = BS._mono_unit_blocks(BS.extract_track(d, k, 512, channels=1))
            M = {1: BS._MONO_MAP_1, 2: BS._MONO_MAP_2, 3: BS._MONO_MAP_3}[len(str(k))]
            for key, spec in M.items():
                buf = bytearray(ref_blocks[key])
                o, w = spec["name"]
                buf[o:o + w] = str(k).encode()
                for off, wd, fn in spec["lin"]:
                    buf[off:off + wd] = int(fn(k)).to_bytes(wd, "little")
                self.assertEqual(bytes(buf), ref_blocks[key],
                                 msg=f"k={k} {key}: mono re-key must equal the real unit")

    def test_synth_mono_unit_is_one_lane(self) -> None:
        d = _load_path(_MONO512)
        t = tuple(BS.extract_track(d, kk, 512, channels=1) for kk in (2, 10, 100))
        u = BS.synth_mono_unit(30, *t)
        self.assertEqual(len(u.b1052), 1, msg="mono unit must have exactly one 0x1052 lane")
        self.assertEqual(u.name_entry[10:12], b"30", msg="track-30 name digits")


class MonoInlineTests(unittest.TestCase):
    """`synthesize_mono_inline` builds an n-mono session from inlined assets alone (8-track
    scaffold + 512-mono templates in `_mono_data`), no external control files."""

    def test_inline_mono_builds(self) -> None:
        for n in (8, 30, 100):
            synth = BS.synthesize_mono_inline(n)
            self.assertEqual(len(BS.track_types(synth)), n, msg=f"n={n} tracks")
            self.assertEqual(BS.channel_count(synth), n, msg=f"n={n} mono = 1 channel/track")
            blk = BS._by_type(BS.parse(synth), 0x2519)[0]
            first_child = min(c.offset - 7 for c in blk.child)
            self.assertEqual(len(BS._name_table_entries(synth[blk.offset:first_child])), n)
            with tempfile.NamedTemporaryFile(suffix=".ptx", delete=False) as tmp:
                tmp.write(W.encrypt_session_data(synth)); path = tmp.name
            try:
                self.assertEqual(PTFFormat().load(path, 48000), 0, msg=f"n={n} reload")
            finally:
                Path(path).unlink(missing_ok=True)

    def test_inline_mono_no_chimera(self) -> None:
        """The 512-mono templates' folder leaf is normalized to the scaffold's (no chimera)."""
        from ptxformatwriter import _mono_data as MD
        synth = BS.synthesize_mono_inline(30)
        self.assertEqual(BS.track_name_occurrences(synth, MD.TEMPLATE_LEAF), [],
                         msg="template folder leaf must be renamed away")

    def test_inline_mono_guards(self) -> None:
        with self.assertRaises(ValueError):
            BS.synthesize_mono_inline(5)
        with self.assertRaises(ValueError):
            BS.synthesize_mono_inline(BS._STEREO_INLINE_MAX + 1)


class MixedInlineTests(unittest.TestCase):
    """`synthesize_mixed_inline` builds an arbitrary mono+stereo ORDER from inlined assets
    (4 start-pair donors in `_mixed_data` + the stereo/mono unit templates), no external files."""

    def test_arbitrary_order_builds(self) -> None:
        from ptxformatwriter import _mixed_data as XD
        for spec in ([2, 1, 2, 1, 2, 1, 1, 2, 2, 1], [1, 2, 1, 2, 1, 2, 2, 1, 1, 2],
                     [2] * 8, [1] * 8, [2, 1] * 10):
            synth = BS.synthesize_mixed_inline(spec)
            self.assertEqual([t.channels for t in BS.track_types(synth)], spec, msg=f"spec {spec}")
            self.assertEqual(BS.channel_count(synth), sum(spec), msg="total channels")
            base, want = 0, []
            for ch in spec:
                want.append(list(range(base, base + ch))); base += ch
            self.assertEqual([BS.track_channel_indices(synth, t.offset) for t in BS.track_types(synth)],
                             want, msg="cumulative channel allocation")
            donor = XD.mixed_donor(spec[0], spec[1])
            dl = BS._folder_leaf(donor)
            for tl in (XD.STEREO_TEMPLATE_LEAF, XD.MONO_TEMPLATE_LEAF):
                if tl != dl:
                    self.assertEqual(BS.track_name_occurrences(synth, tl), [],
                                     msg=f"template leaf {tl!r} must be normalized (no chimera)")
            with tempfile.NamedTemporaryFile(suffix=".ptx", delete=False) as tmp:
                tmp.write(W.encrypt_session_data(synth)); path = tmp.name
            try:
                self.assertEqual(PTFFormat().load(path, 48000), 0, msg=f"reload {spec}")
            finally:
                Path(path).unlink(missing_ok=True)

    def test_mixed_guards(self) -> None:
        with self.assertRaises(ValueError):
            BS.synthesize_mixed_inline([2])           # < 2 tracks (no grow anchor)
        with self.assertRaises(ValueError):
            BS.synthesize_mixed_inline([2, 3])        # 3 = invalid channel count


class SynthesizeTests(unittest.TestCase):
    """The unified `synthesize(spec, click=...)`: arbitrary mono+stereo order + an optional
    single Click track (top/bottom), all from inlined assets (no external control files)."""

    def test_synthesize_variants(self) -> None:
        cases = [([2] * 12, "top"), ([1] * 12, "bottom"), ([2, 1, 2, 1, 2, 1, 1, 2], "bottom"),
                 ([2, 1, 2, 1], None), ([1, 2, 1], "top")]
        for spec, click in cases:
            out = BS.synthesize(spec, click=click)
            self.assertEqual([t.channels for t in BS.track_types(out)], spec, msg=f"spec {spec}")
            self.assertEqual(bool(BS.track_name_occurrences(out, "Click 1")), click is not None,
                             msg=f"click {click} for {spec}")
            with tempfile.NamedTemporaryFile(suffix=".ptx", delete=False) as tmp:
                tmp.write(W.encrypt_session_data(out)); path = tmp.name
            try:
                self.assertEqual(PTFFormat().load(path, 48000), 0, msg=f"reload {spec} click={click}")
            finally:
                Path(path).unlink(missing_ok=True)

    def test_synthesize_click_guard(self) -> None:
        with self.assertRaises(ValueError):
            BS.synthesize([2, 2], click="middle")

    def test_no_volume_view_blocks(self) -> None:
        """REGRESSION: every track's edit-window view must be WAVEFORM, not volume. The click
        splice (and a click-converted donor/scaffold track) can leave volume-view blocks (0x203b
        under 0x2015/0x2589); `synthesize` runs `set_waveform_view` last to force waveform."""
        for spec, click in (([2, 1, 2, 1, 2, 1, 1, 2], "bottom"), ([2] * 12, "top"),
                            ([1, 2, 1], None)):
            out = BS.synthesize(spec, click=click)
            ptf = BS.parse(out)
            ctype: dict = {}
            par: dict = {}

            def _rec(b, p):
                z = b.offset - 7
                ctype[z] = b.content_type
                par[z] = p
                for c in b.child:
                    _rec(c, z)

            for b in ptf.blocks:
                _rec(b, None)
            vol = sum(1 for z, ct in ctype.items()
                      if ct == 0x203b and ctype.get(par[z]) in (0x2015, 0x2589)
                      and out[z:z + len(BS._VIEW_VOLUME)] == BS._VIEW_VOLUME)
            self.assertEqual(vol, 0, msg=f"spec {spec} click={click}: {vol} volume-view blocks left")


_MIDI8 = CONTROL_DIR / "8 MIDI tracks.ptx"


@unittest.skipUnless(_MIDI8.exists(), "need the 8-MIDI control")
class MidiTests(unittest.TestCase):
    """MIDI track synthesis: `synth_midi_unit` (its own block set, no audio lanes) +
    `grow_one_midi_track` + the MIDI index path (`final_index.add_midi_track`). Grow the 8-MIDI
    control to 9 and build the index; every index offset must resolve (no danglers) + reload."""

    def test_midi_grow_and_index(self) -> None:
        d = _load_path(_MIDI8)
        body = d[:FI.final_index_ref(d).start]
        tmpl = BS.extract_midi_unit(d, 2, 8)
        body9 = BS.grow_one_midi_track(body, 8, BS.synth_midi_unit(9, tmpl))
        self.assertEqual(len(BS._by_type(BS.parse(body9), 0x2620)), 9, msg="9 MIDI playlist blocks")
        body9 = BS._fix_name_table_special_last(body9, 9)
        idx = FI.compose_index(d, body9 + d[FI.final_index_ref(d).start:], 8, 9,
                               channels=[0] * 9, track_names=[f"MIDI {i + 1}" for i in range(9)],
                               midi_tracks={9})
        out = BS._set_index_offset(body9 + idx)
        ref = FI.final_index_ref(out)
        z2t, by_type = FI.block_layout(out)
        zmarks = set(z2t)
        recs = FI.parse_records(ref.data, set(by_type), zmarks)
        offs = [c.offset for r in recs for c in r.child_refs] + \
               [o for r in recs for e in r.elements for o in e.offsets]
        self.assertTrue(offs and all(o in zmarks for o in offs), msg="all MIDI index offsets resolve")
        with tempfile.NamedTemporaryFile(suffix=".ptx", delete=False) as tmp:
            tmp.write(W.encrypt_session_data(out)); path = tmp.name
        try:
            self.assertEqual(PTFFormat().load(path, 48000), 0, msg="9-MIDI reload")
        finally:
            Path(path).unlink(missing_ok=True)

    def test_synth_midi_unit_single_digit_gate(self) -> None:
        tmpl = BS.extract_midi_unit(_load_path(_MIDI8), 2, 8)
        self.assertIn(b"MIDI 7", BS.synth_midi_unit(7, tmpl)["name_entry"])
        with self.assertRaises(ValueError):     # multi-digit MIDI is gated (no >=10-track control)
            BS.synth_midi_unit(10, tmpl)

    def _name_entry_sizes(self, body: bytes) -> list[int]:
        """Per-entry byte sizes of the 0x2519 own-byte name table (start-to-start)."""
        b = BS._by_type(BS.parse(body), 0x2519)[0]
        fc = min(c.offset - 7 for c in b.child)
        region = body[b.offset:fc]
        starts = [m.start() - 4 for m in BS._NAME_RE.finditer(region)]
        return [(starts[i + 1] - starts[i]) if i + 1 < len(starts) else (len(region) - starts[i])
                for i in range(len(starts))]

    def test_midi_name_table_special_is_last(self) -> None:
        """PT-confirmed root cause (2026-06-20): the grow path strands the donor's SPECIAL (short)
        last name entry mid-table; PT reads at the normal stride, misaligns, and runs off the block
        -> "end of stream encountered". `_fix_name_table_special_last` must move the short entry to
        the LAST position. It silently no-ops on MIDI/Click if it hardcodes "Audio ", so assert it
        actually fired (the short entry is last, not stranded) and stayed size-preserving."""
        d = _load_path(_MIDI8)
        body = d[:FI.final_index_ref(d).start]
        body9 = BS.grow_one_midi_track(body, 8, BS.synth_midi_unit(9, BS.extract_midi_unit(d, 2, 8)))
        before = self._name_entry_sizes(body9)
        self.assertEqual(len(before), 9, msg="9 name entries")
        self.assertLess(before.index(min(before)), 8, msg="precondition: short entry is stranded mid-table")
        fixed = BS._fix_name_table_special_last(body9, 9)
        self.assertNotEqual(fixed, body9, msg="fix must fire on MIDI (a no-op re-opens the EOS bug)")
        after = self._name_entry_sizes(fixed)
        self.assertEqual(after.index(min(after)), 8, msg="SPECIAL (short) entry must be LAST")
        self.assertEqual(after[:8], [max(after)] * 8, msg="all but last are full-size")
        self.assertEqual(sum(after), sum(before), msg="size-preserving")


_MTT_FRESH = MTT_DIR / "multiple track types no click fresh.ptx"


@unittest.skipUnless(_MTT_FRESH.exists() and _MIDI8.exists(),
                     "need the multiple-track-types donor + 8-MIDI control")
class MixedMidiTests(unittest.TestCase):
    """Arbitrary mono+stereo+MIDI synthesis via `synthesize_mixed_midi` (the audio+MIDI weave on
    top of `synthesize_mixed_session`). The donor is a `multiple track types` control (internal
    order [mono, MIDI, stereo] = [1, 0, 2]); spec is INTERNAL order. Validates structure that our
    lenient reader would miss — channel multiset, the per-name SPECIAL-last name table, every index
    offset resolving to the right block type (incl. MIDI 0x2620 playlists), 0x2589 == n-1, no path
    chimera — then a reload. PT-confirmed at internal specs [1,0,2,1] / [1,0,2,1,0] (2026-06-20)."""

    SPECS = ([1, 0, 2, 1], [1, 0, 2, 1, 0], [1, 0, 2, 2, 0, 1], [1, 0, 2, 1, 2, 1, 0, 2, 0])

    def setUp(self) -> None:
        self.donor = _load_path(_MTT_FRESH)
        self.tmpl = BS.extract_midi_unit(_load_path(_MIDI8), 2, 8)

    def _build(self, spec):
        return BS.synthesize_mixed_midi(spec, self.donor, self.tmpl)

    def test_donor_internal_order(self) -> None:
        self.assertEqual(BS._internal_channel_order(self.donor), [1, 0, 2])

    def test_structure_and_reload(self) -> None:
        from ptxformatwriter import _mixed_data as _XD
        for spec in self.SPECS:
            with self.subTest(spec=spec):
                out = self._build(spec)
                n = len(spec)
                # channel multiset (display order differs from internal spec)
                self.assertEqual(Counter(t.channels for t in BS.track_types(out)), Counter(spec))
                # name table: per-name full sizes, SPECIAL (-2) on the last entry only
                b = BS._by_type(BS.parse(out), 0x2519)[0]
                fc = min(c.offset - 7 for c in b.child)
                region = out[b.offset:fc]
                ms = list(BS._NAME_RE.finditer(region))
                starts = [m.start() - 4 for m in ms]
                sizes = [(starts[i + 1] - starts[i]) if i + 1 < len(starts) else (len(region) - starts[i])
                         for i in range(len(starts))]
                fulls = [4 + (m.end() - m.start()) + BS._NAME_ENTRY_SUFFIX for m in ms]
                self.assertEqual(len(sizes), n)
                for i, (s, f) in enumerate(zip(sizes, fulls)):
                    self.assertEqual(s, f - 2 if i == n - 1 else f, msg=f"entry {i} size")
                # every index offset is a live ZMARK; MIDI playlists/lanes are referenced
                ref = FI.final_index_ref(out)
                z2t, by_type = FI.block_layout(out)
                zmarks = set(z2t)
                recs = FI.parse_records(ref.data, set(by_type), zmarks)
                offs = [c.offset for r in recs for c in r.child_refs] + \
                       [o for r in recs for e in r.elements for o in e.offsets]
                self.assertTrue(offs and all(o != 0 and o in zmarks for o in offs), msg="offsets resolve")
                cts = Counter(z2t[o] for o in offs)
                self.assertEqual(cts.get(0x2620, 0), spec.count(0), msg="one 0x2620 per MIDI track")
                self.assertGreater(cts.get(0x251a, 0), 0, msg="lanes referenced")
                # overview/counters + no path chimera
                self.assertEqual(len(BS._by_type(BS.parse(out), 0x2589)), n - 1, msg="0x2589 == n-1")
                for tl in (_XD.STEREO_TEMPLATE_LEAF, _XD.MONO_TEMPLATE_LEAF):
                    self.assertFalse(BS.track_name_occurrences(out, tl), msg=f"leaf {tl!r} normalized")
                # reload
                with tempfile.NamedTemporaryFile(suffix=".ptx", delete=False) as t:
                    t.write(W.encrypt_session_data(out))
                    p = t.name
                try:
                    self.assertEqual(PTFFormat().load(p, 48000), 0, msg=f"reload spec {spec}")
                finally:
                    Path(p).unlink(missing_ok=True)

    def test_misuse_guards(self) -> None:
        with self.assertRaises(ValueError):          # spec[:base_n] must match donor internal order
            self._build([2, 0, 1, 1])
        with self.assertRaises(ValueError):          # grown MIDI must sit at internal position <= 9
            self._build([1, 0, 2] + [1] * 7 + [0])   # MIDI at internal position 11

    def test_inline_path_byte_identical(self) -> None:
        """The no-external-files `synthesize_mixed_midi_inline` (inlined donor + MIDI template) is
        byte-identical to the control-file donor path — so PT-confirmed by transitivity — and the
        top-level `synthesize` routes MIDI specs to it."""
        for spec in self.SPECS:
            with self.subTest(spec=spec):
                ctrl = self._build(spec)
                inline = BS.synthesize_mixed_midi_inline(spec)
                self.assertEqual(inline, ctrl, msg="inline == control-file path")
                self.assertEqual(BS.synthesize(spec), inline, msg="synthesize() routes MIDI to inline")

    def test_click_plus_midi(self) -> None:
        """A click coexists with MIDI — top, bottom, and (via display_order over the N+1 tracks)
        anywhere in between. Every index offset resolves (the click's lane refs AND the MIDI
        playlists — the bug was `_finish_click` rebuilding the index for audio tracks only), and
        the session reloads."""
        def ok(data, expect_display):
            self.assertEqual(self._display_names(data), expect_display)
            ref = FI.final_index_ref(data)
            zt, bt = FI.block_layout(data)
            zmarks = set(zt)
            recs = FI.parse_records(ref.data, set(bt), zmarks)
            offs = [c.offset for r in recs for c in r.child_refs] + \
                   [o for r in recs for e in r.elements for o in e.offsets]
            self.assertTrue(offs and all(o != 0 and o in zmarks for o in offs),
                            msg="all offsets resolve (no zeros/danglers)")
            with tempfile.NamedTemporaryFile(suffix=".ptx", delete=False) as t:
                t.write(W.encrypt_session_data(data)); p = t.name
            try:
                self.assertEqual(PTFFormat().load(p, 48000), 0, msg="reload")
            finally:
                Path(p).unlink(missing_ok=True)
        ok(BS.synthesize([1, 0, 2, 1, 0], click="bottom"),
           ["Audio 1", "MIDI 1", "Audio 2", "Audio 3", "MIDI 2", "Click 1"])
        ok(BS.synthesize([1, 0, 2, 1, 0], click="top"),
           ["Click 1", "Audio 1", "MIDI 1", "Audio 2", "Audio 3", "MIDI 2"])
        ok(BS.synthesize([1, 0, 2, 1, 0], click="bottom", display_order=[0, 1, 5, 2, 3, 4]),
           ["Audio 1", "MIDI 1", "Click 1", "Audio 2", "Audio 3", "MIDI 2"])

    def _display_names(self, data):
        """Edit-window display order = the 0x2624 container list (what PT reads), as track names."""
        name_at = {}
        for ct in (0x261c, 0x261e, 0x2620):
            for b in BS._by_type(BS.parse(data), ct):
                blk = data[b.offset - 7:b.offset + b.block_size]
                m = BS._NAME_RE.search(blk)
                name_at[b.offset - 7] = m.group().decode() if m else "?"
        return [name_at.get(o, "?") for o in BS._container_display_offsets(data)]

    def test_display_order(self) -> None:
        """`synthesize(spec, display_order=...)` sets the edit-window order independently of build
        order (spec-relative indices), placing MIDI anywhere — including the literal middle of an
        audio run. Index-only (body unchanged); identity is a no-op; reorders reload."""
        spec = [1, 0, 2, 1, 0]  # build: A1(mono), MIDI 1, A2(stereo), A3(mono), MIDI 2
        base = BS.synthesize(spec)
        self.assertEqual(self._display_names(base),
                         ["Audio 1", "MIDI 1", "Audio 2", "Audio 3", "MIDI 2"], msg="build order")
        self.assertEqual(self._display_names(BS.synthesize(spec, display_order=[0, 2, 1, 3, 4])),
                         ["Audio 1", "Audio 2", "MIDI 1", "Audio 3", "MIDI 2"], msg="MIDI to middle")
        self.assertEqual(self._display_names(BS.synthesize(spec, display_order=[4, 3, 2, 1, 0])),
                         ["MIDI 2", "Audio 3", "Audio 2", "MIDI 1", "Audio 1"], msg="reverse")
        # Identity display_order keeps the display order (it normalizes the count==4 instance
        # ordinals to display-position/ground-truth form, which is a byte change but same display).
        ident = BS.synthesize(spec, display_order=[0, 1, 2, 3, 4])
        self.assertEqual(self._display_names(ident), self._display_names(base), msg="identity display")
        body0 = base[:FI.final_index_ref(base).start]
        reordered = BS.synthesize(spec, display_order=[0, 2, 1, 3, 4])
        self.assertEqual(reordered[:FI.final_index_ref(reordered).start], body0, msg="index-only")
        with tempfile.NamedTemporaryFile(suffix=".ptx", delete=False) as t:
            t.write(W.encrypt_session_data(reordered)); p = t.name
        try:
            self.assertEqual(PTFFormat().load(p, 48000), 0, msg="reordered reload")
        finally:
            Path(p).unlink(missing_ok=True)

    def test_display_order_matches_control(self) -> None:
        """A synthesized MIDI-in-middle reorder structurally matches the real PT control of the same
        order (A2 M1 A1): same 0x2624 container type-sequence + same count==4 instance (ordinal,
        childtype) pairs — the instance ordinals skip the MIDI display slot exactly as PT writes."""
        def structure(data):
            type_at = {}
            for ct in (0x261c, 0x261e, 0x2620):
                for b in BS._by_type(BS.parse(data), ct):
                    type_at[b.offset - 7] = ct
            ref = FI.final_index_ref(data)
            zt, bt = FI.block_layout(data)
            cont, insts = [], []
            for r in FI.parse_records(ref.data, set(bt), set(zt)):
                if r.content_type == 0x2624 and r.count == 1 and r.flag == 1:
                    cont = [type_at.get(e.offsets[0]) for e in r.elements[1:] if e.offsets]
                elif r.content_type == 0x2624 and r.count == 4 and len(r.child_refs) >= 2:
                    insts.append((r.ordinal, r.child_refs[0].child_type))
            return cont, sorted(insts)
        mine = BS.synthesize([1, 0, 2], display_order=[2, 1, 0])  # display: stereo, MIDI, mono
        ctrl = _load_path(MTT_DIR / "multiple track types no click A2 M1 A1.ptx")
        self.assertEqual(structure(mine), structure(ctrl), msg="0x2624 structure matches PT control")

    def test_click_midi_waveform_view(self) -> None:
        """The click splice embeds a VOLUME-view block chain inside the click's 0x261e (the
        source control saved the click in volume view), nested too deep for the block parser to
        reach. set_waveform_view scrubs it (raw scan) so no audio track renders in volume view when
        it lands at the click's display row. Assert zero non-automation view-volume blocks remain,
        and that real 0x2580 volume-automation is preserved (not over-converted)."""
        for kw in (dict(click="bottom"), dict(click="top"),
                   dict(click="bottom", display_order=[0, 1, 5, 2, 3, 4])):
            with self.subTest(**kw):
                out = BS.synthesize([1, 0, 2, 1, 0], **kw)
                self.assertEqual(BS._view_volume_offsets(out, FI.final_index_ref(out).start), [],
                                 msg="no non-automation view-volume blocks remain")
                autos = sum(1 for b in BS.flat_blocks(BS.parse(out)) if b.content_type == 0x203b)
                self.assertGreater(autos, 0, msg="real 0x2580 volume-automation preserved")

    def test_clean_per_type_names(self) -> None:
        """Grown tracks get clean per-type counters (no internal-position gaps): the audio tracks
        are named Audio 1..A and the MIDI tracks MIDI 1..M, each a gap-free 1..k run."""
        for spec in self.SPECS:
            with self.subTest(spec=spec):
                names = [t.name for t in BS.track_types(self._build(spec))]
                audio = [int(n.split()[1]) for n in names if n.startswith("Audio ")]
                midi = [int(n.split()[1]) for n in names if n.startswith("MIDI ")]
                self.assertEqual(audio, list(range(1, len(audio) + 1)), msg="Audio 1..A no gaps")
                self.assertEqual(midi, list(range(1, len(midi) + 1)), msg="MIDI 1..M no gaps")


if __name__ == "__main__":
    unittest.main()
