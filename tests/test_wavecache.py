"""Tests for the Pro Tools waveform-overview cache generator (`ptxformatwriter.wavecache`).

Pro Tools writes overviews to a session-folder `WaveCache.wfm` (DDZCHX container) only on
Import/Recalculate, never on plain Open, so a synthesized session shows blank waveforms
until a manual recompute. `wavecache.build_wavecache_for_wavs` produces a cache PT accepts
on open (PT-CONFIRMED on the 8-stem Ride Like the Wind session).

Validated against two real PT-authored single-file caches captured from clean imports:
`probe1` (a DC staircase, 65536 samples = exact window multiple) and `probe2` (a ramp,
70000 samples = a partial final window, and real min!=max). The peak engine + container
reproduce `probe2` BYTE-FOR-BYTE; `probe1` differs only in PT's stale per-session `w11`
bookkeeping (the generator writes the correct clean cache offsets, which `probe2` confirms).
"""
import struct
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WFM = ROOT / "control_files" / "wavecache"
_P1_WAV, _P1_WFM = WFM / "probe1.wav", WFM / "probe1.wfm"
_P2_WAV, _P2_WFM = WFM / "probe2.wav", WFM / "probe2.wfm"

try:
    from ptxformatwriter import wavecache as WC
    _HAVE_NUMPY = True
except Exception:  # numpy missing
    _HAVE_NUMPY = False


def _identity(wfm: bytes):
    """(umid, name, ft1, ft2, audsize) from a single-file cache's CAHIDX entry."""
    io = struct.unpack_from("<I", wfm, 12)[0]
    um = wfm.find(b"\x2a", io + 16)
    nl = struct.unpack_from("<I", wfm, um + 9)[0]
    name = wfm[um + 13:um + 13 + nl].decode("latin1")
    q = um + 13 + nl
    ft1, ft2, audsize = struct.unpack_from("<QQQ", wfm, q)
    return wfm[um:um + 8], name, ft1, ft2, audsize


def _entry_for(wav: Path, wfm: bytes) -> dict:
    """Build a generator entry for `wav` using the identity captured in the real cache, so
    a byte-exact comparison isolates structure/peaks/offsets from file-identity."""
    umid, name, ft1, ft2, audsize = _identity(wfm)
    e = WC.wavecache_entry_for_wav(str(wav), umid=umid, name=name)
    e["ft"], e["ft2"], e["filesize"] = ft1, ft2, audsize
    return e


@unittest.skipUnless(_HAVE_NUMPY and _P2_WAV.exists() and _P2_WFM.exists(),
                     "numpy / wavecache probe fixtures not present")
class WaveCacheByteExactTests(unittest.TestCase):
    def test_probe2_byte_exact(self) -> None:
        """The generator reproduces a real PT ramp cache (partial final window, real
        min!=max) BYTE-FOR-BYTE. This proves peaks, framing, sizes, the w11 peak offsets,
        and the CAHIDX index are all correct."""
        real = _P2_WFM.read_bytes()
        mine = WC.build_wavecache([_entry_for(_P2_WAV, real)])
        self.assertEqual(mine, real, msg="generated cache != real PT cache (ramp)")

    def test_probe1_differs_only_in_w11(self) -> None:
        """probe1 (a stale-session capture) differs ONLY in the per-stream w11 field; the
        generator writes the correct clean cache offsets (proven by probe2)."""
        real = _P1_WFM.read_bytes()
        mine = WC.build_wavecache([_entry_for(_P1_WAV, real)])
        self.assertEqual(len(mine), len(real))
        diffs = [i for i in range(len(real)) if mine[i] != real[i]]
        # every IndxHdr's w11 u32 sits at tag+69; collect those 4-byte windows
        w11 = set()
        o = 0
        while True:
            j = real.find(b"PacketStreamIndxHdr", o)
            if j < 0:
                break
            w11.update(range(j + 69, j + 73))
            o = j + 1
        self.assertTrue(diffs, "expected stale-w11 diffs")
        self.assertTrue(set(diffs) <= w11, msg=f"diffs outside w11 fields: {sorted(set(diffs) - w11)}")


@unittest.skipUnless(_HAVE_NUMPY and _P2_WAV.exists() and _P2_WFM.exists(),
                     "numpy / wavecache probe fixtures not present")
class WaveCacheStructureTests(unittest.TestCase):
    def test_partial_window_is_ceil(self) -> None:
        """A non-multiple length yields ceil(n/spp) overview points (the final partial
        window still gets a point)."""
        chans, n = WC._read_wav_channels(str(_P2_WAV))
        self.assertEqual(n, 70000)
        self.assertEqual(len(WC._peak_bytes(chans[0], 256)) // 4, -(-70000 // 256))   # 274
        self.assertEqual(len(WC._peak_bytes(chans[0], 16384)) // 4, -(-70000 // 16384))  # 5

    def test_peak_point_is_max_then_min_int16(self) -> None:
        """Each overview point is (max, min) as int16 = clip(round(sample/256))."""
        chans, _ = WC._read_wav_channels(str(_P2_WAV))
        pk = WC._peak_bytes(chans[0], 256)
        mx0, mn0 = struct.unpack_from("<hh", pk, 0)
        self.assertGreaterEqual(mx0, mn0)  # max first
        # ramp starts at -full-scale -> first window min ~ -32768, max slightly above
        self.assertLessEqual(mn0, -32000)

    def test_audsize_equals_filesize(self) -> None:
        """The CAHIDX audsize field is the referenced WAV's byte size (probe1 captured
        in-place, so its on-disk size matches the cache)."""
        if not (_P1_WAV.exists() and _P1_WFM.exists()):
            self.skipTest("probe1 not present")
        _umid, _name, _ft1, _ft2, audsize = _identity(_P1_WFM.read_bytes())
        self.assertEqual(audsize, _P1_WAV.stat().st_size)

    def test_multifile_two_files(self) -> None:
        """A multi-file cache concatenates one block per file and a multi-entry index;
        every data_offset lands on an AnalysisSetsHdr and umids are distinct."""
        if not (_P1_WAV.exists() and _P1_WFM.exists()):
            self.skipTest("probe1 not present")
        cache = WC.build_wavecache([_entry_for(_P2_WAV, _P2_WFM.read_bytes()),
                                    _entry_for(_P1_WAV, _P1_WFM.read_bytes())])
        io = struct.unpack_from("<I", cache, 12)[0]
        n = struct.unpack_from("<I", cache, io + 12)[0]
        # walk index entries
        p, umids, offs = io + 16, [], []
        for _ in range(n):
            um = cache.find(b"\x2a", p); nl = struct.unpack_from("<I", cache, um + 9)[0]
            q = um + 13 + nl
            umids.append(cache[um:um + 8]); offs.append(struct.unpack_from("<I", cache, q + 32)[0])
            p = q + 48
        self.assertEqual(len(set(umids)), n, "umids not distinct")
        for off in offs:
            self.assertEqual(cache[off:off + 15], b"AnalysisSetsHdr", "data_offset off a record")


if __name__ == "__main__":
    unittest.main()
