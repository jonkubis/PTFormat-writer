"""Tests for the deterministic 0x0002 master-index rebuilder (Pass 2).

These validate the architecture documented in
`docs/final-index-0x0002-schema.md`:
  * offset holes are detected by *schema position*, not by value, so coincidental
    scalars (a count/size that equals a block offset at some N) are never
    misclassified as offsets;
  * every offset can be rebuilt losslessly from the block layout.
"""
from pathlib import Path
import unittest

from ptxformatwriter.core import PTFFormat
from ptxformatwriter import final_index as FI

ROOT = Path(__file__).resolve().parents[1]
CONTROL_DIR = ROOT / "control_files" / "lots of stereo tracks"
MONO_DIR = ROOT / "control_files" / "lots of mono tracks"


def _control(n: int) -> Path:
    return CONTROL_DIR / f"{n} stereo tracks.ptx"


def _available_counts() -> list[int]:
    return [n for n in range(1, 17) if _control(n).exists()]


def _mono_control(n: int) -> Path:
    return MONO_DIR / f"{n} mono tracks.ptx"


def _mono_available() -> list[int]:
    return [n for n in range(1, 17) if _mono_control(n).exists()]


def _load_mono(n: int) -> bytes:
    ptf = PTFFormat()
    ptf.load(str(_mono_control(n)), 48000)
    return ptf.unxored_data()


def _load(n: int) -> bytes:
    ptf = PTFFormat()
    ptf.load(str(_control(n)), 48000)
    return ptf.unxored_data()


def _records(n: int):
    data = _load(n)
    ref = FI.final_index_ref(data)
    zmark_to_type, by_type = FI.block_layout(data)
    return FI.parse_records(ref.data, set(by_type), set(zmark_to_type)), ref.data


def _signature(record) -> tuple:
    return (
        record.content_type,
        record.count,
        record.flag,
        record.ordinal,
        tuple(c.child_type for c in record.child_refs),
        tuple((e.tag1, len(e.offsets)) for e in record.elements),
    )


def _hole_values(records) -> list[int]:
    values: list[int] = []
    for record in records:
        values.extend(c.offset for c in record.child_refs)
        for element in record.elements:
            values.extend(element.offsets)
    return values


def _assign_holes(records, values) -> None:
    it = iter(values)
    for record in records:
        for child in record.child_refs:
            child.offset = next(it)
        for element in record.elements:
            element.offsets = [next(it) for _ in element.offsets]


@unittest.skipUnless(_available_counts(), "stereo-track control files not present")
class FinalIndexRebuildTests(unittest.TestCase):
    def test_round_trip_identity(self) -> None:
        """Rebuilding offsets from the unchanged layout reproduces the file."""
        for n in _available_counts():
            data = _load(n)
            self.assertEqual(
                FI.rebuild_index_offsets(data),
                data,
                msg=f"round-trip changed bytes for n={n}",
            )

    def test_marker_holes_match_formula(self) -> None:
        """Grammar-parsed marker-element offsets follow markers = 8N + 164."""
        for n in _available_counts():
            _ref, holes = FI.offset_holes(_load(n))
            markers = sum(1 for h in holes if h[4] == "marker")
            self.assertEqual(markers, 8 * n + 164, msg=f"marker count off for n={n}")

    def test_childref_holes_are_linear(self) -> None:
        """Child-ref holes follow childrefs = 4N + 51."""
        for n in _available_counts():
            _ref, holes = FI.offset_holes(_load(n))
            childrefs = sum(1 for h in holes if h[4] == "childref")
            self.assertEqual(childrefs, 4 * n + 51, msg=f"childref count off for n={n}")

    def test_no_coincidental_scalar_holes(self) -> None:
        """N=1 and N=7 contain scalars equal to block offsets; grammar parsing
        must not pick them up (a value-scanning detector would). Both land
        exactly on the formula."""
        for n in (1, 7):
            if n not in _available_counts():
                continue
            _ref, holes = FI.offset_holes(_load(n))
            markers = sum(1 for h in holes if h[4] == "marker")
            self.assertEqual(
                markers,
                8 * n + 164,
                msg=f"grammar parsing drifted at noisy n={n}: {markers}",
            )

    def test_forward_parser_tiles_exactly(self) -> None:
        """The grammar parser frames records with zero leftover and yields
        exactly record_count == 2N + 159 records."""
        for n in _available_counts():
            data = _load(n)
            ref = FI.final_index_ref(data)
            zmark_to_type, by_type = FI.block_layout(data)
            records = FI.parse_records(ref.data, set(by_type), set(zmark_to_type))
            declared = int.from_bytes(ref.data[9:13], "little")
            self.assertEqual(len(records), declared, msg=f"record count != field for n={n}")
            self.assertEqual(len(records), 2 * n + 159, msg=f"record count off for n={n}")
            # contiguous tiling from +13 with no gaps or overlaps
            self.assertEqual(records[0].start, 13)
            for prev, nxt in zip(records, records[1:]):
                self.assertEqual(prev.end, nxt.start, msg=f"gap/overlap at n={n}")
            self.assertEqual(records[-1].end, len(ref.data), msg=f"leftover bytes for n={n}")

    def test_all_holes_resolve_to_block_starts(self) -> None:
        """Every detected hole targets a real block ZMARK (0 unresolved)."""
        for n in _available_counts():
            data = _load(n)
            zmark_to_type, _by_type = FI.block_layout(data)
            _ref, holes = FI.offset_holes(data)
            for abs_pos, value, target_type, _rank, _kind in holes:
                self.assertIn(value, zmark_to_type, msg=f"unresolved hole at {abs_pos} (n={n})")
                self.assertEqual(zmark_to_type[value], target_type)

    def test_serialize_round_trips(self) -> None:
        """serialize_final_block(parse(index)) reproduces the index byte-exact."""
        for n in _available_counts():
            records, block = _records(n)
            self.assertEqual(FI.serialize_final_block(records), block, msg=f"serialize off for n={n}")

    def test_compose_index_byte_exact(self) -> None:
        """compose_index builds the exact N-track index from a smaller donor and
        the real N-track body (offsets resolved against the body layout), for both
        single-step (N-1 -> N) and multi-step (e.g. 1 -> N) growth."""
        available = set(_available_counts())
        pairs = [(n - 1, n) for n in sorted(available) if (n - 1) in available]
        for base, target in [(1, 8), (2, 8), (1, 16), (3, 10)]:
            if base in available and target in available:
                pairs.append((base, target))
        self.assertTrue(pairs, "need stereo controls to test composition")
        for base, target in pairs:
            target_data = _load(target)
            composed = FI.compose_index(_load(base), target_data, base, target)
            real_index = FI.final_index_ref(target_data).data
            self.assertEqual(composed, real_index, msg=f"composed index != real for {base}->{target}")

    @unittest.skipUnless(_mono_available(), "mono control files not present")
    def test_compose_index_mono_byte_exact(self) -> None:
        """compose_index(channels=1) builds the exact MONO index from a smaller
        mono donor. Mono tracks add ONE 0x1052 audio lane (vs stereo's two), so the
        0x1054 channel-container record must gain one marker per track, not two --
        the stereo shape produces an index that loads in ptxformatwriter but fails Pro
        Tools with "magic ID does not match while translating Audio Playlists"."""
        available = set(_mono_available())
        pairs = [(b, t) for (b, t) in [(2, 3), (3, 4), (2, 4), (1, 3), (2, 6)]
                 if b in available and t in available]
        self.assertTrue(pairs, "need adjacent mono controls")
        for base, target in pairs:
            target_data = _load_mono(target)
            composed = FI.compose_index(_load_mono(base), target_data, base, target, channels=1)
            real_index = FI.final_index_ref(target_data).data
            self.assertEqual(composed, real_index, msg=f"composed mono index != real for {base}->{target}")

    def test_synthesis_matches_real_controls(self) -> None:
        """Synthesizing N tracks from a smaller base reproduces the real index:
        identical record structure, and byte-exact once the layout-computed
        offsets are placed in the synthesized holes."""
        available = _available_counts()
        pairs = [(a, b) for (a, b) in [(2, 3), (3, 4), (4, 5), (7, 8), (2, 8)]
                 if a in available and b in available]
        # also a long synthesis run if the range is present
        if 1 in available and 16 in available:
            pairs.append((1, 16))
        self.assertTrue(pairs, "need adjacent stereo controls to test synthesis")
        for base, target in pairs:
            base_records, _ = _records(base)
            real_records, real_block = _records(target)
            synth = FI.synthesize_index_records(base_records, base, target)
            self.assertEqual(
                [_signature(r) for r in synth],
                [_signature(r) for r in real_records],
                msg=f"synthesized structure != real for {base}->{target}",
            )
            _assign_holes(synth, _hole_values(real_records))
            self.assertEqual(
                FI.serialize_final_block(synth),
                real_block,
                msg=f"synthesized bytes != real for {base}->{target}",
            )


if __name__ == "__main__":
    unittest.main()
