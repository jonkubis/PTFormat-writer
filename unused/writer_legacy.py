"""Legacy template-assisted PTX writer implementation.

This module contains the original monolithic `ptxformatwriter.writer` implementation:
template-assisted block replacement, WAV metadata patching, and heuristic
final-index repair. It remains useful as a compatibility backend, but newer
session-construction work in this repo prefers the more explicit byte-synthesis
tools in `ptxformatwriter.body_synth` plus the deterministic index logic in
`ptxformatwriter.final_index`.
"""

from __future__ import annotations

import struct
import hashlib
import os
import shutil
import time
import tempfile
import wave
from datetime import datetime
from dataclasses import dataclass
from difflib import SequenceMatcher
from bisect import bisect_right
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .core import Block, MeterEvent, MidiEvent, PTFFormat, TempoEvent, ZERO_TICKS


PPQ_TICKS = 960000
DEFAULT_MIDI_REGION_TICKS = 4 * PPQ_TICKS
FINAL_INDEX_PATCH_OCCURRENCE_LIMIT = 64
_FINAL_INDEX_OFFSET_MARKER = b"\x01\x04\x00\x01\x00"
_FINAL_INDEX_UNMARKED_SKIP_TYPES = {0x0003, 0x2519}
_FINAL_INDEX_LARGE_UNMARKED_SKIP_TYPES = {0x2624}
_FINAL_INDEX_2519_CHILD_REF_TYPES = {0x251A, 0x251B, 0x251C, 0x2716}
_FINAL_INDEX_2624_END_REF_TYPES = {0x261B}


@dataclass(frozen=True)
class BlockRef:
    block: Block
    start: int
    end: int
    data: bytes


@dataclass(frozen=True)
class MidiClipSpec:
    """A MIDI clip placement to synthesize into a template session.

    `startpos` is the absolute clip start in Pro Tools ticks.  Note positions
    are clip-relative.
    """

    name: str = ""
    startpos: int = 0
    notes: Sequence[MidiEvent] = ()
    length: int | None = None


@dataclass(frozen=True)
class MidiTrackSpec:
    name: str = "MIDI 1"
    clips: Sequence[MidiClipSpec] = ()


@dataclass(frozen=True)
class MidiSessionSpec:
    tracks: Sequence[MidiTrackSpec]
    tempo_events: Sequence[TempoEvent] = ()
    meter_events: Sequence[MeterEvent] = ()


@dataclass(frozen=True)
class AudioFileSpec:
    """An audio file reference to write into a PT12 template session.

    `channels` is used to match clips to mono/stereo track scaffolds.  The
    opaque channel metadata still comes from the audio template.  When
    `source_path` is set, the writer patches known BWF/UMID identity fields in
    the template 0x1003 record so Pro Tools can relink to that WAV directly.
    When `length` is omitted, it is inferred from `source_path`.
    """

    filename: str
    length: int | None = None
    channels: int = 1
    source_path: str | Path | None = None


@dataclass(frozen=True)
class AudioClipSpec:
    """An audio clip placement in sample units.

    `startpos` is the absolute timeline start in samples.  `sampleoffset` and
    `length` are source-file sample positions.  `file_index` references
    `AudioSessionSpec.audio_files`.  When `length` is omitted, the clip uses the
    rest of the source file from `sampleoffset`.
    """

    name: str
    file_index: int = 0
    startpos: int = 0
    sampleoffset: int = 0
    length: int | None = None
    source_start: int | None = None


@dataclass(frozen=True)
class AudioTrackSpec:
    name: str = "Audio 1"
    clips: Sequence[AudioClipSpec] = ()
    channels: int = 1


@dataclass(frozen=True)
class AudioSessionSpec:
    audio_files: Sequence[AudioFileSpec]
    tracks: Sequence[AudioTrackSpec]
    tempo_events: Sequence[TempoEvent] = ()
    meter_events: Sequence[MeterEvent] = ()
    preserve_name_widths: bool = False


@dataclass(frozen=True)
class _MidiClipResolved:
    track_index: int
    track_name: str
    region_index: int
    name: str
    startpos: int
    notes: tuple[MidiEvent, ...]
    length: int


@dataclass(frozen=True)
class _MidiChunkTemplate:
    chunk: bytes
    event: bytes
    nlb_pos: int
    nlb_tail: bytes
    chunk_tail: bytes


@dataclass(frozen=True)
class _MidiRegionTemplate:
    region_entry_type: int
    region_info_type: int
    region_info_content: bytes


@dataclass(frozen=True)
class _MidiPlacementTemplate:
    track_type: int
    placement_type: int
    placement_entry_type: int
    track_tail: bytes
    placement_entry_content: bytes


@dataclass(frozen=True)
class _AudioClipResolved:
    track_index: int
    track_name: str
    region_index: int
    name: str
    file_index: int
    channel_index: int
    startpos: int
    sampleoffset: int
    length: int
    source_start: int


@dataclass(frozen=True)
class _AudioRegionTemplate:
    region_entry_type: int
    region_info_type: int
    region_info_content: bytes
    region_entry_tail: bytes


@dataclass(frozen=True)
class _AudioPlacementTemplate:
    active_type: int
    track_type: int
    placement_type: int
    placement_entry_type: int
    top_tail: bytes
    track_tail: bytes
    placement_tail: bytes
    placement_entry_content: bytes


@dataclass(frozen=True)
class _AudioFileIdentity:
    sidecar_umid_prefix: bytes
    bext_umid_head: bytes
    copy_full_bext_umid: bool
    mtime_filetime: bytes
    bext_time_reference: bytes
    bext_origination_filetime: bytes


_AUDIO_FILE_PRIVATE_ID_TRAILER = b"\x5a\x01\x00\x22\x00\x00\x00\x01\x43"


def encrypt_session_data(unxored: bytes) -> bytes:
    """Apply the Pro Tools session XOR pass to unencrypted session bytes."""

    out = bytearray(unxored)
    if len(out) < 0x14:
        raise ValueError("session is too short")

    xor_type = out[0x12]
    xor_value = out[0x13]
    if xor_type == 0x01:
        xor_delta = PTFFormat.gen_xor_delta(xor_value, 53, False)
    elif xor_type == 0x05:
        xor_delta = PTFFormat.gen_xor_delta(xor_value, 11, True)
    else:
        raise ValueError(f"unsupported XOR type {xor_type:#x}")

    xor_key = [(i * xor_delta) & 0xFF for i in range(256)]
    for i in range(0x14, len(out)):
        xor_index = (i & 0xFF) if xor_type == 0x01 else ((i >> 12) & 0xFF)
        out[i] ^= xor_key[xor_index]
    return bytes(out)


def load_unxored(path: str | Path) -> bytes:
    ptf = PTFFormat()
    if ptf.unxor(path) != 0:
        raise ValueError(f"cannot decrypt {path}")
    return ptf.unxored_data()


def parse_unxored(data: bytes) -> PTFFormat:
    ptf = PTFFormat()
    ptf._ptfunxored = data
    ptf._len = len(data)
    if ptf.parse_version():
        raise ValueError("cannot extract Pro Tools version")
    ptf.parseblocks()
    return ptf


def top_level_refs(data: bytes) -> list[BlockRef]:
    ptf = parse_unxored(data)
    return [
        BlockRef(
            block=block,
            start=block.offset - 7,
            end=block.offset + block.block_size,
            data=data[block.offset - 7 : block.offset + block.block_size],
        )
        for block in ptf.blocks
    ]


def make_block(block_type: int, content: bytes) -> bytes:
    """Build a block from payload bytes that already include content_type."""

    return (
        bytes([0x5A])
        + int(block_type).to_bytes(2, "little")
        + len(content).to_bytes(4, "little")
        + content
    )


def _full_block_bytes(data: bytes, block: Block) -> bytes:
    return data[block.offset - 7 : block.offset + block.block_size]

def _first_top_level(ptf: PTFFormat, content_type: int) -> Block:
    for block in ptf.blocks:
        if block.content_type == content_type:
            return block
    raise ValueError(f"template has no top-level block {content_type:#x}")


def _replace_top_level_blocks(
    data: bytes,
    replacements: Mapping[int, bytes],
    *,
    index_source_data: bytes | None = None,
    patch_unmarked_index_offsets: bool = True,
    robust_index: bool = False,
) -> bytes:
    refs = top_level_refs(data)
    out = bytearray()
    cursor = 0
    for ref in refs:
        out.extend(data[cursor : ref.start])
        out.extend(replacements.get(ref.block.content_type, ref.data))
        cursor = ref.end
    out.extend(data[cursor:])
    source = index_source_data if index_source_data is not None else data
    if robust_index:
        # Deterministic final-index repair (final_index.reindex_after_resize): capture
        # holes from the consistent `source`, refill by (content_type, rank) in the new
        # layout. Replaces the legacy _update_final_index offset-guesser, which corrupts
        # records like 0x2587 at some configs (Pro Tools EOS / magic-ID). Deferred import
        # avoids the final_index <-> writer import cycle.
        from . import final_index as _FI

        return _FI.reindex_after_resize(source, bytes(out))
    return _update_final_index(
        _update_final_block_marker(bytes(out)),
        source,
        patch_unmarked_offsets=patch_unmarked_index_offsets,
    )


def _flatten_block_starts(data: bytes) -> list[tuple[int, int]]:
    return [(start, content_type) for start, _end, content_type in _flatten_block_bounds(data)]


def _flatten_block_bounds(data: bytes) -> list[tuple[int, int, int]]:
    ptf = parse_unxored(data)
    bounds: list[tuple[int, int, int]] = []

    def visit(block: Block) -> None:
        bounds.append((block.offset - 7, block.offset + block.block_size, block.content_type))
        for child in sorted(block.child, key=lambda item: item.offset):
            visit(child)

    for block in sorted(ptf.blocks, key=lambda item: item.offset):
        visit(block)
    return bounds


def _build_offset_maps(
    old_data: bytes,
    new_data: bytes,
) -> tuple[dict[int, int], dict[int, int]]:
    old_bounds = _flatten_block_bounds(old_data)
    new_bounds = _flatten_block_bounds(new_data)
    start_map: dict[int, int] = {}
    end_map: dict[int, int] = {}

    old_types = [content_type for _start, _end, content_type in old_bounds]
    new_types = [content_type for _start, _end, content_type in new_bounds]
    matcher = SequenceMatcher(a=old_types, b=new_types, autojunk=False)
    for tag, old_a, old_b, new_a, new_b in matcher.get_opcodes():
        if tag == "equal":
            old_span = old_bounds[old_a:old_b]
            new_span = new_bounds[new_a:new_b]
        elif old_b - old_a == new_b - new_a:
            old_span = old_bounds[old_a:old_b]
            new_span = new_bounds[new_a:new_b]
        else:
            continue

        for (
            old_start,
            old_end,
            old_type,
        ), (
            new_start,
            new_end,
            new_type,
        ) in zip(old_span, new_span):
            if old_type == new_type:
                start_map[old_start] = new_start
                end_map[old_end] = new_end

    old_top = top_level_refs(old_data)
    new_top = top_level_refs(new_data)
    for old_ref, new_ref in zip(old_top, new_top):
        if old_ref.block.content_type == new_ref.block.content_type:
            start_map[old_ref.start] = new_ref.start
            end_map[old_ref.end] = new_ref.end
    return start_map, end_map


def _shifted_unchanged_top_level_intervals(
    old_data: bytes,
    new_data: bytes,
) -> list[tuple[int, int, int]]:
    intervals: list[tuple[int, int, int]] = []
    for old_ref, new_ref in zip(top_level_refs(old_data), top_level_refs(new_data)):
        if (
            old_ref.block.content_type == new_ref.block.content_type
            and old_ref.data == new_ref.data
            and old_ref.start != new_ref.start
        ):
            intervals.append((old_ref.start, old_ref.end, new_ref.start - old_ref.start))
    return intervals


def _final_index_records(
    final_block: bytes,
    known_content_types: set[int],
) -> list[tuple[int, int, int]]:
    """Return `(start, end, content_type)` records in a final 0x0002 index."""

    if len(final_block) < 13 or final_block[0] != 0x5A:
        return []
    if int.from_bytes(final_block[1:3], "little") != 0x0002:
        return []
    if int.from_bytes(final_block[7:9], "little") != 0x0002:
        return []

    expected_count = int.from_bytes(final_block[9:13], "little")
    starts: list[tuple[int, int]] = []
    for pos in range(13, max(len(final_block) - 10, 13)):
        content_type = int.from_bytes(final_block[pos + 4 : pos + 6], "little")
        if content_type not in known_content_types:
            continue
        has_full_marker = final_block[pos + 6 : pos + 10] == b"\xff\xff\xff\xff"
        has_embedded_offset_marker = (
            final_block[pos + 6 : pos + 9] == b"\xff\xff\xff"
            and final_block[pos + 9] != 0xFF
        )
        if not has_full_marker and not has_embedded_offset_marker:
            continue
        record_count = int.from_bytes(final_block[pos : pos + 4], "little")
        if record_count > 5000:
            continue
        starts.append((pos, content_type))

    if len(starts) != expected_count and len(starts) != expected_count - 1:
        return []
    return [
        (
            start,
            starts[index + 1][0] if index + 1 < len(starts) else len(final_block),
            content_type,
        )
        for index, (start, content_type) in enumerate(starts)
    ]


def _update_final_index(
    data: bytes,
    old_data: bytes,
    *,
    patch_unmarked_offsets: bool = True,
) -> bytes:
    """Patch absolute block offsets in the final 0x0002 index block."""

    refs = top_level_refs(data)
    if not refs or refs[-1].block.content_type != 0x0002:
        return data
    old_refs = top_level_refs(old_data)
    if not old_refs or old_refs[-1].block.content_type != 0x0002:
        return data

    offset_map, end_offset_map = _build_offset_maps(old_data, data)
    shifted_top_level_intervals = _shifted_unchanged_top_level_intervals(old_data, data)
    shifted_top_level_starts = [item[0] for item in shifted_top_level_intervals]
    out = bytearray(data)
    final = refs[-1]
    start = final.start
    end = final.end
    source_final = old_refs[-1].data
    if len(source_final) != end - start:
        source_final = bytes(out[start:end])

    occurrence_counts: dict[int, int] = {}
    end_occurrence_counts: dict[int, int] = {}
    pos = 0
    while pos + 4 <= len(source_final):
        old_value = int.from_bytes(source_final[pos : pos + 4], "little")
        if old_value in offset_map:
            occurrence_counts[old_value] = occurrence_counts.get(old_value, 0) + 1
        if old_value in end_offset_map:
            end_occurrence_counts[old_value] = end_occurrence_counts.get(old_value, 0) + 1
        pos += 1

    final_out = bytearray(out[start:end])
    for pos in range(0, max(len(source_final) - len(_FINAL_INDEX_OFFSET_MARKER) - 4, 0)):
        if source_final[pos : pos + len(_FINAL_INDEX_OFFSET_MARKER)] != _FINAL_INDEX_OFFSET_MARKER:
            continue
        value_pos = pos + len(_FINAL_INDEX_OFFSET_MARKER)
        old_value = int.from_bytes(source_final[value_pos : value_pos + 4], "little")
        new_value = offset_map.get(old_value)
        if new_value is not None:
            final_out[value_pos : value_pos + 4] = int(new_value).to_bytes(4, "little")

    if not patch_unmarked_offsets:
        out[start:end] = final_out
        return bytes(out)

    known_content_types = {content_type for _offset, content_type in _flatten_block_starts(old_data)}
    records = _final_index_records(source_final, known_content_types)
    record_starts = [record[0] for record in records]
    record_type_counts: dict[int, int] = {}
    for _record_start, _record_end, content_type in records:
        record_type_counts[content_type] = record_type_counts.get(content_type, 0) + 1
    skip_unmarked_types = {
        content_type
        for content_type in _FINAL_INDEX_LARGE_UNMARKED_SKIP_TYPES
        if record_type_counts.get(content_type, 0) > FINAL_INDEX_PATCH_OCCURRENCE_LIMIT
    }

    def record_at(pos: int) -> tuple[int, int, int] | None:
        if not records:
            return None
        index = bisect_right(record_starts, pos) - 1
        if index < 0:
            return None
        record_start, record_end, content_type = records[index]
        return (record_start, record_end, content_type) if record_start <= pos < record_end else None

    def is_patchable_2519_child_ref(pos: int) -> bool:
        if pos < 2:
            return False
        child_type = int.from_bytes(source_final[pos - 2 : pos], "little")
        return child_type in _FINAL_INDEX_2519_CHILD_REF_TYPES

    def is_patchable_2519_offset_table_ref(
        pos: int,
        record_start: int,
        record_end: int,
    ) -> bool:
        max_index = min((pos - record_start) // 4, 512)
        for index in range(max_index + 1):
            table_header = pos - 4 - (4 * index)
            if table_header < record_start or table_header + 4 > record_end:
                continue
            table_type = int.from_bytes(source_final[table_header : table_header + 2], "little")
            count = int.from_bytes(source_final[table_header + 2 : table_header + 4], "little")
            if table_type != 0x0034 or not (1 <= count <= 512) or index >= count:
                continue
            values_start = table_header + 4
            values_end = values_start + (4 * count)
            if values_end > record_end:
                continue
            values = [
                int.from_bytes(source_final[offset : offset + 4], "little")
                for offset in range(values_start, values_end, 4)
            ]
            if all(value in offset_map for value in values):
                return True
        return False

    def is_patchable_2519_record_end_ref(pos: int, record_start: int) -> bool:
        return pos == record_start + 10

    def is_patchable_2624_child_end_ref(pos: int) -> bool:
        if pos < 2:
            return False
        child_type = int.from_bytes(source_final[pos - 2 : pos], "little")
        return child_type in _FINAL_INDEX_2624_END_REF_TYPES

    def is_patchable_end_ref(pos: int, record: tuple[int, int, int] | None) -> bool:
        if record is None:
            return False
        record_start, _record_end, record_type = record
        if record_type == 0x2519:
            return is_patchable_2519_record_end_ref(pos, record_start)
        if record_type == 0x2624:
            return pos == record_start + 10 or is_patchable_2624_child_end_ref(pos)
        return False

    def shifted_unchanged_top_level_offset(old_value: int) -> int | None:
        index = bisect_right(shifted_top_level_starts, old_value) - 1
        if index < 0:
            return None
        interval_start, interval_end, delta = shifted_top_level_intervals[index]
        if interval_start <= old_value < interval_end:
            return old_value + delta
        return None

    def is_patchable_embedded_record_offset(
        pos: int,
        record: tuple[int, int, int] | None,
    ) -> bool:
        if record is None:
            return False
        record_start, _record_end, record_type = record
        return (
            record_type == 0x2519
            and pos == record_start + 9
            and source_final[record_start + 6 : record_start + 9] == b"\xff\xff\xff"
            and source_final[record_start + 9] != 0xFF
        )

    for pos in range(0, max(len(source_final) - 3, 0)):
        if (
            pos >= len(_FINAL_INDEX_OFFSET_MARKER)
            and source_final[pos - len(_FINAL_INDEX_OFFSET_MARKER) : pos]
            == _FINAL_INDEX_OFFSET_MARKER
        ):
            continue
        old_value = int.from_bytes(source_final[pos : pos + 4], "little")
        new_value = offset_map.get(old_value)
        record = record_at(pos)
        if new_value is not None:
            if occurrence_counts.get(old_value, 0) > FINAL_INDEX_PATCH_OCCURRENCE_LIMIT:
                continue
            record_type = record[2] if record is not None else None
            if record_type in _FINAL_INDEX_UNMARKED_SKIP_TYPES:
                if record_type != 0x2519 or not (
                    is_patchable_2519_child_ref(pos)
                    or (
                        record is not None
                        and is_patchable_2519_offset_table_ref(pos, record[0], record[1])
                    )
                ):
                    continue
            if record_type in skip_unmarked_types:
                continue
            final_out[pos : pos + 4] = int(new_value).to_bytes(4, "little")
            continue

        new_end_value = end_offset_map.get(old_value)
        if new_end_value is None:
            shifted_value = shifted_unchanged_top_level_offset(old_value)
            if shifted_value is None or not is_patchable_embedded_record_offset(pos, record):
                continue
            final_out[pos : pos + 4] = int(shifted_value).to_bytes(4, "little")
            continue
        if end_occurrence_counts.get(old_value, 0) <= FINAL_INDEX_PATCH_OCCURRENCE_LIMIT:
            if is_patchable_end_ref(pos, record):
                final_out[pos : pos + 4] = int(new_end_value).to_bytes(4, "little")

    out[start:end] = final_out
    return bytes(out)


def _update_final_block_marker(data: bytes) -> bytes:
    refs = top_level_refs(data)
    if not refs:
        return data
    out = bytearray(data)
    final_start = refs[-1].start
    first = refs[0]
    if first.start == 0x14 and first.block.block_size == 4:
        out[first.block.offset : first.block.offset + 2] = (final_start & 0xFFFF).to_bytes(
            2, "little"
        )
    return bytes(out)


def _latin1_string(value: str) -> bytes:
    encoded = value.encode("latin-1")
    return len(encoded).to_bytes(4, "little") + encoded


def _fixed_width_latin1_string(value: str, width: int, label: str) -> bytes:
    encoded = value.encode("latin-1")
    if len(encoded) > width:
        raise ValueError(f"{label} {value!r} is longer than template slot width {width}")
    return width.to_bytes(4, "little") + encoded + (b" " * (width - len(encoded)))


def _fixed_width_audio_clip_string(value: str, width: int) -> bytes:
    suffix = ""
    for candidate in (".L", ".R"):
        if value.endswith(candidate):
            suffix = candidate
            break
    if suffix:
        suffix_len = len(suffix.encode("latin-1"))
        base_width = width - suffix_len
        if base_width < 0:
            raise ValueError(f"audio clip name slot is too short for channel suffix: {width}")
        base = value[: -len(suffix)]
        return (
            width.to_bytes(4, "little")
            + _fixed_width_latin1_string(base, base_width, "audio clip name")[4:]
            + suffix.encode("latin-1")
        )
    return _fixed_width_latin1_string(value, width, "audio clip name")


def _read_latin1_string(data: bytes, pos: int) -> str:
    return _read_latin1_string_with_end(data, pos)[0]


def _read_latin1_string_with_end(data: bytes, pos: int) -> tuple[str, int]:
    if pos + 4 > len(data):
        return "", pos
    length = int.from_bytes(data[pos : pos + 4], "little")
    start = pos + 4
    end = start + length
    if length < 0 or end > len(data):
        return "", pos
    return data[start:end].decode("latin-1"), end


def _block_type_from(data: bytes, seed_data: bytes, content_type: int) -> int:
    for source in (data, seed_data):
        try:
            return _first_top_level(parse_unxored(source), content_type).block_type
        except ValueError:
            continue
    raise ValueError(f"no block template for {content_type:#x}")


def _varint_len(value: int) -> int:
    if value < 0:
        raise ValueError("negative MIDI tick values are not supported")
    for nbytes in range(1, 6):
        if value < (1 << (nbytes * 8)):
            return nbytes
    raise ValueError(f"MIDI tick value is too large: {value}")


def _three_point_end(content: bytes, pos: int) -> int:
    if pos + 5 > len(content):
        raise ValueError("three-point value is outside the template region")
    offset_n = (content[pos + 1] & 0xF0) >> 4
    length_n = (content[pos + 2] & 0xF0) >> 4
    start_n = (content[pos + 3] & 0xF0) >> 4
    return pos + 5 + offset_n + length_n + start_n


def _three_point_start_len(content: bytes, pos: int) -> int:
    if pos + 4 > len(content):
        raise ValueError("three-point value is outside the template region")
    return (content[pos + 3] & 0xF0) >> 4


def _encode_midi_region_points(startpos: int, length: int) -> bytes:
    length_n = _varint_len(length)
    start_n = _varint_len(startpos) if startpos else 0
    out = bytearray()
    out.extend(
        bytes(
            [
                0x00,
                (5 << 4) | 0x01,
                length_n << 4,
                (start_n << 4) | (start_n if start_n else 0),
                0x08,
            ]
        )
    )
    out.extend(int(ZERO_TICKS).to_bytes(5, "little"))
    out.extend(int(length).to_bytes(length_n, "little"))
    if start_n:
        out.extend(int(startpos).to_bytes(start_n, "little"))
    return bytes(out)


def _encode_audio_region_points(sampleoffset: int, length: int, source_start: int) -> bytes:
    offset_n = _varint_len(sampleoffset)
    length_n = _varint_len(length)
    source_start_n = _varint_len(source_start)
    out = bytearray()
    out.extend(
        bytes(
            [
                0x00,
                offset_n << 4,
                length_n << 4,
                (source_start_n << 4) | source_start_n,
                0x08,
            ]
        )
    )
    out.extend(int(sampleoffset).to_bytes(offset_n, "little"))
    out.extend(int(length).to_bytes(length_n, "little"))
    out.extend(int(source_start).to_bytes(source_start_n, "little"))
    return bytes(out)


def _looks_audio_filename(name: str) -> bool:
    lower = name.lower()
    return lower.endswith((".wav", ".wave", ".aif", ".aiff"))


def _wav_frame_count(path: str | Path) -> int:
    with wave.open(str(path), "rb") as wav:
        return int(wav.getnframes())


def _audio_file_length(audio_file: AudioFileSpec) -> int:
    length = None if audio_file.length is None else int(audio_file.length)
    if audio_file.source_path is None:
        if length is None:
            raise ValueError("audio file length is required when source_path is not set")
        if length <= 0:
            raise ValueError("audio file length must be positive")
        return length

    source_length = _wav_frame_count(audio_file.source_path)
    if source_length <= 0:
        raise ValueError(f"audio source has no frames: {audio_file.source_path}")
    if length is not None and length != source_length:
        raise ValueError(
            f"audio file length {length} does not match source_path frame count "
            f"{source_length}: {audio_file.source_path}"
        )
    return source_length


def _channel_suffix(channels: int, channel_index: int) -> str:
    if channels == 1:
        return ""
    if channels == 2:
        return (".L", ".R")[channel_index]
    raise NotImplementedError("structured audio writing currently supports mono and stereo tracks")


def _audio_track_channels(track: AudioTrackSpec) -> int:
    channels = int(track.channels)
    if channels < 1:
        raise ValueError("audio track channels must be positive")
    if channels > 2:
        raise NotImplementedError("structured audio writing currently supports mono and stereo tracks")
    return channels


def _audio_lane_names(tracks: Sequence[AudioTrackSpec]) -> list[str]:
    lanes: list[str] = []
    for track in tracks:
        lanes.extend([track.name] * _audio_track_channels(track))
    return lanes


def _resolved_audio_clips(
    audio_files: Sequence[AudioFileSpec],
    tracks: Sequence[AudioTrackSpec],
    seed_data: bytes,
) -> list[_AudioClipResolved]:
    if not audio_files:
        raise ValueError("audio writing needs at least one audio file")
    for audio_file in audio_files:
        if int(audio_file.channels) < 1:
            raise ValueError("audio file channels must be positive")
    audio_file_lengths = [_audio_file_length(audio_file) for audio_file in audio_files]
    origins = _audio_file_origins(seed_data)
    default_origin = next(iter(origins.values()), 0)

    clips: list[_AudioClipResolved] = []
    lane_base = 0
    for track in tracks:
        channels = _audio_track_channels(track)
        for clip_index, clip in enumerate(track.clips):
            if clip.file_index < 0 or clip.file_index >= len(audio_files):
                raise ValueError(f"audio clip references missing file index {clip.file_index}")
            file_channels = int(audio_files[clip.file_index].channels)
            if file_channels != channels:
                raise NotImplementedError(
                    "audio clip file channels must match the destination track channels"
                )
            sampleoffset = int(clip.sampleoffset)
            if sampleoffset < 0:
                raise ValueError("audio clip sampleoffset must be non-negative")
            file_length = audio_file_lengths[clip.file_index]
            if sampleoffset > file_length:
                raise ValueError("audio clip sampleoffset exceeds source file length")
            if clip.length is None:
                length = file_length - sampleoffset
            else:
                length = int(clip.length)
            if length <= 0:
                raise ValueError("audio clip length must be positive")
            if sampleoffset + length > file_length:
                raise ValueError("audio clip range exceeds source file length")
            startpos = int(clip.startpos)
            if startpos < 0:
                raise ValueError("audio clip startpos must be non-negative")
            source_start = clip.source_start
            if source_start is None:
                source_start = origins.get(clip.file_index, default_origin) + sampleoffset
            base_name = clip.name or f"{track.name}-{clip_index + 1:02d}"
            for channel_index in range(channels):
                clips.append(
                    _AudioClipResolved(
                        track_index=lane_base + channel_index,
                        track_name=track.name,
                        region_index=len(clips),
                        name=f"{base_name}{_channel_suffix(channels, channel_index)}",
                        file_index=int(clip.file_index),
                        channel_index=channel_index,
                        startpos=startpos,
                        sampleoffset=sampleoffset,
                        length=length,
                        source_start=int(source_start),
                    )
                )
        lane_base += channels
    return clips


def _audio_file_origins(seed_data: bytes) -> dict[int, int]:
    try:
        regions = _first_top_level(parse_unxored(seed_data), 0x262A)
    except ValueError:
        return {}

    origins: dict[int, int] = {}
    for region in regions.child:
        if region.content_type != 0x2629:
            continue
        region_info = next((child for child in region.child if child.content_type == 0x2628), None)
        if region_info is None:
            continue
        info = seed_data[region_info.offset : region_info.offset + region_info.block_size]
        if len(info) < 6 or info[:2] != b"\x28\x26":
            continue
        name_len = int.from_bytes(info[2:6], "little")
        points = 6 + name_len
        if points + 5 > len(info):
            continue
        offset_n = (info[points + 1] & 0xF0) >> 4
        length_n = (info[points + 2] & 0xF0) >> 4
        start_n = (info[points + 3] & 0xF0) >> 4
        sampleoffset_pos = points + 5
        source_start_pos = sampleoffset_pos + offset_n + length_n
        if source_start_pos + start_n > len(info):
            continue
        sampleoffset = int.from_bytes(info[sampleoffset_pos : sampleoffset_pos + offset_n], "little")
        source_start = int.from_bytes(info[source_start_pos : source_start_pos + start_n], "little")
        tail_pos = region_info.offset + region_info.block_size
        if tail_pos + 4 > len(seed_data):
            continue
        file_index = int.from_bytes(seed_data[tail_pos : tail_pos + 4], "little")
        origins.setdefault(file_index, source_start - sampleoffset)
    return origins


def _audio_file_list_content(content: bytes, audio_files: Sequence[AudioFileSpec]) -> bytes:
    entries: list[tuple[str, bytes]] = []
    pos = 11
    while pos + 13 <= len(content):
        name_len = int.from_bytes(content[pos : pos + 4], "little")
        name_start = pos + 4
        name_end = name_start + name_len
        entry_end = name_end + 9
        if name_len < 0 or entry_end > len(content):
            break
        name = content[name_start:name_end].decode("latin-1")
        entries.append((name, bytes(content[name_end:entry_end])))
        pos = entry_end

    audio_entry_indices = [
        idx for idx, (name, _suffix) in enumerate(entries) if _looks_audio_filename(name)
    ]
    if not audio_entry_indices:
        raise ValueError(
            "audio template must contain at least one audio file list entry "
            "to synthesize audio file references"
        )

    first_audio = audio_entry_indices[0]
    last_audio = audio_entry_indices[-1]
    before_entries = entries[:first_audio]
    after_entries = entries[last_audio + 1 :]
    audio_type = entries[first_audio][1][:4]
    parsed_end = pos

    rebuilt_entries: list[tuple[str, bytes]] = []
    rebuilt_entries.extend(before_entries)
    for idx, audio_file in enumerate(audio_files):
        if idx == len(audio_files) - 1:
            suffix = audio_type + b"\x00\xff\xff\xff\xff"
        else:
            suffix = audio_type + b"\x02\x00\x00\x00\x00"
        rebuilt_entries.append((audio_file.filename, suffix))

    for entry_index, (name, suffix) in enumerate(
        after_entries,
        start=len(rebuilt_entries),
    ):
        if len(suffix) == 9:
            suffix = suffix[:5] + int(entry_index).to_bytes(4, "little")
        rebuilt_entries.append((name, suffix))

    out = bytearray(content[:11])
    if len(out) >= 11:
        out[2:6] = int(len(rebuilt_entries) + 1).to_bytes(4, "little")
        out[7:11] = int(len(rebuilt_entries)).to_bytes(4, "little")
    for name, suffix in rebuilt_entries:
        out.extend(_latin1_string(name))
        out.extend(suffix)
    out.extend(content[parsed_end:])
    return bytes(out)


def _riff_wave_chunks(path: str | Path) -> dict[bytes, list[bytes]]:
    data = Path(path).read_bytes()
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError(f"audio source is not a RIFF/WAVE file: {path}")

    chunks: dict[bytes, list[bytes]] = {}
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        chunk_size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        chunk_start = pos + 8
        chunk_end = chunk_start + chunk_size
        if chunk_end > len(data):
            break
        chunks.setdefault(chunk_id, []).append(data[chunk_start:chunk_end])
        pos = chunk_end + (chunk_size & 1)
    return chunks


def _windows_filetime_from_unix_ns(unix_ns: int) -> bytes:
    return (116444736000000000 + int(unix_ns) // 100).to_bytes(8, "little")


def _unix_ns_from_windows_filetime(filetime: bytes) -> int:
    value = int.from_bytes(filetime, "little") - 116444736000000000
    return max(0, value * 100)


def _bext_origination_filetime(bext: bytes, fallback: bytes) -> bytes:
    if len(bext) < 338:
        return fallback
    date_text = bext[320:330].decode("ascii", errors="ignore")
    time_text = bext[330:338].decode("ascii", errors="ignore")
    try:
        origin = datetime.strptime(f"{date_text} {time_text}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return fallback
    unix_seconds = time.mktime(origin.timetuple())
    return _windows_filetime_from_unix_ns(int(unix_seconds * 1_000_000_000))


def _looks_pro_tools_bext_umid_tail(bext_umid_head: bytes) -> bool:
    tail = bext_umid_head[24:36]
    return len(tail) == 12 and tail[2:] == b"\x29\x31\x18\x14\xfc\xa4\x00\x00\x00\x00"


def _audio_file_identity(path: str | Path) -> _AudioFileIdentity:
    path = Path(path)
    chunks = _riff_wave_chunks(path)
    bext = next(iter(chunks.get(b"bext", ())), None)
    if bext is None or len(bext) < 412:
        raise NotImplementedError(
            f"audio source has no BWF bext UMID identity metadata: {path}"
        )

    bext_umid_head = bext[348:384]
    if not bext_umid_head.strip(b"\0"):
        raise NotImplementedError(f"audio source has an empty BWF UMID: {path}")

    sidecar_umid = next(iter(chunks.get(b"umid", ())), b"")
    if len(sidecar_umid) >= 16:
        sidecar_umid_prefix = sidecar_umid[:16]
    else:
        # Pro Tools WAVs carry a separate 24-byte `umid` chunk.  When a BWF file
        # has only a bext UMID, synthesize the observed prefix shape from the
        # BWF UMID's material-package identifier bytes.
        sidecar_umid_prefix = (b"\0" * 7) + b"\x2a" + bext_umid_head[16:24]

    filesystem_mtime_filetime = _windows_filetime_from_unix_ns(path.stat().st_mtime_ns)
    origination_filetime = _bext_origination_filetime(bext, filesystem_mtime_filetime)

    return _AudioFileIdentity(
        sidecar_umid_prefix=sidecar_umid_prefix,
        bext_umid_head=bext_umid_head,
        copy_full_bext_umid=_looks_pro_tools_bext_umid_tail(bext_umid_head),
        mtime_filetime=origination_filetime,
        bext_time_reference=bext[338:346][:5],
        bext_origination_filetime=origination_filetime,
    )


def copy_audio_file_for_session(
    source_path: str | Path,
    audio_files_dir: str | Path,
    filename: str | None = None,
) -> Path:
    """Copy a BWF WAV into a session Audio Files folder with PT-safe mtime.

    The structured writer uses the WAV BWF origination time in the PTX file
    metadata.  Pro Tools also checks the on-disk file modified time during link
    resolution, so the staged copy must use the same timestamp.
    """

    source = Path(source_path)
    destination_dir = Path(audio_files_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / (filename or source.name)
    identity = _audio_file_identity(source)
    if source.resolve() != destination.resolve():
        shutil.copy2(source, destination)
    mtime_ns = _unix_ns_from_windows_filetime(identity.bext_origination_filetime)
    os.utime(destination, ns=(mtime_ns, mtime_ns))
    return destination


def _audio_file_2106_content_offset(content: bytes) -> int:
    for pos in range(0, max(len(content) - 8, 0)):
        if content[pos] == 0x5A and content[pos + 7 : pos + 9] == b"\x06\x21":
            return pos + 7
    raise ValueError("audio file metadata template has no 0x2106 child")


def _audio_file_bext_umid_offset(content: bytes) -> int:
    offset = content.find(b"\x06\x0a\x2b\x34\x01\x01")
    if offset < 0:
        offset = content.find(b"\x06\x0a\x2b\x34")
    if offset < 0:
        raise ValueError("audio file metadata template has no BWF UMID marker")
    return offset


def _audio_file_private_id_offset(content: bytes) -> int:
    trailer = content.find(_AUDIO_FILE_PRIVATE_ID_TRAILER)
    if trailer < 16:
        raise ValueError("audio file metadata template has no private file id trailer")
    return trailer - 16


def _synthetic_audio_file_private_id(audio_file: AudioFileSpec, audio_file_index: int) -> bytes:
    source = "" if audio_file.source_path is None else str(audio_file.source_path)
    length = _audio_file_length(audio_file)
    seed = (
        f"ptxformatwriter-audio-file-id\0{audio_file_index}\0{audio_file.filename}\0"
        f"{length}\0{audio_file.channels}\0{source}"
    ).encode("utf-8", errors="surrogateescape")
    value = bytearray(hashlib.sha256(seed).digest()[:16])
    value[6] = (value[6] & 0x0F) | 0x40
    value[8] = (value[8] & 0x3F) | 0x80
    return bytes(value)


def _patch_audio_file_identity(content: bytearray, identity: _AudioFileIdentity) -> None:
    if len(content) < 46:
        raise ValueError("audio file metadata template is too short for BWF identity")
    content[30:46] = identity.sidecar_umid_prefix
    child_offset = _audio_file_2106_content_offset(content)
    content[child_offset + 31 : child_offset + 39] = identity.mtime_filetime
    content[child_offset + 102 : child_offset + 107] = identity.bext_time_reference
    content[child_offset + 107 : child_offset + 115] = identity.bext_origination_filetime
    bext_offset = _audio_file_bext_umid_offset(content)
    bext_length = 36 if identity.copy_full_bext_umid else 24
    bext_end = bext_offset + bext_length
    if bext_end > len(content):
        raise ValueError("audio file metadata template has truncated BWF identity")
    content[bext_offset:bext_end] = identity.bext_umid_head[:bext_length]


def _audio_file_metadata_content(
    data: bytes,
    block: Block,
    audio_file: AudioFileSpec,
    *,
    audio_file_index: int | None = None,
    refresh_private_id: bool = False,
) -> bytes:
    content = bytearray(data[block.offset : block.offset + block.block_size])
    if audio_file_index is not None and len(content) >= 6:
        content[2:6] = int(audio_file_index + 1).to_bytes(4, "little")
    if audio_file.source_path is not None:
        _patch_audio_file_identity(content, _audio_file_identity(audio_file.source_path))
    if refresh_private_id or audio_file.source_path is not None:
        if audio_file_index is None:
            raise ValueError("audio file index is required to refresh private ids")
        private_id_offset = _audio_file_private_id_offset(content)
        content[private_id_offset : private_id_offset + 16] = (
            _synthetic_audio_file_private_id(audio_file, audio_file_index)
        )
    length_written = False
    for child in block.child:
        if child.content_type != 0x1001:
            continue
        local = child.offset - block.offset + 8
        if local + 8 > len(content):
            raise ValueError("audio file metadata template is invalid")
        content[local : local + 8] = _audio_file_length(audio_file).to_bytes(8, "little")
        length_written = True
    if not length_written:
        raise ValueError("audio file metadata template has no 0x1001 length child")
    return bytes(content)


def _audio_files_content(seed_data: bytes, audio_files: Sequence[AudioFileSpec]) -> bytes:
    table = _first_top_level(parse_unxored(seed_data), 0x1004)
    original = bytearray(seed_data[table.offset : table.offset + table.block_size])
    if len(original) < 6 or original[:2] != b"\x04\x10":
        raise ValueError("audio file table template is invalid")
    original[2:6] = len(audio_files).to_bytes(4, "little")

    file_list_done = False
    metadata_index = 0
    metadata_children = [child for child in table.child if child.content_type == 0x1003]
    if not metadata_children and audio_files:
        raise NotImplementedError(
            "audio_template must contain at least one real 0x1003 audio file "
            "metadata record to synthesize audio file references"
        )
    last_metadata_child = metadata_children[-1] if metadata_children else None

    content = bytearray()
    cursor = 0
    for child in sorted(table.child, key=lambda item: item.offset):
        child_start = child.offset - 7 - table.offset
        child_end = child.offset + child.block_size - table.offset
        content.extend(original[cursor:child_start])
        if child.content_type == 0x103A and not file_list_done:
            child_content = _audio_file_list_content(
                seed_data[child.offset : child.offset + child.block_size],
                audio_files,
            )
            content.extend(make_block(child.block_type, child_content))
            file_list_done = True
        elif child.content_type == 0x1003 and metadata_index < len(audio_files):
            child_content = _audio_file_metadata_content(
                seed_data,
                child,
                audio_files[metadata_index],
                audio_file_index=metadata_index,
            )
            content.extend(make_block(child.block_type, child_content))
            metadata_index += 1
        elif child.content_type == 0x1003:
            pass
        else:
            content.extend(_full_block_bytes(seed_data, child))
        if child is last_metadata_child:
            while metadata_index < len(audio_files):
                clone = metadata_children[metadata_index % len(metadata_children)]
                child_content = _audio_file_metadata_content(
                    seed_data,
                    clone,
                    audio_files[metadata_index],
                    audio_file_index=metadata_index,
                    refresh_private_id=True,
                )
                content.extend(make_block(clone.block_type, child_content))
                metadata_index += 1
        cursor = child_end
    content.extend(original[cursor:])

    if not file_list_done:
        raise ValueError("audio file table template has no 0x103a file list child")
    if metadata_index != len(audio_files):
        raise ValueError("audio file table template has no 0x1003 metadata child")
    return bytes(content)


def _audio_region_templates(seed_data: bytes) -> list[_AudioRegionTemplate]:
    regions = _first_top_level(parse_unxored(seed_data), 0x262A)
    templates: list[_AudioRegionTemplate] = []
    for region in regions.child:
        if region.content_type != 0x2629:
            continue
        region_info = next((child for child in region.child if child.content_type == 0x2628), None)
        if region_info is None:
            continue
        info_block_end = region_info.offset + region_info.block_size
        tail = seed_data[info_block_end : region.offset + region.block_size]
        if len(tail) < 4:
            raise ValueError("audio region template has no source file index tail")
        templates.append(
            _AudioRegionTemplate(
                region_entry_type=region.block_type,
                region_info_type=region_info.block_type,
                region_info_content=seed_data[
                    region_info.offset : region_info.offset + region_info.block_size
                ],
                region_entry_tail=tail,
            )
        )
    if not templates:
        raise ValueError("audio synthesis needs a template with at least one audio region")
    return templates


def _audio_region_info_content(
    template: _AudioRegionTemplate,
    clip: _AudioClipResolved,
    *,
    preserve_name_widths: bool = False,
) -> bytes:
    content = template.region_info_content
    if len(content) < 6 or content[:2] != b"\x28\x26":
        raise ValueError("audio region template is invalid")
    name_len = int.from_bytes(content[2:6], "little")
    old_points = 6 + name_len
    old_points_end = _three_point_end(content, old_points)
    tail = content[old_points_end:]

    out = bytearray(b"\x28\x26")
    if preserve_name_widths:
        out.extend(_fixed_width_audio_clip_string(clip.name, name_len))
    else:
        out.extend(_latin1_string(clip.name))
    out.extend(_encode_audio_region_points(clip.sampleoffset, clip.length, clip.source_start))
    out.extend(tail)
    return bytes(out)


def _audio_regions_content(
    seed_data: bytes,
    clips: Sequence[_AudioClipResolved],
    *,
    preserve_name_widths: bool = False,
) -> bytes:
    templates = _audio_region_templates(seed_data)
    content = bytearray(b"\x2a\x26")
    content.extend(len(clips).to_bytes(4, "little"))
    for idx, clip in enumerate(clips):
        template = templates[min(idx, len(templates) - 1)]
        region_info = _audio_region_info_content(
            template,
            clip,
            preserve_name_widths=preserve_name_widths,
        )
        region_info_block = make_block(template.region_info_type, region_info)
        entry_tail = bytearray(template.region_entry_tail)
        entry_tail[:4] = int(clip.file_index).to_bytes(4, "little")
        if len(entry_tail) >= 8:
            entry_tail[4:8] = int(clip.channel_index).to_bytes(4, "little")
        if len(entry_tail) >= 8:
            entry_tail[-8:-4] = int(clip.file_index).to_bytes(4, "little")

        region_entry = bytearray(b"\x29\x26")
        region_entry.extend(region_info_block)
        region_entry.extend(entry_tail)
        content.extend(make_block(template.region_entry_type, bytes(region_entry)))
    return bytes(content)


def _audio_placement_template(seed_data: bytes) -> _AudioPlacementTemplate:
    active = _first_top_level(parse_unxored(seed_data), 0x1054)
    track = next((child for child in active.child if child.content_type == 0x1052), None)
    if track is None:
        raise ValueError("audio synthesis needs a template with an audio track placement")

    top_content = seed_data[active.offset : active.offset + active.block_size]
    top_tail_rel = len(top_content)
    for child in sorted(active.child, key=lambda item: item.offset):
        if child.content_type != 0x1052:
            top_tail_rel = child.offset - 7 - active.offset
            break
    top_tail = bytes(top_content[top_tail_rel:])

    track_content = seed_data[track.offset : track.offset + track.block_size]
    track_tail_rel = len(track_content)
    for child in sorted(track.child, key=lambda item: item.offset):
        if child.content_type == 0x1050:
            track_tail_rel = child.offset + child.block_size - track.offset
    track_tail = bytes(track_content[track_tail_rel:])

    placement = next((child for child in track.child if child.content_type == 0x1050), None)
    if placement is None:
        raise ValueError("audio synthesis needs a template with an audio clip placement")
    placement_entry = next(
        (child for child in placement.child if child.content_type == 0x104F),
        None,
    )
    if placement_entry is None:
        raise ValueError("audio synthesis needs a template with an audio placement entry")

    placement_content = seed_data[placement.offset : placement.offset + placement.block_size]
    entry_end = placement_entry.offset + placement_entry.block_size - placement.offset
    return _AudioPlacementTemplate(
        active_type=active.block_type,
        track_type=track.block_type,
        placement_type=placement.block_type,
        placement_entry_type=placement_entry.block_type,
        top_tail=top_tail,
        track_tail=track_tail,
        placement_tail=bytes(placement_content[entry_end:]),
        placement_entry_content=seed_data[
            placement_entry.offset : placement_entry.offset + placement_entry.block_size
        ],
    )


def _audio_placement_from_template(
    template: _AudioPlacementTemplate,
    clip: _AudioClipResolved,
) -> bytes:
    entry = bytearray(template.placement_entry_content)
    if len(entry) < 13 or entry[:2] != b"\x4f\x10":
        raise ValueError("audio placement template is invalid")
    entry[4:8] = int(clip.region_index).to_bytes(4, "little")
    entry[9:13] = int(clip.startpos).to_bytes(4, "little")
    entry_block = make_block(template.placement_entry_type, bytes(entry))
    return make_block(template.placement_type, b"\x50\x10" + entry_block + template.placement_tail)


def _audio_active_lane_name_widths(seed_data: bytes) -> list[int]:
    active = _first_top_level(parse_unxored(seed_data), 0x1054)
    widths: list[int] = []
    for track in sorted(active.child, key=lambda item: item.offset):
        if track.content_type != 0x1052:
            continue
        content = seed_data[track.offset : track.offset + track.block_size]
        if len(content) < 6 or content[:2] != b"\x52\x10":
            continue
        widths.append(int.from_bytes(content[2:6], "little"))
    return widths


def _audio_active_content(
    seed_data: bytes,
    tracks: Sequence[AudioTrackSpec],
    clips: Sequence[_AudioClipResolved],
    *,
    preserve_name_widths: bool = False,
) -> bytes:
    template = _audio_placement_template(seed_data)
    lane_names = _audio_lane_names(tracks)
    lane_widths = _audio_active_lane_name_widths(seed_data) if preserve_name_widths else []
    if preserve_name_widths and len(lane_widths) != len(lane_names):
        raise ValueError("audio active lane template does not match requested lane count")
    content = bytearray(b"\x54\x10")
    content.extend(len(lane_names).to_bytes(4, "little"))

    clips_by_track: list[list[_AudioClipResolved]] = [[] for _ in lane_names]
    for clip in clips:
        clips_by_track[clip.track_index].append(clip)

    for lane_index, (lane_name, track_clips) in enumerate(zip(lane_names, clips_by_track)):
        track_content = bytearray(b"\x52\x10")
        if preserve_name_widths:
            track_content.extend(
                _fixed_width_latin1_string(
                    lane_name,
                    lane_widths[lane_index],
                    "audio track lane name",
                )
            )
        else:
            track_content.extend(_latin1_string(lane_name))
        track_content.extend(len(track_clips).to_bytes(4, "little"))
        for clip in track_clips:
            track_content.extend(_audio_placement_from_template(template, clip))
        track_content.extend(template.track_tail)
        content.extend(make_block(template.track_type, bytes(track_content)))
    content.extend(template.top_tail)
    return bytes(content)


def _audio_track_names_from_metadata(data: bytes) -> list[str]:
    names: list[str] = []
    for name, _channels in _audio_track_layout_from_metadata(data):
        if name and name not in names:
            names.append(name)
    return names


def _audio_track_layout_from_metadata(data: bytes) -> list[tuple[str, int]]:
    try:
        block = _first_top_level(parse_unxored(data), 0x1015)
    except ValueError:
        return []

    layout: list[tuple[str, int]] = []
    for child in block.child:
        if child.content_type != 0x1014:
            continue
        name, end = _read_latin1_string_with_end(data, child.offset + 2)
        channels_pos = end + 1
        if not name or channels_pos + 4 > child.offset + child.block_size:
            continue
        channels = int.from_bytes(data[channels_pos : channels_pos + 4], "little")
        layout.append((name, channels))
    return layout


def _audio_active_lane_count(data: bytes) -> int:
    try:
        block = _first_top_level(parse_unxored(data), 0x1054)
    except ValueError:
        return 0
    return sum(1 for child in block.child if child.content_type == 0x1052)


def _ensure_audio_track_scaffold(data: bytes, tracks: Sequence[AudioTrackSpec]) -> None:
    requested_channels = [_audio_track_channels(track) for track in tracks]
    metadata_channels = [channels for _name, channels in _audio_track_layout_from_metadata(data)]
    required_lanes = sum(requested_channels)
    actual_lanes = _audio_active_lane_count(data)
    if metadata_channels != requested_channels or actual_lanes != required_lanes:
        raise NotImplementedError(
            "structured audio writing needs a template or block_sources with matching "
            "audio track/channel scaffold"
        )


def _audio_track_metadata_replacements(
    data: bytes,
    tracks: Sequence[AudioTrackSpec],
    *,
    preserve_name_widths: bool = False,
) -> dict[int, bytes]:
    old_names = _audio_track_names_from_metadata(data)
    replacements: dict[bytes, bytes] = {}
    for old_name, track in zip(old_names, tracks):
        replacement = track.name
        if preserve_name_widths:
            replacement_bytes = _fixed_width_latin1_string(
                replacement,
                len(old_name.encode("latin-1")),
                "audio track name",
            )[4:]
        else:
            replacement_bytes = replacement.encode("latin-1")
        if old_name.encode("latin-1") != replacement_bytes:
            replacements[old_name.encode("latin-1")] = replacement_bytes
    if not replacements:
        return {}

    ptf = parse_unxored(data)
    blocks: dict[int, bytes] = {}
    for content_type in (0x1015, 0x2107, 0x2519):
        try:
            block = _first_top_level(ptf, content_type)
        except ValueError:
            continue
        content = _rebuild_block_with_string_replacements(data, block, replacements)
        blocks[content_type] = make_block(block.block_type, content)
    return blocks


def _resolved_midi_clips(tracks: Sequence[MidiTrackSpec]) -> list[_MidiClipResolved]:
    clips: list[_MidiClipResolved] = []
    for track_index, track in enumerate(tracks):
        for clip_index, clip in enumerate(track.clips):
            notes = tuple(clip.notes)
            length = clip.length
            if length is None:
                length = max((event.pos + event.length for event in notes), default=0)
            length = max(int(length), DEFAULT_MIDI_REGION_TICKS)
            name = clip.name or f"{track.name}-{clip_index + 1:02d}"
            clips.append(
                _MidiClipResolved(
                    track_index=track_index,
                    track_name=track.name,
                    region_index=len(clips),
                    name=name,
                    startpos=int(clip.startpos),
                    notes=notes,
                    length=length,
                )
            )
    return clips


def _midi_chunk_templates(seed_data: bytes) -> _MidiChunkTemplate:
    block = _first_top_level(parse_unxored(seed_data), 0x2000)
    content = seed_data[block.offset : block.offset + block.block_size]
    if len(content) < 18 or int.from_bytes(content[2:6], "little") < 1:
        raise ValueError("MIDI synthesis needs a template with at least one MIDI clip")

    if content[6:12] != b"MdChun":
        raise ValueError("MIDI template has no MdChun chunk")
    chunk_size = int.from_bytes(content[14:18], "little")
    chunk = bytes(content[6 : 18 + chunk_size])

    nlb_pos = chunk.find(b"MdNLB")
    if nlb_pos < 0:
        raise ValueError("MIDI template has no MdNLB note list")
    nlb_size = int.from_bytes(chunk[nlb_pos + 7 : nlb_pos + 11], "little")
    event_count = int.from_bytes(chunk[nlb_pos + 11 : nlb_pos + 15], "little")
    event_start = nlb_pos + 15
    if event_count < 1 or event_start + 35 > len(chunk):
        raise ValueError("MIDI template has no MIDI note event to clone")

    nlb_end = nlb_pos + 11 + nlb_size
    events_end = event_start + event_count * 35
    if events_end > nlb_end or nlb_end > len(chunk):
        raise ValueError("MIDI template has an invalid MdNLB size")

    return _MidiChunkTemplate(
        chunk=chunk,
        event=bytes(chunk[event_start : event_start + 35]),
        nlb_pos=nlb_pos,
        nlb_tail=bytes(chunk[events_end:nlb_end]),
        chunk_tail=bytes(chunk[nlb_end:]),
    )


def _midi_region_template(seed_data: bytes) -> _MidiRegionTemplate:
    regions = _first_top_level(parse_unxored(seed_data), 0x2634)
    region_entry = next((child for child in regions.child if child.content_type == 0x2633), None)
    if region_entry is None:
        raise ValueError("MIDI synthesis needs a template with a MIDI region entry")
    region_info = next(
        (child for child in region_entry.child if child.content_type == 0x2628),
        None,
    )
    if region_info is None:
        raise ValueError("MIDI synthesis needs a template with a MIDI region info block")

    return _MidiRegionTemplate(
        region_entry_type=region_entry.block_type,
        region_info_type=region_info.block_type,
        region_info_content=seed_data[region_info.offset : region_info.offset + region_info.block_size],
    )


def _midi_placement_template(seed_data: bytes) -> _MidiPlacementTemplate:
    active = _first_top_level(parse_unxored(seed_data), 0x1058)
    track = next((child for child in active.child if child.content_type == 0x1057), None)
    if track is None:
        raise ValueError("MIDI synthesis needs a template with a MIDI track placement")

    track_content = seed_data[track.offset : track.offset + track.block_size]
    name_len = int.from_bytes(track_content[2:6], "little")
    after_count = 2 + 4 + name_len + 4
    tail_rel = len(track_content)
    for child in sorted(track.child, key=lambda item: item.offset):
        if child.content_type != 0x1056:
            tail_rel = child.offset - 7 - track.offset
            break
    track_tail = bytes(track_content[tail_rel:]) if tail_rel >= after_count else b""

    placement = next((child for child in track.child if child.content_type == 0x1056), None)
    if placement is None:
        raise ValueError("MIDI synthesis needs a template with a MIDI clip placement")
    placement_entry = next(
        (child for child in placement.child if child.content_type == 0x104F),
        None,
    )
    if placement_entry is None:
        raise ValueError("MIDI synthesis needs a template with a MIDI placement entry")

    return _MidiPlacementTemplate(
        track_type=track.block_type,
        placement_type=placement.block_type,
        placement_entry_type=placement_entry.block_type,
        track_tail=track_tail,
        placement_entry_content=seed_data[
            placement_entry.offset : placement_entry.offset + placement_entry.block_size
        ],
    )


def _midi_event_from_template(template: bytes, event: MidiEvent) -> bytes:
    if len(template) != 35:
        raise ValueError("MIDI event template must be 35 bytes")
    record = bytearray(template)
    record[0:5] = int(ZERO_TICKS + event.pos).to_bytes(5, "little")
    record[8] = int(event.note) & 0x7F
    record[9:14] = int(event.length).to_bytes(5, "little")
    record[17] = int(event.velocity) & 0x7F
    return bytes(record)


def _midi_chunk_from_template(template: _MidiChunkTemplate, clip: _MidiClipResolved) -> bytes:
    records = b"".join(_midi_event_from_template(template.event, event) for event in clip.notes)
    nlb_size = 4 + len(records) + len(template.nlb_tail)

    chunk = bytearray()
    chunk.extend(template.chunk[: template.nlb_pos + 7])
    chunk.extend(nlb_size.to_bytes(4, "little"))
    chunk.extend(len(clip.notes).to_bytes(4, "little"))
    chunk.extend(records)
    chunk.extend(template.nlb_tail)
    chunk.extend(template.chunk_tail)
    chunk[8:12] = (len(chunk) - 12).to_bytes(4, "little")
    return bytes(chunk)


def _midi_events_content(seed_data: bytes, clips: Sequence[_MidiClipResolved]) -> bytes:
    if not clips:
        return b"\x00\x20\x00\x00\x00\x00"
    template = _midi_chunk_templates(seed_data)
    content = bytearray(b"\x00\x20")
    content.extend(len(clips).to_bytes(4, "little"))
    for clip in clips:
        content.extend(_midi_chunk_from_template(template, clip))
    return bytes(content)


def _midi_region_info_content(
    template: _MidiRegionTemplate,
    clip: _MidiClipResolved,
) -> bytes:
    content = template.region_info_content
    if len(content) < 6 or content[:2] != b"\x28\x26":
        raise ValueError("MIDI region template is invalid")
    name_len = int.from_bytes(content[2:6], "little")
    old_points = 6 + name_len
    old_points_end = _three_point_end(content, old_points)
    old_start_n = _three_point_start_len(content, old_points)
    tail = content[old_points_end + old_start_n :]

    out = bytearray(b"\x28\x26")
    out.extend(_latin1_string(clip.name))
    out.extend(_encode_midi_region_points(clip.startpos, max(clip.length, 1)))
    if clip.startpos:
        out.extend(int(clip.startpos).to_bytes(_varint_len(clip.startpos), "little"))
    out.extend(tail)
    return bytes(out)


def _midi_regions_content(
    seed_data: bytes,
    clips: Sequence[_MidiClipResolved],
) -> bytes:
    template = _midi_region_template(seed_data)
    content = bytearray(b"\x34\x26")
    content.extend(len(clips).to_bytes(4, "little"))
    for clip in clips:
        region_info = _midi_region_info_content(template, clip)
        region_info_block = make_block(template.region_info_type, region_info)
        region_entry = bytearray(b"\x33\x26")
        region_entry.extend(region_info_block)
        region_entry.extend(clip.region_index.to_bytes(4, "little"))
        content.extend(make_block(template.region_entry_type, bytes(region_entry)))
    return bytes(content)


def _midi_track_names_from_metadata(data: bytes) -> list[str]:
    try:
        block = _first_top_level(parse_unxored(data), 0x2519)
    except ValueError:
        return []

    names: list[str] = []
    for child in block.child:
        if child.content_type != 0x251A:
            continue
        name = _read_latin1_string(data, child.offset + 4)
        if name and name not in names:
            names.append(name)
    return names


def _rewrite_length_prefixed_strings(segment: bytes, replacements: Mapping[bytes, bytes]) -> bytes:
    out = bytearray()
    pos = 0
    while pos < len(segment):
        replaced = False
        if pos + 4 <= len(segment):
            length = int.from_bytes(segment[pos : pos + 4], "little")
            start = pos + 4
            end = start + length
            if 0 < length <= 255 and end <= len(segment):
                value = segment[start:end]
                replacement = replacements.get(value)
                if replacement is not None:
                    out.extend(len(replacement).to_bytes(4, "little"))
                    out.extend(replacement)
                    pos = end
                    replaced = True
        if not replaced:
            out.append(segment[pos])
            pos += 1
    return bytes(out)


def _rebuild_block_with_string_replacements(
    data: bytes,
    block: Block,
    replacements: Mapping[bytes, bytes],
) -> bytes:
    original = data[block.offset : block.offset + block.block_size]
    content = bytearray()
    cursor = 0
    for child in sorted(block.child, key=lambda item: item.offset):
        child_start = child.offset - 7 - block.offset
        child_end = child.offset + child.block_size - block.offset
        content.extend(_rewrite_length_prefixed_strings(original[cursor:child_start], replacements))
        child_content = _rebuild_block_with_string_replacements(data, child, replacements)
        content.extend(make_block(child.block_type, child_content))
        cursor = child_end
    content.extend(_rewrite_length_prefixed_strings(original[cursor:], replacements))
    return bytes(content)


def _midi_track_metadata_replacements(
    data: bytes,
    tracks: Sequence[MidiTrackSpec],
) -> dict[int, bytes]:
    old_names = _midi_track_names_from_metadata(data)
    replacements: dict[bytes, bytes] = {}
    for old_name, track in zip(old_names, tracks):
        if old_name != track.name:
            replacements[old_name.encode("latin-1")] = track.name.encode("latin-1")
    if not replacements:
        return {}

    ptf = parse_unxored(data)
    blocks: dict[int, bytes] = {}
    for content_type in (0x2519, 0x2107):
        try:
            block = _first_top_level(ptf, content_type)
        except ValueError:
            continue
        content = _rebuild_block_with_string_replacements(data, block, replacements)
        blocks[content_type] = make_block(block.block_type, content)
    return blocks


def _midi_placement_from_template(
    template: _MidiPlacementTemplate,
    clip: _MidiClipResolved,
) -> bytes:
    entry = bytearray(template.placement_entry_content)
    if len(entry) < 14 or entry[:2] != b"\x4f\x10":
        raise ValueError("MIDI placement template is invalid")
    entry[4:8] = clip.region_index.to_bytes(4, "little")
    entry[9:14] = int(ZERO_TICKS + clip.startpos).to_bytes(5, "little")
    entry_block = make_block(template.placement_entry_type, bytes(entry))
    return make_block(template.placement_type, b"\x56\x10" + entry_block)


def _midi_active_content(
    seed_data: bytes,
    tracks: Sequence[MidiTrackSpec],
    clips: Sequence[_MidiClipResolved],
) -> bytes:
    template = _midi_placement_template(seed_data)
    content = bytearray(b"\x58\x10")
    content.extend(len(tracks).to_bytes(4, "little"))

    clips_by_track: list[list[_MidiClipResolved]] = [[] for _ in tracks]
    for clip in clips:
        clips_by_track[clip.track_index].append(clip)

    for track, track_clips in zip(tracks, clips_by_track):
        track_content = bytearray(b"\x57\x10")
        track_content.extend(_latin1_string(track.name))
        track_content.extend(len(track_clips).to_bytes(4, "little"))
        for clip in track_clips:
            track_content.extend(_midi_placement_from_template(template, clip))
        track_content.extend(template.track_tail)
        content.extend(make_block(template.track_type, bytes(track_content)))
    return bytes(content)


def with_midi_tracks(
    data: bytes,
    tracks: Iterable[MidiTrackSpec],
    *,
    midi_template: str | Path | bytes | None = None,
) -> bytes:
    """Synthesize PT12 MIDI event, region, and active placement blocks.

    This is intentionally still template-assisted: the known musical fields are
    generated from `tracks`, while opaque per-block tails are cloned from an
    existing MIDI clip.  If `data` has no MIDI clip yet, pass `midi_template`
    pointing at a small known-good MIDI session.
    """

    tracks = list(tracks)
    clips = _resolved_midi_clips(tracks)
    if midi_template is None:
        seed_data = data
    elif isinstance(midi_template, bytes):
        seed_data = midi_template
    else:
        seed_data = load_unxored(midi_template)

    replacements = {
        0x2000: make_block(
            _block_type_from(data, seed_data, 0x2000),
            _midi_events_content(seed_data, clips),
        ),
        0x2634: make_block(
            _block_type_from(data, seed_data, 0x2634),
            _midi_regions_content(seed_data, clips),
        ),
        0x1058: make_block(
            _block_type_from(data, seed_data, 0x1058),
            _midi_active_content(seed_data, tracks, clips),
        ),
    }
    replacements.update(_midi_track_metadata_replacements(data, tracks))
    return _replace_top_level_blocks(data, replacements)


def with_audio_tracks(
    data: bytes,
    audio_files: Iterable[AudioFileSpec],
    tracks: Iterable[AudioTrackSpec],
    *,
    audio_template: str | Path | bytes | None = None,
    preserve_name_widths: bool = False,
    robust_index: bool = False,
) -> bytes:
    """Synthesize PT12 audio file, region, and placement blocks.

    Multiple audio files and multiple regions are supported.  The output
    template must already have matching mono/stereo audio track scaffolding, or
    receive it first through `block_sources`.
    """

    audio_files = list(audio_files)
    tracks = list(tracks)
    _ensure_audio_track_scaffold(data, tracks)

    if audio_template is None:
        seed_data = data
    elif isinstance(audio_template, bytes):
        seed_data = audio_template
    else:
        seed_data = load_unxored(audio_template)

    clips = _resolved_audio_clips(audio_files, tracks, seed_data)
    replacements = {
        0x1004: make_block(
            _block_type_from(data, seed_data, 0x1004),
            _audio_files_content(seed_data, audio_files),
        ),
        0x262A: make_block(
            _block_type_from(data, seed_data, 0x262A),
            _audio_regions_content(
                seed_data,
                clips,
                preserve_name_widths=preserve_name_widths,
            ),
        ),
        0x1054: make_block(
            _block_type_from(data, seed_data, 0x1054),
            _audio_active_content(
                seed_data,
                tracks,
                clips,
                preserve_name_widths=preserve_name_widths,
            ),
        ),
    }
    replacements.update(
        _audio_track_metadata_replacements(
            data,
            tracks,
            preserve_name_widths=preserve_name_widths,
        )
    )
    return _replace_top_level_blocks(data, replacements, robust_index=robust_index)


def _tempo_content_from_template(
    data: bytes,
    template: Block,
    events: Sequence[TempoEvent],
) -> bytes:
    content = bytearray(data[template.offset : template.offset + template.block_size])
    old_count = int.from_bytes(content[13:17], "little") if len(content) >= 17 else 0
    if len(content) < 34:
        raise ValueError("tempo template is too short")

    records_start = 21
    records_end = records_start + old_count * 61
    footer = bytes(content[records_end:])
    if len(footer) != 13:
        footer = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x80\x88\xe5\x40"

    record_templates = [
        bytes(content[records_start + i * 61 : records_start + (i + 1) * 61])
        for i in range(old_count)
    ]
    if not record_templates:
        raise ValueError("tempo template has no record to clone")

    new_content = bytearray(content[:records_start])
    new_content[9:13] = (4 + len(events) * 61).to_bytes(4, "little")
    new_content[13:17] = len(events).to_bytes(4, "little")
    for idx, event in enumerate(events):
        rec = bytearray(record_templates[min(idx, len(record_templates) - 1)])
        if idx:
            rec[21] = 0
            rec[39] = 0
        rec[30:35] = int(ZERO_TICKS + event.pos).to_bytes(5, "little")
        rec[40:48] = struct.pack("<d", float(event.bpm))
        rec[48:52] = int(event.ppq).to_bytes(4, "little")
        new_content.extend(rec)
    new_content.extend(footer)
    return bytes(new_content)


def _meter_content_from_template(events: Sequence[MeterEvent]) -> bytes:
    content = bytearray()
    content.extend(b"\x29\x20Meter\x02\x00")
    payload_len = 12 + len(events) * 52
    content.extend(payload_len.to_bytes(4, "little"))
    content.extend(len(events).to_bytes(4, "little"))

    for idx, event in enumerate(events):
        ordinal = event.ordinal if event.ordinal else idx + 1
        content.extend(int(ZERO_TICKS + event.pos).to_bytes(5, "little"))
        content.extend(b"\x00\x00\x00")
        content.extend(int(ordinal).to_bytes(4, "little"))
        content.extend(int(event.numerator).to_bytes(4, "little"))
        content.extend(int(event.denominator).to_bytes(4, "little"))
        content.extend((196609).to_bytes(4, "little"))
        content.extend((0).to_bytes(4, "little"))
        content.extend((2).to_bytes(4, "little"))
        content.extend((3).to_bytes(4, "little"))

    for idx, event in enumerate(events):
        ordinal = event.ordinal if event.ordinal else idx + 1
        content.extend(int(ordinal).to_bytes(4, "little"))
        content.extend((1).to_bytes(4, "little"))
        content.extend(b"\x00" * 8)
    content.extend(int(ZERO_TICKS).to_bytes(5, "little"))
    content.extend(b"\x00\x00\x00")
    return bytes(content)


def _lane_content_with_child(
    data: bytes,
    lane: Block,
    child_content_type: int,
    replacement_child: bytes,
) -> bytes:
    original = data[lane.offset : lane.offset + lane.block_size]
    content = bytearray()
    cursor = 0
    replaced = False
    for child in sorted(lane.child, key=lambda item: item.offset):
        child_start = child.offset - 7 - lane.offset
        child_end = child.offset + child.block_size - lane.offset
        content.extend(original[cursor:child_start])
        if child.content_type == child_content_type:
            content.extend(replacement_child)
            replaced = True
        else:
            content.extend(_full_block_bytes(data, child))
        cursor = child_end
    content.extend(original[cursor:])
    if not replaced:
        content.extend(replacement_child)
    return bytes(content)


def with_tempo_events(data: bytes, events: Iterable[TempoEvent]) -> bytes:
    events = list(events)
    ptf = parse_unxored(data)
    tempo = _first_top_level(ptf, 0x2028)
    lane = _first_top_level(ptf, 0x2718)
    lane_tempo = next((child for child in lane.child if child.content_type == 0x2028), tempo)

    top_content = _tempo_content_from_template(data, tempo, events)
    child_content = _tempo_content_from_template(data, lane_tempo, events)
    top_block = make_block(tempo.block_type, top_content)
    child_block = make_block(lane_tempo.block_type, child_content)
    lane_block = make_block(lane.block_type, _lane_content_with_child(data, lane, 0x2028, child_block))
    return _replace_top_level_blocks(
        data,
        {0x2028: top_block, 0x2718: lane_block},
        patch_unmarked_index_offsets=False,
    )


def with_meter_events(data: bytes, events: Iterable[MeterEvent]) -> bytes:
    events = list(events)
    ptf = parse_unxored(data)
    meter = _first_top_level(ptf, 0x2029)
    lane = _first_top_level(ptf, 0x2719)
    lane_meter = next((child for child in lane.child if child.content_type == 0x2029), meter)

    content = _meter_content_from_template(events)
    top_block = make_block(meter.block_type, content)
    child_block = make_block(lane_meter.block_type, content)
    lane_block = make_block(lane.block_type, _lane_content_with_child(data, lane, 0x2029, child_block))
    return _replace_top_level_blocks(
        data,
        {0x2029: top_block, 0x2719: lane_block},
        patch_unmarked_index_offsets=False,
    )


def with_blocks_from(data: bytes, source: str | Path, content_types: Iterable[int]) -> bytes:
    source_data = load_unxored(source)
    source_refs = top_level_refs(source_data)
    wanted = set(content_types)
    replacements: dict[int, bytes] = {}
    for ref in source_refs:
        if ref.block.content_type in wanted:
            replacements[ref.block.content_type] = ref.data
    missing = wanted - set(replacements)
    if missing:
        names = ", ".join(f"{content_type:#x}" for content_type in sorted(missing))
        raise ValueError(f"source has no top-level blocks: {names}")
    return _replace_top_level_blocks(
        data,
        replacements,
        index_source_data=source_data if 0x0002 in wanted else None,
    )


def write_template_session(
    template: str | Path,
    output: str | Path,
    *,
    audio_files: Iterable[AudioFileSpec] | None = None,
    audio_tracks: Iterable[AudioTrackSpec] | None = None,
    audio_template: str | Path | bytes | None = None,
    preserve_audio_name_widths: bool = False,
    midi_tracks: Iterable[MidiTrackSpec] | None = None,
    midi_template: str | Path | bytes | None = None,
    tempo_events: Iterable[TempoEvent] | None = None,
    meter_events: Iterable[MeterEvent] | None = None,
    block_sources: Mapping[str | Path, Iterable[int]] | None = None,
    validate_output: bool = True,
    audit_natural_order: Sequence[str] | None = None,
) -> None:
    """Write an encrypted PTX derived from a template session.

    `block_sources` is a conservative escape hatch for unknown scaffolding:
    it copies complete top-level blocks from known-good sessions.  `audio_files`
    and `audio_tracks`, `midi_tracks`, `tempo_events`, and `meter_events`
    synthesize their decoded musical payloads.  By default, the output file is
    audited for known Pro Tools open hazards after writing.  Pass
    `validate_output=False` only for deliberate reverse-engineering probes.
    """

    data = load_unxored(template)
    if block_sources:
        for source, content_types in block_sources.items():
            data = with_blocks_from(data, source, content_types)
    if audio_files is not None or audio_tracks is not None:
        if audio_files is None or audio_tracks is None:
            raise ValueError("audio_files and audio_tracks must be provided together")
        data = with_audio_tracks(
            data,
            audio_files,
            audio_tracks,
            audio_template=audio_template,
            preserve_name_widths=preserve_audio_name_widths,
        )
    if midi_tracks is not None:
        data = with_midi_tracks(data, midi_tracks, midi_template=midi_template)
    if tempo_events is not None:
        data = with_tempo_events(data, tempo_events)
    if meter_events is not None:
        data = with_meter_events(data, meter_events)

    output_path = Path(output)
    encrypted = encrypt_session_data(data)
    check_audio_links = audio_files is not None or _block_sources_include(block_sources, 0x1004)
    if validate_output:
        with tempfile.NamedTemporaryFile(
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp:
            temp_path = Path(temp.name)
            temp.write(encrypted)
        try:
            _raise_for_audit_issues(
                temp_path,
                natural_order=audit_natural_order,
                check_audio_links=check_audio_links,
            )
            temp_path.replace(output_path)
        except Exception:
            try:
                temp_path.unlink()
            except OSError:
                pass
            raise
    else:
        output_path.write_bytes(encrypted)


def write_midi_session(
    template: str | Path,
    output: str | Path,
    session: MidiSessionSpec,
    *,
    midi_template: str | Path | bytes | None = None,
    validate_output: bool = True,
) -> None:
    """Write a PT12 MIDI session from a template and structured MIDI data."""

    write_template_session(
        template,
        output,
        midi_tracks=session.tracks,
        midi_template=midi_template,
        tempo_events=session.tempo_events or None,
        meter_events=session.meter_events or None,
        validate_output=validate_output,
    )


def write_audio_session(
    template: str | Path,
    output: str | Path,
    session: AudioSessionSpec,
    *,
    audio_template: str | Path | bytes | None = None,
    validate_output: bool = True,
    preserve_name_widths: bool | None = None,
) -> None:
    """Write a PT12 audio session from matching template scaffolding."""

    if preserve_name_widths is None:
        preserve_name_widths = session.preserve_name_widths
    write_template_session(
        template,
        output,
        audio_files=session.audio_files,
        audio_tracks=session.tracks,
        audio_template=audio_template,
        preserve_audio_name_widths=preserve_name_widths,
        tempo_events=session.tempo_events or None,
        meter_events=session.meter_events or None,
        validate_output=validate_output,
    )


def _raise_for_audit_issues(
    output: str | Path,
    *,
    natural_order: Sequence[str] | None = None,
    check_audio_links: bool = True,
) -> None:
    from .audit import (
        SessionAuditError,
        analyze_session_audit,
        format_session_audit,
        validate_session_audit,
    )

    summary = analyze_session_audit(output, natural_order=natural_order)
    issues = validate_session_audit(summary, check_audio_links=check_audio_links)
    if issues:
        raise SessionAuditError(
            output,
            issues,
            format_session_audit(summary, check_audio_links=check_audio_links),
        )


def _block_sources_include(
    block_sources: Mapping[str | Path, Iterable[int]] | None,
    content_type: int,
) -> bool:
    if not block_sources:
        return False
    return any(content_type in set(content_types) for content_types in block_sources.values())
