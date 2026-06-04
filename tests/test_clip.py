"""Tests for audio-clip insertion (`body_synth.add_audio_clip`).

A clip is added by transplanting every top-level block that differs between a matched
control PAIR — `clip baseline.ptx` (an empty 1-stereo session) and a session with the
clip(s) added — onto the clean session, then robustly repairing the master index with
`final_index.reindex_after_resize`. `add_audio_clip(baseline, baseline, bar1)` is
PT-CONFIRMED. Because it copies whatever the pair differs by, the same function
reproduces ONE clip (`one clip bar 1.ptx`), MULTIPLE clips on a track (`two clips same
track.ptx` -> 4 placements, reused regions), or a clip of a DIFFERENT wav (`clip diff
wav.ptx`) — each byte-exact except session identity (0x2067), the first-block index
pointer, and the 0x0002 index.
"""
from pathlib import Path
import tempfile
import unittest

from ptxformatwriter.core import PTFFormat
from ptxformatwriter import body_synth as BS, final_index as FI, writer as W

ROOT = Path(__file__).resolve().parents[1]
STER = ROOT / "control_files" / "lots of stereo tracks"
_BASELINE = STER / "clip baseline.ptx"
_BAR1 = STER / "one clip bar 1.ptx"
_BAR2 = STER / "one clip bar 2.ptx"
_BAR3 = STER / "one clip bar 3.ptx"
_CLEAN3 = STER / "3 stereo clean.ptx"
_CLIP_T2 = STER / "clip on track 2 of 3.ptx"
_CLIP3 = STER / "3 stereo 3 different clips.ptx"
_TWO_CLIPS = STER / "two clips same track.ptx"
_DIFF_WAV = STER / "clip diff wav.ptx"
_ARB_WAV = ROOT / "control_files" / "lots of wave files" / "Audio Files" / "01.wav"

# The clip's structural footprint (name table, path index, file table, region lib,
# placements). The 0x2624 playlist (timeline 3-points) is also transplanted now.
CLIP_TYPES = (0x2519, 0x0F3D, 0x1004, 0x262A, 0x1054)


def _reproduces_control(testcase, clean_bytes, ref_bytes) -> bytes:
    """add_audio_clip(clean, clean, ref) reproduces `ref` byte-for-byte except the
    first-block index pointer [0], session identity 0x2067, and the 0x0002 index."""
    out = BS.add_audio_clip(clean_bytes, clean_bytes, ref_bytes)
    to, tr = BS.parse(out).blocks, BS.parse(ref_bytes).blocks
    testcase.assertEqual(len(to), len(tr))
    unexpected = []
    for i in range(len(to)):
        a, b = to[i], tr[i]
        if out[a.offset - 7:a.offset + a.block_size] != ref_bytes[b.offset - 7:b.offset + b.block_size]:
            if i != 0 and b.content_type not in (0x2067, 0x0002):
                unexpected.append((i, hex(b.content_type)))
    testcase.assertEqual(unexpected, [], msg=f"unexpected content diffs vs control: {unexpected}")
    return out


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


def _top_by_type(data: bytes, ct: int) -> bytes:
    b = [x for x in BS.parse(data).blocks if x.content_type == ct][0]
    return data[b.offset - 7 : b.offset + b.block_size]


def _hole_sequence(data: bytes):
    """(target_type, kind) for every master-index offset hole, plus a count of holes
    whose stored offset does NOT land on a live block of the expected type."""
    _ref, holes = FI.offset_holes(data)
    z2t, _bt = FI.block_layout(data)
    bad = sum(1 for _p, v, t, _r, _k in holes if z2t.get(v) != t)
    return [(t, k) for _p, _v, t, _r, k in holes], bad


def _decode_region(r: bytes):
    """Decode (name, findex, findex2, length, lengthbytes) from a standalone 0x2629 region
    block the way Pro Tools reads it: name @22, the region->file index stored twice
    (`findex` after the 0x2628 name sub-block = the display copy; `findex2` in the trailer
    at len-8 = the playback copy), and the length as a variable-width three-point value
    right after the name (width = high nibble at 24+namelen)."""
    import struct
    nl = struct.unpack_from("<I", r, 18)[0]
    name = r[22:22 + nl].decode("latin1", "replace")
    findex = struct.unpack_from("<I", r, 16 + struct.unpack_from("<I", r, 12)[0])[0]
    findex2 = struct.unpack_from("<I", r, len(r) - 8)[0]
    jr = 22 + nl
    obytes = (r[jr + 1] & 0xF0) >> 4
    lbytes = (r[jr + 2] & 0xF0) >> 4
    length = int.from_bytes(r[jr + 5 + obytes:jr + 5 + obytes + lbytes], "little")
    return name, findex, findex2, length, lbytes


def _regions_by_name(data: bytes):
    """Map region name -> its full 0x2629 block bytes."""
    import struct
    out = {}
    for blk in [b for b in BS.parse(data).blocks if b.content_type == 0x262A]:
        for ch in blk.child:
            if ch.content_type != 0x2629:
                continue
            r = data[ch.offset - 7:ch.offset + ch.block_size]
            nl = struct.unpack_from("<I", r, 18)[0]
            out[r[22:22 + nl].decode("latin1")] = r
    return out


@unittest.skipUnless(_BASELINE.exists() and _BAR1.exists(),
                     "clip baseline / one-clip controls not present")
class AudioClipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.baseline = _load_path(_BASELINE)
        self.bar1 = _load_path(_BAR1)
        self.out = BS.add_audio_clip(self.baseline, self.baseline, self.bar1)

    def test_deterministic_and_reloads(self) -> None:
        """add_audio_clip is deterministic and the result re-parses (rc==0)."""
        again = BS.add_audio_clip(self.baseline, self.baseline, self.bar1)
        self.assertEqual(self.out, again, msg="add_audio_clip is not deterministic")
        self.assertEqual(_reload_ok(self.out), 0, msg="clip result failed to reload")

    def test_clip_blocks_byte_exact(self) -> None:
        """The five clip-bearing top-level blocks are transplanted byte-for-byte from
        the control — the structural contribution of the clip is reproduced exactly."""
        for ct in CLIP_TYPES:
            self.assertEqual(
                _top_by_type(self.out, ct), _top_by_type(self.bar1, ct),
                msg=f"clip block 0x{ct:04x} not byte-exact vs the control",
            )

    def test_reproduces_control_byte_exact(self) -> None:
        """The whole result equals `one clip bar 1.ptx` except the first-block index
        pointer, session identity (0x2067), and the 0x0002 index."""
        _reproduces_control(self, self.baseline, self.bar1)

    def test_index_gains_clip_entries_all_valid(self) -> None:
        """The repaired master index has the SAME (target_type, kind) hole sequence as
        the real control, and every stored offset resolves to a live block (no stale
        / mis-ranked pointers — the failure mode of the legacy offset guesser)."""
        seq_out, bad_out = _hole_sequence(self.out)
        seq_ctrl, bad_ctrl = _hole_sequence(self.bar1)
        self.assertEqual(bad_out, 0, msg="index has offsets not landing on a live block")
        self.assertEqual(bad_ctrl, 0, msg="control index unexpectedly has bad offsets")
        self.assertEqual(seq_out, seq_ctrl, msg="index hole sequence differs from control")

    def test_audio_track_list_unchanged(self) -> None:
        """A clip is not a track: the audio track list is identical to the clean
        session (one stereo Audio 1), and channel count is unchanged."""
        self.assertEqual(
            [(t.name, t.kind) for t in BS.track_types(self.out)],
            [(t.name, t.kind) for t in BS.track_types(self.baseline)],
            msg="audio track list changed when adding a clip",
        )
        self.assertEqual(BS.channel_count(self.out), BS.channel_count(self.baseline))

    def test_clip_region_blocks_present(self) -> None:
        """The result gains the stereo clip's two regions (.L/.R) and one wav
        descriptor, matching the control's counts (was 0 in the clean session)."""
        def count(data: bytes, ct: int) -> int:
            return sum(1 for b in BS.flat_blocks(BS.parse(data)) if b.content_type == ct)
        for ct, n in ((0x2628, 2), (0x2629, 2), (0x1003, 1)):
            self.assertEqual(count(self.baseline, ct), 0)
            self.assertEqual(count(self.out, ct), n, msg=f"expected {n}x 0x{ct:04x}")
            self.assertEqual(count(self.out, ct), count(self.bar1, ct))


def _count(data: bytes, ct: int) -> int:
    return sum(1 for b in BS.flat_blocks(BS.parse(data)) if b.content_type == ct)


@unittest.skipUnless(_BASELINE.exists() and _TWO_CLIPS.exists(),
                     "two-clips control not present")
class MultipleClipsTests(unittest.TestCase):
    def test_two_clips_reproduces_control(self) -> None:
        """add_audio_clip with a 2-clip ref reproduces the control byte-exact (mod
        identity), reloads, and the result has 4 placements (2 clips x 2 lanes) while
        reusing the single wav + 2 regions (clips of the same audio share regions)."""
        baseline, two = _load_path(_BASELINE), _load_path(_TWO_CLIPS)
        out = _reproduces_control(self, baseline, two)
        self.assertEqual(_reload_ok(out), 0, msg="two-clip result failed to reload")
        self.assertEqual(_count(out, 0x104F), 4, msg="expected 4 placements for 2 clips")
        self.assertEqual(_count(out, 0x2628), 2, msg="2 clips of one wav reuse 2 regions")
        self.assertEqual(_count(out, 0x1003), 1, msg="2 clips of one wav reuse 1 file")


@unittest.skipUnless(_BASELINE.exists() and _DIFF_WAV.exists(),
                     "different-wav control not present")
class DifferentWavTests(unittest.TestCase):
    def test_diff_wav_reproduces_control(self) -> None:
        """add_audio_clip with a different-wav ref reproduces the control byte-exact
        (mod identity) and reloads — arbitrary WAV is pure block transplant."""
        baseline, diff = _load_path(_BASELINE), _load_path(_DIFF_WAV)
        out = _reproduces_control(self, baseline, diff)
        self.assertEqual(_reload_ok(out), 0, msg="diff-wav result failed to reload")
        self.assertEqual(_count(out, 0x1003), 1)
        self.assertEqual(_count(out, 0x2628), 2)


@unittest.skipUnless(_BAR1.exists() and _ARB_WAV.exists(),
                     "one-clip control / arbitrary wav not present")
class ArbitraryWavTests(unittest.TestCase):
    """set_clip_wav re-points a clip at an arbitrary PT/BWF WAV so Pro Tools auto-links
    it (PT-CONFIRMED with 01.wav). Verifies the WAV's umid material + 2-byte id +
    frame-count length are written, and the 0x2106 umid copy is 00-prefixed (the byte
    that, wrongly left as the 0x1001 marker 2a, caused the relink)."""

    def test_set_clip_wav_relinks_clean(self) -> None:
        bar1 = _load_path(_BAR1)
        sc, umid_material, id2 = BS.wav_clip_identity(str(_ARB_WAV))
        out = BS.set_clip_wav(bar1, str(_ARB_WAV), filename="01.wav")
        self.assertEqual(_reload_ok(out), 0, msg="set_clip_wav result failed to reload")
        b1003 = [b for b in BS.flat_blocks(BS.parse(out)) if b.content_type == 0x1003][0]
        c = out[b1003.offset - 7 : b1003.offset + b1003.block_size]
        self.assertEqual(c[44:52], umid_material, msg="0x1001 umid material wrong")
        self.assertEqual(c[292:300], b"\x00" + umid_material[1:],
                         msg="0x2106 umid copy must be 00-prefixed (not the 2a marker)")
        self.assertEqual(c[301:303], id2, msg="2-byte id wrong")
        # lengths reflect the WAV's frame count; region named by filename stem
        names = sorted(out[b.offset - 7:b.offset + b.block_size][13:13 +
                       int.from_bytes(out[b.offset - 7:b.offset + b.block_size][9:13], "little")]
                       .decode("latin1") for b in BS.flat_blocks(BS.parse(out)) if b.content_type == 0x2628)
        self.assertEqual(names, ["01.L", "01.R"], msg="region not named by filename stem")
        k1001 = [b for b in BS.flat_blocks(BS.parse(out)) if b.content_type == 0x1001][0]
        kb = out[k1001.offset - 7 : k1001.offset + k1001.block_size]
        self.assertEqual(int.from_bytes(kb[15:19], "little"), sc, msg="0x1001 length != frame count")

    def test_wrap_raw_wav_then_link(self) -> None:
        """wrap_raw_wav turns a RAW WAV (fmt+data only) into a PT container with a
        generated UMID, written consistently so set_clip_wav can link it. PT-confirmed
        (a stripped WAV wrapped + linked with no relink)."""
        import struct
        import tempfile
        full = _ARB_WAV.read_bytes()
        # strip to a raw WAV (fmt + data only) -> simulates a non-PT WAV
        body = bytearray()
        for cid, o, sz in BS._wav_chunks(full):
            if cid in (b"fmt ", b"data"):
                body += cid + struct.pack("<I", sz) + full[o:o + sz]
                if sz & 1:
                    body += b"\x00"
        raw = b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + bytes(body)
        self.assertEqual([c for c, _o, _s in BS._wav_chunks(raw)], [b"fmt ", b"data"])

        wrapped = BS.wrap_raw_wav(raw, full, seed="unit-test")
        self.assertEqual(wrapped, BS.wrap_raw_wav(raw, full, seed="unit-test"), msg="not deterministic")
        chunk_ids = [c for c, _o, _s in BS._wav_chunks(wrapped)]
        for need in (b"bext", b"umid", b"regn", b"fmt ", b"data"):
            self.assertIn(need, chunk_ids, msg=f"wrapped WAV missing {need!r}")
        with tempfile.TemporaryDirectory() as td:
            wp = Path(td) / "WRAPPED.wav"
            wp.write_bytes(wrapped)
            sc, material, id2 = BS.wav_clip_identity(str(wp))  # generated identity reads back
            out = BS.set_clip_wav(_load_path(_BAR1), str(wp), filename="WRAPPED.wav")
        b1003 = [b for b in BS.flat_blocks(BS.parse(out)) if b.content_type == 0x1003][0]
        c = out[b1003.offset - 7 : b1003.offset + b1003.block_size]
        self.assertEqual(c[44:52], material, msg="wrapped clip umid material not linked")
        self.assertEqual(_reload_ok(out), 0, msg="wrapped-WAV clip failed to reload")


@unittest.skipUnless(_BAR2.exists() and _BAR3.exists(),
                     "one clip bar 2 / bar 3 controls not present")
class ClipPositionTests(unittest.TestCase):
    """Arbitrary clip position lives SOLELY in the 0x104f placement field (FILE samples),
    mirrored across lanes. Ground truth: bar1=0, bar2=88200 (2.0s@44100), bar3=176400."""

    # blocks that legitimately differ between two positions of the same clip:
    # display caches Pro Tools recomputes, the region-GUID save-nonce, identity, index.
    _ALLOWED = {0x2016, 0x2056, 0x2587, 0x2624, 0x262A, 0x2067, 0x0002}

    def test_reads_known_positions(self) -> None:
        self.assertEqual(BS.clip_positions(_load_path(_BAR1)), [0, 0])
        self.assertEqual(BS.clip_positions(_load_path(_BAR2)), [88200, 88200])
        self.assertEqual(BS.clip_positions(_load_path(_BAR3)), [176400, 176400])

    def test_move_is_size_neutral_and_roundtrips(self) -> None:
        bar2 = _load_path(_BAR2)
        moved = BS.set_clip_position(bar2, 123456)
        self.assertEqual(len(moved), len(bar2), msg="position move must be size-neutral")
        self.assertEqual(BS.clip_positions(moved), [123456, 123456])
        self.assertEqual(_reload_ok(moved), 0, msg="position-moved clip failed to reload")

    def test_patching_bar2_reproduces_bar3(self) -> None:
        """Patching bar2's 0x104f to bar3's value reproduces real bar3 with NO diffs
        outside display-cache / region-GUID-nonce / identity / index."""
        bar2, bar3 = _load_path(_BAR2), _load_path(_BAR3)
        moved = BS.set_clip_position(bar2, 176400)
        mo, br = BS.parse(moved).blocks, BS.parse(bar3).blocks
        self.assertEqual(len(mo), len(br))
        unexpected = []
        for i in range(len(mo)):
            a, b = mo[i], br[i]
            if moved[a.offset - 7:a.offset + a.block_size] != bar3[b.offset - 7:b.offset + b.block_size]:
                if i != 0 and b.content_type not in self._ALLOWED:
                    unexpected.append((i, hex(b.content_type)))
        self.assertEqual(unexpected, [], msg=f"position move touched unexpected blocks: {unexpected}")
        # and the 0x104f placement bytes are now byte-identical to real bar3
        a104f = [x for x in BS.flat_blocks(BS.parse(moved)) if x.content_type == 0x104F]
        b104f = [x for x in BS.flat_blocks(BS.parse(bar3)) if x.content_type == 0x104F]
        for a, b in zip(a104f, b104f):
            self.assertEqual(moved[a.offset - 7:a.offset + a.block_size],
                             bar3[b.offset - 7:b.offset + b.block_size],
                             msg="0x104f placement not byte-exact vs real bar3")


@unittest.skipUnless(_CLEAN3.exists() and _CLIP_T2.exists(),
                     "3-stereo clean / clip-on-track-2 controls not present")
class ClipOnTrackTests(unittest.TestCase):
    """add_clip_to_track places one clip on an ARBITRARY track of an N-stereo session,
    synthesizing the master index (a clip adds exactly one 0x0f3c marker on the 0x0f3d
    record). PT-confirmed on tracks 1/2/3 of a 3-stereo (placement + index is the whole
    load-bearing footprint; 0x2519/0x2624 are display PT rebuilds)."""

    def test_synth_index_reproduces_control(self) -> None:
        """The synthesized clip index equals the real control's 0x0002 byte-for-byte."""
        clean, ctrl = _load_path(_CLEAN3), _load_path(_CLIP_T2)
        ref = FI.final_index_ref(ctrl)
        synth = BS._synth_clip_index(clean, ctrl[: ref.start])
        self.assertEqual(synth, ctrl[ref.start:],
                         msg="synthesized clip index not byte-exact vs control")

    def test_track2_load_bearing_blocks_byte_exact(self) -> None:
        """Building the clip on track 2 (clip_ref = the control) reproduces the control's
        global wav/region/path blocks and the placement byte-for-byte. (The index has
        the same records but different offsets, since we omit the display-only 0x2519/
        0x2624 growth, so the body is smaller — index correctness is covered separately.)"""
        clean, ctrl = _load_path(_CLEAN3), _load_path(_CLIP_T2)
        out = BS.add_clip_to_track(clean, ctrl, 1)
        for ct in (0x1004, 0x262A, 0x0F3D, 0x1054):
            self.assertEqual(_top_by_type(out, ct), _top_by_type(ctrl, ct),
                             msg=f"0x{ct:04x} not byte-exact vs control")
        self.assertEqual(_reload_ok(out), 0)

    def test_clip_on_each_track(self) -> None:
        """A clip lands on exactly track K's two lanes for K in 0/1/2, every result
        reloads, and the index hole sequence matches the real control (K-independent)."""
        clean, ctrl = _load_path(_CLEAN3), _load_path(_CLIP_T2)
        ctrl_seq, ctrl_bad = _hole_sequence(ctrl)
        self.assertEqual(ctrl_bad, 0)
        for k in (0, 1, 2):
            out = BS.add_clip_to_track(clean, ctrl, k)
            self.assertEqual(_reload_ok(out), 0, msg=f"track {k} clip failed to reload")
            lanes = [b for b in BS.flat_blocks(BS.parse(out)) if b.content_type == 0x1052]
            clip_lanes = [i for i, L in enumerate(lanes)
                          if any(c.content_type == 0x1050 for c in L.child)]
            self.assertEqual(clip_lanes, [2 * k, 2 * k + 1],
                             msg=f"clip not on track {k}'s two lanes")
            seq, bad = _hole_sequence(out)
            self.assertEqual(bad, 0, msg=f"track {k} index has offsets off a live block")
            self.assertEqual(seq, ctrl_seq, msg=f"track {k} hole sequence != control")

    def test_position_override_composes(self) -> None:
        """position_file_samples moves the placed clip (set_clip_position composes)."""
        clean, ctrl = _load_path(_CLEAN3), _load_path(_CLIP_T2)
        out = BS.add_clip_to_track(clean, ctrl, 2, position_file_samples=88200)
        self.assertEqual(BS.clip_positions(out), [88200, 88200])
        self.assertEqual(_reload_ok(out), 0)


@unittest.skipUnless(_CLEAN3.exists() and _CLIP3.exists(),
                     "3-stereo clean / 3-different-clips controls not present")
class MultiClipTests(unittest.TestCase):
    """add_clips_to_tracks places M clips (from clip_ref) onto M tracks in one shot,
    assembling the multi-wav file table / region library / placements and synthesizing
    the index (M clips share one path -> still ONE 0x0f3c marker). PT-confirmed via the
    `3 stereo 3 different clips.ptx` byte-pair."""

    def test_reproduces_3clip_control_load_bearing(self) -> None:
        """Placing the 3 clips on tracks 0/1/2 reproduces the control's load-bearing
        blocks (file table, region lib, path, placements) byte-for-byte."""
        clean, ctrl = _load_path(_CLEAN3), _load_path(_CLIP3)
        out = BS.add_clips_to_tracks(clean, ctrl, [0, 1, 2])
        for ct in (0x1004, 0x262A, 0x0F3D, 0x1054):
            self.assertEqual(_top_by_type(out, ct), _top_by_type(ctrl, ct),
                             msg=f"0x{ct:04x} not byte-exact vs 3-clip control")
        self.assertEqual(_reload_ok(out), 0)
        # six placements (3 stereo clips), index gains exactly one 0x0f3c hole
        self.assertEqual(len(BS.clip_positions(out)), 6)
        seq, bad = _hole_sequence(out)
        ctrl_seq, _ = _hole_sequence(ctrl)
        self.assertEqual(bad, 0)
        self.assertEqual(seq, ctrl_seq, msg="multi-clip index hole sequence != control")

    def test_clips_land_on_assigned_tracks(self) -> None:
        """Each clip lands on its assigned track's lanes for several assignments."""
        clean, ctrl = _load_path(_CLEAN3), _load_path(_CLIP3)
        for assign in ([0, 1, 2], [2, 1, 0], [0, 2, 1]):
            out = BS.add_clips_to_tracks(clean, ctrl, assign)
            self.assertEqual(_reload_ok(out), 0, msg=f"assignment {assign} failed to reload")
            lanes = [b for b in BS.flat_blocks(BS.parse(out)) if b.content_type == 0x1052]
            clip_tracks = sorted({i // 2 for i, L in enumerate(lanes)
                                  if any(c.content_type == 0x1050 for c in L.child)})
            self.assertEqual(clip_tracks, sorted(assign),
                             msg=f"clips not on tracks {sorted(assign)}")

    def test_position_override_moves_all(self) -> None:
        clean, ctrl = _load_path(_CLEAN3), _load_path(_CLIP3)
        out = BS.add_clips_to_tracks(clean, ctrl, [0, 1, 2], position_file_samples=176400)
        self.assertEqual(BS.clip_positions(out), [176400] * 6)
        self.assertEqual(_reload_ok(out), 0)

    def test_track_count_must_match(self) -> None:
        clean, ctrl = _load_path(_CLEAN3), _load_path(_CLIP3)
        with self.assertRaises(ValueError):
            BS.add_clips_to_tracks(clean, ctrl, [0, 1])  # ctrl has 3 clips

    def test_per_clip_positions(self) -> None:
        """A list of positions gives each clip its own timeline start (track 1 @bar2,
        track 2 @bar4, track 3 @bar6) — positions are independent 0x104f fields."""
        import struct
        clean, ctrl = _load_path(_CLEAN3), _load_path(_CLIP3)
        out = BS.add_clips_to_tracks(clean, ctrl, [0, 1, 2],
                                     position_file_samples=[88200, 264600, 441000])
        self.assertEqual(_reload_ok(out), 0)
        got = {}
        for L in [b for b in BS.flat_blocks(BS.parse(out)) if b.content_type == 0x1052]:
            lb = out[L.offset - 7:L.offset + L.block_size]
            nl = struct.unpack_from("<I", lb, 9)[0]
            name = lb[13:13 + nl].decode("latin1")
            for c in L.child:
                for g in c.child:
                    if g.content_type == 0x104F:
                        got[name] = struct.unpack_from("<Q", out, g.offset + 9)[0]
        self.assertEqual(got, {"Audio 1": 88200, "Audio 2": 264600, "Audio 3": 441000})

    def test_position_count_must_match(self) -> None:
        clean, ctrl = _load_path(_CLEAN3), _load_path(_CLIP3)
        with self.assertRaises(ValueError):
            BS.add_clips_to_tracks(clean, ctrl, [0, 1, 2], position_file_samples=[88200, 264600])

    def test_audio_filenames_parses_all(self) -> None:
        """The 0x103a filename parser returns every wav in list order (any stem length)."""
        self.assertEqual(BS._audio_filenames(_load_path(_CLIP3)),
                         ["0102.wav", "0496.wav", "0277.wav"])

    def test_set_clip_wavs_repoints_each_clip(self) -> None:
        """set_clip_wavs re-points each clip (track order) to its own staged BWF wav;
        region names follow the new wavs and the result reloads."""
        import struct
        af = STER / "Audio Files"
        wavs = [af / "(2017226 01116)-C2-GIGZ.wav", af / "(2017617 175918)-F#1-16KK.1.wav",
                af / "01.wav"]
        if not all(w.exists() for w in wavs):
            self.skipTest("staged BWF wavs not present")
        out = BS.set_clip_wavs(_load_path(_CLIP3), [str(w) for w in wavs])
        self.assertEqual(_reload_ok(out), 0)
        regs = []
        for b in BS.flat_blocks(BS.parse(out)):
            if b.content_type == 0x2628:
                rb = out[b.offset - 7:b.offset + b.block_size]
                nl = struct.unpack_from("<I", rb, 9)[0]
                regs.append(rb[13:13 + nl].decode("latin1"))
        # clips are in track order: track1=GIGZ, track2=F#1, track3=01
        self.assertEqual(regs[0], "(2017226 01116)-C2-GIGZ.L")
        self.assertEqual(regs[2], "(2017617 175918)-F#1-16KK.1.L")
        self.assertEqual(regs[4], "01.L")

    def test_build_audio_clips_multi_per_track(self) -> None:
        """The unified builder: hierarchical tracks->clips, distinct wavs assembled from
        scratch, multiple clips on one track, arbitrary positions, one index marker."""
        import struct
        af = STER / "Audio Files"
        gigz, fsharp, one = (af / "(2017226 01116)-C2-GIGZ.wav",
                             af / "(2017617 175918)-F#1-16KK.1.wav", af / "01.wav")
        if not all(w.exists() for w in (gigz, fsharp, one)):
            self.skipTest("staged BWF wavs not present")
        clean, ref = _load_path(_CLEAN3), _load_path(_CLIP3)
        tracks = [[(str(gigz), 0), (str(fsharp), 2 * 88200)],  # track 0: two clips
                  [(str(one), 0)],                              # track 1: one clip
                  []]                                           # track 2: empty
        out = BS.build_audio_clips(clean, tracks, ref)
        self.assertEqual(_reload_ok(out), 0)
        lanes = [L for L in BS.flat_blocks(BS.parse(out)) if L.content_type == 0x1052]
        counts = [len([c for c in L.child if c.content_type == 0x1050]) for L in lanes]
        self.assertEqual(counts, [2, 2, 1, 1, 0, 0])  # track0=2, track1=1, track2=0 (per lane)
        # a 2-clip lane must be byte-sized like the real two-clips-same-track control
        # (guards the lane-trailer-per-placement bug, which reloads but fails in PT)
        if _TWO_CLIPS.exists():
            tc_lane = [b for b in BS.flat_blocks(BS.parse(_load_path(_TWO_CLIPS)))
                       if b.content_type == 0x1052][0]
            self.assertEqual(lanes[0].block_size, tc_lane.block_size,
                             msg="2-clip lane size != control (malformed lane trailer)")
        self.assertEqual(BS._audio_filenames(out),
                         ["(2017226 01116)-C2-GIGZ.wav", "(2017617 175918)-F#1-16KK.1.wav", "01.wav"])
        self.assertEqual(BS.clip_positions(out), [0, 176400, 0, 176400, 0, 0])
        _seq, bad = _hole_sequence(out)
        self.assertEqual(bad, 0, msg="built session index has offsets off a live block")

    def test_named_clip_decoupled_from_filename(self) -> None:
        """A (wav, pos, name) clip gets the display name as its region, while the file
        table keeps the WAV's real filename (PT links by the file table, not the name)."""
        af = STER / "Audio Files"
        one = af / "01.wav"
        if not one.exists():
            self.skipTest("01.wav not present")
        clean, ref = _load_path(_CLEAN3), _load_path(_CLIP3)
        out = BS.build_audio_clips(clean, [[(str(one), 0, "Chorus")], [], []], ref)
        self.assertEqual(_reload_ok(out), 0)
        import struct
        regs = []
        for b in BS.flat_blocks(BS.parse(out)):
            if b.content_type == 0x2628:
                rb = out[b.offset - 7:b.offset + b.block_size]
                nl = struct.unpack_from("<I", rb, 9)[0]
                regs.append(rb[13:13 + nl].decode("latin1"))
        self.assertEqual(regs, ["Chorus.L", "Chorus.R"], msg="clip not named 'Chorus'")
        self.assertEqual(BS._audio_filenames(out), ["01.wav"], msg="filename changed")

    def test_build_single_wav_filelist_matches_control(self) -> None:
        """A single-wav build's 0x103a file list (incl. the D-dependent folder-path
        component indices) is byte-identical to the real two-clips-same-track control."""
        af = STER / "Audio Files"
        gigz = af / "(2017226 01116)-C2-GIGZ.wav"
        if not (gigz.exists() and _TWO_CLIPS.exists()):
            self.skipTest("GIGZ wav / two-clips control not present")
        clean, ref = _load_path(_CLEAN3), _load_path(_CLIP3)
        out = BS.build_audio_clips(clean, [[(str(gigz), 0), (str(gigz), 176400)], [], []], ref)
        self.assertEqual(_reload_ok(out), 0)

        def _filelist(d):
            blk = [b for b in BS.parse(d).blocks if b.content_type == 0x1004][0]
            lead = [c for c in blk.child if c.content_type == 0x103A][0]
            return d[lead.offset - 7:lead.offset + lead.block_size]
        self.assertEqual(_filelist(out), _filelist(_load_path(_TWO_CLIPS)),
                         msg="single-wav file list != control (path-index renumbering)")

    def test_build_region_distinct_findex(self) -> None:
        """Every distinct wav's region carries its OWN 0-based findex (the region->file
        link), pointing at its own entry in the 0x103a file list. A shared template
        findex makes Pro Tools play one wav on every track (the 'all 8 play bass' bug)."""
        af = STER / "Audio Files"
        wavs = [af / "0102.wav", af / "0496.wav", af / "0277.wav"]
        if not all(w.exists() for w in wavs):
            self.skipTest("distinct control wavs not present")
        clean, ref = _load_path(_CLEAN3), _load_path(_CLIP3)
        out = BS.build_audio_clips(clean, [[(str(wavs[0]), 0)], [(str(wavs[1]), 0)],
                                           [(str(wavs[2]), 0)]], ref)
        self.assertEqual(_reload_ok(out), 0)
        fstems = [f.rsplit(".wav", 1)[0] for f in BS._audio_filenames(out)]
        seen = set()
        for name, region in _regions_by_name(out).items():
            _n, findex, findex2, _len, _lb = _decode_region(region)
            seen.add(findex)
            self.assertEqual(findex2, findex,
                             msg=f"region {name!r} playback findex {findex2} != display {findex}")
            self.assertEqual(fstems[findex], name.rsplit(".", 1)[0],
                             msg=f"region {name!r} findex {findex} -> {fstems[findex]!r}")
        self.assertEqual(sorted(seen), [0, 1, 2], msg=f"regions not linked to distinct files: {seen}")

    def test_build_region_long_length_not_truncated(self) -> None:
        """_build_region writes the FULL sample count as a variable-width three-point
        value; a clip over 65535 samples (~1.5s) must not truncate to its low 16 bits
        (the ~0.9s region-length bug). Name length is varied to prove the field tracks it."""
        ref = _load_path(_CLIP3)
        treg = BS.block_bytes(ref, [b for b in BS.flat_blocks(BS.parse(ref))
                                    if b.content_type == 0x2629][0])
        for sc in (11439, 70000, 12030774, 0x00FFFFFF):
            for nm in ("x.L", "long take name.R"):
                r = BS._build_region(treg, nm, 0, sc, b"\x11" * 16, 7)
                name, findex, findex2, length, lbytes = _decode_region(bytearray(r))
                self.assertEqual(name, nm)
                self.assertEqual((findex, findex2), (7, 7), msg=f"findex lost for {nm!r}")
                self.assertEqual(length, sc, msg=f"length {length} != {sc} for name {nm!r}")
                self.assertGreaterEqual(lbytes, (max(1, sc).bit_length() + 7) // 8)

    def test_build_region_byte_exact_vs_control(self) -> None:
        """Rebuilt regions are byte-identical to the real PT-authored control regions
        except their 16-byte GUID nonce. Proves the region layout (findex, length, all
        opaque fields) is correct, not merely reload-clean."""
        import struct
        af = STER / "Audio Files"
        wavs = {"0102": af / "0102.wav", "0496": af / "0496.wav", "0277": af / "0277.wav"}
        if not all(w.exists() for w in wavs.values()):
            self.skipTest("distinct control wavs not present")
        clean, ref = _load_path(_CLEAN3), _load_path(_CLIP3)
        pos = BS.clip_positions(ref)
        mine = BS.build_audio_clips(
            clean, [[(str(wavs["0102"]), pos[0])], [(str(wavs["0496"]), pos[2])],
                    [(str(wavs["0277"]), pos[1])]], ref)
        cr, mr = _regions_by_name(ref), _regions_by_name(mine)
        self.assertEqual(set(cr), set(mr), msg="region name set differs from control")
        for name, c in cr.items():
            m = mr[name]
            self.assertEqual(len(c), len(m), msg=f"{name} block length differs")
            nl = struct.unpack_from("<I", c, 18)[0]
            guid = set(range(97 + (nl - 6), 97 + (nl - 6) + 16))
            other = [i for i in range(len(c)) if c[i] != m[i] and i not in guid]
            self.assertEqual(other, [], msg=f"{name} differs outside GUID at {other}")

    def test_inlined_clip_templates_match_donor(self) -> None:
        """build_audio_clips() with no clip_ref uses the inlined `_templates` chunks and
        produces output byte-identical to passing the donor session explicitly — so the
        clip donor file is no longer needed at build time."""
        af = STER / "Audio Files"
        wavs = [af / "0102.wav", af / "0496.wav", af / "0277.wav"]
        if not all(w.exists() for w in wavs):
            self.skipTest("control wavs not present")
        clean, ref = _load_path(_CLEAN3), _load_path(_CLIP3)
        tracks = [[(str(wavs[0]), 88200)], [(str(wavs[1]), 0)], [(str(wavs[2]), 176400)]]
        self.assertEqual(BS.build_audio_clips(clean, tracks),
                         BS.build_audio_clips(clean, tracks, ref),
                         msg="inlined clip templates != donor-extracted templates")


@unittest.skipUnless(_CLEAN3.exists(), "3-stereo clean control not present")
class TrackNameTests(unittest.TestCase):
    """Arbitrary track names via rename_track / set_track_names, including after clips
    are placed (the placement lanes are named after the track and must stay consistent)."""

    def test_set_track_names(self) -> None:
        clean = _load_path(_CLEAN3)
        out = BS.set_track_names(clean, ["Drums", "Bass", "Vox"])
        self.assertEqual([t.name for t in BS.track_types(out)], ["Drums", "Bass", "Vox"])
        self.assertEqual(_reload_ok(out), 0)

    def test_set_track_names_partial(self) -> None:
        """A dict / None entries leave the other tracks untouched."""
        clean = _load_path(_CLEAN3)
        out = BS.set_track_names(clean, {1: "Bass"})
        self.assertEqual([t.name for t in BS.track_types(out)], ["Audio 1", "Bass", "Audio 3"])
        self.assertEqual(_reload_ok(out), 0)

    @unittest.skipUnless(_CLIP_T2.exists(), "clip control not present")
    def test_rename_track_that_has_a_clip(self) -> None:
        """Renaming a track that carries a clip renames the track AND its placement lanes
        consistently, with the clip preserved."""
        clean, ctrl = _load_path(_CLEAN3), _load_path(_CLIP_T2)
        built = BS.add_clip_to_track(clean, ctrl, 0)  # clip on track 1 (Audio 1)
        out = BS.rename_track(built, "Audio 1", "Drums")
        self.assertEqual(_reload_ok(out), 0)
        self.assertEqual([t.name for t in BS.track_types(out)][0], "Drums")
        lanes = [b for b in BS.flat_blocks(BS.parse(out)) if b.content_type == 0x1052]
        import struct
        for L in lanes[:2]:  # track 1's two lanes
            lb = out[L.offset - 7:L.offset + L.block_size]
            nl = struct.unpack_from("<I", lb, 9)[0]
            self.assertEqual(lb[13:13 + nl].decode("latin1"), "Drums")
            self.assertTrue(any(c.content_type == 0x1050 for c in L.child), "clip lost on rename")


_VAR = ROOT / "control_files" / "various"
_CLEAN2 = STER / "2 stereo tracks.ptx"
_CLICK2 = STER / "2 stereo plus click.ptx"
_UNTITLED = _VAR / "Untitled.ptx"
_TEMPO_REF = _VAR / "120 to 140bpm.ptx"
_METER_REF = _VAR / "3-4 meter at bar 2.ptx"
_MARKER_REF = STER / "a few named markers.ptx"


@unittest.skipUnless(all(p.exists() for p in (_CLEAN3, _CLIP3, _CLEAN2, _CLICK2, _UNTITLED,
                                              _TEMPO_REF, _METER_REF, _BASELINE, _MARKER_REF)),
                     "click-composition controls not present")
class ClickOnTopCompositionTests(unittest.TestCase):
    """The orchestrator's full stack must compose: tempo + meter + markers + clips + a top
    Click track + track renaming. Track renaming MUST be LAST — `move_click_to_top` (inside
    `add_click_anyN(at_top=True)`) re-parses the master index and chokes on a renamed-track
    layout, so `beatmap.build_session_from_beatmap` orders it conductor->clips->click->names."""

    def _stack(self, with_click: bool, names_last: bool):
        af = STER / "Audio Files"
        wavs = [af / "0102.wav", af / "0496.wav", af / "0277.wav"]
        if not all(w.exists() for w in wavs):
            self.skipTest("control wavs not present")
        data = _load_path(_CLEAN3)
        # inlined conductor maps (no donor refs) — exactly as build_session_from_beatmap does
        data = BS.set_tempo_map(data, [(120.0, 0), (140.0, 1920000)])
        data = BS.set_meter_map(data, [(4, 4, 0), (2, 4, 1920000)])
        data = BS.set_markers(data, [("Intro", 0), ("Verse", 960000)])
        if not names_last:
            data = BS.set_track_names(data, ["bass", "drums", "vox"])
        data = BS.build_audio_clips(data, [[(str(wavs[0]), 88200)], [(str(wavs[1]), 88200)],
                                           [(str(wavs[2]), 88200)]], _load_path(_CLIP3))
        if with_click:
            data = BS.add_click_anyN(data, _load_path(_CLEAN2), _load_path(_CLICK2), at_top=True)
        if names_last:
            data = BS.set_track_names(data, ["bass", "drums", "vox"])
        return data

    def test_full_stack_click_on_top(self) -> None:
        """conductor + clips + top Click + names-last reloads, keeps clips/names, and adds
        exactly one (click) track on top of the 3 audio tracks."""
        out = self._stack(with_click=True, names_last=True)
        self.assertEqual(_reload_ok(out), 0)
        self.assertEqual([t.name for t in BS.track_types(out)], ["bass", "drums", "vox"])
        self.assertEqual(len(BS.clip_positions(out)), 6, "clips lost under the click")
        self.assertEqual(len(BS.track_playlist_order(out)), 4, "expected 3 audio + 1 click")
        self.assertIn(b"Click", out)

    def test_renaming_before_or_after_click_is_equivalent(self) -> None:
        """The click is now NAME-ROBUST: renaming tracks BEFORE the top-click works and is
        BYTE-IDENTICAL to renaming after (add_click_anyN normalizes audio names to `Audio N`
        for the splice, then restores them). The old 'rename must be last' constraint is gone."""
        before = self._stack(with_click=True, names_last=False)   # rename BEFORE the click
        after = self._stack(with_click=True, names_last=True)      # rename AFTER the click
        self.assertEqual(_reload_ok(before), 0, "rename-before-click failed to reload")
        self.assertEqual(before, after, "rename-before-click != rename-after-click (not name-robust)")
        self.assertEqual([t.name for t in BS.track_types(before)], ["bass", "drums", "vox"])
        self.assertEqual(len(BS.clip_positions(before)), 6, "clips lost under the click")
        self.assertEqual(len(BS.track_playlist_order(before)), 4, "expected 3 audio + 1 click")

    @staticmethod
    def _view_volume_counts(data: bytes):
        """(view-mode volume blocks, automation volume blocks): 0x203b under a track
        chain/overview (0x2015/0x2589) is a VIEW set to volume; under 0x2580 it's real
        volume automation (must be preserved)."""
        ptf = BS.parse(data)
        par, ct = {}, {}

        def rec(b, p):
            z = b.offset - 7; par[z] = p; ct[z] = b.content_type
            for c in sorted(b.child, key=lambda x: x.offset):
                rec(c, z)
        for b in sorted(ptf.blocks, key=lambda x: x.offset):
            rec(b, None)
        view = sum(1 for z, c in ct.items() if c == 0x203b and ct.get(par[z]) in (0x2015, 0x2589))
        auto = sum(1 for z, c in ct.items() if c == 0x203b and ct.get(par[z]) == 0x2580)
        return view, auto

    def test_click_leaves_volume_view_and_set_waveform_view_fixes_it(self) -> None:
        """PT-confirmed bug: the click splice leaves a track in VOLUME view. `set_waveform_view`
        converts every view-mode volume block (the 22-B 0x203b signature) to the waveform block
        (19-B 0x2038), leaving real 0x2580 volume-automation bytes untouched, and the result
        reloads. (We count by raw signature, not the block tree: the click splice leaves the
        0x2587 overview's internal length fields stale — PT tolerates it but our recursive parser
        can't traverse it; the fix only ever rewrites the exact volume-view signature.)"""
        out = self._stack(with_click=True, names_last=True)
        self.assertGreater(self._view_volume_counts(out)[0], 0,
                           "expected the click to leave >=1 track in volume view")
        vol_before, wav_before = out.count(BS._VIEW_VOLUME), out.count(BS._VIEW_WAVEFORM)
        fixed = BS.set_waveform_view(out)
        self.assertEqual(_reload_ok(fixed), 0, "waveform-view fix failed to reload")
        # the exact volume->waveform conversion happened (raw signature counts shift 1:1)
        n = vol_before - fixed.count(BS._VIEW_VOLUME)
        self.assertGreater(n, 0, "no volume view-blocks were converted")
        self.assertEqual(fixed.count(BS._VIEW_WAVEFORM), wav_before + n,
                         "converted volume blocks did not become waveform blocks")
        self.assertEqual(len(fixed), len(out) - 3 * n, "unexpected size change (3 B/block)")
        # tracks / clips / click all intact after the fix
        self.assertEqual([t.name for t in BS.track_types(fixed)], ["bass", "drums", "vox"])
        self.assertEqual(len(BS.clip_positions(fixed)), 6)
        self.assertEqual(len(BS.track_playlist_order(fixed)), 4)

    def test_set_waveform_view_preserves_volume_automation(self) -> None:
        """Real volume-AUTOMATION lanes (0x203b under 0x2580) share the 22-B volume-view
        signature but must NOT be converted. A clean scaffold has automation volume blocks yet
        zero volume VIEWS, so set_waveform_view is a byte-for-byte no-op (proving it touches only
        view-mode blocks, never automation)."""
        clean = _load_path(_CLEAN3)
        self.assertGreater(clean.count(BS._VIEW_VOLUME), 0, "expected automation volume-lane blocks")
        self.assertEqual(self._view_volume_counts(clean)[0], 0, "clean scaffold should have no volume VIEWS")
        self.assertEqual(BS.set_waveform_view(clean), clean, "set_waveform_view touched automation data")

    def test_set_waveform_view_is_noop_without_click(self) -> None:
        """A clean (no-click) build is already all-waveform, so set_waveform_view is a no-op."""
        out = self._stack(with_click=False, names_last=True)
        self.assertEqual(self._view_volume_counts(out)[0], 0, "no-click build had volume views")
        self.assertEqual(BS.set_waveform_view(out), out, "set_waveform_view changed a clean session")


if __name__ == "__main__":
    unittest.main()
