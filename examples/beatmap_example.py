"""Example: beatmap MIDI + audio stems -> one self-contained Pro Tools `.ptx`.

This is an *application* built on top of `ptxformatwriter` — not part of the library itself.
It takes a beatmapping-software MIDI export (tempo map, meter map, named markers, and a
`0xFA` Start message marking the audio head-sync) plus a set of MP3s, and produces ONE
self-contained session: N stereo tracks (one MP3 clip each, all starting at the head-sync),
the tempo/meter maps, the markers, and an optional click track on top.

It is pure glue over the library's construction tools (`body_synth`, `wavecache`, `writer`).
The only external dependency is `ffmpeg` on `PATH` (MP3 -> 44.1k/24-bit/stereo WAV).
Conductor positions come from the MIDI as PT ticks (960000/quarter); the clip head-sync
comes from integrating the tempo map to seconds, then to file samples.

Run it directly:

    python3 examples/beatmap_example.py song/TempoMap.mid out/MySong.ptx \
        song/bass.mp3 song/drums.mp3 --pack control_files/donors.pack

or import `build_session_from_beatmap` / `parse_beatmap_midi` from this module.
"""
from __future__ import annotations

import os
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Make the sibling `ptxformatwriter` package importable when this file is run as a script
# (`python3 examples/beatmap_example.py …`) rather than installed.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ptxformatwriter import body_synth as B, wavecache as WC, writer as W  # noqa: E402
from ptxformatwriter.donorpack import Controls  # noqa: E402  (the donor bundle a build takes)

PT_TICKS_PER_QUARTER = 960000


@dataclass
class Beatmap:
    division: int                              # MIDI ticks per quarter
    tempos: list = field(default_factory=list)  # [(midi_tick, bpm)]
    meters: list = field(default_factory=list)  # [(midi_tick, numerator, denominator)]
    markers: list = field(default_factory=list)  # [(midi_tick, name)]
    head_sync_tick: "int | None" = None         # the 0xFA Start tick

    def pt_tick(self, midi_tick: int) -> int:
        return midi_tick * (PT_TICKS_PER_QUARTER // self.division)

    def seconds_at(self, midi_tick: int) -> float:
        """Integrate the tempo map to `midi_tick` -> seconds."""
        secs = 0.0; cur_t = 0; cur_us = 500000.0  # default 120 bpm
        for et, bpm in sorted(self.tempos):
            if et >= midi_tick:
                break
            secs += (et - cur_t) * cur_us / 1e6 / self.division
            cur_t, cur_us = et, 60e6 / bpm
        secs += (midi_tick - cur_t) * cur_us / 1e6 / self.division
        return secs


def _varlen(b: bytes, i: int):
    v = 0
    while True:
        c = b[i]; i += 1; v = (v << 7) | (c & 0x7F)
        if not (c & 0x80):
            return v, i


def parse_beatmap_midi(path) -> Beatmap:
    """Parse a Standard MIDI File: tempo (FF 51), time-signature (FF 58), marker (FF 06)
    meta events, and the 0xFA Start real-time message (the head-sync). All ticks are
    absolute MIDI ticks. Handles running status."""
    d = Path(path).read_bytes()
    if d[:4] != b"MThd":
        raise ValueError("not a Standard MIDI File")
    hlen = struct.unpack(">I", d[4:8])[0]
    _fmt, _ntrk, division = struct.unpack(">HHH", d[8:14])
    bm = Beatmap(division=division)
    pos = 8 + hlen
    while pos + 8 <= len(d) and d[pos:pos + 4] == b"MTrk":
        tlen = struct.unpack(">I", d[pos + 4:pos + 8])[0]
        end = pos + 8 + tlen
        i = pos + 8; t = 0; status = None
        while i < end:
            dt, i = _varlen(d, i); t += dt
            b0 = d[i]
            if b0 == 0xFF:
                mtype = d[i + 1]; ln, j = _varlen(d, i + 2); data = d[j:j + ln]; i = j + ln
                if mtype == 0x51:
                    bm.tempos.append((t, 60e6 / struct.unpack(">I", b"\x00" + data)[0]))
                elif mtype == 0x58:
                    bm.meters.append((t, data[0], 2 ** data[1]))
                elif mtype == 0x06:
                    bm.markers.append((t, data.decode("latin1")))
            elif b0 in (0xF0, 0xF7):
                ln, j = _varlen(d, i + 1); i = j + ln
            elif b0 >= 0xF8:
                if b0 == 0xFA and bm.head_sync_tick is None:
                    bm.head_sync_tick = t
                i += 1
            else:
                if b0 & 0x80:
                    status = b0; i += 1
                i += 1 if (status & 0xF0) in (0xC0, 0xD0) else 2
        pos = end
    if not bm.tempos:
        bm.tempos = [(0, 120.0)]
    if not bm.meters:
        bm.meters = [(0, 4, 4)]
    return bm


def convert_to_wav(mp3_path, out_wav, *, sample_rate: int = 44100, bits: int = 24) -> None:
    """MP3 -> WAV at `sample_rate`/`bits`-bit/stereo via ffmpeg (overwrites)."""
    codec = {16: "pcm_s16le", 24: "pcm_s24le", 32: "pcm_s32le"}[bits]
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3_path),
         "-ar", str(sample_rate), "-ac", "2", "-c:a", codec, str(out_wav)],
        check=True)


def _stem_name(mp3_path) -> str:
    """A track/clip name from an MP3 filename (its basename without extension)."""
    return os.path.splitext(os.path.basename(str(mp3_path)))[0]


def build_session_from_beatmap(midi_path, mp3_paths, out_ptx, controls: Controls,
                               *, sample_rate: int = 44100, track_names=None,
                               clip_names=None, click: bool = True) -> Beatmap:
    """Build a self-contained Pro Tools session at `out_ptx` from a beatmap MIDI + MP3s.

    Converts each MP3 -> a 44.1k/24-bit/stereo WAV in `out_ptx`'s `Audio Files/` folder,
    builds N stereo tracks (named after `track_names` or the MP3 stems), writes the tempo
    map / meter map / markers from the MIDI, places each MP3's clip on its track at the
    0xFA head-sync, and (when `click` and `controls.click_ref` are set) adds a Click track as
    the first/top track. Returns the parsed Beatmap. The .ptx + Audio Files (+ WaveCache.wfm)
    are self-contained at `out_ptx.parent`.

    Build order: tempo/meter/markers -> clips -> click -> track-names -> waveform-view.
    (Renaming last is no longer required — `add_click_anyN` is now name-robust: it normalizes
    the audio tracks to `Audio N` for the click splice and restores the names after, so the
    click lands correctly regardless of when tracks were renamed. The order is just a tidy
    default. The final `set_waveform_view` undoes the click splice's view-state corruption,
    which otherwise leaves a track in PT's "volume" view instead of "waveform".)"""
    out_ptx = Path(out_ptx)
    bm = parse_beatmap_midi(midi_path)
    n = len(mp3_paths)
    names = track_names or [_stem_name(p) for p in mp3_paths]

    data = controls.nstereo(n)
    data = B.set_tempo_map(data, [(bpm, bm.pt_tick(t)) for t, bpm in bm.tempos])
    data = B.set_meter_map(data, [(num, den, bm.pt_tick(t)) for t, num, den in bm.meters])
    if bm.markers:
        data = B.set_markers(data, [(name, bm.pt_tick(t)) for t, name in bm.markers])

    audio_dir = out_ptx.parent / "Audio Files"
    audio_dir.mkdir(parents=True, exist_ok=True)
    head_samples = round(bm.seconds_at(bm.head_sync_tick or 0) * sample_rate)
    tracks, wav_files = [], []
    for idx, mp3 in enumerate(mp3_paths):
        wav = audio_dir / (_stem_name(mp3) + ".wav")
        convert_to_wav(mp3, wav, sample_rate=sample_rate)
        # ffmpeg output is a raw WAV (no PT/BWF umid) -> wrap it (unique deterministic
        # UMID per stem) so build_audio_clips can link it.
        wrapped = B.wrap_raw_wav(wav.read_bytes(), controls.wav_template, seed=f"{_stem_name(mp3)}-{idx}")
        wav.write_bytes(wrapped)
        wav_files.append(wav)
        cname = (clip_names[idx] if clip_names else None)
        clip = (str(wav), head_samples) if cname is None else (str(wav), head_samples, cname)
        tracks.append([clip])
    data = B.build_audio_clips(data, tracks, controls.clip_ref)
    if click and controls.click_ref is not None:
        # add a Click track as the FIRST (top) track, sourced from a 2-stereo clean/click
        # control pair (layout-independent). Done before renaming (see ORDER note above).
        data = B.add_click_anyN(data, controls.nstereo(2), controls.click_ref, at_top=True)
    data = B.set_track_names(data, names)
    # The click splice can leave a track in PT's "volume" view instead of "waveform" — force
    # every track back to waveform so stems draw their waveforms on open (no-op without a click).
    data = B.set_waveform_view(data)

    out_ptx.write_bytes(W.encrypt_session_data(data))
    # ship a precomputed waveform-overview cache so Pro Tools draws waveforms on first
    # open (it only builds WaveCache.wfm on Import/Recalculate, never on plain Open).
    WC.write_wavecache(out_ptx.parent, [str(w) for w in wav_files])
    return bm


def _controls_from_control_root(root) -> Controls:
    """Build a `Controls` bundle from a loose `control_files/`-style directory (no pack)."""
    root = Path(root)
    ster = root / "lots of stereo tracks"
    return Controls(
        nstereo=lambda n: W.load_unxored(str(ster / f"{n} stereo tracks.ptx")),
        wav_template=(ster / "Audio Files" / "01.wav").read_bytes(),
        click_ref=W.load_unxored(str(ster / "2 stereo plus click.ptx")),
    )


def _main(argv=None) -> int:
    import argparse
    from ptxformatwriter.donorpack import load_controls

    ap = argparse.ArgumentParser(description="Build a Pro Tools .ptx from a beatmap MIDI + MP3 stems.")
    ap.add_argument("midi", help="Standard MIDI file (tempo/meter/markers + 0xFA head-sync)")
    ap.add_argument("out_ptx", help="output .ptx path (Audio Files/ + WaveCache.wfm go beside it)")
    ap.add_argument("mp3s", nargs="+", help="one or more audio stems (one stereo track each)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--pack", help="a donor pack (.pack) built by ptxformatwriter.build_pack")
    src.add_argument("--control-root", help="a loose control_files/ directory")
    ap.add_argument("--no-click", action="store_true", help="don't add a top Click track")
    ap.add_argument("--names", nargs="*", help="track names (default: the MP3 stem names)")
    args = ap.parse_args(argv)

    controls = (load_controls(args.pack) if args.pack
                else _controls_from_control_root(args.control_root))
    bm = build_session_from_beatmap(
        args.midi, args.mp3s, args.out_ptx, controls,
        track_names=args.names, click=not args.no_click)
    print(f"wrote {args.out_ptx}  (tempos={len(bm.tempos)} meters={len(bm.meters)} "
          f"markers={len(bm.markers)} head_sync_tick={bm.head_sync_tick})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
