"""Small Standard MIDI File helpers for writer workflows."""

from __future__ import annotations

from pathlib import Path

from .core import MeterEvent, TempoEvent
from .writer import PPQ_TICKS


def _read_u16(data: bytes, pos: int) -> tuple[int, int]:
    if pos + 2 > len(data):
        raise ValueError("unexpected end of MIDI data")
    return int.from_bytes(data[pos : pos + 2], "big"), pos + 2


def _read_u32(data: bytes, pos: int) -> tuple[int, int]:
    if pos + 4 > len(data):
        raise ValueError("unexpected end of MIDI data")
    return int.from_bytes(data[pos : pos + 4], "big"), pos + 4


def _read_varlen(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for _ in range(4):
        if pos >= len(data):
            raise ValueError("unexpected end of MIDI variable-length value")
        byte = data[pos]
        pos += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, pos
    raise ValueError("MIDI variable-length value is too long")


def _scaled_tick(tick: int, source_ppq: int, target_ppq: int) -> int:
    return round(int(tick) * int(target_ppq) / int(source_ppq))


def _midi_channel_data_length(status: int) -> int:
    event_type = status & 0xF0
    if event_type in (0xC0, 0xD0):
        return 1
    if 0x80 <= event_type <= 0xE0:
        return 2
    raise ValueError(f"unsupported MIDI event status {status:#x}")


def _parse_tempo_meter_track(
    track: bytes,
    *,
    source_ppq: int,
    target_ppq: int,
) -> tuple[list[TempoEvent], list[MeterEvent]]:
    tempos: list[TempoEvent] = []
    meters: list[MeterEvent] = []
    pos = 0
    tick = 0
    running_status: int | None = None

    while pos < len(track):
        delta, pos = _read_varlen(track, pos)
        tick += delta
        if pos >= len(track):
            break

        status = track[pos]
        if status & 0x80:
            pos += 1
            if status < 0xF0:
                running_status = status
        else:
            if running_status is None:
                raise ValueError("MIDI running status used before a channel status")
            status = running_status

        if status == 0xFF:
            if pos >= len(track):
                raise ValueError("unexpected end of MIDI meta event")
            meta_type = track[pos]
            pos += 1
            length, pos = _read_varlen(track, pos)
            payload = track[pos : pos + length]
            if len(payload) != length:
                raise ValueError("unexpected end of MIDI meta payload")
            pos += length

            event_pos = _scaled_tick(tick, source_ppq, target_ppq)
            if meta_type == 0x51 and length == 3:
                micros_per_quarter = int.from_bytes(payload, "big")
                if micros_per_quarter <= 0:
                    raise ValueError("MIDI tempo meta event has zero tempo")
                tempos.append(
                    TempoEvent(
                        pos=event_pos,
                        bpm=60_000_000.0 / micros_per_quarter,
                        ppq=target_ppq,
                    )
                )
            elif meta_type == 0x58 and length >= 2:
                meters.append(
                    MeterEvent(
                        pos=event_pos,
                        numerator=payload[0],
                        denominator=2 ** payload[1],
                    )
                )
            elif meta_type == 0x2F:
                break
        elif status in (0xF0, 0xF7):
            length, pos = _read_varlen(track, pos)
            pos += length
            if pos > len(track):
                raise ValueError("unexpected end of MIDI SysEx payload")
            running_status = None
        else:
            length = _midi_channel_data_length(status)
            pos += length
            if pos > len(track):
                raise ValueError("unexpected end of MIDI channel event")

    return tempos, meters


def read_midi_tempo_map(
    path: str | Path,
    *,
    target_ppq: int = PPQ_TICKS,
    default_tempo_bpm: float = 120.0,
    default_meter: tuple[int, int] = (4, 4),
) -> tuple[list[TempoEvent], list[MeterEvent]]:
    """Read tempo and meter meta events from a Standard MIDI File.

    Returned event positions are scaled into Pro Tools writer ticks, whose
    default resolution is 960000 ticks per quarter note.
    """

    data = Path(path).read_bytes()
    pos = 0
    if data[pos : pos + 4] != b"MThd":
        raise ValueError("not a Standard MIDI File")
    pos += 4
    header_len, pos = _read_u32(data, pos)
    if header_len < 6:
        raise ValueError("MIDI header is too short")
    _format_type, header_pos = _read_u16(data, pos)
    track_count, header_pos = _read_u16(data, header_pos)
    division, header_pos = _read_u16(data, header_pos)
    if division & 0x8000:
        raise NotImplementedError("SMPTE-time MIDI files are not supported")
    source_ppq = division
    pos += header_len

    tempos: list[TempoEvent] = []
    meters: list[MeterEvent] = []
    for _ in range(track_count):
        if data[pos : pos + 4] != b"MTrk":
            raise ValueError("missing MIDI track chunk")
        pos += 4
        track_len, pos = _read_u32(data, pos)
        track = data[pos : pos + track_len]
        if len(track) != track_len:
            raise ValueError("unexpected end of MIDI track chunk")
        pos += track_len
        track_tempos, track_meters = _parse_tempo_meter_track(
            track,
            source_ppq=source_ppq,
            target_ppq=target_ppq,
        )
        tempos.extend(track_tempos)
        meters.extend(track_meters)

    if not any(event.pos == 0 for event in tempos):
        tempos.append(TempoEvent(pos=0, bpm=float(default_tempo_bpm), ppq=target_ppq))
    if not any(event.pos == 0 for event in meters):
        meters.append(
            MeterEvent(pos=0, numerator=int(default_meter[0]), denominator=int(default_meter[1]))
        )

    tempos.sort(key=lambda event: event.pos)
    meters.sort(key=lambda event: event.pos)
    meters = [
        MeterEvent(
            pos=event.pos,
            numerator=event.numerator,
            denominator=event.denominator,
            ordinal=index + 1,
        )
        for index, event in enumerate(meters)
    ]
    return tempos, meters
