from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ptxformatwriter import (
    AudioClipSpec,
    AudioFileSpec,
    AudioSessionSpec,
    AudioTrackSpec,
    MeterEvent,
    MidiClipSpec,
    MidiEvent,
    MidiSessionSpec,
    MidiTrackSpec,
    PTFFormat,
    SessionAuditError,
    TempoEvent,
    copy_audio_file_for_session,
    write_audio_session,
    write_template_session,
    write_midi_session,
)
from ptxformatwriter.writer import (
    FINAL_INDEX_PATCH_OCCURRENCE_LIMIT,
    _audio_file_2106_content_offset,
    _audio_file_bext_umid_offset,
    _audio_file_identity,
    _audio_file_length,
    _build_offset_maps,
    _audio_file_private_id_offset,
    _fixed_width_audio_clip_string,
    _fixed_width_latin1_string,
    _final_index_records,
    _first_top_level,
    _flatten_block_starts,
    _replace_top_level_blocks,
    _synthetic_audio_file_private_id,
    _update_final_block_marker,
    _update_final_index,
    _windows_filetime_from_unix_ns,
    load_unxored,
    parse_unxored,
    top_level_refs,
)


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "bins" / "TestPTX.ptx"
AUDIO_BASE = ROOT / "generated" / "one audio clip bar 1.ptx"
AUDIO_TWO_TRACKS = ROOT / "generated" / "two audio tracks one clip each.ptx"
AUDIO_TWO_TRACKS_EMPTY = ROOT / "generated" / "two audio tracks no clips.ptx"
AUDIO_TWO_FILES = ROOT / "generated" / "two audio files one clip each.ptx"
AUDIO_STEREO_EMPTY = ROOT / "generated" / "one stereo audio track no clips.ptx"
AUDIO_STEREO_CLIP = ROOT / "generated" / "one stereo audio clip bar1.ptx"
AUDIO_TWO_STEREO_EMPTY = ROOT / "generated" / "two stereo audio tracks no clips.ptx"
AUDIO_TWO_STEREO_DISTINCT = ROOT / "generated" / "two stereo tracks two distinct wav clips.ptx"
AUDIO_FOUR_STEREO_EMPTY = ROOT / "generated" / "four stereo audio tracks no clips.ptx"
AUDIO_FOUR_STEREO_DISTINCT = ROOT / "generated" / "four stereo tracks four distinct wav clips.ptx"
AUDIO_512_STEREO_EMPTY = Path(
    "/Users/jonkubis/Music/temp/PT/lots of stereo tracks/lots of stereo tracks.ptx"
)
AUDIO_512_FIRST7_BAD_INDEX = (
    ROOT / "generated" / "synth_audio_512_stereo_scaffold_first7_preserve_lanes_v2.ptx"
)
AUDIO_512_FIRST7_GOOD_INDEX = (
    ROOT / "generated" / "synth_audio_512_first7_index_non_playlist_refs_v6.ptx"
)
SCAFFOLD_7_CONSERVATIVE_INDEX = (
    ROOT / "generated" / "synth_empty_7_stereo_from_16_synth_scaffold_conservative_final_v10.ptx"
)
SCAFFOLD_7_BAD_RECORD0_INDEX = (
    ROOT / "generated" / "synth_empty_7_stereo_from_16_synth_scaffold_source_scan_donor_counts_v15.ptx"
)
SCAFFOLD_7_FIXED_RECORD0_INDEX = (
    ROOT
    / "generated"
    / "synth_empty_7_stereo_from_16_synth_scaffold_source_scan_record0_only_v16.ptx"
)
MIXED_NO_CLICK = Path(
    "/Users/jonkubis/Music/temp/PT/multiple track types/multiple track types no click.ptx"
)
MIXED_NO_CLICK_A1_M1_A2 = Path(
    "/Users/jonkubis/Music/temp/PT/multiple track types/multiple track types no click fresh.ptx"
)
MIXED_NO_CLICK_GLOBAL_RAW_FINAL = (
    ROOT / "generated" / "mixed_no_click_probe_global_no2587_v1.ptx"
)
MIXED_NO_CLICK_GLOBAL_BAD_2519_FINAL = (
    ROOT / "generated" / "mixed_no_click_probe_global_synth_final_v2.ptx"
)
NAME_LENGTH_DIR = Path("/Users/jonkubis/Music/temp/PT/names")
EXPANDING_TRACK_NAMES = NAME_LENGTH_DIR / "expanding_track_names.ptx"
STEREO_13_TRACKS = Path(
    "/Users/jonkubis/Music/temp/PT/lots of stereo tracks/13 stereo tracks.ptx"
)
AUDIO_FILES_DIR = ROOT / "generated" / "Audio Files"
MILESTONE_7_TRACK_NAMES_PADDED = (
    ROOT / "generated" / "milestone_7_stereo_track_names_padded_short_v5.ptx"
)
MILESTONE_7_TRACK_NAMES_SHORT = (
    ROOT / "generated" / "milestone_7_stereo_track_names_shorter_v3.ptx"
)
MILESTONE_7_CLIP_NAMES_PADDED = (
    ROOT / "generated" / "milestone_7_stereo_clip_names_padded_short_v5.ptx"
)
MILESTONE_7_CLIP_NAMES_SHORT = (
    ROOT / "generated" / "milestone_7_stereo_clip_names_shorter_v3.ptx"
)
MILESTONE_7_BOTH_NAMES_PADDED = (
    ROOT / "generated" / "milestone_7_stereo_both_names_padded_public_writer_v6.ptx"
)
MILESTONE_7_BOTH_NAMES_SHORT = (
    ROOT / "generated" / "milestone_7_stereo_both_names_shorter_from_both_padded_v15.ptx"
)


def _load_session(path: Path) -> PTFFormat:
    ptf = PTFFormat()
    result = ptf.load(path, 48000)
    if result != 0:
        raise AssertionError(f"PTFFormat.load returned {result} for {path}")
    return ptf


def _notes(events):
    return [(event.pos, event.length, event.note, event.velocity) for event in events]


def _max_final_index_block_start_hits(path: Path) -> int:
    data = load_unxored(path)
    starts = {offset for offset, _content_type in _flatten_block_starts(data)}
    final = top_level_refs(data)[-1]
    content = data[final.start : final.end]
    hits = {}
    for pos in range(0, max(len(content) - 3, 0)):
        value = int.from_bytes(content[pos : pos + 4], "little")
        if value in starts:
            hits[value] = hits.get(value, 0) + 1
    return max(hits.values(), default=0)


def _final_index_record(data: bytes, record_index: int) -> bytes:
    known_content_types = {content_type for _offset, content_type in _flatten_block_starts(data)}
    final = top_level_refs(data)[-1]
    records = _final_index_records(final.data, known_content_types)
    start, end, _content_type = records[record_index]
    return final.data[start:end]


def _final_index_records_of_type(data: bytes, content_type: int) -> list[bytes]:
    known_content_types = {item_type for _offset, item_type in _flatten_block_starts(data)}
    final = top_level_refs(data)[-1]
    records = _final_index_records(final.data, known_content_types)
    return [
        final.data[start:end]
        for start, end, record_content_type in records
        if record_content_type == content_type
    ]


def _audio_metadata_records(path: Path) -> list[bytes]:
    data = load_unxored(path)
    table = _first_top_level(parse_unxored(data), 0x1004)
    return [
        data[child.offset : child.offset + child.block_size]
        for child in table.child
        if child.content_type == 0x1003
    ]


def _audio_file_list_entries(path: Path) -> tuple[bytes, list[tuple[str, bytes]]]:
    data = load_unxored(path)
    table = _first_top_level(parse_unxored(data), 0x1004)
    child = next(child for child in table.child if child.content_type == 0x103A)
    content = data[child.offset : child.offset + child.block_size]
    pos = 11
    entries = []
    while pos + 13 <= len(content):
        name_len = int.from_bytes(content[pos : pos + 4], "little")
        name_start = pos + 4
        name_end = name_start + name_len
        entry_end = name_end + 9
        if entry_end > len(content):
            break
        entries.append(
            (
                content[name_start:name_end].decode("latin-1"),
                bytes(content[name_end:entry_end]),
            )
        )
        pos = entry_end
    return content[:11], entries


class WriterTests(unittest.TestCase):
    def test_fixed_width_audio_name_helpers_pad_without_moving_channel_suffix(self):
        self.assertEqual(
            _fixed_width_latin1_string("T 01", 7, "track"),
            b"\x07\x00\x00\x00T 01   ",
        )
        self.assertEqual(
            _fixed_width_audio_clip_string("S01 Clip.L", 14),
            b"\x0e\x00\x00\x00S01 Clip    .L",
        )
        with self.assertRaises(ValueError):
            _fixed_width_latin1_string("Too Long", 3, "track")

    def test_write_midi_session_round_trips_music_maps_and_names(self):
        session = MidiSessionSpec(
            tracks=(
                MidiTrackSpec(
                    name="Piano Lead",
                    clips=(
                        MidiClipSpec(
                            name="Piano A",
                            startpos=0,
                            notes=(
                                MidiEvent(pos=0, length=480000, note=60, velocity=90),
                                MidiEvent(pos=960000, length=960000, note=64, velocity=100),
                            ),
                        ),
                    ),
                ),
                MidiTrackSpec(
                    name="Bass Pulse",
                    clips=(
                        MidiClipSpec(
                            name="Bass A",
                            startpos=3840000,
                            notes=(
                                MidiEvent(pos=0, length=960000, note=48, velocity=110),
                            ),
                        ),
                    ),
                ),
            ),
            tempo_events=(
                TempoEvent(pos=0, bpm=120.0, ppq=960000),
                TempoEvent(pos=3840000, bpm=140.0, ppq=960000),
            ),
            meter_events=(
                MeterEvent(pos=0, numerator=4, denominator=4, ordinal=1),
                MeterEvent(pos=3840000, numerator=3, denominator=4, ordinal=2),
            ),
        )

        with TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "writer_smoke.ptx"
            write_midi_session(TEMPLATE, output, session, midi_template=TEMPLATE)

            ptf = _load_session(output)

        self.assertEqual(
            [(event.pos, event.bpm, event.ppq) for event in ptf.tempoevents()],
            [(0, 120.0, 960000), (3840000, 140.0, 960000)],
        )
        self.assertEqual(
            [
                (event.pos, event.numerator, event.denominator, event.ordinal)
                for event in ptf.meterevents()
            ],
            [(0, 4, 4, 1), (3840000, 3, 4, 2)],
        )

        self.assertEqual(
            [
                (region.index, region.name, region.length, _notes(region.midi))
                for region in ptf.midiregions()
            ],
            [
                (
                    0,
                    "Piano A",
                    1920000,
                    [(0, 480000, 60, 90), (960000, 960000, 64, 100)],
                ),
                (1, "Bass A", 960000, [(0, 960000, 48, 110)]),
            ],
        )
        self.assertEqual(
            [
                (
                    track.index,
                    track.name,
                    track.reg.index,
                    track.reg.name,
                    track.reg.startpos,
                    _notes(track.reg.midi),
                )
                for track in ptf.miditracks()
            ],
            [
                (
                    0,
                    "Piano Lead",
                    0,
                    "Piano A",
                    0,
                    [(0, 480000, 60, 90), (960000, 960000, 64, 100)],
                ),
                (
                    1,
                    "Bass Pulse",
                    1,
                    "Bass A",
                    3840000,
                    [(0, 960000, 48, 110)],
                ),
            ],
        )
        self.assertEqual(
            [
                (
                    placement.track_index,
                    placement.track_name,
                    placement.region_index,
                    placement.region_name,
                    placement.startpos,
                    _notes(placement.midi),
                )
                for placement in ptf.midiplacements()
            ],
            [
                (
                    0,
                    "Piano Lead",
                    0,
                    "Piano A",
                    0,
                    [(0, 480000, 60, 90), (960000, 960000, 64, 100)],
                ),
                (
                    1,
                    "Bass Pulse",
                    1,
                    "Bass A",
                    3840000,
                    [(0, 960000, 48, 110)],
                ),
            ],
        )

    @unittest.skipUnless(
        AUDIO_BASE.exists() and AUDIO_TWO_TRACKS.exists(),
        "audio reverse-engineering controls are not present",
    )
    def test_block_source_can_copy_source_final_index(self):
        with TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "two_tracks_with_source_index.ptx"
            write_template_session(
                AUDIO_BASE,
                output,
                block_sources={
                    AUDIO_TWO_TRACKS: (
                        0x262A,
                        0x1054,
                        0x1015,
                        0x2107,
                        0x2519,
                        0x2624,
                        0x2587,
                        0x202B,
                        0x0002,
                    )
                },
            )

            ptf = PTFFormat()
            self.assertEqual(ptf.load(output, 44100), 0)

            final = top_level_refs(load_unxored(output))[-1]
            source_final = top_level_refs(load_unxored(AUDIO_TWO_TRACKS))[-1]

        self.assertEqual(final.block.content_type, 0x0002)
        self.assertEqual(final.block.block_size, source_final.block.block_size)
        self.assertEqual(
            [
                (
                    track.index,
                    track.name,
                    track.reg.index,
                    track.reg.name,
                    track.reg.startpos,
                )
                for track in ptf.tracks()
            ],
            [
                (0, "Audio 1", 0, "Clip 1", 0),
                (1, "Audio 2", 0, "Clip 1", 0),
            ],
        )

    @unittest.skipUnless(
        AUDIO_512_STEREO_EMPTY.exists()
        and AUDIO_512_FIRST7_BAD_INDEX.exists()
        and AUDIO_512_FIRST7_GOOD_INDEX.exists(),
        "512-track final-index controls are not present",
    )
    def test_large_final_index_skips_unmarked_playlist_references(self):
        base = load_unxored(AUDIO_512_STEREO_EMPTY)
        bad = load_unxored(AUDIO_512_FIRST7_BAD_INDEX)
        good = load_unxored(AUDIO_512_FIRST7_GOOD_INDEX)
        base_final = top_level_refs(base)[-1]
        bad_final = top_level_refs(bad)[-1]

        unpatched = (
            bad[: bad_final.start]
            + base[base_final.start : base_final.end]
            + bad[bad_final.end :]
        )
        patched = _update_final_index(_update_final_block_marker(unpatched), base)

        self.assertEqual(patched, good)

    @unittest.skipUnless(
        SCAFFOLD_7_CONSERVATIVE_INDEX.exists()
        and SCAFFOLD_7_BAD_RECORD0_INDEX.exists()
        and SCAFFOLD_7_FIXED_RECORD0_INDEX.exists(),
        "7-track scaffold final-index controls are not present",
    )
    def test_final_index_skips_unmarked_offsets_in_record_zero(self):
        source = load_unxored(SCAFFOLD_7_CONSERVATIVE_INDEX)
        fixed = load_unxored(SCAFFOLD_7_FIXED_RECORD0_INDEX)
        bad = load_unxored(SCAFFOLD_7_BAD_RECORD0_INDEX)

        patched = _update_final_index(_update_final_block_marker(fixed), source)

        self.assertEqual(_final_index_record(patched, 0), _final_index_record(fixed, 0))
        self.assertNotEqual(_final_index_record(bad, 0), _final_index_record(fixed, 0))

    @unittest.skipUnless(
        MIXED_NO_CLICK.exists()
        and MIXED_NO_CLICK_GLOBAL_RAW_FINAL.exists()
        and MIXED_NO_CLICK_GLOBAL_BAD_2519_FINAL.exists(),
        "mixed-track 0x2519 final-index controls are not present",
    )
    def test_final_index_skips_unmarked_offsets_in_2519_records(self):
        source = load_unxored(MIXED_NO_CLICK)
        raw_final = load_unxored(MIXED_NO_CLICK_GLOBAL_RAW_FINAL)
        bad_2519 = load_unxored(MIXED_NO_CLICK_GLOBAL_BAD_2519_FINAL)

        patched = _update_final_index(_update_final_block_marker(raw_final), source)

        self.assertEqual(
            _final_index_records_of_type(patched, 0x2519),
            _final_index_records_of_type(raw_final, 0x2519),
        )
        self.assertNotEqual(
            _final_index_records_of_type(bad_2519, 0x2519),
            _final_index_records_of_type(raw_final, 0x2519),
        )

    @unittest.skipUnless(
        all(
            (NAME_LENGTH_DIR / f"{name}.ptx").exists()
            for name in (
                "A",
                "AB",
                "ABC",
                "ABCD",
                "ABCDEFG",
                "ABCDEFGH",
                "ABCDEFGHIJ",
                "ABCDEFGHIJKLMNOP",
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            )
        ),
        "track-name length controls are not present",
    )
    def test_final_index_patches_tagged_2519_child_references(self):
        pairs = (
            ("A", "AB"),
            ("AB", "ABCD"),
            ("ABC", "ABCDEFG"),
            ("ABCD", "ABCDEFGHIJ"),
            ("ABCDEFGH", "ABCDEFGHIJKLMNOP"),
            ("ABCD", "AB"),
            ("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "A"),
        )
        for source_name, target_name in pairs:
            with self.subTest(source=source_name, target=target_name):
                source = load_unxored(NAME_LENGTH_DIR / f"{source_name}.ptx")
                target = load_unxored(NAME_LENGTH_DIR / f"{target_name}.ptx")
                source_refs = {
                    ref.block.content_type: ref for ref in top_level_refs(source)
                }
                replacements = {
                    ref.block.content_type: ref.data
                    for ref in top_level_refs(target)
                    if ref.block.content_type != 0x0002
                    and source_refs.get(ref.block.content_type) is not None
                    and source_refs[ref.block.content_type].data != ref.data
                }

                synthesized = _replace_top_level_blocks(source, replacements)
                self.assertEqual(synthesized[:0x13], target[:0x13])
                self.assertEqual(synthesized[0x14:], target[0x14:])

    @unittest.skipUnless(
        EXPANDING_TRACK_NAMES.exists() and STEREO_13_TRACKS.exists(),
        "expanding track-name controls are not present",
    )
    def test_final_index_patches_2519_child_offset_tables(self):
        source = load_unxored(STEREO_13_TRACKS)
        target = load_unxored(EXPANDING_TRACK_NAMES)
        source_refs = {ref.block.content_type: ref for ref in top_level_refs(source)}
        replacements = {
            ref.block.content_type: ref.data
            for ref in top_level_refs(target)
            if ref.block.content_type != 0x0002
            and source_refs.get(ref.block.content_type) is not None
            and source_refs[ref.block.content_type].data != ref.data
        }

        synthesized = _replace_top_level_blocks(source, replacements)
        self.assertEqual(synthesized[:0x13], target[:0x13])
        self.assertEqual(synthesized[0x14:], target[0x14:])

    @unittest.skipUnless(
        all(
            path.exists()
            for path in (
                MILESTONE_7_TRACK_NAMES_PADDED,
                MILESTONE_7_TRACK_NAMES_SHORT,
                MILESTONE_7_CLIP_NAMES_PADDED,
                MILESTONE_7_CLIP_NAMES_SHORT,
                MILESTONE_7_BOTH_NAMES_PADDED,
                MILESTONE_7_BOTH_NAMES_SHORT,
            )
        ),
        "milestone short-name controls are not present",
    )
    def test_final_index_patches_unmarked_end_references(self):
        cases = (
            (
                MILESTONE_7_TRACK_NAMES_PADDED,
                MILESTONE_7_TRACK_NAMES_SHORT,
                (0x0DCC4, 0x1267B, 0x17FA8),
                (0x8FC5,),
            ),
            (
                MILESTONE_7_CLIP_NAMES_PADDED,
                MILESTONE_7_CLIP_NAMES_SHORT,
                (0x1267B,),
                (0x8FC5,),
            ),
            (
                MILESTONE_7_BOTH_NAMES_PADDED,
                MILESTONE_7_BOTH_NAMES_SHORT,
                (0x0DCC4, 0x1267B, 0x17FA8),
                (0x8FC5,),
            ),
        )
        for source_path, target_path, final_positions, embedded_positions in cases:
            with self.subTest(source=source_path.name, target=target_path.name):
                source = load_unxored(source_path)
                target = load_unxored(target_path)
                source_refs = {
                    ref.block.content_type: ref for ref in top_level_refs(source)
                }
                replacements = {
                    ref.block.content_type: ref.data
                    for ref in top_level_refs(target)
                    if ref.block.content_type != 0x0002
                    and source_refs.get(ref.block.content_type) is not None
                    and source_refs[ref.block.content_type].data != ref.data
                }

                synthesized = _replace_top_level_blocks(source, replacements)
                _start_map, end_map = _build_offset_maps(source, synthesized)
                source_final = top_level_refs(source)[-1].data
                synthesized_final = top_level_refs(synthesized)[-1].data

                for final_position in final_positions:
                    old_end = int.from_bytes(
                        source_final[final_position : final_position + 4],
                        "little",
                    )
                    self.assertIn(old_end, end_map)
                    self.assertEqual(
                        int.from_bytes(
                            synthesized_final[final_position : final_position + 4],
                            "little",
                        ),
                        end_map[old_end],
                    )

                source_final = top_level_refs(source)[-1].data
                synthesized_final = top_level_refs(synthesized)[-1].data
                source_top = top_level_refs(source)
                synthesized_top = top_level_refs(synthesized)
                shifted_unchanged = [
                    (old_ref.start, old_ref.end, new_ref.start - old_ref.start)
                    for old_ref, new_ref in zip(source_top, synthesized_top)
                    if old_ref.block.content_type == new_ref.block.content_type
                    and old_ref.data == new_ref.data
                    and old_ref.start != new_ref.start
                ]
                for final_position in embedded_positions:
                    old_offset = int.from_bytes(
                        source_final[final_position : final_position + 4],
                        "little",
                    )
                    expected = next(
                        old_offset + delta
                        for start, end, delta in shifted_unchanged
                        if start <= old_offset < end
                    )
                    self.assertEqual(
                        int.from_bytes(
                            synthesized_final[final_position : final_position + 4],
                            "little",
                        ),
                        expected,
                    )

    @unittest.skipUnless(
        MIXED_NO_CLICK.exists() and MIXED_NO_CLICK_A1_M1_A2.exists(),
        "mixed-track reorder controls are not present",
    )
    def test_write_template_session_audits_generated_output_by_default(self):
        with TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "stale_mixed_order.ptx"
            with self.assertRaises(SessionAuditError) as raised:
                write_template_session(
                    MIXED_NO_CLICK,
                    output,
                    block_sources={
                        MIXED_NO_CLICK_A1_M1_A2: (
                            0x2107,
                            0x2519,
                            0x2624,
                            0x2587,
                        )
                    },
                )

            self.assertFalse(output.exists())

        self.assertIn("0x206a MIDI slot", str(raised.exception))
        self.assertIn("mixed-order validation", raised.exception.report)

    @unittest.skipUnless(
        MIXED_NO_CLICK.exists() and MIXED_NO_CLICK_A1_M1_A2.exists(),
        "mixed-track reorder controls are not present",
    )
    def test_write_template_session_can_skip_audit_for_research_probes(self):
        with TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "stale_mixed_order.ptx"
            write_template_session(
                MIXED_NO_CLICK,
                output,
                block_sources={
                    MIXED_NO_CLICK_A1_M1_A2: (
                        0x2107,
                        0x2519,
                        0x2624,
                        0x2587,
                    )
                },
                validate_output=False,
            )

            self.assertTrue(output.exists())

    @unittest.skipUnless(
        AUDIO_STEREO_EMPTY.exists() and AUDIO_STEREO_CLIP.exists(),
        "audio reverse-engineering controls are not present",
    )
    def test_audio_region_tail_references_follow_file_and_channel(self):
        session = AudioSessionSpec(
            audio_files=(
                AudioFileSpec(filename="First.wav", length=264600, channels=2),
                AudioFileSpec(filename="Second.wav", length=264600, channels=2),
            ),
            tracks=(
                AudioTrackSpec(
                    name="Stereo",
                    channels=2,
                    clips=(
                        AudioClipSpec(
                            name="First Clip",
                            file_index=0,
                            startpos=0,
                            sampleoffset=0,
                            length=44100,
                        ),
                        AudioClipSpec(
                            name="Second Clip",
                            file_index=1,
                            startpos=44100,
                            sampleoffset=0,
                            length=44100,
                        ),
                    ),
                ),
            ),
        )

        with TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "region_tail_refs.ptx"
            write_audio_session(
                AUDIO_STEREO_EMPTY,
                output,
                session,
                audio_template=AUDIO_STEREO_CLIP,
            )
            data = load_unxored(output)

        ptf = parse_unxored(data)
        regions = next(block for block in ptf.blocks if block.content_type == 0x262A).child

        for idx, region in enumerate(regions):
            region_info = next(
                child for child in region.child if child.content_type == 0x2628
            )
            tail_start = region_info.offset + region_info.block_size
            tail_end = region.offset + region.block_size
            tail = data[tail_start:tail_end]
            expected_file = idx // 2
            expected_channel = idx % 2

            self.assertEqual(int.from_bytes(tail[:4], "little"), expected_file)
            self.assertEqual(int.from_bytes(tail[4:8], "little"), expected_channel)
            self.assertEqual(int.from_bytes(tail[-8:-4], "little"), expected_file)

    @unittest.skipUnless(
        AUDIO_BASE.exists() and AUDIO_TWO_FILES.exists(),
        "audio reverse-engineering controls are not present",
    )
    def test_write_audio_session_round_trips_same_track_files_regions_and_placements(self):
        session = AudioSessionSpec(
            audio_files=(
                AudioFileSpec(filename="Audio 1-SigGen_01.wav", length=264600),
                AudioFileSpec(filename="Audio 1-SigGen_02.wav", length=264600),
            ),
            tracks=(
                AudioTrackSpec(
                    name="Audio 1",
                    clips=(
                        AudioClipSpec(
                            name="First File",
                            file_index=0,
                            startpos=0,
                            sampleoffset=88200,
                            length=88200,
                        ),
                        AudioClipSpec(
                            name="Second File",
                            file_index=1,
                            startpos=176400,
                            sampleoffset=88200,
                            length=88200,
                        ),
                    ),
                ),
            ),
        )

        with TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "structured_audio.ptx"
            write_audio_session(
                AUDIO_BASE,
                output,
                session,
                audio_template=AUDIO_TWO_FILES,
            )

            ptf = PTFFormat()
            self.assertEqual(ptf.load(output, 44100), 0)

        self.assertEqual(
            [(audio.index, audio.filename, audio.length) for audio in ptf.audiofiles()],
            [
                (0, "Audio 1-SigGen_01.wav", 264600),
                (1, "Audio 1-SigGen_02.wav", 264600),
            ],
        )
        self.assertEqual(
            [
                (
                    region.index,
                    region.name,
                    region.wave.index,
                    region.sampleoffset,
                    region.length,
                )
                for region in ptf.regions()
            ],
            [
                (0, "First File", 0, 88200, 88200),
                (1, "Second File", 1, 88200, 88200),
            ],
        )
        self.assertEqual(
            [
                (
                    track.index,
                    track.name,
                    track.reg.index,
                    track.reg.wave.index,
                    track.reg.startpos,
                )
                for track in ptf.tracks()
            ],
            [
                (0, "Audio 1", 0, 0, 0),
                (0, "Audio 1", 1, 1, 176400),
            ],
        )

    @unittest.skipUnless(
        AUDIO_BASE.exists() and AUDIO_TWO_TRACKS_EMPTY.exists(),
        "audio reverse-engineering controls are not present",
    )
    def test_write_audio_session_round_trips_two_mono_tracks_with_scaffold(self):
        session = AudioSessionSpec(
            audio_files=(
                AudioFileSpec(filename="Audio 1-SigGen_01.wav", length=264600),
            ),
            tracks=(
                AudioTrackSpec(
                    name="DX Left",
                    clips=(
                        AudioClipSpec(
                            name="Line A",
                            file_index=0,
                            startpos=0,
                            sampleoffset=88200,
                            length=88200,
                        ),
                    ),
                ),
                AudioTrackSpec(
                    name="DX Right",
                    clips=(
                        AudioClipSpec(
                            name="Line B",
                            file_index=0,
                            startpos=176400,
                            sampleoffset=88200,
                            length=88200,
                        ),
                    ),
                ),
            ),
            tempo_events=(
                TempoEvent(pos=0, bpm=120.0, ppq=960000),
                TempoEvent(pos=3840000, bpm=140.0, ppq=960000),
            ),
            meter_events=(
                MeterEvent(pos=0, numerator=4, denominator=4, ordinal=1),
                MeterEvent(pos=3840000, numerator=3, denominator=4, ordinal=2),
            ),
        )

        with TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "structured_two_mono_tracks.ptx"
            write_audio_session(
                AUDIO_TWO_TRACKS_EMPTY,
                output,
                session,
                audio_template=AUDIO_BASE,
            )

            ptf = PTFFormat()
            self.assertEqual(ptf.load(output, 44100), 0)
            max_final_index_hits = _max_final_index_block_start_hits(output)

        self.assertEqual(
            [
                (
                    region.index,
                    region.name,
                    region.wave.index,
                    region.sampleoffset,
                    region.length,
                )
                for region in ptf.regions()
            ],
            [
                (0, "Line A", 0, 88200, 88200),
                (1, "Line B", 0, 88200, 88200),
            ],
        )
        self.assertLessEqual(max_final_index_hits, FINAL_INDEX_PATCH_OCCURRENCE_LIMIT)
        self.assertEqual(
            [(event.pos, event.bpm, event.ppq) for event in ptf.tempoevents()],
            [(0, 120.0, 960000), (3840000, 140.0, 960000)],
        )
        self.assertEqual(
            [
                (event.pos, event.numerator, event.denominator, event.ordinal)
                for event in ptf.meterevents()
            ],
            [(0, 4, 4, 1), (3840000, 3, 4, 2)],
        )
        self.assertEqual(
            [
                (
                    track.index,
                    track.name,
                    track.reg.index,
                    track.reg.name,
                    track.reg.startpos,
                )
                for track in ptf.tracks()
            ],
            [
                (0, "DX Left", 0, "Line A", 0),
                (1, "DX Right", 1, "Line B", 176400),
            ],
        )

    @unittest.skipUnless(
        AUDIO_STEREO_EMPTY.exists() and AUDIO_STEREO_CLIP.exists(),
        "audio reverse-engineering controls are not present",
    )
    def test_write_audio_session_round_trips_stereo_track_with_scaffold(self):
        session = AudioSessionSpec(
            audio_files=(
                AudioFileSpec(filename="Audio 1-SigGen_04.wav", length=264600, channels=2),
            ),
            tracks=(
                AudioTrackSpec(
                    name="Stereo Print",
                    clips=(
                        AudioClipSpec(
                            name="Print A",
                            file_index=0,
                            startpos=0,
                            sampleoffset=88200,
                            length=88200,
                        ),
                    ),
                    channels=2,
                ),
            ),
            tempo_events=(
                TempoEvent(pos=0, bpm=120.0, ppq=960000),
                TempoEvent(pos=3840000, bpm=140.0, ppq=960000),
            ),
            meter_events=(
                MeterEvent(pos=0, numerator=4, denominator=4, ordinal=1),
                MeterEvent(pos=3840000, numerator=3, denominator=4, ordinal=2),
            ),
        )

        with TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "structured_stereo.ptx"
            write_audio_session(
                AUDIO_STEREO_EMPTY,
                output,
                session,
                audio_template=AUDIO_STEREO_CLIP,
            )

            ptf = PTFFormat()
            self.assertEqual(ptf.load(output, 44100), 0)

        self.assertEqual(
            [
                (
                    region.index,
                    region.name,
                    region.wave.index,
                    region.sampleoffset,
                    region.length,
                )
                for region in ptf.regions()
            ],
            [
                (0, "Print A.L", 0, 88200, 88200),
                (1, "Print A.R", 0, 88200, 88200),
            ],
        )
        self.assertEqual(
            [(event.pos, event.bpm, event.ppq) for event in ptf.tempoevents()],
            [(0, 120.0, 960000), (3840000, 140.0, 960000)],
        )
        self.assertEqual(
            [
                (event.pos, event.numerator, event.denominator, event.ordinal)
                for event in ptf.meterevents()
            ],
            [(0, 4, 4, 1), (3840000, 3, 4, 2)],
        )
        self.assertEqual(
            [
                (
                    track.index,
                    track.name,
                    track.reg.index,
                    track.reg.name,
                    track.reg.startpos,
                )
                for track in ptf.tracks()
            ],
            [
                (0, "Stereo Print", 0, "Print A.L", 0),
                (1, "Stereo Print", 1, "Print A.R", 0),
            ],
        )

    @unittest.skipUnless(
        AUDIO_STEREO_EMPTY.exists() and AUDIO_STEREO_CLIP.exists(),
        "audio reverse-engineering controls are not present",
    )
    def test_write_template_session_round_trips_audio_midi_and_maps(self):
        with TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "audio_midi_maps.ptx"
            write_template_session(
                AUDIO_STEREO_EMPTY,
                output,
                audio_files=(
                    AudioFileSpec(
                        filename="Audio 1-SigGen_04.wav",
                        length=264600,
                        channels=2,
                    ),
                ),
                audio_tracks=(
                    AudioTrackSpec(
                        name="Stereo Print",
                        channels=2,
                        clips=(
                            AudioClipSpec(
                                name="Print A",
                                file_index=0,
                                startpos=88200,
                                sampleoffset=88200,
                                length=88200,
                            ),
                        ),
                    ),
                ),
                audio_template=AUDIO_STEREO_CLIP,
                midi_tracks=(
                    MidiTrackSpec(
                        name="Tempo Guide",
                        clips=(
                            MidiClipSpec(
                                name="Guide Note",
                                startpos=0,
                                notes=(
                                    MidiEvent(pos=0, length=960000, note=60, velocity=96),
                                    MidiEvent(
                                        pos=3840000,
                                        length=480000,
                                        note=64,
                                        velocity=96,
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
                midi_template=TEMPLATE,
                tempo_events=(
                    TempoEvent(pos=0, bpm=120.0, ppq=960000),
                    TempoEvent(pos=3840000, bpm=140.0, ppq=960000),
                ),
                meter_events=(
                    MeterEvent(pos=0, numerator=4, denominator=4, ordinal=1),
                    MeterEvent(pos=3840000, numerator=3, denominator=4, ordinal=2),
                ),
            )

            ptf = PTFFormat()
            self.assertEqual(ptf.load(output, 44100), 0)

        self.assertEqual(
            [(track.index, track.name, track.reg.name, track.reg.startpos) for track in ptf.tracks()],
            [
                (0, "Stereo Print", "Print A.L", 88200),
                (1, "Stereo Print", "Print A.R", 88200),
            ],
        )
        self.assertEqual(
            [
                (
                    track.index,
                    track.name,
                    track.reg.name,
                    track.reg.startpos,
                    [(event.pos, event.length, event.note, event.velocity) for event in track.reg.midi],
                )
                for track in ptf.miditracks()
            ],
            [
                (
                    0,
                    "Tempo Guide",
                    "Guide Note",
                    0,
                    [(0, 960000, 60, 96), (3840000, 480000, 64, 96)],
                ),
            ],
        )
        self.assertEqual(
            [(event.pos, event.bpm, event.ppq) for event in ptf.tempoevents()],
            [(0, 120.0, 960000), (3840000, 140.0, 960000)],
        )
        self.assertEqual(
            [
                (event.pos, event.numerator, event.denominator, event.ordinal)
                for event in ptf.meterevents()
            ],
            [(0, 4, 4, 1), (3840000, 3, 4, 2)],
        )

    @unittest.skipUnless(
        AUDIO_FOUR_STEREO_EMPTY.exists() and AUDIO_STEREO_CLIP.exists(),
        "audio reverse-engineering controls are not present",
    )
    def test_write_audio_session_clones_missing_file_metadata_records(self):
        session = AudioSessionSpec(
            audio_files=tuple(
                AudioFileSpec(filename=f"Stem {idx}.wav", length=264600, channels=2)
                for idx in range(1, 5)
            ),
            tracks=tuple(
                AudioTrackSpec(
                    name=f"Stem {idx}",
                    channels=2,
                    clips=(
                        AudioClipSpec(
                            name=f"Stem {idx} Region",
                            file_index=idx - 1,
                            startpos=(idx - 1) * 44100,
                            sampleoffset=88200,
                            length=88200,
                        ),
                    ),
                )
                for idx in range(1, 5)
            ),
        )

        with TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "four_stereo_stems.ptx"
            write_audio_session(
                AUDIO_FOUR_STEREO_EMPTY,
                output,
                session,
                audio_template=AUDIO_STEREO_CLIP,
            )
            ptf = PTFFormat()
            self.assertEqual(ptf.load(output, 44100), 0)
            metadata_records = _audio_metadata_records(output)
            file_list_header, file_list_entries = _audio_file_list_entries(output)

        self.assertEqual(
            [(wav.index, wav.filename, wav.length) for wav in ptf.audiofiles()],
            [(idx, f"Stem {idx + 1}.wav", 264600) for idx in range(4)],
        )
        self.assertEqual(int.from_bytes(file_list_header[2:6], "little"), len(file_list_entries) + 1)
        self.assertEqual(int.from_bytes(file_list_header[7:11], "little"), len(file_list_entries))
        audio_entries = [
            (name, suffix)
            for name, suffix in file_list_entries
            if name.lower().endswith((".wav", ".wave", ".aif", ".aiff"))
        ]
        self.assertEqual(
            [suffix for _name, suffix in audio_entries],
            [b"EVAW\x02\x00\x00\x00\x00"] * 3 + [b"EVAW\x00\xff\xff\xff\xff"],
        )
        first_path_index = 1 + len(audio_entries)
        for entry_index, (_name, suffix) in enumerate(
            file_list_entries[first_path_index:],
            start=first_path_index,
        ):
            self.assertEqual(suffix[5:9], entry_index.to_bytes(4, "little"))
        private_ids = [
            record[
                _audio_file_private_id_offset(record) : _audio_file_private_id_offset(record)
                + 16
            ]
            for record in metadata_records
        ]
        self.assertEqual(len(set(private_ids)), 4)
        self.assertTrue(all((value[6] >> 4) == 4 for value in private_ids))
        self.assertTrue(all((value[8] & 0xC0) == 0x80 for value in private_ids))

    @unittest.skipUnless(
        AUDIO_TWO_STEREO_EMPTY.exists() and AUDIO_TWO_STEREO_DISTINCT.exists(),
        "audio reverse-engineering controls are not present",
    )
    def test_write_audio_session_round_trips_two_stereo_files_with_real_metadata(self):
        session = AudioSessionSpec(
            audio_files=(
                AudioFileSpec(filename="Stem 1.wav", length=264600, channels=2),
                AudioFileSpec(filename="Stem 2.wav", length=264600, channels=2),
            ),
            tracks=(
                AudioTrackSpec(
                    name="Stem 1",
                    channels=2,
                    clips=(
                        AudioClipSpec(
                            name="Stem 1 Region",
                            file_index=0,
                            startpos=88200,
                            sampleoffset=88200,
                            length=88200,
                        ),
                    ),
                ),
                AudioTrackSpec(
                    name="Stem 2",
                    channels=2,
                    clips=(
                        AudioClipSpec(
                            name="Stem 2 Region",
                            file_index=1,
                            startpos=176400,
                            sampleoffset=88200,
                            length=88200,
                        ),
                    ),
                ),
            ),
        )

        with TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "two_stereo_stems.ptx"
            write_audio_session(
                AUDIO_TWO_STEREO_EMPTY,
                output,
                session,
                audio_template=AUDIO_TWO_STEREO_DISTINCT,
            )
            ptf = PTFFormat()
            self.assertEqual(ptf.load(output, 44100), 0)

        self.assertEqual(
            [(wav.index, wav.filename, wav.length) for wav in ptf.audiofiles()],
            [(0, "Stem 1.wav", 264600), (1, "Stem 2.wav", 264600)],
        )
        self.assertEqual(
            [
                (track.index, track.name, track.reg.wave.index, track.reg.name, track.reg.startpos)
                for track in ptf.tracks()
            ],
            [
                (0, "Stem 1", 0, "Stem 1 Region.L", 88200),
                (1, "Stem 1", 0, "Stem 1 Region.R", 88200),
                (2, "Stem 2", 1, "Stem 2 Region.L", 176400),
                (3, "Stem 2", 1, "Stem 2 Region.R", 176400),
            ],
        )

    @unittest.skipUnless(
        AUDIO_TWO_STEREO_EMPTY.exists()
        and AUDIO_TWO_STEREO_DISTINCT.exists()
        and (AUDIO_FILES_DIR / "Stem 1.wav").exists()
        and (AUDIO_FILES_DIR / "Stem 2.wav").exists(),
        "audio reverse-engineering controls are not present",
    )
    def test_write_audio_session_patches_wav_identity_from_source_path(self):
        stem_1 = AUDIO_FILES_DIR / "Stem 1.wav"
        stem_2 = AUDIO_FILES_DIR / "Stem 2.wav"
        session = AudioSessionSpec(
            audio_files=(
                AudioFileSpec(
                    filename="Stem 1.wav",
                    length=264600,
                    channels=2,
                    source_path=stem_1,
                ),
                AudioFileSpec(
                    filename="Stem 2.wav",
                    length=264600,
                    channels=2,
                    source_path=stem_2,
                ),
            ),
            tracks=(
                AudioTrackSpec(
                    name="Stem 1",
                    channels=2,
                    clips=(
                        AudioClipSpec(
                            name="Stem 1 Region",
                            file_index=0,
                            startpos=88200,
                            sampleoffset=88200,
                            length=88200,
                        ),
                    ),
                ),
                AudioTrackSpec(
                    name="Stem 2",
                    channels=2,
                    clips=(
                        AudioClipSpec(
                            name="Stem 2 Region",
                            file_index=1,
                            startpos=176400,
                            sampleoffset=88200,
                            length=88200,
                        ),
                    ),
                ),
            ),
        )

        with TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "two_stereo_arbitrary_stems.ptx"
            write_audio_session(
                AUDIO_TWO_STEREO_EMPTY,
                output,
                session,
                audio_template=AUDIO_TWO_STEREO_DISTINCT,
            )
            ptf = PTFFormat()
            self.assertEqual(ptf.load(output, 44100), 0)
            metadata_records = _audio_metadata_records(output)

        for idx, (record, source_path, audio_file) in enumerate(
            zip(metadata_records, (stem_1, stem_2), session.audio_files)
        ):
            identity = _audio_file_identity(source_path)
            self.assertEqual(identity.mtime_filetime, identity.bext_origination_filetime)
            self.assertEqual(record[30:46], identity.sidecar_umid_prefix)
            child_offset = _audio_file_2106_content_offset(record)
            self.assertEqual(
                record[child_offset + 31 : child_offset + 39],
                identity.mtime_filetime,
            )
            self.assertEqual(
                record[child_offset + 102 : child_offset + 107],
                identity.bext_time_reference,
            )
            self.assertEqual(
                record[child_offset + 107 : child_offset + 115],
                identity.bext_origination_filetime,
            )
            bext_offset = _audio_file_bext_umid_offset(record)
            self.assertEqual(
                record[bext_offset : bext_offset + 24],
                identity.bext_umid_head[:24],
            )
            if identity.copy_full_bext_umid:
                self.assertEqual(
                    record[bext_offset + 24 : bext_offset + len(identity.bext_umid_head)],
                    identity.bext_umid_head[24:],
                )
            else:
                self.assertNotEqual(
                    record[bext_offset + 24 : bext_offset + len(identity.bext_umid_head)],
                    identity.bext_umid_head[24:],
                )
            private_id_offset = _audio_file_private_id_offset(record)
            self.assertEqual(
                record[private_id_offset : private_id_offset + 16],
                _synthetic_audio_file_private_id(audio_file, idx),
            )
        self.assertEqual(
            [(wav.index, wav.filename, wav.length) for wav in ptf.audiofiles()],
            [(0, "Stem 1.wav", 264600), (1, "Stem 2.wav", 264600)],
        )

    @unittest.skipUnless(
        (AUDIO_FILES_DIR / "Stem 1.wav").exists(),
        "audio reverse-engineering controls are not present",
    )
    def test_copy_audio_file_for_session_sets_bwf_origination_mtime(self):
        source = AUDIO_FILES_DIR / "Stem 1.wav"
        identity = _audio_file_identity(source)

        with TemporaryDirectory() as tempdir:
            staged = copy_audio_file_for_session(
                source,
                Path(tempdir) / "Audio Files",
                "Copied Stem.wav",
            )

            self.assertEqual(staged.name, "Copied Stem.wav")
            self.assertTrue(staged.exists())
            self.assertEqual(
                _windows_filetime_from_unix_ns(staged.stat().st_mtime_ns),
                identity.bext_origination_filetime,
            )
            self.assertEqual(
                _audio_file_identity(staged).mtime_filetime,
                identity.bext_origination_filetime,
            )

    @unittest.skipUnless(
        AUDIO_STEREO_EMPTY.exists()
        and AUDIO_STEREO_CLIP.exists()
        and (AUDIO_FILES_DIR / "Stem 1.wav").exists(),
        "audio reverse-engineering controls are not present",
    )
    def test_write_audio_session_infers_source_file_and_clip_length(self):
        source = AUDIO_FILES_DIR / "Stem 1.wav"
        self.assertEqual(_audio_file_length(AudioFileSpec(source.name, source_path=source)), 264600)

        session = AudioSessionSpec(
            audio_files=(
                AudioFileSpec(
                    filename=source.name,
                    channels=2,
                    source_path=source,
                ),
            ),
            tracks=(
                AudioTrackSpec(
                    name="Full Stem",
                    channels=2,
                    clips=(
                        AudioClipSpec(
                            name="Full Stem Region",
                            file_index=0,
                            startpos=0,
                            sampleoffset=0,
                        ),
                    ),
                ),
            ),
        )

        with TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "inferred_source_length.ptx"
            write_audio_session(
                AUDIO_STEREO_EMPTY,
                output,
                session,
                audio_template=AUDIO_STEREO_CLIP,
            )
            ptf = PTFFormat()
            self.assertEqual(ptf.load(output, 44100), 0)

        self.assertEqual(
            [(wav.index, wav.filename, wav.length) for wav in ptf.audiofiles()],
            [(0, "Stem 1.wav", 264600)],
        )
        self.assertEqual(
            [(region.name, region.sampleoffset, region.length) for region in ptf.regions()],
            [("Full Stem Region.L", 0, 264600), ("Full Stem Region.R", 0, 264600)],
        )
        self.assertEqual(
            [(region.name, region.startpos) for region in ptf.regions()],
            [("Full Stem Region.L", 158671800), ("Full Stem Region.R", 158671800)],
        )

    @unittest.skipUnless(
        AUDIO_FOUR_STEREO_EMPTY.exists() and AUDIO_FOUR_STEREO_DISTINCT.exists(),
        "audio reverse-engineering controls are not present",
    )
    def test_write_audio_session_round_trips_four_stereo_files_with_real_metadata(self):
        filenames = (
            "Audio 1-SigGen_05.wav",
            "Audio 1-SigGen_06.wav",
            "Audio 3-SigGen_01.wav",
            "Audio 4-SigGen_01.wav",
        )
        session = AudioSessionSpec(
            audio_files=tuple(
                AudioFileSpec(filename=filename, length=264600, channels=2)
                for filename in filenames
            ),
            tracks=tuple(
                AudioTrackSpec(
                    name=f"Stem {idx}",
                    channels=2,
                    clips=(
                        AudioClipSpec(
                            name=f"Stem {idx} Region",
                            file_index=idx - 1,
                            startpos=(idx - 1) * 44100,
                            sampleoffset=88200,
                            length=88200,
                        ),
                    ),
                )
                for idx in range(1, 5)
            ),
        )

        with TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "four_stereo_stems.ptx"
            write_audio_session(
                AUDIO_FOUR_STEREO_EMPTY,
                output,
                session,
                audio_template=AUDIO_FOUR_STEREO_DISTINCT,
            )
            ptf = PTFFormat()
            self.assertEqual(ptf.load(output, 44100), 0)

        self.assertEqual(
            [(wav.index, wav.filename, wav.length) for wav in ptf.audiofiles()],
            [(idx, filename, 264600) for idx, filename in enumerate(filenames)],
        )
        self.assertEqual(
            [
                (track.index, track.name, track.reg.wave.index, track.reg.name, track.reg.startpos)
                for track in ptf.tracks()
            ],
            [
                (0, "Stem 1", 0, "Stem 1 Region.L", 0),
                (1, "Stem 1", 0, "Stem 1 Region.R", 0),
                (2, "Stem 2", 1, "Stem 2 Region.L", 44100),
                (3, "Stem 2", 1, "Stem 2 Region.R", 44100),
                (4, "Stem 3", 2, "Stem 3 Region.L", 88200),
                (5, "Stem 3", 2, "Stem 3 Region.R", 88200),
                (6, "Stem 4", 3, "Stem 4 Region.L", 132300),
                (7, "Stem 4", 3, "Stem 4 Region.R", 132300),
            ],
        )


if __name__ == "__main__":
    unittest.main()
