"""Offline mixed-track order inspection helpers.

These helpers are intentionally narrow and evidence-driven.  They summarize the
mixed MIDI/audio track-order blocks we have controls for without attempting to
rewrite sessions.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .writer import (
    _final_index_records,
    _first_top_level,
    _flatten_block_starts,
    _full_block_bytes,
    _read_latin1_string_with_end,
    load_unxored,
    parse_unxored,
    top_level_refs,
)


@dataclass(frozen=True)
class AudioTrackOrderEntry:
    name: str
    channels: int
    channel_indexes: tuple[int, ...]


@dataclass(frozen=True)
class PlaylistOrderEntry:
    slot: int
    name: str
    content_type: int
    full_size: int
    one_based_slot: int | None


@dataclass(frozen=True)
class NameListOrderEntry:
    group_index: int
    slot: int
    name: str
    midi_slot_byte: int
    audio_slot_byte: int


@dataclass(frozen=True)
class MixedTrackOrderSummary:
    path: Path
    global_order: tuple[str, ...]
    audio_metadata: tuple[AudioTrackOrderEntry, ...]
    audio_lanes: tuple[str, ...]
    midi_metadata: tuple[str, ...]
    name_list: tuple[NameListOrderEntry, ...]
    playlist: tuple[PlaylistOrderEntry, ...]
    marker_201f: int | None
    midi_zero_based_slot_206a: int | None
    cache_2587_slot_bytes: tuple[tuple[int, int], ...]
    final_index_record_count: int
    final_index_type_counts: tuple[tuple[int, int], ...]

    @property
    def audio_order(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.audio_metadata)

    @property
    def playlist_order(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.playlist)

    @property
    def midi_names(self) -> tuple[str, ...]:
        return tuple(
            entry.name for entry in self.playlist if entry.content_type == 0x2620
        )


def analyze_mixed_track_order(path: str | Path) -> MixedTrackOrderSummary:
    """Extract known mixed MIDI/audio track-order fields from a PTX session."""

    session_path = Path(path)
    data = load_unxored(session_path)
    ptf = parse_unxored(data)

    global_order = _global_order(data, ptf)
    known_names = tuple(global_order)
    audio_metadata = _audio_metadata(data, ptf)
    audio_lanes = _audio_lanes(data, ptf, known_names)
    midi_metadata = _midi_metadata(data, ptf, known_names)
    name_list = _name_list(data, ptf, known_names)
    playlist = _playlist(data, ptf, known_names)
    final_counts = _final_index_type_counts(data)

    return MixedTrackOrderSummary(
        path=session_path,
        global_order=global_order,
        audio_metadata=audio_metadata,
        audio_lanes=audio_lanes,
        midi_metadata=midi_metadata,
        name_list=name_list,
        playlist=playlist,
        marker_201f=_marker_201f(data, ptf),
        midi_zero_based_slot_206a=_midi_slot_206a(data),
        cache_2587_slot_bytes=_cache_2587_slot_bytes(data, ptf),
        final_index_record_count=sum(count for _content_type, count in final_counts),
        final_index_type_counts=final_counts,
    )


def validate_mixed_track_order(
    summary: MixedTrackOrderSummary,
    *,
    natural_order: Sequence[str] | None = None,
) -> list[str]:
    """Return consistency issues for fields currently understood."""

    issues: list[str] = []
    global_order = summary.global_order
    if not global_order:
        issues.append("missing global 0x2107 order")

    first_name_group = tuple(
        entry.name
        for entry in summary.name_list
        if entry.group_index == 0
    )
    if first_name_group and first_name_group != global_order:
        issues.append(
            f"0x2519 first name group {first_name_group!r} != global order {global_order!r}"
        )

    second_name_group = tuple(
        entry.name
        for entry in summary.name_list
        if entry.group_index == 1
    )
    if second_name_group and second_name_group != global_order:
        issues.append(
            f"0x2519 second name group {second_name_group!r} != global order {global_order!r}"
        )

    if summary.playlist_order and summary.playlist_order != global_order:
        issues.append(
            f"0x2624 playlist order {summary.playlist_order!r} != global order {global_order!r}"
        )

    audio_names = {entry.name for entry in summary.audio_metadata}
    expected_audio_order = tuple(name for name in global_order if name in audio_names)
    if summary.audio_order != expected_audio_order:
        issues.append(
            f"0x1015 audio order {summary.audio_order!r} != visible audio order "
            f"{expected_audio_order!r}"
        )

    expected_lanes: list[str] = []
    for entry in summary.audio_metadata:
        expected_lanes.extend([entry.name] * entry.channels)
    if summary.audio_lanes != tuple(expected_lanes):
        issues.append(
            f"0x1054 audio lanes {summary.audio_lanes!r} != expected "
            f"{tuple(expected_lanes)!r}"
        )

    midi_names = summary.midi_names
    if len(midi_names) == 1:
        midi_name = midi_names[0]
        expected_zero_based = global_order.index(midi_name)
        if (
            summary.midi_zero_based_slot_206a is not None
            and summary.midi_zero_based_slot_206a != 0xFF
            and summary.midi_zero_based_slot_206a != expected_zero_based
        ):
            issues.append(
                f"0x206a MIDI slot {summary.midi_zero_based_slot_206a!r} != "
                f"{expected_zero_based!r}"
            )
        for entry in summary.playlist:
            if entry.name == midi_name and entry.one_based_slot != expected_zero_based + 1:
                issues.append(
                    f"0x2624 MIDI one-based slot {entry.one_based_slot!r} != "
                    f"{expected_zero_based + 1!r}"
                )
                break

    for entry in summary.name_list:
        visible_slot = global_order.index(entry.name) + 1
        if entry.name in midi_names:
            if entry.midi_slot_byte != visible_slot:
                issues.append(
                    f"0x2519 MIDI ordinal for {entry.name!r} group "
                    f"{entry.group_index} is {entry.midi_slot_byte}, expected {visible_slot}"
                )
        elif entry.name in audio_names:
            if entry.audio_slot_byte != visible_slot:
                issues.append(
                    f"0x2519 audio ordinal for {entry.name!r} group "
                    f"{entry.group_index} is {entry.audio_slot_byte}, expected {visible_slot}"
                )

    for entry in summary.playlist:
        if entry.content_type != 0x261C:
            continue
        visible_slot = global_order.index(entry.name) + 1
        if entry.one_based_slot is not None and entry.one_based_slot != visible_slot:
            issues.append(
                f"0x2624 audio ordinal for {entry.name!r} is "
                f"{entry.one_based_slot}, expected {visible_slot}"
            )

    if natural_order is not None and summary.marker_201f is not None:
        expected_marker = 5 if tuple(natural_order) == global_order else 0xFFFFFFFF
        if summary.marker_201f != expected_marker:
            issues.append(
                f"0x201f marker {summary.marker_201f:#x} != {expected_marker:#x}"
            )

    return issues


def validate_mixed_track_open_risk(summary: MixedTrackOrderSummary) -> list[str]:
    """Return mixed-order issues that are risky enough for generated-output audit."""

    issues: list[str] = []
    global_order = summary.global_order
    playlist_order = summary.playlist_order
    if not global_order:
        return ["missing global 0x2107 order"]

    playlist_is_global_subsequence = (
        bool(playlist_order)
        and _is_subsequence(global_order, playlist_order)
    )
    if playlist_order and not playlist_is_global_subsequence:
        issues.append(
            f"0x2624 playlist order {playlist_order!r} is not a global-order "
            f"subsequence of {global_order!r}"
        )

    midi_names = summary.midi_names
    if len(midi_names) == 1:
        midi_name = midi_names[0]
        if midi_name in global_order:
            expected_zero_based = global_order.index(midi_name)
            if (
                summary.midi_zero_based_slot_206a is not None
                and summary.midi_zero_based_slot_206a != 0xFF
                and summary.midi_zero_based_slot_206a != expected_zero_based
            ):
                issues.append(
                    f"0x206a MIDI slot {summary.midi_zero_based_slot_206a!r} != "
                    f"{expected_zero_based!r}"
                )

    # Stale 0x2519/name-list state is accepted when the playlist is a simple
    # global-order subset, e.g. a removed click track.  Once global and playlist
    # order claim to match, stale name-list ordinals have correlated with
    # malformed mixed sessions.
    if playlist_order == global_order:
        audio_names = {entry.name for entry in summary.audio_metadata}
        for entry in summary.name_list:
            visible_slot = global_order.index(entry.name) + 1
            if entry.name in midi_names:
                if entry.midi_slot_byte != visible_slot:
                    issues.append(
                        f"0x2519 MIDI ordinal for {entry.name!r} group "
                        f"{entry.group_index} is {entry.midi_slot_byte}, "
                        f"expected {visible_slot}"
                    )
            elif entry.name in audio_names:
                if entry.audio_slot_byte != visible_slot:
                    issues.append(
                        f"0x2519 audio ordinal for {entry.name!r} group "
                        f"{entry.group_index} is {entry.audio_slot_byte}, "
                        f"expected {visible_slot}"
                    )

    return issues


def format_mixed_track_order(summary: MixedTrackOrderSummary) -> str:
    """Format a compact human-readable mixed-order summary."""

    lines = [
        f"{summary.path}",
        f"  global: {', '.join(summary.global_order)}",
        "  audio metadata: "
        + ", ".join(
            f"{entry.name}[{','.join(str(index) for index in entry.channel_indexes)}]"
            for entry in summary.audio_metadata
        ),
        f"  audio lanes: {', '.join(summary.audio_lanes)}",
        f"  MIDI metadata: {', '.join(summary.midi_metadata)}",
        "  playlist: "
        + ", ".join(
            f"{entry.slot}:{entry.name}:{entry.content_type:#06x}:"
            f"{entry.full_size}:ord={entry.one_based_slot}"
            for entry in summary.playlist
        ),
        f"  0x201f marker: {_format_optional_int(summary.marker_201f)}",
        f"  0x206a MIDI slot: {_format_optional_int(summary.midi_zero_based_slot_206a)}",
        "  0x2587 bytes: "
        + ", ".join(f"{offset}={value}" for offset, value in summary.cache_2587_slot_bytes),
        f"  final records: {summary.final_index_record_count}",
    ]
    return "\n".join(lines)


def _global_order(data: bytes, ptf) -> tuple[str, ...]:
    block = _first_top_level(ptf, 0x2107)
    names: list[str] = []
    for child in sorted(block.child, key=lambda item: item.offset):
        if child.content_type != 0x210B:
            continue
        name = _display_name(_full_block_bytes(data, child))
        if name:
            names.append(name)
    return tuple(names)


def _audio_metadata(data: bytes, ptf) -> tuple[AudioTrackOrderEntry, ...]:
    block = _first_top_level(ptf, 0x1015)
    entries: list[AudioTrackOrderEntry] = []
    for child in sorted(block.child, key=lambda item: item.offset):
        if child.content_type != 0x1014:
            continue
        name, end = _read_latin1_string_with_end(data, child.offset + 2)
        channels_pos = end + 1
        if not name or channels_pos + 4 > child.offset + child.block_size:
            continue
        channels = int.from_bytes(data[channels_pos : channels_pos + 4], "little")
        channel_indexes = tuple(
            int.from_bytes(data[pos : pos + 2], "little")
            for pos in range(channels_pos + 4, channels_pos + 4 + channels * 2, 2)
            if pos + 2 <= child.offset + child.block_size
        )
        entries.append(
            AudioTrackOrderEntry(
                name=name,
                channels=channels,
                channel_indexes=channel_indexes,
            )
        )
    return tuple(entries)


def _audio_lanes(data: bytes, ptf, known_names: Sequence[str]) -> tuple[str, ...]:
    block = _first_top_level(ptf, 0x1054)
    names: list[str] = []
    for child in sorted(block.child, key=lambda item: item.offset):
        if child.content_type != 0x1052:
            continue
        name = _display_name(_full_block_bytes(data, child), known_names)
        if name:
            names.append(name)
    return tuple(names)


def _midi_metadata(data: bytes, ptf, known_names: Sequence[str]) -> tuple[str, ...]:
    block = _first_top_level(ptf, 0x1058)
    names: list[str] = []
    for child in sorted(block.child, key=lambda item: item.offset):
        if child.content_type != 0x1057:
            continue
        name = _display_name(_full_block_bytes(data, child), known_names)
        if name:
            names.append(name)
    return tuple(names)


def _name_list(
    data: bytes,
    ptf,
    known_names: Sequence[str],
) -> tuple[NameListOrderEntry, ...]:
    block = _first_top_level(ptf, 0x2519)
    names_per_group = len(known_names) if known_names else 1
    entries: list[NameListOrderEntry] = []
    name_index = 0
    for child in sorted(block.child, key=lambda item: item.offset):
        if child.content_type != 0x251A:
            continue
        full = _full_block_bytes(data, child)
        name = _display_name(full, known_names)
        if name:
            entries.append(
                NameListOrderEntry(
                    group_index=name_index // names_per_group,
                    slot=name_index % names_per_group + 1,
                    name=name,
                    midi_slot_byte=full[39] if len(full) > 39 else -1,
                    audio_slot_byte=full[40] if len(full) > 40 else -1,
                )
            )
            name_index += 1
    return tuple(entries)


def _playlist(
    data: bytes,
    ptf,
    known_names: Sequence[str],
) -> tuple[PlaylistOrderEntry, ...]:
    block = _first_top_level(ptf, 0x2624)
    entries: list[PlaylistOrderEntry] = []
    slot = 1
    for child in sorted(block.child, key=lambda item: item.offset):
        if child.content_type not in (0x261C, 0x2620, 0x261E):
            continue
        full = _full_block_bytes(data, child)
        name = _display_name(full, known_names)
        if not name:
            continue
        entries.append(
            PlaylistOrderEntry(
                slot=slot,
                name=name,
                content_type=child.content_type,
                full_size=len(full),
                one_based_slot=_playlist_one_based_slot(child.content_type, name, full),
            )
        )
        slot += 1
    return tuple(entries)


def _playlist_one_based_slot(content_type: int, name: str, full_block: bytes) -> int | None:
    if content_type == 0x2620 and len(full_block) >= 452:
        return int.from_bytes(full_block[448:452], "little")
    if content_type != 0x261C:
        return None
    # These offsets are currently control-derived for the no-click mixed session:
    # Audio 1 is mono, Audio 2 is stereo.
    offset = {"Audio 1": 1482, "Audio 2": 1521}.get(name)
    if offset is None or offset + 4 > len(full_block):
        return None
    value = int.from_bytes(full_block[offset : offset + 4], "little")
    return value if 1 <= value <= 1024 else None


def _marker_201f(data: bytes, ptf) -> int | None:
    try:
        block = _first_top_level(ptf, 0x201F)
    except ValueError:
        return None
    pos = block.offset + 78
    if pos + 4 > block.offset + block.block_size:
        return None
    return int.from_bytes(data[pos : pos + 4], "little")


def _midi_slot_206a(data: bytes) -> int | None:
    for ref in top_level_refs(data):
        if ref.block.content_type != 0x206A or len(ref.data) <= 100:
            continue
        if len(ref.data) <= 42:
            return None
        return ref.data[42]
    return None


def _cache_2587_slot_bytes(data: bytes, ptf) -> tuple[tuple[int, int], ...]:
    try:
        block = _first_top_level(ptf, 0x2587)
    except ValueError:
        return ()
    offsets = (1110, 1376, 1644)
    return tuple(
        (offset, data[block.offset + offset])
        for offset in offsets
        if block.offset + offset < block.offset + block.block_size
    )


def _final_index_type_counts(data: bytes) -> tuple[tuple[int, int], ...]:
    refs = top_level_refs(data)
    if not refs:
        return ()
    known_content_types = {content_type for _offset, content_type in _flatten_block_starts(data)}
    records = _final_index_records(refs[-1].data, known_content_types)
    counts: dict[int, int] = {}
    for _start, _end, content_type in records:
        counts[content_type] = counts.get(content_type, 0) + 1
    return tuple(sorted(counts.items()))


def _display_name(segment: bytes, known_names: Sequence[str] = ()) -> str:
    for name in sorted(known_names, key=len, reverse=True):
        if name.encode("latin-1") in segment:
            return name
    for value in _length_prefixed_strings(segment):
        if _looks_like_track_name(value):
            return value
    return ""


def _length_prefixed_strings(segment: bytes) -> tuple[str, ...]:
    values: list[str] = []
    for pos in range(0, max(len(segment) - 4, 0)):
        length = int.from_bytes(segment[pos : pos + 4], "little")
        if not 1 <= length <= 255:
            continue
        start = pos + 4
        end = start + length
        if end > len(segment):
            continue
        raw = segment[start:end]
        if all(32 <= byte < 127 for byte in raw):
            values.append(raw.decode("latin-1"))
    return tuple(values)


def _looks_like_track_name(value: str) -> bool:
    if not value or value.startswith("Info #"):
        return False
    return any(char.isalpha() for char in value)


def _is_subsequence(order: Sequence[str], candidate: Sequence[str]) -> bool:
    cursor = 0
    for name in order:
        if cursor < len(candidate) and candidate[cursor] == name:
            cursor += 1
    return cursor == len(candidate)


def _format_optional_int(value: int | None) -> str:
    return "n/a" if value is None else f"{value} ({value:#x})"
