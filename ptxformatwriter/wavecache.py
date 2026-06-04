"""Pro Tools waveform-overview cache (``WaveCache.wfm``) generator.

Pro Tools stores waveform overviews in a session-folder sidecar ``WaveCache.wfm`` (a
``DDZCHX`` container), keyed by each audio file's UMID + filename. It builds this cache
only on Import / Recalculate Waveform Overviews — NEVER on a plain session Open — so a
synthesized ``.ptx`` shows blank waveforms until the user forces a recompute. Writing a
correct ``WaveCache.wfm`` next to the ``.ptx`` makes the session open with waveforms
immediately, with no manual step.

Format (reverse-engineered against Pro Tools; peak bytes validated byte-exact):

    header(80) | 12 zero bytes | per-file data block * N | CAHIDX index

  * Per-file block = ``AnalysisSetsHdr``{ count=nchannels, ``AnalysisSet`` * nch, 12-byte
    trailer } + an ``AnalysisSetsHdr`` footer(16) + 8 zero bytes.
  * Per channel (``AnalysisSet``) = ``PacketStreamSetHdr``{ 2 * ``PacketStreamIndxHdr`` +
    2 * ``PacketStreamIndx`` + 2 * ``PacketStreamData`` } for the two zoom levels
    (256 and 16384 samples per overview point).
  * Each overview point = ``(max:int16, min:int16)`` where the int16 is
    ``clip(round(sample_24bit / 256))``. Point count = ``ceil(nsamples / spp)`` (the final
    partial window still yields a point).
  * Every record is ``name + ver:u16 + size:u32 + payload[size]`` (size = payload length).
  * ``CAHIDX`` index entry = ``7*00 + umid(8) + 00 + namelen:u32 + name + ft:u64 + ft:u64 +
    filesize:u64 + 8*00 + data_offset:u32 + 4*00 + data_length:u32 + ff*8``.

Use :func:`build_wavecache_for_wavs` to produce the bytes for a set of staged WAVs and
write them to ``<session>/WaveCache.wfm``.
"""
from __future__ import annotations

import os
import struct
from pathlib import Path

import numpy as np

LEVELS = (256, 16384)  # samples per overview point: [fine, coarse]
_FILETIME_EPOCH = 11644473600  # seconds between 1601-01-01 and 1970-01-01

# constant 16-byte stream-type id per zoom level (IndxHdr payload words 0..3)
_STREAM_ID = {
    256:   bytes.fromhex("01140e5a") + bytes.fromhex("0ef5ac46") + bytes.fromhex("4935af34") + bytes.fromhex("f2a42a91"),
    16384: bytes.fromhex("01140e5a") + bytes.fromhex("3ef5ac46") + bytes.fromhex("4935af34") + bytes.fromhex("f2a42a91"),
}


def _record(name: bytes, ver: int, payload: bytes) -> bytes:
    """A WaveCache record: name + ver(u16) + size(u32) + payload (size = len(payload))."""
    return name + struct.pack("<HI", ver, len(payload)) + payload


def _file_filetime(path) -> int:
    """A file's mtime as a Windows FILETIME (100-ns ticks since 1601-01-01 UTC)."""
    return round((os.path.getmtime(path) + _FILETIME_EPOCH) * 10_000_000)


def _read_wav_channels(path) -> "tuple[list[np.ndarray], int]":
    """Read a PCM WAV into a list of per-channel int32 sample arrays (+ frame count).
    Supports 16/24/32-bit little-endian PCM."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError(f"not a RIFF/WAVE file: {path}")
    i, fmt, dat = 12, None, None
    while i + 8 <= len(data):
        cid = data[i:i + 4]; sz = struct.unpack_from("<I", data, i + 8 - 4)[0]
        if cid == b"fmt ":
            fmt = data[i + 8:i + 8 + sz]
        elif cid == b"data":
            dat = (i + 8, sz)
        i += 8 + sz + (sz & 1)
    if fmt is None or dat is None:
        raise ValueError(f"WAV missing fmt/data: {path}")
    nch, _sr = struct.unpack_from("<HI", fmt, 2)
    bits = struct.unpack_from("<H", fmt, 14)[0]
    doff, dsz = dat
    raw = data[doff:doff + dsz]
    if bits == 24:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        s = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        s = np.where(s & 0x800000, s - 0x1000000, s)
    elif bits == 16:
        s = np.frombuffer(raw, dtype="<i2").astype(np.int32)
    elif bits == 32:
        s = np.frombuffer(raw, dtype="<i4").astype(np.int32) >> 8  # 32->24-bit domain
    else:
        raise ValueError(f"unsupported bit depth {bits} in {path}")
    s = s[:(len(s) // nch) * nch].reshape(-1, nch)
    return [s[:, c] for c in range(nch)], s.shape[0]


def _peak_bytes(channel: np.ndarray, spp: int) -> bytes:
    """Overview peaks for one channel at `spp` samples/point: (max,min) int16 per point,
    ceil(n/spp) points, int16 = clip(round(sample/256)). Returns the raw peak bytes."""
    n = len(channel)
    npts = -(-n // spp)  # ceil
    full = n // spp
    mx = np.empty(npts, dtype=np.float64)
    mn = np.empty(npts, dtype=np.float64)
    if full:
        w = channel[:full * spp].reshape(full, spp)
        mx[:full] = w.max(axis=1); mn[:full] = w.min(axis=1)
    if full < npts:  # final partial window
        last = channel[full * spp:]
        mx[full] = last.max(); mn[full] = last.min()
    mx16 = np.clip(np.round(mx / 256.0), -32768, 32767).astype("<i2")
    mn16 = np.clip(np.round(mn / 256.0), -32768, 32767).astype("<i2")
    return np.stack([mx16, mn16], axis=1).tobytes()  # (max, min) interleaved


def _build_channel(ch_index: int, peaks_per_level: "list[bytes]") -> bytes:
    """Build one AnalysisSet (one audio channel) wrapping its per-level PacketStream
    records. `peaks_per_level` is the raw peak bytes for each LEVELS entry. The IndxHdr
    `w11` field (absolute peak offset) is left 0 here and patched after final assembly."""
    fine_size = len(peaks_per_level[0])
    indxhdrs, indxs, datas = [], [], []
    for li, spp in enumerate(LEVELS):
        pk = peaks_per_level[li]
        npts = len(pk) // 4
        w5 = 98 if li == 0 else 82 + fine_size
        payload = (_STREAM_ID[spp]
                   + struct.pack("<II", 0, w5)
                   + struct.pack("<I", len(pk))           # w6 = data size
                   + struct.pack("<III", 0, 0, 0)
                   + struct.pack("<I", npts)              # w10 = point count
                   + struct.pack("<II", 0, 0))            # w11 (patched later), w12
        indxhdrs.append(_record(b"PacketStreamIndxHdr", 4, payload))
        indxs.append(_record(b"PacketStreamIndx", 3, struct.pack("<IIII", 4, spp, 0, 0)))
        datas.append(_record(b"PacketStreamData", 1, pk))
    pss_payload = struct.pack("<II", 1, len(LEVELS)) + b"".join(indxhdrs + indxs + datas)
    pss = _record(b"PacketStreamSetHdr", 1, pss_payload)
    return _record(b"AnalysisSet", 2, struct.pack("<H", ch_index + 1) + pss)


def _build_file_block(channels_peaks: "list[list[bytes]]") -> bytes:
    """Build one file's data block: the big AnalysisSetsHdr (count + AnalysisSet per
    channel + 12-byte trailer), the 16-byte AnalysisSetsHdr footer, and 8 trailing zeros."""
    sets = [_build_channel(ci, ch) for ci, ch in enumerate(channels_peaks)]
    trailer = struct.pack("<I", 0) + b"\xff" * 8
    big = _record(b"AnalysisSetsHdr", 4, struct.pack("<I", len(channels_peaks)) + b"".join(sets) + trailer)
    footer = _record(b"AnalysisSetsHdr", 4, struct.pack("<Q", 0) + b"\xff" * 8)
    return big + footer + b"\x00" * 8


def _build_index_entry(umid: bytes, name: str, ft1: int, ft2: int, filesize: int,
                       data_offset: int, data_length: int) -> bytes:
    nb = name.encode("latin1")
    return (b"\x00" * 7 + umid + b"\x00" + struct.pack("<I", len(nb)) + nb
            + struct.pack("<QQQ", ft1, ft2, filesize) + b"\x00" * 8
            + struct.pack("<I", data_offset) + b"\x00" * 4
            + struct.pack("<I", data_length) + b"\xff" * 8)


def _patch_w11(buf: bytearray) -> None:
    """Set every PacketStreamIndxHdr's `w11` field to the absolute cache offset of its
    PacketStreamData peaks. The i-th IndxHdr pairs with the i-th PacketStreamData (both
    are emitted in the same channel/level order)."""
    ih, dt, i = [], [], 0
    while True:
        j = buf.find(b"PacketStreamIndxHdr", i)
        if j < 0:
            break
        ih.append(j + 19 + 6 + 44)  # offset of the w11 u32 within the IndxHdr payload
        i = j + 1
    i = 0
    while True:
        j = buf.find(b"PacketStreamData", i)
        if j < 0:
            break
        dt.append(j + 16 + 6)       # offset where this record's peak bytes start
        i = j + 1
    for w11_pos, peak_pos in zip(ih, dt):
        struct.pack_into("<I", buf, w11_pos, peak_pos)


def build_wavecache(entries: "list[dict]") -> bytes:
    """Assemble a complete WaveCache.wfm from per-file `entries`. Each entry dict has:
    ``channels`` (list of per-channel peak-bytes lists), ``umid`` (8 bytes), ``name``
    (str filename), ``ft`` (FILETIME u64 = file mtime), ``filesize`` (int), and optionally
    ``ft2`` (a second timestamp; defaults to ``ft``). Returns the cache bytes."""
    DATA_BASE = 92  # header(80) + 12 zero preamble
    blocks, index_rows, off = [], [], DATA_BASE
    for e in entries:
        blk = _build_file_block(e["channels"])
        index_rows.append((e["umid"], e["name"], e["ft"], e.get("ft2", e["ft"]),
                           e["filesize"], off, len(blk)))
        blocks.append(blk)
        off += len(blk)
    index_off = off
    entry_bytes = b"".join(_build_index_entry(*row) for row in index_rows)
    cahidx = b"CAHIDX" + struct.pack("<HI", 2, 4 + len(entry_bytes)) + struct.pack("<I", len(entries)) + entry_bytes

    header = bytearray(80)
    header[0:6] = b"DDZCHX"
    struct.pack_into("<H", header, 6, 1)
    struct.pack_into("<I", header, 8, 80)            # data_start
    struct.pack_into("<I", header, 12, index_off)    # index offset
    struct.pack_into("<I", header, 20, len(cahidx))  # index length

    buf = bytearray(header) + b"\x00" * 12 + b"".join(blocks) + cahidx
    _patch_w11(buf)
    return bytes(buf)


def wavecache_entry_for_wav(wav_path, *, umid: "bytes | None" = None,
                            name: "str | None" = None) -> dict:
    """Build a single build_wavecache() entry from a staged WAV: compute its per-channel
    peaks at both zoom levels, and read its UMID/mtime/size. `umid` defaults to the WAV's
    own umid chunk; `name` defaults to the WAV's basename."""
    chans, _n = _read_wav_channels(wav_path)
    channels = [[_peak_bytes(c, spp) for spp in LEVELS] for c in chans]
    if umid is None:
        from . import body_synth as B
        _sc, umid, _id2 = B.wav_clip_identity(wav_path)
    return {
        "channels": channels,
        "umid": umid,
        "name": name or os.path.basename(str(wav_path)),
        "ft": _file_filetime(wav_path),
        "filesize": os.path.getsize(wav_path),
    }


def build_wavecache_for_wavs(wav_paths) -> bytes:
    """Build a WaveCache.wfm covering every WAV in `wav_paths` (in order)."""
    return build_wavecache([wavecache_entry_for_wav(w) for w in wav_paths])


def write_wavecache(session_dir, wav_paths) -> Path:
    """Compute and write ``<session_dir>/WaveCache.wfm`` for `wav_paths`. Returns the path."""
    out = Path(session_dir) / "WaveCache.wfm"
    out.write_bytes(build_wavecache_for_wavs(wav_paths))
    return out
