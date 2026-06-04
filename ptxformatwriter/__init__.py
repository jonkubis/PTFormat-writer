"""ptxformatwriter — read and programmatically construct Pro Tools session (`.ptx`) files.

Sessions are built by growing and splicing byte-exact content from known-good control
sessions — the approach Pro Tools itself round-trips cleanly. High-level surface:

- ``ptxformatwriter.workbench`` — the curated session-construction toolkit: add/rename audio &
  MIDI clips, tempo/meter/marker maps, a click track, track names, and from-scratch
  mono/stereo/mixed synthesis (all re-exported here for ``import ptxformatwriter as ptx``).
- ``ptxformatwriter.wavecache`` — generate Pro Tools waveform-overview caches (``WaveCache.wfm``)
  so sessions open with waveforms drawn.
- ``ptxformatwriter.donorpack`` — bundle the donors a build needs into one regenerable file.
- ``ptxformatwriter.core`` — the low-level ``.ptx`` reader/parser.
- ``ptxformatwriter.writer`` — low-level I/O primitives (XOR (de)obfuscation, block parsing,
  master-index repair) the above build on.

A worked end-to-end application — beatmap MIDI + audio stems → a finished session — lives
outside the library, in ``examples/beatmap_example.py``.
"""
from . import body_synth, core, donorpack, wavecache, workbench, writer
from .donorpack import Controls, DonorPack, build_pack, load_controls, write_inline_templates
from .core import (
    Block,
    MeterEvent,
    MidiEvent,
    MidiPlacement,
    PTFFormat,
    PTFParseError,
    Region,
    TempoEvent,
    Track,
    Wav,
)
from .workbench import *  # noqa: F401,F403  — the curated session-construction API
from .workbench import __all__ as _workbench_all

__all__ = [
    # submodules
    "body_synth", "core", "donorpack", "wavecache", "workbench", "writer",
    # donor pack (bundle all donors into one regenerable file) + the donor bundle it yields
    "Controls", "DonorPack", "build_pack", "load_controls", "write_inline_templates",
    # core reader types
    "Block", "MeterEvent", "MidiEvent", "MidiPlacement", "PTFFormat",
    "PTFParseError", "Region", "TempoEvent", "Track", "Wav",
    # curated construction API (from workbench)
    *_workbench_all,
]
