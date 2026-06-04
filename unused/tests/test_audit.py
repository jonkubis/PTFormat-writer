from pathlib import Path
import unittest

from ptxformatwriter import analyze_session_audit, validate_session_audit


ROOT = Path(__file__).resolve().parents[1]
CONTROL_DIR = Path("/Users/jonkubis/Music/temp/PT/multiple track types")
MIXED_FRESH_A1_M1_A2 = CONTROL_DIR / "multiple track types no click fresh.ptx"
MIXED_STALE_GENERATED = (
    ROOT / "generated" / "mixed_no_click_reorder_audio_midi_audio_global_ordinals_synth_v8.ptx"
)
MIXED_NO_CLICK_2624_ONLY_GOOD = (
    ROOT / "generated" / "mixed_no_click_probe_2624_synth_final_v2.ptx"
)
MIXED_REORDER_2624_ONLY_BAD = (
    ROOT / "generated" / "mixed_no_click_reorder_audio_midi_audio_2624_synth_v5.ptx"
)
FINAL_INDEX_BAD_RECORD_ZERO = (
    ROOT / "generated" / "synth_empty_7_stereo_from_16_synth_scaffold_source_scan_2624_only_v16.ptx"
)
FINAL_INDEX_PLAYLIST_STALE_ONLY = (
    ROOT / "generated" / "synth_empty_7_stereo_from_16_synth_scaffold_source_scan_record0_only_v16.ptx"
)
MVP_TEMPO_METER_V2 = ROOT / "generated" / "mvp_midi_tempo_meter_v2.ptx"
MVP_TEMPO_METER_V3 = ROOT / "generated" / "mvp_midi_tempo_meter_v3.ptx"
AUDIO_CLONED_FILELIST_BAD = (
    ROOT / "generated" / "known_good_with_cloned_1004_exact_semantics_v8.ptx"
)
AUDIO_CLONED_FILELIST_GOOD = (
    ROOT / "generated" / "known_good_with_cloned_1004_filelist_v9.ptx"
)


class SessionAuditTests(unittest.TestCase):
    @unittest.skipUnless(
        MIXED_FRESH_A1_M1_A2.exists(),
        "mixed-track Pro Tools control is not present",
    )
    def test_audit_accepts_real_mixed_reorder_control(self):
        summary = analyze_session_audit(MIXED_FRESH_A1_M1_A2)

        self.assertEqual(validate_session_audit(summary), [])
        self.assertTrue(summary.mixed_order_checked)

    @unittest.skipUnless(
        MIXED_STALE_GENERATED.exists(),
        "stale generated mixed-order probe is not present",
    )
    def test_audit_flags_stale_mixed_order_state(self):
        issues = validate_session_audit(analyze_session_audit(MIXED_STALE_GENERATED))

        self.assertTrue(any("mixed-order: 0x206a MIDI slot" in issue for issue in issues))

    @unittest.skipUnless(
        MIXED_NO_CLICK_2624_ONLY_GOOD.exists(),
        "known-opening 0x2624-only no-click probe is not present",
    )
    def test_audit_allows_global_order_superset_when_playlist_is_subsequence(self):
        summary = analyze_session_audit(MIXED_NO_CLICK_2624_ONLY_GOOD)

        self.assertEqual(validate_session_audit(summary), [])
        self.assertTrue(summary.mixed_order_checked)

    @unittest.skipUnless(
        MIXED_REORDER_2624_ONLY_BAD.exists(),
        "malformed 0x2624-only reorder probe is not present",
    )
    def test_audit_flags_playlist_order_that_is_not_global_subsequence(self):
        issues = validate_session_audit(analyze_session_audit(MIXED_REORDER_2624_ONLY_BAD))

        self.assertTrue(
            any("0x2624 playlist order" in issue for issue in issues)
        )

    @unittest.skipUnless(
        FINAL_INDEX_BAD_RECORD_ZERO.exists(),
        "record-zero final-index hazard control is not present",
    )
    def test_audit_flags_critical_final_marker_reference(self):
        summary = analyze_session_audit(FINAL_INDEX_BAD_RECORD_ZERO)
        issues = validate_session_audit(summary)

        self.assertEqual(len(summary.critical_invalid_marker_refs), 1)
        self.assertTrue(
            any("critical final-index marker refs" in issue for issue in issues)
        )

    @unittest.skipUnless(
        FINAL_INDEX_PLAYLIST_STALE_ONLY.exists(),
        "playlist-stale final-index control is not present",
    )
    def test_audit_does_not_treat_playlist_marker_refs_as_fatal(self):
        summary = analyze_session_audit(FINAL_INDEX_PLAYLIST_STALE_ONLY)

        self.assertGreater(len(summary.invalid_marker_refs), 0)
        self.assertEqual(summary.critical_invalid_marker_refs, ())
        self.assertEqual(validate_session_audit(summary), [])

    @unittest.skipUnless(
        MVP_TEMPO_METER_V2.exists() and MVP_TEMPO_METER_V3.exists(),
        "MVP tempo/meter controls are not present",
    )
    def test_strict_final_index_mode_flags_old_mvp_marker_refs(self):
        bad_summary = analyze_session_audit(MVP_TEMPO_METER_V2)
        good_summary = analyze_session_audit(MVP_TEMPO_METER_V3)

        self.assertEqual(validate_session_audit(bad_summary), [])
        self.assertEqual(validate_session_audit(good_summary), [])
        self.assertGreater(len(bad_summary.suspicious_invalid_marker_refs), 0)
        self.assertEqual(good_summary.suspicious_invalid_marker_refs, ())
        self.assertTrue(
            any(
                "suspicious final-index marker refs" in issue
                for issue in validate_session_audit(
                    bad_summary,
                    strict_final_index_refs=True,
                )
            )
        )
        self.assertEqual(
            validate_session_audit(
                good_summary,
                strict_final_index_refs=True,
            ),
            [],
        )

    @unittest.skipUnless(
        AUDIO_CLONED_FILELIST_BAD.exists() and AUDIO_CLONED_FILELIST_GOOD.exists(),
        "cloned 0x1004 file-list controls are not present",
    )
    def test_audit_flags_broken_cloned_audio_file_list(self):
        bad_summary = analyze_session_audit(AUDIO_CLONED_FILELIST_BAD)
        good_summary = analyze_session_audit(AUDIO_CLONED_FILELIST_GOOD)

        bad_issues = validate_session_audit(bad_summary)
        self.assertTrue(
            any("audio-link: 0x103a header counts" in issue for issue in bad_issues)
        )
        self.assertTrue(
            any("audio-link: 0x103a audio entry" in issue for issue in bad_issues)
        )
        self.assertEqual(validate_session_audit(good_summary), [])
        self.assertEqual(good_summary.audio_file_list_count, 4)
        self.assertEqual(good_summary.audio_metadata_count, 4)


if __name__ == "__main__":
    unittest.main()
