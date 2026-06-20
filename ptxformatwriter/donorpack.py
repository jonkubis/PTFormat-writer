"""Donor pack — bundle the few control/donor sessions a build still needs into ONE file.

Most of a Pro Tools session is now built from INLINED byte templates (`body_synth._templates`
— clip/region, tempo, meter, and marker chunks, each <=512 B). The only donors a build still
needs are the ones too large or order-sensitive to inline: the clean N-stereo scaffolds, a
click pair, and a BWF WAV to wrap audio in.

A *donor pack* is a single zip that bundles those (de-obfuscated, by role) plus a manifest.
`DonorPack.load(path).controls()` expands it back into the `Controls` bundle a build takes
— so at build time you carry **one file**.

The pack is a *build artifact*: the source donor `.ptx` files remain the truth (re-authorable
in Pro Tools), and `build_pack()` regenerates the pack from them. The INLINED templates are
also regenerable from their source donors via `write_inline_templates()`. To update for a new
Pro Tools version: re-author the source donors, re-run `build_pack` + `write_inline_templates`,
re-run the tests.

Notes:
- The clean 2-stereo scaffold doubles as the Click track's clean-ref (no separate entry).
- The N-stereo family is bundled per size: the stereo "overview order" is a hash-table
  iteration order with no closed form, so each track count needs its own control (it cannot
  be grown from a smaller donor). `build_pack` bundles whatever sizes it finds.
"""
from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import body_synth as B, writer as W


@dataclass
class Controls:
    """The donor bundle a build draws from (de-obfuscated `.ptx`/WAV byte templates).

    Tempo, meter, markers, and clips are ALL built from INLINED byte templates
    (`body_synth._templates`), so the only donors a build needs are the clean N-stereo
    scaffold, a BWF WAV to wrap audio in, and (optionally) the click pair. Produced by
    `DonorPack.controls()` / `load_controls()`; consumed by the beatmap pipeline (see
    `examples/beatmap_example.py`). (Pass `tempo_ref`/`meter_ref` DIRECTLY to
    `set_tempo_map`/`set_meter_map` to reproduce a control byte-exact; that override path
    doesn't go through `Controls`.)"""
    nstereo: "callable"   # n -> bytes (clean N-stereo session)
    wav_template: bytes   # a PT/BWF WAV (44.1k/24-bit/stereo) to wrap raw ffmpeg output in
    clip_ref: "bytes | None" = None  # clip byte templates; None uses the INLINED templates
    # (`body_synth._templates`), so no clip donor is needed — pass a control only to override.
    click_ref: "bytes | None" = None  # a 2-stereo+click control (e.g. `2 stereo plus click.ptx`);
    # paired with nstereo(2), it supplies the default top Click track. None disables the click.

PACK_VERSION = 1

# role -> source path (relative to the control_files root). The N-stereo family is handled
# separately (one entry per available size).
_FEATURE_ROLES = {
    "click_ref":    "lots of stereo tracks/2 stereo plus click.ptx",
    # NOTE: the clip, marker, tempo AND meter templates are now INLINED (body_synth._templates),
    # so their donors ('3 stereo 3 different clips.ptx', 'clip baseline.ptx', 'a few named
    # markers.ptx', 'Untitled.ptx', '120 to 140bpm.ptx', '3-4 meter at bar 2.ptx') are no longer
    # bundled — the build tools grow the inlined chunks. Those donor SOURCES remain referenced by
    # `write_inline_templates` so the inlined copies stay regenerable.
}
_WAV_ROLE = "wav_template"
_WAV_SRC = "lots of stereo tracks/Audio Files/01.wav"
_NSTEREO_DIR = "lots of stereo tracks"
_CLICK_CLEAN_N = 2   # the bundled N-stereo size reused as the Click track's clean-ref


def build_pack(control_root, out_path, *, nstereo_sizes=None) -> Path:
    """Build a donor pack zip at `out_path` from the source donors under `control_root`.

    `nstereo_sizes` defaults to every `N stereo tracks.ptx` found under
    `<control_root>/lots of stereo tracks/`. Stores de-obfuscated session bytes by role +
    a manifest recording each role's source path (so the pack is fully reconstructable)."""
    control_root = Path(control_root)
    sdir = control_root / _NSTEREO_DIR
    if nstereo_sizes is None:
        nstereo_sizes = sorted(int(p.stem.split()[0]) for p in sdir.glob("* stereo tracks.ptx")
                               if p.stem.split()[0].isdigit())
    manifest = {"version": PACK_VERSION, "nstereo_sizes": nstereo_sizes,
                "click_clean_n": _CLICK_CLEAN_N, "sources": {}}
    out_path = Path(out_path)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for n in nstereo_sizes:
            rel = f"{_NSTEREO_DIR}/{n} stereo tracks.ptx"
            z.writestr(f"nstereo/{n}.bin", W.load_unxored(str(control_root / rel)))
            manifest["sources"][f"nstereo_{n}"] = rel
        for role, rel in _FEATURE_ROLES.items():
            z.writestr(f"{role}.bin", W.load_unxored(str(control_root / rel)))
            manifest["sources"][role] = rel
        z.writestr(f"{_WAV_ROLE}.wav", (control_root / _WAV_SRC).read_bytes())
        manifest["sources"][_WAV_ROLE] = _WAV_SRC
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
    return out_path


_CLIP_DONOR = "lots of stereo tracks/3 stereo 3 different clips.ptx"
_MARKER_DONOR = "lots of stereo tracks/a few named markers.ptx"
_TEMPO_DONOR = "various/120 to 140bpm.ptx"
_METER_DONOR = "various/3-4 meter at bar 2.ptx"


def write_inline_templates(control_root, out_path="ptxformatwriter/_templates.py") -> Path:
    """Regenerate `ptxformatwriter/_templates.py` (the inlined <=512 B clip byte templates) by
    re-extracting them from the clip control donor under `control_root`. Run this after
    re-authoring the source donors so the inlined copies stay in sync."""
    control_root = Path(control_root)
    items = []  # (constant_name, bytes, description)
    # clip byte templates (from the clip donor)
    clip_descs = ["0x1003 wav descriptor",
                  "0x2629 region (.L template; renamed + findex/length/channel/GUID patched per clip)",
                  "0x0F3D audio-files path block",
                  "clip lane tail: <count:4> + 0x1050{0x104f} + 2-byte lane trailer",
                  "0x1004 file-table 9-byte header", "0x1004 file-table trailer",
                  "0x262A region-list 9-byte header",
                  "0x103a folder-path trailer (component indices renumbered per D at build)"]
    items += list(zip(B._CLIP_TEMPLATE_NAMES,
                      B._extract_clip_templates(W.load_unxored(str(control_root / _CLIP_DONOR))),
                      clip_descs))
    # marker template: a minimal 1-record 0x2030 (resized to N markers at build — byte-identical
    # to the full donor block for any marker list, since only the first 0x2077 record templates).
    mr = W.load_unxored(str(control_root / _MARKER_DONOR))
    m2030 = B.block_bytes(mr, [b for b in B.parse(mr).blocks if b.content_type == 0x2030][0])
    items.append(("MARKER_BLOCK_TEMPLATE", B._resize_marker_block(m2030, [("M", 0)]),
                  "0x2030 marker list (minimal 1-record template; resized to N markers at build)"))
    # tempo + meter templates: the map + ruler blocks, each resized to a minimal 1-record
    # template (resized to N events at build — byte-identical to the full donor block for any
    # event list, since only the first record templates). The tools grow ONLY these blocks.
    def _blk(d, ct):
        return B.block_bytes(d, [b for b in B.parse(d).blocks if b.content_type == ct][0])
    tr = W.load_unxored(str(control_root / _TEMPO_DONOR))
    items.append(("TEMPO_MAP_TEMPLATE", B._resize_tempo_block(_blk(tr, 0x2028), 1),
                  "0x2028 tempo map (minimal 1-record template; resized to N tempo events)"))
    items.append(("TEMPO_RULER_TEMPLATE", B._resize_tempo_block(_blk(tr, 0x2718), 1),
                  "0x2718 tempo ruler (minimal 1-record template; resized to N tempo events)"))
    me = W.load_unxored(str(control_root / _METER_DONOR))
    items.append(("METER_MAP_TEMPLATE", B._resize_meter_block(_blk(me, 0x2029), 1),
                  "0x2029 meter map (minimal 1-record template; resized to N meter events)"))
    items.append(("METER_RULER_TEMPLATE", B._resize_meter_block(_blk(me, 0x2719), 1),
                  "0x2719 meter ruler (minimal 1-record template; resized to N meter events)"))
    lines = ['"""Inlined byte templates (<=512 B) for session construction — no external donor needed.',
             '',
             'These chunks are extracted verbatim from control donors so the construction tools can',
             'run without carrying the full donor sessions. GENERATED by',
             '`donorpack.write_inline_templates(control_root)`; regenerate after re-authoring the',
             f"source donors. Sources: '{_CLIP_DONOR}', '{_MARKER_DONOR}', '{_TEMPO_DONOR}',",
             f"'{_METER_DONOR}'.", '"""', '']
    for name, b, desc in items:
        h = b.hex()
        lines.append(f"# {desc} ({len(b)} B)")
        lines.append(f"{name} = bytes.fromhex(")
        lines += [f'    "{h[i:i + 96]}"' for i in range(0, len(h), 96)]
        lines += [")", ""]
    out = Path(out_path)
    out.write_text("\n".join(lines) + "\n")
    return out


_STEREO_SCAFFOLD_SRC = "lots of stereo tracks/8 stereo tracks.ptx"
_STEREO_TEMPLATE_SRC = "lots of stereo tracks/512 stereo tracks.ptx"


def write_stereo_data(control_root, out_path="ptxformatwriter/_stereo_data.py") -> Path:
    """Regenerate `ptxformatwriter/_stereo_data.py` — the inlined assets that let
    `body_synth.synthesize_stereo_inline` build N-stereo sessions with NO external control
    files. Bundles an 8-track scaffold (base_n=8; its `0x261c` layout matches the 512) plus
    the track-2 (single-digit) and track-10 (double-digit) per-track unit templates from the
    512-track control. `synth_stereo_unit` re-keys these per index; the only fields it can't
    derive (an 8-byte handle + a 160 B blob) are PT-confirmed self-contained (need only be
    present). Run after re-authoring the source controls, then re-run the tests."""
    import base64
    import zlib
    control_root = Path(control_root)
    scaffold = W.load_unxored(str(control_root / _STEREO_SCAFFOLD_SRC))
    lib512 = W.load_unxored(str(control_root / _STEREO_TEMPLATE_SRC))

    def _ser(u) -> bytes:
        blks = [u.b1014, u.b1052[0], u.b1052[1], u.b251a[0], u.b251a[1],
                u.b210b, u.b261c, u.b2589, u.name_entry]
        return b"".join(len(b).to_bytes(4, "little") + b for b in blks)

    def _enc(b: bytes) -> str:
        return base64.b64encode(zlib.compress(b, 9)).decode()

    t1 = _ser(B.extract_track(lib512, 2, 512))     # 1-digit name template (tracks 1-9)
    t2 = _ser(B.extract_track(lib512, 10, 512))    # 2-digit name template (tracks 10-99)
    t3 = _ser(B.extract_track(lib512, 100, 512))   # 3-digit name template (tracks 100-512)
    body = (
        '"""GENERATED stereo-synthesis assets — inlined so N-stereo sessions build with NO external\n'
        'control files. Regenerate via `donorpack.write_stereo_data(control_root)` after re-authoring the\n'
        "source controls. Sources: '8 stereo tracks.ptx' (scaffold, base_n=8, Mac/512-compatible 0x261c\n"
        "layout) + '512 stereo tracks.ptx' (track-2/10/100 = 1/2/3-digit unit templates, for tracks\n"
        "1-9 / 10-99 / 100-512). The per-track GUID + 160 B blob are PT-confirmed self-contained.\"\"\"\n"
        "import base64 as _b64, zlib as _z\n"
        "from . import body_synth as _B\n\n"
        f'_SCAFFOLD = "{_enc(scaffold)}"\n'
        f'_TMPL1 = "{_enc(t1)}"\n'
        f'_TMPL2 = "{_enc(t2)}"\n'
        f'_TMPL3 = "{_enc(t3)}"\n\n'
        "def _dec(s): return _z.decompress(_b64.b64decode(s))\n"
        "def _unit(blob):\n"
        "    o = 0; bs = []\n"
        "    for _ in range(9):\n"
        '        n = int.from_bytes(blob[o:o + 4], "little"); o += 4; bs.append(blob[o:o + n]); o += n\n'
        "    return _B.StereoTrackUnit(b1014=bs[0], b1052=(bs[1], bs[2]), b251a=(bs[3], bs[4]),\n"
        "                              b210b=bs[5], b261c=bs[6], b2589=bs[7], name_entry=bs[8])\n\n"
        "def stereo_scaffold(): return _dec(_SCAFFOLD)          # 8-track Mac clean session (base_n=8)\n"
        "def stereo_templates():                                # (1-digit, 2-digit, 3-digit)\n"
        "    return _unit(_dec(_TMPL1)), _unit(_dec(_TMPL2)), _unit(_dec(_TMPL3))\n"
    )
    out = Path(out_path)
    out.write_text(body)
    return out


class DonorPack:
    """A loaded donor pack. Use :meth:`controls` to get a `Controls` bundle."""

    def __init__(self, entries: "dict[str, bytes]", manifest: dict):
        self._entries = entries
        self.manifest = manifest

    @classmethod
    def load(cls, path) -> "DonorPack":
        with zipfile.ZipFile(path) as z:
            manifest = json.loads(z.read("manifest.json"))
            entries = {name: z.read(name) for name in z.namelist() if name != "manifest.json"}
        return cls(entries, manifest)

    def nstereo(self, n: int) -> bytes:
        """The clean `n`-stereo scaffold (de-obfuscated). Each size is bundled verbatim."""
        key = f"nstereo/{n}.bin"
        if key not in self._entries:
            have = self.manifest.get("nstereo_sizes", [])
            raise KeyError(f"pack has no {n}-stereo scaffold (bundled sizes: {have}); "
                           f"rebuild the pack with that size or add the control")
        return self._entries[key]

    def controls(self) -> "Controls":
        """Expand the pack into the `Controls` bundle a build takes. The bundled 2-stereo
        scaffold is reused as the Click track's clean-ref. Tempo/meter/marker/clip templates
        are inlined, so only the click pair + WAV are carried as feature donors."""
        return Controls(
            nstereo=self.nstereo,
            wav_template=self._entries[f"{_WAV_ROLE}.wav"],
            clip_ref=self._entries.get("clip_ref.bin"),   # None -> inlined clip templates
            click_ref=self._entries["click_ref.bin"],
        )


def load_controls(path) -> "Controls":
    """Convenience: load a donor pack and return its `Controls` bundle."""
    return DonorPack.load(path).controls()


if __name__ == "__main__":   # python -m ptxformatwriter.donorpack [control_root] [out.pack]
    import sys
    root = sys.argv[1] if len(sys.argv) > 1 else "control_files"
    out = sys.argv[2] if len(sys.argv) > 2 else "control_files/donors.pack"
    p = build_pack(root, out)
    print(f"wrote {p} ({p.stat().st_size} bytes; sizes "
          f"{json.loads(__import__('zipfile').ZipFile(p).read('manifest.json'))['nstereo_sizes']})")
