"""The curated `ptxformatwriter.workbench` surface re-exports the preferred session-construction
API (from-scratch synthesis and the low-level I/O primitive). The beatmap MIDI+stems
application is no longer part of the library — it lives in `examples/beatmap_example.py`."""
import unittest

from ptxformatwriter import writer, workbench
from ptxformatwriter.body_synth import synthesize_stereo_session


class ApiLayerTests(unittest.TestCase):
    def test_workbench_exposes_preferred_session_construction_surface(self):
        self.assertIs(workbench.synthesize_stereo_session, synthesize_stereo_session)
        self.assertIs(workbench.encrypt_session_data, writer.encrypt_session_data)

    def test_beatmap_pipeline_is_not_in_the_library(self):
        """The beatmap application moved out of the package; neither the workbench nor the
        top-level namespace re-exports it (it's `examples/beatmap_example.py` now)."""
        import ptxformatwriter as ptx
        self.assertFalse(hasattr(workbench, "build_session_from_beatmap"))
        self.assertFalse(hasattr(ptx, "beatmap"))
        self.assertFalse(hasattr(ptx, "build_session_from_beatmap"))
