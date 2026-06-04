from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ptxformatwriter import read_midi_tempo_map


def _varlen(value: int) -> bytes:
    parts = [value & 0x7F]
    value >>= 7
    while value:
        parts.append(0x80 | (value & 0x7F))
        value >>= 7
    return bytes(reversed(parts))


def _track(events: bytes) -> bytes:
    return b"MTrk" + len(events).to_bytes(4, "big") + events


class MidiTempoMapTests(unittest.TestCase):
    def test_read_midi_tempo_map_scales_to_pro_tools_ticks(self):
        division = 480
        events = b"".join(
            (
                _varlen(0),
                b"\xff\x51\x03\x07\xa1\x20",  # 120 bpm
                _varlen(0),
                b"\xff\x58\x04\x04\x02\x18\x08",  # 4/4
                _varlen(1920),
                b"\xff\x51\x03\x06\x1a\x80",  # 150 bpm
                _varlen(0),
                b"\xff\x58\x04\x03\x02\x18\x08",  # 3/4
                _varlen(0),
                b"\xff\x2f\x00",
            )
        )
        midi = (
            b"MThd"
            + (6).to_bytes(4, "big")
            + (1).to_bytes(2, "big")
            + (1).to_bytes(2, "big")
            + division.to_bytes(2, "big")
            + _track(events)
        )

        with TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "tempo-map.mid"
            path.write_bytes(midi)
            tempos, meters = read_midi_tempo_map(path)

        self.assertEqual(
            [(event.pos, event.bpm, event.ppq) for event in tempos],
            [(0, 120.0, 960000), (3840000, 150.0, 960000)],
        )
        self.assertEqual(
            [
                (event.pos, event.numerator, event.denominator, event.ordinal)
                for event in meters
            ],
            [(0, 4, 4, 1), (3840000, 3, 4, 2)],
        )


if __name__ == "__main__":
    unittest.main()
