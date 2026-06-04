# ptxformatwriter

Read **and programmatically construct** Pro Tools session files (`.ptx`) in pure Python.

A `.ptx` reader/parser, and—built on top of it—a control-based **session builder**: a
toolkit for assembling Pro Tools sessions (tracks, audio clips, regions, tempo/meter maps,
markers, a click track, MIDI notes, and the waveform-overview cache), plus a one-call
**beatmap MIDI + stems → finished session** pipeline. Everything the builder produces has
been validated by Pro Tools opening the result.

---

## Reading a session

```python
from ptxformatwriter import PTFFormat

session = PTFFormat()
if session.load("file.ptx", 48000) == 0:          # 0 == ok
    print(session.version(), session.sessionrate())
    for track in session.tracks():
        print(track.name, track.reg.wave.filename)
    for region in session.regions():
        print(region.name, region.startpos, region.length)
    for tempo in session.tempoevents():
        print(tempo.pos, tempo.bpm)
```

`load()` return codes: `0` success · `-1` decrypt/open error · `-2` version-detect error ·
`-3` incompatible version · `-4` parse error.

| PT version | Decrypt | Audio src/region/track | MIDI chunk/region/track |
| --- | --- | --- | --- |
| 5–9 | yes | yes | (8–9: yes) |
| 10–12 | yes | yes (regions: no groups) | yes (regions: no groups) |

---

## Building a session

The builder is control-based: it grows/splices byte-exact content from known-good control
sessions and deterministically rebuilds the master index, so the output is something Pro
Tools actually opens. Compose the individual tools (`ptxformatwriter.workbench` /
`ptxformatwriter.body_synth`):

```python
from ptxformatwriter import workbench as wb     # curated API
data = wb.set_tempo_map(data, [(120.0, 0), (140.0, 1_920_000)])   # inlined template — no donor
data = wb.build_audio_clips(data, tracks)   # clip_ref optional (inlined when omitted)
data = wb.add_click_anyN(data, clean2, click2, at_top=True)
data = wb.set_track_names(data, ["bass", "drums"])
open("out.ptx", "wb").write(wb.encrypt_session_data(data))
```

### Worked example: beatmap MIDI + stems → finished session

A complete end-to-end application built on these tools lives in
[`examples/beatmap_example.py`](examples/beatmap_example.py): give it a beatmap MIDI
(tempo/meter/markers + a `0xFA` head-sync) plus audio stems, and it produces a
self-contained session — named tracks, each stem's clip at the head-sync, the
tempo/meter/marker maps, a click track, and the waveform cache:

```sh
python3 examples/beatmap_example.py song/TempoMap.mid song/synth/MySong.ptx \
    song/bass.mp3 song/drums.mp3 --pack control_files/donors.pack
# -> MySong.ptx + Audio Files/*.wav + WaveCache.wfm
```

(or `import build_session_from_beatmap` / `parse_beatmap_midi` from that file.)

---

## Documentation

- **[`docs/programmatic-session-construction-guide.md`](docs/programmatic-session-construction-guide.md)** — how to build sessions with the library (recipes for every tool).
- **[`docs/ptx-format-spec.md`](docs/ptx-format-spec.md)** — a reverse-engineered, implementation-grade specification of the `.ptx` byte format (XOR layer, blocks, audio linking, the master-index "holes" model, `WaveCache.wfm`, …). No comparable public spec exists.
- **[`docs/gen1-vs-gen2-architecture.md`](docs/gen1-vs-gen2-architecture.md)** — why the index is rebuilt deterministically (the "holes" model) rather than by guessing offsets.

---

## Repository layout

- `ptxformatwriter/` — the library:
  - `core` (reader), `writer` (low-level `.ptx` I/O primitives),
  - `body_synth` (the session-construction toolkit), `donorpack` (bundle donors + the
    `Controls` build bundle),
  - `wavecache` (`WaveCache.wfm` generation), `final_index` (deterministic master-index
    repair), `workbench` (the curated public API), `click_clone` (click-track cloning).
- `examples/` — worked applications built on the library, e.g. `beatmap_example.py`
  (beatmap MIDI + audio stems → a finished session).
- `unused/` — **deprecated, archived for reference**: the earlier spec-based "writer"
  engine (`writer_legacy`, `write_audio_session` / `AudioSessionSpec`), plus `audit`,
  `mixed_order`, `midi`, the CLI, their tests, and the decode-history notes. See
  `docs/gen1-vs-gen2-architecture.md` for why it was superseded.
- `docs/` — the documentation above.
- `control_files/` — local-only byte templates and test fixtures (git-ignored).

---

## Tests

```sh
python3 -m unittest discover -s tests
```

(Many tests `skipUnless` their local `control_files/` fixtures are present.)

---

## License

MIT — see [`LICENSE`](LICENSE).

---

## Special thanks

Deep thanks to **Damien Zammit** and **Robin Gareus** for the `ptformat` C++ library and
reverse-engineering the Pro Tools session format!