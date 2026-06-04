"""Tests for the donor pack (`ptxformatwriter.donorpack`).

A donor pack bundles every control/donor session a build needs into one regenerable zip.
These tests confirm the pack is byte-faithful to its source donors and that building a
session from the pack is byte-identical to building from the loose control files.
"""
import tempfile
import unittest
from pathlib import Path

from ptxformatwriter import body_synth as B, donorpack as DP, writer as W
from ptxformatwriter.core import PTFFormat

ROOT = Path(__file__).resolve().parents[1]
CTRL = ROOT / "control_files"
STER = CTRL / "lots of stereo tracks"
_NEEDED = [STER / "2 stereo tracks.ptx", STER / "3 stereo tracks.ptx",
           STER / "8 stereo tracks.ptx", STER / "3 stereo 3 different clips.ptx",
           STER / "2 stereo plus click.ptx", STER / "Audio Files" / "01.wav"]


def _reload_ok(data: bytes) -> int:
    with tempfile.NamedTemporaryFile(suffix=".ptx", delete=False) as tmp:
        tmp.write(W.encrypt_session_data(data)); path = tmp.name
    try:
        return PTFFormat().load(path, 48000)
    finally:
        Path(path).unlink(missing_ok=True)


@unittest.skipUnless(all(p.exists() for p in _NEEDED), "donor control files not present")
class DonorPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pack_path = Path(tempfile.mkdtemp()) / "donors.pack"
        DP.build_pack(str(CTRL), str(self.pack_path))
        self.pack = DP.DonorPack.load(str(self.pack_path))

    def test_pack_is_byte_faithful_to_sources(self) -> None:
        """Bundled bytes equal load_unxored() of the source donors."""
        for n in (2, 3, 8):
            self.assertEqual(self.pack.nstereo(n),
                             W.load_unxored(str(STER / f"{n} stereo tracks.ptx")),
                             msg=f"{n}-stereo scaffold != source")
        c = self.pack.controls()
        self.assertIsNone(c.clip_ref, "clip templates are inlined; the clip donor isn't bundled")
        self.assertEqual(c.click_ref, W.load_unxored(str(STER / "2 stereo plus click.ptx")))
        self.assertEqual(c.wav_template, (STER / "Audio Files" / "01.wav").read_bytes())
        # conductor donors (untitled/tempo/meter) are no longer bundled — tempo/meter are inlined
        self.assertNotIn("untitled.bin", self.pack._entries)
        self.assertNotIn("tempo_ref.bin", self.pack._entries)
        self.assertNotIn("meter_ref.bin", self.pack._entries)

    def test_missing_size_raises_clearly(self) -> None:
        with self.assertRaises(KeyError):
            self.pack.nstereo(9999)

    def test_build_from_pack_matches_build_from_files(self) -> None:
        """A full conductor+clips+click+names build is byte-identical whether the donors
        come from the pack or from the loose control files (the pack is transparent)."""
        af = STER / "Audio Files"
        wavs = [af / "0102.wav", af / "0496.wav", af / "0277.wav"]
        if not all(w.exists() for w in wavs):
            self.skipTest("control clip wavs not present")
        L = lambda p: W.load_unxored(str(p))

        def build(nstereo, clip_ref, clean2, click_ref):
            d = nstereo(3)
            d = B.set_tempo_map(d, [(120.0, 0), (128.0, 1_920_000)])   # inlined (no donor)
            d = B.set_meter_map(d, [(4, 4, 0)])                        # inlined (no donor)
            d = B.set_markers(d, [("Intro", 0)])                       # inlined (no donor)
            d = B.build_audio_clips(d, [[(str(wavs[0]), 0)], [(str(wavs[1]), 0)],
                                        [(str(wavs[2]), 88200)]], clip_ref)
            d = B.add_click_anyN(d, clean2, click_ref, at_top=True)
            return B.set_track_names(d, ["bass", "drums", "vox"])

        c = self.pack.controls()
        from_pack = build(c.nstereo, c.clip_ref, c.nstereo(2), c.click_ref)
        from_files = build(lambda n: L(STER / f"{n} stereo tracks.ptx"),
                           L(STER / "3 stereo 3 different clips.ptx"), L(STER / "2 stereo tracks.ptx"),
                           L(STER / "2 stereo plus click.ptx"))
        self.assertEqual(from_pack, from_files, msg="pack build != loose-file build")
        self.assertEqual(_reload_ok(from_pack), 0)


if __name__ == "__main__":
    unittest.main()
