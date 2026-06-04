from pathlib import Path
from io import StringIO
import unittest

from ptxformatwriter import analyze_mixed_track_order, validate_mixed_track_order
from ptxformatwriter.cli import main


CONTROL_DIR = Path("/Users/jonkubis/Music/temp/PT/multiple track types")
CONTROLS = {
    "M1_A1_A2": CONTROL_DIR / "multiple track types no click resaved no changes.ptx",
    "M1_A2_A1": CONTROL_DIR / "multiple track types no click M1 A2 A1.ptx",
    "A1_M1_A2": CONTROL_DIR / "multiple track types no click fresh.ptx",
    "A2_M1_A1": CONTROL_DIR / "multiple track types no click A2 M1 A1.ptx",
    "A1_A2_M1": CONTROL_DIR / "multiple track types no click A1 A2 M1.ptx",
    "A2_A1_M1": CONTROL_DIR / "multiple track types no click A2 A1 M1.ptx",
}


@unittest.skipUnless(
    all(path.exists() for path in CONTROLS.values()),
    "mixed-track permutation controls are not present",
)
class MixedTrackOrderTests(unittest.TestCase):
    def test_six_permutation_grid_extracts_known_order_fields(self):
        summaries = {
            name: analyze_mixed_track_order(path)
            for name, path in CONTROLS.items()
        }
        natural_order = summaries["M1_A1_A2"].global_order

        expected_orders = {
            "M1_A1_A2": ("MIDI 1", "Audio 1", "Audio 2"),
            "M1_A2_A1": ("MIDI 1", "Audio 2", "Audio 1"),
            "A1_M1_A2": ("Audio 1", "MIDI 1", "Audio 2"),
            "A2_M1_A1": ("Audio 2", "MIDI 1", "Audio 1"),
            "A1_A2_M1": ("Audio 1", "Audio 2", "MIDI 1"),
            "A2_A1_M1": ("Audio 2", "Audio 1", "MIDI 1"),
        }
        expected_audio_orders = {
            "M1_A1_A2": ("Audio 1", "Audio 2"),
            "M1_A2_A1": ("Audio 2", "Audio 1"),
            "A1_M1_A2": ("Audio 1", "Audio 2"),
            "A2_M1_A1": ("Audio 2", "Audio 1"),
            "A1_A2_M1": ("Audio 1", "Audio 2"),
            "A2_A1_M1": ("Audio 2", "Audio 1"),
        }
        expected_lanes = {
            "M1_A1_A2": ("Audio 1", "Audio 2", "Audio 2"),
            "M1_A2_A1": ("Audio 2", "Audio 2", "Audio 1"),
            "A1_M1_A2": ("Audio 1", "Audio 2", "Audio 2"),
            "A2_M1_A1": ("Audio 2", "Audio 2", "Audio 1"),
            "A1_A2_M1": ("Audio 1", "Audio 2", "Audio 2"),
            "A2_A1_M1": ("Audio 2", "Audio 2", "Audio 1"),
        }
        expected_midi_slots = {
            "M1_A1_A2": 0,
            "M1_A2_A1": 0,
            "A1_M1_A2": 1,
            "A2_M1_A1": 1,
            "A1_A2_M1": 2,
            "A2_A1_M1": 2,
        }
        expected_markers = {
            "M1_A1_A2": 5,
            "M1_A2_A1": 0xFFFFFFFF,
            "A1_M1_A2": 0xFFFFFFFF,
            "A2_M1_A1": 0xFFFFFFFF,
            "A1_A2_M1": 0xFFFFFFFF,
            "A2_A1_M1": 0xFFFFFFFF,
        }

        for name, summary in summaries.items():
            with self.subTest(name=name):
                self.assertEqual(summary.global_order, expected_orders[name])
                self.assertEqual(summary.playlist_order, expected_orders[name])
                self.assertEqual(summary.audio_order, expected_audio_orders[name])
                self.assertEqual(summary.audio_lanes, expected_lanes[name])
                self.assertEqual(summary.midi_zero_based_slot_206a, expected_midi_slots[name])
                self.assertEqual(summary.marker_201f, expected_markers[name])
                self.assertEqual(
                    validate_mixed_track_order(summary, natural_order=natural_order),
                    [],
                )

    def test_midi_playlist_size_is_not_only_slot_dependent(self):
        summaries = {
            name: analyze_mixed_track_order(path)
            for name, path in CONTROLS.items()
        }
        midi_sizes = {
            name: next(
                entry.full_size
                for entry in summary.playlist
                if entry.content_type == 0x2620
            )
            for name, summary in summaries.items()
        }

        self.assertEqual(midi_sizes["M1_A1_A2"], 712)
        self.assertEqual(midi_sizes["M1_A2_A1"], 714)
        self.assertEqual(midi_sizes["A1_A2_M1"], 712)
        self.assertEqual(midi_sizes["A2_A1_M1"], 714)

    def test_cli_validation_does_not_infer_natural_order_from_input_order(self):
        out = StringIO()
        err = StringIO()

        rc = main(
            [
                "mixed-order",
                "--validate",
                str(CONTROLS["A1_M1_A2"]),
            ],
            out=out,
            err=err,
        )

        self.assertEqual(rc, 0)
        self.assertIn("validation: ok", out.getvalue())

    def test_cli_strict_returns_nonzero_for_known_stale_generated_order(self):
        generated = (
            Path(__file__).resolve().parents[1]
            / "generated"
            / "mixed_no_click_reorder_audio_midi_audio_global_ordinals_synth_v8.ptx"
        )
        if not generated.exists():
            self.skipTest("stale generated mixed-order probe is not present")

        out = StringIO()
        err = StringIO()
        rc = main(
            [
                "mixed-order",
                "--validate",
                "--strict",
                str(generated),
            ],
            out=out,
            err=err,
        )

        self.assertEqual(rc, 1)
        self.assertIn("0x206a MIDI slot", out.getvalue())

    def test_validator_treats_255_midi_slot_as_unset(self):
        generated = (
            Path(__file__).resolve().parents[1]
            / "generated"
            / "mixed_reorder_probe_names_playlists_final2624_v2.ptx"
        )
        if not generated.exists():
            self.skipTest("0xff MIDI-slot mixed-order probe is not present")

        summary = analyze_mixed_track_order(generated)
        issues = validate_mixed_track_order(summary)

        self.assertEqual(summary.midi_zero_based_slot_206a, 0xFF)
        self.assertFalse(any("0x206a MIDI slot" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
