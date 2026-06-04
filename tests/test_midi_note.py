"""Tests for MIDI clip/note insertion (`body_synth.add_midi_note`).

A one-note MIDI clip is added by the same matched-pair transplant + robust reindex as
the audio clip / tempo / meter: the clip's contribution is exactly the top-level
blocks that differ between an empty-MIDI-track session (`one midi track no clips
v2.ptx`) and the SAME session with a one-note clip (`one midi note.ptx`). The note's
pitch / velocity / length are then patched in the 0x2000 events block (pitch @ rec+8,
velocity @ rec+17, length 5-byte @ rec+9, plus a length-derived i32 @ rec+28).

Guards: the transplant reproduces the control byte-for-byte except the session
identity (0x2067), the first-block index pointer, and the 0x0002 index; each note
parameter patch reproduces the corresponding control variant's 0x2000 byte-for-byte.
"""
from pathlib import Path
import tempfile
import unittest

from ptxformatwriter.core import PTFFormat
from ptxformatwriter import body_synth as BS, writer as W

ROOT = Path(__file__).resolve().parents[1]
VAR = ROOT / "control_files" / "various"
_CLEAN = VAR / "one midi track no clips v2.ptx"
_NOTE = VAR / "one midi note.ptx"  # pitch 72, velocity 80, quarter (960000 ticks)
_NOTE_NOEDITOR = VAR / "midi note editor closed.ptx"  # same note, MIDI editor window closed


def _load_path(p: Path) -> bytes:
    ptf = PTFFormat()
    ptf.load(str(p), 48000)
    return ptf.unxored_data()


def _reload_ok(data: bytes) -> int:
    with tempfile.NamedTemporaryFile(suffix=".ptx", delete=False) as tmp:
        tmp.write(W.encrypt_session_data(data))
        path = tmp.name
    try:
        return PTFFormat().load(path, 48000)
    finally:
        Path(path).unlink(missing_ok=True)


def _top(data: bytes, ct: int) -> bytes:
    b = [x for x in BS.parse(data).blocks if x.content_type == ct][0]
    return data[b.offset - 7 : b.offset + b.block_size]


def _note(data: bytes) -> tuple:
    d = _top(data, 0x2000)
    m = d.find(b"MdNLB")
    rec = m + 11 + 4
    return (d[rec + 8], int.from_bytes(d[rec + 9:rec + 14], "little"), d[rec + 17])


@unittest.skipUnless(_CLEAN.exists() and _NOTE.exists(),
                     "MIDI controls (empty track / one note) not present")
class MidiNoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clean = _load_path(_CLEAN)
        self.note = _load_path(_NOTE)

    def test_transplant_byte_exact_mod_identity(self) -> None:
        """add_midi_note(clean, clean, note) reproduces the one-note control except the
        session identity (0x2067), the first-block index pointer, and the 0x0002 index
        — the same fidelity bar the audio clip passes."""
        out = BS.add_midi_note(self.clean, self.clean, self.note)
        to, tn = BS.parse(out).blocks, BS.parse(self.note).blocks
        self.assertEqual(len(to), len(tn))
        unexpected = []
        for i in range(len(to)):
            a, b = to[i], tn[i]
            if out[a.offset - 7:a.offset + a.block_size] != self.note[b.offset - 7:b.offset + b.block_size]:
                if i != 0 and b.content_type not in (0x2067, 0x0002):
                    unexpected.append((i, hex(b.content_type)))
        self.assertEqual(unexpected, [], msg=f"unexpected content diffs vs control: {unexpected}")
        self.assertEqual(_reload_ok(out), 0, msg="MIDI transplant failed to reload")
        self.assertEqual(_note(out), (72, 960000, 80))

    def test_note_param_patches_byte_exact(self) -> None:
        """Patching pitch / velocity / length reproduces the corresponding control
        variant's 0x2000 events block byte-for-byte (and reloads)."""
        cases = [
            (dict(pitch=60), "one midi note pitch 60.ptx"),
            (dict(velocity=127), "one midi note velocity 127.ptx"),
            (dict(velocity=64), "one midi note velocity 64.ptx"),
            (dict(length_ticks=1920000), "one midi note half note.ptx"),
            (dict(length_ticks=3840000), "one midi note whole note.ptx"),
        ]
        for kw, ctrl_name in cases:
            ctrl = VAR / ctrl_name
            if not ctrl.exists():
                continue
            out = BS.add_midi_note(self.clean, self.clean, self.note, **kw)
            self.assertEqual(_top(out, 0x2000), _top(_load_path(ctrl), 0x2000),
                             msg=f"0x2000 not byte-exact vs {ctrl_name} for patch {kw}")
            self.assertEqual(_reload_ok(out), 0, msg=f"patch {kw} failed to reload")

    def test_arbitrary_note(self) -> None:
        """An arbitrary note (pitch 48, velocity 100, half) is written and reloads."""
        out = BS.add_midi_note(self.clean, self.clean, self.note,
                               pitch=48, velocity=100, length_ticks=1920000)
        self.assertEqual(_note(out), (48, 1920000, 100))
        self.assertEqual(_reload_ok(out), 0, msg="arbitrary note failed to reload")

    @unittest.skipUnless(_NOTE_NOEDITOR.exists(), "editor-closed control not present")
    def test_editor_closed_ref_suppresses_window(self) -> None:
        """Passing an editor-CLOSED midi_ref strips the ~10 MIDI-editor records from
        0x2587 (0x2582/0x2584/0x2586/0x2583 etc.), so Pro Tools opens to just the Edit
        window. The note still lands and the index stays valid. PT-confirmed."""
        from collections import Counter
        out = BS.add_midi_note(self.clean, self.clean, _load_path(_NOTE_NOEDITOR))
        c = Counter(b.content_type for b in BS.flat_blocks(BS.parse(out)))
        for ct in (0x2582, 0x2584, 0x2586, 0x2583):
            self.assertEqual(c.get(ct, 0), 0, msg=f"editor record 0x{ct:04x} not removed")
        # the editor-OPEN ref keeps those records (sanity: suppression is ref-driven)
        op = BS.add_midi_note(self.clean, self.clean, self.note)
        oc = Counter(b.content_type for b in BS.flat_blocks(BS.parse(op)))
        self.assertEqual(oc.get(0x2586, 0), 1, msg="open ref should keep the editor record")
        self.assertEqual(_note(out), (72, 960000, 80))
        self.assertEqual(_reload_ok(out), 0, msg="editor-closed MIDI note failed to reload")


if __name__ == "__main__":
    unittest.main()
