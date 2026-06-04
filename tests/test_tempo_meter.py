"""Tests for session tempo / meter insertion (`body_synth.set_tempo` / `set_meter`).

Both reuse the same matched-pair transplant + robust reindex as the audio clip: a
tempo or meter change is exactly the set of top-level blocks that differ between a
default-tempo/4-4 session (`Untitled.ptx`) and the SAME session with the change
(`121bpm.ptx`, `90bpm.ptx`, `3-4 meter.ptx`). Those blocks are transplanted onto the
target (which keeps its own name/display) and the master index is repaired by
`final_index.reindex_after_resize`.

Tempo bpm is a float64 stored in BOTH 0x2718 and 0x2028 (and ONLY there — confirmed
by the 90↔121 diff), so `set_tempo(bpm=...)` can mint any tempo from one non-default
ref; the guard reproduces the real 121bpm control's blocks from the 90bpm ref. Meter
numerator/denominator are u32s after the "Meter" tag in 0x2719/0x2029.
"""
import struct
from pathlib import Path
import tempfile
import unittest

from ptxformatwriter.core import PTFFormat
from ptxformatwriter import body_synth as BS, writer as W, final_index as FI

ROOT = Path(__file__).resolve().parents[1]
VAR = ROOT / "control_files" / "various"
_UNTITLED = VAR / "Untitled.ptx"
_90 = VAR / "90bpm.ptx"
_121 = VAR / "121bpm.ptx"
_M34 = VAR / "3-4 meter.ptx"
_T140 = VAR / "120 to 140bpm.ptx"          # 2 tempo events (120 @0, 140 @bar2)
_M34B2 = VAR / "3-4 meter at bar 2.ptx"    # 2 meter events (4/4 @0, 3/4 @bar2)
_STER = ROOT / "control_files" / "lots of stereo tracks"
_BASELINE = _STER / "clip baseline.ptx"            # no markers
_MARKERS = _STER / "a few named markers.ptx"       # 4 named markers (bars 1/5/9/13)
_CLEAN3 = _STER / "3 stereo clean.ptx"
_CLIP3 = _STER / "3 stereo 3 different clips.ptx"
_TQ = 960000                                # ticks per quarter; bar 2 (4/4) = 4*_TQ


def _load_path(p: Path) -> bytes:
    ptf = PTFFormat()
    ptf.load(str(p), 48000)
    return ptf.unxored_data()


def _reload_ok(data: bytes) -> int:
    with tempfile.NamedTemporaryFile(suffix=".ptx", delete=False) as tmp:
        tmp.write(W.encrypt_session_data(data))
        path = tmp.name
    try:
        return PTFFormat().load(path, 48000)
    finally:
        Path(path).unlink(missing_ok=True)


def _top(data: bytes, ct: int) -> bytes:
    b = [x for x in BS.parse(data).blocks if x.content_type == ct][0]
    return data[b.offset - 7 : b.offset + b.block_size]


@unittest.skipUnless(_UNTITLED.exists() and _121.exists() and _90.exists(),
                     "tempo controls (Untitled / 90bpm / 121bpm) not present")
class TempoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.unt = _load_path(_UNTITLED)
        self.u90 = _load_path(_90)
        self.u121 = _load_path(_121)

    def test_transplant_byte_exact_and_reloads(self) -> None:
        """set_tempo(Untitled, Untitled, 121bpm) reproduces the 121bpm control's tempo
        blocks (0x1040 conductor scaffold + 0x2718/0x2028 value) byte-for-byte, reads
        back 121 bpm, and re-parses."""
        out = BS.set_tempo(self.unt, self.unt, self.u121)
        for ct in (0x1040, 0x2718, 0x2028):
            self.assertEqual(_top(out, ct), _top(self.u121, ct),
                             msg=f"0x{ct:04x} not byte-exact vs 121bpm control")
        self.assertEqual(_reload_ok(out), 0, msg="tempo transplant failed to reload")
        self.assertEqual(BS._find_bpm_double(_top(out, 0x2718))[1], 121.0)

    def test_bpm_patch_reproduces_control(self) -> None:
        """Minting 121 bpm from the 90bpm ref reproduces the real 121bpm control's
        0x2718/0x2028 blocks byte-for-byte — the bpm float64 is the only difference,
        so the parameterized patch is exact."""
        out = BS.set_tempo(self.unt, self.unt, self.u90, bpm=121.0)
        self.assertEqual(_top(out, 0x2718), _top(self.u121, 0x2718))
        self.assertEqual(_top(out, 0x2028), _top(self.u121, 0x2028))

    def test_arbitrary_bpm(self) -> None:
        """An arbitrary tempo (140) is set in both 0x2718 and 0x2028 and reloads."""
        out = BS.set_tempo(self.unt, self.unt, self.u121, bpm=140.0)
        self.assertEqual(BS._find_bpm_double(_top(out, 0x2718))[1], 140.0)
        self.assertEqual(BS._find_bpm_double(_top(out, 0x2028))[1], 140.0)
        self.assertEqual(_reload_ok(out), 0, msg="140 bpm failed to reload")


@unittest.skipUnless(_UNTITLED.exists() and _M34.exists(),
                     "meter controls (Untitled / 3-4 meter) not present")
class MeterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.unt = _load_path(_UNTITLED)
        self.m34 = _load_path(_M34)

    def _read_meter(self, data: bytes) -> tuple:
        body = _top(data, 0x2029)
        m = body.find(b"Meter")
        no = m + 5 + 2 + 4 + 4 + 5 + 3 + 4
        return (int.from_bytes(body[no:no + 4], "little"),
                int.from_bytes(body[no + 4:no + 8], "little"))

    def test_transplant_byte_exact_and_reloads(self) -> None:
        """set_meter(Untitled, Untitled, 3-4 meter) reproduces the control's meter
        blocks (0x1040 + the 0x2719/0x2029 meter map) byte-for-byte, reads back 3/4,
        and re-parses."""
        out = BS.set_meter(self.unt, self.unt, self.m34)
        for ct in (0x1040, 0x2719, 0x2029):
            self.assertEqual(_top(out, ct), _top(self.m34, ct),
                             msg=f"0x{ct:04x} not byte-exact vs 3-4 meter control")
        self.assertEqual(_reload_ok(out), 0, msg="meter transplant failed to reload")
        self.assertEqual(self._read_meter(out), (3, 4))

    def test_meter_numerator_denominator_patch(self) -> None:
        """Patching numerator/denominator updates the 0x2029 meter map and reloads."""
        out = BS.set_meter(self.unt, self.unt, self.m34, numerator=5, denominator=8)
        self.assertEqual(self._read_meter(out), (5, 8))
        self.assertEqual(_reload_ok(out), 0, msg="patched meter failed to reload")


def _events_via_reader(data: bytes, kind: str):
    with tempfile.NamedTemporaryFile(suffix=".ptx", delete=False) as tmp:
        tmp.write(W.encrypt_session_data(data))
        path = tmp.name
    try:
        p = PTFFormat()
        p.load(path, 48000)
        if kind == "tempo":
            return [(round(e.bpm, 2), e.pos) for e in p.tempoevents()]
        return [(e.numerator, e.denominator, e.pos) for e in p.meterevents()]
    finally:
        Path(path).unlink(missing_ok=True)


@unittest.skipUnless(_UNTITLED.exists() and _T140.exists(),
                     "2-tempo control (120 to 140bpm) not present")
class TempoMapTests(unittest.TestCase):
    """Mid-session tempo changes via set_tempo_map (a list of (bpm, tick) events)."""

    def test_reproduces_two_tempo_control(self) -> None:
        """set_tempo_map reproduces the 120->140 @bar2 control's tempo blocks
        (0x2028 map + 0x2718 ruler + 0x1040 scaffold) byte-for-byte, and the reader
        reads back both tempo events."""
        unt, t140 = _load_path(_UNTITLED), _load_path(_T140)
        out = BS.set_tempo_map(unt, [(120, 0), (140, 4 * _TQ)], t140, unt)   # override = byte-exact
        for ct in (0x2028, 0x2718, 0x1040):
            self.assertEqual(_top(out, ct), _top(t140, ct),
                             msg=f"0x{ct:04x} not byte-exact vs 120->140 control")
        self.assertEqual(_reload_ok(out), 0)
        self.assertEqual(_events_via_reader(out, "tempo"), [(120.0, 0), (140.0, 3840000)])

    def test_inlined_default_path_roundtrips(self) -> None:
        """The DEFAULT (no-ref) path grows ONLY 0x2028/0x2718 from the inlined templates
        and reads back both events — and reproduces the override path's tempo blocks
        byte-for-byte (the inlined template IS the control's record, resized)."""
        unt, t140 = _load_path(_UNTITLED), _load_path(_T140)
        ev = [(120, 0), (140, 4 * _TQ)]
        inlined = BS.set_tempo_map(unt, ev)                # default: no donor
        override = BS.set_tempo_map(unt, ev, t140, unt)    # from the control pair
        for ct in (0x2028, 0x2718):
            self.assertEqual(_top(inlined, ct), _top(override, ct),
                             msg=f"0x{ct:04x} inlined != override")
        self.assertEqual(_reload_ok(inlined), 0)
        self.assertEqual(_events_via_reader(inlined, "tempo"), [(120.0, 0), (140.0, 3840000)])

    def test_arbitrary_tempo_change(self) -> None:
        """An arbitrary mid-session tempo change (100 -> 150 at bar 1.5) round-trips."""
        unt, t140 = _load_path(_UNTITLED), _load_path(_T140)
        out = BS.set_tempo_map(unt, [(100, 0), (150, 2 * _TQ)], t140, unt)
        self.assertEqual(_reload_ok(out), 0)
        self.assertEqual(_events_via_reader(out, "tempo"), [(100.0, 0), (150.0, 1920000)])

    def test_builds_fewer_records_than_ref(self) -> None:
        """The builder synthesizes ANY count from the ref's scaffold: one event from
        a 2-record ref yields a single-tempo session that round-trips."""
        unt, t140 = _load_path(_UNTITLED), _load_path(_T140)
        out = BS.set_tempo_map(unt, [(123, 0)], t140, unt)
        self.assertEqual(_reload_ok(out), 0)
        self.assertEqual(_events_via_reader(out, "tempo"), [(123.0, 0)])
        # the 1-record build matches the real 1-tempo control's tempo-block sizes
        self.assertEqual(len(_top(out, 0x2028)), len(_top(_load_path(_121), 0x2028)))
        self.assertEqual(len(_top(out, 0x2718)), len(_top(_load_path(_121), 0x2718)))

    def test_builds_many_records(self) -> None:
        """A 5-event tempo map (more than any control) is built and reads back exactly,
        and the blocks grow by the decoded +61/record (0x1040 stays the explicit +42)."""
        unt, t140 = _load_path(_UNTITLED), _load_path(_T140)
        events = [(120, 0), (90, 4 * _TQ), (140, 8 * _TQ), (75, 12 * _TQ), (200, 16 * _TQ)]
        out = BS.set_tempo_map(unt, events, t140, unt)
        self.assertEqual(_reload_ok(out), 0)
        got = _events_via_reader(out, "tempo")
        self.assertEqual(got, [(120.0, 0), (90.0, 3840000), (140.0, 7680000),
                               (75.0, 11520000), (200.0, 15360000)])
        # each block grows by exactly +61 per record beyond the 2-record control
        self.assertEqual(len(_top(out, 0x2028)), len(_top(t140, 0x2028)) + 61 * 3)
        self.assertEqual(len(_top(out, 0x2718)), len(_top(t140, 0x2718)) + 61 * 3)


@unittest.skipUnless(_UNTITLED.exists() and _M34B2.exists(),
                     "2-meter control (3-4 meter at bar 2) not present")
class MeterMapTests(unittest.TestCase):
    """Mid-session meter changes via set_meter_map (a list of (num, den, tick) events)."""

    def test_reproduces_two_meter_control(self) -> None:
        """set_meter_map reproduces the 4/4 -> 3/4 @bar2 control's meter blocks
        (0x2029 + 0x2719 + 0x1040) byte-for-byte; the reader reads back both events."""
        unt, m = _load_path(_UNTITLED), _load_path(_M34B2)
        out = BS.set_meter_map(unt, [(4, 4, 0), (3, 4, 4 * _TQ)], m, unt)   # override = byte-exact
        for ct in (0x2029, 0x2719, 0x1040):
            self.assertEqual(_top(out, ct), _top(m, ct),
                             msg=f"0x{ct:04x} not byte-exact vs 4/4->3/4 control")
        self.assertEqual(_reload_ok(out), 0)
        self.assertEqual(_events_via_reader(out, "meter"), [(4, 4, 0), (3, 4, 3840000)])

    def test_inlined_default_path_roundtrips(self) -> None:
        """The DEFAULT (no-ref) path grows ONLY 0x2029/0x2719 from the inlined templates,
        reads back both events, and reproduces the override path's meter blocks byte-for-byte."""
        unt, m = _load_path(_UNTITLED), _load_path(_M34B2)
        ev = [(4, 4, 0), (3, 4, 4 * _TQ)]
        inlined = BS.set_meter_map(unt, ev)                # default: no donor
        override = BS.set_meter_map(unt, ev, m, unt)       # from the control pair
        for ct in (0x2029, 0x2719):
            self.assertEqual(_top(inlined, ct), _top(override, ct),
                             msg=f"0x{ct:04x} inlined != override")
        self.assertEqual(_reload_ok(inlined), 0)
        self.assertEqual(_events_via_reader(inlined, "meter"), [(4, 4, 0), (3, 4, 3840000)])

    def test_arbitrary_meter_change(self) -> None:
        """An arbitrary mid-session meter change (7/8 -> 5/4 at bar 3) round-trips."""
        unt, m = _load_path(_UNTITLED), _load_path(_M34B2)
        out = BS.set_meter_map(unt, [(7, 8, 0), (5, 4, 8 * _TQ)], m, unt)
        self.assertEqual(_reload_ok(out), 0)
        self.assertEqual(_events_via_reader(out, "meter"), [(7, 8, 0), (5, 4, 7680000)])

    def test_builds_many_meters(self) -> None:
        """A 4-event meter map (more than any control) is built and reads back exactly;
        each block grows by the decoded +52/record beyond the 2-record control."""
        unt, m = _load_path(_UNTITLED), _load_path(_M34B2)
        events = [(4, 4, 0), (3, 4, 4 * _TQ), (7, 8, 7 * _TQ), (5, 4, 12 * _TQ)]
        out = BS.set_meter_map(unt, events, m, unt)
        self.assertEqual(_reload_ok(out), 0)
        self.assertEqual(_events_via_reader(out, "meter"),
                         [(4, 4, 0), (3, 4, 3840000), (7, 8, 6720000), (5, 4, 11520000)])
        self.assertEqual(len(_top(out, 0x2029)), len(_top(m, 0x2029)) + 52 * 2)
        self.assertEqual(len(_top(out, 0x2719)), len(_top(m, 0x2719)) + 52 * 2)

    def test_builds_single_meter_from_two_ref(self) -> None:
        """One meter event built from the 2-event ref yields a single-meter session."""
        unt, m = _load_path(_UNTITLED), _load_path(_M34B2)
        out = BS.set_meter_map(unt, [(6, 8, 0)], m, unt)
        self.assertEqual(_reload_ok(out), 0)
        self.assertEqual(_events_via_reader(out, "meter"), [(6, 8, 0)])


@unittest.skipUnless(_BASELINE.exists() and _MARKERS.exists(),
                     "marker controls (clip baseline / a few named markers) not present")
class MarkersTests(unittest.TestCase):
    """Session markers via set_markers: the 0x2030 marker list = count + one 0x2077 record
    per marker (ordinal, length-prefixed name, position = ZERO_TICKS+ticks, a unique GUID).
    Control pair: `clip baseline.ptx` (none) -> `a few named markers.ptx` (4, bars 1/5/9/13)."""

    _CTRL_MARKERS = [("MARKER_ONE", 0), ("MARKER_TWO", 16 * _TQ),
                     ("MARKER_THREE", 32 * _TQ), ("MARKER_FOUR", 48 * _TQ)]

    def test_reproduces_marker_control_mod_guid(self) -> None:
        """set_markers reproduces the 4-marker control's 0x2030 byte-for-byte except the
        per-marker GUID nonce (4 * 16 bytes); the result reloads."""
        baseline, mref = _load_path(_BASELINE), _load_path(_MARKERS)
        out = BS.set_markers(baseline, self._CTRL_MARKERS, mref)   # mref = override (byte-exact vs control)
        a, b = _top(out, 0x2030), _top(mref, 0x2030)
        self.assertEqual(len(a), len(b), msg="marker list size != control")
        ndiff = sum(1 for i in range(len(a)) if a[i] != b[i])
        self.assertLessEqual(ndiff, 4 * 16, msg="markers differ beyond the GUID nonce")
        self.assertEqual(_reload_ok(out), 0)

    def test_arbitrary_markers(self) -> None:
        """Any count / names / positions: 3 markers with custom names build + reload, and
        each name is present in the marker list."""
        baseline, mref = _load_path(_BASELINE), _load_path(_MARKERS)
        out = BS.set_markers(baseline, [("Intro", 0), ("Verse", 16 * _TQ), ("Chorus", 32 * _TQ)])
        self.assertEqual(_reload_ok(out), 0)
        blk = _top(out, 0x2030)
        for name in (b"Intro", b"Verse", b"Chorus"):
            self.assertIn(name, blk)


@unittest.skipUnless(_UNTITLED.exists() and _T140.exists() and _M34B2.exists()
                     and _BASELINE.exists() and _MARKERS.exists() and _CLEAN3.exists()
                     and _CLIP3.exists(), "composition controls not all present")
class CompositionTests(unittest.TestCase):
    """The conductor features (tempo/meter/marker maps) apply to a MULTI-TRACK session and
    compose with each other + clips. They reindex against `data` (not the 0/1-track ref),
    so a 3-stereo keeps its 3-track holes (251) and a clip adds exactly one (252)."""

    def _holes(self, data):
        _r, h = FI.offset_holes(data)
        z2t, _bt = FI.block_layout(data)
        bad = sum(1 for _p, v, t, _r, _k in h if z2t.get(v) != t)
        return len(h), bad

    def test_tempo_meter_markers_clips_compose_on_3stereo(self) -> None:
        clean3 = _load_path(_CLEAN3)
        n0, _ = self._holes(clean3)  # 251 for a clean 3-stereo
        # the orchestrator path: inlined tempo/meter/markers (no donor refs) on an N-stereo
        out = BS.set_tempo_map(clean3, [(120, 0), (140, 4 * _TQ)])
        out = BS.set_meter_map(out, [(4, 4, 0), (3, 4, 4 * _TQ)])
        out = BS.set_markers(out, [("Intro", 0), ("Verse", 16 * _TQ)])
        n1, bad1 = self._holes(out)
        self.assertEqual((n1, bad1), (n0, 0), msg="conductor maps changed/broke the index")
        self.assertEqual(_reload_ok(out), 0)
        self.assertEqual([t.kind for t in BS.track_types(out)], ["stereo"] * 3)
        # + clips: one new 0x0f3c hole, everything still resolves
        af = _STER / "Audio Files"
        wav = af / "01.wav"
        if wav.exists():
            out = BS.build_audio_clips(out, [[(str(wav), 88200)], [], []], _load_path(_CLIP3))
            n2, bad2 = self._holes(out)
            self.assertEqual((n2, bad2), (n0 + 1, 0), msg="clip on conductor session broke index")
            self.assertEqual(_reload_ok(out), 0)


if __name__ == "__main__":
    unittest.main()
