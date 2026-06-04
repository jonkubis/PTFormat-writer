# control_files/

Donor/control `.ptx` sessions (plus a few WAVs) that the builder splices byte-exact content
from. Pro Tools is strict, so sessions are grown from these known-good controls rather than
synthesized blank — see [`../docs/programmatic-session-construction-guide.md`](../docs/programmatic-session-construction-guide.md).

**Only a minimal subset is committed here** (the larger local library of controls is
git-ignored). What's bundled is enough to:

- **build sessions** — the clean N-stereo scaffolds `lots of stereo tracks/{1..8} stereo
  tracks.ptx`, the click pair `2 stereo plus click.ptx`, and a BWF WAV template
  `lots of stereo tracks/Audio Files/01.wav`;
- **regenerate the inlined byte templates** (`ptxformatwriter/_templates.py`) via
  `donorpack.write_inline_templates` — the clip donor `3 stereo 3 different clips.ptx`,
  the marker donor `a few named markers.ptx`, the tempo donor `various/120 to 140bpm.ptx`,
  and the meter donor `various/3-4 meter at bar 2.ptx`;
- **run the donor-pack integration test** — the clip WAVs `Audio Files/{0102,0277,0496}.wav`.

Need more track counts? Author another `N stereo tracks.ptx` in Pro Tools and drop it in
`lots of stereo tracks/`; `donorpack.build_pack` bundles whatever sizes it finds. (The
stereo "overview order" has no closed form, so each track count needs its own scaffold.)
Other tests (`test_clip`, `test_tempo_meter`, …) `skipUnless` their byte-exact control
pairs are present; those fixtures are not bundled here.
